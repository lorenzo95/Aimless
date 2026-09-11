import base64
import hashlib
import os

import pytest

from aimless import crypto, protocol
from aimless.store import Store


def _split(data, size=None):
    size = size or protocol.FILE_CHUNK_SIZE
    return [data[i:i + size] for i in range(0, len(data), size)]


def test_file_chunk_roundtrip_out_of_order():
    alice, bob = crypto.new_identity(), crypto.new_identity()
    data = os.urandom(100_000)
    pieces = _split(data)
    total = len(pieces)
    tid = protocol.new_transfer_id()
    sha = hashlib.sha256(data).hexdigest()

    wire = []
    for i, piece in enumerate(pieces):
        chunk = protocol.make_chunk(tid, i, total, "photo.jpg", "image", sha, len(data), piece)
        sealed = protocol.seal_file_chunk(alice, bytes(bob.verify_key).hex(), chunk)
        wire.append(base64.b64encode(
            protocol.file_header(bytes.fromhex(tid), i, total) + sealed).decode())

    # feed back out of order
    received = {}
    for idx in reversed(range(total)):
        parsed = protocol.parse_file_payload(wire[idx])
        opened = protocol.open_file_chunk(bob, parsed["sealed"])
        assert opened["transfer_id"] == tid
        assert parsed["tid"].hex() == tid, "header tid must match the sealed body"
        assert parsed["index"] == opened["index"] == idx
        assert parsed["total"] == opened["total"] == total
        received[opened["index"]] = base64.b64decode(opened["data"])

    rebuilt = protocol.reassemble_file(received, total)
    assert rebuilt == data, "out-of-order reassembly must reconstruct the file"
    assert hashlib.sha256(rebuilt).hexdigest() == sha, "sha256 must verify"


def test_file_chunk_wrong_recipient_rejected():
    alice, bob, eve = crypto.new_identity(), crypto.new_identity(), crypto.new_identity()
    tid = protocol.new_transfer_id()
    chunk = protocol.make_chunk(tid, 0, 1, "f.bin", "application", "ab" * 32, 1, b"x")
    sealed = protocol.seal_file_chunk(alice, bytes(bob.verify_key).hex(), chunk)
    with pytest.raises(Exception):
        protocol.open_file_chunk(eve, sealed)


def test_file_reassembly_never_false_completes():
    assert protocol.reassemble_file({0: b"a", 2: b"c"}, 3) is None, "missing chunk"
    assert protocol.reassemble_file({0: b"a", 1: b"b"}, 3) is None, "incomplete"
    assert protocol.reassemble_file({0: b"a"}, 1) == b"a", "single chunk"
    data = b"hello world"
    assert hashlib.sha256(b"tampered").hexdigest() != hashlib.sha256(data).hexdigest()


def test_file_payload_bad_header():
    tid = protocol.new_transfer_id()
    with pytest.raises(ValueError):
        protocol.file_header(bytes.fromhex(tid), 5, 3)  # index >= total
    with pytest.raises(ValueError):
        protocol.parse_file_payload(base64.b64encode(b"short").decode())


def test_file_size_cap():
    protocol.validate_send_size(protocol.MAX_SEND_BYTES)
    with pytest.raises(ValueError):
        protocol.validate_send_size(protocol.MAX_SEND_BYTES + 1)


def test_cache_attachment_shape(tmp_path):
    path = str(tmp_path / "cache.json.enc")
    c = Store(path, "pw")
    att = {"path": "/x/attachments/1-a.jpg", "filename": "a.jpg", "mime_hint": "image", "size": 10}
    assert c.add_recv("conv", "n1", 7, 100, "a.jpg", attachment=att) is True
    assert c.add_recv("conv", "n1", 7, 100, "a.jpg", attachment=att) is False, "dedup by seq"
    c2 = Store(path, "pw")
    msgs = c2.msgs("conv")
    assert len(msgs) == 1
    assert msgs[0]["attachment"] == att
    assert msgs[0]["text"] == "a.jpg"