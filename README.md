# aimless

Serverless chat with an AIM heart. Bitmessage's architecture (decentralized, end-to-end encrypted, store-and-forward), AIM's face (screen names, buddy list, away messages), Yggdrasil's legs (embedded transport — no TUN, no NAT traversal, no ports to forward).

```
┌──────────┐  unix socket   ┌─────────┐   encrypted packets   ┌─────────┐  unix socket  ┌──────────┐
│  client  │ ─────────────▶ │ aimlessd│ ────────────────────▶ │ aimlessd│ ────────────▶ │  client  │
│ (Python) │   NDJSON       │  (Go)   │   Yggdrasil overlay   │  (Go)   │   NDJSON      │ (Python) │
└──────────┘                └─────────┘                       └─────────┘               └──────────┘
   plaintext                  ciphertext                        ciphertext                plaintext
   in RAM only                journals on disk                  journals on disk          in RAM only
```

## Security model

- **Identity** = client Ed25519 keypair (PyNaCl). Your invite string contains your client key (what buddies encrypt to) and your daemon's node key (where to route). The Yggdrasil address is derived from the node key — permanent, unspoofable.
- **End-to-end encryption** — NaCl sealed boxes per recipient, made by the client. Messages are signed by the sender's identity key.
- **The daemon never sees plaintext.** It journals ciphertext, retries until ACKed, and relays presence blobs it cannot read.
- **No plaintext on disk anywhere.** The client keeps history in an encrypted local cache (passphrase-derived scrypt key). The identity keyfile is passphrase-encrypted the same way.
- Presence probes carry no content; screen names and away messages travel inside encrypted STATUS blobs.

## Components

| Piece | Language | Role |
|---|---|---|
| `daemon/` | Go | `aimlessd` — embedded yggdrasil core (no TUN), packet transport, journals, retry/ACK, presence probing, local JSON API on a Unix socket |
| `client/` | Python | `aimless` CLI — identity, contacts, encrypted history cache; **GTK desktop app** (`aimless gui`) — buddy list, conversations, contacts management, tray, daemon supervisor |
| `deploy/` | — | Dockerfile + compose (two-node demo) + smoke test |

## Quick start

Two terminals on one machine (or two machines; each user runs their own daemon):

```sh
# 1. build + run the daemon (joins the public Yggdrasil network)
cd daemon
go build -o aimlessd .
./aimlessd -datadir ~/.local/share/aimless
# prints your address (200::/7) and node pubkey

# 2. install the client (GUI needs the GTK stack: sudo apt install python3-gi)
cd ../client
pip install .

# 3. create your identity
aimless init

# 4. chat
aimless gui            # full desktop app — starts the daemon for you if needed,
                       # buddies, conversations, contacts, tray on close

# or headless:
aimless invite                     # send this string to a friend
aimless add "<their invite>" bob   # paste theirs
aimless chat bob                   # REPL; /away <msg>, /back, /quit
aimless list                       # buddy list: online/away + away messages
```

## Desktop integration

```sh
aimless tray       # tray daemon: supervises aimlessd, lives in the notification area,
                   # click to open Messages, menu to stop the daemon
aimless autostart  # installs the login autostart entry for the tray
```

Closing the Messages window hides it to the tray; the daemon keeps receiving while you're gone. `Quit` in the tray menu stops the tray — the daemon itself keeps running unless you explicitly stop it.


## Deployment

```sh
cd deploy
python3 smoke.py       # two-node docker compose demo incl. offline delivery
                       # (falls back to local daemons if docker is unavailable)
```

`docker-compose.yml` runs two daemons on an internal network; unix sockets surface through the bind mounts (`data-a/`, `data-b/`).

## How delivery works

- **Both online** — packets flow directly over the Yggdrasil overlay (end-to-end encrypted sessions, source-key authenticated).
- **Buddy offline** — the sender's daemon journals every message and retries (default every 2s, and instantly when the buddy's path comes back). Delivered messages are ACKed; the journal drains.
- **History** — the receiving daemon keeps the last N (default 50) messages per buddy in an encrypted-at-rest store and replays them to clients with an explicit gap floor. Your client's encrypted cache is the long-term archive.
- **Presence** — daemons probe watched buddies (default every 15s); any packet from a buddy marks them seen. STATUS blobs carry screen name + away message, encrypted per buddy.

## Local API (Unix socket, newline-JSON)

| Op | Reply / Event |
|---|---|
| `whoami` | `{"op":"whoami","address":"200:…","key":"<node pk hex>"}` |
| `status` | peers, MTU, build |
| `send {to, payload}` | `{"op":"queued","seq":n}` |
| `history {from, seq}` | `{"op":"history","msgs":[…],"oldest":n,"latest":m}` |
| `watch {to}` | start probing a buddy (persisted) |
| `setstatus {to, payload}` | encrypted away/screen blob |
| `presence` | per-buddy online + opaque status blob |
| event `recv` | `{"op":"recv","from":…,"seq":n,"ts":t,"payload":b64}` |
| event `acked` | `{"op":"acked","to":…,"seq":n}` |

Wire envelope: `version(1) · type(1: MSG/ACK/STATUS/PROBE) · seq(8) · ts(8) · len(2) · payload` — one datagram per message over ironwood's encrypted `PacketConn`.

## Tests

```sh
cd daemon && go test ./...      # codec, journals, delivery, presence, loopback integration
cd client && pytest tests/      # crypto, cache, protocol, real-daemon e2e
cd deploy && python3 smoke.py   # two-node deployment incl. offline delivery
```

## Roadmap

- LAN multicast discovery ("nearby buddies")
- DHT screen-name directory (decentralized first-come registration)
- TOC bridge for real retro clients
- Photo/file chunking, PoW stamps for unknown senders

## Status

Experimental. Both Yggdrasil and aimless are alpha software — do not use for security-critical purposes.
