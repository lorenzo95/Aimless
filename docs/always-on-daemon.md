# Plan: always-on daemon (Docker) + a client over an app-managed SSH socket tunnel

Status: **not started** — stored for later. Branch: `always-on-daemon`.

## Problem

Store-and-forward lives on the sending daemon. If the laptop sends a message
while the recipient's machine is off, then the laptop suspends before that
machine comes online, nothing bridges the gap: delivery only happens when both
ends are online at once. There is no relay/mailbox — a message is addressed to
the recipient's Yggdrasil node key, and only that node's own daemon can accept
it.

## Solution

Run one `aimlessd` on an always-on box (a Docker container in daemon-only mode)
holding your `node.key`, so your address is unchanged and the daemon is always
reachable. The client connects to the remote daemon's API over an SSH-forwarded
Unix socket, app-managed by the client. The laptop can suspend right after
handing a message to the server; the server owns retries.

Transport is SSH **Unix-socket (streamlocal) forwarding** straight to the
container's bind-mounted `api.sock` — the socket's `0600` permissions are the
auth, so no TCP and no extra auth layer.

**Assume a single client.** One device connects to the daemon at a time; this
goes in the README.

## On-disk layout (post-0.8.11)

One root, split by owner (see `client/aimless/paths.py`):

```
<root>/                 (~/.local/share/aimless; AIMLESS_HOME)
├── client/   identity.json · contacts.json · state.db · prefs.json · attachments/
├── daemon/   node.key · config.json · aimless.db · contacts.json · blocked.json · lock · api.sock
├── logs/     app.log · daemon.log · tunnel.log
└── run/      app.pid · aimlessd.pid · tunnel.pid
```

- Client config (including the remote settings) is `client/prefs.json`.
- The daemon socket is `<root>/daemon/api.sock`; the daemon datadir is
  `<root>/daemon`.
- The only thing outside the root is the freedesktop autostart entry.

## A. Client settings (config file, not env)

In `client/prefs.json`:

```json
"remote": {
  "host": "user@docker-host",
  "socket": "/abs/host/path/aimless-data/daemon/api.sock",
  "local_socket": "/run/user/1000/aimless/remote.sock"
}
```

- Presence of `remote.host` ⇒ external-daemon mode.
- `local_socket` is deliberately distinct from `<root>/daemon/api.sock` so it
  never clashes with a local daemon's socket. Default when omitted:
  `$XDG_RUNTIME_DIR/aimless/remote.sock`, else `<root>/run/remote.sock`.
- Read at startup (menu/autostart/terminal all work — no session env).
- Optional env overrides (`AIMLESS_REMOTE`, `AIMLESS_REMOTE_SOCK`,
  `AIMLESS_SOCK`) for tests/dev only.

## B. App-managed SSH tunnel (`client/aimless/gtkui.py`)

New `TunnelSupervisor` spawns/supervises:

```
ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o StreamLocalBindUnlink=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L <local_socket>:<remote_socket> <host>
```

- `BatchMode=yes` fails fast (no password hang); `StreamLocalBindUnlink=yes`
  clears a stale local socket after a crash.
- stderr → `<root>/logs/tunnel.log`; pid → `<root>/run/tunnel.pid`.
- `is_running()` = child alive + local socket present; `stop()` kills only the
  child/pid we own (cmdline-checked). Creates the socket's parent dir `0700`.

Changes:

- `DaemonSupervisor` external mode: `ensure()` = ensure tunnel, then wait for
  the daemon handshake; `spawn()` raises; `stop()` **never** `os.kill`s the
  remote `whoami` PID.
- `poll()`: external mode re-ensures the tunnel on drop (log-only);
  `DaemonClient` auto-reconnect covers the rest.
- `quit()` / `stop_all()`: stop only the owned tunnel; skip `pkill -x aimlessd`.
- `_open_window_unlocked` error text is mode-aware.
- Guard: in external mode, if a daemon is reachable at the *local*
  `<root>/daemon/api.sock`, log a loud warning (node-key conflict).
- `cli.py` also reads `remote` from prefs and ensures the tunnel, so headless
  commands work when the tray is not running.

## C. Docker — daemon-only mode

- `deploy/docker/docker-entrypoint.sh`: add `AIMLESS_MODE` (default `webtop`).
  When `daemon`: fetch artifacts + fix `/data` ownership, then
  `exec su-exec "${AIMLESS_UID:-1000}:${AIMLESS_GID:-1000}" /data/bin/aimlessd-linux-amd64 -datadir /data/daemon`
  - no supervisord/Xvfb/openbox/VNC/noVNC, no ports; Docker `restart` handles
    restarts.
  - optional `AIMLESS_UID`/`AIMLESS_GID` (chown `/data`) so
    `daemon/api.sock` is owned by the host SSH user.
- New `deploy/docker/docker-compose.daemon.yml`: same image + `./aimless-data:/data`,
  `AIMLESS_MODE=daemon`, no ports; optional `HEALTHCHECK` on `test -S /data/daemon/api.sock`.
- `deploy/docker/README.md`: "Daemon-only mode" — seed `daemon/node.key` (your
  existing key, preserves the address) and `daemon/config.json`
  (`{"peers": [...]}`); socket on the host is
  `<host>/…/aimless-data/daemon/api.sock`; uid-matching note; Docker Desktop
  caveat (Unix sockets in bind mounts only work on native Linux Docker).
- No daemon/protocol changes.

## D. Setup / migration

1. Host: `docker compose -f docker-compose.daemon.yml up -d`, with `node.key`
   (+ optional `aimless.db`) copied into `aimless-data/daemon/`.
2. Client: keep `client/` (identity/contacts/history); add the `remote` block to
   `client/prefs.json`; ensure the client's SSH key is authorized on the host.
3. Retire the local daemon so two daemons never share one `node.key`.

## E. Caveats (documented)

- Single client per daemon.
- Messages beyond `inboxCapacity` while the client is long offline can be missed
  → raise `-inbox`.
- Attachments are freed daemon-side when the client downloads them.

## F. Tests & release

- Unit: tunnel argv includes `-N`, `-L local:remote`, `BatchMode`,
  `StreamLocalBindUnlink`; external `ensure()` doesn't spawn; `spawn()` raises;
  `stop()` never kills the remote PID; `poll()` doesn't respawn; `stop_all()`
  skips `pkill`; prefs `remote` round-trips.
- Integration with `two_nodes`/`gtk_app`: fake `remote.host` (no-op tunnel),
  `AIMLESS_SOCK=<fixture daemon>`, verify send/receive.
- Bump client to the next minor, package + `check_dist`, push `v2` then `main`;
  rebuild/publish the webtop image to GHCR (entrypoint changed).
