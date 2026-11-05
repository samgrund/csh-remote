#!/usr/bin/env python3
"""Minimal scripted automation: spawn a csh, run hardcoded commands."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "python"))
from csh_remote import CshRPC  # noqa: E402

COMMANDS = [
    "remote status",
    "ping -n 1 -t 300",   # no CSP network -> "No reply" (but ret 0!)
    "ident -n 1 -t 300",  # unreachable node -> silent failure
    "sleep 200",
]

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean(text: str) -> str:
    """Make captured terminal output readable: strip ANSI escapes
    and carriage returns, drop blank lines."""
    text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")
    return "\n".join(ln for ln in text.splitlines() if ln.strip())


def connect() -> CshRPC:
    """Start a csh with the csh_remote APM loaded and connect to it.

    (To drive an already-running csh instead:
        return CshRPC("/tmp/csh-remote.sock")   # after 'remote start' there)
    """
    rpc = CshRPC.spawn(echo=True)
    rpc.read_terminal()  # drop the startup banner from later captures
    return rpc


def run_command(rpc: CshRPC, cmd: str):
    """Run one command; print its output, verdict and terminal view."""
    r = rpc.execute(cmd)
    print(f"$ {cmd}")
    if r.output:
        print(r.output, end="" if r.output.endswith("\n") else "\n")
    if r.status == "failure":
        print(f"  -> FAILED - {r.reason}  [{r.duration:.2f}s, ret={r.ret}]")
    elif r.status == "success":
        print(f"  -> OK  [{r.duration:.2f}s]")
    else:
        print(f"  -> unknown (no corpus entry)  [{r.duration:.2f}s, ret={r.ret}]")
    term = _clean(rpc.read_terminal())  # what the csh terminal displayed
    if term:
        print("  csh terminal:")
        for line in term.splitlines():
            print(f"    {line}")
    print()
    return r


if __name__ == "__main__":
    rpc = connect()
    for cmd in COMMANDS:
        run_command(rpc, cmd)
    rpc.close()
