#pragma once

/*
 * csh_remote wire protocol
 * =======================
 *
 * Transport: Unix domain socket (SOCK_STREAM), default path
 * /tmp/csh-remote.sock (override with the CSH_REMOTE_SOCK environment
 * variable).  Socket file permissions are 0600 (owner access only).
 *
 * Requests are newline-terminated ASCII lines, optionally followed by a
 * length-prefixed binary payload:
 *
 *   EXEC <len>\n<command bytes><len>\n   Execute a csh command line
 *   INTR\n                               Interrupt the running command
 *   PING\n                               Liveness probe
 *   BYE\n                                Close this connection (client may
 *                                        also just close the socket)
 *
 * Responses:
 *
 *   RET <code>\n                         Result of INTR (command exit code
 *                                        is carried by the EXEC response)
 *   RET <code>\nOUT <len>\n<bytes>\n     EXEC response: command exit code
 *                                        followed by everything the command
 *                                        wrote to stdout and stderr
 *   PONG\n                               Response to PING
 *   ERR <message>\n                      Malformed request / server error
 *
 * Notes:
 *   - The <command bytes> payload is NOT newline terminated on the wire
 *     except for the single trailing '\n' shown above; the length prefix
 *     makes binary-safe commands possible.
 *   - EXEC responses contain exactly the output of the command; the
 *     terminal annotation "[remote] <command>" is never included.
 *   - EXEC is serialized globally (one command at a time across all
 *     clients).  INTR and PING are answered immediately.
 */

#define CRP_DEFAULT_SOCK_PATH "/tmp/csh-remote.sock"
#define CRP_SOCK_PATH_MAX 108 /* struct sockaddr_un::sun_path size */

#define CRP_MAX_CMD_LEN 8192   /* longest accepted command line */
#define CRP_MAX_LINE_LEN 64    /* longest accepted request line (excl. EXEC payload) */

/* Environment variables */
#define CRP_ENV_SOCK "CSH_REMOTE_SOCK"  /* default path for 'remote start' */
#define CRP_ENV_ECHO "CSH_REMOTE_ECHO"  /* 1 = echo to terminal (default) */
