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

## Command evaluation (corpus)

csh's exit code only signals that a command parsed and ran - most
operational failures (a ping with no reply, an unknown parameter, ...)
still return SLASH_SUCCESS.  execute() therefore classifies the result
against a command corpus of success/failure markers (python/corpus.toml
next to this module, or $CSH_REMOTE_CORPUS):

    r = rpc.execute("ping -n 1 -t 1000")
    r.ret     # 0 - the command ran
    r.status  # "failure" - corpus saw "No reply"
    r.ok      # False

status is "success" / "failure" / "unknown" (unknown = no corpus entry
for the command) and ok is corpus-aware.  Pass eval=False for the raw
transport semantics (ok == ret == 0).  Extend the corpus by editing the
TOML; load_corpus() validates and (re)loads it.
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

try:
    import tomllib  # Python >= 3.11
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # corpus evaluation unavailable

__all__ = ["CshRPC", "CommandResult", "CshRemoteError", "CshTimeout",
           "load_corpus"]

DEFAULT_SOCK_PATH = "/tmp/csh-remote.sock"
APM_ENV_VAR = "CSH_REMOTE_APM"
APM_SEARCH_PATHS = (
    os.path.expanduser("~/.local/lib/csh/libcsh_remote.so"),
    # Development checkout location, relative to this file:
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "builddir", "libcsh_remote.so"),
)
SOCK_PATH_MAX = 100  # struct sockaddr_un::sun_path is 108 bytes

CORPUS_ENV_VAR = "CSH_REMOTE_CORPUS"
_CORPUS_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "corpus.toml")


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
    """Result of one remotely executed csh command.

    ret is the raw slash exit code: 0 means the command parsed and ran,
    NOT that it succeeded operationally (csh returns SLASH_SUCCESS for
    most operational failures - a failed ping still exits 0).  With
    evaluation enabled (execute(eval=True), the default) the result is
    additionally classified against the command corpus:

      status: "success" | "failure" | "unknown"
              "unknown" = no corpus entry for this command (or eval off)
      reason: why, e.g. "failure marker 'No reply'"
      ok:     corpus-aware - False when status is "failure"; otherwise
              (unknown, or eval off) it falls back to ret == 0.
    """
    command: str
    ret: int          # slash exit code (0 = SLASH_SUCCESS)
    output: str       # everything the command wrote to stdout and stderr
    duration: float   # wall-clock seconds
    status: str = "unknown"
    reason: str = ""

    @property
    def ok(self) -> bool:
        if self.status == "failure":
            return False
        return self.ret == 0


# ------------------------------------------------------------------
# Command corpus: success/failure markers per command (corpus.toml)
# ------------------------------------------------------------------

@dataclass(frozen=True)
class _CorpusEntry:
    """One corpus command and its output markers."""
    names: tuple[tuple[str, ...], ...]  # each name split into words
    success: tuple[str, ...]
    failure: tuple[str, ...]


@dataclass(frozen=True)
class _Corpus:
    global_failure: tuple[str, ...]
    entries: tuple[_CorpusEntry, ...]


# Loaded lazily on first evaluation; replaced by load_corpus().
_corpus: _Corpus | None = None


def _marker_list(table: dict, key: str, where: str, path: str) -> tuple[str, ...]:
    if key not in table:
        return ()
    markers = table[key]
    if (not isinstance(markers, list)
            or not all(isinstance(m, str) and m for m in markers)):
        raise CshRemoteError(
            f"corpus {path}: {where}: {key!r} must be a list of "
            "non-empty strings")
    return tuple(markers)


def load_corpus(path: str | None = None) -> _Corpus:
    """Load, validate and cache the command corpus, replacing any
    previously cached one.  Returns the parsed corpus.

    path defaults to $CSH_REMOTE_CORPUS or python/corpus.toml next to
    this module.  A malformed corpus raises CshRemoteError naming the
    file and the offending key - it never degrades to silently passing
    commands as successful.
    """
    global _corpus

    if tomllib is None:
        raise CshRemoteError(
            "command corpus requires Python 3.11+ (tomllib); "
            "use execute(..., eval=False) or upgrade Python")
    if path is None:
        path = os.environ.get(CORPUS_ENV_VAR) or _CORPUS_DEFAULT_PATH

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise CshRemoteError(f"cannot read corpus {path}: {e}") from None
    except tomllib.TOMLDecodeError as e:
        raise CshRemoteError(f"malformed corpus {path}: {e}") from None

    def no_unknown_keys(table: dict, allowed: set, where: str) -> None:
        unknown = set(table) - allowed
        if unknown:
            raise CshRemoteError(
                f"corpus {path}: {where}: unknown key(s): "
                f"{', '.join(sorted(unknown))}")

    no_unknown_keys(data, {"version", "global", "command"}, "document")
    if data.get("version") != 1:
        raise CshRemoteError(
            f"corpus {path}: version must be 1 "
            f"(got {data.get('version')!r})")

    global_tbl = data.get("global", {})
    if not isinstance(global_tbl, dict):
        raise CshRemoteError(f"corpus {path}: [global] must be a TOML table")
    no_unknown_keys(global_tbl, {"failure"}, "[global]")
    global_failure = _marker_list(global_tbl, "failure", "[global]", path)

    raw_entries = data.get("command", [])
    if not isinstance(raw_entries, list):
        raise CshRemoteError(
            f"corpus {path}: [[command]] must be an array of tables")

    entries: list = []
    for i, tbl in enumerate(raw_entries):
        where = f"[[command]] #{i + 1}"
        if not isinstance(tbl, dict):
            raise CshRemoteError(
                f"corpus {path}: {where} must be a TOML table")
        no_unknown_keys(tbl, {"names", "success", "failure"}, where)

        names_raw = tbl.get("names")
        if (not isinstance(names_raw, list) or not names_raw
                or not all(isinstance(n, str) and n.strip()
                           for n in names_raw)):
            raise CshRemoteError(
                f"corpus {path}: {where}: 'names' must be a non-empty "
                "list of command name strings")
        names = tuple(tuple(n.split()) for n in names_raw)

        success = _marker_list(tbl, "success", where, path)
        failure = _marker_list(tbl, "failure", where, path)
        if not success and not failure:
            raise CshRemoteError(
                f"corpus {path}: {where} needs at least one success or "
                "failure marker")
        entries.append(_CorpusEntry(names=names, success=success,
                                    failure=failure))

    corpus = _Corpus(global_failure=global_failure, entries=tuple(entries))
    _corpus = corpus
    return corpus


def _get_corpus() -> _Corpus:
    """The cached corpus, loading it (default path or env override) on
    first use."""
    global _corpus
    if _corpus is None:
        load_corpus()
    return _corpus


def _find_entry(corpus: _Corpus, tokens: list) -> "_CorpusEntry | None":
    """Corpus entry for a command, longest name match wins ("remote
    status" beats a hypothetical "remote" entry)."""
    best = None
    best_len = 0
    for entry in corpus.entries:
        for name in entry.names:
            if len(name) > best_len and tuple(tokens[:len(name)]) == name:
                best = entry
                best_len = len(name)
    return best


def _evaluate(command: str, ret: int, output: str) -> tuple:
    """Classify a finished command against the corpus.

    Returns (status, reason); status is one of "success", "failure",
    "unknown".  Order: exit code, then failure markers (entry's plus
    global), then success markers, then "unknown" for commands with no
    corpus entry.
    """
    corpus = _get_corpus()
    entry = _find_entry(corpus, command.split())

    if ret != 0:
        return "failure", f"exit code {ret}"

    failure_markers = corpus.global_failure
    if entry is not None:
        failure_markers = failure_markers + entry.failure
    for marker in failure_markers:
        if marker in output:
            return "failure", f"failure marker {marker!r}"

    if entry is not None:
        for marker in entry.success:
            if marker in output:
                return "success", f"success marker {marker!r}"
        if entry.success:
            expected = ", ".join(repr(m) for m in entry.success)
            return "failure", f"no success marker matched (expected {expected})"
        return "success", "no failure marker matched"

    return "unknown", "no corpus entry for this command"


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

    def execute(self, command: str, timeout: float | None = None,
                eval: bool = True) -> CommandResult:
        """Execute a csh command line and return its output.

        With eval=True (the default) the result is additionally
        classified against the command corpus (see the module docstring):
        status is "success"/"failure"/"unknown" and ok is corpus-aware -
        a ping that prints "No reply" comes back with ret == 0 but
        status "failure" and ok False.  eval=False gives the raw
        transport semantics (ok == (ret == 0)).

        On timeout, the command is interrupted (a best-effort INTR is
        sent) and CshTimeout is raised once the response arrives (or the
        grace period expires), so the connection stays usable afterwards.
        The timeout path is never corpus-evaluated.
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

        status, reason = "unknown", ""
        if eval:
            status, reason = _evaluate(command, result[0], result[1])

        return CommandResult(command=command, ret=result[0], output=result[1],
                             duration=time.monotonic() - t0,
                             status=status, reason=reason)

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
