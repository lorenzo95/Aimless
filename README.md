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

## Download and run

No installer. Two files, wget them into a folder and run:

```sh
mkdir aimless && cd aimless
wget https://raw.githubusercontent.com/lorenzo95/Aimless/main/dist/aimlessd-linux-amd64
wget https://raw.githubusercontent.com/lorenzo95/Aimless/main/dist/aimless.pyz
chmod +x aimlessd-linux-amd64 aimless.pyz

./aimlessd-linux-amd64 &        # 1. the daemon joins the Yggdrasil mesh
./aimless.pyz init              # 2. one-time: identity (passphrase + screen name)
./aimless.pyz --version         #    should print: aimless 0.2.8
./aimless.pyz                   # 3. tray + messages window
```

Note the `./` — Linux does not search the current directory.

Requires: Linux, python3 + `pip install pynacl` (client), `python3-gi` (distro package, for the GTK window). The daemon itself has zero dependencies — it's a static binary.

If you previously installed an older aimless via pip, **uninstall it** — its `aimless` command still contains the old tkinter GUI and will shadow the current app:

```sh
pip uninstall aimless-client
```

Check what you're running: `./aimless.pyz --version` → `aimless 0.2.8`. The window title bar and the Activity tab show the same versions. If `--version` says something else, you downloaded a stale file — use the commit-pinned URL shown in the release notes.

Build from source instead: see [Development](#development) below.

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
| `client/` | Python | `aimless` CLI — identity, contacts, encrypted history cache; **GTK desktop app** (`aimless gui`) — buddy list, conversations, contacts management, tray |
| `deploy/` | — | Dockerfile + compose (two-node demo) + smoke test + release packager |

## Quick start (from source)

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

# 4. run everything — tray + daemon + messages window
aimless

# the tray owns the daemon: closing the messages window closes just the window,
# clicking the tray icon reopens it (no re-typing your passphrase while the
# same daemon instance is alive), and tray > Quit shuts the whole stack down.
# after a daemon restart you unlock once more.

# or headless:
aimless invite                     # send this string to a friend
aimless add "<their invite>" bob   # paste theirs
aimless chat bob                   # REPL; /away <msg>, /back, /quit
aimless list                       # buddy list: online/away + away messages
```

## Desktop integration

```sh
aimless tray       # tray daemon: supervises aimlessd, lives in the notification area,
                   # click to open Messages, Quit shuts down the whole stack
aimless autostart  # installs the login autostart entry for the full stack
aimless stop       # shut everything down from the CLI
```

## Deployment

```sh
cd deploy
python3 smoke.py       # two-node docker compose demo incl. offline delivery
                       # (falls back to local daemons if docker is unavailable)
./package.sh           # builds dist/aimless-dist.tar.gz + wget-able release artifacts
```

`docker-compose.yml` runs two daemons on an internal network; unix sockets surface through the bind mounts (`data-a/`, `data-b/`).

## How delivery works

- **Both online** — packets flow directly over the Yggdrasil overlay (end-to-end encrypted sessions, source-key authenticated).
- **Buddy offline** — the sender's daemon journals every message and retries (default every 2s, and instantly when the buddy's path comes back). Delivered messages are ACKed; the journal drains.
- **History** — the receiving daemon keeps the last N (default 50) messages per buddy in an encrypted-at-rest store and replays them to clients with an explicit gap floor. Your client's encrypted cache is the long-term archive.
- **Presence** — daemons probe watched buddies (default every 15s); any packet from a buddy marks them seen. STATUS blobs carry screen name + away message, encrypted per buddy.

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

The Yggdrasil session layer already authenticates the sender's node key and encrypts everything between nodes; the envelope types on top are:

- **MSG** — `payload` is a NaCl sealed box made by the *sender's client* to the recipient's Curve25519 key. The daemon cannot read it. Delivered messages are ACKed (see below); the sender's journal retries until then.
- **ACK** — `payload` empty, `seq` echoes the confirmed MSG. Not journaled; a probe ACK is intentionally indistinguishable from noise to the journal (probe seqs are never in it).
- **STATUS** — `payload` is a sealed box containing the sender's screen name and away message, re-sent with every presence probe. The receiving daemon stores only the latest opaque blob per buddy.
- **PROBE** — `payload` empty. Presence ping; answered by *any* packet, which is what flips the buddy to "online".

### Inside a MSG payload (decrypted by the recipient's client)

```json
{"v": 1, "kind": "msg", "from": "<64-hex client pubkey>", "body": "<json>", "sig": "<b64>"}
```

`body` is the canonical JSON `{"text": …, "ts": …}` and `sig` is the sender's Ed25519 signature over `aimless\x01 + body`. The recipient verifies the signature against the claimed `from` key — sender authenticity is enforced at the client layer, independently of the transport.

### Envelope payloads the daemon can see

| Envelope type | What the daemon knows | What it can't know |
|---|---|---|
| MSG | destination address, seq, timestamp, size | message text, sender's screen name |
| ACK | which seq was confirmed | what the message said |
| STATUS | source address, timestamp | screen name, away message |
| PROBE | that the buddy exists | anything else |

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

## Development

```sh
cd daemon && go test ./...      # codec, journals, delivery, presence, loopback integration
cd client && pytest tests/      # crypto, cache, protocol, real-daemon e2e, GTK runtime
cd deploy && python3 smoke.py   # two-node deployment incl. offline delivery
./deploy/package.sh             # dist tarball + release artifacts (see Download and run)
```

## Roadmap

- LAN multicast discovery ("nearby buddies")
- DHT screen-name directory (decentralized first-come registration)
- TOC bridge for real retro clients
- Photo/file chunking, PoW stamps for unknown senders

## Status

Experimental. Both Yggdrasil and aimless are alpha software — do not use for security-critical purposes.
