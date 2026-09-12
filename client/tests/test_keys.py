import os
import stat

import pytest

from aimless import keys


# yggdrasil-go src/address/address_test.go TestAddress_AddrForKey
PUB = bytes([189, 186, 207, 216, 34, 64, 222, 61, 205, 18, 57, 36, 203, 181, 82, 86,
             251, 141, 171, 8, 170, 152, 227, 5, 82, 138, 184, 79, 65, 158, 110, 251])
EXPECTED_ADDR = "200:848a:604f:bb7e:4384:65db:8db6:6895"


def test_address_derivation_matches_yggdrasil_go():
    assert keys.yggdrasil_address(PUB) == EXPECTED_ADDR


def test_parse_node_key_seed_and_private():
    import nacl.signing
    seed = bytes(range(32))
    priv = nacl.signing.SigningKey(seed)
    pub = bytes(priv.verify_key)

    s1, p1 = keys.parse_node_key(seed.hex())                      # 64 hex (seed)
    assert s1 == seed and p1 == pub
    s2, p2 = keys.parse_node_key(bytes(priv).hex())               # 128 hex (full key)
    assert s2 == seed and p2 == pub
    s3, p3 = keys.parse_node_key("0x" + seed.hex().upper())       # 0x + uppercase
    assert s3 == seed and p3 == pub


def test_parse_node_key_rejects_bad_input():
    for bad in ("", "xyz", "ab" * 10, "ab" * 100):
        with pytest.raises(ValueError):
            keys.parse_node_key(bad)


def test_random_node_key_round_trips():
    seed_hex, addr = keys.random_node_key()
    seed, pub = keys.parse_node_key(seed_hex)
    assert keys.yggdrasil_address(pub) == addr
    assert addr.startswith("2")


def test_write_node_key_atomic_and_0600(tmp_path):
    path = keys.write_node_key(str(tmp_path), bytes(range(32)))
    assert os.path.basename(path) == "node.key"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    with open(path) as f:
        assert bytes.fromhex(f.read().strip()) == bytes(range(32))
    keys.remove_node_key(str(tmp_path))
    assert not os.path.exists(path)


def test_bundle_round_trip_and_failures(tmp_path):
    path = str(tmp_path / "backup.json")
    payload = {"identitySeed": "aa" * 32, "nodeSeed": "bb" * 32, "screen": "Alice",
               "contacts": {"bob": {"node": "cc" * 32}}}
    keys.save_bundle(path, "correct horse", payload)
    assert keys.load_bundle(path, "correct horse") == payload

    with pytest.raises(ValueError):
        keys.load_bundle(path, "wrong")
    # tampering breaks the SecretBox auth tag
    with open(path, "r+") as f:
        data = f.read()
        f.seek(0)
        f.write(data.replace('"ct": "', '"ct": "A', 1))
    with pytest.raises(ValueError):
        keys.load_bundle(path, "correct horse")
    with pytest.raises(ValueError):
        keys.load_bundle(path, "correct horse")


def test_bundle_rejects_bad_version(tmp_path):
    import json
    path = str(tmp_path / "b.json")
    keys.save_bundle(path, "pw", {"x": 1})
    blob = json.load(open(path))
    blob["v"] = 999
    json.dump(blob, open(path, "w"))
    with pytest.raises(ValueError):
        keys.load_bundle(path, "pw")
