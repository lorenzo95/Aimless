import pytest

from aimless import crypto


def test_cache_add_and_persist(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    c1 = crypto.Cache(path, "pw")
    assert c1.add_recv("aa", 1, 100, "hi") is True
    assert c1.add_recv("aa", 1, 100, "hi") is False
    assert c1.add_sent("aa", 1, 101, "yo") is True
    c2 = crypto.Cache(path, "pw")
    msgs = c2.msgs("aa")
    assert len(msgs) == 2
    assert c2.recv_last("aa") == 1


def test_cache_wrong_passphrase(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    crypto.Cache(path, "right").add_recv("aa", 1, 100, "hi")
    with pytest.raises(Exception):
        crypto.Cache(path, "wrong")


def test_cache_buddy_isolation(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    c = crypto.Cache(path, "pw")
    c.add_recv("aa", 1, 100, "for aa")
    c.add_recv("bb", 5, 200, "for bb")
    assert c.recv_last("aa") == 1
    assert c.recv_last("bb") == 5
    assert len(c.msgs("aa")) == 1
