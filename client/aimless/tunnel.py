"""App-managed SSH tunnel to an always-on remote aimlessd.

External mode is enabled by a ``remote`` block in the client prefs
(``client/prefs.json``)::

    "remote": {
      "host": "user@always-on-host",
      "socket": "/host/path/aimless-data/daemon/api.sock",
      "local_socket": "/run/user/1000/aimless/remote.sock"
    }

The client then talks to a local Unix socket forwarded to the remote daemon's
API socket over SSH (streamlocal). The socket's 0600 permissions are the only
auth; no TCP and no extra auth layer.

Pure enough to unit-test without GTK or a daemon.
"""

import json
import os
import shutil
import signal
import subprocess
import time

from . import paths
from .daemon import DaemonClient

SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "StreamLocalBindUnlink=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "TCPKeepAlive=yes",
)

BACKOFF = (2, 4, 8, 15, 30)


def default_local_socket() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "aimless", "remote.sock")
    return os.path.join(paths.run_dir(), "remote.sock")


def remote_config():
    """The ``remote`` block from client/prefs.json, or None when unset/invalid."""
    try:
        with open(paths.prefs_path()) as f:
            prefs = json.load(f)
    except Exception:
        return None
    remote = prefs.get("remote")
    if not isinstance(remote, dict) or not remote.get("host") or not remote.get("socket"):
        return None
    return {
        "host": str(remote["host"]),
        "socket": str(remote["socket"]),
        "local_socket": str(remote.get("local_socket") or default_local_socket()),
    }


def local_socket():
    remote = remote_config()
    return remote["local_socket"] if remote else None


def client_socket() -> str:
    """Where the client should connect: env override > remote tunnel > local daemon."""
    if os.environ.get("AIMLESS_SOCK"):
        return os.environ["AIMLESS_SOCK"]
    remote = remote_config()
    if remote:
        return remote["local_socket"]
    return paths.sock_path()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmdline(pid: int):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().decode(errors="replace").split("\x00")
    except OSError:
        return []


def _is_our_ssh(pid: int, local_socket_path: str) -> bool:
    parts = _cmdline(pid)
    if not parts or os.path.basename(parts[0]) != "ssh":
        return False
    return any(local_socket_path in p for p in parts)


class TunnelSupervisor:
    """Spawn/supervise a single ``ssh -L`` forwarding a local socket to the
    remote daemon's API socket."""

    def __init__(self, remote):
        self.host = remote["host"]
        self.remote_socket = remote["socket"]
        self.local_socket = remote["local_socket"]
        self.child = None
        self.restarts = 0
        self.last_error = ""
        self.last_ok = 0.0
        self._backoff_index = 0

    # -- command ----------------------------------------------------------
    def ssh_argv(self):
        return ["ssh", "-N", *SSH_OPTS,
                "-L", f"{self.local_socket}:{self.remote_socket}", self.host]

    def _ssh_bin(self):
        return shutil.which("ssh") or "/usr/bin/ssh"

    # -- pid file ---------------------------------------------------------
    def _pid_from_file(self):
        try:
            with open(paths.tunnel_pid_path()) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _write_pid(self, pid):
        try:
            os.makedirs(paths.run_dir(), exist_ok=True)
            with open(paths.tunnel_pid_path(), "w") as f:
                f.write(str(pid))
        except Exception:
            pass

    def _remove_pid(self):
        try:
            os.remove(paths.tunnel_pid_path())
        except OSError:
            pass

    # -- lifecycle --------------------------------------------------------
    def is_running(self) -> bool:
        if self.child is not None and self.child.poll() is None:
            return os.path.exists(self.local_socket)
        pid = self._pid_from_file()
        return bool(pid and _pid_alive(pid) and os.path.exists(self.local_socket))

    def spawn(self):
        os.makedirs(os.path.dirname(self.local_socket) or ".", mode=0o700, exist_ok=True)
        try:
            os.makedirs(paths.logs_dir(), exist_ok=True)
            log = open(paths.tunnel_log_path(), "ab")
        except OSError:
            log = subprocess.DEVNULL
        self.child = subprocess.Popen(
            self.ssh_argv(), start_new_session=True, stdout=log, stderr=log)
        if log is not subprocess.DEVNULL:
            log.close()  # the child inherited its own copy
        self._write_pid(self.child.pid)
        return self.child.pid

    def probe(self, timeout=2.0) -> bool:
        """A real round-trip through the tunnel — a socket connect() alone is
        not enough (a live-but-dead ssh accepts locally then blackholes)."""
        d = None
        try:
            d = DaemonClient(self.local_socket)
            d.request("whoami", timeout=timeout)
            self.last_ok = time.time()
            self._backoff_index = 0
            return True
        except Exception as e:
            self.last_error = str(e)
            return False
        finally:
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass

    def ensure(self, log=None) -> bool:
        if self.is_running() and self.probe():
            return True
        return self.restart(log=log)

    def restart(self, log=None) -> bool:
        self.stop()
        self.restarts += 1
        self.spawn()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.child is not None and self.child.poll() is not None:
                self.last_error = "ssh exited immediately"
                if log:
                    log(f"tunnel: ssh exited immediately ({self.host})")
                return False
            if os.path.exists(self.local_socket) and self.probe():
                self._backoff_index = 0
                if log:
                    log(f"tunnel: up ({self.host})")
                return True
            time.sleep(0.2)
        self.last_error = "tunnel did not come up within 20s"
        if log:
            log(f"tunnel: not up after 20s ({self.host})")
        return False

    def stop(self):
        pid = self.child.pid if self.child is not None else self._pid_from_file()
        if pid and _is_our_ssh(pid, self.local_socket):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.1)
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                time.sleep(0.3)
        self.child = None
        self._remove_pid()
        try:
            os.remove(self.local_socket)
        except OSError:
            pass

    # -- backoff ----------------------------------------------------------
    def next_backoff(self) -> int:
        delay = BACKOFF[min(self._backoff_index, len(BACKOFF) - 1)]
        self._backoff_index += 1
        return delay

    def reset_backoff(self):
        self._backoff_index = 0

    # -- status -----------------------------------------------------------
    def state(self) -> dict:
        return {
            "up": self.is_running(),
            "host": self.host,
            "remote_socket": self.remote_socket,
            "local_socket": self.local_socket,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "last_ok": self.last_ok,
        }
