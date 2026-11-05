#!/usr/bin/env python3
"""End-to-end test of the csh_remote APM + Python client.

Build the APM first (../configure && ../build), then run:

    python3 tests/test_e2e.py

Requires a csh binary (found via $CSH or PATH, fallback ~/.local/bin/csh).

The client library only connects; this test brings its own csh: the
SpawnedCsh harness below starts a csh under a pty with the APM loaded
and 'remote start' run, so the tests exercise the connect-only client
API exactly as a user would.
"""
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
from csh_remote import (  # noqa: E402
    CshRPC,
    CshRemoteError,
    CshTimeout,
    load_corpus,
)

APM = os.path.join(ROOT, "builddir", "libcsh_remote.so")
CSH = os.environ.get("CSH") or shutil.which("csh") or os.path.expanduser("~/.local/bin/csh")

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        print(f"  FAIL: {name} {detail}")


def _connect_fails(path: str) -> bool:
    import socket as _socket
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(path)
        s.close()
        return False
    except OSError:
        return True


class SpawnedCsh:
    """Test harness: a csh under a pty with the csh_remote APM loaded
    and its listener started, plus a connected CshRPC client.

    (This lives here, not in the client library - the library only
    connects to running csh sessions.)
    """

    def __init__(self, csh: str, apm: str, sock_path: str,
                 echo: bool = True, connect_timeout: float = 15.0):
        import pty

        # Init file: load the APM (csh's 'apm load' takes -f <name
        # substring> and -p <search paths>, not a file path) and start
        # its listener on our private socket.
        fd, init_file = tempfile.mkstemp(suffix=".csh", prefix="csh-remote-init-")
        with os.fdopen(fd, "w") as f:
            f.write(f"apm load -f {os.path.basename(apm)} "
                    f"-p {os.path.dirname(apm) or '.'}\n")
            f.write(f"remote start {sock_path}\n")

        env = dict(os.environ)
        env["CSH_REMOTE_SOCK"] = sock_path
        env["CSH_REMOTE_ECHO"] = "1" if echo else "0"

        master, slave = pty.openpty()
        try:
            self._proc = subprocess.Popen(
                [csh, "-i", init_file], stdin=slave, stdout=slave,
                stderr=slave, env=env, start_new_session=True, close_fds=True)
        finally:
            os.close(slave)

        self._master = master
        self._log = bytearray()
        self._tmp_paths = [sock_path, init_file]
        self.rpc = None
        try:
            deadline = time.monotonic() + connect_timeout
            while True:
                self._drain()
                if self._proc.poll() is not None:
                    self._drain()
                    tail = bytes(self._log[-2000:]).decode("utf-8", "replace")
                    raise CshRemoteError(f"csh exited during startup:\n{tail}")
                try:
                    self.rpc = CshRPC.connect(sock_path, timeout=1.0)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= deadline:
                        raise CshRemoteError(
                            f"csh_remote socket {sock_path} did not appear "
                            f"within {connect_timeout}s") from None
                    time.sleep(0.1)
        except Exception:
            self.close()
            raise

    def _drain(self) -> None:
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
            self._log.extend(chunk)
            if len(self._log) > 256 * 1024:
                del self._log[:128 * 1024]

    def read_terminal(self) -> str:
        """Everything the csh terminal displayed since the last call
        (what a human sitting there would have seen)."""
        self._drain()
        out = bytes(self._log).decode("utf-8", errors="replace")
        self._log.clear()
        return out

    def close(self) -> None:
        if self.rpc is not None:
            try:
                self.rpc.close()
            except OSError:
                pass
            self.rpc = None
        master, self._master = self._master, None
        if master is not None:
            try:
                os.write(master, b"exit\n")
            except OSError:
                pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass
        for path in self._tmp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._tmp_paths = []

    def __enter__(self) -> "SpawnedCsh":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def main():
    if not os.path.isfile(APM):
        sys.exit(f"APM not built: {APM} (run ./configure && ./build first)")
    if not os.path.isfile(CSH):
        sys.exit(f"csh binary not found: {CSH}")

    sock = f"/tmp/csh-remote-test-{os.getpid()}.sock"
    with SpawnedCsh(CSH, APM, sock) as spawned:
        rpc = spawned.rpc
        # 1. liveness
        lat = rpc.ping()
        check("ping", lat < 1.0, f"lat={lat}")

        # 2. simple command with output
        r = rpc.execute("help")
        check("help returns output", "remote status" in r.output, r.output[:100])
        check("help ret==0", r.ret == 0)
        check("no [remote] tag in RPC output", "[remote]" not in r.output)

        # 3. exit code propagation for unknown command
        r = rpc.execute("nosuchcommand")
        check("unknown cmd ret != 0", r.ret != 0, f"ret={r.ret}")
        check("unknown cmd output", "No such command" in r.output, r.output[:100])

        # 4. remote status shows our socket
        r = rpc.execute("remote status")
        check("remote status", "listening" in r.output and rpc.sock_path in r.output,
              r.output[:200])

        # 5. command with args + sleep (ms)
        r = rpc.execute("node 5")
        check("node cmd", r.ret == 0, f"ret={r.ret} out={r.output[:80]}")
        r = rpc.execute("sleep 200")
        check("sleep 200ms", r.ret == 0 and 0.15 < r.duration < 2.0,
              f"ret={r.ret} dur={r.duration:.2f}")

        # 6. echo appears on terminal
        #     (checks use the raw byte log: every echo is one atomic pty
        #     write, so the substrings stay contiguous even when the
        #     interactive prompt redraw interleaves between writes)
        spawned.read_terminal()  # clear
        rpc.execute("help")
        term = spawned.read_terminal()
        check("echo shows [remote] prefix", "[remote] help" in term, term[-200:])
        check("echo shows command output", "remote status" in term, term[-400:])

        # 7. echo off
        rpc.execute("remote echo off")
        spawned.read_terminal()
        rpc.execute("help")
        term = spawned.read_terminal()
        check("echo off hides prefix", "[remote]" not in term, term[-200:])
        rpc.execute("remote echo on")

        # 8. timeout + interrupt keeps connection usable
        #    (watch -n 10000 waits interruptibly via slash_wait_interruptible)
        try:
            rpc.execute("watch -n 10000 echo hi", timeout=1.5)
            check("timeout raises", False)
        except CshTimeout as e:
            check("timeout raises", True)
            check("timeout returns partial output", "Executing" in e.output, e.output[:120])
        r = rpc.execute("help")
        check("connection usable after timeout", r.ret == 0 and "remote status" in r.output)

        # 9. parallel INTR while another thread waits
        result = {}

        def worker():
            try:
                result["r"] = rpc.execute("watch -n 10000 echo hi")
            except Exception as e:  # noqa: BLE001
                result["e"] = e

        t = threading.Thread(target=worker)
        t0 = time.monotonic()
        t.start()
        time.sleep(1.0)
        rpc.interrupt()
        t.join(timeout=15)
        dur = time.monotonic() - t0
        check("INTR interrupts long command",
              not t.is_alive() and "r" in result and dur < 8.0,
              f"alive={t.is_alive()} dur={dur:.1f} result={str(result)[:150]}")

        # 10. apm info shows our libinfo
        r = rpc.execute("apm info")
        check("apm info", "csh_remote" in r.output, r.output[:200])

        # 11. stats updated
        r = rpc.execute("remote status")
        check("stats counted", "commands executed:" in r.output, r.output[:300])

        # 12. listener lifecycle: stop, restart on a new path over the
        #     same (still-open) connection, then reconnect
        old_sock = rpc.sock_path
        new_sock = old_sock + "-2"

        r = rpc.execute("remote stop")
        check("remote stop", r.ok and "stopped listening" in r.output,
              r.output[:120])
        check("socket file removed", not os.path.exists(old_sock))
        check("no new connections after stop",
              _connect_fails(old_sock))

        # the old connection survives; restart on a new path through it
        r = rpc.execute(f"remote start {new_sock}")
        check("remote start new path",
              r.ok and "listening on" in r.output and new_sock in r.output,
              r.output[:150])

        with CshRPC.connect(new_sock) as rpc2:
            check("new client on new socket", rpc2.ping() < 1.0)
            r = rpc2.execute("remote status")
            check("status shows new socket",
                  new_sock in r.output and "listening" in r.output,
                  r.output[:150])
            # idempotent: starting the same path again is a no-op
            r = rpc2.execute(f"remote start {new_sock}")
            check("remote start idempotent",
                  r.ok and "already listening" in r.output, r.output[:120])
            # and stopping twice in a row is fine too
            r = rpc2.execute("remote stop")
            check("remote stop (after restart)", r.ok, r.output[:120])
            r = rpc2.execute("remote stop")
            check("remote stop again",
                  r.ok and "not listening" in r.output, r.output[:120])

        # bring the server back for the spawned csh teardown
        rpc.execute(f"remote start {new_sock}")

        # 13. corpus evaluation (execute(eval=True), the default).
        #     There is no CSP network here, so ping always fails in its
        #     text output while still exiting 0 - the exact trap the
        #     corpus exists to catch.
        r = rpc.execute("ping -n 1 -t 500 -s 0")
        check("ping dead node ret==0", r.ret == 0,
              f"ret={r.ret} out={r.output[:80]}")
        check("ping dead node status failure", r.status == "failure",
              f"status={r.status}")
        check("ping dead node reason names marker", "No reply" in r.reason,
              r.reason)
        check("ping dead node ok False", r.ok is False, f"ok={r.ok}")

        r = rpc.execute("remote status")
        check("remote status eval success",
              r.status == "success" and "socket:" in r.reason,
              f"status={r.status} reason={r.reason}")

        # ident to an unreachable node fails SILENTLY: ret 0, no output
        # at all (csp_connect/csp_read failures in slash_csp.c print
        # nothing). The corpus's success marker catches this: empty
        # output matches nothing.
        r = rpc.execute("ident -n 1 -t 300")
        check("ident dead node ret==0, silent",
              r.ret == 0 and r.output == "",
              f"ret={r.ret} out={r.output[:60]!r}")
        check("ident dead node status failure", r.status == "failure",
              f"status={r.status}")
        check("ident dead node ok False", r.ok is False, f"ok={r.ok}")

        # ident to node 0 (self) - the spawned csh answers its own CMP
        # request, and the success marker "IDENT" is present.  (Explicit
        # -n 0 because an earlier test ran 'node 5', which changed the
        # session's default node.)
        r = rpc.execute("ident -n 0 -t 1000")
        check("ident self eval success",
              r.status == "success" and "IDENT" in r.output,
              f"status={r.status} out={r.output[:60]!r}")

        r = rpc.execute("sleep 200")
        check("command without corpus entry is unknown",
              r.status == "unknown" and r.ok,
              f"status={r.status} ok={r.ok}")

        # eval=False restores the raw semantics (the original trap:
        # ret==0 counts as ok even though the ping failed)
        r = rpc.execute("ping -n 1 -t 500 -s 0", eval=False)
        check("eval=False raw semantics",
              r.status == "unknown" and r.ok and r.ret == 0,
              f"status={r.status} ok={r.ok}")

        # custom corpus via load_corpus()
        fd, custom = tempfile.mkstemp(suffix=".toml", prefix="csh-corpus-")
        with os.fdopen(fd, "w") as f:
            f.write('version = 1\n\n[[command]]\nnames = ["help"]\n'
                    'success = ["definitely-not-in-help-output"]\n')
        load_corpus(custom)
        r = rpc.execute("help")
        check("custom corpus applied",
              r.status == "failure" and "no success marker" in r.reason,
              f"status={r.status} reason={r.reason}")

        # malformed corpus is rejected loudly (never silently permissive)
        fd, bad = tempfile.mkstemp(suffix=".toml", prefix="csh-corpus-")
        with os.fdopen(fd, "w") as f:
            f.write("version = 2\n")
        try:
            load_corpus(bad)
            check("malformed corpus rejected", False, "no error raised")
        except CshRemoteError as e:
            check("malformed corpus rejected", "version" in str(e), str(e))

        # restore the default corpus (also verifies the reload path)
        load_corpus()
        r = rpc.execute("remote status")
        check("default corpus restored", r.status == "success",
              f"status={r.status} reason={r.reason}")
        os.unlink(custom)
        os.unlink(bad)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
