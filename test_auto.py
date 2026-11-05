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

with CshRPC.spawn(echo=False) as rpc:
    for cmd in COMMANDS:
        r = rpc.execute(cmd)
        print(f"$ {cmd}")
        if r.output:
            print(r.output, end="" if r.output.endswith("\n") else "\n")
        print(f"  -> {r.status}: {r.reason}  [ret={r.ret}]\n")
