"""ssh_tunnel.py - reach a remote aimlessd daemon through an SSH tunnel.

The daemon listens on a local unix socket (api.sock). To talk to an always-on
remote daemon (a VPS or the daemon_only container), the client opens an SSH
tunnel that creates a *local* unix socket whose traffic is forwarded, encrypted,
to the remote socket. Everything else in the client keeps using plain AF_UNIX
against that local path, so only this module knows a tunnel exists.

`local_socket` defaults to <CONFIG_DIR>/remote-api.sock - deliberately a
different path from the local daemon's api.sock, so SSH mode never collides
with a locally running daemon.
"""

import os
import signal
import socket
import subprocess
import time


class SSHTunnel:
    def __init__(self, host, remote_socket, local_socket, identity=None):
        self.host = host
        self.remote_socket = remote_socket
        self.local_socket = local_socket
        self.identity = identity
        self.child = None

    def command(self):
        cmd = ["ssh", "-N",
               "-o", "BatchMode=yes",
               "-o", "ExitOnForwardFailure=yes",
               "-o", "ServerAliveInterval=60",
               "-o", "ServerAliveCountMax=3",
               "-o", "ConnectTimeout=15",
               "-o", "StrictHostKeyChecking=accept-new"]
        if self.identity:
            cmd += ["-i", os.path.expanduser(self.identity)]
        cmd += ["-L", f"{self.local_socket}:{self.remote_socket}", self.host]
        return cmd

    def _local_ready(self):
        try:
            s = socket.socket(socket.AF_UNIX)
            s.connect(self.local_socket)
            s.close()
            return True
        except OSError:
            return False

    def start(self, log=None):
        self.stop()
        try:
            if os.path.exists(self.local_socket):
                os.remove(self.local_socket)
        except OSError:
            pass
        if log:
            log(f"ssh tunnel: {self.local_socket} -> {self.remote_socket} @ {self.host}")
        self.child = subprocess.Popen(
            self.command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.child.poll() is not None:
                self.child = None
                raise RuntimeError(
                    "ssh exited immediately - check the host/identity and that the "
                    "remote socket path is right (ssh -L exits on forward failure)")
            if self._local_ready():
                return True
            time.sleep(0.3)
        self.stop()
        raise RuntimeError("ssh tunnel did not come up within 30s")

    def is_ready(self):
        if self.child is None:
            return False
        if self.child.poll() is not None:
            self.child = None
            return False
        return self._local_ready()

    def stop(self):
        child, self.child = self.child, None
        if child is not None:
            try:
                os.killpg(os.getpgid(child.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    child.terminate()
                except Exception:
                    pass
            try:
                child.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
                except Exception:
                    pass
        try:
            if os.path.exists(self.local_socket):
                os.remove(self.local_socket)
        except OSError:
            pass