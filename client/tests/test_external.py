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


def test_probe_failure_confirms_then_restarts(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    t = app.supervisor.tunnel
    monkeypatch.setattr(t, "is_running", lambda: True)
    monkeypatch.setattr(t, "probe", lambda timeout=2.0: False)

    assert app.probe_tunnel() is True          # first failure -> confirm scheduled
    assert app._tunnel_fail == 1
    app._confirm_tunnel()                       # confirm also failed -> restart
    assert app._tunnel_fail == 0
    assert app._tunnel_restarting is True


def test_probe_healthy_is_noop(home, monkeypatch):
    _remote(home)
    app = gtkui.AimlessApp()
    t = app.supervisor.tunnel
    monkeypatch.setattr(t, "is_running", lambda: True)
    monkeypatch.setattr(t, "probe", lambda timeout=2.0: True)
    app._tunnel_fail = 1
    assert app.probe_tunnel() is True
    assert app._tunnel_fail == 0
    assert app._tunnel_restarting is False
