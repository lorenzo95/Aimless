import pytest

gi = pytest.importorskip("gi")

from aimless import gtkui
from aimless.daemon import DaemonClient


def test_stop_all_kills_spawned_daemon(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setenv("AIMLESS_SOCK", str(home / "api.sock"))
    monkeypatch.setattr(gtkui, "TRAY_PID_FILE", str(home / "tray.pid"))
    monkeypatch.setattr(gtkui, "AIMLESSD_PID_FILE", str(home / "aimlessd.pid"))

    supervisor = gtkui.DaemonSupervisor()
    supervisor.ensure(log=print)
    assert supervisor.is_running()

    stopped = gtkui.stop_all()
    assert any("aimlessd" in s for s in stopped)
    assert not supervisor.is_running()

    stopped_again = gtkui.stop_all()
    assert stopped_again == []


def test_stop_all_stale_tray_pid_is_tolerated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setenv("AIMLESS_SOCK", str(home / "api.sock"))
    monkeypatch.setattr(gtkui, "TRAY_PID_FILE", str(home / "tray.pid"))
    monkeypatch.setattr(gtkui, "AIMLESSD_PID_FILE", str(home / "aimlessd.pid"))

    stale = tmp_path / "tray.pid"
    stale.write_text("999999999")
    assert gtkui.stop_all() == []
    assert not gtkui.DaemonSupervisor().is_running()
