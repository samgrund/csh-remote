#!/usr/bin/env python3
"""Minimal scripted automation: connect to a running csh, run commands.

The csh session must have the csh_remote APM loaded and its listener
started ('remote start'); see init/remote.csh for an init-file template.
"""
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
    """Connect to a running csh ($CSH_REMOTE_SOCK or /tmp/csh-remote.sock)."""
    return CshRPC.connect()


def run_command(rpc: CshRPC, cmd: str):
    """Run one command; print its output and verdict."""
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
    print()
    return r


if __name__ == "__main__":
    rpc = connect()
    for cmd in COMMANDS:
        run_command(rpc, cmd)
    rpc.close()
