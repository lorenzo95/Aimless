# Plan: always-on daemon (Docker) + native clients over an app-managed SSH socket tunnel

Status: **not started** — stored for later. Branch: `always-on-daemon`.

## Problem

Store-and-forward lives on the sending daemon. If the laptop sends a message
while the desktop is off, then the laptop suspends before the desktop comes
online, nothing bridges the gap: delivery only happens when both ends are online
at once. There is no relay/mailbox — a message is addressed to the recipient's
Yggdrasil node key, and only the recipient's own daemon can accept it.

## Solution

Run one `aimlessd` on an always-on box (a Docker container in daemon-only mode)
holding your `node.key`, so your address is unchanged and the daemon is always
reachable. Each device runs the normal native client in a new **external-daemon
mode**: it connects to the remote daemon's API over an SSH-forwarded Unix socket
instead of spawning a local daemon. The laptop can suspend right after handing a
message to the server; the server owns retries.

Transport is SSH **Unix-socket (streamlocal) forwarding** straight to the
container's bind-mounted `api.sock` — the socket's `0600` permissions are the
auth, so no TCP and no extra auth layer.

## A. Docker image — daemon-only mode

- `deploy/docker/docker-entrypoint.sh`: add `AIMLESS_MODE` (default `webtop`).
  When `daemon`: keep the artifact fetch + `/data` ownership fix, then
  `exec su-exec "${AIMLESS_UID:-1000}:${AIMLESS_GID:-1000}" /data/bin/aimlessd-linux-amd64 -datadir /data/state`
  - no supervisord/Xvfb/openbox/VNC/noVNC, no ports; Docker `restart` policy
    handles restarts.
  - optional `AIMLESS_UID`/`AIMLESS_GID` (chown `/data` to them) so
    `state/api.sock` is owned by the host SSH user.
- New `deploy/docker/docker-compose.daemon.yml`: same image + `./aimless-data:/data`,
  `AIMLESS_MODE=daemon`, no ports; optional `HEALTHCHECK` on `test -S /data/state/api.sock`.
- `deploy/docker/README.md`: "Daemon-only mode" section — seed `state/node.key`
  (your existing key, preserves the address) and `state/config.json`
  (`{"peers": [...]}`); socket path on the host is
  `<host>/…/aimless-data/state/api.sock`; uid-matching note; Docker Desktop
  caveat (Unix sockets in bind mounts only work on native Linux Docker).
- No daemon/protocol changes.

## B. Client — external daemon + app-managed SSH (`client/aimless/gtkui.py`)

Env-only config:

- `AIMLESS_REMOTE=user@docker-host` → enables external mode
- `AIMLESS_REMOTE_SOCK=/host/path/aimless-data/state/api.sock` (required)
- `AIMLESS_SOCK` optional local socket
  (`$XDG_RUNTIME_DIR/aimless/api.sock`, else `<AIMLESS_HOME>/api.sock`; parent `0700`)
- `AIMLESS_SSH_OPTS` optional; otherwise `~/.ssh/config` supplies key/host

New `TunnelSupervisor` spawns/supervises:

```
ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o StreamLocalBindUnlink=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L <local_sock>:<remote_sock> <remote>
```

- `BatchMode=yes` fails fast (no password hang); `StreamLocalBindUnlink=yes`
  clears stale local sockets after a crash; stderr → `CONFIG_DIR/tunnel.log`;
  tracks child + `CONFIG_DIR/tunnel.pid`; `is_running()` = child alive + local
  socket present; `stop()` kills only the child/pid we own (cmdline-checked).

Changes:

- `DaemonSupervisor` (~680–790): external mode `ensure()` = ensure tunnel, then
  wait for the daemon handshake; `spawn()` raises; `stop()` **never**
  `os.kill`s the remote `whoami` PID (~763–771).
- `poll()` (~3972): external mode re-ensures the tunnel on drop (log-only);
  `DaemonClient` auto-reconnect covers the socket.
- `quit()` / `stop_all()`: stop only the owned tunnel; skip `pkill -x aimlessd`
  (~4141).
- `_open_window_unlocked` (~3941–3946): mode-aware error text
  ("SSH tunnel unreachable…").
- `install_autostart()` (~4077): bake `AIMLESS_REMOTE`/`AIMLESS_REMOTE_SOCK`
  into the generated `Exec=`.
- `cli.py`: unchanged (connect-only, honors `AIMLESS_SOCK`).

## C. Setup / migration

1. Host: `docker compose -f docker-compose.daemon.yml up -d`, with `node.key`
   (+ optional `aimless.db`) copied into `aimless-data/state/`.
2. Each device: keep `identity.json` + `client-contacts.json` in local
   `AIMLESS_HOME`; set `AIMLESS_REMOTE`, `AIMLESS_REMOTE_SOCK`; ensure the
   client's SSH key is authorized on the Docker host.
3. Retire the desktop's local daemon so two daemons never share one `node.key`.

## D. Caveats (documented)

- Messages beyond `inboxCapacity` while a device is long offline can be missed →
  raise `-inbox`.
- Attachments are freed daemon-side by the first device to download → fix later
  (per-client consumption).
- Unread/clear-history is per-device; daemon blocklist is global.

## E. Tests & release

- Unit: tunnel argv includes `-N`, `-L local:remote`, `BatchMode`,
  `StreamLocalBindUnlink`; external `ensure()` doesn't spawn a daemon;
  `spawn()` raises; `stop()` never kills the remote PID; `poll()` doesn't
  respawn; `stop_all()` skips `pkill`.
- Integration with `two_nodes`/`gtk_app`: fake `AIMLESS_REMOTE`, no-op tunnel
  spawn, `AIMLESS_SOCK=<fixture daemon>`, verify send/receive.
- Bump client to **0.9.0**, package + `check_dist`, push `v2` then `main`;
  rebuild/publish the webtop image to GHCR (entrypoint changed).
