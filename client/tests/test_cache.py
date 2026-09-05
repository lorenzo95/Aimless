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


def _write_legacy_format(path, data, passphrase):
    """Legacy on-disk format: nonce||ct with the deterministic pre-0.5.11 salt."""
    salt = hashlib.sha256(b"aimless-cache" + kdf_key(passphrase, b"aimless-cache-salt")).digest()[:16]
    box = nacl.secret.SecretBox(kdf_key(passphrase, salt))
    with open(path, "wb") as f:
        f.write(box.encrypt(json.dumps(data).encode()))


def test_cache_salt_is_per_file(tmp_path):
    p1 = str(tmp_path / "a.json.enc")
    p2 = str(tmp_path / "b.json.enc")
    c1 = crypto.Cache(p1, "same-pw")
    c2 = crypto.Cache(p2, "same-pw")
    assert c1.salt != c2.salt, "same passphrase must not reuse a deterministic salt"
    assert c1._key != c2._key
    c1.add_recv("aa", "aa", 1, 100, "hi")  # first mutation writes the file
    c3 = crypto.Cache(p1, "same-pw")
    assert c3.salt == c1.salt, "salt persists with the file, so reloads re-derive the same key"
    assert c3._key == c1._key


def test_cache_legacy_format_rewrites_with_header(tmp_path):
    path = str(tmp_path / "legacy.json.enc")
    data = {"conversations": {
        "aa": {"dm": True, "members": {}, "msgs": [], "recv_last": {"aa": 1},
               "sent_last": {}, "scan_last": {"aa": 1}}},
        "muted": [], "pending": []}
    _write_legacy_format(path, data, "pw")

    legacy_salt = hashlib.sha256(b"aimless-cache" + kdf_key("pw", b"aimless-cache-salt")).digest()[:16]
    c = crypto.Cache(path, "pw")
    assert c.recv_last("aa", "aa") == 1, "legacy file must decrypt and load"
    assert c.salt != legacy_salt, "file must be re-keyed with a fresh random salt"

    with open(path, "rb") as f:
        header_line = f.readline()
    header = json.loads(header_line)
    assert header["v"] == 2 and header.get("salt"), "file must be rewritten with a header"

    c2 = crypto.Cache(path, "pw")
    assert c2.recv_last("aa", "aa") == 1, "rewritten file must round-trip"


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
