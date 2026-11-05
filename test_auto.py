#!/usr/bin/env python3
"""test_auto - run a sequence of csh commands automatically.

Spawns a dedicated csh (or attaches to a running one via --sock),
executes the commands one after another, and prints each command's
output plus its corpus classification.  Exits nonzero if any command
failed - the template for scripted csh automation (no tmux needed).

    ./test_auto.py                            # built-in demo sequence
    ./test_auto.py "remote status" "sleep 200" # your own commands
    ./test_auto.py --sock /tmp/csh-remote.sock # drive a running csh
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "python"))
from csh_remote import CshRPC, CshRemoteError, CshTimeout  # noqa: E402

# Demo sequence: deliberately mixes outcomes to show the classification.
SCRIPT = [
    "remote status",       # SUCCESS (marker "socket:")
    "node 5",
    "ping -n 1 -t 300",     # FAILURE: "No reply" (but ret 0!)
    "ident -n 1 -t 300",    # FAILURE: silent (empty output, ret 0)
    "sleep 200",           # unknown: no corpus entry
    "nosuchcommand",       # FAILURE: exit code + "No such command"
]


def run(rpc: CshRPC, commands: list[str], timeout: float) -> int:
    counts = {"success": 0, "failure": 0, "unknown": 0}
    failed = 0

    for cmd in commands:
        print(f"$ {cmd}")
        try:
            r = rpc.execute(cmd, timeout=timeout)
        except CshTimeout:
            print("  TIMED OUT (interrupted)")
            counts["failure"] += 1
            failed += 1
            continue
        if r.output:
            sys.stdout.write(r.output if r.output.endswith("\n")
                             else r.output + "\n")
        note = {"success": "OK", "failure": "FAILED",
                "unknown": "unknown"}[r.status]
        print(f"  -> {note}"
              + (f" ({r.reason})" if r.reason else "")
              + f"  [ret={r.ret} {r.duration:.2f}s]")
        counts[r.status] += 1
        failed += r.status == "failure"

    print(f"\n{len(commands)} commands: {counts['success']} succeeded, "
          f"{counts['failure']} failed, {counts['unknown']} unknown")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a sequence of csh commands over csh_remote.")
    parser.add_argument("commands", nargs="*",
                        help="commands to run (default: built-in demo)")
    parser.add_argument("--sock", default=None,
                        help="attach to a running csh at this socket "
                             "(default: spawn a dedicated csh)")
    parser.add_argument("--csh", default=None,
                        help="csh binary to spawn (default: csh from PATH)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="per-command timeout in seconds (default: 30)")
    args = parser.parse_args()
    commands = args.commands or SCRIPT

    try:
        if args.sock:
            rpc = CshRPC(args.sock)
            spawned = False
        else:
            csh = args.csh or shutil.which("csh") \
                or os.path.expanduser("~/.local/bin/csh")
            rpc = CshRPC.spawn(csh=csh, echo=False)
            spawned = True
    except (OSError, CshRemoteError) as e:
        sys.exit(f"cannot start: {e}")

    try:
        return run(rpc, commands, args.timeout)
    except CshRemoteError as e:
        sys.exit(f"connection error: {e}")
    finally:
        rpc.close()
        if spawned:
            print("(spawned csh exited)")


if __name__ == "__main__":
    sys.exit(main())
