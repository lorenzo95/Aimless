import time

import pytest

from aimless import crypto, gtkui
from aimless.daemon import DaemonClient, Client
from aimless import protocol
from test_e2e import _build_daemon, _free_port, _wait_socket
from test_gtk import _pump


@pytest.fixture
def daemon_up_client_later(tmp_path, monkeypatch):
    """A's daemon is running with NO client connected; B sends messages that the
    daemon receives (recv events broadcast to nobody = lost for the later client).
    Then the GUI starts and must catch up via the unread sweep."""
    import subprocess
    binpath = _build_daemon(str(tmp_path))
    port = _free_port()
    dir_a = str(tmp_path / "nodeA")
    dir_b = str(tmp_path / "nodeB")
    sock_a = str(tmp_path / "a.sock")
    sock_b = str(tmp_path / "b.sock")

    procs = []
    procs.append(subprocess.Popen(
        [binpath, "-datadir", dir_a, "-api", sock_a, "-listen", f"tcp://127.0.0.1:{port}",
         "-peers", "none", "-retry", "300ms", "-probe", "300ms"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    _wait_socket(sock_a)
    time.sleep(0.3)
    a_dc = DaemonClient(sock_a)
    a_node = a_dc.request("whoami")["key"]
    a_dc.close()

    procs.append(subprocess.Popen(
        [binpath, "-datadir", dir_b, "-api", sock_b, "-peers", f"tcp://127.0.0.1:{port}",
         "-retry", "300ms", "-probe", "300ms"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    _wait_socket(sock_b)
    time.sleep(0.3)

    b_ident = crypto.new_identity()
    b_dc = DaemonClient(sock_b)
    b_dc.request("watch", to=a_node)
    client_b = Client(b_dc, b_ident, "Bob")
    a_ident = crypto.new_identity()
    a_pub = bytes(a_ident.verify_key).hex()
    for i in range(3):
        client_b.send(a_pub, a_node, f"missed {i}", int(time.time() * 1000))

    # Let A's daemon (client-less) receive the 3 messages. broadcast() reaches nobody.
    time.sleep(6)

    b_node = client_b.node_key()

    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setenv("AIMLESS_SOCK", sock_a)
    monkeypatch.setattr(gtkui, "CONFIG_DIR", str(config))
    monkeypatch.setattr(gtkui, "APP_PID_FILE", str(config / "app.pid"))
    monkeypatch.setattr(gtkui, "AIMLESSD_PID_FILE", str(config / "aimlessd.pid"))
    crypto.save_identity(str(home / "identity.json"), a_ident, "testpass")
    crypto.Cache(str(home / "cache.json.enc"), "testpass")
    protocol.save_contacts(str(home / "client-contacts.json"), {
        "_self": {"screen": "Alice", "pubkey": a_pub},
        "bob": {"pubkey": bytes(b_ident.verify_key).hex(), "node": b_node, "screen": "Bob"},
    })

    monkeypatch.setattr(gtkui, "ask_passphrase", lambda parent: "testpass")
    monkeypatch.setattr(gtkui.AimlessWindow, "poll_status", lambda self: True)

    session = gtkui.Session("testpass")
    supervisor = gtkui.DaemonSupervisor()
    win = gtkui.AimlessWindow(session, supervisor)
    win.show_all()

    yield {"win": win, "session": session, "a_node": a_node, "b_node": b_node,
           "client_b": client_b, "a_pub": a_pub, "sock_a": sock_a}

    win.destroy()
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except Exception:
            pass


def test_catchup_counts_messages_received_while_client_was_away(daemon_up_client_later):
    app = daemon_up_client_later
    win = app["win"]
    b_node = app["b_node"]

    def unread_val():
        t = win.messages.threads.get(b_node)
        return t and t["unread"] or 0
    pump = lambda: _pump(win, lambda: unread_val() >= 3, timeout=30)
    ok = pump()
    print(f"\nUNREAD after catch-up sweep = {unread_val()}")
    assert ok, f"catch-up never counted the 3 missed messages (unread={unread_val()})"

    # A fresh online message must stack on top: 3 + 1 = 4.
    app["client_b"].send(app["a_pub"], app["a_node"], "now online", int(time.time() * 1000))
    _pump(win, lambda: unread_val() >= 4, timeout=30)
    print(f"UNREAD after 1 online message = {unread_val()}")
    assert unread_val() == 4, f"expected 4, got {unread_val()}"

    # Opening the thread clears the badge.
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    assert unread_val() == 0, "opening the thread must clear unread"