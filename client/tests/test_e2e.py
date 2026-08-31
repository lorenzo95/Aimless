import os
import shutil
import socket
import subprocess
import time

import pytest

from aimless import crypto
from aimless.daemon import Client, DaemonClient

DAEMON_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "daemon"))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_socket(path, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.1)
    raise RuntimeError(f"socket {path} never appeared")


def _build_daemon(tmp):
    go = shutil.which("go") or ("/tmp/opencode/golang/bin/go" if os.path.exists("/tmp/opencode/golang/bin/go") else None)
    if go is None:
        pytest.skip("go toolchain not installed")
    binpath = os.path.join(tmp, "aimlessd")
    subprocess.run([go, "build", "-o", binpath, "."], cwd=DAEMON_DIR, check=True, capture_output=True)
    return binpath


@pytest.fixture
def two_nodes(tmp_path):
    binpath = _build_daemon(str(tmp_path))
    port = _free_port()
    dir_a, dir_b = str(tmp_path / "nodeA"), str(tmp_path / "nodeB")
    sock_a, sock_b = str(tmp_path / "a.sock"), str(tmp_path / "b.sock")
    procs = []
    procs.append(
        subprocess.Popen(
            [binpath, "-datadir", dir_a, "-api", sock_a, "-listen", f"tcp://127.0.0.1:{port}", "-peers", "none", "-retry", "300ms", "-probe", "300ms", "-verbose"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    )
    _wait_socket(sock_a)
    time.sleep(0.5)
    procs.append(
        subprocess.Popen(
            [binpath, "-datadir", dir_b, "-api", sock_b, "-peers", f"tcp://127.0.0.1:{port}", "-retry", "300ms", "-probe", "300ms", "-verbose"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    )
    _wait_socket(sock_b)
    time.sleep(0.5)
    yield sock_a, sock_b
    for p in procs:
        p.terminate()
    for p in procs:
        p.wait(timeout=10)


def _make_client(sock, tmp_path, name):
    identity = crypto.new_identity()
    daemon = DaemonClient(sock)
    return Client(daemon, identity, name), daemon, identity


def _wait_for(predicate, timeout=30.0, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


def _wait_recv(daemon, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = daemon.next_event(timeout=0.5)
        if ev is not None and ev.get("op") == "recv":
            return ev
    raise AssertionError("no recv event within timeout")


def test_end_to_end_exchange(two_nodes, tmp_path):
    sock_a, sock_b = two_nodes
    alice, daemon_a, id_a = _make_client(sock_a, tmp_path, "alice")
    bob, daemon_b, id_b = _make_client(sock_b, tmp_path, "bob")
    a_hex = alice.pubkey_hex
    b_hex = bob.pubkey_hex
    a_node = alice.node_key()
    b_node = bob.node_key()

    alice.add_contact(b_node)
    bob.add_contact(a_node)

    ts = int(time.time() * 1000)
    resp = alice.send(b_hex, b_node, "hello bob", ts)
    assert resp["op"] == "queued"
    assert resp["seq"] == 1

    event = _wait_recv(daemon_b)
    assert event["from"] == a_node
    opened = bob.decrypt_recv(event)
    assert opened["text"] == "hello bob"
    assert opened["from"] == a_hex

    ts2 = int(time.time() * 1000)
    bob.send(a_hex, a_node, "hi alice", ts2)
    event2 = _wait_recv(daemon_a)
    assert event2["from"] == b_node
    opened2 = alice.decrypt_recv(event2)
    assert opened2["text"] == "hi alice"

    hist = alice.history(b_node, 0)
    assert hist["op"] == "history"
    assert hist["latest"] == 1
    assert len(hist["msgs"]) == 1
    from aimless import protocol
    opened_hist = protocol.open_message(id_a, hist["msgs"][0]["payload"])
    assert opened_hist["text"] == "hi alice"


def test_end_to_end_presence_and_status(two_nodes):
    sock_a, sock_b = two_nodes
    alice, daemon_a, _ = _make_client(sock_a, None, "alice")
    bob, daemon_b, _ = _make_client(sock_b, None, "bob")
    a_hex, b_hex = alice.pubkey_hex, bob.pubkey_hex
    a_node, b_node = alice.node_key(), bob.node_key()

    alice.add_contact(b_node)
    bob.add_contact(a_node)

    _wait_for(lambda: any(p["key"] == b_node and p["online"] for p in alice.presence()))
    _wait_for(lambda: any(p["key"] == a_node and p["online"] for p in bob.presence()))

    alice.set_status(b_hex, b_node, "brb — lunch")
    _wait_for(lambda: any(
        (lambda p: p.get("status_payload") and _open_status(bob, p) == "brb — lunch")(p)
        for p in bob.presence() if p["key"] == a_node
    ))


def _open_status(client, p):
    from aimless import protocol
    return protocol.open_status(client.identity, p["status_payload"])["away"]
