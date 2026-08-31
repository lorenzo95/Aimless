import base64
import hashlib
import json
import os

import nacl.bindings
import nacl.exceptions
import nacl.secret
import nacl.signing
import nacl.utils

KDF_N = 2**15
KDF_R = 8
KDF_P = 1
KDF_MAXMEM = 2**26


def new_identity() -> nacl.signing.SigningKey:
    return nacl.signing.SigningKey.generate()


def curve_keys(identity: nacl.signing.SigningKey):
    seed = bytes(identity)
    sign_pk = bytes(identity.verify_key)
    curve_pk = nacl.bindings.crypto_sign_ed25519_pk_to_curve25519(sign_pk)
    curve_sk = nacl.bindings.crypto_sign_ed25519_sk_to_curve25519(seed + sign_pk)
    return curve_pk, curve_sk


def kdf_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=KDF_N, r=KDF_R, p=KDF_P, dklen=32,
        maxmem=KDF_MAXMEM,
    )


def save_identity(path: str, identity: nacl.signing.SigningKey, passphrase: str) -> None:
    salt = nacl.utils.random(16)
    key = kdf_key(passphrase, salt)
    box = nacl.secret.SecretBox(key)
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
    ct = box.encrypt(bytes(identity), nonce)
    data = {
        "v": 1,
        "kdf": "scrypt",
        "n": KDF_N,
        "r": KDF_R,
        "p": KDF_P,
        "salt": base64.b64encode(salt).decode(),
        "ct": base64.b64encode(ct).decode(),
    }
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_identity(path: str, passphrase: str) -> nacl.signing.SigningKey:
    with open(path) as f:
        data = json.load(f)
    salt = base64.b64decode(data["salt"])
    ct = base64.b64decode(data["ct"])
    key = kdf_key(passphrase, salt)
    box = nacl.secret.SecretBox(key)
    try:
        seed = box.decrypt(ct)
    except nacl.exceptions.CryptoError as e:
        raise ValueError("wrong passphrase or corrupted identity file") from e
    if len(seed) != 32:
        raise ValueError("corrupted identity file")
    return nacl.signing.SigningKey(seed)


class Cache:
    def __init__(self, path: str, passphrase: str):
        self.path = path
        salt = hashlib.sha256(b"aimless-cache" + kdf_key(passphrase, b"aimless-cache-salt")).digest()[:16]
        self._key = kdf_key(passphrase, salt)
        self._box = nacl.secret.SecretBox(self._key)
        self._data = {"buddies": {}}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            blob = f.read()
        nonce = blob[: nacl.secret.SecretBox.NONCE_SIZE]
        ct = blob[nacl.secret.SecretBox.NONCE_SIZE :]
        self._data = json.loads(self._box.decrypt(ct, nonce))

    def _flush(self) -> None:
        blob = self._box.encrypt(json.dumps(self._data).encode("utf-8"))
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, self.path)

    def buddy(self, pubkey_hex: str) -> dict:
        return self._data["buddies"].setdefault(pubkey_hex, {"msgs": [], "recv_last": 0, "sent_last": 0})

    def add_recv(self, pubkey_hex: str, seq: int, ts: int, text: str) -> bool:
        b = self.buddy(pubkey_hex)
        if any(m["dir"] == "in" and m["seq"] == seq for m in b["msgs"]):
            return False
        b["msgs"].append({"dir": "in", "seq": seq, "ts": ts, "text": text})
        if seq > b["recv_last"]:
            b["recv_last"] = seq
        self._flush()
        return True

    def add_sent(self, pubkey_hex: str, seq: int, ts: int, text: str) -> bool:
        b = self.buddy(pubkey_hex)
        if any(m["dir"] == "out" and m["seq"] == seq for m in b["msgs"]):
            return False
        b["msgs"].append({"dir": "out", "seq": seq, "ts": ts, "text": text})
        if seq > b["sent_last"]:
            b["sent_last"] = seq
        self._flush()
        return True

    def msgs(self, pubkey_hex: str) -> list:
        return list(self.buddy(pubkey_hex)["msgs"])

    def recv_last(self, pubkey_hex: str) -> int:
        return self.buddy(pubkey_hex)["recv_last"]
