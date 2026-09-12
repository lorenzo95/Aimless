"""Key management: Yggdrasil address derivation + encrypted backups.

Pure helpers so the risky parts (address derivation, key parsing, bundle
encryption) are testable without a GUI or daemon.

The address derivation mirrors yggdrasil-go's ``address.AddrForKey`` exactly
(``src/address/address.go``): invert the public key, count the leading 1s, drop
the leading 1s and the first 0, then pack the remaining bits after the 0x02
prefix. Parity is checked against yggdrasil-go's own test vector in the tests.
"""

import base64
import ipaddress
import json
import os
import re

import nacl.secret
import nacl.signing
import nacl.utils

from . import crypto

BUNDLE_VERSION = 1


def yggdrasil_address(pubkey: bytes) -> str:
    """Canonical Yggdrasil IPv6 address string for an ed25519 public key."""
    if len(pubkey) != 32:
        raise ValueError("public key must be 32 bytes")
    buf = bytes(b ^ 0xFF for b in pubkey)
    addr = bytearray(16)
    temp = []
    done = False
    ones = 0
    bits = 0
    nbits = 0
    for idx in range(8 * len(buf)):
        bit = (buf[idx // 8] & (0x80 >> (idx % 8))) >> (7 - (idx % 8))
        if not done and bit != 0:
            ones = (ones + 1) & 0xFF
            continue
        if not done and bit == 0:
            done = True
            continue
        bits = ((bits << 1) | bit) & 0xFF
        nbits += 1
        if nbits == 8:
            nbits = 0
            temp.append(bits)
    addr[0] = 0x02
    addr[1] = ones
    for i, b in enumerate(temp):
        if 2 + i < 16:
            addr[2 + i] = b
    return ipaddress.IPv6Address(bytes(addr)).compressed


def parse_node_key(text: str):
    """Parse a Yggdrasil node key from hex.

    Accepts a 64-hex seed (the daemon's ``node.key`` format) or a 128-hex full
    ed25519 private key (the first 32 bytes are the seed). Returns
    ``(seed_bytes, pubkey_bytes)``.
    """
    raw = re.sub(r"\s+", "", (text or "")).lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if not raw or not re.fullmatch(r"[0-9a-f]+", raw):
        raise ValueError("key must be hex")
    try:
        data = bytes.fromhex(raw)
    except ValueError as e:
        raise ValueError(f"bad hex: {e}") from e
    if len(data) == 64:
        seed = data[:32]
    elif len(data) == 32:
        seed = data
    else:
        raise ValueError("expected a 64- or 128-hex-character key")
    priv = nacl.signing.SigningKey(seed)
    return seed, bytes(priv.verify_key)


def signing_key(seed: bytes):
    return nacl.signing.SigningKey(seed)


def random_node_key():
    """(seed_hex, address) for a fresh node key."""
    import nacl.signing as _s
    priv = _s.SigningKey.generate()
    seed = bytes(priv)
    return seed.hex(), yggdrasil_address(bytes(priv.verify_key))


def _atomic_write(path, data: bytes, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_node_key(datadir: str, seed: bytes) -> str:
    """Write ``<datadir>/node.key`` (hex seed, 0600, atomic). Returns the path."""
    os.makedirs(datadir, exist_ok=True)
    path = os.path.join(datadir, "node.key")
    _atomic_write(path, seed.hex().encode() + b"\n")
    return path


def remove_node_key(datadir: str) -> None:
    try:
        os.remove(os.path.join(datadir, "node.key"))
    except FileNotFoundError:
        pass


def save_bundle(path: str, passphrase: str, data: dict) -> None:
    """Write an encrypted backup bundle (scrypt + SecretBox)."""
    salt = nacl.utils.random(16)
    key = crypto.kdf_key(passphrase, salt)
    box = nacl.secret.SecretBox(key)
    ct = box.encrypt(json.dumps(data).encode("utf-8"))
    blob = {
        "v": BUNDLE_VERSION,
        "kdf": "scrypt",
        "salt": base64.b64encode(salt).decode(),
        "ct": base64.b64encode(ct).decode(),
    }
    _atomic_write(path, json.dumps(blob, indent=2).encode("utf-8"))


def load_bundle(path: str, passphrase: str) -> dict:
    """Decrypt a backup bundle. Raises ValueError on a wrong passphrase/corrupt."""
    with open(path) as f:
        blob = json.load(f)
    if blob.get("v") != BUNDLE_VERSION:
        raise ValueError("unsupported backup version")
    salt = base64.b64decode(blob["salt"])
    ct = base64.b64decode(blob["ct"])
    box = nacl.secret.SecretBox(crypto.kdf_key(passphrase, salt))
    try:
        data = box.decrypt(ct)
    except Exception as e:
        raise ValueError("wrong passphrase or corrupted backup") from e
    return json.loads(data)
