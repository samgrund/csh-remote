/*
 * csh_remote - remote control APM for CSP Shell (csh)
 *
 * Exposes a running csh instance over a Unix domain socket so that
 * external programs (e.g. Python automation) can inject commands and
 * receive their output without scraping a terminal/log.
 *
 * See csh_remote_proto.h for the wire protocol.
 *
 * Loaded with:  apm load -f libcsh_remote.so -p <dir>
 *
 * The APM is inert when loaded: it does not listen on anything until
 * the user (or an init file) runs
 *
 *     remote start [socket path]
 *
 * and stops again with
 *
 *     remote stop
 *
 * 'remote start' without an argument uses $CSH_REMOTE_SOCK (read when
 * the command runs), or /tmp/csh-remote.sock.  Configuration:
 *   CSH_REMOTE_SOCK   default socket path for 'remote start'
 *   CSH_REMOTE_ECHO   1 = tee remote output to the interactive terminal
 *                                          (default), 0 = silent
 *
 * The socket server executes commands with its own `struct slash`
 * instance (the same approach used by csh's embedded Python), captures
 * everything the command writes to stdout/stderr, and returns it to the
 * client.  With echo enabled the captured output is also replayed to the
 * interactive terminal live, prefixed by "[remote] <command>".
 */

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#include <slash/slash.h>
#include <apm/apm.h>

#include "csh_remote_proto.h"

#define CSH_REMOTE_VERSION "0.2.0"

/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */

/* Last/current socket path (empty until the first 'remote start') */
static char sock_path[CRP_SOCK_PATH_MAX];
static int listen_fd = -1;
static volatile int server_running = 0;
static volatile int echo_enabled = 1;
static volatile int sock_owned = 0;

static pthread_t server_thread;
static int server_thread_valid = 0;
/* Serializes start/stop of the listener (may run from the interactive
 * thread and from remote connections at the same time). */
static pthread_mutex_t sock_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t exec_lock = PTHREAD_MUTEX_INITIALIZER;

/* The slash context used for remote executions.  Never touches the
 * interactive terminal: fd_read/fd_write are never used because we call
 * slash_execute() directly, and waitfunc is replaced with a version that
 * polls our interrupt flag instead of selecting on stdin. */
static struct slash remote_slash;
static char remote_line_buf[CRP_MAX_CMD_LEN];
static char remote_hist_buf[8192];

/* Nonzero while a remote command is executing (for INTR handling) */
static volatile int exec_active = 0;
/* Command currently executing (for status display / echo prefix) */
static char cur_cmd[CRP_MAX_CMD_LEN];

/* Statistics */
static uint64_t stat_exec = 0, stat_errors = 0, stat_intr = 0, stat_clients = 0;

void libinfo(void);

/* ------------------------------------------------------------------ */
/* Wire helpers                                                        */
/* ------------------------------------------------------------------ */

struct conn_buf {
	int fd;
	unsigned char buf[4096];
	size_t pos, len;
};

/* Read one newline-terminated line.  Returns the line length (nul
 * terminated, '\n' stripped), 0 on EOF, -1 on error or overflow. */
static int conn_read_line(struct conn_buf *cb, char *line, size_t maxlen)
{
	size_t n = 0;

	for (;;) {
		if (cb->pos == cb->len) {
			ssize_t r = recv(cb->fd, cb->buf, sizeof(cb->buf), 0);
			if (r < 0) {
				if (errno == EINTR)
					continue;
				return -1;
			}
			if (r == 0)
				return 0; /* EOF */
			cb->len = (size_t)r;
			cb->pos = 0;
		}

		while (cb->pos < cb->len) {
			unsigned char c = cb->buf[cb->pos++];
			if (c == '\n') {
				line[n] = '\0';
				return (int)n;
			}
			if (c == '\0')
				continue; /* reject embedded nul bytes */
			if (n + 1 >= maxlen)
				return -1;
			line[n++] = (char)c;
		}
	}
}

/* Read exactly len bytes.  0 on success, -1 on error/EOF. */
static int conn_read_exact(struct conn_buf *cb, void *data, size_t len)
{
	size_t got = 0;
	char *p = data;

	while (got < len) {
		if (cb->pos < cb->len) {
			size_t take = cb->len - cb->pos;
			if (take > len - got)
				take = len - got;
			memcpy(p + got, cb->buf + cb->pos, take);
			cb->pos += take;
			got += take;
			continue;
		}
		ssize_t r = recv(cb->fd, p + got, len - got, 0);
		if (r < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		if (r == 0)
			return -1;
		got += (size_t)r;
	}

	return 0;
}

/* Robust, SIGPIPE-safe socket write */
static int conn_send(int fd, const void *data, size_t len)
{
	const char *p = data;

	while (len > 0) {
		ssize_t w = send(fd, p, len, MSG_NOSIGNAL);
		if (w < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		p += w;
		len -= (size_t)w;
	}

	return 0;
}

static int conn_send_str(int fd, const char *s)
{
	return conn_send(fd, s, strlen(s));
}

/* ------------------------------------------------------------------ */
/* Command execution with output capture                               */
/* ------------------------------------------------------------------ */

/* Replacement wait function: sleeps in small slices and honors the
 * interrupt flag.  Never touches the interactive terminal's stdin (the
 * stock slash_wait_select would steal input from the interactive loop). */
static int remote_waitfunc(void *slashp, unsigned int ms)
{
	struct slash *s = slashp;

	while (ms > 0) {
		if (s->signal)
			return -EINTR;
		unsigned int slice = (ms > 20) ? 20 : ms;
		usleep(slice * 1000);
		ms -= slice;
	}

	return s->signal ? -EINTR : 0;
}

struct drain_ctx {
	int fd;      /* pipe read end */
	int echo_fd; /* real stdout, or -1 when echo is off */
	char *buf;   /* accumulated output (grows) */
	size_t len;
	size_t cap;
};

static int drain_append(struct drain_ctx *ctx, const char *data, size_t n)
{
	if (ctx->len + n > ctx->cap) {
		size_t ncap = ctx->cap ? ctx->cap : 4096;
		while (ctx->len + n > ncap)
			ncap *= 2;
		char *nb = realloc(ctx->buf, ncap);
		if (!nb)
			return -1;
		ctx->buf = nb;
		ctx->cap = ncap;
	}
	memcpy(ctx->buf + ctx->len, data, n);
	ctx->len += n;
	return 0;
}

/* Drains the capture pipe into the response buffer and, when echo is
 * enabled, replays every chunk live to the interactive terminal. */
static void *drain_thread(void *arg)
{
	struct drain_ctx *ctx = arg;
	char chunk[4096];

	for (;;) {
		ssize_t n = read(ctx->fd, chunk, sizeof(chunk));
		if (n < 0) {
			if (errno == EINTR)
				continue;
			break;
		}
		if (n == 0)
			break; /* EOF: all write ends closed */

		if (drain_append(ctx, chunk, (size_t)n) < 0)
			break; /* out of memory: drop further output */

		if (ctx->echo_fd >= 0) {
			size_t off = 0;
			while (off < (size_t)n) {
				ssize_t w = write(ctx->echo_fd, chunk + off, (size_t)n - off);
				if (w < 0) {
					if (errno == EINTR)
						continue;
					break; /* terminal gone: keep capturing */
				}
				off += (size_t)w;
			}
		}
	}

	return NULL;
}

/* Execute one command line with output captured.
 *
 * Returns 0 on success (with *ret = command exit code, and *out / *outlen
 * set to a malloc'd output buffer to be freed by the caller) or -1 on
 * internal failure (capture machinery, not the command itself).
 */
static int do_execute(const char *cmd, int *ret, char **out, size_t *outlen)
{
	*out = NULL;
	*outlen = 0;

	struct drain_ctx ctx = {
		.fd = -1,
		.echo_fd = -1,
		.len = 0,
		.cap = 0,
	};
	pthread_t drainer;
	int have_drainer = 0;
	int saved_out = -1, saved_err = -1;
	int pfd[2] = {-1, -1};

	pthread_mutex_lock(&exec_lock);

	exec_active = 1;
	__atomic_fetch_add(&stat_exec, 1, __ATOMIC_RELAXED);
	strncpy(cur_cmd, cmd, sizeof(cur_cmd) - 1);
	cur_cmd[sizeof(cur_cmd) - 1] = '\0';

	/* Flush whatever is pending, then announce the command on the real
	 * terminal (before fd 1 is redirected, so this never enters the
	 * captured output). */
	fflush(stdout);
	fflush(stderr);
	if (echo_enabled)
		dprintf(STDOUT_FILENO, "\n[remote] %s\n", cmd);

	saved_out = dup(STDOUT_FILENO);
	saved_err = dup(STDERR_FILENO);
	if (saved_out < 0 || saved_err < 0 || pipe(pfd) < 0)
		goto out;

	/* Redirect stdout and stderr into the capture pipe */
	dup2(pfd[1], STDOUT_FILENO);
	dup2(pfd[1], STDERR_FILENO);

	ctx.fd = pfd[0];
	ctx.echo_fd = echo_enabled ? saved_out : -1;
	if (pthread_create(&drainer, NULL, drain_thread, &ctx) == 0)
		have_drainer = 1;

	*ret = slash_execute(&remote_slash, (char *)cmd);

	/* Flush the redirected streams, restore the real fds and let the
	 * drain thread see EOF. */
	fflush(stdout);
	fflush(stderr);
	dup2(saved_out, STDOUT_FILENO);
	dup2(saved_err, STDERR_FILENO);

out:
	/* Close the pipe write end first so the drain thread sees EOF, then
	 * wait for it to finish echoing before closing the terminal fds it
	 * writes to (closing them earlier would cut the echo short). */
	if (pfd[1] >= 0)
		close(pfd[1]);
	if (have_drainer)
		pthread_join(drainer, NULL);
	if (saved_out >= 0)
		close(saved_out);
	if (saved_err >= 0)
		close(saved_err);
	if (pfd[0] >= 0)
		close(pfd[0]);

	if (*ret != SLASH_SUCCESS)
		__atomic_fetch_add(&stat_errors, 1, __ATOMIC_RELAXED);

	exec_active = 0;
	cur_cmd[0] = '\0';
	pthread_mutex_unlock(&exec_lock);

	if (pfd[1] < 0) {
		/* Internal failure: capture never started */
		free(ctx.buf);
		return -1;
	}

	*out = ctx.buf ? ctx.buf : calloc(1, 1);
	*outlen = ctx.len;
	return 0;
}

/* ------------------------------------------------------------------ */
/* Connection handling                                                 */
/* ------------------------------------------------------------------ */

static void handle_exec(struct conn_buf *cb, size_t len)
{
	char cmd[CRP_MAX_CMD_LEN];
	char hdr[64];

	if (len >= sizeof(cmd)) {
		conn_send_str(cb->fd, "ERR command too long\n");
		return;
	}
	if (conn_read_exact(cb, cmd, len) < 0) {
		conn_send_str(cb->fd, "ERR truncated EXEC payload\n");
		return;
	}
	/* slash_execute() needs a nul-terminated line */
	cmd[len] = '\0';
	/* Consume the single trailing newline after the payload */
	char nl;
	if (conn_read_exact(cb, &nl, 1) < 0 || nl != '\n') {
		conn_send_str(cb->fd, "ERR malformed EXEC payload\n");
		return;
	}

	int ret = 0;
	char *out = NULL;
	size_t outlen = 0;
	if (do_execute(cmd, &ret, &out, &outlen) < 0) {
		conn_send_str(cb->fd, "ERR internal capture failure\n");
		return;
	}

	snprintf(hdr, sizeof(hdr), "RET %d\nOUT %zu\n", ret, outlen);
	if (conn_send_str(cb->fd, hdr) < 0 ||
	    (outlen > 0 && conn_send(cb->fd, out, outlen) < 0) ||
	    conn_send_str(cb->fd, "\n") < 0) {
		/* Client went away mid-response; nothing to do */
	}
	free(out);
}

static void *conn_thread(void *arg)
{
	int fd = (int)(intptr_t)arg;
	struct conn_buf cb = {.fd = fd, .pos = 0, .len = 0};
	char line[CRP_MAX_LINE_LEN + 16];

	__atomic_fetch_add(&stat_clients, 1, __ATOMIC_RELAXED);

	for (;;) {
		int n = conn_read_line(&cb, line, sizeof(line));
		if (n <= 0)
			break; /* EOF, error, or oversized line */

		if (strncmp(line, "EXEC ", 5) == 0) {
			char *end = NULL;
			unsigned long long len = strtoull(line + 5, &end, 10);
			if (end == line + 5 || *end != '\0' || len > CRP_MAX_CMD_LEN) {
				conn_send_str(fd, "ERR malformed EXEC request\n");
				break;
			}
			handle_exec(&cb, (size_t)len);
		} else if (strcmp(line, "INTR") == 0) {
			/* Interrupt whatever command is currently executing
			 * (from any connection).  Mirrors what slash_sigint()
			 * does for the busy interactive instance, minus the
			 * terminal refresh we must not perform. */
			if (exec_active) {
				remote_slash.signal = SIGINT;
				__atomic_fetch_add(&stat_intr, 1, __ATOMIC_RELAXED);
			}
			conn_send_str(fd, "RET 0\n");
		} else if (strcmp(line, "PING") == 0) {
			conn_send_str(fd, "PONG\n");
		} else if (strcmp(line, "BYE") == 0) {
			conn_send_str(fd, "RET 0\n");
			break;
		} else {
			conn_send_str(fd, "ERR unknown request\n");
			break;
		}
	}

	close(fd);
	return NULL;
}

static void *server_loop(void *arg)
{
	(void)arg;

	while (server_running) {
		int fd = accept(listen_fd, NULL, NULL);
		if (fd < 0) {
			if (errno == EINTR)
				continue;
			if (errno == EINVAL || errno == EBADF)
				break; /* listener shut down under us */
			/* Transient (EMFILE, ENFILE, ENOMEM, ...):
			 * do not kill the server, back off and retry. */
			usleep(100 * 1000);
			continue;
		}

		pthread_t t;
		if (pthread_create(&t, NULL, conn_thread, (void *)(intptr_t)fd) == 0) {
			pthread_detach(t);
		} else {
			close(fd);
		}
	}

	return NULL;
}

/* ------------------------------------------------------------------ */
/* Socket setup                                                        */
/* ------------------------------------------------------------------ */

static void sock_cleanup(void)
{
	if (sock_owned) {
		unlink(sock_path);
		sock_owned = 0;
	}
}

static int setup_socket(void)
{
	struct sockaddr_un addr;

	listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (listen_fd < 0)
		return -errno;

	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", sock_path);

	/* Refuse to shadow a live server, clean up a stale socket file */
	int probe = socket(AF_UNIX, SOCK_STREAM, 0);
	if (probe >= 0) {
		int rc = connect(probe, (struct sockaddr *)&addr, sizeof(addr));
		close(probe);
		if (rc == 0) {
			close(listen_fd);
			listen_fd = -1;
			return -EADDRINUSE;
		}
	}
	unlink(sock_path);

	if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		int e = errno;
		close(listen_fd);
		listen_fd = -1;
		return -e;
	}

	chmod(sock_path, 0600);
	sock_owned = 1;

	if (listen(listen_fd, 8) < 0) {
		int e = errno;
		sock_cleanup();
		close(listen_fd);
		listen_fd = -1;
		return -e;
	}

	return 0;
}

/* ------------------------------------------------------------------ */
/* Listener start/stop (driven by the 'remote start'/'remote stop'     */
/* commands)                                                           */
/* ------------------------------------------------------------------ */

/* Caller must hold sock_lock.  Returns 0 on success, negative errno on
 * failure (listener state unchanged on failure). */
static int start_listen_locked(const char *path)
{
	int rc;

	if (path[0] != '/' || strlen(path) >= sizeof(sock_path))
		return -EINVAL;

	snprintf(sock_path, sizeof(sock_path), "%s", path);

	rc = setup_socket();
	if (rc < 0)
		return rc;

	server_running = 1;
	int prc = pthread_create(&server_thread, NULL, server_loop, NULL);
	if (prc != 0) {
		sock_cleanup();
		close(listen_fd);
		listen_fd = -1;
		server_running = 0;
		return -prc;
	}
	server_thread_valid = 1;

	return 0;
}

/* Caller must hold sock_lock.  Never fails; a no-op when not running. */
static void stop_listen_locked(void)
{
	if (!server_running)
		return;

	server_running = 0;

	/* Wake the accept() loop (shutdown on a listening socket makes a
	 * blocked accept() return on Linux), then wait for it to exit. */
	if (listen_fd >= 0)
		shutdown(listen_fd, SHUT_RDWR);
	if (server_thread_valid) {
		pthread_join(server_thread, NULL);
		server_thread_valid = 0;
	}
	if (listen_fd >= 0) {
		close(listen_fd);
		listen_fd = -1;
	}
	sock_cleanup(); /* unlink the socket file if we own it */
}

/* Resolve the path for 'remote start' with no argument: $CSH_REMOTE_SOCK
 * (read at command time) or the built-in default. */
static const char *default_sock_path(void)
{
	const char *path = getenv(CRP_ENV_SOCK);
	return (path && *path) ? path : CRP_DEFAULT_SOCK_PATH;
}

/* ------------------------------------------------------------------ */
/* APM entry points                                                    */
/* ------------------------------------------------------------------ */

/* apm_init() is called by libapm_csh's default libmain() after the
 * slash commands below have been registered.  Loading is deliberately
 * inert: nothing is listening until 'remote start [path]' is run. */
int apm_init(void)
{
	const char *echo = getenv(CRP_ENV_ECHO);

	if (echo)
		echo_enabled = (atoi(echo) != 0);

	/* Live terminal echo requires line-buffered stdio while fd 1 points
	 * at the capture pipe (pipes default to full buffering).  On an
	 * interactive terminal stdout is already line buffered, so this
	 * changes nothing for the interactive experience. */
	setvbuf(stdout, NULL, _IOLBF, 0);

	slash_create_static(&remote_slash, remote_line_buf, sizeof(remote_line_buf),
			    remote_hist_buf, sizeof(remote_hist_buf));
	remote_slash.waitfunc = remote_waitfunc;

	atexit(sock_cleanup);

	printf("csh_remote " CSH_REMOTE_VERSION " loaded (not listening)\n");
	printf("Use 'remote start [%s]' to begin accepting connections\n",
	       default_sock_path());
	return 0;
}

void libinfo(void)
{
	printf("csh_remote " CSH_REMOTE_VERSION ": %s%s, echo %s, "
	       "%llu commands executed (%llu failed), %llu interrupts, %llu clients\n",
	       server_running ? "listening on " : "not listening",
	       server_running ? sock_path : "",
	       echo_enabled ? "on" : "off",
	       (unsigned long long)__atomic_load_n(&stat_exec, __ATOMIC_RELAXED),
	       (unsigned long long)__atomic_load_n(&stat_errors, __ATOMIC_RELAXED),
	       (unsigned long long)__atomic_load_n(&stat_intr, __ATOMIC_RELAXED),
	       (unsigned long long)__atomic_load_n(&stat_clients, __ATOMIC_RELAXED));
}

/* ------------------------------------------------------------------ */
/* Interactive commands                                                */
/* ------------------------------------------------------------------ */

static int remote_status_cmd(struct slash *slash)
{
	slash_printf(slash, "csh_remote " CSH_REMOTE_VERSION "\n");
	if (server_running)
		slash_printf(slash, "socket: %s (listening)\n", sock_path);
	else if (sock_path[0] != '\0')
		slash_printf(slash, "socket: %s (not listening)\n", sock_path);
	else
		slash_printf(slash, "socket: not started (use 'remote start [path]')\n");
	slash_printf(slash, "echo: %s\n", echo_enabled ? "on" : "off");
	/* Don't report ourselves as the executing command - a remotely
	 * run 'remote status' would always show itself. */
	if (exec_active && strcmp(cur_cmd, "remote status") != 0)
		slash_printf(slash, "currently executing: %s\n", cur_cmd);
	slash_printf(slash, "commands executed: %llu (%llu failed), "
			    "interrupts: %llu, clients: %llu\n",
		     (unsigned long long)__atomic_load_n(&stat_exec, __ATOMIC_RELAXED),
		     (unsigned long long)__atomic_load_n(&stat_errors, __ATOMIC_RELAXED),
		     (unsigned long long)__atomic_load_n(&stat_intr, __ATOMIC_RELAXED),
		     (unsigned long long)__atomic_load_n(&stat_clients, __ATOMIC_RELAXED));
	return SLASH_SUCCESS;
}

static int remote_start_cmd(struct slash *slash)
{
	/* argv[0] is a separator artifact in this slash version; the real
	 * arguments start at argv[1] (see e.g. the echo builtin). */
	if (slash->argc > 3) {
		slash_printf(slash, "usage: remote start [socket path]\n");
		return SLASH_EUSAGE;
	}
	const char *path = (slash->argc >= 2) ? slash->argv[1]
					     : default_sock_path();

	pthread_mutex_lock(&sock_lock);

	if (server_running && strcmp(sock_path, path) == 0) {
		pthread_mutex_unlock(&sock_lock);
		slash_printf(slash, "already listening on %s\n", sock_path);
		return SLASH_SUCCESS;
	}

	/* Switching to a different path (or starting fresh): tear the old
	 * listener down first so we never leak sockets or threads. */
	if (server_running) {
		slash_printf(slash, "stopping %s...\n", sock_path);
		stop_listen_locked();
	}

	int rc = start_listen_locked(path);
	pthread_mutex_unlock(&sock_lock);

	if (rc < 0) {
		if (rc == -EADDRINUSE)
			slash_printf(slash,
				     "remote start: another server is already "
				     "listening on %s\n", path);
		else if (rc == -EINVAL)
			slash_printf(slash,
				     "remote start: socket path must be an "
				     "absolute path shorter than %zu "
				     "characters (got \"%s\")\n",
				     sizeof(sock_path), path);
		else
			slash_printf(slash, "remote start: cannot listen on %s: %s\n",
				     path, strerror(-rc));
		return SLASH_EINVAL;
	}

	slash_printf(slash, "listening on %s\n", sock_path);
	return SLASH_SUCCESS;
}

static int remote_stop_cmd(struct slash *slash)
{
	char old_path[CRP_SOCK_PATH_MAX];

	pthread_mutex_lock(&sock_lock);
	int was_running = server_running;
	snprintf(old_path, sizeof(old_path), "%s", sock_path);
	stop_listen_locked();
	pthread_mutex_unlock(&sock_lock);

	if (!was_running) {
		slash_printf(slash, "not listening%s\n",
			     old_path[0] ? " (already stopped)" : "");
		return SLASH_SUCCESS;
	}

	slash_printf(slash, "stopped listening on %s\n", old_path);
	/* Note: already-connected clients keep their connections until they
	 * close them; the socket file is removed so no new ones arrive. */
	return SLASH_SUCCESS;
}

static int remote_echo_cmd(struct slash *slash)
{
	/* argv[0] is a separator artifact in this slash version; the real
	 * arguments start at argv[1] (see e.g. the echo builtin). */
	if (slash->argc < 2) {
		slash_printf(slash, "usage: remote echo <on|off>\n");
		return SLASH_EUSAGE;
	}

	for (int i = 1; i < slash->argc; i++) {
		if (strcmp(slash->argv[i], "on") == 0)
			echo_enabled = 1;
		else if (strcmp(slash->argv[i], "off") == 0)
			echo_enabled = 0;
		else {
			slash_printf(slash, "remote echo: expected 'on' or 'off', got '%s'\n",
				     slash->argv[i]);
			return SLASH_EUSAGE;
		}
	}

	slash_printf(slash, "remote echo is now %s\n", echo_enabled ? "on" : "off");
	return SLASH_SUCCESS;
}

slash_command_sub(remote, status, remote_status_cmd, "", "Show csh_remote server status")
slash_command_sub(remote, start, remote_start_cmd, "[socket path]",
		  "Start listening (default path: $CSH_REMOTE_SOCK or /tmp/csh-remote.sock)")
slash_command_sub(remote, stop, remote_stop_cmd, "", "Stop listening and remove the socket file")
slash_command_sub(remote, echo, remote_echo_cmd, "<on|off>",
		  "Enable/disable echoing remote command output to this terminal")
