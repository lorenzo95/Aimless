"""Local, encrypted, SQLite-backed conversation state.

This replaces the hand-rolled encrypted JSON cache and its three cursors
(recv_last / sent_last / scan_last) plus list-scan dedup. The model is a pure
projection of the daemon journal:

  * one monotonic ingest cursor per peer (``watermark``) advances to the highest
    seq seen in that peer's stream, whether or not this client could route it —
    so a peer's shared inbox stream is never re-scanned;
  * message identity is a primary key (``in:<sender>:<seq>`` / a deterministic
    outgoing id), so replay and double-fetch are no-ops via INSERT OR IGNORE;
  * unread and history dismissal are derived from timestamps, not cursors.

Sensitive fields (text, attachment JSON, member rosters, screen names, pending
payloads) are encrypted at rest with a passphrase-derived SecretBox key; routing
metadata (conv id, sender node, ts, seq, flags) is plaintext, as before.
"""

import base64
import json
import os
import sqlite3
import threading
import uuid

import nacl.secret
import nacl.utils

from . import crypto

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    dm         INTEGER NOT NULL DEFAULT 1,
    members    BLOB,                       -- encrypted roster JSON
    muted      INTEGER NOT NULL DEFAULT 0,
    hidden     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,           -- in:<sender>:<seq> | out:<deterministic>
    conv       TEXT NOT NULL,
    sender     TEXT NOT NULL,              -- peer node hex, or 'self'
    dir        TEXT NOT NULL,              -- 'in' | 'out'
    ts         INTEGER NOT NULL,
    seq        INTEGER NOT NULL DEFAULT 0,
    seqs       TEXT NOT NULL,              -- {node: seq}
    text       BLOB,
    attachment BLOB
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv, ts);
CREATE TABLE IF NOT EXISTS watermark (
    peer TEXT PRIMARY KEY,
    seq  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS read_marks (
    conv      TEXT PRIMARY KEY,
    upto_ts   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending (
    node    TEXT PRIMARY KEY,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS blocked_screens (
    node   TEXT PRIMARY KEY,
    screen BLOB
);
CREATE TABLE IF NOT EXISTS muted_nodes (
    node TEXT PRIMARY KEY
);
-- Per outgoing message: one row per (recipient, seq). A message is "delivered"
-- when every row has been acked by that peer's daemon; rows with acked=0 are
-- still in flight. Legacy outgoing messages have no rows and render with no
-- tick.
CREATE TABLE IF NOT EXISTS deliveries (
    node   TEXT    NOT NULL,
    seq    INTEGER NOT NULL,
    msg_id TEXT    NOT NULL,
    acked  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node, seq)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_msg ON deliveries(msg_id);
"""


class Store:
    def __init__(self, path: str, passphrase: str):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        try:
            self.conn.execute("ALTER TABLE messages ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass
        salt = self._salt()
        self.box = nacl.secret.SecretBox(crypto.kdf_key(passphrase, salt))

    # -- encryption helpers -------------------------------------------------
    def _salt(self) -> bytes:
        row = self.conn.execute("SELECT value FROM meta WHERE key='salt'").fetchone()
        if row:
            return base64.b64decode(row[0])
        salt = nacl.utils.random(16)
        self.conn.execute("INSERT INTO meta(key, value) VALUES('salt', ?)",
                          (base64.b64encode(salt).decode(),))
        self.conn.commit()
        return salt

    def _enc(self, s):
        if s is None:
            return None
        return self.box.encrypt(s.encode("utf-8"))

    def _dec(self, blob):
        if blob is None:
            return None
        return self.box.decrypt(bytes(blob)).decode("utf-8")

    def close(self) -> None:
        self.conn.close()

    # -- ingest cursor (one per peer) --------------------------------------
    def cursor(self, peer: str) -> int:
        row = self.conn.execute("SELECT seq FROM watermark WHERE peer=?", (peer,)).fetchone()
        return row[0] if row else 0

    def set_cursor(self, peer: str, seq: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO watermark(peer, seq) VALUES(?, ?) "
                "ON CONFLICT(peer) DO UPDATE SET seq=MAX(seq, excluded.seq)",
                (peer, seq))
            self.conn.commit()

    # -- conversations -----------------------------------------------------
    def _ensure_conv(self, conv: str, dm: bool) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO conversations(id, dm, members, muted, hidden) VALUES(?, ?, ?, 0, 0)",
            (conv, 1 if dm else 0, self._enc("{}")))

    def ensure_room(self, conv: str, members: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO conversations(id, dm, members, hidden) VALUES(?, 0, ?, 0) "
                "ON CONFLICT(id) DO UPDATE SET dm=0, members=excluded.members, hidden=0",
                (conv, self._enc(json.dumps(members))))
            self.conn.commit()

    def rooms(self) -> list:
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM conversations WHERE dm=0 AND hidden=0")]

    def members(self, conv: str) -> dict:
        row = self.conn.execute("SELECT members FROM conversations WHERE id=?", (conv,)).fetchone()
        if not row or row[0] is None:
            return {}
        return json.loads(self._dec(row[0]))

    def is_room(self, conv: str) -> bool:
        row = self.conn.execute("SELECT dm FROM conversations WHERE id=?", (conv,)).fetchone()
        return bool(row) and row[0] == 0

    def delete_room(self, conv: str, scan_points: dict = None) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE conv=?", (conv,))
            self.conn.execute("DELETE FROM read_marks WHERE conv=?", (conv,))
            self.conn.execute("UPDATE conversations SET hidden=1 WHERE id=?", (conv,))
            self.conn.commit()
        # Advance the per-peer watermark past the dismissed backlog so a
        # resurrecting room starts from the new messages only.
        for node, latest in (scan_points or {}).items():
            if latest:
                self.set_cursor(node, latest)

    def clear_history(self, conv: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE conv=?", (conv,))
            self.conn.execute("DELETE FROM read_marks WHERE conv=?", (conv,))
            self.conn.commit()

    # -- messages ----------------------------------------------------------
    def ingest(self, conv: str, sender: str, seq: int, ts: int, text: str,
               attachment: dict = None) -> bool:
        """Route one decrypted inbound message. Idempotent by (sender, seq)."""
        mid = f"in:{sender}:{seq}"
        with self._lock:
            self._ensure_conv(conv, dm=(conv == sender))
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages(id, conv, sender, dir, ts, seq, seqs, text, attachment) "
                "VALUES(?, ?, ?, 'in', ?, ?, ?, ?, ?)",
                (mid, conv, sender, ts, seq, json.dumps({sender: seq}), self._enc(text),
                 self._enc(json.dumps(attachment)) if attachment else None))
            if cur.rowcount:
                self.conn.execute("UPDATE conversations SET hidden=0 WHERE id=?", (conv,))
            self.conn.commit()
            return cur.rowcount > 0

    def add_sent(self, conv: str, seqs: dict, ts: int, text: str,
                 attachment: dict = None, delivery_keys=None) -> bool:
        """Record an outbound message; deterministic id makes retries no-ops.

        ``delivery_keys`` is the full set of (node, seq) acks to track. It
        defaults to ``seqs`` but files pass every chunk key (the daemon acks
        each chunk), so delivery reflects the whole transfer, not just its last
        chunk."""
        mid = self.out_id(seqs)
        if delivery_keys is None:
            keys = {(n, int(s)) for n, s in seqs.items()}
        else:
            keys = {(n, int(s)) for n, s in delivery_keys if int(s)}
        with self._lock:
            self._ensure_conv(conv, dm=True)
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages(id, conv, sender, dir, ts, seqs, text, attachment) "
                "VALUES(?, ?, 'self', 'out', ?, ?, ?, ?)",
                (mid, conv, ts, json.dumps(seqs), self._enc(text),
                 self._enc(json.dumps(attachment)) if attachment else None))
            for node, seq in keys:
                self.conn.execute(
                    "INSERT OR IGNORE INTO deliveries(node, seq, msg_id, acked) VALUES(?, ?, ?, 0)",
                    (node, seq, mid))
            self.conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def out_id(seqs: dict) -> str:
        return "out:" + ",".join(f"{n}={s}" for n, s in sorted(seqs.items()))

    def is_delivered(self, mid: str):
        row = self.conn.execute(
            "SELECT COUNT(*), SUM(acked) FROM deliveries WHERE msg_id=?", (mid,)).fetchone()
        if not row or not row[0]:
            return None
        total, acked = row[0], row[1] or 0
        return acked == total

    def delivery_progress(self, mid: str):
        """(total, acked) tracked chunks for a message; (0, 0) if untracked."""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(acked), 0) FROM deliveries WHERE msg_id=?",
            (mid,)).fetchone()
        return row[0], row[1]

    def undelivered(self, mid: str):
        return [(n, s) for n, s in self.conn.execute(
            "SELECT node, seq FROM deliveries WHERE msg_id=? AND acked=0", (mid,))]

    def mark_delivered(self, node: str, seq: int):
        """Mark one chunk acked. Returns the message id it belongs to, or None."""
        with self._lock:
            row = self.conn.execute(
                "SELECT msg_id, acked FROM deliveries WHERE node=? AND seq=?",
                (node, int(seq))).fetchone()
            if row is None:
                return None
            mid, acked = row
            if not acked:
                self.conn.execute(
                    "UPDATE deliveries SET acked=1 WHERE node=? AND seq=?", (node, int(seq)))
                self.conn.commit()
            return mid

    def _delivery_summary(self, mids):
        summary = {}
        ids = list({m for m in mids if m})
        if not ids:
            return summary
        marks = ",".join("?" for _ in ids)
        for mid, total, acked in self.conn.execute(
                f"SELECT msg_id, COUNT(*), SUM(acked) FROM deliveries "
                f"WHERE msg_id IN ({marks}) GROUP BY msg_id", ids):
            summary[mid] = (total, acked)
        return summary

    def _row_to_msg(self, row, summary=None) -> dict:
        mid, conv, sender, direction, ts, seqs, text, att = row
        msg = {
            "id": mid, "dir": direction, "seqs": json.loads(seqs), "ts": ts, "sender": sender,
            "text": self._dec(text),
            "attachment": json.loads(self._dec(att)) if att is not None else None,
        }
        if direction == "out":
            s = (summary or {}).get(mid)
            # None = untracked (legacy message): render no tick; False = in
            # flight; True = every recipient chunk acked.
            msg["delivered"] = None if s is None else (s[0] == s[1])
        return msg

    def messages(self, conv: str) -> list:
        rows = self.conn.execute(
            "SELECT id, conv, sender, dir, ts, seqs, text, attachment FROM messages "
            "WHERE conv=? ORDER BY ts, id", (conv,)).fetchall()
        summary = self._delivery_summary([r[0] for r in rows])
        return [self._row_to_msg(r, summary) for r in rows]

    # Compatibility aliases: the GTK layer historically spoke the cache's
    # per-conversation cursor vocabulary. Progress is now a single per-peer
    # watermark, so scan/set_scan delegate to it.
    def add_recv(self, conv: str, sender: str, seq: int, ts: int, text: str,
                 attachment: dict = None) -> bool:
        return self.ingest(conv, sender, seq, ts, text, attachment)

    def msgs(self, conv: str) -> list:
        return self.messages(conv)

    def recv_last(self, conv: str, node: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(seq) FROM messages WHERE conv=? AND dir='in' AND sender=?",
            (conv, node)).fetchone()
        return row[0] or 0

    def scan_last(self, conv: str, node: str) -> int:
        return self.cursor(node)

    def set_scan_last(self, conv: str, node: str, seq: int) -> None:
        self.set_cursor(node, seq)

    # -- read / unread -----------------------------------------------------
    def mark_read(self, conv: str) -> None:
        row = self.conn.execute("SELECT MAX(ts) FROM messages WHERE conv=?", (conv,)).fetchone()
        upto = row[0] or 0
        with self._lock:
            self.conn.execute(
                "INSERT INTO read_marks(conv, upto_ts) VALUES(?, ?) "
                "ON CONFLICT(conv) DO UPDATE SET upto_ts=MAX(upto_ts, excluded.upto_ts)",
                (conv, upto))
            self.conn.commit()

    def unread(self, conv: str) -> int:
        row = self.conn.execute("SELECT upto_ts FROM read_marks WHERE conv=?", (conv,)).fetchone()
        upto = row[0] if row else 0
        return self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conv=? AND dir='in' AND ts > ?",
            (conv, upto)).fetchone()[0]

    def total_unread(self) -> int:
        return sum(self.unread(c) for c, in self.conn.execute(
            "SELECT id FROM conversations WHERE dm=0 OR dm=1"))

    # -- mute / block ------------------------------------------------------
    def is_conversation_muted(self, conv: str) -> bool:
        row = self.conn.execute("SELECT muted FROM conversations WHERE id=?", (conv,)).fetchone()
        return bool(row and row[0])

    def mute_conversation(self, conv: str) -> None:
        with self._lock:
            self._ensure_conv(conv, dm=True)
            self.conn.execute("UPDATE conversations SET muted=1 WHERE id=?", (conv,))
            self.conn.commit()

    def unmute_conversation(self, conv: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE conversations SET muted=0 WHERE id=?", (conv,))
            self.conn.commit()

    def is_muted(self, node: str) -> bool:
        return self.conn.execute("SELECT 1 FROM muted_nodes WHERE node=?", (node,)).fetchone() is not None

    def mute(self, node: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO muted_nodes(node) VALUES(?)", (node,))
            self.conn.commit()

    def unmute(self, node: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM muted_nodes WHERE node=?", (node,))
            self.conn.commit()

    def set_blocked_screen(self, node: str, screen: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO blocked_screens(node, screen) VALUES(?, ?) "
                "ON CONFLICT(node) DO UPDATE SET screen=excluded.screen", (node, self._enc(screen)))
            self.conn.commit()

    def blocked_screen(self, node: str):
        row = self.conn.execute("SELECT screen FROM blocked_screens WHERE node=?", (node,)).fetchone()
        return self._dec(row[0]) if row and row[0] is not None else None

    def clear_blocked_screen(self, node: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM blocked_screens WHERE node=?", (node,))
            self.conn.commit()

    # -- pending contact requests -----------------------------------------
    def add_pending(self, req: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO pending(node, payload) VALUES(?, ?)",
                (req.get("node"), self._enc(json.dumps(req))))
            self.conn.commit()

    def pending(self) -> list:
        return [json.loads(self._dec(r[0])) for r in
                self.conn.execute("SELECT payload FROM pending ORDER BY rowid")]

    def pending_pop(self):
        row = self.conn.execute("SELECT node, payload FROM pending ORDER BY rowid LIMIT 1").fetchone()
        if row is None:
            return None
        with self._lock:
            self.conn.execute("DELETE FROM pending WHERE node=?", (row[0],))
            self.conn.commit()
        return json.loads(self._dec(row[1]))
