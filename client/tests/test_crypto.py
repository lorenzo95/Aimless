import os

import pytest

from aimless import crypto


def test_identity_roundtrip(tmp_path):
    path = str(tmp_path / "identity.json")
    identity = crypto.new_identity()
    crypto.save_identity(path, identity, "hunter2")
    assert os.path.exists(path)
    loaded = crypto.load_identity(path, "hunter2")
    assert bytes(loaded.verify_key) == bytes(identity.verify_key)
    assert bytes(loaded) == bytes(identity)


def test_identity_wrong_passphrase(tmp_path):
    path = str(tmp_path / "identity.json")
    crypto.save_identity(path, crypto.new_identity(), "correct")
    with pytest.raises(ValueError):
        crypto.load_identity(path, "wrong")


def test_identity_tampered(tmp_path):
    path = str(tmp_path / "identity.json")
    crypto.save_identity(path, crypto.new_identity(), "pw")
    with open(path) as f:
        data = f.read()
    data = data[:-4] + "AAAA"
    with open(path, "w") as f:
        f.write(data)
    with pytest.raises(ValueError):
        crypto.load_identity(path, "pw")


def test_curve_keys_match():
    identity = crypto.new_identity()
    curve_pk, curve_sk = crypto.curve_keys(identity)
    assert len(curve_pk) == 32 and len(curve_sk) == 32
