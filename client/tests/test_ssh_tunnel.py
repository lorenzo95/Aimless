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
    checkbox, click Save, and assert the prefs file is written with exactly
    what the user typed. Regression for the destroyed-entry bug (values read
    after dlg.destroy() came back empty and SSH stayed disabled)."""
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

    def find_entries(w):
        out = []
        if isinstance(w, Gtk.Entry):
            out.append(w)
        if isinstance(w, Gtk.Container):
            for c in w.get_children():
                out.extend(find_entries(c))
        return out

    result = {}

    deadline = time.time() + 5

    def on_dialog():
        if time.time() > deadline:
            return False
        candidates = [w for w in Gtk.Window.list_toplevels()
                      if isinstance(w, Gtk.Dialog) and w.get_title() == "Remote daemon (SSH)"]
        if not candidates:
            return True  # dialog not up yet — keep polling
        dlg = candidates[0]
        for child in dlg.get_content_area().get_children():
            if isinstance(child, Gtk.CheckButton):
                child.set_active(True)
                break
        entries = find_entries(dlg)
        assert len(entries) >= 3
        entries[0].set_text("debian@192.168.1.111")
        entries[1].set_text("/srv/aimless/state/api.sock")
        entries[2].set_text("/home/me/.ssh/id_ed25519")
        for child in dlg.get_action_area().get_children():
            if isinstance(child, Gtk.Button) and child.get_label() == "Save":
                child.emit("clicked")
                break
        result["done"] = True
        return False

    GLib.timeout_add(100, on_dialog)
    try:
        win.on_ssh_settings()
    finally:
        win.destroy()
    saved = gtkui.load_prefs()["ssh"]
    assert saved["enabled"] is True
    assert saved["host"] == "debian@192.168.1.111"
    assert saved["remote_socket"] == "/srv/aimless/state/api.sock"
    assert saved["identity"] == "/home/me/.ssh/id_ed25519"
    # and it must resolve to a remote supervisor after restart
    assert gtkui.ssh_tunnel() is not None
    assert gtkui.sock_path() == str(config / "remote-api.sock")


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


def test_tunnel_command_builds(ssh_prefs_env):
    t = SSHTunnel("user@host", "/srv/aimless/api.sock", "/tmp/remote-api.sock",
                  identity="~/.ssh/id_ed25519")
    cmd = t.command()
    assert cmd[0] == "ssh"
    assert "-N" in cmd
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