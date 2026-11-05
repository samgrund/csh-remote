#!/usr/bin/env python3
"""End-to-end test of the csh_remote APM + Python client.

Build the APM first (../configure && ../build), then run:

    python3 tests/test_e2e.py

Requires a csh binary (found via $CSH or PATH, fallback ~/.local/bin/csh).
"""
import os
import shutil
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))
from csh_remote import CshRPC, CshTimeout  # noqa: E402

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


def main():
    if not os.path.isfile(APM):
        sys.exit(f"APM not built: {APM} (run ./configure && ./build first)")
    if not os.path.isfile(CSH):
        sys.exit(f"csh binary not found: {CSH}")

    with CshRPC.spawn(csh=CSH, apm_path=APM, echo=True) as rpc:
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
        rpc.read_terminal()  # clear
        rpc.execute("help")
        term = rpc.read_terminal()
        check("echo shows [remote] prefix", "[remote] help" in term, term[-200:])
        check("echo shows command output", "remote status" in term, term[-400:])

        # 7. echo off
        rpc.execute("remote echo off")
        rpc.read_terminal()
        rpc.execute("help")
        term = rpc.read_terminal()
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

        with CshRPC(new_sock) as rpc2:
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

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
