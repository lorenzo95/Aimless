#!/usr/bin/env python3
"""gtkui.py — aimless GTK desktop app.

Modes:
  aimless gui        messages window (manages the aimlessd daemon for you)
  aimless tray       tray daemon: supervises aimlessd, lives in the notification area
  aimless autostart  install autostart entry for `aimless tray`
"""

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import warnings
from datetime import datetime

warnings.filterwarnings("ignore", category=DeprecationWarning)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango

from . import crypto, protocol
from .daemon import DaemonClient, Client, DaemonError
from . import __version__ as client_version

APP_NAME = "AIMless"
CONFIG_DIR = os.environ.get("AIMLESS_CONFIG") or os.path.expanduser("~/.config/aimless")
TRAY_PID_FILE = os.path.join(CONFIG_DIR, "tray.pid")
AIMLESSD_PID_FILE = os.path.join(CONFIG_DIR, "aimlessd.pid")
PREFS_FILE = os.path.join(CONFIG_DIR, "gtk.json")
UI_CMD = [sys.executable, "-m", "aimless.cli", "gui"]


def data_dir():
    return os.environ.get("AIMLESS_HOME") or os.path.expanduser("~/.local/share/aimless")


def sock_path():
    return os.environ.get("AIMLESS_SOCK") or os.path.join(data_dir(), "api.sock")


def contacts_path():
    return os.path.join(data_dir(), "client-contacts.json")


def identity_path():
    return os.path.join(data_dir(), "identity.json")


def cache_path():
    return os.path.join(data_dir(), "cache.json.enc")

CSS = """
headerbar {
    background-image: none;
    background-color: #14161d;
    color: #e8eaf0;
    border-bottom: 1px solid #0d0e13;
    min-height: 40px;
}

.aimless-window { background-color: #191b22; }

.aimless-window button {
    background-image: none;
    background-color: #262a35;
    color: #dfe3ec;
    border: 1px solid #3a3e4a;
    border-radius: 8px;
}

.aimless-window button:hover { background-color: #2e3240; }
.aimless-window button:active { background-color: #33363f; }
.aimless-window button:checked { background-color: #33363f; }
.aimless-window button:disabled { opacity: 0.5; }

.aimless-send { padding: 10px 20px; }

stackswitcher {
    background-color: #1d2029;
    border-radius: 8px;
}

stackswitcher > button {
    background-image: none;
    background-color: transparent;
    border: none;
    box-shadow: none;
    color: #aab0bd;
    padding: 5px 14px;
    margin: 2px;
    border-radius: 6px;
    outline: none;
}

stackswitcher > button:checked {
    background-color: #33363f;
    color: #f2f4f8;
}

menu { background-color: #1e212b; color: #dfe3ec; border: 1px solid #3a3e4a; border-radius: 6px; }
menuitem { color: #dfe3ec; }
menuitem:hover { background-color: #33363f; }

.muted { opacity: 0.62; font-size: 90%; }

.aimless-sidebar scrolledwindow,
.aimless-sidebar list,
.aimless-sidebar row { background-color: #21242e; }

.aimless-sidebar row:hover { background-color: #262a35; }
.aimless-sidebar row:selected { background-color: #323748; }

.aimless-chat,
.aimless-chat stack,
.aimless-chat scrolledwindow,
.aimless-chat list,
.aimless-chat row { background-color: #191b22; }

.aimless-chat separator { background-color: #2a2d37; min-height: 1px; }

.aimless-bubble { padding: 8px 12px; border-radius: 14px; }
.aimless-bubble-in { background-color: #31343d; color: #e8eaf0; }
.aimless-bubble-out { background-color: #8ab4f8; color: #10131a; }

.aimless-badge {
    background-color: #7fa8f0;
    color: #10131a;
    border-radius: 10px;
    padding: 0 8px;
    font-size: 85%;
}

.aimless-composer-frame { background-color: #1e212b; border: 1px solid #3a3e4a; border-radius: 6px; }

.aimless-composer-frame textview,
.aimless-composer-frame textview text {
    background-color: transparent;
    color: #e8eaf0;
    caret-color: #e8eaf0;
}

.aimless-window entry {
    background-color: #1e212b;
    color: #e8eaf0;
    border: 1px solid #3a3e4a;
    border-radius: 6px;
    padding: 6px 10px;
}

.aimless-window entry:focus { border-color: #4a5060; }

.aimless-log text,
.aimless-log textview,
.aimless-log textview text {
    background-color: #14161d;
    color: #c8cdd8;
}

.aimless-route-bar {
    background-color: #16181f;
    border-top: 1px solid #2a2d37;
    color: #aab0bd;
}

.aimless-route-bar image { color: #aab0bd; }

.aimless-contacts frame { border-color: #3a3e4a; }
"""


def load_prefs():
    try:
        with open(PREFS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def first_icon(*names):
    theme = Gtk.IconTheme.get_default()
    for name in names:
        if theme.has_icon(name):
            return name
    return names[-1]


def scroll_to_bottom(scrolled):
    def _scroll():
        adj = scrolled.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False
    GLib.idle_add(_scroll)


def clear_children(container):
    container.foreach(lambda w: w.destroy())


def contacts_path():
    return os.path.join(data_dir(), "client-contacts.json")


def identity_path():
    return os.path.join(data_dir(), "identity.json")


def cache_path():
    return os.path.join(data_dir(), "cache.json.enc")


def daemon_binary():
    found = shutil.which("aimlessd")
    if found:
        return found
    local = os.path.expanduser("~/.local/bin/aimlessd")
    return local if os.path.exists(local) else None


class DaemonSupervisor:
    def __init__(self):
        self.datadir = data_dir()
        self.sock = sock_path()
        self.child = None

    def binary(self):
        return daemon_binary()

    def is_running(self):
        try:
            DaemonClient(self.sock).close()
            return True
        except Exception:
            return False

    def status(self):
        try:
            d = DaemonClient(self.sock)
            who = d.request("whoami", timeout=5)
            st = d.request("status", timeout=5)
            d.close()
            peers = st.get("peers", [])
            return {
                "address": who.get("address", ""),
                "pubkey": who.get("key", ""),
                "peers_up": sum(1 for p in peers if p.get("up")),
                "peers_total": len(peers),
                "build": st.get("build", ""),
                "mtu": st.get("mtu", 0),
            }
        except Exception:
            return None

    def spawn(self):
        binary = self.binary()
        if not binary:
            raise RuntimeError(
                "aimlessd not found — install it (aimless-dist/install.sh) or add it to PATH")
        self.child = subprocess.Popen(
            [binary, "-datadir", self.datadir],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(AIMLESSD_PID_FILE, "w") as f:
                f.write(str(self.child.pid))
        except Exception:
            pass
        return self.child.pid

    def ensure(self, log=None):
        if self.is_running():
            return True
        if log:
            log("starting aimlessd …")
        self.spawn()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.is_running():
                if log:
                    log("daemon running")
                return True
            if self.child and self.child.poll() is not None:
                raise RuntimeError("aimlessd exited immediately (check ~/.local/share/aimless)")
            time.sleep(0.2)
        raise RuntimeError("daemon did not come up within 20s")

    def stop(self):
        pid = read_pid(AIMLESSD_PID_FILE)
        if pid is None and self.child:
            pid = self.child.pid
        if pid is None:
            found = subprocess.run(["pgrep", "-x", "aimlessd"], capture_output=True, text=True)
            pids = [int(p) for p in found.stdout.split() if p.isdigit()]
            pid = pids[0] if pids else None
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 5
        stopped = False
        while time.time() < deadline:
            if not self.is_running():
                stopped = True
                break
            time.sleep(0.2)
        if not stopped and pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)
        try:
            os.remove(AIMLESSD_PID_FILE)
        except OSError:
            pass


class Session:
    def __init__(self, passphrase):
        self.identity = crypto.load_identity(identity_path(), passphrase)
        self.cache = crypto.Cache(cache_path(), passphrase)
        self.daemon = DaemonClient(sock_path())
        contacts = protocol.load_contacts(contacts_path())
        self.self_screen = contacts.get("_self", {}).get("screen", "anonymous")
        self.client = Client(self.daemon, self.identity, self.self_screen)
        self.pubkey_hex = self.client.pubkey_hex

    def contacts(self):
        allc = protocol.load_contacts(contacts_path())
        return {k: v for k, v in allc.items() if k != "_self"}

    def save_contacts(self, contacts, self_info):
        contacts["_self"] = self_info
        protocol.save_contacts(contacts_path(), contacts)

    def my_invite(self):
        who = self.client.whoami()
        return protocol.make_invite(self.identity, who["key"], self.self_screen)


def run_async(fn, on_done=None, on_error=None):
    def worker():
        try:
            result = fn()
        except Exception as e:
            result = e
            if on_error:
                GLib.idle_add(lambda: on_error(e) or False)
        else:
            if on_done:
                GLib.idle_add(lambda: on_done(result) or False)
    threading.Thread(target=worker, daemon=True).start()


class MessagesView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.app = app
        self.get_style_context().add_class("aimless-chat")
        self.threads = {}
        self.selected = None
        self._send_in_flight = False
        self._history_busy = False

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.get_style_context().add_class("aimless-sidebar")
        sidebar.set_size_request(240, -1)

        hint = Gtk.Label(label="Buddies")
        hint.set_xalign(0.0)
        hint.set_margin_start(10)
        hint.set_margin_top(8)
        hint.set_margin_bottom(4)
        hint.get_style_context().add_class("muted")
        sidebar.pack_start(hint, False, False, 0)

        self.thread_scroll = Gtk.ScrolledWindow()
        self.thread_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.thread_list = Gtk.ListBox()
        self.thread_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.thread_list.connect("row-selected", self.on_thread_selected)
        self.thread_scroll.add(self.thread_list)
        sidebar.pack_start(self.thread_scroll, True, True, 0)

        paned.pack1(sidebar, False, False)
        paned.set_position(240)

        self.stack = Gtk.Stack()

        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.set_halign(Gtk.Align.CENTER)
        ph_icon = Gtk.Image.new_from_icon_name(
            first_icon("mail-unread-symbolic", "dialog-information-symbolic"), Gtk.IconSize.DIALOG)
        ph_label = Gtk.Label(label="Select a conversation")
        ph_label.get_style_context().add_class("muted")
        placeholder.pack_start(ph_icon, False, False, 0)
        placeholder.pack_start(ph_label, False, False, 0)
        self.stack.add_titled(placeholder, "placeholder", "placeholder")

        conversation_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.set_border_width(10)
        self.conversation_header = Gtk.Label()
        self.conversation_header.set_ellipsize(Pango.EllipsizeMode.END)
        self.conversation_header.set_xalign(0.0)
        header_box.pack_start(self.conversation_header, True, True, 0)
        conversation_box.pack_start(header_box, False, False, 0)
        conversation_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        self.conversation_scroll = Gtk.ScrolledWindow()
        self.conversation_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.conversation = Gtk.ListBox()
        self.conversation.set_selection_mode(Gtk.SelectionMode.NONE)
        self.conversation_scroll.add(self.conversation)
        conversation_box.pack_start(self.conversation_scroll, True, True, 0)

        composer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        composer_box.set_border_width(8)
        self.composer = Gtk.TextView()
        self.composer.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.composer.set_size_request(-1, 48)
        self.composer.set_left_margin(8)
        self.composer.set_right_margin(8)
        self.composer.set_top_margin(6)
        self.composer.set_bottom_margin(6)
        self.composer.connect("key-press-event", self.on_composer_key)
        composer_frame = Gtk.Frame()
        composer_frame.set_shadow_type(Gtk.ShadowType.NONE)
        composer_frame.get_style_context().add_class("aimless-composer-frame")
        composer_frame.add(self.composer)
        composer_box.pack_start(composer_frame, True, True, 0)
        self.send_button = Gtk.Button(label="Send")
        self.send_button.set_valign(Gtk.Align.END)
        self.send_button.get_style_context().add_class("aimless-send")
        self.send_button.connect("clicked", lambda *_: self.send_message())
        composer_box.pack_start(self.send_button, False, False, 0)
        conversation_box.pack_start(composer_box, False, False, 0)

        self.stack.add_titled(conversation_box, "conversation", "conversation")
        self.stack.set_visible_child_name("placeholder")

        paned.pack2(self.stack, True, True)
        self.pack_start(paned, True, True, 0)

        self.sync_sidebar()

    def sync_sidebar(self):
        for node in list(self.threads):
            if node not in self.app.session.contacts():
                thread = self.threads.pop(node)
                if self.selected is thread:
                    self.selected = None
                    self.stack.set_visible_child_name("placeholder")
                if "row" in thread:
                    thread["row"].destroy()
        for petname, info in sorted(self.app.session.contacts().items()):
            node = info["node"]
            if node in self.threads:
                thread = self.threads[node]
                thread["petname"], thread["contact"] = petname, info
                thread["screen"] = info.get("screen", petname)
            else:
                thread = {
                    "node": node, "petname": petname, "contact": info, "online": False, "away": None,
                    "preview": "", "unread": 0, "screen": info.get("screen", petname),
                }
                self.threads[node] = thread
                self.append_thread_row(node, thread)
            self.update_thread_row(node)
        if self.selected:
            row = self.selected.get("row")
            if row:
                self.thread_list.select_row(row)

    def refresh_presence(self, presence):
        for node, thread in self.threads.items():
            p = presence.get(node, {})
            thread["online"] = p.get("online", False)
            away = None
            if p.get("status_payload"):
                try:
                    st = self.app.session.client.decrypt_status(p["status_payload"])
                    if st.get("away"):
                        away = st["away"]
                except (ValueError, KeyError):
                    pass
            thread["away"] = away
            self.update_thread_row(node)

    def append_thread_row(self, node, thread):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_border_width(10)

        dot_color = "#a6e3a1" if thread["online"] else ("#fab387" if thread["away"] else "#6c7086")
        title = Gtk.Label()
        title.set_markup(
            f"<span foreground='{dot_color}'>●</span>  <b>{GLib.markup_escape_text(thread['screen'])}</b>")
        title.set_xalign(0.0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_use_markup(True)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        labels.pack_start(title, False, False, 0)

        subtitle_text = thread["away"] if thread["away"] else thread["preview"]
        subtitle = Gtk.Label(label=subtitle_text)
        subtitle.set_xalign(0.0)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.set_single_line_mode(True)
        subtitle.get_style_context().add_class("muted")
        labels.pack_start(subtitle, False, False, 0)
        box.pack_start(labels, True, True, 0)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        meta.set_valign(Gtk.Align.START)
        badge = None
        if thread["unread"] > 0:
            badge = Gtk.Label()
            badge.set_markup(f"<b>{thread['unread']}</b>")
            badge.set_halign(Gtk.Align.END)
            badge.get_style_context().add_class("aimless-badge")
            meta.pack_start(badge, False, False, 0)
        box.pack_start(meta, False, False, 0)

        row.add(box)
        self.thread_list.add(row)
        row.show_all()
        thread["row"] = row
        thread["widgets"] = {"title": title, "subtitle": subtitle, "badge": badge}

    def update_thread_row(self, node):
        thread = self.threads.get(node)
        if not thread or "row" not in thread:
            return
        w = thread["widgets"]
        dot_color = "#a6e3a1" if thread["online"] else ("#fab387" if thread["away"] else "#6c7086")
        w["title"].set_markup(
            f"<span foreground='{dot_color}'>●</span>  <b>{GLib.markup_escape_text(thread['screen'])}</b>")
        w["subtitle"].set_text(thread["away"] if thread["away"] else thread["preview"])
        if thread["unread"] > 0 and not w["badge"]:
            badge = Gtk.Label()
            badge.set_markup(f"<b>{thread['unread']}</b>")
            badge.set_halign(Gtk.Align.END)
            badge.get_style_context().add_class("aimless-badge")
            meta_box = thread["row"].get_child().get_children()[-1]
            meta_box.pack_start(badge, False, False, 0)
            badge.show()
            w["badge"] = badge
        elif thread["unread"] == 0 and w["badge"]:
            w["badge"].destroy()
            w["badge"] = None

    def on_thread_selected(self, listbox, row):
        if row is None:
            self.stack.set_visible_child_name("placeholder")
            self.selected = None
            return
        node = next((n for n, t in self.threads.items() if t.get("row") is row), None)
        if node is None:
            return
        thread = self.threads[node]
        self.selected = thread
        thread["unread"] = 0
        self.update_thread_row(node)

        self.conversation_header.set_markup(
            f"<big><b>{GLib.markup_escape_text(thread['screen'])}</b></big>"
            f"  <span size='small' foreground='#8c8c8c'>{node[:16]}…</span>")
        clear_children(self.conversation)
        for m in sorted(self.app.session.cache.msgs(node), key=lambda m: (m["ts"], m["seq"])):
            self.append_bubble(m["dir"] == "out", m["text"], m["ts"])
        self.stack.set_visible_child_name("conversation")
        scroll_to_bottom(self.conversation_scroll)
        self.load_history_async(node)

    def load_history_async(self, node):
        if self._history_busy:
            return
        self._history_busy = True

        def worker():
            return self.app.session.client.history(node, self.app.session.cache.recv_last(node))

        def done(hist):
            self._history_busy = False
            self._history_loaded(node, hist)

        def fail(e):
            self._history_busy = False
            self._history_failed(e)

        run_async(worker, on_done=done, on_error=fail)

    def _history_loaded(self, node, hist):
        if self.selected is None or self.selected["node"] != node:
            return False
        for m in hist.get("msgs", []):
            try:
                opened = protocol.open_message(self.app.session.identity, m["payload"])
            except (ValueError, KeyError):
                continue
            self.app.session.cache.add_recv(node, m["seq"], opened["ts"], opened["text"])
        oldest = hist.get("oldest", 0)
        recv_last = self.app.session.cache.recv_last(node)
        if recv_last and oldest and recv_last + 1 < oldest:
            self.append_system_note(f"gap — messages before seq {oldest} were dropped by retention")
        clear_children(self.conversation)
        for m in sorted(self.app.session.cache.msgs(node), key=lambda m: (m["ts"], m["seq"])):
            self.append_bubble(m["dir"] == "out", m["text"], m["ts"])
        scroll_to_bottom(self.conversation_scroll)
        return False

    def _history_failed(self, e):
        self.append_system_note(f"history unavailable: {e}")
        return False

    def append_bubble(self, outgoing, text, ts):
        stamp = datetime.fromtimestamp(ts / 1000).strftime("%H:%M") if ts else ""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for attr, val in (("margin-start", 8), ("margin-end", 8), ("margin-top", 8), ("margin-bottom", 1)):
            box.set_property(attr, val)
        box.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        bubble = Gtk.Label()
        bubble.set_markup(GLib.markup_escape_text(text))
        bubble.set_line_wrap(True)
        bubble.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        bubble.set_max_width_chars(48)
        bubble.set_xalign(0.0)
        bubble.set_selectable(True)
        bubble.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        style = bubble.get_style_context()
        style.add_class("aimless-bubble")
        style.add_class("aimless-bubble-out" if outgoing else "aimless-bubble-in")
        box.pack_start(bubble, False, False, 0)
        if stamp:
            time_label = Gtk.Label(label=stamp)
            time_label.set_xalign(1.0 if outgoing else 0.0)
            time_label.get_style_context().add_class("muted")
            box.pack_start(time_label, False, False, 0)
        row.add(box)
        self.conversation.add(row)
        row.show_all()

    def append_system_note(self, text):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        note = Gtk.Label(label=text)
        note.set_line_wrap(True)
        note.set_xalign(0.5)
        note.get_style_context().add_class("muted")
        row.add(note)
        self.conversation.add(row)
        row.show_all()
        scroll_to_bottom(self.conversation_scroll)

    def on_composer_key(self, widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not (event.state & Gdk.ModifierType.SHIFT_MASK):
            self.send_message()
            return True
        return False

    def send_message(self):
        if self.selected is None:
            return
        buf = self.composer.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, True).strip()
        if not text:
            return
        if self._send_in_flight:
            return
        contact = self.selected["contact"]
        ts = int(time.time() * 1000)
        self._send_in_flight = True
        self.send_button.set_sensitive(False)
        buf.set_text("")

        def worker():
            return self.app.session.client.send(contact["pubkey"], contact["node"], text, ts)

        def done(resp):
            self._send_in_flight = False
            self.send_button.set_sensitive(True)
            self.app.session.cache.add_sent(contact["node"], resp.get("seq", 0), ts, text)
            self.append_bubble(True, text, ts)
            self.selected["preview"] = text
            self.update_thread_row(contact["node"])
            scroll_to_bottom(self.conversation_scroll)

        def fail(e):
            self._send_in_flight = False
            self.send_button.set_sensitive(True)
            self.append_system_note(f"⚠ send failed: {e} — the message was not queued")
            self.app.activity.log(f"send failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def incoming(self, ev):
        node = ev.get("from")
        thread = self.threads.get(node)
        if thread is None:
            self.sync_sidebar()
            thread = self.threads.get(node)
            if thread is None:
                return
        try:
            opened = self.app.session.client.decrypt_recv(ev)
        except (ValueError, KeyError):
            return
        self.app.session.cache.add_recv(node, ev.get("seq", 0), opened["ts"], opened["text"])
        thread["preview"] = opened["text"]
        if self.selected is thread:
            self.append_bubble(False, opened["text"], opened["ts"])
            scroll_to_bottom(self.conversation_scroll)
        else:
            thread["unread"] += 1
        self.update_thread_row(node)


class ContactsView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app = app
        self.set_border_width(14)
        self.get_style_context().add_class("aimless-contacts")

        invite_frame = Gtk.Frame(label="Your invite — send this to a friend")
        invite_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        invite_box.set_border_width(10)
        self.invite_entry = Gtk.Entry(editable=False)
        invite_box.pack_start(self.invite_entry, False, False, 0)
        copy_btn = Gtk.Button(label="Copy to clipboard")
        copy_btn.connect("clicked", self.on_copy_invite)
        invite_box.pack_start(copy_btn, False, False, 0)
        invite_frame.add(invite_box)
        self.pack_start(invite_frame, False, False, 0)

        add_frame = Gtk.Frame(label="Add a buddy — paste their invite")
        add_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        add_box.set_border_width(10)
        self.add_invite_entry = Gtk.Entry(placeholder_text="aimless1:<client-pk>:<node-pk>:<screen>")
        add_box.pack_start(self.add_invite_entry, False, False, 0)
        pet_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_petname_entry = Gtk.Entry(placeholder_text="Petname (optional)")
        pet_box.pack_start(self.add_petname_entry, True, True, 0)
        add_btn = Gtk.Button(label="Add buddy")
        add_btn.get_style_context().add_class("aimless-send")
        add_btn.connect("clicked", self.on_add)
        pet_box.pack_start(add_btn, False, False, 0)
        add_box.pack_start(pet_box, False, False, 0)
        self.add_status = Gtk.Label(label="")
        self.add_status.set_xalign(0.0)
        self.add_status.set_line_wrap(True)
        add_box.pack_start(self.add_status, False, False, 0)
        add_frame.add(add_box)
        self.pack_start(add_frame, False, False, 0)

        list_frame = Gtk.Frame(label="Buddies")
        self.buddy_scroll = Gtk.ScrolledWindow()
        self.buddy_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.buddy_scroll.set_size_request(-1, 240)
        self.buddy_list = Gtk.ListBox()
        self.buddy_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.buddy_scroll.add(self.buddy_list)
        list_frame.add(self.buddy_scroll)
        self.pack_start(list_frame, True, True, 0)

    def on_copy_invite(self, *_):
        cb = Gtk.Clipboard.get_default(Gdk.Display.get_default())
        cb.set_text(self.invite_entry.get_text(), -1)
        self.add_status.set_text("invite copied to clipboard")

    def refresh(self):
        clear_children(self.buddy_list)
        self._buddy_rows = {}
        contacts = self.app.session.contacts()
        for petname, info in sorted(contacts.items()):
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_border_width(8)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label()
            title.set_markup(f"<b>{GLib.markup_escape_text(info.get('screen', petname))}</b>")
            title.set_xalign(0.0)
            labels.pack_start(title, False, False, 0)
            sub = Gtk.Label(label=f"{petname} · {info['node'][:20]}…")
            sub.set_xalign(0.0)
            sub.get_style_context().add_class("muted")
            labels.pack_start(sub, False, False, 0)
            box.pack_start(labels, True, True, 0)
            rm = Gtk.Button(label="Remove")
            rm.connect("clicked", self.on_remove, petname)
            box.pack_start(rm, False, False, 0)
            row.add(box)
            self.buddy_list.add(row)
            row.show_all()
            self._buddy_rows[petname] = title
        self.refresh_presence(getattr(self, "_cached_presence", {}))

        def worker():
            invite = self.app.session.my_invite()
            presence = {p["key"]: p for p in self.app.session.client.presence(timeout=3)}
            return invite, presence

        def done(result):
            invite, presence = result
            self._cached_presence = presence
            self.invite_entry.set_text(invite)
            self.refresh_presence(presence)

        run_async(worker, on_done=done)

    def refresh_presence(self, presence):
        contacts = self.app.session.contacts()
        for petname, title in getattr(self, "_buddy_rows", {}).items():
            info = contacts.get(petname, {})
            p = presence.get(info.get("node"), {})
            color = "#a6e3a1" if p.get("online") else "#9aa0ad"
            state = "online" if p.get("online") else "offline"
            title.set_markup(
                f"<b>{GLib.markup_escape_text(info.get('screen', petname))}</b> "
                f"<span foreground='{color}' size='small'>{state}</span>")

    def on_add(self, *_):
        invite = self.add_invite_entry.get_text().strip()
        petname = self.add_petname_entry.get_text().strip()
        try:
            client_hex, node_hex, screen = protocol.parse_invite(invite)
        except ValueError as e:
            self.add_status.set_text(f"error: {e}")
            return
        if client_hex == self.app.session.pubkey_hex:
            self.add_status.set_text("error: that's your own invite — send it to a friend")
            return
        contacts = self.app.session.contacts()
        for k, c in contacts.items():
            if c.get("pubkey") == client_hex:
                if c.get("node") != node_hex:
                    allc = protocol.load_contacts(contacts_path())
                    allc[k]["node"] = node_hex
                    protocol.save_contacts(contacts_path(), allc)
                    self.add_status.set_text(f"updated routing key for {k}")
                    self.app.messages.sync_sidebar()
                    return
                self.add_status.set_text(f"already known as {k}")
                return
        allc = protocol.load_contacts(contacts_path())
        allc[petname or screen] = {"pubkey": client_hex, "node": node_hex, "screen": screen}
        protocol.save_contacts(contacts_path(), allc)
        run_async(lambda: self.app.session.client.add_contact(node_hex))
        self.add_invite_entry.set_text("")
        self.add_petname_entry.set_text("")
        self.add_status.set_text(f"added {screen}")
        self.refresh()
        self.app.messages.sync_sidebar()

    def on_remove(self, btn, petname):
        allc = protocol.load_contacts(contacts_path())
        allc.pop(petname, None)
        protocol.save_contacts(contacts_path(), allc)
        self.add_status.set_text(f"removed {petname}")
        self.refresh()
        self.app.messages.sync_sidebar()


class ActivityView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.app = app
        self.set_border_width(14)
        self.info_label = Gtk.Label(label="daemon: …")
        self.info_label.set_xalign(0.0)
        self.info_label.get_style_context().add_class("muted")
        self.pack_start(self.info_label, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.get_style_context().add_class("aimless-log")
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_left_margin(10)
        self.log_view.set_right_margin(10)
        scroll.add(self.log_view)
        self.pack_start(scroll, True, True, 0)
        self.info_label.set_markup("<span foreground='#9aa0ad'>○  checking daemon …</span>")

    def refresh_info(self, st):
        if not st:
            self.info_label.set_markup("<span foreground='#f38ba8'>●  offline — daemon not reachable</span>")
            return
        build = st.get("build", "")
        version_note = ""
        if not build:
            version_note = ("\n<span foreground='#f38ba8'>this daemon is an old build — "
                            "run `aimless stop`, then reopen aimless to update</span>")
            build = "unknown"
        state = ("<span foreground='#a6e3a1'>●  you are online</span>" if st["peers_up"] > 0
                 else "<span foreground='#fab387'>●  connecting — no Yggdrasil peers yet</span>")
        self.info_label.set_markup(
            f"{state}  —  address <b>{st['address']}</b>  ·  peers {st['peers_up']}/{st['peers_total']}\n"
            f"daemon: {build}  ·  client: aimless/{client_version}{version_note}")

    def log(self, line):
        stamp = datetime.now().strftime("%H:%M:%S")
        buf = self.log_view.get_buffer()
        buf.insert(buf.get_end_iter(), f"[{stamp}] {line}\n")
        scroll_to_bottom(self.log_view.get_parent())


class AimlessWindow(Gtk.Window):
    def __init__(self, session, supervisor):
        super().__init__(title=APP_NAME)
        self.session = session
        self.supervisor = supervisor
        self.get_style_context().add_class("aimless-window")
        self.set_default_icon_name(first_icon("user-available-symbolic", "phone", "applications-internet"))
        self.prefs = load_prefs()
        self.set_default_size(self.prefs.get("window_width", 1008), self.prefs.get("window_height", 723))
        self.connect("delete-event", self.on_delete)

        header_bar = Gtk.HeaderBar()
        header_bar.set_show_close_button(True)
        header_bar.set_title(APP_NAME)
        self.set_titlebar(header_bar)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.messages = MessagesView(self)
        self.stack.add_titled(self.messages, "messages", "Messages")

        self.contacts = ContactsView(self)
        self.stack.add_titled(self.contacts, "contacts", "Contacts")

        self.activity = ActivityView(self)
        self.stack.add_titled(self.activity, "activity", "Activity")

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        header_bar.set_custom_title(switcher)

        menu_button = Gtk.MenuButton()
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        header_bar.pack_end(menu_button)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.pack_start(self.stack, True, True, 0)

        route_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        route_bar.set_border_width(6)
        route_bar.get_style_context().add_class("aimless-route-bar")
        self.route_label = Gtk.Label(label="daemon: starting …")
        route_bar.pack_start(Gtk.Image.new_from_icon_name(
            first_icon("network-wireless-signal-excellent-symbolic", "applications-internet"), Gtk.IconSize.MENU),
            False, False, 0)
        route_bar.pack_start(self.route_label, False, False, 0)
        root.pack_start(route_bar, False, False, 0)
        self.add(root)

        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        for key, view in (("1", "messages"), ("2", "contacts"), ("3", "activity")):
            accel.connect(Gdk.keyval_from_name(key), Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE,
                          lambda *_, v=view: self.stack.set_visible_child_name(v))

        options_menu = Gtk.Menu()
        self.daemon_status_item = Gtk.MenuItem()
        status_label = Gtk.Label()
        status_label.set_halign(Gtk.Align.START)
        status_label.set_markup("<span foreground='#9aa0ad'>○  checking daemon …</span>")
        self.daemon_status_item.add(status_label)
        self.daemon_status_item.set_sensitive(False)
        self.daemon_status_item.connect("activate", lambda *_: self.stack.set_visible_child(self.activity))
        self._daemon_status_label = status_label
        options_menu.append(self.daemon_status_item)
        self.daemon_start_item = Gtk.MenuItem(label="Start aimlessd")
        self.daemon_start_item.connect("activate", lambda *_: self.on_start_daemon())
        options_menu.append(self.daemon_start_item)
        self.daemon_stop_item = Gtk.MenuItem(label="Stop aimlessd")
        self.daemon_stop_item.connect("activate", lambda *_: self.on_stop_daemon())
        options_menu.append(self.daemon_stop_item)
        options_menu.append(Gtk.SeparatorMenuItem())
        away_item = Gtk.MenuItem(label="Set away …")
        away_item.connect("activate", self.on_set_away)
        options_menu.append(away_item)
        avail_item = Gtk.MenuItem(label="Available")
        avail_item.connect("activate", lambda *_: self.set_away(None))
        options_menu.append(avail_item)
        options_menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: self.close())
        options_menu.append(quit_item)
        options_menu.show_all()
        options_menu.connect("show", lambda *_: self.refresh_daemon_menu())
        menu_button.set_popup(options_menu)

        self.connect("destroy", self.on_destroy)
        self._presence_busy = False
        self._status_busy = False
        self._daemon_user_stopped = False
        self.contacts.refresh()
        self.poll_status()

        self.stack.connect("notify::visible-child-name", self.on_view_changed)

        GLib.timeout_add(150, self.drain_events)
        GLib.timeout_add_seconds(3, self.poll_presence)
        GLib.timeout_add_seconds(5, self.poll_status)

        def watch_all():
            for info in self.session.contacts().values():
                try:
                    self.session.client.add_contact(info["node"])
                except DaemonError:
                    pass

        run_async(watch_all)

    def on_view_changed(self, stack, param):
        if stack.get_visible_child_name() == "contacts":
            self.contacts.refresh()

    def on_set_away(self, *_):
        away = ask_text(self, "Away message", "Away message (empty = available):")
        self.set_away(away.strip() if away and away.strip() else None)

    def set_away(self, away):
        contacts = list(self.session.contacts().values())

        def worker():
            errors = []
            for info in contacts:
                try:
                    self.session.client.set_status(info["pubkey"], info["node"], away)
                except Exception as e:
                    errors.append(f"{info.get('screen', '?')}: {e}")
            return errors

        def done(errors):
            if errors:
                self.activity.log("setstatus failed: " + "; ".join(errors))
            self.activity.log("away: " + away if away else "available")

        run_async(worker, on_done=done)

    def drain_events(self):
        try:
            while True:
                ev = self.session.daemon.next_event(timeout=0)
                if ev is None:
                    break
                if ev.get("op") == "recv":
                    self.messages.incoming(ev)
                elif ev.get("op") == "acked":
                    self.activity.log(f"delivered: seq {ev.get('seq')} → {str(ev.get('to'))[:16]}…")
        except Exception as e:
            try:
                self.activity.log(f"event error: {e}")
            except Exception:
                pass
        return True

    def refresh_daemon_menu(self):
        def worker():
            return self.supervisor.is_running(), self.supervisor.status()

        def done(result):
            running, st = result
            self.daemon_start_item.set_sensitive(not running)
            self.daemon_stop_item.set_sensitive(running)
            if not st:
                self._daemon_status_label.set_markup(
                    "<span foreground='#f38ba8'>○  daemon not running — use Start aimlessd</span>")
            elif st["peers_up"] == 0:
                self._daemon_status_label.set_markup(
                    f"<span foreground='#fab387'>●  daemon up — connecting… ({st['address']})</span>")
            else:
                build = st.get("build", "old build — run aimless stop + update")
                self._daemon_status_label.set_markup(
                    f"<span foreground='#a6e3a1'>✓  daemon up — online</span>  "
                    f"<span size='small'>{st['address']} · {build}</span>")

        run_async(worker, on_done=done)

    def on_start_daemon(self):
        self.daemon_start_item.set_sensitive(False)

        def worker():
            self.supervisor.ensure(log=self.activity.log)

        def done(_r):
            self.activity.log("aimlessd started")
            self.refresh_daemon_menu()

        def fail(e):
            self.activity.log(f"start failed: {e}")
            self.refresh_daemon_menu()

        run_async(worker, on_done=done, on_error=fail)

    def on_stop_daemon(self):
        self.daemon_stop_item.set_sensitive(False)
        self._daemon_user_stopped = True

        def worker():
            self.supervisor.stop()

        def done(_r):
            self.activity.log("aimlessd stopped")
            self.refresh_daemon_menu()
            self.refresh_route(None)

        run_async(worker, on_done=done)

    def poll_presence(self):
        if self._presence_busy:
            return True
        self._presence_busy = True

        def worker():
            return {p["key"]: p for p in self.session.client.presence(timeout=3)}

        def done(presence):
            self._presence_busy = False
            self.messages.refresh_presence(presence)
            self.contacts.refresh_presence(presence)

        def fail(_e):
            self._presence_busy = False
            presence = {}
            for t in self.messages.threads.values():
                t["online"] = False
            self.messages.refresh_presence(presence)

        run_async(worker, on_done=done, on_error=fail)
        return True

    def poll_status(self):
        if self._status_busy:
            return True
        self._status_busy = True

        def worker():
            running = self.supervisor.is_running()
            return running, self.supervisor.status()

        def done(result):
            self._status_busy = False
            running, st = result
            self.activity.refresh_info(st)
            self.refresh_route(st)

        def fail(_e):
            self._status_busy = False

        run_async(worker, on_done=done, on_error=fail)
        return True

    def refresh_route(self, st):
        if not st:
            self.route_label.set_markup("<span foreground='#f38ba8'>●  offline — daemon not reachable</span>")
        elif st["peers_up"] == 0:
            self.route_label.set_markup(
                "<span foreground='#fab387'>●  connecting — no Yggdrasil peers yet</span>")
        else:
            self.route_label.set_markup(
                f"<span foreground='#a6e3a1'>●  online</span>  —  {st['address']}  ·  "
                f"peers {st['peers_up']}/{st['peers_total']}")

    def on_delete(self, *_):
        self.save_geometry()
        self.hide()
        return True

    def on_destroy(self, *_):
        self.save_geometry()
        Gtk.main_quit()

    def save_geometry(self):
        w, h = self.get_size()
        self.prefs["window_width"] = w
        self.prefs["window_height"] = h
        save_prefs(self.prefs)


def ask_passphrase(parent):
    dlg = Gtk.Dialog(title="AIMless — passphrase", transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Unlock", Gtk.ResponseType.OK)
    dlg.set_default_size(360, 100)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label="Enter your passphrase to unlock your identity"))
    entry = Gtk.Entry(visibility=False, activates_default=True)
    box.add(entry)
    dlg.show_all()
    resp = dlg.run()
    text = entry.get_text()
    dlg.destroy()
    if resp == Gtk.ResponseType.OK and text:
        return text
    return None


def ask_text(parent, title, label):
    dlg = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
    dlg.set_default_size(420, 100)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label=label))
    entry = Gtk.Entry()
    box.add(entry)
    dlg.show_all()
    resp = dlg.run()
    text = entry.get_text()
    dlg.destroy()
    if resp == Gtk.ResponseType.OK:
        return text
    return None


def run_app():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS.encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    supervisor = DaemonSupervisor()
    try:
        supervisor.ensure()
    except RuntimeError as e:
        err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=str(e))
        err.run()
        err.destroy()
        return 1

    passphrase = ask_passphrase(None)
    if not passphrase:
        return 1
    for attempt in range(3):
        try:
            session = Session(passphrase)
            break
        except ValueError:
            err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                    text="wrong passphrase or corrupted identity — try again")
            err.run()
            err.destroy()
            passphrase = ask_passphrase(None)
            if not passphrase:
                return 1
    else:
        return 1

    win = AimlessWindow(session, supervisor)
    win.show_all()
    Gtk.main()
    return 0


def read_pid(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def tray_alive():
    import fcntl
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fh = open(TRAY_PID_FILE, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
        except OSError:
            try:
                with open(TRAY_PID_FILE) as f:
                    return int(f.read().strip())
            except Exception:
                return -1
        finally:
            fh.close()
    except Exception:
        return None


TRAY_SCRIPT_TEMPLATE = """
import gi, sys, os, signal, subprocess
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

pid_file  = {pid_file!r}
ui_cmd    = {ui_cmd!r}

def open_ui(*_):
    subprocess.Popen(ui_cmd, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def on_activate(icon, *_):
    open_ui()

def on_popup(icon, button, t):
    menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, t)

menu = Gtk.Menu()
mi_open = Gtk.MenuItem(label='Open AIMless')
mi_open.connect('activate', open_ui)
menu.append(mi_open)
mi_stop = Gtk.MenuItem(label='Stop aimlessd')
def _stop(*_):
    from aimless.gtkui import DaemonSupervisor
    DaemonSupervisor().stop()
mi_stop.connect('activate', _stop)
menu.append(mi_stop)
menu.append(Gtk.SeparatorMenuItem())
mi_quit = Gtk.MenuItem(label='Quit — also stops the daemon')
def _quit(*_):
    from aimless.gtkui import DaemonSupervisor
    DaemonSupervisor().stop()
    Gtk.main_quit()
mi_quit.connect('activate', _quit)
menu.append(mi_quit)
menu.show_all()

icon = Gtk.StatusIcon()
icon.set_from_icon_name({icon_name!r})
icon.set_title('AIMless')
icon.set_tooltip_text('AIMless — daemon running\\nLeft-click to open Messages')
icon.connect('activate', on_activate)
icon.connect('popup-menu', on_popup)
icon.set_visible(True)

def _heartbeat():
    try:
        with open(pid_file) as f:
            sup_pid = int(f.read().strip())
        os.kill(sup_pid, 0)
    except Exception:
        Gtk.main_quit()
        return False
    return True
GLib.timeout_add_seconds(10, _heartbeat)

Gtk.main()
"""


def spawn_tray():
    code = TRAY_SCRIPT_TEMPLATE.format(
        pid_file=TRAY_PID_FILE,
        ui_cmd=UI_CMD,
        icon_name=first_icon("user-available-symbolic", "phone"),
    )
    try:
        return subprocess.Popen(
            [sys.executable, "-c", code],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[aimless tray] unavailable: {e}")
        return None


def run_tray():
    import fcntl
    os.makedirs(CONFIG_DIR, exist_ok=True)
    pid_fh = open(TRAY_PID_FILE, "a+")
    try:
        fcntl.flock(pid_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pid = read_pid(TRAY_PID_FILE)
        print(f"[aimless tray] already running (pid {pid}) — stop it with: aimless stop")
        return 0
    pid_fh.seek(0)
    pid_fh.truncate()
    pid_fh.write(str(os.getpid()))
    pid_fh.flush()
    print(f"[aimless tray] PID {os.getpid()} — supervising aimlessd")

    supervisor = DaemonSupervisor()
    try:
        supervisor.ensure(log=lambda s: print(f"[aimless tray] {s}"))
    except RuntimeError as e:
        print(f"[aimless tray] {e}")
        return 1

    tray_proc = spawn_tray()
    loop = GLib.MainLoop()

    def cleanup(*_):
        print("[aimless tray] shutting down — stopping aimlessd")
        if tray_proc:
            try:
                tray_proc.terminate()
            except Exception:
                pass
        try:
            supervisor.stop()
        except Exception:
            pass
        try:
            pid_fh.close()
        except Exception:
            pass
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, cleanup)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, cleanup)
    loop.run()
    return 0


def install_autostart():
    autostart_dir = os.path.expanduser("~/.config/autostart")
    desktop_path = os.path.join(autostart_dir, "aimless-tray.desktop")
    aimless_bin = shutil.which("aimless") or os.path.expanduser("~/.local/bin/aimless")
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AIMless\n"
        "Comment=AIMless tray + daemon supervisor\n"
        f"Exec={aimless_bin} tray\n"
        "Icon=user-available\n"
        "Categories=Network;InstantMessaging;\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-XFCE-Autostart-Override=true\n"
        "Hidden=false\n"
    )
    os.makedirs(autostart_dir, exist_ok=True)
    with open(desktop_path, "w") as f:
        f.write(content)
    return desktop_path


def stop_all():
    stopped = []
    sup_pid = read_pid(TRAY_PID_FILE)
    if sup_pid:
        try:
            os.kill(sup_pid, signal.SIGTERM)
            stopped.append(f"tray supervisor (pid {sup_pid})")
        except OSError:
            pass
    try:
        os.remove(TRAY_PID_FILE)
    except OSError:
        pass
    supervisor = DaemonSupervisor()
    if supervisor.is_running():
        supervisor.stop()
        stopped.append("aimlessd")
    subprocess.run(["pkill", "-x", "aimlessd"], capture_output=True)
    subprocess.run(["pkill", "-f", "aimless.cli tray"], capture_output=True)
    subprocess.run(["pkill", "-f", "aimless.cli gui"], capture_output=True)
    return stopped


def main():
    args = sys.argv[1:]
    if "autostart" in args:
        print(install_autostart())
    elif "tray" in args:
        sys.exit(run_tray())
    else:
        sys.exit(run_app())


if __name__ == "__main__":
    main()
