import os

import pytest

from aimless import crypto, logging
from aimless import gtkui


def test_log_fn_appends_and_stamps(tmp_path):
    path = str(tmp_path / "x.log")
    log = logging.log_fn(path)
    log("one")
    log("two")
    lines = open(path).read().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("one") and lines[1].endswith("two")
    assert lines[0].startswith("[")


def test_log_fn_rotates(tmp_path):
    path = str(tmp_path / "r.log")
    log = logging.log_fn(path, max_bytes=64)
    for i in range(50):
        log("x" * 40)
    assert os.path.exists(path + ".1"), "rotation never happened"
    assert os.path.getsize(path) <= 64 + 64


def test_corrupt_cache_recovers(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cache = home / "cache.json.enc"
    cache.write_bytes(b"garbage-not-a-zip-enc-blob")

    from aimless import gtkui
    session = gtkui.Session.__new__(gtkui.Session)

    import nacl
    recovered = None
    try:
        c = crypto.Cache(str(cache), "pw")
    except Exception as e:
        recovered = e
        os.replace(str(cache), str(cache) + ".bad")
        c = crypto.Cache(str(cache), "pw")
    assert recovered is not None
    assert os.path.exists(str(cache) + ".bad")
    assert c.buddy("aa") == {"msgs": [], "recv_last": 0, "sent_last": 0}


def test_corrupt_cache_session_recovery(tmp_path, monkeypatch):
    home = tmp_path / "home2"
    home.mkdir()
    cache = home / "cache.json.enc"
    cache.write_bytes(b"broken")

    monkeypatch.setattr(gtkui, "cache_path", lambda: str(cache))
    from aimless.daemon import DaemonClient

    class FakeDaemon:
        pass

    identity = crypto.new_identity()
    monkeypatch.setattr(gtkui, "identity_path", lambda: str(tmp_path / "no-identity.json"))
    monkeypatch.setattr("aimless.crypto.load_identity", lambda p, pw: crypto.new_identity())
    monkeypatch.setattr(gtkui, "sock_path", lambda: "/nonexistent.sock")

    session = gtkui.Session.__new__(gtkui.Session)
    session.identity = identity
    session.cache_recovered = None
    try:
        session.cache = crypto.Cache(str(cache), "pw")
    except Exception as e:
        os.replace(str(cache), str(cache) + ".bad")
        session.cache = crypto.Cache(str(cache), "pw")
        session.cache_recovered = str(e)
    assert session.cache_recovered
    assert os.path.exists(str(cache) + ".bad")
