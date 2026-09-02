# aimless

Serverless chat with an AIM heart. Bitmessage's architecture (decentralized, end-to-end encrypted, store-and-forward), AIM's face (screen names, buddy list, away messages), Yggdrasil's legs (embedded transport — no TUN, no NAT traversal, no ports to forward).

## Download and run

```sh
mkdir -p ~/.local/bin && cd ~/.local/bin

wget https://raw.githubusercontent.com/lorenzo95/Aimless/main/dist/aimlessd-linux-amd64
wget https://raw.githubusercontent.com/lorenzo95/Aimless/main/dist/aimless.pyz

chmod +x aimlessd-linux-amd64 aimless.pyz

./aimless.pyz init    # once: passphrase + screen name
./aimless.pyz         # tray + daemon + messages window
./aimless.pyz autostart   # optional: start the whole stack at login
```

That's the whole setup. The tray owns the daemon: closing the window closes just the window, clicking the tray icon reopens it, and tray `Quit` shuts everything down. `./aimless.pyz --version` tells you which build you're running.

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

## Build from source

```sh
cd daemon && go build -o aimlessd . && ./aimlessd -datadir ~/.local/share/aimless
cd ../client && pip install . && aimless
```

## Security model

- **Identity** = client Ed25519 keypair (PyNaCl). Your invite string contains your client key (what buddies encrypt to) and your daemon's node key (where to route). The Yggdrasil address is derived from the node key — permanent, unspoofable.
- **End-to-end encryption** — NaCl sealed boxes per recipient, made by the client. Messages are signed by the sender's identity key.
- **The daemon never sees plaintext.** It journals ciphertext, retries until ACKed, and relays presence blobs it cannot read.
- **No plaintext on disk anywhere.** History lives in an encrypted local cache (passphrase-derived scrypt key); the identity keyfile is passphrase-encrypted the same way.

## Components

| Piece | Language | Role |
|---|---|---|
| `daemon/` | Go | `aimlessd` — embedded yggdrasil core (no TUN), packet transport, journals, retry/ACK, presence probing, local JSON API on a Unix socket |
| `client/` | Python | `aimless` CLI — identity, contacts, encrypted history cache; GTK desktop app — buddy list, conversations, contacts management, tray |
| `deploy/` | — | Dockerfile + compose (two-node demo) + smoke test + `package.sh` release builder |

## Packet format

Everything between daemons is a single datagram over ironwood's encrypted `PacketConn` (end-to-end encrypted sessions keyed by the nodes' Ed25519 keys — the source address of every packet is cryptographically authenticated). One datagram = one envelope.

### Envelope (20-byte header + payload, all integers little-endian)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | `version` | `1` |
| 1 | 1 | `type` | `1` MSG · `2` ACK · `3` STATUS · `4` PROBE |
| 2 | 8 | `seq` | `uint64`, monotonic per sending buddy |
| 10 | 8 | `ts` | sender clock, unix milliseconds |
| 18 | 2 | `payload_len` | `uint16`, max 65535 |
| 20 | n | `payload` | see below |

The Yggdrasil session layer already authenticates the sender's node key and encrypts everything between nodes. Envelope types on top:

- **MSG** — `payload` is a NaCl sealed box made by the *sender's client* to the recipient's Curve25519 key. The daemon cannot read it. Delivered messages are ACKed; the sender's journal retries until then.
- **ACK** — `payload` empty, `seq` echoes the confirmed MSG. Not journaled.
- **STATUS** — `payload` is a sealed box containing the sender's screen name and away message, re-sent with every presence probe. The receiving daemon stores only the latest opaque blob per buddy.
- **PROBE** — `payload` empty. Presence ping; answered by *any* packet, which is what flips the buddy to "online".

Status is **announce-and-refresh, never stored**: the sender's app re-announces its current status on startup and every 60s, and the daemon re-sends the latest blob with each probe. That way every side converges from scratch within one probe cycle after any restart, and nobody but the sender ever holds their status. If someone's app is fully quit, they show offline — which is the truth. (Messages, by contrast, are durable store-and-forward.)

### Inside a MSG payload (decrypted by the recipient's client)

```json
{"v": 1, "kind": "msg", "from": "<64-hex client pubkey>", "body": "<json>", "sig": "<b64>"}
```

`body` is the canonical JSON `{"text": …, "ts": …}` plus, when the sender's client includes it, `"screen"` (their screen name) and — for group conversations — `"conv"` (the room id: a SHA-256 of the sorted member node keys, so identical member sets always agree on the conversation) and `"members"` (the full member set as `{"node", "pubkey", "screen"}` triplets, carried in every room message since there is no server to ask). `sig` is the sender's Ed25519 signature over `aimless\x01 + body`. The recipient verifies the signature against the claimed `from` key — sender authenticity is enforced at the client layer, independently of the transport.

### Rooms and contact requests

- **Rooms are conversations with 3+ members**, delivered by client-side fan-out: the sender seals one copy per member and the daemon's normal retry/ACK/offline machinery handles each copy. Membership is learned from the messages themselves (advisory by design — a member can restate it, but every message is individually signed, so nobody can impersonate anyone). The sidebar shows a liveness dot and online count (`● 6/10`); the conversation header shows one presence dot per member plus clickable member chips — a non-buddy chip adds them as a buddy (identity as claimed by the room roster; direct invite exchange is stronger). History is fetched per conversation: the daemon journals everything a buddy sends you in one stream, and each conversation scans that stream keeping only its own messages — DMs and rooms never mix. Clearing history or deleting a room **dismisses** the existing backlog (the scan cursor moves past it) — only messages that arrive afterwards are shown; deleted rooms reappear if someone sends to them again, containing just those new messages. **Muting** a room keeps it in the sidebar, dimmed and silent (no badges, no previews, no request popups) while history still stores; unmute restores everything. All of this is local-only — you can't be removed from someone else's roster, and they can't be removed from yours; "leaving" means your side stops caring.
- **First contact is a handshake**: a chat message from someone not in your contacts pops an *Accept / Deny* dialog (persisted if the app is closed). **Accept** adds them and delivers the message; **Deny** mutes the node — their messages are dropped silently. Adding someone's invite later un-mutes them. All participants need aimless ≥ 0.5.0 for rooms; older clients simply see room messages as ordinary DMs from the sender.

### What the daemon knows vs. can't know

| Envelope type | Daemon knows | Daemon can't know |
|---|---|---|
| MSG | destination address, seq, timestamp, size | message text, sender's screen name |
| ACK | which seq was confirmed | what the message said |
| STATUS | source address, timestamp | screen name, away message |
| PROBE | that the buddy exists | anything else |

## Local API (Unix socket, newline-JSON)

| Op | Reply / Event |
|---|---|
| `whoami` | `{"op":"whoami","address":"200:…","key":"<node pk hex>","pid":n}` |
| `status` | peers, MTU, build |
| `send {to, payload}` | `{"op":"queued","seq":n}` |
| `history {from, seq}` | `{"op":"history","msgs":[…],"oldest":n,"latest":m}` |
| `watch {to}` | start probing a buddy (persisted) |
| `setstatus {to, payload}` | encrypted away/screen blob |
| `presence` | per-buddy online + opaque status blob |
| event `recv` | `{"op":"recv","from":…,"seq":n,"ts":t,"payload":b64}` |
| event `acked` | `{"op":"acked","to":…,"seq":n}` |

## Tests

```sh
cd daemon && go test ./...      # codec, journals, delivery, presence, loopback integration
cd client && pytest tests/      # crypto, cache, protocol, real-daemon e2e, GTK runtime
cd deploy && python3 smoke.py   # two-node deployment incl. offline delivery
./deploy/package.sh             # builds the versioned release artifacts in dist/
```

## Status

Experimental. Both Yggdrasil and aimless are alpha software — do not use for security-critical purposes.
