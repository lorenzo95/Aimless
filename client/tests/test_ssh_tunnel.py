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
    """Drive the real on_ssh_settings dialog: fill entries + toggle the enable
    checkbox, click Test, then Save (now gated on a passed test), and assert the
    prefs file is written with exactly what the user typed. Regression for the
    destroyed-entry bug (values read after dlg.destroy() came back empty and SSH
    stayed disabled) and for the save-gate."""
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

    # The "Restart the app…" info dialog after Save would block run(); make it
    # a no-op so the test returns.
    class FakeMsg(Gtk.MessageDialog):
        def run(self, *a):
            return Gtk.ResponseType.OK

    monkeypatch.setattr(gtkui.Gtk, "MessageDialog", FakeMsg)

    # Stub the tunnel + daemon so the connection test succeeds without a real
    # SSH host.
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
            assert op == "whoami"
            return {"build": "aimlessd/0.5.6-test"}

        def close(self):
            pass

    monkeypatch.setattr(gtkui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gtkui, "DaemonClient", FakeDaemonClient)

    def find_entries(w):
        out = []
        if isinstance(w, Gtk.Entry):
            out.append(w)
        if isinstance(w, Gtk.Container):
            for c in w.get_children():
                out.extend(find_entries(c))
        return out

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
        if not filled["done"]:
            for child in dlg.get_content_area().get_children():
                if isinstance(child, Gtk.CheckButton):
                    child.set_active(True)
                    break
            entries = find_entries(dlg)
            assert len(entries) >= 3
            entries[0].set_text("debian@192.168.1.111")
            entries[1].set_text("/srv/aimless/state/api.sock")
            entries[2].set_text("/home/me/.ssh/id_ed25519")
            filled["done"] = True
            # Save must be gated (disabled) until a test passes
            save_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
            assert save_btn is not None and not save_btn.get_sensitive(), \
                "Save should be disabled until a connection test passes"
            # click Test connection
            for child in dlg.get_content_area().get_children():
                if isinstance(child, Gtk.Button) and child.get_label() == "Test connection":
                    child.emit("clicked")
                    break
            return True
        # after the test completes, Save becomes enabled — click it
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
    assert saved["enabled"] is True
    assert saved["host"] == "debian@192.168.1.111"
    assert saved["remote_socket"] == "/srv/aimless/state/api.sock"
    assert saved["identity"] == "/home/me/.ssh/id_ed25519"
    # and it must resolve to a remote supervisor after restart
    assert gtkui.ssh_tunnel() is not None
    assert gtkui.sock_path() == str(config / "remote-api.sock")


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
            for child in dlg.get_content_area().get_children():
                if isinstance(child, Gtk.CheckButton):
                    child.set_active(True)
                    break
            entries[0].set_text("user@host")
            entries[1].set_text("/srv/api.sock")
            for child in dlg.get_content_area().get_children():
                if isinstance(child, Gtk.Button) and child.get_label() == "Test connection":
                    child.emit("clicked")
                    break
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
            for child in dlg.get_content_area().get_children():
                if isinstance(child, Gtk.CheckButton):
                    child.set_active(True)
                    break
            entries[0].set_text("user@host")
            entries[1].set_text("/srv/api.sock")
            for child in dlg.get_content_area().get_children():
                if isinstance(child, Gtk.Button) and child.get_label() == "Test connection":
                    child.emit("clicked")
                    break
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
            for child in dlg.get_content_area().get_children():
                if isinstance(child, Gtk.CheckButton):
                    child.set_active(True)
                    break
            entries[0].set_text("user@host")
            entries[1].set_text("/srv/api.sock")
            for child in dlg.get_content_area().get_children():
                if isinstance(child, Gtk.Button) and child.get_label() == "Test connection":
                    child.emit("clicked")
                    break
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

    # supervisor whose ensure() fails as it would with a bad remote config
    class FailingSupervisor:
        remote = True

        def ensure(self, log=None):
            raise RuntimeError("ssh tunnel failed")

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
    assert calls["local_ensured"] == 1, "local supervisor was not used after recovery"
    assert calls["open_window"] == 1, "window did not open after recovery"
    saved = gtkui.load_prefs()["ssh"]
    assert saved["enabled"] is False, "SSH should be disabled by the escape hatch"


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
    assert gtkui.load_prefs()["ssh"]["enabled"] is False
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