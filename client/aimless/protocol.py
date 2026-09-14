import base64
import hashlib
import json
import os
import struct
import uuid

import nacl.exceptions
import nacl.public
import nacl.signing
import nacl.bindings

from . import base58
from . import crypto

INVITE_PREFIX = "aimless1:"
MAGIC = b"aimless\x01"

FILE_CHUNK_SIZE = 32 * 1024
MAX_SEND_BYTES = 20 * 1024 * 1024


def new_transfer_id() -> str:
    """16-byte transfer id as hex (also carried raw in the 20-byte wire header)."""
    return uuid.uuid4().hex


def file_header(tid_bytes: bytes, index: int, total: int) -> bytes:
    if len(tid_bytes) != 16:
        raise ValueError("transfer id must be 16 bytes")
    if not (0 <= index < total):
        raise ValueError("chunk index out of range")
    return tid_bytes + struct.pack("<HH", index, total)


def parse_file_payload(payload_b64: str) -> dict:
    """Split a received TypeFile payload into its 20-byte routing header (raw
    tid bytes + index/total) and the sealed chunk body. The client must
    cross-check the header against the decrypted body."""
    raw = base64.b64decode(payload_b64)
    if len(raw) < 20:
        raise ValueError("truncated file payload")
    tid = raw[:16]
    index, total = struct.unpack("<HH", raw[16:20])
    if total == 0 or index >= total:
        raise ValueError("bad file chunk index/total")
    return {"tid": tid, "index": index, "total": total, "sealed": raw[20:]}


def make_chunk(tid: str, index: int, total: int, filename: str, mime_hint: str,
               sha256: str, size: int, data: bytes, conv: str = None) -> dict:
    chunk = {"transfer_id": tid, "index": index, "total": total, "filename": filename,
             "mime_hint": mime_hint, "sha256": sha256, "size": size,
             "data": base64.b64encode(data).decode()}
    if conv:
        chunk["conv"] = conv
    return chunk


def build_file_payload(identity: nacl.signing.SigningKey, recipient_pubkey_hex: str, chunk: dict) -> str:
    """The full wire payload for one chunk: 20-byte routing header + sealed body,
    base64 for the daemon's sendfile op."""
    sealed = seal_file_chunk(identity, recipient_pubkey_hex, chunk)
    header = file_header(bytes.fromhex(chunk["transfer_id"]), chunk["index"], chunk["total"])
    return base64.b64encode(header + sealed).decode()


def seal_file_chunk(identity: nacl.signing.SigningKey, buddy_pubkey_hex: str, chunk: dict) -> bytes:
    """Seal one file chunk (kind:"file", same convention as text) into raw bytes.
    Callers prepend the 20-byte routing header before handing it to the daemon."""
    body = json.dumps(chunk, sort_keys=True).encode("utf-8")
    sig = _sign(identity, body)
    inner = json.dumps({
        "v": 1, "kind": "file", "from": bytes(identity.verify_key).hex(),
        "body": body.decode(), "sig": sig,
    }).encode("utf-8")
    curve_pk = nacl.bindings.crypto_sign_ed25519_pk_to_curve25519(bytes.fromhex(buddy_pubkey_hex))
    return nacl.public.SealedBox(nacl.public.PublicKey(curve_pk)).encrypt(inner)


def open_file_chunk(identity: nacl.signing.SigningKey, sealed: bytes) -> dict:
    _, curve_sk = crypto.curve_keys(identity)
    inner = nacl.public.SealedBox(nacl.public.PrivateKey(curve_sk)).decrypt(sealed)
    msg = json.loads(inner)
    if msg.get("kind") != "file" or "from" not in msg or "body" not in msg or "sig" not in msg:
        raise ValueError("malformed file chunk")
    chunk = json.loads(msg["body"])
    if not _verify(msg["from"], msg["body"].encode("utf-8"), msg["sig"]):
        raise ValueError("bad signature")
    chunk["from"] = msg["from"]
    return chunk


def reassemble_file(chunks: dict, total: int) -> bytes:
    """Concatenate a chunk map in index order; returns None if incomplete."""
    if len(chunks) != total or any(i not in chunks for i in range(total)):
        return None
    return b"".join(chunks[i] for i in range(total))


def validate_send_size(size: int) -> None:
    if size > MAX_SEND_BYTES:
        raise ValueError(f"file is {size:,} bytes - aimless caps attachments at {MAX_SEND_BYTES:,} bytes")


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
