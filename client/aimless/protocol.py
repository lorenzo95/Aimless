import base64
import hashlib
import json
import os

import nacl.exceptions
import nacl.public
import nacl.signing
import nacl.bindings

from . import base58
from . import crypto

INVITE_PREFIX = "aimless1:"
MAGIC = b"aimless\x01"


def save_contacts(path: str, contacts: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(contacts, f, indent=2)
    os.replace(tmp, path)


def load_contacts(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def make_invite(identity: nacl.signing.SigningKey, node_pubkey_hex: str, screen_name: str) -> str:
    client_key = base58.b58encode(bytes(identity.verify_key))
    node_key = base58.b58encode(bytes.fromhex(node_pubkey_hex))
    return f"{INVITE_PREFIX}{client_key}:{node_key}:{screen_name}"


def _decode_key_field(field: str, label: str) -> bytes:
    if len(field) == 64 and all(c in "0123456789abcdefABCDEF" for c in field):
        key = bytes.fromhex(field)
        if len(key) != 32:
            raise ValueError(f"{label} must be 64 hex chars")
        return key
    try:
        key = base58.b58decode(field)
    except ValueError as e:
        raise ValueError(f"{label} is neither valid hex nor base58: {e}") from e
    if len(key) != 32:
        raise ValueError(f"{label} must decode to 32 bytes")
    return key


def parse_invite(invite: str):
    invite = invite.strip()
    if not invite.startswith(INVITE_PREFIX):
        raise ValueError("invite must start with " + INVITE_PREFIX)
    rest = invite[len(INVITE_PREFIX) :]
    parts = rest.split(":")
    if len(parts) != 3:
        raise ValueError("invite must be aimless1:<client-pk>:<node-pk>:<screen-name>")
    client_field, node_field, screen_name = parts
    client_bytes = _decode_key_field(client_field, "client key")
    node_bytes = _decode_key_field(node_field, "node key")
    if not screen_name:
        raise ValueError("missing screen name")
    return client_bytes.hex(), node_bytes.hex(), screen_name


def _sign(identity: nacl.signing.SigningKey, body: bytes) -> str:
    return base64.b64encode(identity.sign(MAGIC + body).signature).decode()


def _verify(from_hex: str, body: bytes, sig_b64: str) -> bool:
    try:
        verify_key = nacl.signing.VerifyKey(bytes.fromhex(from_hex))
        verify_key.verify(MAGIC + body, base64.b64decode(sig_b64))
        return True
    except (ValueError, nacl.exceptions.BadSignatureError, nacl.exceptions.ValueError):
        return False


def room_id(member_nodes) -> str:
    """Stable conversation id for a room: identical member sets always agree."""
    return hashlib.sha256("|".join(sorted(member_nodes)).encode("utf-8")).hexdigest()


def seal_message(
    identity: nacl.signing.SigningKey, buddy_pubkey_hex: str, text: str, ts: int,
    screen: str = None, conv: str = None, members=None,
) -> str:
    body_obj = {"text": text, "ts": ts}
    if screen:
        body_obj["screen"] = screen
    if conv is not None:
        body_obj["conv"] = conv
    if members is not None:
        body_obj["members"] = members
    body = json.dumps(body_obj, sort_keys=True).encode("utf-8")
    sig = _sign(identity, body)
    inner = json.dumps(
        {"v": 1, "kind": "msg", "from": bytes(identity.verify_key).hex(), "body": body.decode(), "sig": sig}
    ).encode("utf-8")
    buddy_key_bytes = bytes.fromhex(buddy_pubkey_hex)
    curve_pk = nacl.bindings.crypto_sign_ed25519_pk_to_curve25519(buddy_key_bytes)
    recipient = nacl.public.PublicKey(curve_pk)
    return base64.b64encode(nacl.public.SealedBox(recipient).encrypt(inner)).decode()


def open_message(
    identity: nacl.signing.SigningKey, payload_b64: str, ts: int = 0
) -> dict:
    _, curve_sk = crypto.curve_keys(identity)
    inner = nacl.public.SealedBox(nacl.public.PrivateKey(curve_sk)).decrypt(
        base64.b64decode(payload_b64)
    )
    msg = json.loads(inner)
    if msg.get("kind") != "msg" or "from" not in msg or "body" not in msg or "sig" not in msg:
        raise ValueError("malformed message")
    body_obj = json.loads(msg["body"])
    if not _verify(msg["from"], msg["body"].encode("utf-8"), msg["sig"]):
        raise ValueError("bad signature")
    out = {"from": msg["from"], "text": body_obj["text"], "ts": body_obj["ts"]}
    for field in ("screen", "conv", "members"):
        if field in body_obj:
            out[field] = body_obj[field]
    return out


def seal_status(
    identity: nacl.signing.SigningKey, buddy_pubkey_hex: str, screen: str, away, ts: int
) -> str:
    body = json.dumps({"screen": screen, "away": away, "ts": ts}, sort_keys=True).encode("utf-8")
    sig = _sign(identity, body)
    inner = json.dumps(
        {"v": 1, "kind": "status", "from": bytes(identity.verify_key).hex(), "body": body.decode(), "sig": sig}
    ).encode("utf-8")
    buddy_key_bytes = bytes.fromhex(buddy_pubkey_hex)
    curve_pk = nacl.bindings.crypto_sign_ed25519_pk_to_curve25519(buddy_key_bytes)
    recipient = nacl.public.PublicKey(curve_pk)
    return base64.b64encode(nacl.public.SealedBox(recipient).encrypt(inner)).decode()


def open_status(identity: nacl.signing.SigningKey, payload_b64: str) -> dict:
    _, curve_sk = crypto.curve_keys(identity)
    inner = nacl.public.SealedBox(nacl.public.PrivateKey(curve_sk)).decrypt(
        base64.b64decode(payload_b64)
    )
    st = json.loads(inner)
    if st.get("kind") != "status" or "from" not in st or "body" not in st or "sig" not in st:
        raise ValueError("malformed status")
    body_obj = json.loads(st["body"])
    if not _verify(st["from"], st["body"].encode("utf-8"), st["sig"]):
        raise ValueError("bad signature")
    return {"from": st["from"], "screen": body_obj["screen"], "away": body_obj["away"], "ts": body_obj["ts"]}
