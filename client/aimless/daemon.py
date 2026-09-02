import json
import queue
import socket
import threading
import time


class DaemonError(Exception):
    pass


EVENT_OPS = ("recv", "acked")

RESPONSE_MAP = {
    "send": "queued",
    "watch": "watching",
    "setstatus": "statusset",
}


class DaemonClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.events: "queue.Queue[dict]" = queue.Queue()
        self._raw: "queue.Queue[dict]" = queue.Queue()
        self._stash: list = []
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._want_reconnect = threading.Event()
        self._sock = None
        self._sockfile = None
        self._generation = 0
        self._connect()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _connect(self):
        self._sock = socket.socket(socket.AF_UNIX)
        self._sock.connect(self.socket_path)
        self._sockfile = self._sock.makefile("r")
        self._generation += 1

    def _close_socket(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._sockfile = None

    def _reader_reconnect(self):
        self._close_socket()
        while not self._stop.is_set():
            try:
                self._connect()
                self._want_reconnect.clear()
                return True
            except OSError:
                if self._stop.wait(0.5):
                    return False
        return False

    def _wait_generation_change(self, old_gen: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if self._generation != old_gen:
                return True
            time.sleep(0.05)
        return False

    def _read_loop(self):
        while not self._stop.is_set():
            if self._want_reconnect.is_set() or self._sock is None:
                if not self._reader_reconnect():
                    break
                continue
            try:
                line = self._sockfile.readline()
            except (OSError, ValueError, AttributeError):
                line = ""
            if not line:
                self._want_reconnect.set()
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("op") in EVENT_OPS:
                self.events.put(msg)
            else:
                self._raw.put(msg)

    def request(self, op: str, timeout: float = 10.0, **fields) -> dict:
        req = {"op": op}
        req.update(fields)
        expected = RESPONSE_MAP.get(op, op)
        with self._send_lock:
            deadline = time.monotonic() + timeout
            sent_gen = self._generation
            sent = False
            while True:
                if self._sock is None or self._want_reconnect.is_set():
                    if not self._wait_generation_change(sent_gen, 10.0):
                        raise DaemonError(f"daemon unreachable ({op})")
                    deadline = time.monotonic() + timeout
                    sent = False
                if not sent:
                    try:
                        self._sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
                        sent = True
                        sent_gen = self._generation
                    except (OSError, AttributeError):
                        self._want_reconnect.set()
                        if not self._wait_generation_change(sent_gen, 10.0):
                            raise DaemonError(f"daemon unreachable ({op})")
                        deadline = time.monotonic() + timeout
                        sent = False
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DaemonError(f"timeout waiting for response to {op}")
                try:
                    msg = self._raw.get(timeout=min(0.5, remaining))
                except queue.Empty:
                    continue
                if msg.get("op") in EVENT_OPS:
                    self.events.put(msg)
                    continue
                if msg.get("op") != expected:
                    self._stash.append(msg)
                    continue
                if msg.get("op") == "error":
                    raise DaemonError(msg.get("error", "unknown error"))
                return msg

    def next_event(self, timeout: float = None):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._stop.set()
        self._close_socket()


class Client:
    def __init__(self, daemon: DaemonClient, identity, screen_name: str):
        self.daemon = daemon
        self.identity = identity
        self.pubkey_hex = bytes(identity.verify_key).hex()
        self.screen_name = screen_name

    def whoami(self) -> dict:
        return self.daemon.request("whoami")

    def node_key(self) -> str:
        return self.whoami()["key"]

    def add_contact(self, buddy_node_hex: str) -> dict:
        return self.daemon.request("watch", to=buddy_node_hex)

    def send(self, buddy_client_hex: str, buddy_node_hex: str, text: str, ts: int) -> dict:
        from . import protocol
        payload = protocol.seal_message(self.identity, buddy_client_hex, text, ts,
                                        screen=self.screen_name)
        return self.daemon.request("send", to=buddy_node_hex, payload=payload)

    def send_room(self, members: list, conv: str, text: str, ts: int) -> dict:
        """members: full member set [{node, pubkey, screen}] including self; one sealed
        copy is sent to every member except self. Returns {node: seq} per stream."""
        from . import protocol
        triplets = [{"node": m["node"], "pubkey": m["pubkey"], "screen": m.get("screen", "")}
                    for m in members]
        my_node = self.node_key()
        seqs = {}
        for m in triplets:
            if m["node"] == my_node:
                continue
            payload = protocol.seal_message(self.identity, m["pubkey"], text, ts,
                                            screen=self.screen_name, conv=conv, members=triplets)
            resp = self.daemon.request("send", to=m["node"], payload=payload)
            seqs[m["node"]] = resp.get("seq", 0)
        return seqs

    def set_status(self, buddy_client_hex: str, buddy_node_hex: str, away) -> dict:
        from . import protocol
        payload = protocol.seal_status(
            self.identity, buddy_client_hex, self.screen_name, away, ts=int(time.time() * 1000))
        return self.daemon.request("setstatus", to=buddy_node_hex, payload=payload)

    def history(self, buddy_node_hex: str, after_seq: int) -> list:
        return self.daemon.request("history", **{"from": buddy_node_hex, "seq": after_seq})

    def presence(self, timeout: float = 10.0) -> list:
        return self.daemon.request("presence", timeout=timeout).get("presence", [])

    def decrypt_recv(self, event: dict) -> dict:
        from . import protocol
        return protocol.open_message(self.identity, event["payload"])

    def decrypt_status(self, payload_b64: str) -> dict:
        from . import protocol
        return protocol.open_status(self.identity, payload_b64)
