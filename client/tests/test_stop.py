import time

import pytest

gi = pytest.importorskip("gi")

from aimless import gtkui


def _iso(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setenv("AIMLESS_SOCK", str(home / "api.sock"))
    monkeypatch.setattr(gtkui, "CONFIG_DIR", str(config))
    monkeypatch.setattr(gtkui, "APP_PID_FILE", str(config / "app.pid"))
    monkeypatch.setattr(gtkui, "AIMLESSD_PID_FILE", str(config / "aimlessd.pid"))
    return home


def test_stop_all_kills_spawned_daemon(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    supervisor = gtkui.DaemonSupervisor()
    supervisor.ensure(log=print)
    assert supervisor.is_running()

    stopped = gtkui.stop_all()
    assert any("aimlessd" in s for s in stopped)
    assert not supervisor.is_running()

    stopped_again = gtkui.stop_all()
    assert stopped_again == []


def test_stop_all_stale_app_pid_is_tolerated(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    (tmp_path / "config" / "app.pid").write_text("999999999")
    assert gtkui.stop_all() == []
    assert not gtkui.DaemonSupervisor().is_running()


def test_stop_all_signals_running_app(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    config = tmp_path / "config"
    app_pid_file = config / "app.pid"

    import subprocess
    victim = subprocess.Popen(["sleep", "300"])
    app_pid_file.write_text(str(victim.pid))

    monkeypatch.setattr(gtkui.DaemonSupervisor, "is_running", lambda self: False)

    stopped = gtkui.stop_all()
    assert any("app (pid" in s for s in stopped)
    victim.wait(timeout=10)
    assert victim.poll() is not None


def test_stop_without_pidfile_stops_daemon_via_socket(tmp_path, monkeypatch):
    import subprocess
    home = _iso(tmp_path, monkeypatch)

    binary = gtkui.daemon_binary()
    assert binary, "aimlessd binary must be built for this test"
    proc = subprocess.Popen(
        [binary, "-datadir", str(home), "-api", str(home / "api.sock"), "-peers", "none"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    supervisor = gtkui.DaemonSupervisor()
    assert supervisor.stop() is None
    deadline = time.time() + 10
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.2)
    assert proc.poll() is not None, "daemon survived stop without a pidfile"
    assert not supervisor.is_running()
