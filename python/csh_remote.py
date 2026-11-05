"""csh_remote - Python client for the csh_remote APM.

Drives a running CSP Shell (csh) instance over its remote-control Unix
socket: inject commands, receive their output, interrupt long-running
commands.  No dependencies beyond the standard library.

The client only CONNECTS - it never starts or owns a csh.  On the csh
side, load the APM and start its listener (see the project README and
init/remote.csh for an init-file template):

    apm load -f libcsh_remote.so -p <directory>
    remote start /tmp/csh-remote.sock

then, from Python:

    from csh_remote import CshRPC

    rpc = CshRPC.connect("/tmp/csh-remote.sock")
    r = rpc.execute("ping -n 1 -t 1000")
    print(r.ret, r.status, r.output)
    rpc.interrupt()      # if a previous command is stuck
    rpc.close()

The socket path defaults to $CSH_REMOTE_SOCK or /tmp/csh-remote.sock.
With echo enabled on the csh side (the default), a human watching that
csh sees "[remote] <command>" plus the output live while Python reads
the same bytes from the socket.

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

import os
import socket
import time
from dataclasses import dataclass

try:
    import tomllib  # Python >= 3.11
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # corpus evaluation unavailable

__all__ = ["CshRPC", "CommandResult", "CshRemoteError", "CshTimeout",
           "load_corpus"]

DEFAULT_SOCK_PATH = "/tmp/csh-remote.sock"
SOCK_ENV_VAR = "CSH_REMOTE_SOCK"  # same default as 'remote start' on the csh side
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
    """Client for the csh_remote APM socket server inside a running csh.

    The csh session must have the APM loaded and its listener started
    ('remote start [path]').  Get a connected client with the connect()
    classmethod; the constructor only records the socket path.
    """

    def __init__(self, sock_path: str | None = None):
        if sock_path is None:
            sock_path = os.environ.get(SOCK_ENV_VAR) or DEFAULT_SOCK_PATH
        if len(sock_path) >= SOCK_PATH_MAX:
            raise CshRemoteError(f"socket path too long: {sock_path!r}")
        self.sock_path = sock_path
        self._sock: socket.socket | None = None
        self._reader: _Reader | None = None

    # -- connection ---------------------------------------------------

    @classmethod
    def connect(cls, sock_path: str | None = None,
                timeout: float = 10.0) -> "CshRPC":
        """Connect to a running csh's csh_remote socket and return the
        client.  sock_path defaults to $CSH_REMOTE_SOCK or
        /tmp/csh-remote.sock.

        Use as a context manager to close the connection automatically:

            with CshRPC.connect() as rpc:
                rpc.execute("help")
        """
        rpc = cls(sock_path)
        rpc.reconnect(timeout)
        return rpc

    def reconnect(self, timeout: float = 10.0) -> None:
        """(Re)open the connection - e.g. after close(), or after the
        csh session stopped and restarted its listener."""
        self.close()
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
            self._reader = None

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

    # -- context manager -------------------------------------------------

    def __enter__(self) -> "CshRPC":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
