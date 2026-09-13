import json
import os

import pytest

from aimless import paths, tunnel


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("AIMLESS_HOME", str(root))
    monkeypatch.delenv("AIMLESS_SOCK", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths.ensure_dirs()
    return root


def _write_prefs(remote):
    open(paths.prefs_path(), "w").write(json.dumps({"remote": remote} if remote else {}))


def test_remote_config_none_when_unset(home):
    assert tunnel.remote_config() is None
    assert tunnel.local_socket() is None


def test_remote_config_defaults(home, monkeypatch):
    _write_prefs({"host": "u@h", "socket": "/srv/daemon/api.sock"})
    cfg = tunnel.remote_config()
    assert cfg["host"] == "u@h"
    assert cfg["socket"] == "/srv/daemon/api.sock"
    assert cfg["local_socket"] == os.path.join(paths.run_dir(), "remote.sock")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/7")
    assert tunnel.remote_config()["local_socket"] == "/run/user/7/aimless/remote.sock"
    _write_prefs({"host": "u@h", "socket": "/s", "local_socket": "/tmp/x.sock"})
    assert tunnel.remote_config()["local_socket"] == "/tmp/x.sock"


def test_remote_config_invalid(home):
    _write_prefs({"host": "u@h"})  # no socket
    assert tunnel.remote_config() is None
    _write_prefs({"socket": "/s"})  # no host
    assert tunnel.remote_config() is None


def test_client_socket_precedence(home, monkeypatch):
    assert tunnel.client_socket() == paths.sock_path()
    _write_prefs({"host": "u@h", "socket": "/s", "local_socket": "/tmp/r.sock"})
    assert tunnel.client_socket() == "/tmp/r.sock"
    monkeypatch.setenv("AIMLESS_SOCK", "/tmp/env.sock")
    assert tunnel.client_socket() == "/tmp/env.sock"


def test_ssh_argv():
    t = tunnel.TunnelSupervisor(
        {"host": "u@h", "socket": "/srv/api.sock", "local_socket": "/run/r.sock"})
    argv = t.ssh_argv()
    assert argv[0] == "ssh"
    assert "-N" in argv
    assert "/run/r.sock:/srv/api.sock" in argv
    assert argv[-1] == "u@h"
    joined = " ".join(argv)
    for opt in ("BatchMode=yes", "ExitOnForwardFailure=yes", "StreamLocalBindUnlink=yes",
                "ServerAliveInterval=15", "TCPKeepAlive=yes"):
        assert opt in joined


def test_next_backoff_caps_and_resets():
    t = tunnel.TunnelSupervisor({"host": "u@h", "socket": "/s", "local_socket": "/l"})
    assert [t.next_backoff() for _ in range(7)] == [2, 4, 8, 15, 30, 30, 30]
    t.reset_backoff()
    assert t.next_backoff() == 2


def test_is_our_ssh(monkeypatch):
    monkeypatch.setattr(tunnel, "_cmdline", lambda pid: ["ssh", "-N", "-L", "/l:/r", "u@h"])
    assert tunnel._is_our_ssh(1, "/l") is True
    monkeypatch.setattr(tunnel, "_cmdline", lambda pid: ["python3", "x"])
    assert tunnel._is_our_ssh(1, "/l") is False
    monkeypatch.setattr(tunnel, "_cmdline", lambda pid: [])
    assert tunnel._is_our_ssh(1, "/l") is False


class _FakeChild:
    pid = 4242

    def poll(self):
        return None


def _fake_popen(local):
    def _popen(*args, **kwargs):
        open(local, "w").close()
        return _FakeChild()
    return _popen


def test_spawn_writes_pid_and_is_running(home, monkeypatch):
    local = str(home / "run" / "r.sock")
    t = tunnel.TunnelSupervisor({"host": "u@h", "socket": "/s", "local_socket": local})
    monkeypatch.setattr(tunnel.subprocess, "Popen", _fake_popen(local))
    t.spawn()
    assert open(paths.tunnel_pid_path()).read() == "4242"
    assert t.is_running() is True


def test_probe_true_and_false(home, monkeypatch):
    t = tunnel.TunnelSupervisor(
        {"host": "u@h", "socket": "/s", "local_socket": str(home / "r.sock")})

    class OK:
        def request(self, op, timeout=None):
            return {"key": "k"}

        def close(self):
            pass

    monkeypatch.setattr(tunnel, "DaemonClient", lambda path: OK())
    assert t.probe() is True

    class Bad:
        def request(self, op, timeout=None):
            raise OSError("nope")

        def close(self):
            pass

    monkeypatch.setattr(tunnel, "DaemonClient", lambda path: Bad())
    assert t.probe() is False
    assert "nope" in t.last_error


def test_restart_success_resets_backoff(home, monkeypatch):
    local = str(home / "run" / "r.sock")
    t = tunnel.TunnelSupervisor({"host": "u@h", "socket": "/s", "local_socket": local})
    monkeypatch.setattr(tunnel.subprocess, "Popen", _fake_popen(local))
    monkeypatch.setattr(t, "probe", lambda timeout=2.0: True)
    t.next_backoff()
    t.next_backoff()
    assert t.restart() is True
    assert t._backoff_index == 0
    assert t.restarts == 1
