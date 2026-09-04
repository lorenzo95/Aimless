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

```bash
# From the aimless repo root
cd deploy/docker
./build.sh            # syncs dist/aimless.pyz + daemon sources, then compose up
```

No extra steps needed: on first run the browser window shows a **create identity**
dialog (passphrase + confirm + screen name). Fill it in and you're set.

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

- **arm64 hosts**: work out of the box — the daemon is built from source in the
  image's `golang:alpine` stage with `CGO_ENABLED=0`, so the platform matches
  whatever host builds it. No host toolchain needed.
- **Internet exposure**: a VPS on the public internet should sit behind a
  reverse proxy with TLS + basic auth (or SSH tunnel), since noVNC + the VNC
  password alone are thin protection for a remote host.