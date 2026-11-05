#!/usr/bin/env python3
"""Interactive harness for csh_remote.

Connect to a running csh over its csh_remote socket and see exactly what
the client gets back (exit code, corpus classification, captured output,
duration).  The csh session must have the APM loaded and its listener
started ('remote start') - see init/remote.csh.

    ./test.py                       # socket from --sock or $CSH_REMOTE_SOCK
    ./test.py --sock /tmp/my.sock

Anything typed at the prompt is executed in csh; commands starting with
':' are handled locally (see ':help'). Ctrl-C interrupts the running
command (sends INTR), Ctrl-D or ':quit' exits.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "python"))
from csh_remote import (  # noqa: E402
    DEFAULT_SOCK_PATH,
    CshRemoteError,
    CshRPC,
    CshTimeout,
    CommandResult,
)

HELP = """\
Built-in commands (anything else is sent to csh as-is):
  <command>           execute a csh command line
  :intr               interrupt the currently running command
  :ping               liveness probe (round-trip latency)
  :status             run 'remote status' in csh
  :echo on|off        run 'remote echo on/off' in csh (terminal echo)
  :eval on|off        toggle corpus evaluation of results (default: on)
  :timeout [s|none]   default execute timeout (default: none)
  :raw                toggle repr() display of captured output
  :help               this help
  :quit               exit (same as Ctrl-D)

Notes:
  - Ctrl-C while a command runs sends INTR (the command's output so far
    is retrieved afterwards; the connection stays in sync).
  - CshTimeout is raised (with partial output) if :timeout is set and
    the command does not finish in time.
  - With eval on, the status line classifies the command via the corpus
    in python/corpus.toml (SUCCESS / FAILURE (reason)); csh's exit code
    alone says nothing about operational success (a failed ping is
    ret=0). With eval off it falls back to ok/ERROR on ret.
"""


def dim(text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "\033[2m" + text + "\033[0m"


def execute(rpc: CshRPC, cmd: str, timeout: float | None,
           eval: bool = True) -> CommandResult:
    """execute() with local Ctrl-C support: interrupt the running command
    and still consume its response so the protocol stream stays in sync."""
    try:
        return rpc.execute(cmd, timeout=timeout, eval=eval)
    except KeyboardInterrupt:
        try:
            rpc.interrupt()
        except CshRemoteError:
            pass
        try:
            rpc._sock.settimeout(10.0)  # grace period for the response
            ret, output = rpc._read_execute_response()
            rpc._sock.settimeout(None)
            return CommandResult(command=cmd, ret=ret, output=output,
                                 duration=-1.0)
        except Exception as e:  # noqa: BLE001
            raise CshRemoteError(
                f"command was interrupted but its response was lost ({e}); "
                "the connection may be out of sync - restart test.py"
            ) from None


def show(result: CommandResult, raw: bool, note: str = "") -> None:
    if result.output:
        text = repr(result.output) if raw else result.output
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    else:
        print(dim("(no output)"))
    if note:
        print(dim(note))
    dur = f"{result.duration:.2f}s" if result.duration >= 0 else "?"
    if result.status == "failure":
        state = f"FAILURE ({result.reason})"
    elif result.status == "success":
        state = "SUCCESS"
    else:  # unknown: no corpus entry, or eval off
        state = "ok" if result.ok else "ERROR"
    print(dim(f"── ret={result.ret} ({state}) in {dur} ──"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactively drive a running csh over csh_remote.")
    parser.add_argument("--sock", default=os.environ.get("CSH_REMOTE_SOCK",
                                                         DEFAULT_SOCK_PATH),
                        help="socket path (default: $CSH_REMOTE_SOCK or %s)"
                             % DEFAULT_SOCK_PATH)
    parser.add_argument("--eval", choices=("on", "off"), default="on",
                        help="evaluate results against python/corpus.toml "
                             "(default: on)")
    parser.add_argument("--timeout", default="none",
                        help="default per-command timeout in seconds "
                             "('none' = wait forever)")
    args = parser.parse_args()

    timeout: float | None
    try:
        timeout = None if args.timeout.lower() in ("none", "off") \
            else float(args.timeout)
    except ValueError:
        sys.exit(f"invalid --timeout value: {args.timeout!r}")

    try:
        rpc = CshRPC.connect(args.sock, timeout=1.0)
    except (OSError, CshRemoteError):
        sys.exit(f"cannot connect to {args.sock}\n"
                 f"Is csh running with the csh_remote APM loaded and\n"
                 f"'remote start' issued? (see init/remote.csh)")

    do_eval = args.eval == "on"
    raw = False

    print(f"csh_remote test harness - socket {rpc.sock_path}")
    print("Type ':help' for built-in commands, Ctrl-D to quit.\n")

    while True:
        try:
            line = input("csh> ")
        except KeyboardInterrupt:
            # at the prompt: just clear the line
            print()
            continue
        except EOFError:
            print()
            break

        cmd = line.strip()
        if not cmd:
            continue

        # local (:) commands
        if cmd.startswith(":"):
            parts = cmd.split()
            op = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            try:
                if op in (":quit", ":q", ":exit"):
                    break
                elif op == ":help":
                    print(HELP)
                elif op == ":ping":
                    print(f"PONG in {rpc.ping() * 1000:.1f} ms")
                elif op == ":intr":
                    rpc.interrupt()
                    print("INTR sent")
                elif op == ":status":
                    show(rpc.execute("remote status", eval=do_eval), raw)
                elif op == ":echo":
                    if arg not in ("on", "off"):
                        print("usage: :echo on|off")
                    else:
                        show(rpc.execute(f"remote echo {arg}", eval=do_eval),
                             raw)
                elif op == ":eval":
                    if arg not in ("on", "off"):
                        print("usage: :eval on|off")
                    else:
                        do_eval = arg == "on"
                        print(f"corpus evaluation: {arg}")
                elif op == ":timeout":
                    if arg.lower() in ("none", "off", ""):
                        timeout = None
                        print("default timeout: none (wait forever)")
                    else:
                        try:
                            timeout = float(arg)
                            print(f"default timeout: {timeout}s")
                        except ValueError:
                            print("usage: :timeout <seconds>|none")
                elif op == ":raw":
                    raw = not raw
                    print(f"raw output display: {'on' if raw else 'off'}")
                else:
                    print(f"unknown command {op} - try ':help'")
            except CshRemoteError as e:
                print(f"error: {e}")
            continue

        # csh command
        try:
            result = execute(rpc, cmd, timeout, eval=do_eval)
        except KeyboardInterrupt:
            print(dim("aborted"))
            continue
        except CshTimeout as e:
            if e.output:
                sys.stdout.write(e.output)
                if not e.output.endswith("\n"):
                    print()
            print(dim(f"── timed out (interrupted); partial output above ──"))
            continue
        except CshRemoteError as e:
            print(f"error: {e}")
            continue
        show(result, raw)

    rpc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
