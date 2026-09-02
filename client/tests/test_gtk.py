import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

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
    monkeypatch.setattr(gtkui, "APP_PID_FILE", str(config / "app.pid"))
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
        "home": home, "sock_b": sock_b, "sock_a": sock_a, "dir_a": str(tmp_path / "nodeA"),
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


def test_close_hides_to_tray_and_window_survives(gtk_app):
    win = gtk_app["win"]

    class _StubTray:
        have_tray = True

    class _StubApp:
        tray = _StubTray()

        @staticmethod
        def log(msg):
            pass

    win.app_ref = _StubApp()
    stopped = win.emit("delete-event", Gdk.Event())
    assert stopped is True, "delete-event should be swallowed when a tray icon exists"
    assert not win.get_visible(), "window should hide instead of closing"

    win.deiconify()
    win.present()
    assert win.get_visible(), "window should come back"

    win.app_ref = None


def _bob_sees_away(app, expected):
    """True when bob's presence snapshot shows alice with the expected away (None = available)."""
    for p in app["bob"].presence():
        if p["key"] != app["a_node"] or not p.get("status_payload"):
            continue
        st = protocol.open_status(app["bob_identity"], p["status_payload"])
        if st.get("away") == expected:
            return True
    return False


def test_window_creation_announces_current_status(gtk_app):
    app = gtk_app
    assert _pump(app["win"], lambda: _bob_sees_away(app, None), timeout=30), \
        "window creation never announced the (available) status"


def test_reassert_pushes_away_and_available(gtk_app):
    app = gtk_app
    win = app["win"]

    win.set_away("brb — lunch")
    assert _pump(win, lambda: _bob_sees_away(app, "brb — lunch"), timeout=30)

    win.prefs["away"] = ""
    win._reassert_status()
    assert _pump(win, lambda: _bob_sees_away(app, None), timeout=30), \
        "re-assert never healed the stale away (stuck-away bug)"

    win.prefs["away"] = "gone again"
    win._reassert_status()
    assert _pump(win, lambda: _bob_sees_away(app, "gone again"), timeout=30)


def test_status_survives_own_daemon_restart(gtk_app):
    import os as _os
    import subprocess

    app = gtk_app
    win = app["win"]

    win.set_away("gone fishing")
    assert _pump(win, lambda: _bob_sees_away(app, "gone fishing"), timeout=30), \
        "away never reached bob before restart"

    who = DaemonClient(app["sock_a"]).request("whoami")
    old_pid = int(who["pid"])
    cmdline = open(f"/proc/{old_pid}/cmdline", "rb").read().decode().split("\x00")
    port = cmdline[cmdline.index("-listen") + 1].split(":")[-1]
    _os.kill(old_pid, 9)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            _os.kill(old_pid, 0)
            time.sleep(0.1)
        except OSError:
            break

    proc = subprocess.Popen(
        [gtkui.daemon_binary(), "-datadir", app["dir_a"], "-api", app["sock_a"],
         "-listen", f"tcp://127.0.0.1:{port}", "-peers", "none",
         "-retry", "300ms", "-probe", "300ms"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _pump(win, lambda: _bob_sees_away(app, "gone fishing"), timeout=30), \
            "away not re-delivered after own daemon restart (probe piggyback broken)"
    finally:
        proc.terminate()


def test_gui_room_create_send_receive(gtk_app):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]

    # a third party for the room: carol, known to alice via contacts only
    carol_identity = crypto.new_identity()
    carol = Client(DaemonClient(app["sock_b"]), carol_identity, "Carol")
    c_node = "cd" * 32  # fake node for carol (not probing)
    chosen = [
        {"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
        {"node": c_node, "pubkey": carol.pubkey_hex, "screen": "Carol"},
    ]

    win.messages.create_room(chosen)
    conv = None
    for key, t in win.messages.threads.items():
        if t.get("is_room"):
            conv = key
    assert conv is not None, "room thread missing after create_room"
    thread = win.messages.threads[conv]
    assert "Bob" in thread["screen"] and "Carol" in thread["screen"]
    assert not thread["contact"]

    win.messages.thread_list.select_row(thread["row"])
    buf = win.messages.composer.get_buffer()
    buf.set_text("hello room")
    win.messages.send_message()

    def bob_got_room_msg():
        hist = bob.history(a_node, 0)
        if not hist.get("msgs"):
            return False
        opened = protocol.open_message(app["bob_identity"], hist["msgs"][-1]["payload"])
        return opened["text"] == "hello room" and opened["conv"] == conv
    assert _pump(win, bob_got_room_msg, timeout=30), "room message never reached bob"

    texts = [m["text"] for m in win.session.cache.msgs(conv)]
    assert "hello room" in texts


def test_gui_request_accept_and_deny(gtk_app, tmp_path, monkeypatch):
    app = gtk_app
    win = app["win"]
    contacts_path = str(app["home"] / "client-contacts.json")

    stranger_node = "ab" * 32
    req = {"node": stranger_node, "pubkey": "ff" * 32, "screen": "Mallory",
           "conv": None, "members": [], "seq": 1, "ts": 1000, "text": "hi there"}
    win.session.cache.add_pending(req)

    answers = []
    monkeypatch.setattr(win, "_ask_request", lambda r: answers.append(True) or True)
    win.surface_pending_requests()

    contacts = protocol.load_contacts(contacts_path)
    assert "Mallory" in contacts
    assert contacts["Mallory"]["node"] == stranger_node
    assert not win.session.cache.pending()
    texts = [m["text"] for m in win.session.cache.msgs(stranger_node)]
    assert "hi there" in texts
    assert stranger_node in win.messages.threads, "accepted stranger has no thread"

    # deny the next one → muted, no contact, no thread, message dropped
    stranger2 = "cd" * 32
    win.session.cache.add_pending({"node": stranger2, "pubkey": "ee" * 32, "screen": "Spam",
                                   "conv": None, "members": [], "seq": 2, "ts": 1001, "text": "buy stuff"})
    monkeypatch.setattr(win, "_ask_request", lambda r: False)
    win.surface_pending_requests()
    contacts = protocol.load_contacts(contacts_path)
    assert "Spam" not in contacts
    assert win.session.cache.is_muted(stranger2)
    assert win.session.cache.msgs(stranger2) == []
    assert stranger2 not in win.messages.threads


def test_gui_request_persists_until_answered(gtk_app):
    app = gtk_app
    win = app["win"]
    req = {"node": "ef" * 32, "pubkey": "11" * 32, "screen": "Later",
           "conv": None, "members": [], "seq": 3, "ts": 1002, "text": "hey"}
    win.session.cache.add_pending(req)
    # a fresh Session on the same cache must still see the pending request
    fresh = gtkui.Session("testpass")
    assert fresh.cache.pending() and fresh.cache.pending()[0]["node"] == "ef" * 32


def test_gui_incoming_from_unknown_sender_queues_request(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]

    # simulate a v0.5 room invite arriving from bob... no — from a node NOT in contacts:
    stranger = "99" * 32
    alice_ident = win.session.identity
    payload = protocol.seal_message(alice_ident, alice_ident and _self_pub(app), "let me in", 500,
                                    screen="Newbie")
    ev = {"op": "recv", "from": stranger, "seq": 7, "payload": payload}
    monkeypatch.setattr(win, "_ask_request", lambda r: True)
    win.messages.incoming(ev)

    # the synchronous stub accepted the request, so pending is consumed and applied
    contacts = protocol.load_contacts(str(app["home"] / "client-contacts.json"))
    assert "Newbie" in contacts
    # accepted → message delivered into the new thread
    texts = [m["text"] for m in win.session.cache.msgs(stranger)]
    assert "let me in" in texts


def _self_pub(app):
    return app["session"].client.pubkey_hex
