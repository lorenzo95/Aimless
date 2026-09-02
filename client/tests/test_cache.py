import hashlib
import json

import nacl.secret
import pytest

from aimless import crypto
from aimless.crypto import kdf_key


def _write_legacy(path, buddies, passphrase="legacy-pw"):
    salt = hashlib.sha256(b"aimless-cache" + kdf_key(passphrase, b"aimless-cache-salt")).digest()[:16]
    box = nacl.secret.SecretBox(kdf_key(passphrase, salt))
    with open(path, "wb") as f:
        f.write(box.encrypt(json.dumps({"buddies": buddies}).encode()))


def test_cache_add_and_persist(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    c1 = crypto.Cache(path, "pw")
    assert c1.add_recv("aa", "aa", 1, 100, "hi") is True
    assert c1.add_recv("aa", "aa", 1, 100, "hi") is False
    assert c1.add_sent("aa", {"aa": 1}, 101, "yo") is True
    c2 = crypto.Cache(path, "pw")
    msgs = c2.msgs("aa")
    assert len(msgs) == 2
    assert msgs[0]["sender"] == "aa"
    assert msgs[1]["sender"] == "self"
    assert c2.recv_last("aa", "aa") == 1


def test_cache_wrong_passphrase(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    crypto.Cache(path, "right").add_recv("aa", "aa", 1, 100, "hi")
    with pytest.raises(Exception):
        crypto.Cache(path, "wrong")


def test_cache_conversation_isolation(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    c = crypto.Cache(path, "pw")
    c.add_recv("aa", "aa", 1, 100, "for aa")
    c.add_recv("bb", "bb", 5, 200, "for bb")
    assert c.recv_last("aa", "aa") == 1
    assert c.recv_last("bb", "bb") == 5
    assert len(c.msgs("aa")) == 1


def test_cache_room_dedup_is_per_sender(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    c = crypto.Cache(path, "pw")
    conv = "roomhash"
    c.ensure_room(conv, {"n1": {"pubkey": "pk1", "screen": "One"},
                         "n2": {"pubkey": "pk2", "screen": "Two"}})
    assert c.rooms() == [conv]
    assert c.add_recv(conv, "n1", 1, 100, "from one") is True
    assert c.add_recv(conv, "n2", 1, 101, "from two") is True
    assert c.add_recv(conv, "n1", 1, 100, "replay") is False
    assert len(c.msgs(conv)) == 2
    assert c.members(conv)["n2"]["screen"] == "Two"
    assert c.add_sent(conv, {"n1": 3, "n2": 4}, 102, "to both") is True
    assert c.add_sent(conv, {"n1": 3, "n2": 4}, 102, "to both") is False
    assert len([m for m in c.msgs(conv) if m["dir"] == "out"]) == 1


def test_cache_muted_and_pending(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    c = crypto.Cache(path, "pw")
    assert c.is_muted("n9") is False
    c.mute("n9")
    assert crypto.Cache(path, "pw").is_muted("n9") is True
    c.unmute("n9")
    assert c.is_muted("n9") is False

    c.add_pending({"node": "n1", "text": "hi"})
    assert c.pending()[0]["node"] == "n1"
    c.add_pending({"node": "n1", "text": "dup ignored"})
    assert len(c.pending()) == 1
    req = c.pending_pop()
    assert req["node"] == "n1"
    assert c.pending_pop() is None


def test_cache_migrates_legacy_buddies(tmp_path):
    path = str(tmp_path / "legacy.json.enc")
    _write_legacy(path, {
        "aa": {"msgs": [
            {"dir": "in", "seq": 2, "ts": 100, "text": "hello"},
            {"dir": "out", "seq": 3, "ts": 110, "text": "hi back"},
        ], "recv_last": 2, "sent_last": 3},
    })
    c = crypto.Cache(path, "legacy-pw")
    assert "aa" in c._data["conversations"]
    conv = c._data["conversations"]["aa"]
    assert conv["dm"] is True
    assert conv["recv_last"] == {"aa": 2}
    assert conv["sent_last"] == {"aa": 3}
    assert conv["msgs"][0] == {"dir": "in", "seqs": {"aa": 2}, "ts": 100,
                               "text": "hello", "sender": "aa"}
    assert conv["msgs"][1]["sender"] == "self"
    assert c.recv_last("aa", "aa") == 2
    # migrated-once: reloading does not re-migrate or duplicate
    c2 = crypto.Cache(path, "legacy-pw")
    assert len(c2.msgs("aa")) == 2


def test_room_id_order_independent():
    from aimless import protocol
    assert protocol.room_id(["aa", "bb", "cc"]) == protocol.room_id(["cc", "aa", "bb"])
    assert protocol.room_id(["aa", "bb"]) != protocol.room_id(["aa", "bb", "cc"])
