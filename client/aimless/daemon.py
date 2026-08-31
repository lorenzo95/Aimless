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
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(socket_path)
        self.sockfile = self.sock.makefile("r")
        self.events: "queue.Queue[dict]" = queue.Queue()
        self._raw: "queue.Queue[dict]" = queue.Queue()
        self._stash: list = []
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                line = self.sockfile.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
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
            self.sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
            end = time.monotonic() + timeout
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise DaemonError(f"timeout waiting for response to {op}")
                try:
                    msg = self._raw.get(timeout=remaining)
                except queue.Empty:
                    raise DaemonError(f"timeout waiting for response to {op}")
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
        try:
            self.sock.close()
        except OSError:
            pass


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
        payload = protocol.seal_message(self.identity, buddy_client_hex, text, ts)
        return self.daemon.request("send", to=buddy_node_hex, payload=payload)

    def set_status(self, buddy_client_hex: str, buddy_node_hex: str, away) -> dict:
        from . import protocol
        payload = protocol.seal_status(self.identity, buddy_client_hex, self.screen_name, away, ts=0)
        return self.daemon.request("setstatus", to=buddy_node_hex, payload=payload)

    def history(self, buddy_node_hex: str, after_seq: int) -> list:
        return self.daemon.request("history", **{"from": buddy_node_hex, "seq": after_seq})

    def presence(self) -> list:
        return self.daemon.request("presence").get("presence", [])

    def decrypt_recv(self, event: dict) -> dict:
        from . import protocol
        return protocol.open_message(self.identity, event["payload"])

    def decrypt_status(self, payload_b64: str) -> dict:
        from . import protocol
        return protocol.open_status(self.identity, payload_b64)
