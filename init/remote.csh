# Example init file for loading the csh_remote APM.
#
# IMPORTANT: csh's "apm load" command does not accept a file path as a
# positional argument - it only supports:
#   -p PATH   ';' separated search paths (default: ~/.local/lib/csh, the
#             directory of the csh binary, and /usr/lib/csh)
#   -f NAME   substring that the APM file name must contain
# and only picks up libraries whose name contains "libcsh_" and ".so".
#
# So to load a specific build, point -p at its directory and -f at (part of)
# its file name, e.g.:
#
#   apm load -f libcsh_remote.so -p /home/user/.local/lib/csh
#   apm load -f csh_remote -p /home/user/STEP/csh_remote/builddir
#
# Environment variables honored at load time:
#   CSH_REMOTE_SOCK   socket path (default /tmp/csh-remote.sock)
#   CSH_REMOTE_ECHO   1 = echo remote output to the terminal (default), 0 = silent
