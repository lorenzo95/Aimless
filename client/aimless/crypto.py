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
        self._data = {"conversations": {}, "muted": [], "pending": []}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            blob = f.read()
        nonce = blob[: nacl.secret.SecretBox.NONCE_SIZE]
        ct = blob[nacl.secret.SecretBox.NONCE_SIZE :]
        data = json.loads(self._box.decrypt(ct, nonce))
        if "buddies" in data and "conversations" not in data:
            data = self._migrate(data)
            self._data = data
            self._flush()
        else:
            for c in data.get("conversations", {}).values():
                c.setdefault("scan_last", dict(c.get("recv_last", {})))
            self._data = data

    @staticmethod
    def _migrate(old: dict) -> dict:
        convs = {}
        for key, b in old.get("buddies", {}).items():
            convs[key] = {
                "dm": True,
                "members": {},
                "msgs": [
                    {"dir": m["dir"], "seqs": {key: m["seq"]}, "ts": m["ts"], "text": m["text"],
                     "sender": key if m["dir"] == "in" else "self"}
                    for m in b["msgs"]
                ],
                "recv_last": {key: b["recv_last"]},
                "sent_last": {key: b["sent_last"]},
                "scan_last": {key: b["recv_last"]},
            }
        return {"conversations": convs, "muted": [], "pending": []}

    def _flush(self) -> None:
        blob = self._box.encrypt(json.dumps(self._data).encode("utf-8"))
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, self.path)

    def conversation(self, conv_id: str) -> dict:
        return self._data["conversations"].setdefault(
            conv_id, {"dm": True, "members": {}, "msgs": [], "recv_last": {}, "sent_last": {},
                      "scan_last": {}})

    def ensure_room(self, conv_id: str, members: dict) -> dict:
        c = self.conversation(conv_id)
        c["dm"] = False
        c["members"] = members
        c.pop("deleted", None)
        self._flush()
        return c

    def rooms(self) -> list:
        return [cid for cid, c in self._data["conversations"].items()
                if not c.get("dm") and not c.get("deleted")]

    def delete_room(self, conv_id: str, scan_points: dict) -> dict:
        """Tombstone: the record survives (empty, hidden) with the scan cursor
        advanced past the backlog, so a resurrecting room starts after it."""
        c = self.conversation(conv_id)
        c["dm"] = False
        c["deleted"] = True
        c["msgs"] = []
        c["recv_last"] = {}
        scan = c.setdefault("scan_last", {})
        scan.update(scan_points)
        self._flush()
        return c

    def is_conversation_muted(self, conv_id: str) -> bool:
        c = self._data["conversations"].get(conv_id)
        return bool(c and c.get("muted"))

    def mute_conversation(self, conv_id: str) -> None:
        c = self.conversation(conv_id)
        if not c.get("muted"):
            c["muted"] = True
            self._flush()

    def unmute_conversation(self, conv_id: str) -> None:
        c = self._data["conversations"].get(conv_id)
        if c and c.get("muted"):
            c["muted"] = False
            self._flush()

    def members(self, conv_id: str) -> dict:
        return dict(self.conversation(conv_id)["members"])

    def add_recv(self, conv_id: str, sender_node: str, seq: int, ts: int, text: str) -> bool:
        c = self.conversation(conv_id)
        if any(m["dir"] == "in" and m["seqs"].get(sender_node) == seq for m in c["msgs"]):
            return False
        c["msgs"].append({"dir": "in", "seqs": {sender_node: seq}, "ts": ts, "text": text,
                          "sender": sender_node})
        if seq > c["recv_last"].get(sender_node, 0):
            c["recv_last"][sender_node] = seq
        self._flush()
        return True

    def add_sent(self, conv_id: str, seqs: dict, ts: int, text: str) -> bool:
        c = self.conversation(conv_id)
        for m in c["msgs"]:
            if m["dir"] == "out" and all(m["seqs"].get(n) == s for n, s in seqs.items()):
                return False
        c["msgs"].append({"dir": "out", "seqs": dict(seqs), "ts": ts, "text": text, "sender": "self"})
        for n, s in seqs.items():
            if s > c["sent_last"].get(n, 0):
                c["sent_last"][n] = s
        self._flush()
        return True

    def msgs(self, conv_id: str) -> list:
        return list(self.conversation(conv_id)["msgs"])

    def recv_last(self, conv_id: str, node: str) -> int:
        return self.conversation(conv_id)["recv_last"].get(node, 0)

    def scan_last(self, conv_id: str, node: str) -> int:
        """Fetch cursor: highest journal seq already examined for this conversation
        from this member, regardless of which conversation the messages belonged to."""
        return self.conversation(conv_id).setdefault("scan_last", {}).get(node, 0)

    def set_scan_last(self, conv_id: str, node: str, seq: int) -> None:
        c = self.conversation(conv_id)
        scan = c.setdefault("scan_last", {})
        if seq > scan.get(node, 0):
            scan[node] = seq
            self._flush()

    def clear_history(self, conv_id: str) -> None:
        c = self.conversation(conv_id)
        c["msgs"] = []
        c["recv_last"] = {}
        c["scan_last"] = {}
        self._flush()

    def remove_conversation(self, conv_id: str) -> None:
        self._data["conversations"].pop(conv_id, None)
        self._flush()

    def is_muted(self, node: str) -> bool:
        return node in self._data["muted"]

    def mute(self, node: str) -> None:
        if node not in self._data["muted"]:
            self._data["muted"].append(node)
            self._flush()

    def unmute(self, node: str) -> None:
        if node in self._data["muted"]:
            self._data["muted"].remove(node)
            self._flush()

    def add_pending(self, req: dict) -> None:
        if not any(p.get("node") == req.get("node") for p in self._data["pending"]):
            self._data["pending"].append(req)
            self._flush()

    def pending(self) -> list:
        return list(self._data["pending"])

    def pending_pop(self) -> dict:
        if not self._data["pending"]:
            return None
        req = self._data["pending"].pop(0)
        self._flush()
        return req
