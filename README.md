# csh_remote

Remote control for the [CSP Shell (`csh`)](https://github.com/spaceinventor/csh).

`csh_remote` is an APM plugin (shared library) that, once loaded into a
running `csh`, exposes that instance over a local Unix domain socket.
External programs — the bundled Python client, for example — can inject
commands and receive their output directly, with no terminal scraping,
tmux/`send-keys`, or log-file tailing.

```
┌──────────────┐  EXEC/INTR/PING   ┌─────────────────────────────┐
│ Python client│ ────────────────▶ │ csh (interactive)            │
│ (stdlib only)│ ◀──────────────── │  └─ csh_remote APM (this repo)│
└──────────────┘  RET + output    │      Unix socket server      │
                                  └─────────────────────────────┘
```

Features:

- Execute any csh command line remotely and get its exit code plus
  everything it wrote to **stdout and stderr** (binary-safe framing).
- **Inert when loaded** — nothing listens until you run
  `remote start [socket path]`; `remote stop` tears it down again.
  Switching to a different socket path at any time is supported.
- Output is, by default, also echoed live to the interactive terminal
  (prefixed with `[remote] <command>`) so a human watching the shell
  stays in the loop. Toggle per session with `remote echo on|off`.
- Interrupt running commands (`INTR`), including commands waiting in
  `slash_wait_interruptible` (e.g. `watch`).
- Remote commands do not pollute the interactive history or completion.
- One command at a time (serialized); several clients may connect.
- Socket permissions 0600 — local, single-user access only.

Not (yet) supported: headless csh (no TTY), remote (network) access.

## Building

```sh
./configure            # meson setup, prefix ~/.local, libdir lib/csh
./build                # ninja
./install              # meson install (skips subprojects)
```

Requires `meson` >= 1.6 and `ninja`. The resulting plugin is
`builddir/libcsh_remote.so`.

The submodules in `lib/` (apm_csh, slash, param, csp) are pinned to the
same commits as the `csh` checkout this plugin is intended for, so the
ABIs match exactly. Re-pull them with `git submodule update --init`.

## Loading into csh

**Important:** csh's `apm load` does not take a file path as a
positional argument. It takes `-p <search paths>` and `-f <filename
search string>`, and only picks up libraries whose file name contains
`libcsh_` and `.so`:

```
apm load -f libcsh_remote.so -p ~/.local/lib/csh
```

or, during development:

```
apm load -f csh_remote -p /path/to/csh_remote/builddir
```

Put the line in your csh init file (`csh -i init.csh`) to load it at
startup. See `init/remote.csh` for a template.

**The APM does not listen when loaded.** Start the socket server with:

```
remote start                 # uses $CSH_REMOTE_SOCK or /tmp/csh-remote.sock
remote start /tmp/my.sock     # explicit path (absolute, < 100 chars)
remote stop                   # stop listening, remove the socket file
```

`remote start` is idempotent (restarting the same path is a no-op) and
transparently switches sockets when given a new path. Already-connected
clients keep their connections after a stop — only new ones are refused
until the next start (this also means a client can stop and restart the
server over its own connection, on a new path if desired).

Configuration:

| Environment variable | Meaning                                              | Default                |
|----------------------|-----------------------------------------------------|------------------------|
| `CSH_REMOTE_SOCK`    | Default socket path for bare `remote start`          | `/tmp/csh-remote.sock` |
| `CSH_REMOTE_ECHO`    | `1` echo remote output to the terminal, `0` silent    | `1`                    |

If another server is already listening on the requested socket, the
start is refused (it will not steal the socket).

## Interactive commands

The plugin adds these commands to the running csh:

```
remote status            # socket path/state, echo mode, statistics
remote start [path]      # start listening (see above)
remote stop              # stop listening, remove the socket file
remote echo <on|off>     # toggle terminal echo of remote commands
```

`apm info` also shows a `csh_remote` summary line via the APM's
`libinfo()`.

## Python client

`python/csh_remote.py` is a single-module, stdlib-only client:

```python
from csh_remote import CshRPC

# Drive an already-running csh that loaded the APM:
rpc = CshRPC("/tmp/csh-remote.sock")
result = rpc.execute("help")
print(result.ret, result.ok, result.output)
rpc.interrupt()      # if a previous command is stuck
rpc.close()

# Or spawn a dedicated csh under a pty and drive it (replaces
# tmux send-keys automation):
with CshRPC.spawn(echo=False) as rpc:
    print(rpc.execute("ident").output)
    rpc.interrupt()
    print(rpc.read_terminal())   # what a human would have seen
```

`CshRPC.spawn()` searches for the plugin in `~/.local/lib/csh/` and
`../builddir/` (relative to the module), or set `CSH_REMOTE_APM` to point
at a specific `libcsh_remote.so`.

`execute()` accepts an optional `timeout`; on expiry it interrupts the
command and raises `CshTimeout` (with any partial output) while leaving
the connection usable.

## Command evaluation (corpus)

csh's exit code only says that a command **parsed and ran** — most
operational failures still return `SLASH_SUCCESS`. A ping to a dead
node, for example, exits 0 and reports the failure purely in its text
output:

```
csh> ping -n 1 -t 1000
Ping node 1 size 0 timeout 1000: No reply      # ret is still 0!
```

To make results trustworthy, `execute()` (with `eval=True`, the
default) classifies the output against a **command corpus** of
success/failure markers defined in `python/corpus.toml`:

```python
r = rpc.execute("ping -n 1 -t 1000")
r.ret      # 0                 - raw slash exit code, unchanged
r.status   # "failure"         - "success" | "failure" | "unknown"
r.reason   # "failure marker 'No reply'"
r.ok       # False             - corpus-aware
```

Evaluation order (all markers are plain substring matches):

1. `ret != 0` → **failure** (exit code)
2. a failure marker (the command's + the global ones) appears in the
   output → **failure** (reason names the marker)
3. success markers are defined for the command but none appears →
   **failure**
4. otherwise, with a corpus entry → **success**; without one →
   **unknown** (`ok` then falls back to `ret == 0`)

`execute(..., eval=False)` disables all of this and restores the raw
semantics (`ok == (ret == 0)`). `CshTimeout` results are never
corpus-evaluated.

### Extending the corpus

Add a `[[command]]` entry to `python/corpus.toml`:

```toml
[[command]]
names = ["ping"]           # first word(s) of the command line,
success = ["Reply in "]    # longest match wins ("remote status" style)
failure = ["No reply"]
```

- `names` may list several names for one entry and may be multi-word
  (matched against the first word(s) of the command line).
- `[global]` failure markers apply to every command.
- Every marker must be a **verbatim string** that csh actually prints;
  keep the `# source:` comments pointing at the csh file so markers can
  be re-verified after a csh update.
- `load_corpus(path)` validates and reloads a corpus at runtime
  (malformed files raise `CshRemoteError` naming the offending key —
  the corpus never degrades to silently passing commands as
  successful).
- Set `$CSH_REMOTE_CORPUS` to use a different corpus file altogether.

Evaluation is a heuristic: a command whose failure wording is not in
the corpus comes back `unknown`, never a false `success` — curating the
corpus is how you close that gap, command by command.

`test.py` shows the classification in its status line
(`── ret=0 (FAILURE (failure marker 'No reply')) in 1.00s ──`) and can
toggle it live with `:eval on|off` (or `--eval off` at startup).

## Wire protocol

`src/csh_remote_proto.h` documents the protocol in full. Summary:

```
client                         server
------                         ------
EXEC <len>\n<cmd>\n        ->  RET <code>\nOUT <len>\n<bytes>\n
INTR\n                     ->  RET 0\n     (interrupt running command)
PING\n                     ->  PONG\n
BYE\n                      ->  RET 0\n     (then close)
```

Malformed requests get `ERR <message>\n`. The `OUT` payload is exactly
what the command wrote to stdout/stderr; terminal annotations such as
`[remote] <cmd>` are never part of it.

## How it works

- On `apm load`, apm_csh's default `libmain()` registers the
  `remote ...` commands (via the `slash` linker section) and calls this
  plugin's `apm_init()`. Loading is inert: no socket is created until
  `remote start [path]` runs, which binds the socket and spawns the
  server thread; `remote stop` shuts the listener down (wake `accept()`
  via `shutdown()`, join the thread, unlink the socket file).
- Commands run through `slash_execute()` on the APM's own `struct slash`
  instance — the same approach csh's embedded Python uses — so the
  interactive instance (prompt, history, stdin) is never touched. The
  instance's `waitfunc` is replaced with a signal-polling sleeper so
  remote `INTR` works without stealing the interactive stdin.
- Output is captured by redirecting fd 1/2 into a pipe; a drain thread
  accumulates the response and, when echo is enabled, replays every
  chunk live to the real terminal.
- Executions serialize on a mutex; `INTR` sets the slash instance's
  signal flag from any connection.

## Tests

With the plugin built:

```sh
python3 tests/test_e2e.py
```

spawns a real csh under a pty, loads the plugin and exercises the
protocol end to end (execution, capture, echo on/off, timeout,
interrupt, stats, the `remote start`/`remote stop` listener lifecycle,
and corpus evaluation).

For interactive poking, `test.py` gives you a prompt that runs commands
over the socket and shows exactly what the client gets back (exit code,
corpus classification, captured output, duration):

```sh
./test.py                 # attach to a running csh's socket
./test.py --spawn         # or start a dedicated csh and drive it
```

Type `:help` there for built-in commands (`:intr`, `:ping`, `:eval`,
`:term`, `:timeout`, `:raw`, ...). Ctrl-C interrupts the running
command.
