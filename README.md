# aimless

Serverless, end-to-end-encrypted chat over the [Yggdrasil](https://yggdrasil-network.github.io/) overlay. Message 1:1 or in group rooms, send files, and keep chatting when the other side is offline — no servers, no accounts, no ports to forward, no TUN device and no root. It's AIM for the mesh era: screens names, buddy lists, away messages, and durable store-and-forward.

- **Serverless & account-free** — identity is an Ed25519 keypair; your address is derived from it.
- **End-to-end encrypted** — per-recipient NaCl sealed boxes made by the client; the relay never sees plaintext.
- **Durable** — messages and files are journaled and retried until delivered, even if the recipient is offline.
- **Linux desktop + CLI** — a GTK app with a tray, plus a headless CLI.

---

## Try it in 60 seconds (Docker)

Runs the full GUI in your browser — nothing to install:

```sh
docker run -d --name aimless-webtop --restart unless-stopped \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:5900:5900 \
  -e VNC_PASS="aimless" \
  -v "$PWD/aimless-data:/data" \
  ghcr.io/lorenzo95/aimless/aimless-webtop:latest
```

Open <http://localhost:8080/vnc.html> and enter the password (`aimless`). First run asks you to create an identity (passphrase + screen name).

**See two clients talk to each other** — start a second container on another port/volume, then in the first window open *Contacts → Copy* and paste the invite into the second window's *Add a buddy*:

```sh
docker run -d --name aimless-webtop2 --restart unless-stopped \
  -p 127.0.0.1:8081:8080 \
  -e VNC_PASS="aimless" \
  -v "$PWD/aimless-data2:/data" \
  ghcr.io/lorenzo95/aimless/aimless-webtop:latest
```

Open <http://localhost:8081/vnc.html>, add each other, and chat. Turn one off and send from the other — it arrives when it comes back.

> The container has no audio and no notification daemon, so sound/notifications are silent there by design. Exposing the page publicly? Put it behind TLS + auth or an SSH tunnel, and change `VNC_PASS` — whoever reaches it gets your desktop.

## Install on Linux

```sh
mkdir -p ~/.local/bin && cd ~/.local/bin
wget https://raw.githubusercontent.com/lorenzo95/Aimless/main/dist/aimlessd-linux-amd64
wget https://raw.githubusercontent.com/lorenzo95/Aimless/main/dist/aimless.pyz
chmod +x aimlessd-linux-amd64 aimless.pyz
./aimless.pyz            # first run creates your identity; after that: unlock + tray + window
./aimless.pyz autostart  # optional: start the whole stack at login
./aimless.pyz --version  # which build you're running
```

Requires Linux, `python3` with `pynacl`, and the GTK stack (`python3-gi`, `gir1.2-gtk-3.0`). The daemon (`aimlessd`) is a static binary with no dependencies. If an old pip-installed client exists, remove it first (`pip uninstall aimless-client`).

**From source:**

```sh
cd daemon && go build -o aimlessd . && ./aimlessd -datadir ~/.local/share/aimless
cd ../client && pip install . && aimless
```

## Using it

The tray owns the daemon: closing the window just hides it (tray click reopens; tray *Quit* shuts everything down).

### Preferences
Header menu → **Preferences …** (stored in `~/.config/aimless/gtk.json`):

- **Remember window position and size** (off-screen-safe; best-effort on Wayland) with a *Reset* button.
- **Desktop notifications** (libnotify / `notify-send`) and **notification sound** — Off, single, double, triple, or long synthesised beep (no audio files, with a *Test* button; `AIMLESS_SOUND` overrides).
- **Enter to send**, **24h/12h timestamps**.
- **Theme** — System (follows desktop light/dark live), Aimless Dark, Mocha, Nord, Tokyo Night, Latte, Dawn, plus **Matrix** and **Amber CRT** (monospace). `AIMLESS_THEME` overrides.

The window title and tray tooltip show total unread. Sent text shows a delivered tick; files show per-recipient progress (`1/3 delivered` → `Delivered ✓`).

### Keys & identity
Header menu → **Keys & Identity …**:

- **Yggdrasil node key** — view your address and set a specific key (paste 64/128-hex from a vanity generator), generate a random one, or reset to default. The address is previewed; applying restarts the daemon. Changing it changes your address, so old invites go stale.
- **Client identity** — replace your encryption key from a 64-hex seed. This breaks existing conversations; restart aimless afterwards.
- **Backup & restore** — one passphrase-encrypted bundle (identity seed, node key, screen name, contacts). Restoring overwrites your keys.

Keys are written `0600` and atomically; seeds are never logged.

### CLI
`aimless init`, `invite`, `add <invite> [petname]`, `list`, `send`, `chat`, `away`, `gui`, `tray`, `stop` — see `aimless --help`.

---

## How it works

```
┌──────────┐  unix socket   ┌─────────┐   encrypted packets   ┌─────────┐  unix socket  ┌──────────┐
│  client  │ ─────────────▶ │ aimlessd│ ────────────────────▶ │ aimlessd│ ────────────▶ │  client  │
│ (Python) │   NDJSON       │  (Go)   │   Yggdrasil overlay   │  (Go)   │   NDJSON      │ (Python) │
└──────────┘                └─────────┘                       └─────────┘               └──────────┘
   plaintext                  ciphertext                        ciphertext                plaintext
   in RAM only                journals on disk                  journals on disk          in RAM only
```

The **client** owns identity, encryption, and display. The **daemon** is a dumb, durable relay: it journals ciphertext, retries until ACKed, stores file chunks, and probes presence — it **never sees plaintext**. Transport is an embedded (no TUN) Yggdrasil node; each peer is addressed by its node key.

| Piece | Language | Role |
|---|---|---|
| `daemon/` | Go | embedded Yggdrasil core, packet transport, journals/retry/ACK, attachments, blocklist, presence, local JSON API on a Unix socket |
| `client/` | Python | identity, contacts, encrypted SQLite state, GTK app (rooms, files, themes, prefs) + CLI |
| `deploy/` | — | webtop image, `package.sh` release builder, `check_dist.py` gate, two-node smoke test |

## Security model

- **Identity** = a client Ed25519 keypair. Your invite (`aimless1:<client-pk>:<node-pk>:<screen>`) carries the client key (what friends encrypt to) and the node key (where to route). The Yggdrasil address is derived from the node key.
- **E2E encryption** — per-recipient NaCl sealed boxes made by the client; every message is Ed25519-signed, verified against the claimed sender independently of the transport.
- **The daemon can't read messages, names, filenames, or status.** It sees only routing metadata (above).
- **No forward secrecy** — long-term keys, no ratchet. Anyone with your private key can decrypt past traffic; treat it like email, not Signal.
- **Encrypted at rest** — history is an encrypted SQLite DB (`state.db`, scrypt key); the identity file likewise. The **one exception is attachment bytes**, written to `~/.local/share/aimless/attachments/<conv>/` in plaintext so images can render/save locally.
- Experimental alpha — don't use for security-critical purposes.

## Under the hood

**Packet format.** Each datagram is an envelope over ironwood's encrypted `PacketConn`: a 20-byte header (`version=1`, `type`, `seq` uint64, `ts` ms, `payload_len`) plus payload. Types: `MSG`, `ACK`, `STATUS`, `PROBE`, `FILE`.

- **MSG** — sealed box from the sender's client. ACKed; the sender retries until then.
- **STATUS** — sealed screen/away blob, re-sent with probes, never stored by the receiver.
- **PROBE** — presence ping; any packet back marks the peer online. Status is *announce-and-refresh*, so everyone converges after a restart and offline means offline.
- **FILE** — 20-byte cleartext routing header (transfer id + chunk index/total) + sealed chunk body. The daemon keys/joins chunks, but filename, MIME, hash, and contents stay encrypted. Chunks persist until the client reassembles and verifies (SHA-256), ACKs consumption.

**Messages.** Inside a MSG payload: `{"v":1,"kind":"msg","from":<hex>,"body":<canonical JSON>,"sig":<b64>}`, where `body` is `{"text","ts"}` plus optional `screen`, and for rooms `conv` (SHA-256 of sorted member node keys) and `members`. `sig` is Ed25519 over `aimless\x01 + body`.

**Rooms** are 3+ members via client-side fan-out (one sealed copy per member, normal retry/ACK). Membership is learned from signed messages, so nobody can impersonate anyone. History is one per-peer sync stream routed by `conv` with a single watermark; clearing/deleting dismisses the backlog; mute keeps history but stays silent.

**What the daemon knows vs. can't:**

| Type | Knows | Can't know |
|---|---|---|
| MSG | destination, seq, timestamp, size | text, screen name |
| FILE | transfer id, chunk index/total, size | filename, MIME, hash, contents |
| STATUS/PROBE | source, timestamp | screen name, away message |

**Local API** (Unix socket, newline-JSON; requests may carry an `id` echoed on the reply): `whoami`, `status`, `send`, `sendfile`, `history`, `watch`, `setstatus`, `presence`, `block`/`unblock`/`blocklist`, `pendingattachments`/`fetchattachment`/`ackfile`; events `recv` and `acked`.

## Development

```sh
cd daemon && go test ./...      # codec, journals, delivery, presence, integration
cd client && pytest tests/      # crypto, protocol, store, real-daemon e2e, GTK
cd deploy && python3 smoke.py   # two-node deployment incl. offline delivery
./deploy/package.sh             # build versioned dist/ artifacts
python3 deploy/check_dist.py    # release gate: committed dist matches source
```

## Status

Experimental. Both Yggdrasil and aimless are alpha — not for security-critical use.

---

*Developed with heavy AI assistance: most of the code, packaging, and this README are produced collaboratively with large language models. Review carefully and audit what you run; releases and images are human-reviewed and tested before publishing.*
