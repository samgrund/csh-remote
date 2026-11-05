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

# echo=True: the csh terminal also shows "[remote] <cmd>" + output.
# The spawned csh runs under a detached pty (nobody is looking at it);
# read_terminal() returns what a person sitting there would have seen.
with CshRPC.spawn(echo=True) as rpc:
    rpc.read_terminal()  # drop the startup banner/prompt from captures
    for cmd in COMMANDS:
        r = rpc.execute(cmd)
        print(f"$ {cmd}")
        if r.output:
            print(r.output, end="" if r.output.endswith("\n") else "\n")
        print(f"  -> {r.status}: {r.reason}  [ret={r.ret}]")
        term = rpc.read_terminal()
        if term:
            print(f"  [csh terminal] {term!r}")
        print()
