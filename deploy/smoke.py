import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLIENT = os.path.join(ROOT, "client")
DAEMON = os.path.join(ROOT, "daemon")
sys.path.insert(0, CLIENT)

from aimless import crypto, protocol
from aimless.daemon import Client, DaemonClient


def protocol_open(client, payload_b64):
    return protocol.open_message(client.identity, payload_b64)

SOCK_A = os.path.join(HERE, "data-a", "api.sock")
SOCK_B = os.path.join(HERE, "data-b", "api.sock")


def sh(*args):
    return subprocess.run(args, cwd=HERE, check=True, capture_output=True, text=True)


def docker_available():
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_socket(path, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            time.sleep(1.0)
            return
        time.sleep(0.25)
    raise RuntimeError(f"socket {path} never appeared")


def wait_for(fn, what, timeout=60.0):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    raise AssertionError(f"timeout: {what} (last: {last_err})")


def recv_event(daemon, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = daemon.next_event(timeout=1.0)
        if ev and ev.get("op") == "recv":
            return ev
    raise AssertionError("no recv event")


def wait_b_ready(sock_path, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            d = DaemonClient(sock_path)
            d.request("whoami", timeout=5)
            d.close()
            return sock_path
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("nodeb daemon never became ready")


def run_scenario(sock_a, sock_b, stop_b, start_b, wait_b_sock):
    alice = Client(DaemonClient(sock_a), crypto.new_identity(), "alice")
    bob = Client(DaemonClient(sock_b), crypto.new_identity(), "bob")
    a_node, b_node = alice.node_key(), bob.node_key()
    alice.add_contact(b_node)
    bob.add_contact(a_node)

    ts = int(time.time() * 1000)
    r = alice.send(bob.pubkey_hex, b_node, "hello bob (online)", ts)
    assert r["op"] == "queued" and r["seq"] == 1, r
    ev = recv_event(bob.daemon)
    opened = bob.decrypt_recv(ev)
    assert opened["text"] == "hello bob (online)", opened
    print("online delivery: OK")

    print("stopping nodeb (simulating offline) …")
    stop_b()
    time.sleep(1)
    for text in ("while you were out #1", "while you were out #2"):
        r = alice.send(bob.pubkey_hex, b_node, text, int(time.time() * 1000))
        assert r["op"] == "queued", r
    print("2 messages queued while B down")

    print("starting nodeb again …")
    start_b()
    sock_b2 = wait_b_sock()
    bob2 = Client(DaemonClient(sock_b2), bob.identity, "bob")

    def replayed():
        hist = bob2.history(a_node, 1)
        return [m["seq"] for m in hist.get("msgs", [])] == [2, 3]
    wait_for(replayed, "offline replay of seqs 2,3")
    hist = bob2.history(a_node, 1)
    texts = [protocol_open(bob2, m["payload"])["text"] for m in hist["msgs"]]
    assert texts == ["while you were out #1", "while you were out #2"], texts
    print("offline delivery: OK —", texts)

    def full_history():
        hist = bob2.history(a_node, 0)
        return len(hist.get("msgs", [])) == 3
    wait_for(full_history, "history completeness")
    hist = bob2.history(a_node, 0)
    assert hist["oldest"] == 1 and hist["latest"] == 3, hist
    print(f"history replay: OK (seqs {hist['oldest']}..{hist['latest']})")

    def online():
        return any(p["key"] == a_node and p["online"] for p in bob2.presence())
    wait_for(online, "presence")
    print("presence: OK")

    print("\nALL SMOKE CHECKS PASSED")


def compose_mode():
    for d in ("data-a", "data-b"):
        p = os.path.join(HERE, d)
        if os.path.exists(p):
            shutil.rmtree(p)
        os.makedirs(p)
        os.chmod(p, 0o777)
    print("building + starting containers …")
    sh("docker", "compose", "up", "-d", "--build")
    try:
        wait_socket(SOCK_A)
        wait_socket(SOCK_B)
        print("both daemons up")

        def stop_b():
            sh("docker", "compose", "stop", "nodeb")

        def start_b():
            sh("docker", "compose", "start", "nodeb")

        def wait_b_sock():
            return wait_b_ready(SOCK_B)

        run_scenario(SOCK_A, SOCK_B, stop_b, start_b, wait_b_sock)
    finally:
        sh("docker", "compose", "down", "-v")
        print("compose torn down")


def host_mode():
    go = shutil.which("go") or ("/tmp/opencode/golang/bin/go" if os.path.exists("/tmp/opencode/golang/bin/go") else None)
    if go is None:
        print("host fallback needs a go toolchain", file=sys.stderr)
        sys.exit(2)
    tmp = tempfile.mkdtemp(prefix="aimless-smoke-", dir="/tmp/opencode")
    binpath = os.path.join(tmp, "aimlessd")
    subprocess.run([go, "build", "-o", binpath, "."], cwd=DAEMON, check=True, capture_output=True)
    port = free_port()
    dir_a, dir_b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
    os.makedirs(dir_a)
    os.makedirs(dir_b)
    sock_a, sock_b = os.path.join(dir_a, "api.sock"), os.path.join(dir_b, "api.sock")
    common = ["-retry", "500ms", "-probe", "500ms"]
    procs = []
    procs.append(subprocess.Popen(
        [binpath, "-datadir", dir_a, "-api", sock_a, "-peers", "none", "-listen", f"tcp://127.0.0.1:{port}"] + common,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    procs.append(subprocess.Popen(
        [binpath, "-datadir", dir_b, "-api", sock_b, "-peers", f"tcp://127.0.0.1:{port}"] + common,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        wait_socket(sock_a, timeout=30)
        wait_socket(sock_b, timeout=30)
        time.sleep(1.5)
        print("both host daemons up")

        def stop_b():
            procs[1].terminate()
            procs[1].wait(timeout=10)
            time.sleep(0.5)

        def start_b():
            procs[1] = subprocess.Popen(
                [binpath, "-datadir", dir_b, "-api", sock_b, "-peers", f"tcp://127.0.0.1:{port}"] + common,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def wait_b_sock():
            return wait_b_ready(sock_b)

        run_scenario(sock_a, sock_b, stop_b, start_b, wait_b_sock)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def main():
    print("== aimless smoke ==")
    if docker_available():
        compose_mode()
    else:
        print("docker unavailable — host-mode fallback (same scenario, local daemons)")
        host_mode()
    print("done")


if __name__ == "__main__":
    main()
