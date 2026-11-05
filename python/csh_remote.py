"""csh_remote - Python client for the csh_remote APM.

Drives a running CSP Shell (csh) instance over its remote-control Unix
socket: inject commands, receive their output, interrupt long-running
commands.  No dependencies beyond the standard library.

The server side is the csh_remote APM (libcsh_remote.so), loaded into csh
with `apm load -f libcsh_remote.so -p <directory>` (csh's apm load takes a
filename search string and search paths, not a file path - see the project
README).  See the wire protocol description in
src/csh_remote_proto.h of the csh_remote project.

Example:

    from csh_remote import CshRPC

    # Connect to an already-running csh that loaded the APM:
    rpc = CshRPC("/tmp/csh-remote.sock")
    result = rpc.execute("ping 5")
    print(result.ret, result.output)
    rpc.close()

    # Or spawn a dedicated csh under a pty and drive it (replaces
    # tmux send-keys / pane-scraping automation):
    with CshRPC.spawn(echo=False) as rpc:
        print(rpc.execute("ident").output)
        rpc.interrupt()            # if something is stuck

By default, spawn() echoes remote commands in the csh terminal
("[remote] <command>" plus the command output) - set echo=False for
silent operation.
"""

from __future__ import annotations

import errno
import os
import secrets
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass

__all__ = ["CshRPC", "CommandResult", "CshRemoteError", "CshTimeout"]

DEFAULT_SOCK_PATH = "/tmp/csh-remote.sock"
APM_ENV_VAR = "CSH_REMOTE_APM"
APM_SEARCH_PATHS = (
    os.path.expanduser("~/.local/lib/csh/libcsh_remote.so"),
    # Development checkout location, relative to this file:
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "builddir", "libcsh_remote.so"),
)
SOCK_PATH_MAX = 100  # struct sockaddr_un::sun_path is 108 bytes


class CshRemoteError(RuntimeError):
    """Generic csh_remote client error."""


class CshTimeout(CshRemoteError):
    """A command did not finish in time after interrupt() was sent.

    Attributes:
        command: the command that timed out.
        output:  partial output received so far (may be empty).
    """

    def __init__(self, command: str, output: str = ""):
        self.command = command
        self.output = output
        super().__init__(f"csh command timed out: {command!r}")


@dataclass
class CommandResult:
    """Result of one remotely executed csh command."""
    command: str
    ret: int          # slash exit code (0 = SLASH_SUCCESS)
    output: str       # everything the command wrote to stdout and stderr
    duration: float   # wall-clock seconds

    @property
    def ok(self) -> bool:
        return self.ret == 0


class _Reader:
    """Buffered reader over a socket."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()

    def _fill(self) -> bool:
        chunk = self._sock.recv(4096)
        if not chunk:
            return False
        self._buf.extend(chunk)
        return True

    def read_line(self) -> str:
        """Read one '\\n'-terminated line (newline stripped)."""
        while b"\n" not in self._buf:
            if not self._fill():
                raise CshRemoteError("connection closed by csh_remote server")
        line, _, rest = self._buf.partition(b"\n")
        self._buf = bytearray(rest)
        return line.decode("utf-8", errors="replace")

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if not self._fill():
                raise CshRemoteError("connection closed by csh_remote server")
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data


class CshRPC:
    """Client for the csh_remote APM socket server inside a csh process."""

    def __init__(self, sock_path: str = DEFAULT_SOCK_PATH, connect_timeout: float = 10.0):
        if len(sock_path) >= SOCK_PATH_MAX:
            raise CshRemoteError(f"socket path too long: {sock_path!r}")
        self.sock_path = sock_path
        self._sock: socket.socket | None = None
        self._reader: _Reader | None = None
        self.connect(connect_timeout)

        # Set only by spawn(): resources owned by a dedicated csh process
        self._proc: subprocess.Popen | None = None
        self._master: int | None = None
        self._terminal_log = bytearray()
        self._cleanup_paths: list[str] = []

    # -- connection ---------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.sock_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                sock.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        self._sock = sock
        self._reader = _Reader(sock)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._send_request("BYE")
            except OSError:
                pass
            self._sock.close()
            self._sock = None
        self._teardown_spawn()

    # -- protocol -----------------------------------------------------

    def _send_request(self, line: str, payload: bytes = b"") -> None:
        assert self._sock is not None
        data = line.encode() + b"\n" + payload
        self._sock.sendall(data)

    def ping(self, timeout: float = 5.0) -> float:
        """Round-trip liveness probe; returns latency in seconds."""
        assert self._sock is not None
        t0 = time.monotonic()
        self._sock.settimeout(timeout)
        self._send_request("PING")
        line = self._reader.read_line()
        if line != "PONG":
            raise CshRemoteError(f"unexpected response to PING: {line!r}")
        self._sock.settimeout(None)
        return time.monotonic() - t0

    def interrupt(self, timeout: float = 5.0) -> None:
        """Interrupt the currently running remote command.

        Opens its own short-lived connection so it can be called while an
        execute() call is still waiting for its response.
        """
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(self.sock_path)
            sock.sendall(b"INTR\n")
            try:
                resp = sock.recv(64)
            except OSError:
                resp = b""
            if b"RET" not in resp:
                raise CshRemoteError(f"unexpected response to INTR: {resp!r}")

    def execute(self, command: str, timeout: float | None = None) -> CommandResult:
        """Execute a csh command line and return its output.

        On timeout, the command is interrupted (a best-effort INTR is
        sent) and CshTimeout is raised once the response arrives (or the
        grace period expires), so the connection stays usable afterwards.
        """
        if self._sock is None:
            raise CshRemoteError("not connected")

        payload = command.encode()
        if len(payload) > 8192:
            raise CshRemoteError("command too long (max 8192 bytes)")

        t0 = time.monotonic()
        self._sock.settimeout(timeout)
        self._send_request(f"EXEC {len(payload)}", payload + b"\n")

        try:
            result = self._read_execute_response()
        except (socket.timeout, TimeoutError):
            # Interrupt the running command, then keep reading so the
            # protocol stream stays in sync for future requests.
            self.interrupt()
            self._sock.settimeout(10.0)  # grace period for the response
            try:
                result = self._read_execute_response()
            except (socket.timeout, TimeoutError, CshRemoteError):
                raise CshTimeout(command) from None
            raise CshTimeout(command, result[1]) from None
        finally:
            if self._sock is not None:
                self._sock.settimeout(None)
            self._drain_terminal()

        return CommandResult(command=command, ret=result[0], output=result[1],
                             duration=time.monotonic() - t0)

    def _read_execute_response(self) -> tuple[int, str]:
        line = self._reader.read_line()
        if line.startswith("ERR"):
            raise CshRemoteError(line)
        if not line.startswith("RET "):
            raise CshRemoteError(f"unexpected response: {line!r}")
        ret = int(line[4:])

        out_line = self._reader.read_line()
        if not out_line.startswith("OUT "):
            raise CshRemoteError(f"unexpected response: {out_line!r}")
        length = int(out_line[4:])
        output = self._reader.read_exact(length).decode("utf-8", errors="replace")

        if self._reader.read_exact(1) != b"\n":
            raise CshRemoteError("malformed EXEC response framing")
        return ret, output

    # -- spawning a dedicated csh --------------------------------------

    @classmethod
    def spawn(
        cls,
        csh: str = "csh",
        init_file: str | None = None,
        apm_path: str | None = None,
        sock_path: str | None = None,
        echo: bool = True,
        connect_timeout: float = 15.0,
        extra_env: dict[str, str] | None = None,
    ) -> "CshRPC":
        """Start a dedicated csh under a pty with the APM loaded, and
        return a connected client.  Use as a context manager; teardown
        exits csh, removes the socket and any generated init file.

        If init_file is given, it must load the APM itself (it typically
        also initializes CSP interfaces for the test setup).
        """
        import pty

        if sock_path is None:
            sock_path = f"/tmp/csh-remote-{os.getpid()}-{secrets.token_hex(4)}.sock"

        if init_file is None:
            apm = apm_path or cls._find_apm()
            cls._validate_apm_name(apm)
            fd, init_file = tempfile.mkstemp(suffix=".csh", prefix="csh-remote-init-")
            with os.fdopen(fd, "w") as f:
                # NB: "apm load <path>" does NOT load that path - csh's
                # apm load command only takes -f (filename search string)
                # and -p (';'-separated search paths).  Using -f/-p scoped
                # to the APM's own directory also avoids picking up
                # unrelated (possibly stale) libcsh_*.so APMs.
                f.write(f"apm load -f {os.path.basename(apm)} -p {os.path.dirname(apm) or '.'}\n")
                # The APM does not listen when loaded; start it explicitly
                # on our private socket path.
                f.write(f"remote start {sock_path}\n")
            generated_init = init_file
        else:
            generated_init = None

        env = dict(os.environ)
        env["CSH_REMOTE_SOCK"] = sock_path
        env["CSH_REMOTE_ECHO"] = "1" if echo else "0"
        if extra_env:
            env.update(extra_env)

        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                [csh, "-i", init_file],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(slave)

        client = None
        try:
            client = cls.__new__(cls)  # bypass __init__ (we connect ourselves)
            client.sock_path = sock_path
            client._sock = None
            client._reader = None
            client._proc = proc
            client._master = master
            client._terminal_log = bytearray()
            client._cleanup_paths = [sock_path] + ([generated_init] if generated_init else [])

            deadline = time.monotonic() + connect_timeout
            while True:
                client._drain_terminal()
                if proc.poll() is not None:
                    client._drain_terminal()
                    tail = bytes(client._terminal_log[-2000:]).decode("utf-8", "replace")
                    raise CshRemoteError(f"csh exited during startup:\n{tail}")
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(sock_path)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    sock.close()
                    if time.monotonic() >= deadline:
                        raise CshRemoteError(
                            f"csh_remote socket {sock_path} did not appear within "
                            f"{connect_timeout}s"
                        ) from None
                    time.sleep(0.1)
            client._sock = sock
            client._reader = _Reader(sock)
            return client
        except Exception:
            if client is not None:
                client._teardown_spawn()
                if client._sock is not None:
                    client._sock.close()
            else:
                os.close(master)
                proc.kill()
                proc.wait()
            raise

    @staticmethod
    def _find_apm() -> str:
        env = os.environ.get(APM_ENV_VAR)
        if env:
            return env
        for path in APM_SEARCH_PATHS:
            if os.path.isfile(path):
                return os.path.abspath(path)
        raise CshRemoteError(
            f"cannot find libcsh_remote.so; pass apm_path= or set ${APM_ENV_VAR}"
        )

    @staticmethod
    def _validate_apm_name(apm: str) -> None:
        """csh's 'apm load' only accepts libraries whose file name contains
        'libcsh_' and '.so' - anything else is silently skipped by the
        directory search."""
        base = os.path.basename(apm)
        if "libcsh_" not in base or ".so" not in base:
            raise CshRemoteError(
                f"APM file name must contain 'libcsh_' and '.so' for csh's "
                f"'apm load' to find it (got {base!r})"
            )

    # -- pty helpers (only when spawned) --------------------------------

    def _drain_terminal(self) -> None:
        if self._master is None:
            return
        import select

        while True:
            ready, _, _ = select.select([self._master], [], [], 0)
            if not ready:
                return
            try:
                chunk = os.read(self._master, 4096)
            except OSError:
                return
            if not chunk:
                return
            self._terminal_log.extend(chunk)
            # Keep the log bounded
            if len(self._terminal_log) > 256 * 1024:
                del self._terminal_log[:128 * 1024]

    def read_terminal(self) -> str:
        """Return (and clear) everything captured from the csh terminal
        since the last call.  Only meaningful for spawn()-ed instances;
        this is what a human sitting at the terminal would see."""
        self._drain_terminal()
        out = bytes(self._terminal_log).decode("utf-8", errors="replace")
        self._terminal_log.clear()
        return out

    def _teardown_spawn(self) -> None:
        proc, self._proc = self._proc, None
        master, self._master = self._master, None
        if proc is None:
            return

        if master is not None:
            try:
                os.write(master, b"exit\n")
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass

        for path in self._cleanup_paths:
            try:
                os.unlink(path)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    pass
        self._cleanup_paths = []

    # -- context manager -------------------------------------------------

    def __enter__(self) -> "CshRPC":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
