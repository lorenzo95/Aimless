import json
import subprocess
import time

import pytest

from aimless import gtkui
from aimless.daemon import DaemonClient

import test_e2e


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setenv("AIMLESS_SOCK", str(home / "api.sock"))
    monkeypatch.setattr(gtkui, "TRAY_PID_FILE", str(home / "tray.pid"))
    monkeypatch.setattr(gtkui, "AIMLESSD_PID_FILE", str(home / "aimlessd.pid"))
    monkeypatch.setattr(gtkui, "GUI_PID_FILE", str(home / "gui.pid"))
    monkeypatch.setattr(gtkui, "SESSION_FILE", str(home / "session.json"))
    return home


def test_session_cache_survives_gui_reopen_but_not_daemon_restart(isolated):
    binp = gtkui.daemon_binary()
    assert binp, "aimlessd binary must be built for this test"
    sock = str(isolated / "api.sock")
    args = [binp, "-datadir", str(isolated), "-api", sock, "-peers", "none"]

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    test_wait_sock = sock
    deadline = time.time() + 30
    import os
    while time.time() < deadline and not os.path.exists(test_wait_sock):
        time.sleep(0.2)

    gtkui.write_session("secret-pw")
    assert gtkui.read_valid_session() == "secret-pw"
    assert gtkui.read_valid_session() == "secret-pw"

    proc.terminate()
    proc.wait(timeout=10)
    time.sleep(0.5)

    proc2 = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        import os
        while time.time() < deadline and not os.path.exists(sock):
            time.sleep(0.2)
        assert gtkui.read_valid_session() is None, "session must not survive a daemon restart"
    finally:
        proc2.terminate()
        proc2.wait(timeout=10)


def test_clear_session_removes_file(isolated):
    gtkui.CONFIG_DIR = str(isolated)
    isolated.joinpath("session.json").write_text("{}")
    monkey_file = isolated / "session.json"
    monkey_file.write_text("{}")
    gtkui.clear_session()
    assert not monkey_file.exists()
    gtkui.clear_session()
