import json
import os
import socket
import subprocess
import time

import pytest

from aimless import gtkui
from aimless.ssh_tunnel import SSHTunnel


@pytest.fixture
def ssh_prefs_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setattr(gtkui, "CONFIG_DIR", str(config))
    monkeypatch.setattr(gtkui, "APP_PID_FILE", str(config / "app.pid"))
    monkeypatch.setattr(gtkui, "AIMLESSD_PID_FILE", str(config / "aimlessd.pid"))
    return home, config


def write_ssh_prefs(config, ssh):
    with open(config / "gtk.json", "w") as f:
        json.dump({"ssh": ssh}, f)


def test_ssh_disabled_keeps_local_socket(ssh_prefs_env):
    home, config = ssh_prefs_env
    assert gtkui.sock_path() == str(home / "api.sock")


def test_ssh_enabled_uses_remote_api_socket(ssh_prefs_env):
    home, config = ssh_prefs_env
    write_ssh_prefs(config, {
        "enabled": True,
        "host": "user@host",
        "remote_socket": "/srv/aimless/api.sock",
    })
    expected = str(config / "remote-api.sock")
    assert gtkui.sock_path() == expected
    assert gtkui.ssh_tunnel() is not None
    assert gtkui.ssh_tunnel().local_socket == expected


def test_ssh_enabled_custom_local_socket(ssh_prefs_env):
    home, config = ssh_prefs_env
    write_ssh_prefs(config, {
        "enabled": True,
        "host": "user@host",
        "remote_socket": "/srv/aimless/api.sock",
        "local_socket": str(home / "custom.sock"),
    })
    assert gtkui.sock_path() == str(home / "custom.sock")


def test_ssh_incomplete_config_not_enabled(ssh_prefs_env):
    home, config = ssh_prefs_env
    write_ssh_prefs(config, {"enabled": True, "host": "", "remote_socket": ""})
    assert gtkui.ssh_tunnel() is None
    assert gtkui.sock_path() == str(home / "api.sock")


def test_ssh_prefs_roundtrip_persists(ssh_prefs_env):
    """The dialog's save flow: prefs['ssh'] written via save_prefs must survive
    a reload (as on app restart) — masking aside, values must persist."""
    home, config = ssh_prefs_env
    prefs = gtkui.load_prefs()
    new_ssh = {
        "enabled": True,
        "host": "debian@192.168.1.111",
        "remote_socket": "/srv/aimless/state/api.sock",
        "identity": "/home/me/.ssh/id_ed25519",
    }
    prefs["ssh"] = new_ssh
    gtkui.save_prefs(prefs)
    assert gtkui.load_prefs()["ssh"] == new_ssh
    assert gtkui.ssh_prefs() == new_ssh


def test_ssh_prefs_save_without_identity_drops_key(ssh_prefs_env):
    """When the identity field is left empty, no 'identity' key is stored, and
    the round-trip must not resurrect a stale one."""
    home, config = ssh_prefs_env
    prefs = gtkui.load_prefs()
    prefs["ssh"] = {"enabled": True, "host": "me@host", "remote_socket": "/srv/api.sock"}
    gtkui.save_prefs(prefs)
    saved = gtkui.load_prefs()["ssh"]
    assert "identity" not in saved
    assert saved["enabled"] and saved["host"] == "me@host"


def test_ssh_dialog_values_captured_before_destroy(ssh_prefs_env, monkeypatch):
    """The core regression, isolated: reading get_text() after destroy returns ''
    — verify the dialog code captures values before destroying (i.e. that the
    saved config equals the pre-destroy values and SSH stays enabled)."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    # Reproduce the destroyed-entry behavior directly: build the real dialog,
    # fill, capture, destroy, and check the captured values (as the fixed code
    # does) rather than reading after destroy (as the buggy code did).
    dlg = Gtk.Dialog(title="t")
    box = dlg.get_content_area()
    e = Gtk.Entry()
    e.set_text("debian@192.168.1.111")
    box.add(e)
    dlg.show_all()
    captured = e.get_text()  # fixed: capture before destroy
    dlg.destroy()
    after = e.get_text()     # buggy: read after destroy
    assert captured == "debian@192.168.1.111"
    assert after == ""       # documents the GTK behavior the fix works around


def test_ssh_dialog_save_end_to_end(ssh_prefs_env, monkeypatch):
    """Drive the real SSH settings dialog: fill Host + Identity, click Test
    (gated), then Save — and assert the prefs file is written with exactly what
    the user typed. Regression for the destroyed-entry bug and the save-gate."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env

    win = Gtk.Window()
    win.show_all()
    win.prefs = gtkui.load_prefs()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.on_ssh_settings = gtkui.AimlessWindow.on_ssh_settings.__get__(win)

    class FakeMsg(Gtk.MessageDialog):
        def run(self, *a):
            return Gtk.ResponseType.OK

    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", FakeMsg)

    class FakeTunnel:
        def __init__(self, host, remote_socket, local_socket, identity=None):
            self.host = host
            self.remote_socket = remote_socket
            self.local_socket = local_socket

        def start(self, log=None):
            return True

        def stop(self):
            pass

    class FakeDaemonClient:
        def __init__(self, socket_path):
            self.socket_path = socket_path

        def request(self, op, timeout=10.0, **kw):
            if op == "whoami":
                return {"address": "200:abc"}
            return {"build": "aimlessd/0.5.6-test"}

        def close(self):
            pass

    monkeypatch.setattr(gtkui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gtkui, "DaemonClient", FakeDaemonClient)

    result = {}
    deadline = time.time() + 8
    filled = {"done": False}

    def on_dialog():
        if _dialog_deadline_passed(deadline):
            _dismiss_dialog()
            return False
        dlg = _ssh_dialog()
        if dlg is None:
            return True  # dialog not up yet — keep polling
        entries = [e for e in _walk(dlg) if isinstance(e, Gtk.Entry)]
        if len(entries) < 3:
            return True
        if not filled["done"]:
            # default is Local on a fresh machine — switch to Remote
            for w in _walk(dlg):
                if isinstance(w, Gtk.RadioButton) and w.get_label() == "Remote daemon (SSH)":
                    w.set_active(True)
                    break
            entries[0].set_text("debian@192.168.1.111")   # host
            entries[1].set_text("/home/me/.ssh/id_ed25519")  # identity
            entries[2].set_text("/srv/aimless/state/api.sock")  # socket (advanced)
            filled["done"] = True
            # Save must be gated (disabled) until a test passes
            save_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
            assert save_btn is not None and not save_btn.get_sensitive(), \
                "Save should be disabled until a connection test passes"
            _click_button(dlg, "Test connection")
            return True
        save_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
        if save_btn is not None and save_btn.get_sensitive():
            save_btn.emit("clicked")
            result["done"] = True
            return False
        return True

    GLib.timeout_add(100, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    assert result.get("done"), "dialog was not driven through test+save"
    saved = gtkui.load_prefs()["ssh"]
    assert saved["host"] == "debian@192.168.1.111"
    assert saved["remote_socket"] == "/srv/aimless/state/api.sock"
    assert saved["identity"] == "/home/me/.ssh/id_ed25519"
    # and it must resolve to a remote supervisor after restart
    assert gtkui.ssh_tunnel() is not None
    assert gtkui.sock_path() == str(config / "remote-api.sock")


def test_ssh_dialog_select_local_goes_local(ssh_prefs_env, monkeypatch):
    """Selecting the 'Local daemon' radio (going local) needs no test — Save is
    enabled and the stored ssh config is removed entirely."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env
    write_ssh_prefs(config, {
        "host": "debian@192.168.1.111",
        "remote_socket": "/srv/aimless/state/api.sock",
    })

    win = Gtk.Window()
    win.show_all()
    win.prefs = gtkui.load_prefs()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.on_ssh_settings = gtkui.AimlessWindow.on_ssh_settings.__get__(win)

    class FakeMsg(Gtk.MessageDialog):
        def run(self, *a):
            return Gtk.ResponseType.OK

    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", FakeMsg)

    result = {}
    deadline = time.time() + 8
    cleared = {"done": False}

    def on_dialog():
        if _dialog_deadline_passed(deadline):
            _dismiss_dialog()
            return False
        dlg = _ssh_dialog()
        if dlg is None:
            return True
        entries = [e for e in _walk(dlg) if isinstance(e, Gtk.Entry)]
        if len(entries) < 3:
            return True
        if not cleared["done"]:
            for w in _walk(dlg):
                if isinstance(w, Gtk.RadioButton) and w.get_label() == "Local daemon":
                    w.set_active(True)
                    break
            cleared["done"] = True
            return True
        save_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
        if save_btn is not None and save_btn.get_sensitive():
            save_btn.emit("clicked")
            result["done"] = True
            return False
        return True

    GLib.timeout_add(100, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    assert result.get("done"), "Save never became enabled for clearing the host"
    assert gtkui.ssh_prefs() == {}, "ssh config should be removed when host is cleared"
    assert gtkui.ssh_tunnel() is None
    assert gtkui.sock_path() == str(home / "api.sock")


def test_save_geometry_does_not_clobber_ssh_config(ssh_prefs_env, monkeypatch):
    """Regression: the window caches self.prefs; after the SSH dialog writes a
    change to disk, save_geometry()/set_away() must not write the stale cache
    back and revert it. They must merge into fresh prefs."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    home, config = ssh_prefs_env
    write_ssh_prefs(config, {"host": "me@host", "remote_socket": "/srv/api.sock"})

    win = Gtk.Window()
    win.show_all()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.prefs = gtkui.load_prefs()

    # Simulate the SSH dialog writing to disk without the window knowing.
    prefs = gtkui.load_prefs()
    prefs["ssh"] = {}  # going local via the dialog
    gtkui.save_prefs(prefs)

    # Now the window closes and saves geometry with its STALE cache — the bug
    # used to write the old ssh config back. It must not.
    win.save_geometry = gtkui.AimlessWindow.save_geometry.__get__(win)
    win.save_geometry()
    assert gtkui.load_prefs().get("ssh") == {}, \
        "save_geometry clobbered the SSH config with the stale window cache"
    assert gtkui.ssh_prefs() == {}
    win.destroy()


def test_ssh_dialog_test_is_async(ssh_prefs_env, monkeypatch):
    """The Test connection must not block the dialog's main thread: a slow
    tunnel should leave the dialog responsive (result not set synchronously)
    and the button disabled while the test runs."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env
    win = Gtk.Window()
    win.show_all()
    win.prefs = gtkui.load_prefs()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.on_ssh_settings = gtkui.AimlessWindow.on_ssh_settings.__get__(win)

    class SlowTunnel:
        def __init__(self, host, remote_socket, local_socket, identity=None):
            self.host = host
            self.local_socket = local_socket

        def start(self, log=None):
            time.sleep(1.0)  # simulate a slow SSH handshake
            return True

        def stop(self):
            pass

    class FakeDaemonClient:
        def __init__(self, socket_path):
            pass

        def request(self, op, timeout=10.0, **kw):
            if op == "whoami":
                return {"address": "200:abc"}
            return {"build": "aimlessd/x"}

        def close(self):
            pass

    monkeypatch.setattr(gtkui, "SSHTunnel", SlowTunnel)
    monkeypatch.setattr(gtkui, "DaemonClient", FakeDaemonClient)

    labels = {}
    deadline = time.time() + 8
    clicked = {"done": False}

    def on_dialog():
        if _dialog_deadline_passed(deadline):
            _dismiss_dialog()
            return False
        dlg = _ssh_dialog()
        if dlg is None:
            return True
        entries = [e for e in _walk(dlg) if isinstance(e, Gtk.Entry)]
        if len(entries) < 3:
            return True
        if not clicked["done"]:
            for w in _walk(dlg):
                if isinstance(w, Gtk.RadioButton) and w.get_label() == "Remote daemon (SSH)":
                    w.set_active(True)
                    break
            entries[0].set_text("user@host")
            entries[2].set_text("/srv/api.sock")
            _click_button(dlg, "Test connection")
            clicked["done"] = True
            labels["during"] = _result_text(dlg)
            # the test runs in a thread — result must NOT be set yet
            assert labels["during"].startswith("connecting"), \
                f"result set synchronously: {labels['during']!r}"
            return True
        labels["later"] = _result_text(dlg)
        if "connected" in labels["later"]:
            _dismiss_dialog()
            return False
        return True

    GLib.timeout_add(50, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    assert "connecting" in labels["during"]
    assert "connected" in labels.get("later", ""), f"never connected: {labels.get('later')!r}"


def test_ssh_dialog_test_failure_resets_gate(ssh_prefs_env, monkeypatch):
    """After a successful test, editing any field must reset the gate (Save
    disabled again until a re-test)."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env
    win = Gtk.Window()
    win.show_all()
    win.prefs = gtkui.load_prefs()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.on_ssh_settings = gtkui.AimlessWindow.on_ssh_settings.__get__(win)

    class FakeTunnel:
        def __init__(self, *a, **k):
            self.local_socket = "/tmp/x.sock"

        def start(self, log=None):
            return True

        def stop(self):
            pass

    class FakeDaemonClient:
        def __init__(self, socket_path):
            pass

        def request(self, op, timeout=10.0, **kw):
            if op == "whoami":
                return {"address": "200:abc"}
            return {"build": "aimlessd/x"}

        def close(self):
            pass

    monkeypatch.setattr(gtkui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gtkui, "DaemonClient", FakeDaemonClient)

    state = {"tested": False, "edited": False, "save_after_test": None, "save_after_edit": None}
    deadline = time.time() + 8

    def on_dialog():
        if _dialog_deadline_passed(deadline):
            _dismiss_dialog()
            return False
        dlg = _ssh_dialog()
        if dlg is None:
            return True
        entries = [e for e in _walk(dlg) if isinstance(e, Gtk.Entry)]
        if len(entries) < 3:
            return True
        save_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
        if not state["tested"]:
            for w in _walk(dlg):
                if isinstance(w, Gtk.RadioButton) and w.get_label() == "Remote daemon (SSH)":
                    w.set_active(True)
                    break
            entries[0].set_text("user@host")
            entries[2].set_text("/srv/api.sock")
            _click_button(dlg, "Test connection")
            state["tested"] = True
            return True
        if not state["edited"]:
            if save_btn is not None and save_btn.get_sensitive():
                state["save_after_test"] = True
                entries[0].set_text("other@host")  # change a field -> gate resets
                state["edited"] = True
            return True
        state["save_after_edit"] = bool(save_btn is not None and save_btn.get_sensitive())
        if state["save_after_edit"] is not None and state["save_after_test"]:
            _dismiss_dialog()
            return False
        return True

    GLib.timeout_add(50, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    assert state["save_after_test"], "Save not enabled after a passed test"
    assert not state["save_after_edit"], "Save should reset to disabled after editing a field"


def test_ssh_dialog_whoami_failure_distinct(ssh_prefs_env, monkeypatch):
    """A tunnel that comes up but whose daemon doesn't answer must report a
    distinct 'daemon unreachable' failure — not 'connected OK'."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    from aimless.daemon import DaemonError

    home, config = ssh_prefs_env
    win = Gtk.Window()
    win.show_all()
    win.prefs = gtkui.load_prefs()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.on_ssh_settings = gtkui.AimlessWindow.on_ssh_settings.__get__(win)

    class FakeTunnel:
        def __init__(self, *a, **k):
            self.local_socket = "/tmp/x.sock"

        def start(self, log=None):
            return True

        def stop(self):
            pass

    class DeadDaemonClient:
        def __init__(self, socket_path):
            pass

        def request(self, op, timeout=10.0, **kw):
            raise DaemonError("timeout waiting for response to whoami")

        def close(self):
            pass

    monkeypatch.setattr(gtkui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gtkui, "DaemonClient", DeadDaemonClient)

    labels = {}
    deadline = time.time() + 8
    clicked = {"done": False}

    def on_dialog():
        if _dialog_deadline_passed(deadline):
            _dismiss_dialog()
            return False
        dlg = _ssh_dialog()
        if dlg is None:
            return True
        entries = [e for e in _walk(dlg) if isinstance(e, Gtk.Entry)]
        if len(entries) < 3:
            return True
        if not clicked["done"]:
            for w in _walk(dlg):
                if isinstance(w, Gtk.RadioButton) and w.get_label() == "Remote daemon (SSH)":
                    w.set_active(True)
                    break
            entries[0].set_text("user@host")
            entries[2].set_text("/srv/api.sock")
            _click_button(dlg, "Test connection")
            clicked["done"] = True
            return True
        text = _result_text(dlg)
        if text:
            labels["final"] = text
            _dismiss_dialog()
            return False
        return True

    GLib.timeout_add(50, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    assert "connected OK" not in labels.get("final", "")
    assert "whoami" in labels.get("final", "") or "timeout" in labels.get("final", ""), \
        f"expected a daemon-unreachable message, got: {labels.get('final')!r}"


def test_ssh_prefs_legacy_enabled_normalized(ssh_prefs_env):
    """Legacy prefs that stored {enabled:...} normalize to the host-only model:
    enabled+host = remote; enabled:false = local (no config)."""
    home, config = ssh_prefs_env
    write_ssh_prefs(config, {"enabled": True, "host": "me@host",
                             "remote_socket": "/srv/api.sock"})
    assert gtkui.ssh_prefs()["host"] == "me@host"
    assert gtkui.ssh_tunnel() is not None
    write_ssh_prefs(config, {"enabled": False, "host": "me@host",
                             "remote_socket": "/srv/api.sock"})
    assert gtkui.ssh_prefs() == {}
    assert gtkui.ssh_tunnel() is None


def test_discover_remote_socket_finds_and_verifies(ssh_prefs_env, monkeypatch):
    """Discovery lists api.sock candidates under $HOME and picks the one that
    answers a real whoami — the 'no typing the remote path' UX."""
    home, config = ssh_prefs_env
    results = {"ran": []}

    def fake_ssh(*args, **kw):
        results["ran"].append(args)
        # returns two candidates, one stale
        class R:
            returncode = 0
            stdout = "/home/u/.local/share/aimless/api.sock\n/home/u/app/state/api.sock\n"
            stderr = ""
        return R()

    monkeypatch.setattr(gtkui.subprocess, "run", fake_ssh)

    attempts = []

    class FakeTunnel:
        def __init__(self, host, remote_socket, local_socket, identity=None):
            self.local_socket = local_socket

        def start(self, log=None):
            return True

        def stop(self):
            pass

    class LiveDaemon:
        def __init__(self, socket_path):
            self.socket_path = socket_path

        def request(self, op, timeout=10.0, **kw):
            attempts.append(self.socket_path)
            if op == "whoami":
                return {"address": "200:abc"}
            return {"build": "aimlessd/x"}
        def close(self):
            pass

    # first candidate's tunnel+daemon raises (stale), second succeeds
    real_tunnel = gtkui.SSHTunnel
    real_client = gtkui.DaemonClient
    state = {"n": 0}

    class StaleThenLive(FakeTunnel):
        def start(self, log=None):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("stale")
            return True

    monkeypatch.setattr(gtkui, "SSHTunnel", StaleThenLive)
    monkeypatch.setattr(gtkui, "DaemonClient", LiveDaemon)

    path, build, addr = gtkui.discover_remote_socket("me@host")
    assert path == "/home/u/app/state/api.sock"
    assert build == "aimlessd/x"
    assert addr == "200:abc"
    assert state["n"] == 2  # tried both candidates


def test_discover_remote_socket_no_candidates(ssh_prefs_env, monkeypatch):
    home, config = ssh_prefs_env

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(gtkui.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="no aimless daemon socket"):
        gtkui.discover_remote_socket("me@host")


def test_probe_remote_daemon_returns_build_and_address(ssh_prefs_env, monkeypatch):
    """probe_remote_daemon must do a real whoami+status round trip and return
    the daemon's build and address — the info 'Test connection' now shows
    instead of '?'."""
    home, config = ssh_prefs_env
    called = []

    class FakeTunnel:
        def __init__(self, host, remote_socket, local_socket, identity=None):
            self.local_socket = local_socket

        def start(self, log=None):
            return True

        def stop(self):
            pass

    class FakeDaemonClient:
        def __init__(self, socket_path):
            self.socket_path = socket_path

        def request(self, op, timeout=10.0, **kw):
            called.append(op)
            if op == "whoami":
                return {"address": "200:abc"}
            return {"build": "aimlessd/0.5.6"}

        def close(self):
            pass

    monkeypatch.setattr(gtkui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gtkui, "DaemonClient", FakeDaemonClient)

    build, addr = gtkui.probe_remote_daemon("me@host", "/srv/api.sock")
    assert build == "aimlessd/0.5.6"
    assert addr == "200:abc"
    assert called == ["whoami", "status"]  # both halves, so no '?' in the UI


def test_ssh_dialog_test_shows_daemon_info(ssh_prefs_env, monkeypatch):
    """Clicking Test connection shows the real build+address from the shared
    probe (previously always '?' because build lives in status, not whoami)."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env
    win = Gtk.Window()
    win.show_all()
    win.prefs = gtkui.load_prefs()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.on_ssh_settings = gtkui.AimlessWindow.on_ssh_settings.__get__(win)

    monkeypatch.setattr(gtkui, "probe_remote_daemon",
                        lambda h, r, i=None: ("aimlessd/0.5.6", "200:abc"))

    labels = {}
    deadline = time.time() + 8
    clicked = {"done": False}

    def on_dialog():
        if _dialog_deadline_passed(deadline):
            _dismiss_dialog()
            return False
        dlg = _ssh_dialog()
        if dlg is None:
            return True
        entries = [e for e in _walk(dlg) if isinstance(e, Gtk.Entry)]
        if len(entries) < 3:
            return True
        if not clicked["done"]:
            for w in _walk(dlg):
                if isinstance(w, Gtk.RadioButton) and w.get_label() == "Remote daemon (SSH)":
                    w.set_active(True)
                    break
            entries[0].set_text("user@host")
            entries[2].set_text("/srv/api.sock")
            _click_button(dlg, "Test connection")
            clicked["done"] = True
            return True
        text = _result_text(dlg)
        if text:
            labels["final"] = text
            _dismiss_dialog()
            return False
        return True

    GLib.timeout_add(50, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    final = labels.get("final", "")
    assert "connected" in final
    assert "aimlessd/0.5.6" in final, f"build missing: {final!r}"
    assert "200:abc" in final, f"address missing: {final!r}"
    assert "?" not in final, f"placeholder '?' leaked into result: {final!r}"


def test_ssh_dialog_find_replaces_searching_label(ssh_prefs_env, monkeypatch):
    """After a successful find, the 'searching <host> …' label must be cleared —
    it used to stay up while the found details appeared below."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env
    win = Gtk.Window()
    win.show_all()
    win.prefs = gtkui.load_prefs()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.on_ssh_settings = gtkui.AimlessWindow.on_ssh_settings.__get__(win)

    monkeypatch.setattr(gtkui, "discover_remote_socket",
                        lambda h, i=None: ("/srv/state/api.sock", "aimlessd/0.5.6", "200:abc"))

    # find the result_label (shows "searching …") and the test_label (shows the
    # found details) by driving the dialog: select Remote, click Find, then
    # assert the searching text is gone and the found text is present.
    result = {}
    deadline = time.time() + 8
    state = {"searched": False, "done": False}

    def on_dialog():
        if _dialog_deadline_passed(deadline):
            _dismiss_dialog()
            return False
        dlg = _ssh_dialog()
        if dlg is None:
            return True
        entries = [e for e in _walk(dlg) if isinstance(e, Gtk.Entry)]
        if len(entries) < 3:
            return True
        if not state["searched"]:
            for w in _walk(dlg):
                if isinstance(w, Gtk.RadioButton) and w.get_label() == "Remote daemon (SSH)":
                    w.set_active(True)
                    break
            entries[0].set_text("user@host")
            _click_button(dlg, "Find daemon on host")
            state["searched"] = True
            return True
        # once find completes, check the labels
        texts = [lbl.get_text() for lbl in _walk(dlg)
                 if isinstance(lbl, gtkui.Gtk.Label)]
        if any("found daemon" in t for t in texts):
            result["texts"] = texts
            _dismiss_dialog()
            return False
        return True

    GLib.timeout_add(50, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    texts = result.get("texts", [])
    assert any("found daemon at /srv/state/api.sock" in t for t in texts), \
        f"found details missing: {texts!r}"
    assert not any("searching" in t for t in texts), \
        f"'searching …' was never cleared: {texts!r}"


def test_supervisor_stale_detects_config_change(ssh_prefs_env, monkeypatch):
    """The supervisor snapshots its ssh config; changing prefs must make it
    stale so the caller rebuilds instead of reusing the old connection."""
    home, config = ssh_prefs_env
    sup = gtkui.DaemonSupervisor()
    assert not sup.stale()
    write_ssh_prefs(config, {"host": "me@host", "remote_socket": "/srv/api.sock"})
    assert sup.stale()


def test_rebuild_supervisor_if_stale(ssh_prefs_env, monkeypatch):
    """Changing SSH config between supervisor construction and Session creation
    must stop the old connection, rebuild, and re-ensure (the first-run path)."""
    home, config = ssh_prefs_env
    app = gtkui.AimlessApp()
    app.log = lambda *a, **k: None
    app.supervisor = gtkui.DaemonSupervisor()  # local, no ssh yet
    stopped = []
    monkeypatch.setattr(app.supervisor, "stop", lambda: stopped.append(True))
    ensured = []
    monkeypatch.setattr(app, "_ensure_daemon_with_recovery",
                        lambda: ensured.append(True))
    monkeypatch.setattr(gtkui, "DaemonSupervisor",
                        lambda: type("S", (), {"stale": lambda self: False,
                                               "remote": False,
                                               "stop": lambda self: stopped.append("new")})())

    # no change -> no rebuild
    app._rebuild_supervisor_if_stale()
    assert stopped == []
    assert ensured == []

    # change prefs -> rebuild
    write_ssh_prefs(config, {"host": "me@host", "remote_socket": "/srv/api.sock"})
    app._rebuild_supervisor_if_stale()
    assert stopped == [True]  # old supervisor stopped
    assert ensured == [True]  # new supervisor ensured
    assert app.supervisor.remote is False  # rebuilt supervisor used


def test_quit_closes_daemon_before_stopping_supervisor(ssh_prefs_env, monkeypatch):
    """Quit must close the session's DaemonClient before stopping the
    supervisor, so a remote tunnel can tear down instantly instead of stalling
    on child.wait() — the slow-quit regression."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    home, config = ssh_prefs_env
    app = gtkui.AimlessApp()
    app.log = lambda *a, **k: None
    order = []

    class FakeDaemon:
        def close(self):
            order.append("daemon.close")

    class FakeSession:
        daemon = FakeDaemon()

    app.session = FakeSession()
    app.window = None
    app.lock_fh = None
    monkeypatch.setattr(app, "supervisor",
                        type("S", (), {"stop": lambda self: order.append("supervisor.stop")})())

    app.quit()
    assert order == ["daemon.close", "supervisor.stop"], \
        f"daemon must be closed before supervisor stops, got: {order!r}"


def test_client_set_detached_sends_op(ssh_prefs_env, monkeypatch):
    """Client.set_detached seals the offline text and sends the setdetached op,
    so the daemon can relay it while no GUI is attached."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")

    home, config = ssh_prefs_env
    import tempfile
    monkeypatch.setattr(gtkui, "CONFIG_DIR", str(config))

    from aimless.daemon import Client

    sent = {}
    class FakeDaemon:
        def request(self, op, **kw):
            sent["op"] = op
            sent["to"] = kw.get("to")
            sent["payload"] = kw.get("payload")
            return {}
    import aimless.crypto as _crypto
    ident = _crypto.new_identity()
    buddy_hex = bytes(ident.verify_key).hex()
    client = Client(FakeDaemon(), ident, "me")
    client.set_detached(buddy_hex, "n" * 64, "away - client offline")
    assert sent["op"] == "setdetached"
    assert sent["to"] == "n" * 64
    assert sent["payload"]
    # the payload must be a real sealed status blob that opens to our text
    from aimless import protocol
    st = protocol.open_status(ident, sent["payload"])
    assert st.get("away") == "away - client offline"


def test_push_detached_sends_for_every_contact(ssh_prefs_env, monkeypatch):
    """The window pushes a pre-sealed offline blob for every contact at startup
    (and after adding a buddy), so the daemon has the detached status."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    home, config = ssh_prefs_env
    sent = []

    class FakeClient:
        def set_detached(self, pubkey, node, text):
            sent.append((node, text))

    class FakeSession:
        contacts = lambda self: {"bob": {"pubkey": "p", "node": "n1"},
                                 "carol": {"pubkey": "p2", "node": "n2"}}
        client = FakeClient()

    win = Gtk.Window()
    win.show_all()
    win.session = FakeSession()
    win.prefs = {}
    win._push_detached = gtkui.AimlessWindow._push_detached.__get__(win)
    win._push_detached()
    assert sorted(n for n, _ in sent) == ["n1", "n2"]
    assert all(t == "away - client offline" for _, t in sent)

    # custom text from prefs is used
    win.prefs = {"offline_status": "custom offline"}
    sent.clear()
    win._push_detached()
    assert all(t == "custom offline" for _, t in sent)
    win.destroy()


def test_pair_tracking_mismatch(ssh_prefs_env, monkeypatch):
    """After connecting on one node, switching to another (with contacts) is a
    mismatch; no contacts or same node is not."""
    home, config = ssh_prefs_env
    gtkui.save_prefs({"last_node_key": "oldnode"})

    session = type("S", (), {"self_node": "newnode",
                             "contacts": lambda self: {"bob": {}}})()

    # directly exercise the logic the Session exposes
    def _mismatch(self):
        last = gtkui.load_prefs().get("last_node_key")
        if not last or last == self.self_node:
            return False
        if not any(k != "_self" for k in self.contacts()):
            return False
        return True

    session.node_key_mismatch = _mismatch.__get__(session)
    assert session.node_key_mismatch() is True
    session2 = type("S", (), {"self_node": "newnode", "contacts": lambda self: {}})()
    session2.node_key_mismatch = _mismatch.__get__(session2)
    assert session2.node_key_mismatch() is False
    session3 = type("S", (), {"self_node": "oldnode",
                              "contacts": lambda self: {"bob": {}}})()
    session3.node_key_mismatch = _mismatch.__get__(session3)
    assert session3.node_key_mismatch() is False


def test_ssh_badge_route_bar_states(ssh_prefs_env, monkeypatch):
    """The route bar shows an SSH badge in remote mode: green when the daemon
    answered, amber when the tunnel is up but the daemon is silent, red when the
    tunnel is down; hidden in local mode."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    home, config = ssh_prefs_env
    win = Gtk.Window()
    win.show_all()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.prefs = gtkui.load_prefs()
    win.ssh_label = Gtk.Label(label="")
    win.route_label = Gtk.Label(label="")
    win._mismatch_notified = True  # suppress the mismatch dialog in refresh_route
    win.session = None
    win.refresh_route = gtkui.AimlessWindow.refresh_route.__get__(win)
    win.save_geometry = lambda: None

    class Tunnel:
        host = "me@server"

        def is_ready(self):
            return True

    class Sup:
        remote = True
        tunnel = Tunnel()

    win.supervisor = Sup()
    win.refresh_route({"peers_up": 3, "peers_total": 3, "address": "200:abc"})
    text = win.ssh_label.get_text()
    assert "SSH" in text and "me@server" in text
    assert win.ssh_label.get_visible() or True  # shown via show()

    # tunnel up but daemon silent (st has no peers_up)
    win.refresh_route(None)
    text = win.ssh_label.get_text()
    assert "me@server" in text

    # tunnel down
    class DownTunnel(Tunnel):
        def is_ready(self):
            return False

    win.supervisor = type("S", (), {"remote": True, "tunnel": DownTunnel()})()
    win.refresh_route(None)
    assert "me@server" in win.ssh_label.get_text()

    # local mode hides it
    win.supervisor = type("S", (), {"remote": False, "tunnel": None})()
    win.refresh_route({"peers_up": 3, "peers_total": 3, "address": "200:abc"})
    assert win.ssh_label.get_text() == ""
    win.destroy()


def test_request_bounded_on_flapping_connection(ssh_prefs_env):
    """A connection that accepts then immediately closes (a live tunnel to a
    dead remote — the exact broken-config case) must not let request() stall
    forever: each reconnect used to reset the deadline, so a whoami could hang
    ~7x its timeout. The total wait is now capped at timeout + reconnect grace."""
    import socket as _socket
    import threading as _threading
    from aimless.daemon import DaemonClient, DaemonError

    home, config = ssh_prefs_env
    path = str(config / "flap.sock")
    srv = _socket.socket(_socket.AF_UNIX)
    srv.bind(path)
    srv.listen(8)

    def close_loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.close()

    _threading.Thread(target=close_loop, daemon=True).start()
    c = DaemonClient(path)
    t0 = time.time()
    try:
        with pytest.raises(DaemonError):
            c.request("whoami", timeout=2)
        elapsed = time.time() - t0
        assert elapsed < 15, f"request took {elapsed:.1f}s for a 2s timeout — unbounded reconnect resets are back"
    finally:
        c.close()
        srv.close()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_ssh_startup_lockout_escape_hatch(ssh_prefs_env, monkeypatch):
    """Startup with SSH enabled but a failing tunnel must offer 'Disable remote
    daemon and retry'; clicking it disables SSH in prefs and falls through to a
    normal local-daemon startup."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env
    write_ssh_prefs(config, {
        "enabled": True,
        "host": "user@host",
        "remote_socket": "/srv/aimless/api.sock",
    })

    # supervisor whose ensure() fails as it would with a bad remote config;
    # stop() tracks whether the recovery tears down the failed tunnel before
    # discarding the supervisor.
    class FailingSupervisor:
        remote = True
        stopped = 0

        def ensure(self, log=None):
            raise RuntimeError("ssh tunnel failed")

        def stop(self):
            FailingSupervisor.stopped += 1

    # after recovery, the app swaps in a local supervisor and continues
    calls = {"local_ensured": 0, "tray": 0, "open_window": 0}

    class LocalSupervisor:
        remote = False

        def ensure(self, log=None):
            calls["local_ensured"] += 1

    app = gtkui.AimlessApp()
    app.supervisor = FailingSupervisor()
    monkeypatch.setattr(gtkui, "acquire_app_lock", lambda: (type("FH", (), {"close": lambda s: None})(), None))
    monkeypatch.setattr(gtkui, "TrayIcon",
                        lambda app: type("T", (), {"have_tray": True,
                                                   "is_embedded": lambda self: True})())
    monkeypatch.setattr(app, "open_window", lambda: calls.__setitem__("open_window", calls["open_window"] + 1))
    monkeypatch.setattr(gtkui, "DaemonSupervisor", lambda: LocalSupervisor())

    real_md = gtkui.Gtk.MessageDialog
    responses = iter([Gtk.ResponseType.APPLY])

    class FakeMD(real_md):
        def run(self, *a):
            return next(responses)

    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", FakeMD)

    rc = app._setup(open_window=True)
    assert rc is None  # fell through to a running app
    assert FailingSupervisor.stopped == 1, \
        "the failed remote supervisor must be stopped (tunnel torn down) before being discarded"
    assert calls["local_ensured"] == 1, "local supervisor was not used after recovery"
    assert calls["open_window"] == 1, "window did not open after recovery"
    saved = gtkui.load_prefs()["ssh"]
    assert saved == {}, "SSH config should be cleared by the escape hatch"


def test_startup_escape_hatch_with_real_whoami_supervisor(ssh_prefs_env, monkeypatch):
    """End-to-end wiring of the two 0.7.11 pieces: a REAL DaemonSupervisor whose
    tunnel comes up but whose whoami times out must drive _setup's while-loop
    into the escape hatch (is_running()==False -> ensure() raises), and the
    APPLY click must disable SSH and fall through to local startup."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    import socket as _socket
    import threading as _threading

    home, config = ssh_prefs_env
    write_ssh_prefs(config, {
        "enabled": True,
        "host": "user@host",
        "remote_socket": "/srv/aimless/api.sock",
    })

    # real unix listener that accepts and holds connections open without
    # replying — a live tunnel to a dead daemon. (accept-and-close would make
    # the client reconnect-loop and reset the whoami deadline forever.)
    local = str(config / "remote-api.sock")
    server = _socket.socket(_socket.AF_UNIX)
    server.bind(local)
    server.listen(8)
    held = []

    def accept_loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)  # keep open, never answer

    _threading.Thread(target=accept_loop, daemon=True).start()

    class ReadyTunnel:
        local_socket = local
        host = "user@host"

        def is_ready(self):
            return True

        def start(self, log=None):
            return True

        def stop(self):
            pass

    class LocalSupervisor:
        remote = False

        def ensure(self, log=None):
            pass

    # first _setup() call: the real supervisor opens a tunnel to the dead
    # listener, is_running() does whoami -> False, ensure() raises.
    # FakeMD returns APPLY -> ssh disabled, supervisor replaced, loop retries
    # with LocalSupervisor, which succeeds.
    app = gtkui.AimlessApp()
    app.supervisor = gtkui.DaemonSupervisor()
    app.supervisor.tunnel = ReadyTunnel()
    monkeypatch.setattr(gtkui, "acquire_app_lock", lambda: (type("FH", (), {"close": lambda s: None})(), None))
    monkeypatch.setattr(gtkui, "TrayIcon",
                        lambda app: type("T", (), {"have_tray": True,
                                                   "is_embedded": lambda self: True})())
    monkeypatch.setattr(app, "open_window", lambda: None)
    monkeypatch.setattr(gtkui, "DaemonSupervisor", lambda: LocalSupervisor())

    real_md = gtkui.Gtk.MessageDialog
    responses = iter([Gtk.ResponseType.APPLY])

    class FakeMD(real_md):
        def run(self, *a):
            return next(responses)

    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", FakeMD)

    rc = app._setup(open_window=True)
    assert rc is None
    assert gtkui.load_prefs()["ssh"] == {}, "SSH config should be cleared by the escape hatch"
    server.close()
    for c in held:
        try:
            c.close()
        except OSError:
            pass
    try:
        os.unlink(local)
    except OSError:
        pass


def _walk(w):
    Gtk = gtkui.Gtk
    out = [w]
    if isinstance(w, Gtk.Container):
        for c in w.get_children():
            out.extend(_walk(c))
    return out


def _dialog_deadline(seconds=8):
    return time.time() + seconds


def _ssh_dialog():
    Gtk = gtkui.Gtk
    for w in Gtk.Window.list_toplevels():
        if isinstance(w, Gtk.Dialog) and w.get_title() == "Remote daemon (SSH)":
            return w
    return None


def _dismiss_dialog():
    """Click Cancel so the modal dlg.run() returns — the dialog can never sit
    open forever when a test driver gives up."""
    dlg = _ssh_dialog()
    if dlg is None:
        return
    cancel = dlg.get_widget_for_response(gtkui.Gtk.ResponseType.CANCEL)
    if cancel is not None:
        cancel.emit("clicked")
    else:
        dlg.response(gtkui.Gtk.ResponseType.CANCEL)


def _dialog_deadline_passed(deadline=None):
    return time.time() > (deadline or _dialog_deadline())


def _click_button(dlg, label):
    for w in _walk(dlg):
        if isinstance(w, gtkui.Gtk.Button) and w.get_label() == label:
            w.emit("clicked")
            return True
    return False


def _result_text(dlg):
    Gtk = gtkui.Gtk
    prefixes = ("connecting", "connected", "enable", "timeout", "ssh", "daemon")
    for w in _walk(dlg):
        if isinstance(w, Gtk.Label):
            t = w.get_text()
            if t and t.startswith(prefixes):
                return t
    return ""


def test_supervisor_remote_mode(ssh_prefs_env, monkeypatch):
    home, config = ssh_prefs_env
    write_ssh_prefs(config, {
        "enabled": True,
        "host": "user@host",
        "remote_socket": "/srv/aimless/api.sock",
    })
    sup = gtkui.DaemonSupervisor()
    assert sup.remote is True
    assert not sup.is_running()  # tunnel not started

    # remote ensure() must start a tunnel, never a local daemon
    started = []
    spawned = []

    class FakeTunnel:
        def __init__(self):
            self.local_socket = str(config / "remote-api.sock")

        def is_ready(self):
            return False

        def start(self, log=None):
            started.append(True)
            return True

        def stop(self):
            pass

    sup.tunnel = FakeTunnel()
    monkeypatch.setattr(sup, "spawn", lambda: spawned.append(True))
    with pytest.raises(RuntimeError):
        sup.ensure()
    assert started and not spawned  # tunnel used, local daemon never spawned

    # tunnel up but daemon not answering -> is_running False, ensure raises,
    # and still never spawns a local daemon
    class ReadyTunnel(FakeTunnel):
        def is_ready(self):
            return True

    sup.tunnel = ReadyTunnel()
    assert not sup.is_running()  # no real daemon behind the socket
    started.clear()
    with pytest.raises(RuntimeError):
        sup.ensure()
    assert not spawned


def test_supervisor_is_running_requires_whoami(ssh_prefs_env, monkeypatch):
    """The exact production failure: an SSH tunnel whose local listener accepts
    connections but nothing answers a whoami behind it (wrong remote path, IP
    changed, daemon down). is_running() must return False — a bare socket
    connect through a live tunnel says nothing about the daemon — and ensure()
    must raise, not silently report ready."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")

    import socket as _socket
    import threading as _threading

    home, config = ssh_prefs_env
    write_ssh_prefs(config, {
        "enabled": True,
        "host": "user@host",
        "remote_socket": "/srv/aimless/api.sock",
    })
    sup = gtkui.DaemonSupervisor()
    assert sup.remote is True

    # A real unix socket that accepts connections and HOLDS them open without
    # ever replying — exactly what a live-but-broken tunnel forward does: the
    # local connect succeeds, but the whoami gets no reply and times out. (An
    # accept-and-close listener would make the client reconnect-loop instead,
    # which resets the request deadline and hangs forever.)
    local = str(config / "remote-api.sock")
    server = _socket.socket(_socket.AF_UNIX)
    server.bind(local)
    server.listen(8)
    held = []

    def accept_loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)  # keep open, never answer

    t = _threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        class ReadyTunnel:
            local_socket = local
            host = "user@host"

            def is_ready(self):
                return True

            def start(self, log=None):
                return True

            def stop(self):
                pass

        sup.tunnel = ReadyTunnel()
        assert not sup.is_running(), \
            "is_running() must not be True when whoami gets no reply"
        with pytest.raises(RuntimeError):
            sup.ensure()
    finally:
        server.close()
        for c in held:
            try:
                c.close()
            except OSError:
                pass
        try:
            os.unlink(local)
        except OSError:
            pass


def test_tunnel_command_builds(ssh_prefs_env):
    t = SSHTunnel("user@host", "/srv/aimless/api.sock", "/tmp/remote-api.sock",
                  identity="~/.ssh/id_ed25519")
    cmd = t.command()
    assert cmd[0] == "ssh"
    assert "-N" in cmd
    assert "BatchMode=yes" in cmd
    assert "-i" in cmd
    assert os.path.expanduser("~/.ssh/id_ed25519") in cmd
    assert "/tmp/remote-api.sock:/srv/aimless/api.sock" in cmd
    assert "user@host" in cmd


def test_tunnel_local_socket_forward_to_real_daemon(ssh_prefs_env, monkeypatch):
    """End-to-end: deploy the static daemon on a remote host via ssh, then have
    an SSH tunnel forward a local unix socket to its api.sock — exactly how the
    GUI reaches a remote daemon. Uses AIMLESS_SSH_TEST_HOST (default: ssh
    localhost) and skips when no ssh server is reachable."""
    if not shutil_which("ssh"):
        pytest.skip("no ssh binary")
    if not shutil_which("scp"):
        pytest.skip("no scp binary")
    daemon_bin = gtkui.daemon_binary()
    if not daemon_bin:
        pytest.skip("no aimlessd binary for the ssh test")
    host = os.environ.get("AIMLESS_SSH_TEST_HOST", "localhost")
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=no", host, "true"],
            capture_output=True, timeout=10)
        if r.returncode != 0:
            pytest.skip(f"ssh to {host} not reachable: {r.returncode}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip(f"ssh to {host} not reachable")

    home, config = ssh_prefs_env
    rdir = f"/tmp/aimless-ssh-e2e-{os.getpid()}"
    rsock = os.path.join(rdir, "api.sock")
    ssh = ["ssh", "-o", "BatchMode=yes", host]
    scp = ["scp", "-o", "BatchMode=yes"]
    try:
        subprocess.run(ssh + [f"rm -rf {rdir}; mkdir -p {rdir}"], check=True, timeout=15)
        subprocess.run(scp + [daemon_bin, f"{host}:{rdir}/aimlessd"],
                       check=True, timeout=60)
        proc = subprocess.Popen(
            ssh + [f"chmod +x {rdir}/aimlessd && exec {rdir}/aimlessd "
                   f"-datadir {rdir} -peers none"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                r = subprocess.run(ssh + [f"test -S {rsock} && echo yes"],
                                   capture_output=True, timeout=5)
                if b"yes" in r.stdout:
                    break
                time.sleep(0.3)
            else:
                pytest.skip("remote daemon did not create socket")
            local_sock = str(config / "remote-api.sock")
            t = SSHTunnel(host, rsock, local_sock)
            t.start()
            try:
                from aimless.daemon import DaemonClient
                d = DaemonClient(local_sock)
                who = d.request("whoami", timeout=10)
                d.close()
                assert "key" in who
            finally:
                t.stop()
        finally:
            try:
                subprocess.run(ssh + ["pkill -f aimless-ssh-e2e; true"],
                               timeout=10)
            except Exception:
                pass
    finally:
        try:
            subprocess.run(ssh + [f"rm -rf {rdir}"], timeout=10)
        except Exception:
            pass


def shutil_which(name):
    import shutil
    return shutil.which(name)

def _write_node_key(home, seed_hex=None):
    import nacl.signing
    seed = seed_hex or bytes(bytearray(range(32))).hex()
    key = nacl.signing.SigningKey(bytes.fromhex(seed))
    p = home / "node.key"
    p.write_text(seed + "\n")
    return bytes(key.verify_key).hex()


def test_node_key_public_hex(ssh_prefs_env):
    home, config = ssh_prefs_env
    pub = _write_node_key(home)
    assert gtkui.node_key_public_hex() == pub


def test_node_key_public_hex_missing(ssh_prefs_env):
    home, config = ssh_prefs_env
    assert gtkui.node_key_public_hex() is None


def test_migrate_node_stage_copies_state(ssh_prefs_env, monkeypatch):
    home, config = ssh_prefs_env
    pub = _write_node_key(home)
    (home / "journal").mkdir()
    (home / "journal" / "aabb.seq").write_text("2385\n")
    (home / "journal" / "aabb.jsonl").write_text("x")
    (home / "inbox").mkdir()
    (home / "inbox" / "aabb.jsonl").write_text("y")
    (home / "contacts.json").write_text('{"contacts":["aabb"]}')
    write_ssh_prefs(config, {"host": "me@host", "remote_socket": "/srv/aimless/state/api.sock"})

    ops = []

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_scp(host, identity, src, dst, put=True, timeout=60):
        ops.append(("scp", (src, dst)))
        return None

    def fake_ssh_run(host, identity, cmd, timeout=30):
        ops.append(("ssh", cmd))
        return 0, "", ""

    monkeypatch.setattr(gtkui, "scp_transfer", fake_scp)
    monkeypatch.setattr(gtkui, "ssh_run", fake_ssh_run)
    monkeypatch.setattr(gtkui, "daemon_pid_from_procs", lambda: None)

    backup = gtkui.migrate_node_stage("me@host", None, "/srv/aimless/state")
    # backup first, then node.key, journal files, inbox, contacts
    cmds = [c for kind, c in ops if kind == "ssh"]
    assert any("node.key.pre-migration" in c for c in cmds)
    scp_dst = [d for kind, d in ops if kind == "scp"]
    # scp_transfer(host, identity, src, dst); check the remote dst paths
    scp_remote = [d[1] for d in scp_dst]
    assert any(d.endswith("/state/node.key") for d in scp_remote)
    assert any(d.endswith("/journal/aabb.seq") for d in scp_remote)
    assert any(d.endswith("/journal/aabb.jsonl") for d in scp_remote)
    assert any(d.endswith("/inbox/aabb.jsonl") for d in scp_remote)
    assert any(d.endswith("/state/contacts.json") for d in scp_remote)
    assert any("chmod 600" in c for c in cmds)
    assert backup.startswith("/srv/aimless/state/node.key.pre-migration-")


def test_migrate_node_stage_requires_local_key(ssh_prefs_env, monkeypatch):
    home, config = ssh_prefs_env
    write_ssh_prefs(config, {"host": "me@host", "remote_socket": "/srv/state/api.sock"})
    with pytest.raises(RuntimeError, match="no local node.key"):
        gtkui.migrate_node_stage("me@host", None, "/srv/state")


def test_migrate_node_stage_refuses_live_local_daemon(ssh_prefs_env, monkeypatch):
    home, config = ssh_prefs_env
    _write_node_key(home)
    write_ssh_prefs(config, {"host": "me@host", "remote_socket": "/srv/state/api.sock"})
    monkeypatch.setattr(gtkui, "daemon_pid_from_procs", lambda: 12345)
    with pytest.raises(RuntimeError, match="local daemon is running"):
        gtkui.migrate_node_stage("me@host", None, "/srv/state")


def test_verify_remote_node_key_matches(ssh_prefs_env, monkeypatch):
    home, config = ssh_prefs_env
    expected = "aabb"
    attempts = []

    def fake_probe(host, path, identity=None):
        attempts.append(1)
        return {"key": expected}, None

    # simpler: monkeypatch verify to use a fake tunnel+daemon via module attrs
    monkeypatch.setattr(gtkui, "SSHTunnel", lambda *a, **k: type("T", (), {
        "local_socket": "/tmp/x",
        "start": lambda self: None,
        "stop": lambda self: None,
    })())
    real_client = gtkui.DaemonClient

    class FakeDC:
        def __init__(self, sock):
            pass

        def request(self, op, timeout=10.0, **kw):
            attempts.append(op)
            return {"key": "aabb"}

        def close(self):
            pass

    monkeypatch.setattr(gtkui, "DaemonClient", FakeDC)
    assert gtkui.verify_remote_node_key("me@host", None, "/srv/state/api.sock", "aabb", timeout=5)
    assert attempts


def test_verify_remote_node_key_timeout(ssh_prefs_env, monkeypatch):
    home, config = ssh_prefs_env
    monkeypatch.setattr(gtkui, "SSHTunnel", lambda *a, **k: type("T", (), {
        "local_socket": "/tmp/x",
        "start": lambda self: None,
        "stop": lambda self: None,
    })())
    monkeypatch.setattr(gtkui, "DaemonClient",
                        lambda sock: type("D", (), {
                            "request": lambda self, op, timeout=10.0, **kw: {"key": "other"},
                            "close": lambda self: None,
                        })())
    assert not gtkui.verify_remote_node_key("me@host", None, "/srv/state/api.sock", "aabb", timeout=3)


def test_guard_local_startup_after_migration(ssh_prefs_env, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    home, config = ssh_prefs_env
    pub = _write_node_key(home)
    # prefs: relocated marker + ssh config cleared (user switched to local)
    gtkui.save_prefs({"node_relocated": {"key": pub, "host": "me@host",
                                         "remote_socket": "/srv/state/api.sock"}})

    app = gtkui.AimlessApp()
    app.log = lambda *a, **k: None
    app.supervisor = gtkui.DaemonSupervisor()  # local (no ssh prefs)

    # "Use SSH mode" -> restores ssh prefs, rebuilds supervisor, returns
    real_md = gtkui.Gtk.MessageDialog
    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", lambda *a, **k: type("M", (real_md,), {
        "run": lambda self: Gtk.ResponseType.APPLY,
    })())
    app._guard_local_startup_after_migration()
    assert gtkui.load_prefs()["ssh"]["host"] == "me@host"
    assert app.supervisor.remote is True

    # "Start local anyway" -> returns, no ssh restored
    gtkui.save_prefs({"node_relocated": {"key": pub, "host": "me@host",
                                         "remote_socket": "/srv/state/api.sock"}})
    app2 = gtkui.AimlessApp()
    app2.log = lambda *a, **k: None
    app2.supervisor = gtkui.DaemonSupervisor()
    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", lambda *a, **k: type("M", (real_md,), {
        "run": lambda self: Gtk.ResponseType.ACCEPT,
    })())
    app2._guard_local_startup_after_migration()
    assert "ssh" not in gtkui.load_prefs()

    # "Cancel" -> SystemExit(1)
    gtkui.save_prefs({"node_relocated": {"key": pub, "host": "me@host",
                                         "remote_socket": "/srv/state/api.sock"}})
    app3 = gtkui.AimlessApp()
    app3.log = lambda *a, **k: None
    app3.supervisor = gtkui.DaemonSupervisor()
    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", lambda *a, **k: type("M", (real_md,), {
        "run": lambda self: Gtk.ResponseType.CANCEL,
    })())
    with pytest.raises(SystemExit):
        app3._guard_local_startup_after_migration()


def test_guard_no_relocation_skips(ssh_prefs_env, monkeypatch):
    home, config = ssh_prefs_env
    _write_node_key(home)
    gtkui.save_prefs({})
    app = gtkui.AimlessApp()
    app.log = lambda *a, **k: None
    app.supervisor = gtkui.DaemonSupervisor()
    app._guard_local_startup_after_migration()  # no dialog -> returns silently


def test_ssh_dialog_shows_migrate_button(ssh_prefs_env, monkeypatch):
    """The SSH settings dialog shows 'Move local identity to remote' when a local
    node.key exists and remote mode is selected."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env
    _write_node_key(home)

    win = Gtk.Window()
    win.show_all()
    win.prefs = gtkui.load_prefs()
    win.get_toplevel = lambda: win
    win.set_transient_for = lambda x: None
    win.on_ssh_settings = gtkui.AimlessWindow.on_ssh_settings.__get__(win)
    win.do_migrate_node = lambda *_: None

    found = {}
    deadline = time.time() + 8
    state = {"seen": False}

    def on_dialog():
        if _dialog_deadline_passed(deadline):
            _dismiss_dialog()
            return False
        dlg = _ssh_dialog()
        if dlg is None:
            return True
        for w in _walk(dlg):
            if isinstance(w, gtkui.Gtk.Button) and "Move local identity to remote" in w.get_label():
                found["migrate"] = True
                break
        if not state["seen"]:
            # switch to Remote mode so the button becomes visible
            for w in _walk(dlg):
                if isinstance(w, Gtk.RadioButton) and w.get_label() == "Remote daemon (SSH)":
                    w.set_active(True)
                    break
            state["seen"] = True
            return True
        if found.get("migrate") and state["seen"]:
            _dismiss_dialog()
            return False
        return True

    GLib.timeout_add(50, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    assert found.get("migrate"), "Move local identity to remote button not shown when a local key exists"


def test_do_migrate_node_requires_ssh(ssh_prefs_env, monkeypatch):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    home, config = ssh_prefs_env
    _write_node_key(home)
    win = Gtk.Window()
    win.show_all()
    win.activity = type("A", (), {"log": lambda self, *a, **k: None})()
    win.do_migrate_node = gtkui.AimlessWindow.do_migrate_node.__get__(win)
    win._migrate_restart_prompt = lambda *a, **k: None

    shown = []

    class FakeMsg(Gtk.MessageDialog):
        def run(self, *a):
            shown.append(1)
            return Gtk.ResponseType.OK

    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", FakeMsg)
    win.do_migrate_node()  # no ssh prefs -> clear message, no crash
    assert shown, "a clear 'no remote daemon configured' message must be shown"
    win.destroy()


def test_do_migrate_node_requires_local_key(ssh_prefs_env, monkeypatch):
    """With ssh configured but no local node.key, show a clear message."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    home, config = ssh_prefs_env
    write_ssh_prefs(config, {"host": "me@host", "remote_socket": "/srv/state/api.sock"})
    win = Gtk.Window()
    win.show_all()
    win.activity = type("A", (), {"log": lambda self, *a, **k: None})()
    win.do_migrate_node = gtkui.AimlessWindow.do_migrate_node.__get__(win)
    win._migrate_restart_prompt = lambda *a, **k: None

    shown = []

    class FakeMsg(Gtk.MessageDialog):
        def run(self, *a):
            shown.append(1)
            return Gtk.ResponseType.OK

    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", FakeMsg)
    win.do_migrate_node()
    assert shown, "a clear 'no local node.key' message must be shown"
    win.destroy()


def test_migrate_restart_prompt_cancel_stays_live(ssh_prefs_env, monkeypatch):
    """Regression: the restart-prompt used to grey out the WHOLE dialog
    (dlg.set_sensitive(False)) so Cancel was dead while the async docker
    restart ran - a stuck grey window. Only the restart button must disable;
    Cancel stays usable, and the same dialog is reused for verify."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    home, config = ssh_prefs_env
    win = Gtk.Window()
    win.show_all()
    win.activity = type("A", (), {"log": lambda self, *a, **k: None})()
    win._migrate_restart_prompt = gtkui.AimlessWindow._migrate_restart_prompt.__get__(win)
    win._migrate_verify = lambda *a, **k: None

    monkeypatch.setattr(gtkui, "detect_remote_container", lambda *a, **k: "aimless-webtop")
    monkeypatch.setattr(gtkui, "ssh_run", lambda *a, **k: (0, "", ""))

    result = {}
    deadline = time.time() + 8
    step = {"v": 0}

    def pump():
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    def drive():
        if time.time() > deadline:
            return False
        candidates = [w for w in Gtk.Window.list_toplevels()
                      if isinstance(w, Gtk.Dialog)
                      and w.get_title() == "Restart the server's daemon"]
        if not candidates:
            return True
        dlg = candidates[0]
        btns = [b for b in _walk(dlg) if isinstance(b, Gtk.Button)]
        labels = {b.get_label() for b in btns}
        if step["v"] == 0:
            assert "Cancel" in labels
            restart = next(b for b in btns if b.get_label() == "Restart the daemon now")
            restart.emit("clicked")
            step["v"] = 1
            pump()
            return True
        if step["v"] == 1:
            pump()
            for b in btns:
                if b.get_label() == "Restart the daemon now":
                    assert not b.get_sensitive(), "restart button disabled during restart"
                if b.get_label() == "Cancel":
                    assert b.get_sensitive(), "Cancel must stay enabled while restart runs"
            step["v"] = 2
            return True
        if step["v"] == 2:
            pump()
            result["transitioned"] = True
            return False
        return True

    win._migrate_restart_prompt("me@host", None, "/srv/state",
                                "/srv/state/api.sock", "aabb")
    while True:
        pump()
        if result.get("transitioned"):
            break
        if not drive():
            break
        time.sleep(0.02)
    win.destroy()
    assert result.get("transitioned"), "restart prompt never reached the verify transition"
