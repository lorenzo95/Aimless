# aimless

Serverless, decentralized, end-to-end-encrypted chat with durable
store-and-forward. Converse 1:1 or in group rooms, send file attachments, and
share clickable links — no servers, no accounts, no ports to forward. The
transport is an embedded Yggdrasil overlay.

## Try it in your browser (Docker)

Want to see it without installing anything? Run the web desktop container —
it gives you the full aimless GUI in a browser at `http://localhost:8080`:

```sh
docker run -d --name aimless-webtop --restart unless-stopped \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:5900:5900 \
  -e VNC_PASS="aimless" \
  -v "$PWD/aimless-data:/data" \
  ghcr.io/lorenzo95/aimless/aimless-webtop:latest
```

Open <http://localhost:8080/vnc.html> and enter the VNC password (`aimless`
unless you changed `VNC_PASS`). First run walks you through creating your
identity in the browser — no command-line setup. More on this below in
[Run it in a browser — Docker web desktop](#run-it-in-a-browser--docker-web-desktop).

## Download and run

```sh
mkdir -p ~/.local/bin && cd ~/.local/bin

wget https://raw.githubusercontent.com/lorenzo95/Aimless/main/dist/aimlessd-linux-amd64
wget https://raw.githubusercontent.com/lorenzo95/Aimless/main/dist/aimless.pyz

chmod +x aimlessd-linux-amd64 aimless.pyz

./aimless.pyz         # first run: the window asks you to create your identity
                      #   (passphrase + screen name) — no separate step needed
./aimless.pyz         # afterwards: unlock + tray + daemon + messages window
./aimless.pyz autostart   # optional: start the whole stack at login
```

That's the whole setup. On the very first launch the GUI shows a *create your
identity* dialog instead of the unlock prompt, so there's nothing to run before
it. For headless or scripted setups, `./aimless.pyz init` still creates the
identity from the command line. The tray owns the daemon: closing the window
closes just the window, clicking the tray icon reopens it, and tray `Quit`
shuts everything down. (Without a tray — e.g. in the Docker web desktop below —
closing the window quits the app, which is what lets a supervisor restart it.)
`./aimless.pyz --version` tells you which build you're running.

Requires: Linux, python3 + `pip install pynacl`, and `python3-gi` + `gir1.2-gtk-3.0` for the window (distro packages). The daemon is a static binary with zero dependencies.

If an older pip-installed aimless exists on the machine, remove it first — its `aimless` command contains an outdated GUI: `pip uninstall aimless-client`.

```
┌──────────┐  unix socket   ┌─────────┐   encrypted packets   ┌─────────┐  unix socket  ┌──────────┐
│  client  │ ─────────────▶ │ aimlessd│ ────────────────────▶ │ aimlessd│ ────────────▶ │  client  │
│ (Python) │   NDJSON       │  (Go)   │   Yggdrasil overlay   │  (Go)   │   NDJSON      │ (Python) │
└──────────┘                └─────────┘                       └─────────┘               └──────────┘
   plaintext                  ciphertext                        ciphertext                plaintext
   in RAM only                journals on disk                  journals on disk          in RAM only
```

## Reach a remote daemon over SSH

Aimless normally runs the daemon as a child of the desktop app, so when your
laptop is off, your node is offline too. To keep a node online 24/7 — and have
it store-and-forward while you're away — run a daemon somewhere always-on (a
VPS, or the webtop container in `AIMLESS_MODE=daemon_only`, see the docker
README) and point the desktop GUI at it over an **SSH tunnel**.

The daemon's API is a Unix socket (`api.sock`). The GUI's **Remote daemon
(SSH)** settings (hamburger menu) opens a tunnel that creates a local
`remote-api.sock` whose traffic is forwarded, encrypted, to the remote socket —
so no API port is ever exposed, and auth is your normal SSH key/agent.

- In the settings dialog, fill in the SSH **host** (`user@host`), the **remote
  socket path** (the `api.sock` path on the SSH host, e.g. `…/aimless-data/
  state/api.sock` for the container's bind mount), optionally an identity key,
  and hit **Test connection**.
- On save, restart the app to apply. The local daemon stays dormant; the GUI
  talks to the remote one.
- Everything that needs the daemon (history, attachments, presence) works over
  the tunnel exactly as if it were local.

> **One GUI per daemon socket.** Don't run two GUIs against the same daemon
> socket at the same time — e.g. the webtop GUI *and* your laptop GUI
> simultaneously. Both receive every message and both may ACK attachments, so
> the first to finish a file transfer frees the daemon's copy before the other
> can fetch it. Use one at a time.

## Build from source

```sh
cd daemon && go build -o aimlessd . && ./aimlessd -datadir ~/.local/share/aimless
cd ../client && pip install . && aimless
```

## Run it in a browser — Docker web desktop

The webtop image runs the aimless GUI in a browser from a minimal Alpine
container: Xvfb + the tiny `openbox` window manager + `x11vnc` + `noVNC`,
everything supervised and non-root (uid 1000) — the same pattern as the
`bitmessage-docker` project. The one-liner at the top of this page gets you
started; this section covers how it works.

First run shows the same *create your identity* dialog in the browser window.
There is no system tray in the container, so closing the window (or cancelling
the identity dialog) quits the app and supervisord restarts it within a second —
the aimless window is effectively always on, and you can never end up on a black
screen. Everything persists in `./aimless-data/` (identity, contacts, encrypted
cache, node key).

The image is a fixed **environment**; the client (`aimless.pyz`) and the static
daemon are fetched from the git `dist/` artifacts on first start into
`./aimless-data/bin/`, so a new release needs no rebuild — just
`AIMLESS_FETCH=always docker restart aimless-webtop` (or pin a ref via
`AIMLESS_VERSION`). The compose file (`deploy/docker/docker-compose.yml`) also
pulls this image and binds the ports to `127.0.0.1` only. If you expose it on a
public host, put it behind a TLS reverse proxy with auth or an SSH tunnel —
and **change `VNC_PASS`** (`aimless` is only the default; whoever can reach the
page gets your desktop with it).

Set **`AIMLESS_MODE=daemon_only`** to run just the daemon — no X/VNC/GUI — as a
RAM-cheap 24/7 mailbox that your laptop GUI reaches over SSH (see *Reach a
remote daemon over SSH* above). Init your identity in the browser first, then
flip the mode.

## Security model

- **Identity** = client Ed25519 keypair (PyNaCl). Your invite string (`aimless1:<client-pk>:<node-pk>:<screen>`, keys base58-encoded) contains your client key (what buddies encrypt to) and your daemon's node key (where to route). The Yggdrasil address is derived from the node key — permanent, unspoofable.
- **End-to-end encryption** — NaCl sealed boxes per recipient, made by the client. Messages are signed by the sender's identity key.
- **The daemon never sees plaintext.** It journals ciphertext, retries until ACKed, and relays presence blobs it cannot read.
- **No forward secrecy.** Identities are long-term keys with no per-message
  ratchet: messages are encrypted to a static public key, so anyone holding your
  private key (or who later compromises it) can decrypt past traffic. This is a
  store-and-forward design — treat it like email, not Signal.
- **No plaintext on disk anywhere** (text). History lives in an encrypted local
  cache (passphrase-derived scrypt key); the identity keyfile is passphrase-
  encrypted the same way. **Attachments are the one exception**: the actual file
  bytes are written to `~/.local/share/aimless/attachments/<conv>/` in plaintext
  (an image must be renderable/saveable locally), while only metadata is in the
  encrypted cache. Also, the daemon learns a file's transfer id and chunk
  index/total — but never its filename, mime type, hash, or contents, which stay
  end-to-end encrypted.

## Components

| Piece | Language | Role |
|---|---|---|
| `daemon/` | Go | `aimlessd` — embedded yggdrasil core (no TUN), packet transport, journals (text + file chunks), retry/ACK, persistent per-peer blocklist, presence probing, local JSON API on a Unix socket |
| `client/` | Python | `aimless` CLI — identity, contacts, encrypted history cache; GTK desktop app — DMs + group rooms, attachments, clickable links, buddy list, presence/away, contacts with mute/block management, tray |
| `deploy/` | — | webtop container image (GHCR) + `package.sh` release builder + `check_dist.py` gate + two-node smoke test; `main.go`/`api.go` daemon |

## Packet format

Everything between daemons is a single datagram over ironwood's encrypted `PacketConn` (end-to-end encrypted sessions keyed by the nodes' Ed25519 keys — the source address of every packet is cryptographically authenticated). One datagram = one envelope.

### Envelope (20-byte header + payload, all integers little-endian)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | `version` | `1` |
| 1 | 1 | `type` | `1` MSG · `2` ACK · `3` STATUS · `4` PROBE · `5` FILE |
| 2 | 8 | `seq` | `uint64`, monotonic per sending buddy |
| 10 | 8 | `ts` | sender clock, unix milliseconds |
| 18 | 2 | `payload_len` | `uint16`, max 65535 |
| 20 | n | `payload` | see below |

The Yggdrasil session layer already authenticates the sender's node key and encrypts everything between nodes. Envelope types on top:

- **MSG** — `payload` is a NaCl sealed box made by the *sender's client* to the recipient's Curve25519 key. The daemon cannot read it. Delivered messages are ACKed; the sender's journal retries until then.
- **ACK** — `payload` empty, `seq` echoes the confirmed MSG. Not journaled.
- **STATUS** — `payload` is a sealed box containing the sender's screen name and away message, re-sent with every presence probe. The receiving daemon stores only the latest opaque blob per buddy.
- **PROBE** — `payload` empty. Presence ping; answered by *any* packet, which is what flips the buddy to "online".
- **FILE** — one chunk of a file transfer. `payload` is a **20-byte unencrypted routing header** (16-byte transfer id + chunk `index`/`total`, little-endian `uint16`s) followed by a sealed chunk body (`kind:"file"`). The daemon reads only the header — enough to key a per-peer attachment store and judge when a transfer is complete — while the filename, mime type, hash and contents stay end-to-end encrypted. Each chunk is ACKed like a MSG; chunks persist in the receiving daemon's attachment store until the client reassembles + verifies the file and ACKs consumption (so files are as durable as text even for an offline recipient).

Status is **announce-and-refresh, never stored**: the sender's app re-announces its current status on startup and every 60s, and the daemon re-sends the latest blob with each probe. That way every side converges from scratch within one probe cycle after any restart, and nobody but the sender ever holds their status. If someone's app is fully quit, they show offline — which is the truth. (Messages, by contrast, are durable store-and-forward.)

### Inside a MSG payload (decrypted by the recipient's client)

```json
{"v": 1, "kind": "msg", "from": "<64-hex client pubkey>", "body": "<json>", "sig": "<b64>"}
```

`body` is the canonical JSON `{"text": …, "ts": …}` plus, when the sender's client includes it, `"screen"` (their screen name) and — for group conversations — `"conv"` (the room id: a SHA-256 of the sorted member node keys, so identical member sets always agree on the conversation) and `"members"` (the full member set as `{"node", "pubkey", "screen"}` triplets, carried in every room message since there is no server to ask). `sig` is the sender's Ed25519 signature over `aimless\x01 + body`. The recipient verifies the signature against the claimed `from` key — sender authenticity is enforced at the client layer, independently of the transport. File chunks use `"kind":"file"` with `body` being the chunk JSON (`transfer_id`, `index`, `total`, `filename`, `mime_hint`, `sha256`, `size`, `data`) — sealed and signed exactly like a text message.

### Rooms and contact requests

- **Rooms are conversations with 3+ members**, delivered by client-side fan-out: the sender seals one copy per member and the daemon's normal retry/ACK/offline machinery handles each copy. Membership is learned from the messages themselves (advisory by design — a member can restate it, but every message is individually signed, so nobody can impersonate anyone). The sidebar shows a liveness dot and online count (`● 6/10`); the conversation header shows one presence dot per member plus clickable member chips — the full roster with names, so nobody is just '+1'. A filled dot means they're your buddy (click opens your DM), a hollow dot means they aren't yet (click offers to add them — identity as claimed by the room roster; direct invite exchange is stronger). History is fetched per conversation: the daemon journals everything a buddy sends you in one stream, and each conversation scans that stream keeping only its own messages — DMs and rooms never mix. Clearing history or deleting a room **dismisses** the existing backlog (the scan cursor moves past it) — only messages that arrive afterwards are shown; deleted rooms reappear if someone sends to them again, containing just those new messages. **Muting** a conversation — a room *or* a 1:1 DM — keeps it in the sidebar, dimmed and silent (no badges, no previews, no request popups) while history still stores; unmute restores everything. All of this is local-only — you can't be removed from someone else's roster, and they can't be removed from yours; "leaving" means your side stops caring.
- **First contact is a handshake**: a chat message from someone not in your contacts pops an *Accept / Deny / Block* dialog:
  - **Accept** adds them and delivers the message.
  - **Deny** is a one-time decline — that message is discarded, the sender stays a stranger, and their *next* message re-prompts you.
  - **Block** is permanent: it mutes + daemon-blocks the node, so nothing of theirs reaches you, and they appear in your Contacts blocked-rows list with an **Unblock** button. You can also block an existing 1:1 contact from the chat header (it confirms, removes them from contacts, and closes the thread) — undo it later from the Contacts blocked rows. Adding someone's invite later un-blocks and un-mutes them. All participants need aimless ≥ 0.5.0 for rooms; older clients simply see room messages as ordinary DMs from the sender.
- **File attachments** (0.7.0+): send up to 20MB as chunks (~32KB each, addressed by SHA-256 for end-to-end integrity); images render inline as thumbnails (click to expand), anything else shows a Save button. Files keep the same durability as text — the receiving daemon holds chunks until the client reassembles, verifies, and ACKs consumption, so a file that arrives while the recipient's app is offline is still delivered via the startup sweep. Rooms simply fan each chunk out per member.

### What the daemon knows vs. can't know

| Envelope type | Daemon knows | Daemon can't know |
|---|---|---|
| MSG | destination address, seq, timestamp, size | message text, sender's screen name |
| ACK | which seq was confirmed | what the message said |
| STATUS | source address, timestamp | screen name, away message |
| PROBE | that the buddy exists | anything else |
| FILE | transfer id, chunk index/total, size | filename, mime type, hash, contents |

## Local API (Unix socket, newline-JSON)

Requests may carry an optional correlation `id`, echoed back on the matching
reply (success or error) so a client can run concurrent requests safely; older
daemons ignore it and stay serialized. `recv` events can additionally carry
`"type":"file"` for file-chunk arrivals.

| Op | Reply / Event |
|---|---|
| `whoami` | `{"op":"whoami","address":"200:…","key":"<node pk hex>","pid":n}` |
| `status` | peers, MTU, build |
| `send {to, payload}` | `{"op":"queued","seq":n}` |
| `sendfile {to, payload}` | queue a TypeFile chunk (same shape as `send`) |
| `block {to}` / `unblock {to}` | `{"op":"blocked"}` / `{"op":"unblocked"}` — persistent per-peer blocklist |
| `blocklist` | `{"op":"blocklist","blocked":[hex,…]}` |
| `history {from, seq}` | `{"op":"history","msgs":[…],"oldest":n,"latest":m}` |
| `watch {to}` | start probing a buddy (persisted) |
| `setstatus {to, payload}` | encrypted away/screen blob |
| `presence` | per-buddy online + opaque status blob |
| `pendingattachments {from}` | `{"op":"pendingattachments","transfers":[{"tid","total","ts"}]}` — complete files waiting for the client |
| `fetchattachment {from, tid}` | `{"op":"fetchattachment","chunks":[…]} — one transfer's stored chunks |
| `ackfile {from, tid}` | `{"op":"ackfile"}` — frees an attachment the client has consumed |
| event `recv` | `{"op":"recv","from":…,"seq":n,"ts":t,"payload":b64}` (,`"type":"file"` for chunks) |
| event `acked` | `{"op":"acked","to":…,"seq":n}` |

## Tests

```sh
cd daemon && go test ./...      # codec, journals, delivery, presence, loopback integration
cd client && pytest tests/      # crypto, cache, protocol, real-daemon e2e, GTK runtime
cd deploy && python3 smoke.py   # two-node deployment incl. offline delivery
./deploy/package.sh             # builds the versioned release artifacts in dist/
python3 deploy/check_dist.py    # release gate: committed dist artifacts match the source
```

## Status

Experimental. Both Yggdrasil and aimless are alpha software — do not use for
security-critical purposes.

---

*Notably, this project is developed with heavy assistance from AI.* Most of
the code, the packaging/build tooling, and this README are produced
collaboratively with large language models — treat the codebase accordingly,
review carefully, and don't hesitate to ask questions. All releases, tests,
and container images are human-reviewed and run through the test suites before
being published, but there is no substitute for your own audit of what you're
running.
