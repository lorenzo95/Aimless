import json

import pytest

gi = pytest.importorskip("gi")

from aimless import paths, gtkui


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(root))
    monkeypatch.delenv("AIMLESS_SOCK", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    paths.ensure_dirs()
    return root


def _remote(home):
    remote = {
        "host": "u@always-on",
        "socket": "/srv/aimless-data/daemon/api.sock",
        "local_socket": str(home / "run" / "remote.sock"),
    }
    open(paths.prefs_path(), "w").write(json.dumps({"remote": remote}))
    return remote


def test_external_detection(home):
    remote = _remote(home)
    sup = gtkui.DaemonSupervisor()
    assert sup.external is True
    assert sup.tunnel is not None
    assert sup.datadir is None
    assert sup.sock == remote["local_socket"]


def test_local_mode_by_default(home):
    sup = gtkui.DaemonSupervisor()
    assert sup.external is False
    assert sup.tunnel is None
    assert sup.datadir == paths.daemon_dir()


def test_external_spawn_raises(home):
    _remote(home)
    sup = gtkui.DaemonSupervisor()
    with pytest.raises(RuntimeError):
        sup.spawn()


def test_external_refuses_when_local_daemon_running(home, monkeypatch):
    _remote(home)
    sup = gtkui.DaemonSupervisor()
    monkeypatch.setattr(sup, "_local_daemon_running", lambda: True)
    with pytest.raises(RuntimeError) as e:
        sup.ensure()
    assert "local aimlessd" in str(e.value)


def test_external_ensure_uses_tunnel_without_spawning(home, monkeypatch):
    _remote(home)
    sup = gtkui.DaemonSupervisor()
    monkeypatch.setattr(sup, "_local_daemon_running", lambda: False)
    monkeypatch.setattr(sup, "_reachable", lambda: True)
    calls = []
    monkeypatch.setattr(sup.tunnel, "ensure", lambda log=None: calls.append(1) or True)
    assert sup.ensure() is True
    assert calls


def test_external_stop_only_stops_tunnel(home, monkeypatch):
    _remote(home)
    sup = gtkui.DaemonSupervisor()
    called = []
    monkeypatch.setattr(sup.tunnel, "stop", lambda: called.append("tunnel"))
    monkeypatch.setattr(gtkui, "daemon_pid_from_socket", lambda: 111)
    sup.stop()
    assert called == ["tunnel"]


def test_stop_all_external_skips_pkill_daemon(home, monkeypatch):
    _remote(home)
    runs = []
    monkeypatch.setattr(gtkui.subprocess, "run",
                        lambda cmd, **k: runs.append(cmd))
    monkeypatch.setattr(gtkui.DaemonSupervisor, "stop", lambda self: None)
    gtkui.stop_all()
    assert ["pkill", "-x", "aimlessd"] not in runs


def test_probe_tunnel_down_confirms_then_restarts(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    t = app.supervisor.tunnel
    monkeypatch.setattr(t, "is_running", lambda: False)

    assert app.probe_tunnel() is True          # first miss -> confirm scheduled
    assert app._tunnel_fail == 1
    assert app._tunnel_restarting is False
    app._confirm_tunnel()                       # still down -> restart
    assert app._tunnel_fail == 0
    assert app._tunnel_restarting is True


def test_probe_daemon_down_does_not_restart_ssh(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    t = app.supervisor.tunnel
    monkeypatch.setattr(t, "is_running", lambda: True)
    monkeypatch.setattr(t, "probe", lambda timeout=2.0: False)
    assert app.probe_tunnel() is True
    assert app._tunnel_restarting is False, "must not restart ssh when only the daemon is down"
    assert app._tunnel_fail == 0
    assert app._daemon_down_logged is True


def test_probe_healthy_is_noop(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    t = app.supervisor.tunnel
    monkeypatch.setattr(t, "is_running", lambda: True)
    monkeypatch.setattr(t, "probe", lambda timeout=2.0: True)
    app._tunnel_fail = 1
    app._daemon_down_logged = True
    assert app.probe_tunnel() is True
    assert app._tunnel_fail == 0
    assert app._tunnel_restarting is False
    assert app._daemon_down_logged is False


def test_ensure_daemon_down_message(home, monkeypatch):
    _remote(home)
    sup = gtkui.DaemonSupervisor()
    monkeypatch.setattr(sup, "_local_daemon_running", lambda: False)
    monkeypatch.setattr(sup.tunnel, "ensure", lambda log=None: True)
    monkeypatch.setattr(sup, "_reachable", lambda: False)
    monkeypatch.setattr(gtkui.time, "time", lambda: float("inf"))  # skip the wait
    with pytest.raises(RuntimeError) as e:
        sup.ensure()
    assert "remote daemon" in str(e.value)


def test_ensure_tunnel_down_message(home, monkeypatch):
    _remote(home)
    sup = gtkui.DaemonSupervisor()
    monkeypatch.setattr(sup, "_local_daemon_running", lambda: False)
    monkeypatch.setattr(sup.tunnel, "ensure", lambda log=None: False)
    with pytest.raises(RuntimeError) as e:
        sup.ensure()
    assert "SSH tunnel" in str(e.value)


def test_connect_window_status_text():
    win = gtkui.ConnectWindow(
        {"host": "u@h", "socket": "/r.sock", "local_socket": "/l.sock"}, lambda: None)
    win.update("failed", "unreachable")
    assert "ssh: failed" in win.ssh_label.get_text()
    assert "daemon: unreachable" in win.daemon_label.get_text()
    win.update("connected", "connected")
    assert "connected" in win.ssh_label.get_text()
    assert "u@h" in win.ssh_label.get_text()
    win.destroy()


def test_try_connect_classifies_daemon_down(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    app._awaiting_connect = True

    def fake_run_async(fn, on_done=None, on_error=None):
        on_done(fn())

    monkeypatch.setattr(gtkui, "run_async", fake_run_async)
    monkeypatch.setattr(gtkui.GLib, "timeout_add_seconds", lambda *a, **k: 0)

    def boom(log=None):
        raise RuntimeError("remote daemon not reachable through the tunnel (u@h)")

    monkeypatch.setattr(app.supervisor, "ensure", boom)
    monkeypatch.setattr(app.supervisor.tunnel, "is_running", lambda: True)
    app._try_connect()
    assert app._connect_state == {"ssh": "connected", "daemon": "unreachable"}


def test_on_connected_opens_window(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    app._awaiting_connect = True
    app._connect_show = True
    app.tray = type("T", (), {"have_tray": True})()
    opened = []
    monkeypatch.setattr(app, "open_window", lambda: opened.append(1))
    monkeypatch.setattr(app, "log", lambda *a, **k: None)
    app._on_connected()
    assert app._awaiting_connect is False
    assert opened == [1]


class _FakeWindow:
    def __init__(self):
        self.polled = 0

    def poll_status(self):
        self.polled += 1


def test_do_tunnel_restart_without_window_is_safe(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    app.window = None
    monkeypatch.setattr(app.supervisor.tunnel, "restart", lambda log=None: True)
    monkeypatch.setattr(app.supervisor.tunnel, "reset_backoff", lambda: None)
    monkeypatch.setattr(app, "rewatch", lambda: None)
    assert app._do_tunnel_restart() is False


def test_do_tunnel_restart_refreshes_window(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    win = _FakeWindow()
    app.window = win
    monkeypatch.setattr(app.supervisor.tunnel, "restart", lambda log=None: True)
    monkeypatch.setattr(app.supervisor.tunnel, "reset_backoff", lambda: None)
    monkeypatch.setattr(app, "rewatch", lambda: None)
    assert app._do_tunnel_restart() is False
    assert win.polled == 1


def test_do_tunnel_restart_failure_refreshes_window(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    win = _FakeWindow()
    app.window = win
    monkeypatch.setattr(app.supervisor.tunnel, "restart", lambda log=None: False)
    monkeypatch.setattr(app, "_schedule_tunnel_restart", lambda: None)
    assert app._do_tunnel_restart() is False
    assert win.polled == 1


def test_heartbeat_swallows_daemon_errors(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    app.quitting = False
    monkeypatch.setattr(gtkui, "run_async", lambda fn, on_done=None, on_error=None: fn())
    assert app.heartbeat() == gtkui.GLib.SOURCE_CONTINUE


def test_heartbeat_stops_when_quitting(home):
    _remote(home)
    app = gtkui.AimlessApp()
    app.quitting = True
    assert app.heartbeat() == gtkui.GLib.SOURCE_REMOVE
