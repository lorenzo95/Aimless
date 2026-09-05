import json
import socket
import threading

from aimless import crypto
from aimless.daemon import DaemonClient, Client


def _serve_multiplex(path):
    """Echoes ids. Replies to the probe immediately, then to two concurrent
    requests deliberately out of order (the second-read line first)."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)
    received = []

    def run():
        conn, _ = srv.accept()
        f = conn.makefile("r")
        probe = json.loads(f.readline())
        received.append(probe)
        conn.sendall((json.dumps({"op": "whoami", "id": probe["id"], "key": "ab" * 32}) + "\n").encode())
        r1 = json.loads(f.readline())
        received.append(r1)
        r2 = json.loads(f.readline())
        received.append(r2)
        conn.sendall((json.dumps({"op": "echo", "id": r2["id"], "tag": r2.get("tag")}) + "\n").encode())
        conn.sendall((json.dumps({"op": "echo", "id": r1["id"], "tag": r1.get("tag")}) + "\n").encode())
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    return srv, received


def _serve_legacy(path):
    """Pre-correlation daemon: ignores the id, replies with the same op, in order."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        f = conn.makefile("r")
        for _ in range(3):
            req = json.loads(f.readline())
            conn.sendall((json.dumps({"op": req["op"], "n": req.get("n")}) + "\n").encode())
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    return srv


def test_multiplexed_concurrent_requests_resolve_by_id(tmp_path):
    sock = str(tmp_path / "s.sock")
    srv, received = _serve_multiplex(sock)
    dc = DaemonClient(sock)
    try:
        assert dc.request("whoami", timeout=5)["key"] == "ab" * 32, "probe failed"
        results = {}

        def do(tag):
            results[tag] = dc.request("echo", timeout=5, tag=tag)["tag"]

        threads = [threading.Thread(target=do, args=(1,)), threading.Thread(target=do, args=(2,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == {1: 1, 2: 2}, f"correlation broken: {results}"
        ids = [r["id"] for r in received[1:]]
        assert len(set(ids)) == 2, f"expected 2 distinct request ids, got {ids}"
    finally:
        dc.close()


def test_legacy_daemon_falls_back_to_serialized(tmp_path):
    sock = str(tmp_path / "s.sock")
    srv = _serve_legacy(sock)
    dc = DaemonClient(sock)
    try:
        assert dc.request("ping", timeout=5, n=1)["n"] == 1
        assert dc.request("ping", timeout=5, n=2)["n"] == 2
        assert dc.request("ping", timeout=5, n=3)["n"] == 3
        assert dc._multiplex is False, "must stay serialized against a legacy daemon"
    finally:
        dc.close()


def test_client_node_key_cached():
    calls = []

    class _DC:
        def request(self, op, **kw):
            calls.append(op)
            return {"op": "whoami", "key": "ab" * 32}

    c = Client(_DC(), crypto.new_identity(), "T")
    assert c.node_key() == "ab" * 32
    assert c.node_key() == "ab" * 32
    assert calls == ["whoami"], f"whoami called {len(calls)} times"


def test_send_room_calls_whoami_once():
    calls = []

    class _DC:
        def request(self, op, **kw):
            calls.append(op)
            if op == "whoami":
                return {"op": "whoami", "key": "aa" * 32}
            return {"op": "queued", "seq": 1, "to": kw.get("to")}

    c = Client(_DC(), crypto.new_identity(), "T")
    pk1 = bytes(crypto.new_identity().verify_key).hex()
    pk2 = bytes(crypto.new_identity().verify_key).hex()
    members = [{"node": "bb" * 32, "pubkey": pk1, "screen": "X"},
               {"node": "dd" * 32, "pubkey": pk2, "screen": "Y"}]
    c.send_room(members, "conv", "hi", 1)
    c.send_room(members, "conv", "yo", 2)
    assert calls.count("whoami") == 1, f"whoami called {calls.count('whoami')} times"