import time

import pytest

tkinter = pytest.importorskip("tkinter")
import tkinter as tk

import test_e2e
from test_e2e import two_nodes  # noqa: F401

from aimless import crypto, protocol
from aimless import gui as gui_module
from aimless.daemon import Client, DaemonClient


@pytest.fixture
def gui_env(tmp_path, monkeypatch, two_nodes):
    sock_a, sock_b = two_nodes
    home = tmp_path / "alice-home"
    home.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setenv("AIMLESS_SOCK", sock_a)
    monkeypatch.setattr(gui_module.simpledialog, "askstring", lambda *a, **k: "testpass")

    identity = crypto.new_identity()
    crypto.save_identity(str(home / "identity.json"), identity, "testpass")
    crypto.Cache(str(home / "cache.json.enc"), "testpass")

    bob_identity = crypto.new_identity()
    bob_daemon = DaemonClient(sock_b)
    b_node = bob_daemon.request("whoami")["key"]
    contacts = {
        "_self": {"screen": "Alice"},
        "bob": {"pubkey": bytes(bob_identity.verify_key).hex(), "node": b_node, "screen": "Bob"},
    }
    protocol.save_contacts(str(home / "client-contacts.json"), contacts)
    return {
        "home": home,
        "sock_a": sock_a,
        "sock_b": sock_b,
        "bob_identity": bob_identity,
        "bob_daemon": bob_daemon,
    }


def _pump(root, condition, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            root.update()
        except Exception:
            return False
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_gui_buddy_list_and_im_roundtrip(tmp_path, gui_env):
    bob = Client(gui_env["bob_daemon"], gui_env["bob_identity"], "Bob")

    root = tk.Tk()
    root.withdraw()
    app = gui_module.App(root)
    root.deiconify()
    try:
        assert _pump(root, lambda: len(app.contacts) == 1), "contacts not loaded"

        bob_contact = app.contacts["bob"]
        a_node = app.client.node_key()
        bob.add_contact(a_node)

        assert _pump(
            root,
            lambda: any(p["key"] == bob_contact["node"] and p["online"] for p in app.safe_presence()),
        ), "bob never showed online in GUI presence"

        def row_shows_online_bob():
            if app.buddylist.listbox.size() != 1:
                return False
            text = app.buddylist.listbox.get(0)
            return "Bob" in text and "offline" not in text
        assert _pump(root, row_shows_online_bob), "buddy list row never showed Bob online"

        app.open_im("bob", bob_contact)
        assert "bob" in app.im_windows
        im = app.im_windows["bob"]

        def im_layout_ok():
            root.update_idletasks()
            if not im.entry.winfo_ismapped():
                return False
            win_bottom = im.win.winfo_rooty() + im.win.winfo_height()
            entry_bottom = im.entry.winfo_rooty() + im.entry.winfo_height()
            return entry_bottom <= win_bottom and im.entry.winfo_height() > 5
        assert _pump(root, im_layout_ok), "IM input box not visible"

        im.win.geometry("480x260")
        root.update_idletasks()
        assert _pump(root, im_layout_ok), "IM input box clipped in a short window"

        im.entry.insert(0, "hello from the GUI")
        im._send()

        def bob_has_msg():
            hist = bob.history(a_node, 0)
            if not hist.get("msgs"):
                return False
            opened = protocol.open_message(gui_env["bob_identity"], hist["msgs"][-1]["payload"])
            return opened["text"] == "hello from the GUI"
        assert _pump(root, bob_has_msg), "bob never received the GUI message"

        ts = int(time.time() * 1000)
        bob.send(app.client.pubkey_hex, a_node, "reply via daemon", ts)

        def gui_showing_reply():
            return "reply via daemon" in im.log.get("1.0", "end")
        assert _pump(root, gui_showing_reply), "incoming message never rendered in IM window"

        msgs = app.cache.msgs(bob_contact["node"])
        texts = [m["text"] for m in msgs]
        assert "hello from the GUI" in texts
        assert "reply via daemon" in texts

        app.set_away("brb — lunch")

        def away_visible():
            for p in bob.presence():
                if p["key"] == a_node and p.get("status_payload"):
                    st = protocol.open_status(gui_env["bob_identity"], p["status_payload"])
                    return st.get("away") == "brb — lunch"
            return False
        assert _pump(root, away_visible), "away status never reached bob"
    finally:
        root.destroy()
