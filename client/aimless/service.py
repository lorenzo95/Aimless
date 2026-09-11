"""Conversation sync: the one place that fetches, decrypts, routes and stores.

The daemon journals everything a peer ever sent us in a single per-peer stream.
Previously each conversation scanned and filtered that stream separately, which
is why three cursors, repeated decryption and per-conversation ingestion existed.
Here every entry is decrypted once, routed by its ``conv`` field, and stored;
one watermark per peer tracks progress. Any caller can sync any peer and every
conversation benefits.

``fetch`` only talks to the daemon (safe on a worker thread); ``apply`` only
touches the store (run it on the UI thread). ``sync_peer`` is the synchronous
convenience for the CLI.
"""

from . import protocol


class Sync:
    def __init__(self, client, store):
        self.client = client
        self.store = store

    def fetch(self, peers) -> dict:
        """Fetch the new tail of each peer's stream. Daemon I/O only."""
        return {p: self.client.history(p, self.store.cursor(p)) for p in peers}

    def apply(self, peer: str, resp: dict) -> int:
        """Route a fetched history response into the store. Returns new count."""
        after = self.store.cursor(peer)
        max_seen = after
        added = 0
        for m in resp.get("msgs", []):
            seq = m.get("seq", 0)
            if seq > max_seen:
                max_seen = seq
            try:
                opened = protocol.open_message(self.client.identity, m["payload"])
            except (ValueError, KeyError):
                continue
            _conv, new = self._route(peer, seq, opened)
            if new:
                added += 1
        # Advance even when nothing routed (message for another conversation,
        # or undecryptable) so the shared stream is never re-scanned.
        if max_seen > after:
            self.store.set_cursor(peer, max_seen)
        return added

    def sync_peer(self, peer: str) -> int:
        return self.apply(peer, self.fetch([peer])[peer])

    def sync_peers(self, peers) -> int:
        return sum(self.apply(p, r) for p, r in self.fetch(peers).items())

    def on_event(self, event: dict):
        """Handle a live daemon recv event. Returns (conv, is_new)."""
        peer = event.get("from")
        if not peer:
            return None, False
        try:
            opened = self.client.decrypt_recv(event)
        except (ValueError, KeyError):
            return None, False
        return self.ingest_opened(peer, event.get("seq", 0), opened)

    def ingest_opened(self, peer: str, seq: int, opened: dict):
        """Store an already-decrypted message and advance the peer watermark."""
        conv, new = self._route(peer, seq, opened)
        if seq > self.store.cursor(peer):
            self.store.set_cursor(peer, seq)
        return conv, new

    def _route(self, peer: str, seq: int, opened: dict):
        conv = opened.get("conv") or peer
        members = opened.get("members")
        if members:
            self.store.ensure_room(conv, {m["node"]: m for m in members})
        new = self.store.ingest(conv, peer, seq, opened["ts"], opened["text"])
        return conv, new
