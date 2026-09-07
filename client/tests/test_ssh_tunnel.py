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