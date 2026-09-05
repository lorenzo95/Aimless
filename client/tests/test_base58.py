import os

import pytest

from aimless import base58


def test_base58_roundtrip():
    for data in [os.urandom(32), b"\x00" * 32, b"\x00" + os.urandom(31),
                 os.urandom(31) + b"\x00", b"", b"\x00", os.urandom(64)]:
        assert base58.b58decode(base58.b58encode(data)) == data, f"round-trip failed for {len(data)} bytes"


def test_base58_alphabet():
    s = base58.b58encode(b"\x01\x02\x03")
    assert s and all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in s)


def test_base58_rejects_bad_characters():
    with pytest.raises(ValueError):
        base58.b58decode("0OIl10")  # 0, O, I, l are not in the alphabet