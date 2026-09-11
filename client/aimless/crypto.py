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
