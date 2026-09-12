import os
import re
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Gio

import test_e2e
from test_e2e import two_nodes  # noqa: F401

from aimless import crypto, protocol
from aimless.store import Store
from aimless import gtkui
from aimless.daemon import Client, DaemonClient, DaemonError


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
    Store(str(home / "state.db"), "testpass")

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


def test_gui_scroll_reaches_actual_bottom():
    """A row appended just before scroll_to_bottom is laid out by the frame clock,
    which fires after the first scroll attempt — so the pre-fix single idle pass
    always ended up one row short. The scroll must re-settle on a tick boundary
    until it truly sits at the bottom."""
    from aimless import gtkui as g

    win = Gtk.Window()
    win.set_default_size(300, 200)
    sw = Gtk.ScrolledWindow()
    win.add(sw)
    lb = Gtk.ListBox()
    sw.add(lb)
    for i in range(40):
        r = Gtk.ListBoxRow()
        lbl = Gtk.Label(label=f"message line {i} — some wrapping text to give height")
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(48)
        r.add(lbl)
        lb.add(r)
        r.show_all()
    win.show_all()

    def drain_ms(ms):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.002)

    drain_ms(500)
    adj = sw.get_vadjustment()

    for k in range(5):
        r = Gtk.ListBoxRow()
        lbl = Gtk.Label(label=f"NEW MESSAGE {k} arrives at the bottom")
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(48)
        r.add(lbl)
        lb.add(r)
        r.show_all()
        g.scroll_to_bottom(sw)
        drain_ms(2)   # scroll runs here on a stale size (old code stays stuck)
    drain_ms(200)     # frame clock lays out the rows; the re-settle must catch up

    gap = adj.get_upper() - adj.get_page_size() - adj.get_value()
    win.destroy()
    assert gap < 4, f"scroll left one row behind the latest message (gap={gap:.1f})"


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


def test_gui_contacts_add_remove_and_self_guard(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    contacts_path = str(app["home"] / "client-contacts.json")

    contacts_view = win.contacts
    contacts_view.refresh()
    assert _pump(win, lambda: contacts_view.invite_entry.get_text().startswith("aimless1:")), \
        "invite never loaded"
    invite = contacts_view.invite_entry.get_text()
    client_hex, node_out, _ = protocol.parse_invite(invite)
    assert node_out == app["a_node"]
    assert client_hex == app["session"].client.pubkey_hex

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

    # Remove now confirms: declining keeps the contact, accepting removes it.
    monkeypatch.setattr(contacts_view, "_confirm_remove", lambda petname: False)
    contacts_view.on_remove(None, "Carol")
    assert "Carol" in protocol.load_contacts(contacts_path), "declined remove must keep the contact"

    monkeypatch.setattr(contacts_view, "_confirm_remove", lambda petname: True)
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


def test_gui_old_daemon_warns(gtk_app):
    app = gtk_app
    win = app["win"]

    win.activity.refresh_info({"build": "aimlessd/0.4.1", "peers_up": 1, "peers_total": 2,
                               "address": "x", "mtu": 65535})
    label = win.activity.info_label.get_text()
    assert "too old" in label, "an old daemon build must warn in the status line"
    assert "0.8.0" in label, "warning must name the minimum build"

    win.activity.refresh_info({"build": "aimlessd/0.8.0", "peers_up": 1, "peers_total": 2,
                               "address": "x", "mtu": 65535})
    label = win.activity.info_label.get_text()
    assert "too old" not in label, "a current daemon must not warn"


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

        def is_embedded(self):
            return True

    class _StubApp:
        tray = _StubTray()

        @staticmethod
        def log(msg):
            pass

    win.app_ref = _StubApp()
    stopped = win.emit("delete-event", Gdk.Event())
    assert stopped is True, "delete-event should be swallowed when a real tray has the icon"
    assert not win.get_visible(), "window should hide instead of closing"

    win.deiconify()
    win.present()
    assert win.get_visible(), "window should come back"

    win.app_ref = None


def test_close_quits_when_tray_not_embedded(gtk_app):
    win = gtk_app["win"]

    class _StubTray:
        have_tray = True

        def is_embedded(self):
            return False

    class _StubApp:
        tray = _StubTray()

        def __init__(self):
            self.quit_calls = 0

        def log(self, msg):
            pass

        def quit(self):
            self.quit_calls += 1

    app = _StubApp()
    win.app_ref = app
    stopped = win.emit("delete-event", Gdk.Event())
    assert stopped is False, "close should not be swallowed without a real tray"
    win.destroy()
    assert app.quit_calls == 1, "closing the window should quit the app headlessly"
    win.app_ref = None


def test_cancel_without_tray_logs_exit_and_no_window_exit_signal(gtk_app):
    from aimless import gtkui as g

    class _NoTray:
        def is_embedded(self):
            return False

    class _App:
        tray = _NoTray()

        def __init__(self):
            self.quit_calls = 0
            self.logged = []

        def log(self, m):
            self.logged.append(m)

        def quit(self):
            self.quit_calls += 1

    app = _App()
    g.AimlessApp._cancel_or_quit(app)
    assert app.logged, "headless (no tray): cancel must log the exit decision"
    assert app.quit_calls == 0, "exit is decided by setup, not via gtk_main_quit (pre-main-loop)"

    class _Embedded(_NoTray):
        def is_embedded(self):
            return True

    app.tray = _Embedded()
    g.AimlessApp._cancel_or_quit(app)
    assert app.logged[-1].startswith("cancel — keeping app in the system tray")

    class _EmbeddedNoWindow(_Embedded):
        pass

    app.logged = []
    app.window = None
    app.tray = _NoTray()
    assert g.AimlessApp._no_window_headless(app), "container: no window + no tray -> exit"
    app.tray = _Embedded()
    assert not g.AimlessApp._no_window_headless(app), "desktop: tray keeps the app alive"
    app.window = object()
    app.tray = _NoTray()
    assert not g.AimlessApp._no_window_headless(app), "window present -> stay"


def test_create_identity_writes_files(tmp_path, monkeypatch):
    from aimless import gtkui as g
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    identity_file = home / "identity.json"
    assert not identity_file.exists()
    pw = g.create_identity("secret", "Gerry")
    assert pw == "secret"
    identity = crypto.load_identity(str(identity_file), "secret")
    contacts = protocol.load_contacts(str(home / "client-contacts.json"))
    assert contacts["_self"]["screen"] == "Gerry"
    assert contacts["_self"]["pubkey"] == bytes(identity.verify_key).hex()


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
    daemon_exe = _os.readlink(f"/proc/{old_pid}/exe")
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
        [daemon_exe, "-datadir", app["dir_a"], "-api", app["sock_a"],
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
    monkeypatch.setattr(win, "_ask_request", lambda r: answers.append(True) or Gtk.ResponseType.ACCEPT)
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
    monkeypatch.setattr(win, "_ask_request", lambda r: Gtk.ResponseType.REJECT)
    win.surface_pending_requests()
    contacts = protocol.load_contacts(contacts_path)
    assert "Spam" not in contacts
    assert not win.session.cache.is_muted(stranger2), "Deny is a one-time decline, not a mute"
    assert win.session.cache.msgs(stranger2) == []
    assert stranger2 not in win.messages.threads


def test_gui_accepted_room_message_redraws_open_conversation(gtk_app, monkeypatch):
    """A message from a non-buddy in an open room is withheld behind the
    Accept/Deny prompt; accepting must draw it immediately, not only after
    switching threads."""
    app = gtk_app
    win = app["win"]
    session = app["session"]

    stranger = "ab" * 32
    room = "r" + "0" * 63
    members = {
        session.self_node: {"node": session.self_node, "pubkey": session.client.pubkey_hex,
                            "screen": "Alice"},
        stranger: {"node": stranger, "pubkey": "ff" * 32, "screen": "Mallory"},
    }
    session.cache.ensure_room(room, members)
    win.messages.sync_sidebar()
    thread = win.messages.threads[room]
    win.messages.thread_list.select_row(thread["row"])
    pump(0.5)  # let the async history load settle (room still empty)

    req = {"node": stranger, "pubkey": "ff" * 32, "screen": "Mallory", "conv": room,
           "members": [{"node": n, **m} for n, m in members.items()],
           "seq": 1, "ts": 1000, "text": "hello group"}
    session.cache.add_pending(req)
    assert session.cache.msgs(room) == [], "the message must stay withheld until accepted"

    monkeypatch.setattr(win, "_ask_request", lambda r: Gtk.ResponseType.ACCEPT)
    win.surface_pending_requests()

    assert [m["text"] for m in session.cache.msgs(room)] == ["hello group"]
    assert "hello group" in all_texts(win.messages.conversation), \
        "accepted message must be drawn in the already-open room"


def test_gui_deny_accept_block_responses(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    session = app["session"]

    blocked, unblocked = [], []
    monkeypatch.setattr(session.client, "block", lambda node: blocked.append(node) or {"op": "blocked"})
    monkeypatch.setattr(session.client, "unblock", lambda node: unblocked.append(node) or {"op": "unblocked"})

    def send_request(node, screen):
        win.session.cache.add_pending({"node": node, "pubkey": "11" * 32, "screen": screen,
                                       "conv": None, "members": [], "seq": 5, "ts": 1000, "text": "hi"})
        win.surface_pending_requests()

    # Deny: one-time soft decline — no persistent state, no daemon interaction;
    # the sender's next message re-prompts.
    stranger = "cd" * 32
    monkeypatch.setattr(win, "_ask_request", lambda r: Gtk.ResponseType.REJECT)
    send_request(stranger, "Spam")
    assert not win.session.cache.is_muted(stranger)
    assert blocked == [] and unblocked == [], "Deny must not touch the daemon"
    prompts = []
    monkeypatch.setattr(win, "_ask_request", lambda r: prompts.append(1) or Gtk.ResponseType.REJECT)
    send_request(stranger, "Spam")
    assert prompts, "a denied sender must be re-prompted on their next message"

    # Block: mute + daemon block
    bad = "ce" * 32
    monkeypatch.setattr(win, "_ask_request", lambda r: Gtk.ResponseType.NO)
    send_request(bad, "Worse")
    assert win.session.cache.is_muted(bad)
    assert _pump(win, lambda: blocked == [bad], timeout=10), "Block never sent a daemon block"

    # Accept: unblocks (re-adding someone previously blocked/denied)
    friend = "cf" * 32
    monkeypatch.setattr(win, "_ask_request", lambda r: Gtk.ResponseType.ACCEPT)
    send_request(friend, "Friend")
    assert _pump(win, lambda: unblocked == [friend], timeout=10), "Accept never sent a daemon unblock"
    assert not win.session.cache.is_muted(friend)


def test_gui_block_falls_back_when_daemon_lacks_block(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    session = app["session"]

    def no_block(node):
        raise DaemonError("unknown op: block")

    monkeypatch.setattr(session.client, "block", no_block)
    stranger = "cd" * 32
    win.session.cache.add_pending({"node": stranger, "pubkey": "ee" * 32, "screen": "Spam",
                                   "conv": None, "members": [], "seq": 2, "ts": 1001, "text": "buy"})
    monkeypatch.setattr(win, "_ask_request", lambda r: Gtk.ResponseType.NO)
    win.surface_pending_requests()  # must not crash on the failed block op
    assert win.session.cache.is_muted(stranger), "client-side mute must still apply on fallback"
    assert not win.session.cache.msgs(stranger)


def test_client_block_unblock_wrappers():
    calls = []

    class _DC:
        def request(self, op, **kw):
            calls.append((op, kw))
            return {"op": "ok"}

    c = Client(_DC(), crypto.new_identity(), "T")
    c.block("aa" * 32)
    c.unblock("bb" * 32)
    assert calls == [("block", {"to": "aa" * 32}), ("unblock", {"to": "bb" * 32})]


def _contact_rows(win):
    out = []
    for row in win.contacts.buddy_list.get_children():
        box = row.get_child()
        children = box.get_children()
        labels = children[0]
        title = labels.get_children()[0].get_label() or ""
        sub = labels.get_children()[1].get_label() if len(labels.get_children()) > 1 else ""
        btn = children[-1].get_label()
        out.append({"row": row, "btn": btn, "title": title, "sub": sub})
    return out


def test_gui_blocked_contact_renders_unblock(gtk_app):
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]
    win.session.client.block(b_node)
    win.contacts.refresh()

    def blocked_rendered():
        rows = _contact_rows(win)
        b = next((r for r in rows if "Bob" in r["title"]), None)
        return b is not None and b["btn"] == "Unblock"
    assert _pump(win, blocked_rendered, timeout=10), "blocked contact never rendered with Unblock"
    rows = _contact_rows(win)
    bob = next(r for r in rows if "Bob" in r["title"])
    bob["row"].get_child().get_children()[-1].clicked()

    def reverted():
        rows = _contact_rows(win)
        b = next((r for r in rows if "Bob" in r["title"]), None)
        return b is not None and b["btn"] == "Remove"
    assert _pump(win, reverted, timeout=10), "contact did not revert to Remove after unblock"


def test_gui_blocked_stranger_renders_synthetic_row(gtk_app):
    app = gtk_app
    win = app["win"]
    stranger = "cd" * 32
    win.session.client.block(stranger)
    win.contacts.refresh()

    def synthetic_rendered():
        rows = _contact_rows(win)
        return any(stranger[:20] in r["title"] for r in rows)
    assert _pump(win, synthetic_rendered, timeout=10), "denied stranger never got a synthetic row"
    syn = next(r for r in _contact_rows(win) if stranger[:20] in r["title"])
    assert syn["btn"] == "Unblock"

    syn["row"].get_child().get_children()[-1].clicked()

    def gone():
        rows = _contact_rows(win)
        return not any(stranger[:20] in r["title"] for r in rows)
    assert _pump(win, gone, timeout=10), "synthetic row not removed after unblock"


def test_gui_blocked_stranger_shows_screen_name(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    stranger = "cd" * 32
    win.session.cache.add_pending({"node": stranger, "pubkey": "ee" * 32, "screen": "Spammy",
                                   "conv": None, "members": [], "seq": 2, "ts": 1001, "text": "buy"})
    monkeypatch.setattr(win, "_ask_request", lambda r: Gtk.ResponseType.NO)
    win.surface_pending_requests()

    def named_rendered():
        rows = _contact_rows(win)
        return any("Spammy" in r["title"] for r in rows)
    assert _pump(win, named_rendered, timeout=10), "blocked stranger never rendered with its screen name"
    syn = next(r for r in _contact_rows(win) if "Spammy" in r["title"])
    assert syn["btn"] == "Unblock"
    assert stranger[:20] in syn["sub"], "row must show the truncated node for identification"

    syn["row"].get_child().get_children()[-1].clicked()

    def gone():
        rows = _contact_rows(win)
        return not any(stranger[:20] in r["title"] or stranger[:20] in r["sub"] for r in rows)
    assert _pump(win, gone, timeout=10), "synthetic row not removed after unblock"
    assert win.session.cache.blocked_screen(stranger) is None, "blocked screen name not cleared on unblock"


def test_gui_blocklist_error_renders_contacts_only(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]

    def boom():
        raise DaemonError("unknown op: blocklist")

    monkeypatch.setattr(win.session.client, "blocklist", boom)
    win.contacts.refresh()
    rows = _contact_rows(win)
    assert rows, "contacts must still render when blocklist is unavailable"
    assert all(r["btn"] == "Remove" for r in rows), "blocklist failure must not add blocked rows"
    assert _pump(win, lambda: win.contacts.invite_entry.get_text().startswith("aimless1:"), timeout=10), \
        "invite field must still populate on blocklist failure"


def test_gui_1to1_block_button_visibility_and_label(gtk_app):
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]

    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    assert win.messages.block_btn.get_visible(), "block button must show for a 1:1 thread"
    assert win.messages.block_btn.get_label() == "Block…"
    assert win.messages.mute_btn.get_visible(), "mute button must show for a 1:1 thread"
    assert win.messages.mute_btn.get_label() == "Mute…"

    carol_identity = crypto.new_identity()
    chosen = [{"node": b_node, "pubkey": app["bob"].pubkey_hex, "screen": "Bob"},
              {"node": "cd" * 32, "pubkey": bytes(carol_identity.verify_key).hex(), "screen": "Carol"}]
    win.messages.create_room(chosen)
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))
    win.messages.thread_list.select_row(win.messages.threads[conv]["row"])
    assert not win.messages.block_btn.get_visible(), "block button must hide for a room"
    assert win.messages.mute_btn.get_visible(), "mute button must show for a room"


def test_gui_1to1_block_removes_contact(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]
    contacts_path = str(app["home"] / "client-contacts.json")

    monkeypatch.setattr(win.messages, "_confirm_block", lambda screen: True)
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    win.messages.block_btn.clicked()

    contacts = protocol.load_contacts(contacts_path)
    assert not any(c.get("node") == b_node for k, c in contacts.items() if k != "_self"), \
        "block must remove the contact"
    assert b_node not in win.messages.threads, "block must tear down the thread"
    assert win.messages.selected is None, "selection must reset after block"
    assert win.messages.stack.get_visible_child_name() == "placeholder"
    assert win.session.cache.is_muted(b_node), "block must set the client mute"
    assert win.session.cache.blocked_screen(b_node) == "Bob", "block must store the screen name"

    def synthetic_rendered():
        rows = _contact_rows(win)
        return any("Bob" in r["title"] and r["btn"] == "Unblock" for r in rows)
    assert _pump(win, synthetic_rendered, timeout=10), \
        "Contacts must show the blocked stranger's synthetic row with Unblock"


def test_gui_1to1_mute_toggle(gtk_app):
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]

    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    win.messages.on_toggle_mute()
    assert win.session.cache.is_conversation_muted(b_node), "mute must mark the 1:1 conversation"
    assert win.messages.mute_btn.get_label() == "Unmute…"
    assert b_node in win.messages.threads, "mute must keep the thread"
    assert win.messages.threads[b_node]["row"].get_style_context().has_class("aimless-muted")
    assert win.messages.threads[b_node]["widgets"]["subtitle"].get_text() == "muted"

    win.messages.on_toggle_mute()
    assert not win.session.cache.is_conversation_muted(b_node)
    assert win.messages.mute_btn.get_label() == "Mute…"


def _make_file_events(data, filename, mime_hint="application", conv=None, identity=None, pubkey=None, sender=None):
    """Split a file into sealed TypeFile chunk recv events (sealed to `pubkey`)."""
    import base64 as _b64
    import hashlib as _h
    from aimless import protocol as _p

    pieces = [data[i:i + _p.FILE_CHUNK_SIZE] for i in range(0, len(data), _p.FILE_CHUNK_SIZE)]
    tid = _p.new_transfer_id()
    sha = _h.sha256(data).hexdigest()
    events = []
    for i, piece in enumerate(pieces):
        chunk = _p.make_chunk(tid, i, len(pieces), filename, mime_hint, sha, len(data), piece, conv=conv)
        sealed = _p.seal_file_chunk(identity, pubkey, chunk)
        payload = _b64.b64encode(_p.file_header(bytes.fromhex(tid), i, len(pieces)) + sealed).decode()
        events.append({"op": "recv", "type": "file", "from": sender,
                       "seq": 100 + i, "ts": 1000 + i, "payload": payload})
    return tid, sha, events


def test_gui_file_receive_renders_and_stores(gtk_app):
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]
    data = os.urandom(80_000)
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    tid, sha, events = _make_file_events(data, "photo.jpg", "image",
                                         identity=win.session.identity,
                                         pubkey=win.session.client.pubkey_hex, sender=b_node)
    for ev in events:
        win.messages.incoming(ev)

    def stored():
        msgs = [m for m in win.session.cache.msgs(b_node) if m.get("attachment")]
        return msgs and os.path.exists(msgs[-1]["attachment"]["path"])
    assert _pump(win, stored, timeout=10), "attachment never stored"
    msg = [m for m in win.session.cache.msgs(b_node) if m.get("attachment")][-1]
    with open(msg["attachment"]["path"], "rb") as f:
        assert f.read() == data, "file bytes must round-trip"
    assert msg["attachment"]["filename"] == "photo.jpg"
    assert msg["attachment"]["mime_hint"] == "image"

    def save_btn_visible():
        return any(_walk_buttons(w, "Save") for w in win.messages.conversation.get_children())
    assert _pump(win, save_btn_visible, timeout=10), "rendered attachment must have a Save button"


def test_gui_file_receive_out_of_order(gtk_app):
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]
    data = os.urandom(100_000)
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    tid, _, events = _make_file_events(data, "big.bin",
                                       identity=win.session.identity,
                                       pubkey=win.session.client.pubkey_hex, sender=b_node)
    for ev in reversed(events):
        win.messages.incoming(ev)

    def stored():
        msgs = [m for m in win.session.cache.msgs(b_node) if m.get("attachment")]
        return msgs and os.path.exists(msgs[-1]["attachment"]["path"])
    assert _pump(win, stored, timeout=10), "out-of-order chunks never completed"
    msg = [m for m in win.session.cache.msgs(b_node) if m.get("attachment")][-1]
    with open(msg["attachment"]["path"], "rb") as f:
        assert f.read() == data


def test_gui_file_receive_checksum_mismatch(gtk_app):
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]
    data = os.urandom(40_000)
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    # build valid events, then re-seal chunk 0 with corrupted data but the ORIGINAL sha
    import base64 as _b64
    import hashlib as _h
    from aimless import protocol as _p
    tid, sha, events = _make_file_events(data, "bad.bin",
                                         identity=win.session.identity,
                                         pubkey=win.session.client.pubkey_hex, sender=b_node)
    pieces = [data[i:i + _p.FILE_CHUNK_SIZE] for i in range(0, len(data), _p.FILE_CHUNK_SIZE)]
    corrupt = bytearray(pieces[0])
    corrupt[0] ^= 0xFF
    bad_chunk = _p.make_chunk(tid, 0, len(pieces), "bad.bin", "application", sha, len(data), bytes(corrupt))
    events[0]["payload"] = _b64.b64encode(
        _p.file_header(bytes.fromhex(tid), 0, len(pieces)) +
        _p.seal_file_chunk(win.session.identity, win.session.client.pubkey_hex, bad_chunk)).decode()
    for ev in events:
        win.messages.incoming(ev)

    def not_stored():
        return not any(m.get("attachment") for m in win.session.cache.msgs(b_node))
    assert _pump(win, not_stored, timeout=10), "checksum mismatch must not store the file"


def _walk_buttons(widget, label):
    if isinstance(widget, Gtk.Button) and widget.get_label() == label:
        return True
    if isinstance(widget, Gtk.Container):
        return any(_walk_buttons(c, label) for c in widget.get_children())
    return False


def test_gui_fetched_transfer_sweep(gtk_app):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]
    data = os.urandom(50_000)

    import base64 as _b64
    import hashlib as _h
    from aimless import protocol as _p
    pieces = [data[i:i + _p.FILE_CHUNK_SIZE] for i in range(0, len(data), _p.FILE_CHUNK_SIZE)]
    tid = _p.new_transfer_id()
    sha = _h.sha256(data).hexdigest()
    for i, piece in enumerate(pieces):
        chunk = _p.make_chunk(tid, i, len(pieces), "sweep.bin", "application", sha, len(data), piece)
        sealed = _p.seal_file_chunk(app["bob_identity"], app["session"].client.pubkey_hex, chunk)
        payload = _b64.b64encode(_p.file_header(bytes.fromhex(tid), i, len(pieces)) + sealed).decode()
        bob.daemon.request("sendfile", to=a_node, payload=payload)

    def pending():
        try:
            return win.session.client.pending_attachments(b_node)
        except Exception:
            return []
    assert _pump(win, lambda: pending(), timeout=20), "transfer never landed on the daemon"

    chunks = win.session.client.fetch_attachment(b_node, tid)
    assert chunks, "fetch returned nothing"
    win.messages._process_fetched_transfer(b_node, tid, chunks)

    def consumed():
        return win.session.client.pending_attachments(b_node) == []
    assert _pump(win, consumed, timeout=10), "transfer not acked after consumption"

    msgs = [m for m in win.session.cache.msgs(b_node) if m.get("attachment")]
    assert msgs and os.path.exists(msgs[-1]["attachment"]["path"])
    with open(msgs[-1]["attachment"]["path"], "rb") as f:
        assert f.read() == data


def test_gui_file_send_queues_and_renders(gtk_app, tmp_path):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]
    f = tmp_path / "hello.txt"
    f.write_bytes(b"hello attachment " * 5000)
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    win.messages._send_file(win.messages.selected, str(f), "hello.txt", os.path.getsize(str(f)))

    def sent():
        return [m for m in win.session.cache.msgs(b_node)
                if m.get("attachment") and m["dir"] == "out"]
    assert _pump(win, sent, timeout=10), "sent attachment never cached"

    def landed():
        try:
            return bool(bob.daemon.request("pendingattachments", **{"from": a_node}, timeout=5))
        except Exception:
            return False
    assert _pump(win, landed, timeout=20), "chunks never reached bob's daemon"

    def save_btn():
        return any(_walk_buttons(w, "Save") for w in win.messages.conversation.get_children())
    assert _pump(win, save_btn, timeout=10), "sent attachment must render with a Save button"


def test_gui_file_send_delivery_indicator(gtk_app):
    """Sent files get a persistent delivery line that advances with the daemon's
    per-chunk ack events, so a sender can tell the file actually arrived."""
    win = gtk_app["win"]
    m = win.messages

    row = m._append_status_row("")
    # n1 has two chunks, n2 has one: progress is per recipient (2 people).
    m.register_file_delivery(row, "big.bin", {("n1", 1), ("n1", 2), ("n2", 3)})
    assert "Sending big.bin" in row.get_child().get_text()
    assert "0/2" in row.get_child().get_text()

    m.note_acked("n1", 1)
    assert "0/2" in row.get_child().get_text(), "n1 still has a chunk outstanding"
    m.note_acked("n1", 2)
    assert "1/2" in row.get_child().get_text(), "n1 fully delivered"
    m.note_acked("n2", 3)
    text = row.get_child().get_text()
    assert "Delivered" in text and "big.bin" in text

    # An ack that lands before the send finishes registering must still count.
    m.note_acked("n9", 7)
    row2 = m._append_status_row("")
    m.register_file_delivery(row2, "race.bin", {("n9", 7), ("n9", 8)})
    assert "Sending race.bin" in row2.get_child().get_text(), "one recipient outstanding"
    m.note_acked("n9", 8)
    assert "Delivered" in row2.get_child().get_text()


def test_gui_file_send_to_room(gtk_app, tmp_path):
    """Regression: sending an attachment into a 3+ member room used to pass the
    raw members dict (node-hex -> info) into send_file_room, whose per-member
    loop does m["node"] — iterating a dict yields string keys, so it raised
    TypeError: string indices must be integers inside the async worker. The
    call site must convert to member-info dicts first (mirroring text-send),
    and every non-self member must actually receive the chunks."""
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]

    carol_identity = crypto.new_identity()
    chosen = [{"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
              {"node": "cd" * 32, "pubkey": bytes(carol_identity.verify_key).hex(),
               "screen": "Carol"}]
    win.messages.create_room(chosen)
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))
    thread = win.messages.threads[conv]
    win.messages.thread_list.select_row(thread["row"])

    f = tmp_path / "room.bin"
    f.write_bytes(b"room attachment " * 4000)
    win.messages._send_file(thread, str(f), "room.bin", os.path.getsize(str(f)))

    def cached_sent():
        return [m for m in win.session.cache.msgs(conv)
                if m.get("attachment") and m["dir"] == "out"]
    assert _pump(win, cached_sent, timeout=15), "room attachment never cached"

    def bob_got_chunks():
        try:
            return bool(bob.daemon.request("pendingattachments", **{"from": a_node}, timeout=5))
        except Exception:
            return False
    assert _pump(win, bob_got_chunks, timeout=20), "chunks never reached bob's daemon"

    def rendered():
        return any(_walk_buttons(w, "Save") for w in win.messages.conversation.get_children())
    assert _pump(win, rendered, timeout=15), "room attachment must render with a Save button"


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
    monkeypatch.setattr(win, "_ask_request", lambda r: Gtk.ResponseType.ACCEPT)
    win.messages.incoming(ev)

    # the synchronous stub accepted the request, so pending is consumed and applied
    contacts = protocol.load_contacts(str(app["home"] / "client-contacts.json"))
    assert "Newbie" in contacts
    # accepted → message delivered into the new thread
    texts = [m["text"] for m in win.session.cache.msgs(stranger)]
    assert "let me in" in texts


def _self_pub(app):
    return app["session"].client.pubkey_hex


def test_room_history_excludes_dm_history(gtk_app):
    """Regression: creating a room must NOT pull the buddies' old DMs into it,
    and room messages must not leak into the DM threads."""
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]

    ts = int(time.time() * 1000)
    bob.send(app["session"].client.pubkey_hex, a_node, "old private dm", ts)

    def dm_arrived():
        hist = win.session.client.history(b_node, 0)
        msgs = hist.get("msgs", [])
        return any(protocol.open_message(win.session.identity, m["payload"])["text"] == "old private dm"
                   for m in msgs)
    assert _pump(win, dm_arrived, timeout=30), "setup: dm never reached alice"

    chosen = [{"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
              {"node": "cd" * 32, "pubkey": "aa" * 32, "screen": "Carol"}]
    win.messages.create_room(chosen)
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))

    def room_scanned_clean():
        win.messages.thread_list.select_row(win.messages.threads[conv]["row"])
        msgs = win.session.cache.msgs(conv)
        scanned = win.session.cache.scan_last(conv, b_node) > 0
        return scanned and all(m["text"] != "old private dm" for m in msgs)
    assert _pump(win, room_scanned_clean, timeout=30), "room polluted with DM history"
    assert win.session.cache.msgs(conv) == [], "room should have no history yet"

    # a real room message arrives and is the only thing in the room
    carol_key = crypto.new_identity()
    members = [{"node": a_node, "pubkey": app["session"].client.pubkey_hex, "screen": "Alice"},
               {"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
               {"node": "cd" * 32, "pubkey": bytes(carol_key.verify_key).hex(), "screen": "Carol"}]
    bob.send_room(members, conv, "first room msg", ts + 100)

    def room_msg_only():
        msgs = win.session.cache.msgs(conv)
        return [m["text"] for m in msgs] == ["first room msg"]
    assert _pump(win, lambda: (win.messages.thread_list.select_row(
        win.messages.threads[conv]["row"]), room_msg_only())[-1], timeout=30)

    # and the DM thread must not contain the room message
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    dm_texts = [m["text"] for m in win.session.cache.msgs(b_node)]
    assert "old private dm" in dm_texts
    assert "first room msg" not in dm_texts


def test_clear_dismisses_backlog(gtk_app, monkeypatch):
    """Bug regression: clearing must not resurrect received messages via refetch."""
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    b_node = app["b_node"]

    ts = int(time.time() * 1000)
    bob.send(app["session"].client.pubkey_hex, app["a_node"], "received dm", ts)
    assert _pump(win, lambda: [m["text"] for m in win.session.cache.msgs(b_node)] == ["received dm"],
                 timeout=30), "setup: dm never stored"

    monkeypatch.setattr(win.messages, "_confirm_clear", lambda title: True)
    win.messages.selected = win.messages.threads[b_node]
    win.messages.on_clear_history()

    def cleared_and_dismissed():
        return win.session.cache.msgs(b_node) == [] and \
            win.session.cache.scan_last(b_node, b_node) >= 1
    assert _pump(win, cleared_and_dismissed, timeout=30), "clear did not dismiss the backlog"

    # reopening the thread must not pull the dismissed history back
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    deadline = time.time() + 5
    while time.time() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)
    assert win.session.cache.msgs(b_node) == [], "cleared DM history came back"


def test_room_dots_markup():
    from aimless.gtkui import _room_dots_markup
    members = {"n1": {"screen": "Bob"}, "n2": {"screen": "Carol"}, "n3": {"screen": "Dan"}}
    pb = {"n1": {"online": True, "away": None},
          "n2": {"online": False, "away": "gone"},
          "n3": {}}
    markup = _room_dots_markup(members, pb, exclude="me")
    assert markup.count("●") == 3
    greens = markup.count("#a6e3a1")
    oranges = markup.count("#fab387")
    grays = markup.count("#6c7086")
    assert (greens, oranges, grays) == (1, 1, 1), markup
    # self is excluded
    assert _room_dots_markup({"me": {"screen": "Me"}}, {}, exclude="me") == ""


def test_sidebar_room_markup_uses_count():
    from aimless.gtkui import _sidebar_title_markup
    members = {f"n{i}": {"node": f"n{i}", "pubkey": "pk", "screen": f"P{i}"} for i in range(10)}
    members["me"] = {"node": "me", "pubkey": "pk", "screen": "Me"}
    thread = {"is_room": True, "screen": "P0, P1 +8", "members": members, "online": True, "away": None,
              "presence_by_node": {f"n{i}": {"online": True, "away": None} for i in range(6)}}
    markup = _sidebar_title_markup(thread, "me")
    assert markup.count("●") == 1, "sidebar must show ONE dot at any room size"
    assert "6/10" in markup

    dm = {"is_room": False, "screen": "Bob", "online": True, "away": None}
    dm_markup = _sidebar_title_markup(dm, "me")
    assert dm_markup.count("●") == 1
    assert not re.search(r"\d/\d", dm_markup), "DM rows must not show a count"

    dead = dict(thread, online=False,
                presence_by_node={f"n{i}": {"online": False, "away": None} for i in range(10)})
    assert "0/10" in _sidebar_title_markup(dead, "me")


def test_room_header_markup_and_live_update(gtk_app):
    from aimless.gtkui import _room_header_markup
    app = gtk_app
    win = app["win"]
    b_node = app["b_node"]

    chosen = [{"node": b_node, "pubkey": app["bob"].pubkey_hex, "screen": "Bob"},
              {"node": "cd" * 32, "pubkey": "aa" * 32, "screen": "Carol"}]
    win.messages.create_room(chosen)
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))
    win.messages.thread_list.select_row(win.messages.threads[conv]["row"])

    # live update path: presence poll re-renders the header without a reselect.
    # carol's away arrives as a sealed status payload (what the daemon actually carries)
    carol_identity = crypto.new_identity()
    carol_away = protocol.seal_status(carol_identity, app["session"].client.pubkey_hex,
                                      "Carol", "gone", int(time.time() * 1000))
    win.messages.refresh_presence({b_node: {"online": True},
                                   "cd" * 32: {"online": False, "status_payload": carol_away}})
    header = win.messages.conversation_header.get_label()
    assert header.count("●") == 2, "header shows one dot per member"
    assert "#a6e3a1" in header and "#fab387" in header, "away member is orange"
    assert "1/2 online" in header

    win.messages.refresh_presence({b_node: {"online": False}, "cd" * 32: {"online": False}})
    assert "0/2 online" in win.messages.conversation_header.get_label()

    # DM selection hides the delete button
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    assert not win.messages.delete_btn.get_visible()


def test_delete_room_and_reappear(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]

    carol_key = crypto.new_identity()
    chosen = [{"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
              {"node": "cd" * 32, "pubkey": bytes(carol_key.verify_key).hex(), "screen": "Carol"}]
    win.messages.create_room(chosen)
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))
    thread = win.messages.threads[conv]
    win.messages.thread_list.select_row(thread["row"])
    assert win.messages.delete_btn.get_visible(), "delete button must show for rooms"

    monkeypatch.setattr(win.messages, "_confirm_delete", lambda t: True)
    win.messages.on_delete_room()

    def deleted():
        return conv not in win.messages.threads and win.session.cache.rooms() == []
    assert _pump(win, deleted, timeout=30)
    assert not win.messages.delete_btn.get_visible()

    # a member messaging the room brings it back, with the message routed in
    members = [{"node": a_node, "pubkey": app["session"].client.pubkey_hex, "screen": "Alice"},
               {"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
               {"node": "cd" * 32, "pubkey": bytes(carol_key.verify_key).hex(), "screen": "Carol"}]
    bob.send_room(members, conv, "room is back", int(time.time() * 1000))

    def room_back():
        return conv in win.messages.threads and \
            [m["text"] for m in win.session.cache.msgs(conv)] == ["room is back"]
    assert _pump(win, room_back, timeout=30), "deleted room did not reappear on new message"


def test_delete_room_only_new_on_reappear(gtk_app, monkeypatch):
    """Bug regression: a deleted room that comes back must contain only new messages."""
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]
    ts = int(time.time() * 1000)
    carol_key = crypto.new_identity()
    members = [{"node": a_node, "pubkey": app["session"].client.pubkey_hex, "screen": "Alice"},
               {"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
               {"node": "cd" * 32, "pubkey": bytes(carol_key.verify_key).hex(), "screen": "Carol"}]
    win.messages.create_room(members[1:])
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))

    bob.send_room(members, conv, "old one", ts)
    bob.send_room(members, conv, "old two", ts + 1)

    def old_stored():
        return [m["text"] for m in win.session.cache.msgs(conv)] == ["old one", "old two"]
    assert _pump(win, old_stored, timeout=30)

    monkeypatch.setattr(win.messages, "_confirm_delete", lambda t: True)
    win.messages.selected = win.messages.threads[conv]
    win.messages.on_delete_room()

    # removal is async (the dismiss cursor is fetched from the daemon first)
    def deleted():
        return conv not in win.messages.threads and win.session.cache.rooms() == []
    assert _pump(win, deleted, timeout=30)
    assert win.session.cache.scan_last(conv, b_node) >= 2, "tombstone must advance past backlog"

    bob.send_room(members, conv, "the new one", ts + 2)

    def reappeared():
        return conv in win.messages.threads and \
            [m["text"] for m in win.session.cache.msgs(conv)] == ["the new one"]
    assert _pump(win, reappeared, timeout=30), "room should reappear with only the new message"


def test_clear_failure_aborts(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    b_node = app["b_node"]
    ts = int(time.time() * 1000)
    bob.send(app["session"].client.pubkey_hex, app["a_node"], "keep me", ts)
    assert _pump(win, lambda: bool(win.session.cache.msgs(b_node)), timeout=30)

    def boom(n, seq):
        raise RuntimeError("daemon down")
    monkeypatch.setattr(win.session.client, "history", boom)
    monkeypatch.setattr(win.messages, "_confirm_clear", lambda t: True)
    win.messages.selected = win.messages.threads[b_node]
    win.messages.on_clear_history()

    deadline = time.time() + 5
    while time.time() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)
    assert [m["text"] for m in win.session.cache.msgs(b_node)] == ["keep me"], \
        "failed clear must not wipe"
    texts = all_texts(win.messages.conversation)
    assert any("clear failed" in t for t in texts)


def test_room_tombstone_and_mute_units(tmp_path):
    c = Store(str(tmp_path / "state.db"), "pw")
    members = {"n1": {"node": "n1", "pubkey": "pk", "screen": "One"}}
    c.ensure_room("rr", members)
    c.ingest("rr", "n1", 4, 100, "hello")
    assert "rr" in c.rooms()

    c.delete_room("rr", {"n1": 9})
    assert c.rooms() == []
    assert c.messages("rr") == []
    assert c.cursor("n1") == 9

    # resurrection on a new message keeps the dismissed cursor
    c.ingest("rr", "n1", 10, 500, "new")
    assert "rr" in c.rooms()
    assert c.cursor("n1") == 9, "resurrection must not reset the dismissed cursor"

    assert c.is_conversation_muted("nope") is False
    c.mute_conversation("rr")
    assert c.is_conversation_muted("rr")
    c.unmute_conversation("rr")
    assert c.is_conversation_muted("rr") is False
    c.close()


def test_member_chips_add_and_jump(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    b_node = app["b_node"]
    carol_key = crypto.new_identity()
    carol_node = "cd" * 32
    chosen = [{"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
              {"node": carol_node, "pubkey": bytes(carol_key.verify_key).hex(), "screen": "Carol"}]
    win.messages.create_room(chosen)
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))
    win.messages.thread_list.select_row(win.messages.threads[conv]["row"])

    assert win.messages.member_chips.get_visible(), "chips hidden for a selected room"
    assert len(win.messages.member_chips.get_children()) == 2

    win.messages.on_member_chip(None, b_node, "Bob", known=True)
    assert win.messages.selected is win.messages.threads[b_node], "buddy chip should open the DM"

    win.messages.thread_list.select_row(win.messages.threads[conv]["row"])
    monkeypatch.setattr(win.messages, "_confirm_add_member", lambda s: True)
    win.messages.on_member_chip(None, carol_node, "Carol", known=False)

    contacts = protocol.load_contacts(str(app["home"] / "client-contacts.json"))
    assert "Carol" in contacts and contacts["Carol"]["node"] == carol_node
    assert carol_node in win.messages.threads, "added member should get a DM thread"
    assert win.messages.selected is win.messages.threads[carol_node], "should jump to the new DM"


def test_mute_room_silences_and_recovers(gtk_app):
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    a_node = app["a_node"]
    b_node = app["b_node"]
    carol_key = crypto.new_identity()
    members = [{"node": a_node, "pubkey": app["session"].client.pubkey_hex, "screen": "Alice"},
               {"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
               {"node": "cd" * 32, "pubkey": bytes(carol_key.verify_key).hex(), "screen": "Carol"}]
    chosen = members[1:]
    win.messages.create_room(chosen)
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))
    win.messages.thread_list.select_row(win.messages.threads[conv]["row"])

    win.messages.on_toggle_mute()
    assert win.session.cache.is_conversation_muted(conv)
    assert "Unmute…" in win.messages.mute_btn.get_label()
    assert win.messages.threads[conv]["row"].get_style_context().has_class("aimless-muted")

    ts = int(time.time() * 1000)
    bob.send_room(members, conv, "quiet msg", ts)

    def stored_quiet():
        return [m["text"] for m in win.session.cache.msgs(conv)] == ["quiet msg"]
    assert _pump(win, stored_quiet, timeout=30)
    assert win.messages.threads[conv]["unread"] == 0, "muted room must not count unread"
    assert win.messages.threads[conv]["preview"] == "", "muted room must not update preview"

    # an unknown member chatting in the muted room must not trigger a request popup
    stranger = "99" * 32
    payload = protocol.seal_message(
        win.session.identity, app["session"].client.pubkey_hex, "hi from stranger", ts + 5,
        screen="Newbie", conv=conv,
        members=members + [{"node": stranger, "pubkey": "ee" * 32, "screen": "Newbie"}])
    win.messages.incoming({"op": "recv", "from": stranger, "seq": 7, "payload": payload})
    assert not win.session.cache.pending(), "muted room must not pop requests"
    assert "hi from stranger" in [m["text"] for m in win.session.cache.msgs(conv)]

    # unmute → badges and previews resume
    win.messages.on_toggle_mute()
    assert not win.session.cache.is_conversation_muted(conv)
    win.messages.thread_list.select_row(None)  # unread only counts when not viewing
    bob.send_room(members, conv, "loud msg", ts + 10)

    def loud():
        t = win.messages.threads.get(conv)
        return t and t["unread"] == 1 and t["preview"] == "loud msg"
    assert _pump(win, loud, timeout=30)


def test_member_chips_render_visible_labels(gtk_app):
    """Regression: chips rendered as EMPTY buttons (btn.show() never showed the
    label child), leaving no way to tell who's who beyond the +N title truncation."""
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    b_node = app["b_node"]
    carol_key = crypto.new_identity()
    carol_node = "cd" * 32
    chosen = [{"node": b_node, "pubkey": bob.pubkey_hex, "screen": "Bob"},
              {"node": carol_node, "pubkey": bytes(carol_key.verify_key).hex(), "screen": "Carol"}]
    win.messages.create_room(chosen)
    conv = next(k for k, t in win.messages.threads.items() if t.get("is_room"))
    win.messages.thread_list.select_row(win.messages.threads[conv]["row"])

    chips = win.messages.member_chips
    assert chips.get_visible()
    texts, markup = [], []
    for fc in chips.get_children():
        btn = fc.get_child()
        lbl = btn.get_child()
        assert lbl is not None and lbl.get_visible(), "chip label must be visible"
        texts.append(lbl.get_text())
        markup.append(lbl.get_label())
    assert "Bob" in " ".join(texts) and "Carol" in " ".join(texts), texts
    joined = " ".join(markup)
    assert "●" in joined, "buddy chip uses a filled dot"
    assert "○" in joined, "non-buddy chip uses a hollow dot"


def test_unread_badge_increments(gtk_app):
    """Regression: the badge froze at 1 — it was only written when first created."""
    app = gtk_app
    win = app["win"]
    bob = app["bob"]
    b_node = app["b_node"]

    win.messages.thread_list.select_row(None)  # not viewing → unread counts
    ts = int(time.time() * 1000)
    for i in range(3):
        bob.send(app["session"].client.pubkey_hex, app["a_node"], f"msg {i}", ts + i)

    def badge_shows_three():
        t = win.messages.threads.get(b_node)
        badge = t and t["widgets"].get("badge")
        return badge is not None and badge.get_text() == "3"
    assert _pump(win, badge_shows_three, timeout=30), \
        f"badge text: {win.messages.threads[b_node]['widgets']['badge'].get_text()}"

    # opening the thread clears it
    win.messages.thread_list.select_row(win.messages.threads[b_node]["row"])
    assert win.messages.threads[b_node]["unread"] == 0
    assert win.messages.threads[b_node]["widgets"]["badge"] is None


def test_linkify_plain_text_is_escaped_only():
    assert gtkui.linkify("hello <world> & friends") == \
        GLib.markup_escape_text("hello <world> & friends")
    assert gtkui.linkify("no urls here") == "no urls here"


def test_linkify_single_url_makes_one_anchor():
    out = gtkui.linkify("read https://example.com/a?b=1&c=2 now")
    assert out.count("<a href=") == 1
    assert '<a href="https://example.com/a?b=1&amp;c=2">' in out
    assert "</a>" in out
    assert "read " in out and " now" in out


def test_linkify_non_http_schemes_never_linkified():
    for bad in ("javascript:alert(1)", "data:text/html,<b>hi</b>",
                "file:///etc/passwd", "chrome://settings", "aimless:x"):
        out = gtkui.linkify(f"click {bad} here")
        assert "<a href=" not in out, bad
        assert GLib.markup_escape_text(bad) in out


def test_linkify_trailing_punctuation_is_stripped():
    out = gtkui.linkify("check this: https://example.com.")
    assert out.count("<a href=") == 1
    assert '<a href="https://example.com">' in out, out
    out2 = gtkui.linkify("see https://example.com/x?y=1, now")
    assert '<a href="https://example.com/x?y=1">' in out2, out2


def test_linkify_trailing_punctuation_preserved_in_output():
    # the stripped punctuation must survive OUTSIDE the link, not vanish
    cases = {
        "see https://example.com. thanks": ["</a>.", "</a>."],
        "go to https://x.com, ok?": ["</a>,", "</a>,"],
        "did you see https://a.io? it's cool": ["</a>?", "</a>?"],
    }
    for msg, needles in cases.items():
        out = gtkui.linkify(msg)
        for n in needles:
            assert n in out, f"{msg!r}: {n!r} missing from {out!r}"
        # and the preceding text is intact
        for prefix in ("see ", "go to ", "did you see "):
            if msg.startswith(prefix):
                assert prefix in out


def test_linkify_injection_safety():
    # literal markup a peer types must render inert, never as a live anchor
    evil = '<a href="evil">click here</a>'
    out = gtkui.linkify(evil)
    assert "<a href=" not in out, "injected anchor must not survive"
    assert "&lt;a href=" in out, "input markup must be escaped to inert text"

    # a bare close/bare amp is harmless too
    out2 = gtkui.linkify("</&")
    assert "<a href=" not in out2
    assert "&lt;/&amp;" in out2

    # with a real URL alongside literal markup, exactly one anchor — the real one
    mixed = gtkui.linkify('<a href="evil">x</a> https://real.example')
    assert mixed.count("<a href=") == 1, mixed
    assert '<a href="https://real.example">' in mixed

    # a URL can't hide a quote to break the attribute — the regex stops at the
    # quote, so the href is clean and the rest is escaped text
    q = gtkui.linkify('https://example.com" onclick="bad')
    assert q.count("<a href=") == 1
    assert '<a href="https://example.com">' in q
    assert "&quot; onclick=&quot;bad" in q and q.index("</a>") < q.index("&quot;")


def test_on_link_activated_launches_and_logs(gtk_app, monkeypatch):
    app = gtk_app
    win = app["win"]
    calls = []
    monkeypatch.setattr(Gio.AppInfo, "launch_default_for_uri",
                        lambda uri, ctx: calls.append((uri, ctx)) or True)
    win.messages.on_link_activated("label", "https://example.com")
    assert calls == [("https://example.com", None)]

    # provider-style failure is caught and logged, not propagated
    def boom(uri, ctx):
        raise GLib.Error("no handler")

    monkeypatch.setattr(Gio.AppInfo, "launch_default_for_uri", boom)
    win.messages.on_link_activated("label", "https://bad.example")
    buf = win.activity.log_view.get_buffer()
    start, end = buf.get_bounds()
    assert "couldn't open link" in buf.get_text(start, end, False)

    # boolean false (no default handler) is logged too
    monkeypatch.setattr(Gio.AppInfo, "launch_default_for_uri",
                        lambda uri, ctx: False)
    win.messages.on_link_activated("label", "https://none.example")
    buf = win.activity.log_view.get_buffer()
    start, end = buf.get_bounds()
    assert "no default handler" in buf.get_text(start, end, False)


# --- 0.8.2 UX helpers -------------------------------------------------------

def test_clamp_to_workarea():
    areas = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]
    assert gtkui.clamp_to_workarea(100, 100, 800, 600, areas) == (100, 100)
    assert gtkui.clamp_to_workarea(2000, 200, 800, 600, areas) == (2000, 200), "second monitor kept"
    x, y = gtkui.clamp_to_workarea(9000, 9000, 800, 600, areas)
    assert 0 <= x <= 1920 and 0 <= y <= 1080, "off-screen window clamped on-screen"
    assert gtkui.clamp_to_workarea(5, 5, 800, 600, []) == (5, 5), "no monitors = unchanged"


def test_date_label_and_format_time():
    import datetime as _dt
    now = _dt.datetime(2024, 5, 10, 12, 0, 0)
    ts = lambda d: d.timestamp() * 1000
    assert gtkui.date_label(ts(_dt.datetime(2024, 5, 10, 9, 0)), now) == "Today"
    assert gtkui.date_label(ts(_dt.datetime(2024, 5, 9, 9, 0)), now) == "Yesterday"
    assert "2023" in gtkui.date_label(ts(_dt.datetime(2023, 1, 2, 9, 0)), now)
    t = 1700000000000
    assert ":" in gtkui.format_time(t, "24h")
    assert gtkui.format_time(t, "12h").upper().endswith(("AM", "PM"))


def test_should_notify_gating():
    p = {"notifications": True}
    assert gtkui.should_notify(False, False, False, p) is True
    assert gtkui.should_notify(True, False, False, p) is False, "focused window stays quiet"
    assert gtkui.should_notify(False, True, False, p) is False, "muted conversation stays quiet"
    assert gtkui.should_notify(False, False, True, p) is False, "blocked sender stays quiet"
    assert gtkui.should_notify(False, False, False, {"notifications": False}) is False


def test_near_bottom(gtk_app):
    m = gtk_app["win"].messages
    adj = m.conversation_scroll.get_vadjustment()
    adj.set_value(adj.get_upper() - adj.get_page_size())
    assert m._near_bottom(adj) is True
    if adj.get_upper() > adj.get_page_size() + 100:
        adj.set_value(0)
        assert m._near_bottom(adj) is False


def test_unread_indicator_title(gtk_app):
    win = gtk_app["win"]
    win.messages.threads["x"] = {"unread": 3}
    win.refresh_unread_indicator()
    assert "(3)" in win.get_title()
    win.messages.threads["x"]["unread"] = 0
    win.refresh_unread_indicator()
    assert "(" not in win.get_title()


def test_outgoing_text_delivery_tick(gtk_app):
    win = gtk_app["win"]
    m = win.messages
    conv = "n1"
    win.session.store.add_sent(conv, {conv: 7}, 100, "hi")
    m._render_messages(conv, {"conv": conv})
    assert any("sending" in lbl.get_text() for lbl in m._bubble_status.values())
    m.note_acked(conv, 7)
    assert any("delivered" in lbl.get_text() for lbl in m._bubble_status.values())


def test_attachment_open_button(gtk_app, tmp_path):
    win = gtk_app["win"]
    p = tmp_path / "a.txt"
    p.write_bytes(b"hi")
    box = Gtk.Box()
    win.messages._render_attachment_box(
        box, True, {"path": str(p), "filename": "a.txt", "mime_hint": "application", "size": 2})
    labels = [b.get_label() for b in box.get_children() if isinstance(b, Gtk.Button)]
    assert "Open" in labels and "Save" in labels


def test_window_geometry_saves(gtk_app):
    win = gtk_app["win"]
    win.prefs["remember_position"] = True
    win.resize(701, 502)
    pump(0.2)
    w, h = win.get_size()
    win.save_geometry()
    assert (win.prefs["window_width"], win.prefs["window_height"]) == (w, h)
    assert (w, h) != (0, 0)
    win.reset_geometry()
    assert "window_x" not in win.prefs and "window_maximized" not in win.prefs


def test_apply_saved_position_moves_and_clamps(gtk_app, monkeypatch):
    win = gtk_app["win"]
    calls = []
    monkeypatch.setattr(win, "move", lambda x, y: calls.append((x, y)))
    monkeypatch.setattr(win, "get_size", lambda: (1000, 700))
    monkeypatch.setattr(win, "_workareas",
                        lambda: [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)])
    win.prefs["window_x"], win.prefs["window_y"] = 1957, 0
    win._apply_saved_position()
    assert calls[-1] == (1957, 0), "saved second-monitor position is restored"
    calls.clear()
    win.prefs["window_x"], win.prefs["window_y"] = 9000, 9000
    win._apply_saved_position()
    x, y = calls[-1]
    assert x <= 1920 and y <= 1080, "off-screen saved position is clamped on-screen"


def test_on_map_applies_pending_geometry_once(gtk_app, monkeypatch):
    win = gtk_app["win"]
    applied = []
    monkeypatch.setattr(win, "_apply_saved_position", lambda: applied.append(1))
    win._want_geometry = True
    win._on_map()
    pump(0.2)
    assert applied, "map-time restore must apply the saved position"
    applied.clear()
    win._on_map()  # nothing pending now
    pump(0.2)
    assert applied == [], "geometry must not be re-applied on later map events"


def test_group_text_shows_partial_delivery(gtk_app):
    win = gtk_app["win"]
    m = win.messages
    conv = "roomx"
    win.session.store.add_sent(conv, {"n1": 1, "n2": 2, "n3": 3}, 100, "hi all")
    m._render_messages(conv, {"conv": conv})
    m.note_acked("n1", 1)
    m.note_acked("n2", 2)
    texts = [lbl.get_text() for lbl in m._bubble_status.values()]
    assert any("2/3" in t and "delivered" in t for t in texts), texts
    m.note_acked("n3", 3)
    texts = [lbl.get_text() for lbl in m._bubble_status.values()]
    assert any(t.startswith("✓ delivered") for t in texts), texts


# --- 0.8.5 theming ----------------------------------------------------------

REQUIRED_PALETTE_KEYS = {
    "name", "bg", "header", "surface", "sidebar", "sidebar_hover", "sidebar_selected",
    "button", "button_hover", "button_active", "border", "border_dark", "sep",
    "text", "text2", "text_bright", "muted", "muted2", "subtitle",
    "bubble_in", "bubble_in_fg", "bubble_out", "bubble_out_fg", "out_link",
    "badge_bg", "badge_fg", "focus", "log_text",
    "away_bg", "away_border", "away_text", "away_icon", "chip", "chip_hover",
    "online", "away", "offline", "danger", "link",
}


def test_themes_have_all_keys():
    assert set(gtkui.THEMES) == {"dark", "mocha", "nord", "tokyo", "latte", "dawn", "matrix", "amber"}
    for name, pal in gtkui.THEMES.items():
        missing = REQUIRED_PALETTE_KEYS - set(pal)
        assert not missing, f"{name} palette missing {missing}"


def test_build_css_uses_palette_and_mono():
    css = gtkui.build_css(gtkui.THEMES["matrix"])
    assert gtkui.THEMES["matrix"]["bg"] in css
    assert "monospace" in css, "Matrix must request the generic monospace family"
    assert "monospace" not in gtkui.build_css(gtkui.THEMES["dark"]), "default stays proportional"


def test_no_hardcoded_colors_outside_palette():
    import re
    src = open(gtkui.__file__).read()
    in_source = re.findall(r"#[0-9a-fA-F]{6}", src)
    in_palette = re.findall(r"#[0-9a-fA-F]{6}", repr(gtkui.THEMES))
    assert len(in_source) == len(in_palette), (
        "every colour must live in THEMES; found a stray hardcoded hex")


def test_theme_switch_and_env_override(gtk_app, monkeypatch):
    win = gtk_app["win"]
    gtkui.install_css_provider()
    win.apply_theme("matrix")
    assert gtkui.CURRENT_THEME == "matrix"
    assert gtkui.C["bg"] == "#000000"
    assert gtkui.CSS_PROVIDER is not None
    # AIMLESS_THEME wins over the selected theme.
    monkeypatch.setenv("AIMLESS_THEME", "amber")
    win.apply_theme("nord")
    assert gtkui.CURRENT_THEME == "amber"
    monkeypatch.delenv("AIMLESS_THEME")
    win.apply_theme("dark")
    assert gtkui.CURRENT_THEME == "dark"


def test_detect_system_palette_returns_a_theme():
    assert gtkui.detect_system_palette() in ("dark", "latte")


# --- 0.8.6 notification sounds ---------------------------------------------

def test_sound_command_options():
    assert gtkui.sound_command("off") == ""
    assert gtkui.sound_command("bogus") == ""
    assert gtkui.sound_command("single").count("speaker-test") == 1
    assert gtkui.sound_command("double").count("speaker-test") == 2
    assert gtkui.sound_command("triple").count("speaker-test") == 3
    assert "0.5s" in gtkui.sound_command("long")


def test_should_play_sound_gating():
    p = {"notification_sound": "double"}
    assert gtkui.should_play_sound(False, False, False, p) is True
    assert gtkui.should_play_sound(True, False, False, p) is False, "visible conversation stays quiet"
    assert gtkui.should_play_sound(False, True, False, p) is False, "muted stays quiet"
    assert gtkui.should_play_sound(False, False, True, p) is False, "blocked stays quiet"
    assert gtkui.should_play_sound(False, False, False, {"notification_sound": "off"}) is False


def test_sound_env_override(monkeypatch):
    monkeypatch.setenv("AIMLESS_SOUND", "triple")
    assert gtkui.sound_enabled({"notification_sound": "off"}) == "triple", "env wins"
    monkeypatch.setenv("AIMLESS_SOUND", "bogus")
    assert gtkui.sound_enabled({"notification_sound": "off"}) == "off", "invalid env ignored"
    monkeypatch.delenv("AIMLESS_SOUND")
    assert gtkui.sound_enabled({"notification_sound": "double"}) == "double"


def test_play_sound_throttle(gtk_app, monkeypatch):
    played = []
    monkeypatch.setattr(gtkui, "_run_beep", lambda cmd: played.append(cmd))
    gtkui._LAST_SOUND[0] = 0.0
    gtkui.play_notification_sound("single")
    gtkui.play_notification_sound("single")
    assert len(played) == 1, "a second alert within the gap is throttled"
    gtkui.play_notification_sound("single", force=True)
    assert len(played) == 2, "Test/forced sound bypasses the throttle"


def test_incoming_message_plays_sound_when_hidden(gtk_app, monkeypatch):
    win = gtk_app["win"]
    bob = gtk_app["bob"]
    a_node = gtk_app["a_node"]
    played = []
    monkeypatch.setattr(gtkui, "play_notification_sound", lambda kind, force=False: played.append(kind))
    monkeypatch.setattr(gtkui, "notify_new_message", lambda *a, **k: None)
    win.prefs["notification_sound"] = "single"
    win.messages.selected = None  # conversation not showing
    bob.send(win.session.client.pubkey_hex, a_node, "ping", int(time.time() * 1000))
    assert _pump(win, lambda: bool(played), timeout=30), "a hidden incoming message must beep"
