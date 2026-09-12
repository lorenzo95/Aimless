import pytest

from aimless.store import Store


def test_store_add_and_persist(tmp_path):
    path = str(tmp_path / "state.db")
    c1 = Store(path, "pw")
    assert c1.ingest("aa", "aa", 1, 100, "hi") is True
    assert c1.ingest("aa", "aa", 1, 100, "hi") is False, "replay must be idempotent"
    assert c1.add_sent("aa", {"aa": 1}, 101, "yo") is True
    c1.close()
    c2 = Store(path, "pw")
    msgs = c2.messages("aa")
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "aa"
    assert msgs[1]["sender"] == "self"
    assert c2.recv_last("aa", "aa") == 1
    c2.close()


def test_store_wrong_passphrase(tmp_path):
    path = str(tmp_path / "state.db")
    Store(path, "right").ingest("aa", "aa", 1, 100, "secret text")
    with pytest.raises(Exception):
        Store(path, "wrong").messages("aa")


def test_store_conversation_isolation(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    c.ingest("aa", "aa", 1, 100, "for aa")
    c.ingest("bb", "bb", 5, 200, "for bb")
    assert c.recv_last("aa", "aa") == 1
    assert c.recv_last("bb", "bb") == 5
    assert len(c.messages("aa")) == 1


def test_store_room_dedup_is_per_sender(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    conv = "roomhash"
    c.ensure_room(conv, {"n1": {"pubkey": "pk1", "screen": "One"},
                         "n2": {"pubkey": "pk2", "screen": "Two"}})
    assert c.rooms() == [conv]
    assert c.ingest(conv, "n1", 1, 100, "from one") is True
    assert c.ingest(conv, "n2", 1, 101, "from two") is True
    assert c.ingest(conv, "n1", 1, 100, "replay") is False
    assert len(c.messages(conv)) == 2
    assert c.members(conv)["n2"]["screen"] == "Two"
    assert c.add_sent(conv, {"n1": 3, "n2": 4}, 102, "to both") is True
    assert c.add_sent(conv, {"n1": 3, "n2": 4}, 102, "to both") is False
    assert len([m for m in c.messages(conv) if m["dir"] == "out"]) == 1


def test_store_cursor_is_monotonic(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    assert c.cursor("aa") == 0
    c.set_cursor("aa", 5)
    c.set_cursor("aa", 3)  # never goes backwards
    assert c.cursor("aa") == 5


def test_store_unread_and_mark_read(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    c.ingest("aa", "aa", 1, 100, "one")
    c.ingest("aa", "aa", 2, 200, "two")
    assert c.unread("aa") == 2
    c.mark_read("aa")
    assert c.unread("aa") == 0
    c.ingest("aa", "aa", 3, 300, "three")
    assert c.unread("aa") == 1


def test_store_delete_room_hides_and_advances_cursor(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    conv = "roomhash"
    c.ensure_room(conv, {"n1": {"pubkey": "pk", "screen": "One"}})
    c.ingest(conv, "n1", 1, 100, "old")
    c.delete_room(conv, {"n1": 9})
    assert c.rooms() == []
    assert c.messages(conv) == []
    assert c.cursor("n1") == 9
    # a new message resurrects the room with only the new content
    c.ingest(conv, "n1", 10, 500, "new")
    assert c.rooms() == [conv]
    assert [m["text"] for m in c.messages(conv)] == ["new"]


def test_store_muted_and_pending(tmp_path):
    path = str(tmp_path / "state.db")
    c = Store(path, "pw")
    assert c.is_muted("n9") is False
    c.mute("n9")
    assert Store(path, "pw").is_muted("n9") is True
    c.unmute("n9")
    assert c.is_muted("n9") is False

    c.add_pending({"node": "n1", "text": "hi"})
    assert c.pending()[0]["node"] == "n1"
    c.add_pending({"node": "n1", "text": "dup ignored"})
    assert len(c.pending()) == 1
    req = c.pending_pop()
    assert req["node"] == "n1"
    assert c.pending_pop() is None


def test_store_blocked_screen_roundtrip(tmp_path):
    path = str(tmp_path / "state.db")
    node = "aa" * 32
    c = Store(path, "pw")
    assert c.blocked_screen(node) is None
    c.set_blocked_screen(node, "Spammy")
    assert c.blocked_screen(node) == "Spammy"
    c2 = Store(path, "pw")
    assert c2.blocked_screen(node) == "Spammy", "blocked screen must persist across reload"
    c2.clear_blocked_screen(node)
    assert c2.blocked_screen(node) is None
    c3 = Store(path, "pw")
    assert c3.blocked_screen(node) is None, "cleared blocked screen must stay cleared"


def test_store_outgoing_delivery_tracking(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    seqs = {"n1": 5, "n2": 6}
    assert c.add_sent("conv", seqs, 100, "hi", delivery_keys={("n1", 5), ("n2", 6)}) is True
    mid = c.out_id(seqs)
    assert c.is_delivered(mid) is False
    msg = c.messages("conv")[0]
    assert (msg["delivery_total"], msg["delivered_count"], msg["delivered"]) == (2, 0, False)
    c.mark_delivered("n1", 5)
    assert c.is_delivered(mid) is False, "one recipient outstanding"
    msg = c.messages("conv")[0]
    assert (msg["delivery_total"], msg["delivered_count"], msg["delivered"]) == (2, 1, False)
    c.mark_delivered("n2", 6)
    assert c.is_delivered(mid) is True
    assert c.messages("conv")[0]["delivered"] is True


def test_store_file_delivery_spans_all_chunks(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    seqs = {"n1": 9}  # message identity is the last chunk's seq
    keys = {("n1", 1), ("n1", 2), ("n1", 9)}
    c.add_sent("conv", seqs, 100, "file.bin", delivery_keys=keys)
    mid = c.out_id(seqs)
    assert c.is_delivered(mid) is False
    for s in (1, 2, 9):
        c.mark_delivered("n1", s)
    assert c.is_delivered(mid) is True


def test_store_legacy_outgoing_has_no_tick(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    c.conn.execute(
        "INSERT INTO messages(id, conv, sender, dir, ts, seqs, text) "
        "VALUES('out:x','c','self','out',1,'{}',NULL)")
    c.conn.commit()
    assert c.messages("c")[0]["delivered"] is None, "untracked legacy message renders no tick"


def test_room_id_order_independent():
    from aimless import protocol
    assert protocol.room_id(["aa", "bb", "cc"]) == protocol.room_id(["cc", "aa", "bb"])
    assert protocol.room_id(["aa", "bb"]) != protocol.room_id(["aa", "bb", "cc"])
