import subprocess
import time

import pytest

from aimless.daemon import DaemonClient, DaemonError
from aimless import gtkui

import test_e2e
from test_e2e import two_nodes  # noqa: F401


def test_daemon_client_reconnects_after_daemon_restart(tmp_path):
    binp = test_e2e._build_daemon(str(tmp_path))
    sock = str(tmp_path / "api.sock")
    datadir = str(tmp_path / "data")

    args = [binp, "-datadir", datadir, "-api", sock, "-peers", "none", "-retry", "300ms"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    test_e2e._wait_socket(sock, timeout=30)

    client = DaemonClient(sock)
    who = client.request("whoami", timeout=5)
    assert who["op"] == "whoami"

    proc.terminate()
    proc.wait(timeout=10)
    time.sleep(0.5)

    proc2 = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        who2 = client.request("whoami", timeout=15)
        assert who2["op"] == "whoami"
        assert who2["address"] == who["address"]
    finally:
        client.close()
        proc2.terminate()
        proc2.wait(timeout=10)


def test_request_fails_cleanly_when_daemon_gone(tmp_path):
    binp = test_e2e._build_daemon(str(tmp_path))
    sock = str(tmp_path / "api.sock")
    datadir = str(tmp_path / "data")
    args = [binp, "-datadir", datadir, "-api", sock, "-peers", "none"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    test_e2e._wait_socket(sock, timeout=30)

    client = DaemonClient(sock)
    client.request("whoami", timeout=5)
    proc.terminate()
    proc.wait(timeout=10)

    with pytest.raises(DaemonError):
        client.request("whoami", timeout=3)
    client.close()
