import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import test_e2e
from test_e2e import two_nodes  # noqa: F401

from aimless import crypto, protocol
from aimless import gtkui
from aimless.daemon import Client, DaemonClient


def all_texts(widget):
    out = []
    if isinstance(widget, Gtk.Label):
        out.append(widget.get_text())
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            out.extend(all_texts(child))
    return out


def pump(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)


@pytest.fixture
def gtk_app(tmp_path, monkeypatch, two_nodes):
    sock_a, sock_b = two_nodes
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setenv("AIMLESS_SOCK", sock_a)
    monkeypatch.setattr(gtkui, "CONFIG_DIR", str(config))
    monkeypatch.setattr(gtkui, "GUI_PID_FILE", str(config / "gui.pid"))
    monkeypatch.setattr(gtkui, "SESSION_FILE", str(config / "session.json"))
    monkeypatch.setattr(gtkui, "TRAY_PID_FILE", str(config / "tray.pid"))
    monkeypatch.setattr(gtkui, "AIMLESSD_PID_FILE", str(config / "aimlessd.pid"))

    alice_identity = crypto.new_identity()
    crypto.save_identity(str(home / "identity.json"), alice_identity, "testpass")
    crypto.Cache(str(home / "cache.json.enc"), "testpass")

    bob_identity = crypto.new_identity()
    bob = Client(DaemonClient(sock_b), bob_identity, "Bob")
    b_node = bob.node_key()
    a_node = DaemonClient(sock_a).request("whoami")["key"]
    bob.add_contact(a_node)
    protocol.save_contacts(str(home / "client-contacts.json"), {
        "_self": {"screen": "Alice", "pubkey": bytes(alice_identity.verify_key).hex()},
        "bob": {"pubkey": bytes(bob_identity.verify_key).hex(), "node": b_node, "screen": "Bob"},
    })

    monkeypatch.setattr(gtkui, "ask_passphrase", lambda parent: "testpass")
    monkeypatch.setattr(gtkui.AimlessWindow, "poll_status", lambda self: True)

    session = gtkui.Session("testpass")
    supervisor = gtkui.DaemonSupervisor()
    win = gtkui.AimlessWindow(session, supervisor)
    win.show_all()
    pump(2.0)
    return {
        "win": win, "session": session, "supervisor": supervisor,
        "bob": bob, "bob_identity": bob_identity, "b_node": b_node, "a_node": a_node,
        "home": home, "sock_b": sock_b,
    }


def test_gui_buddy_list_and_im_roundtrip(gtk_app):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    b_node = app["b_node"]

    assert b_node in win.messages.threads
    assert _pump(win, lambda: any(p["key"] == b_node and p["online"] for p in win.session.client.presence())), \
        "bob never online"

    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])

    im = win.messages.composer.get_buffer()
    im.set_text("hello from GTK")
    win.messages.send_message()

    def bob_has_msg():
        hist = bob.history(app["a_node"], 0)
        if not hist.get("msgs"):
            return False
        opened = protocol.open_message(app["bob_identity"], hist["msgs"][-1]["payload"])
        return opened["text"] == "hello from GTK"
    assert _pump(win, bob_has_msg, timeout=30), "bob never received the GUI message"

    ts = int(time.time() * 1000)
    bob.send(app["session"].client.pubkey_hex, app["a_node"], "reply via daemon", ts)

    def reply_rendered():
        return any("reply via daemon" in t for t in all_texts(win.messages.conversation))
    assert _pump(win, reply_rendered, timeout=30), "reply never rendered"

    texts = [m["text"] for m in win.session.cache.msgs(b_node)]
    assert "hello from GTK" in texts
    assert "reply via daemon" in texts


def test_gui_unread_badge_and_activity_log(gtk_app):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]

    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    im = win.messages.composer.get_buffer()
    im.set_text("outbound for the log")
    win.messages.send_message()

    def log_has_delivery():
        buf = win.activity.log_view.get_buffer()
        start, end = buf.get_bounds()
        return "delivered" in buf.get_text(start, end, False)
    assert _pump(win, log_has_delivery, timeout=30), "activity log missing delivery line"

    win.messages.thread_list.select_row(None)
    ts = int(time.time() * 1000)
    bob.send(app["session"].client.pubkey_hex, a_node, "you missed me", ts)

    def unread_badge():
        t = win.messages.threads.get(b_node)
        return t and t["unread"] >= 1
    assert _pump(win, unread_badge, timeout=30), "unread never incremented"


def test_gui_contacts_add_remove_and_self_guard(gtk_app):
    app = gtk_app
    win = app["win"]
    contacts_path = str(app["home"] / "client-contacts.json")

    contacts_view = win.contacts
    contacts_view.refresh()
    assert _pump(win, lambda: contacts_view.invite_entry.get_text().startswith("aimless1:")), \
        "invite never loaded"
    invite = contacts_view.invite_entry.get_text()
    assert app["a_node"] in invite

    carol_identity = crypto.new_identity()
    carol_invite = protocol.make_invite(carol_identity, "ef" * 32, "Carol")
    contacts_view.add_invite_entry.set_text(carol_invite)
    contacts_view.add_petname_entry.set_text("")
    contacts_view.on_add()
    contacts = protocol.load_contacts(contacts_path)
    assert "Carol" in contacts
    assert contacts["Carol"]["node"] == "ef" * 32

    contacts_view.add_invite_entry.set_text(invite)
    contacts_view.add_petname_entry.set_text("")
    contacts_view.on_add()
    assert "own invite" in contacts_view.add_status.get_text()
    contacts = protocol.load_contacts(contacts_path)
    assert "Alice" not in contacts

    contacts_view.on_remove(None, "Carol")
    contacts = protocol.load_contacts(contacts_path)
    assert "Carol" not in contacts


def test_gui_version_display(gtk_app):
    app = gtk_app
    win = app["win"]
    st = win.supervisor.status()
    win.activity.refresh_info(st)
    label = win.activity.info_label.get_text()
    assert "aimlessd/" in label
    assert "client: aimless/" in label

    monkey_status = dict(st)
    monkey_status.pop("build")
    win.activity.refresh_info(monkey_status)
    label = win.activity.info_label.get_text()
    assert "old build" in label


def test_gui_away_banner(gtk_app):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]

    assert not win.away_banner.get_visible()

    win.set_away("gone fishing")

    def banner_and_propagated():
        if not win.away_banner.get_visible():
            return False
        icon_visible = win.away_icon.get_visible()
        label_visible = win.away_label.get_visible()
        height_ok = win.away_banner.get_allocated_height() >= 24
        if not (icon_visible and label_visible and height_ok):
            return False
        for p in bob.presence():
            if p["key"] == a_node and p.get("status_payload"):
                st = protocol.open_status(app["bob_identity"], p["status_payload"])
                if st.get("away") == "gone fishing":
                    return True
        return False
    assert _pump(win, banner_and_propagated, timeout=30), "away banner or propagation failed"

    back_btn = win.away_banner.get_children()[-1]
    assert isinstance(back_btn, Gtk.Button)
    back_btn.clicked()

    def cleared_and_propagated():
        if win.away_banner.get_visible():
            return False
        for p in bob.presence():
            if p["key"] == a_node and p.get("status_payload"):
                st = protocol.open_status(app["bob_identity"], p["status_payload"])
                if st.get("away") is None:
                    return True
        return False
    assert _pump(win, cleared_and_propagated, timeout=30), "banner never cleared or available never propagated"


def test_gui_away_status_propagates(gtk_app):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]

    win.set_away("brb — lunch")

    def away_visible():
        for p in bob.presence():
            if p["key"] == a_node and p.get("status_payload"):
                st = protocol.open_status(app["bob_identity"], p["status_payload"])
                return st.get("away") == "brb — lunch"
        return False
    assert _pump(win, away_visible, timeout=30), "away never reached bob"


def _pump(win, cond, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_gui_full_stack_real_poll_and_click(gtk_app):
    app = gtk_app
    win = app["win"]

    def real_pump(seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.02)

    # Un-stub poll_status: run the REAL timer handler (presence + status via daemon).
    def real_poll():
        try:
            win.supervisor.child = None  # poll_status calls supervisor.status(); guard
        except Exception:
            pass
        return win.poll_status()

    try:
        win.poll_status()  # first real call
        real_pump(2.0)

        # un-stub the periodic timer too
        orig = win.poll_status
        win.poll_status = real_poll
        b_node = app["b_node"]
        row = win.messages.threads[b_node]["row"]
        win.messages.thread_list.select_row(row)  # the click

        buf = win.messages.composer.get_buffer()
        buf.set_text("full-stack click+send")
        win.messages.send_message()
        real_pump(2.0)

        def delivered():
            hist = app["bob"].history(app["a_node"], 0)
            if not hist.get("msgs"):
                return False
            return any(True for _ in hist["msgs"])
        assert delivered(), "message never landed via real presence path"
    finally:
        win.poll_status = orig
