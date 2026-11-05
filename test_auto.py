#!/usr/bin/env python3
"""Minimal scripted automation: spawn a csh, run hardcoded commands."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "python"))
from csh_remote import CshRPC  # noqa: E402

COMMANDS = [
    "remote status",
    "ping -n 1 -t 300",   # no CSP network -> "No reply" (but ret 0!)
    "ident -n 1 -t 300",  # unreachable node -> silent failure
    "sleep 200",
]


def connect() -> CshRPC:
    """Start a csh with the csh_remote APM loaded and connect to it.

    (To drive an already-running csh instead:
        return CshRPC("/tmp/csh-remote.sock")   # after 'remote start' there)
    """
    rpc = CshRPC.spawn(echo=True)
    rpc.read_terminal()  # drop the startup banner from later captures
    return rpc


def run_command(rpc: CshRPC, cmd: str):
    """Run one command and print the result (RPC + csh terminal views)."""
    r = rpc.execute(cmd)
    print(f"$ {cmd}")
    if r.output:
        print(r.output, end="" if r.output.endswith("\n") else "\n")
    print(f"  -> {r.status}: {r.reason}  [ret={r.ret}]")
    term = rpc.read_terminal()  # what the csh terminal displayed
    if term:
        print(f"  [csh terminal] {term!r}")
    print()
    return r


if __name__ == "__main__":
    rpc = connect()
    for cmd in COMMANDS:
        run_command(rpc, cmd)
    rpc.close()
