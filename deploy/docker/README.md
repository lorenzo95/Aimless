# aimless in a Docker web desktop (noVNC)

Runs the aimless GUI + daemon inside a minimal **Alpine** container. No desktop
stack is installed — just Xvfb (`:1`), the tiny `openbox` window manager,
`x11vnc` + `noVNC` for browser access, and `supervisord` supervising every
process with `autorestart`.

Everything runs as the non-root user `aimless` (uid 1000). The entrypoint only
runs as root long enough to fix the ownership of the bind-mounted `/data`
volume.

## Why this shape

Same pattern as `~bitmessage-docker`: a virtual display, the app launched in
the foreground, and supervisord restarts it whenever it exits. Because there is
**no system tray** in the container, aimless falls back to window mode — closing
the window quits the app, and supervisord immediately starts it again. So the
aimless window in your browser is effectively "always on".

The daemon connects to the overlay out of the box: with no `config.json` it
uses the built-in public Yggdrasil relay (see `daemon/main.go` `defaultPeers`).

## Setup (one time)

### Option A — pull from GHCR (recommended)

```bash
docker run -d --name aimless-webtop --restart unless-stopped \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:5900:5900 \
  -e VNC_PASS="${VNC_PASS:-aimless}" \
  -v "$PWD/aimless-data:/data" \
  ghcr.io/lorenzo95/aimless/aimless-webtop:latest
```

or with compose (the repo's `deploy/docker/docker-compose.yml` already points at
the GHCR image):

```bash
cd deploy/docker
docker compose up -d
```

### Option B — build locally

```bash
cd deploy/docker
./build.sh     # docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

The container is a fixed **environment**; the client (`aimless.pyz`) and the
daemon (`aimlessd-linux-amd64`, a static binary) are **fetched from the git
`dist/` artifacts on first start** into `./aimless-data/bin/` (a bind-mounted
volume), so they persist across restarts and you never rebuild the image for a
new aimless release.

No extra steps needed: on first run the browser window shows a **create identity**
dialog (passphrase + confirm + screen name). Fill it in and you're set.

## Updating to a new aimless release

No image rebuild — just fetch a newer client/daemon (and, when a new container
release exists, pull the fresh image):

```bash
# latest client+daemon on main, existing image
AIMLESS_FETCH=always docker compose up -d --force-recreate

# pull the newest published container, then latest artifacts
docker compose pull && AIMLESS_FETCH=always docker compose up -d --force-recreate

# or pin to a specific git ref (branch / commit sha)
AIMLESS_VERSION=v0.8.0 docker compose up -d --force-recreate
```

`AIMLESS_FETCH=always` forces a re-fetch on the next start; otherwise the
fetched artifacts are reused until the pinned `AIMLESS_VERSION` changes or you
delete them (`rm aimless-data/bin/*`). The `AIMLESS_VERSION` env is the seam a
future auto-updater can drive.

## Use

1. Open <http://localhost:8080/vnc.html> in a browser.
   Enter the noVNC password (`VNC_PASS`, default `aimless`).
2. First run: the **create identity** dialog appears — pick a passphrase and a
   screen name behind it.
   Later runs show the standard unlock prompt instead.
3. Add buddies in the **Contacts** tab (paste their `aimless1:…` invite).

Restart behaviour (there is no system tray in the container, so aimless runs in
plain window mode):

- **Cancel / Escape** on the identity dialog, and **closing the window**, both
  quit the app; supervisord restarts it within a second. You can never be stuck
  on a black screen — a dialog or window is always up.

## Daemon-only mode (`AIMLESS_MODE=daemon_only`)

Set `AIMLESS_MODE=daemon_only` to run **just the daemon** — no Xvfb, no openbox,
no VNC, no noVNC, no GUI. That leaves a single `aimlessd` process (supervised,
autorestart) acting as a 24/7 mailbox. It's the RAM-cheap way to keep your node
reachable and store messages while your laptop is off.

```bash
AIMLESS_MODE=daemon_only docker compose up -d --force-recreate
```

The daemon writes its socket to `<datadir>/api.sock`, i.e. the host bind-mount
path `aimless-data/state/api.sock`. From your laptop, connect the desktop GUI
to it over an SSH tunnel (see below) — no ports exposed.

### Typical workflow — init in the browser, then go headless

1. Start with the default `webtop` mode and create your identity through the
   browser UI (`http://localhost:8080/vnc.html`). This initialises
   `aimless-data/state/` (identity, keys, config).
2. Set `AIMLESS_MODE=daemon_only` in your compose `.env` (or run with the env
   var) and `docker compose up -d --force-recreate` again.
3. On your laptop, run the desktop GUI and enable **Remote daemon (SSH)** from
   the hamburger menu (see the client README). Point it at the SSH host and the
   `api.sock` path on the host (`…/aimless-data/state/api.sock`). The client
   opens the tunnel and talks to the remote daemon as if it were local.

The web/VNC ports are still bound loopback-only in daemon-only mode; they're
just idle.

> **One GUI per daemon socket.** Don't run two GUIs against the same daemon
> socket at the same time (e.g. the webtop GUI *and* your laptop GUI
> simultaneously). Both receive every message and both may ACK attachments, so
> the first to finish a file transfer frees the daemon's copy before the other
> can fetch it. Use one at a time — either the webtop GUI or the remote laptop
> GUI.

Useful while the container runs:

```bash
docker exec -it aimless-webtop /opt/aimless/aimless.pyz list
docker exec -it aimless-webtop /opt/aimless/aimless.pyz away "bbl"
docker exec -it aimless-webtop /opt/aimless/aimless.pyz send <buddy> "hi"
```

## Persistence

Everything lives in `./aimless-data/` (mounted at `/data`):
`state/` holds the identity, contacts, cache, node key and the daemon socket;
`config/` holds the daemon pid file and a `state/config.json` if you ever want
custom peers:

```json
{"peers": ["tcp://nodea:9001"], "listen": []}
```

## Stop / remove

```bash
docker compose down
rm -rf aimless-data   # (from deploy/docker)
```

## Notes

- **Architecture**: the fetched daemon is the released `aimlessd-linux-amd64`
  (static, x86-64); the container is therefore x86-64 only. If you need arm64,
  the daemon is a trivial `CGO_ENABLED=0 go build` — see the repo `daemon/`.
- **Internet exposure**: a VPS on the public internet should sit behind a
  reverse proxy with TLS + basic auth (or SSH tunnel), since noVNC + the VNC
  password alone are thin protection for a remote host.