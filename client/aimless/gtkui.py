#!/usr/bin/env python3
"""gtkui.py — aimless GTK desktop app.

One process: messages window + tray icon + the aimlessd daemon.

Modes:
  aimless            everything: tray icon, daemon, messages window
  aimless tray       starts hidden — tray icon only, window opens on first click (autostart)
  aimless gui        same as running aimless with no arguments
  aimless autostart  install login autostart entry for `aimless tray`
"""

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import traceback
import time
import warnings
from datetime import datetime

warnings.filterwarnings("ignore", category=DeprecationWarning)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango

from . import crypto, protocol, logging
from .daemon import DaemonClient, Client, DaemonError
from . import __version__ as client_version

APP_NAME = "AIMless"
CONFIG_DIR = os.environ.get("AIMLESS_CONFIG") or os.path.expanduser("~/.config/aimless")
APP_PID_FILE = os.path.join(CONFIG_DIR, "app.pid")
AIMLESSD_PID_FILE = os.path.join(CONFIG_DIR, "aimlessd.pid")
STATUS_REASSERT_SECONDS = 60
def prefs_file():
    return os.path.join(CONFIG_DIR, "gtk.json")


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

.aimless-window {
    background-image: none;
    background-color: #191b22;
    color: #e8eaf0;
}

.aimless-window label { color: #e8eaf0; }

.aimless-window .muted { color: #9aa0ad; }

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

.muted { color: #9aa0ad; font-size: 90%; }

.aimless-sidebar scrolledwindow,
.aimless-sidebar list,
.aimless-sidebar row { background-color: #21242e; }

.aimless-sidebar row:hover { background-color: #262a35; }
.aimless-sidebar row:selected { background-color: #323748; }
.aimless-sidebar row label { color: #dfe3ec; }

.aimless-chat row label { color: #e8eaf0; }

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

.aimless-away-banner {
    background-color: #3a3020;
    border-top: 1px solid #5a4a28;
    border-bottom: 1px solid #5a4a28;
    color: #f5d78e;
}

.aimless-away-banner image { color: #f5d36b; }

.aimless-route-bar {
    background-color: #16181f;
    border-top: 1px solid #2a2d37;
    color: #aab0bd;
}

.aimless-route-bar image { color: #aab0bd; }

.aimless-contacts frame { border-color: #3a3e4a; }

.aimless-muted {
    opacity: 0.55;
}

.aimless-chip {
    padding: 0px 4px;
}
"""


def load_prefs():
    try:
        with open(prefs_file()) as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(prefs_file(), "w") as f:
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
    names = ("aimlessd", "aimlessd-linux-amd64")
    dirs = [
        os.path.dirname(os.path.abspath(sys.argv[0])),
        os.getcwd(),
        os.path.expanduser("~/.local/bin"),
    ]
    seen = set()
    for d in dirs:
        if not d or d in seen:
            continue
        seen.add(d)
        for name in names:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
    found = shutil.which("aimlessd")
    return found


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
                "aimlessd not found — put aimlessd-linux-amd64 (or aimlessd) next to "
                "aimless.pyz, or add it to PATH")
        try:
            os.makedirs(self.datadir, exist_ok=True)
            daemon_log = open(os.path.join(self.datadir, "daemon.log"), "ab")
        except OSError:
            daemon_log = subprocess.DEVNULL
        self.child = subprocess.Popen(
            [binary, "-datadir", self.datadir],
            start_new_session=True,
            stdout=daemon_log,
            stderr=daemon_log,
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
        pid = daemon_pid_from_socket()
        if pid is None:
            pid = read_pid(AIMLESSD_PID_FILE)
        if pid is None and self.child:
            pid = self.child.pid
        if pid is None:
            pid = daemon_pid_from_procs()
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
        self.cache_recovered = None
        try:
            self.cache = crypto.Cache(cache_path(), passphrase)
        except Exception as e:
            try:
                os.replace(cache_path(), cache_path() + ".bad")
            except OSError:
                pass
            self.cache_recovered = f"corrupted cache recovered as .bad ({e})"
            self.cache = crypto.Cache(cache_path(), passphrase)
        self.daemon = DaemonClient(sock_path())
        self.self_node = self.daemon.request("whoami")["key"]
        contacts = protocol.load_contacts(contacts_path())
        for info in contacts.values():
            node = info.get("node")
            if node and self.cache.is_muted(node):
                self.cache.unmute(node)
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


def _room_dots_markup(members, presence_by_node, exclude):
    """One presence dot per room member (sorted like the title), excluding self."""
    parts = []
    for screen, node in sorted((m.get("screen") or n[:8], n) for n, m in members.items()
                               if n != exclude):
        p = presence_by_node.get(node, {})
        color = "#a6e3a1" if p.get("online") else ("#fab387" if p.get("away") else "#6c7086")
        parts.append(f"<span foreground='{color}'>●</span>")
    return "".join(parts)


def _sidebar_title_markup(thread, self_node):
    """Sidebar row title. Rooms: one liveness dot + online count (scales to any room
    size); per-member dots live in the conversation header instead. DMs: single dot."""
    esc = GLib.markup_escape_text
    if thread.get("is_room"):
        pb = thread.get("presence_by_node", {})
        others = [n for n in thread.get("members", {}) if n != self_node]
        online = sum(1 for n in others if pb.get(n, {}).get("online"))
        dot_color = "#a6e3a1" if online else "#6c7086"
        return (f"<span foreground='{dot_color}'>●</span>  "
                f"<span size='small' foreground='#8c8c8c'>{online}/{len(others)}</span>  "
                f"<b>{esc(thread['screen'])}</b>")
    dot_color = "#a6e3a1" if thread["online"] else ("#fab387" if thread["away"] else "#6c7086")
    return f"<span foreground='{dot_color}'>●</span>  <b>{esc(thread['screen'])}</b>"


def _room_header_markup(thread, self_node):
    dots = _room_dots_markup(thread.get("members", {}), thread.get("presence_by_node", {}), self_node)
    others = [n for n in thread.get("members", {}) if n != self_node]
    pb = thread.get("presence_by_node", {})
    online = sum(1 for n in others if pb.get(n, {}).get("online"))
    return (f"<big><b>{GLib.markup_escape_text(thread['screen'])}</b></big>  {dots}  "
            f"<span size='small' foreground='#8c8c8c'>{online}/{len(others)} online</span>")


def _free_petname(contacts, base):
    petname, i = base, 2
    while petname in contacts:
        petname = f"{base} {i}"
        i += 1
    return petname


def _add_contact_from_roster(node, pubkey, screen):
    contacts = protocol.load_contacts(contacts_path())
    petname = _free_petname(contacts, screen or node[:8])
    contacts[petname] = {"pubkey": pubkey, "node": node, "screen": screen or node[:8]}
    protocol.save_contacts(contacts_path(), contacts)
    return petname


def run_async(fn, on_done=None, on_error=None):
    def worker():
        try:
            result = fn()
        except Exception as exc:
            err = exc

            def deliver_error():
                on_error(err)
                return False

            if on_error:
                GLib.idle_add(deliver_error)
        else:
            if on_done:
                def deliver_done():
                    on_done(result)
                    return False

                GLib.idle_add(deliver_done)
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

        new_room_btn = Gtk.Button(label="＋ New room…")
        new_room_btn.set_relief(Gtk.ReliefStyle.NONE)
        new_room_btn.set_halign(Gtk.Align.START)
        new_room_btn.set_margin_start(6)
        new_room_btn.set_margin_bottom(4)
        new_room_btn.connect("clicked", self.on_new_room)
        sidebar.pack_start(new_room_btn, False, False, 0)

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
        self.clear_btn = Gtk.Button(label="Clear history…")
        self.clear_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.clear_btn.set_valign(Gtk.Align.START)
        self.clear_btn.get_style_context().add_class("muted")
        self.clear_btn.connect("clicked", self.on_clear_history)
        header_box.pack_end(self.clear_btn, False, False, 0)
        self.delete_btn = Gtk.Button(label="Delete room…")
        self.delete_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.delete_btn.set_valign(Gtk.Align.START)
        self.delete_btn.get_style_context().add_class("muted")
        self.delete_btn.set_no_show_all(True)
        self.delete_btn.connect("clicked", self.on_delete_room)
        header_box.pack_end(self.delete_btn, False, False, 0)
        self.mute_btn = Gtk.Button(label="Mute room…")
        self.mute_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.mute_btn.set_valign(Gtk.Align.START)
        self.mute_btn.get_style_context().add_class("muted")
        self.mute_btn.set_no_show_all(True)
        self.mute_btn.connect("clicked", self.on_toggle_mute)
        header_box.pack_end(self.mute_btn, False, False, 0)
        conversation_box.pack_start(header_box, False, False, 0)

        self.member_chips = Gtk.FlowBox()
        self.member_chips.set_selection_mode(Gtk.SelectionMode.NONE)
        self.member_chips.set_min_children_per_line(1)
        self.member_chips.set_max_children_per_line(12)
        self.member_chips.set_margin_start(8)
        self.member_chips.set_margin_top(2)
        self.member_chips.set_no_show_all(True)
        conversation_box.pack_start(self.member_chips, False, False, 0)
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
        session = self.app.session
        contacts = session.contacts()
        contact_nodes = {info["node"] for info in contacts.values()}
        for key in list(self.threads):
            thread = self.threads[key]
            if thread.get("is_room"):
                continue
            if thread["node"] not in contact_nodes:
                self.threads.pop(key)
                if self.selected is thread:
                    self.selected = None
                    self.stack.set_visible_child_name("placeholder")
                if "row" in thread:
                    thread["row"].destroy()
        for petname, info in sorted(contacts.items()):
            node = info["node"]
            if node in self.threads:
                thread = self.threads[node]
                thread["petname"], thread["contact"] = petname, info
                thread["screen"] = info.get("screen", petname)
            else:
                thread = {
                    "node": node, "conv": node, "is_room": False,
                    "petname": petname, "contact": info, "online": False, "away": None,
                    "preview": "", "unread": 0, "screen": info.get("screen", petname),
                }
                self.threads[node] = thread
                self.append_thread_row(node, thread)
            self.update_thread_row(node)
        for conv in session.cache.rooms():
            members = session.cache.members(conv)
            screens = sorted(m.get("screen") or n[:8] for n, m in members.items()
                             if n != session.self_node)
            title = ", ".join(screens[:3]) + (f" +{len(screens) - 3}" if len(screens) > 3 else "")
            if conv in self.threads:
                thread = self.threads[conv]
                thread["members"] = members
                thread["screen"] = title
            else:
                thread = {
                    "node": conv, "conv": conv, "is_room": True, "petname": None, "contact": None,
                    "members": members, "screen": title, "online": False, "away": None,
                    "preview": "", "unread": 0, "presence_by_node": {},
                }
                self.threads[conv] = thread
                self.append_thread_row(conv, thread)
            self.update_thread_row(conv)
        if self.selected:
            row = self.selected.get("row")
            if row:
                self.thread_list.select_row(row)

    def on_new_room(self, *_):
        contacts = self.app.session.contacts()
        if len(contacts) < 2:
            self.app.activity.log("a room needs at least two buddies — add more people first")
            return
        dlg = Gtk.Dialog(title="New room", transient_for=self.get_toplevel(), modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(360, 300)
        box = dlg.get_content_area()
        box.set_border_width(10)
        box.add(Gtk.Label(label="Pick the buddies to include (2 or more):"))
        checks = {}
        for petname, info in sorted(contacts.items()):
            cb = Gtk.CheckButton(label=info.get("screen", petname))
            box.add(cb)
            checks[info["node"]] = (cb, info)
        dlg.show_all()
        resp = dlg.run()
        chosen = [info for cb, info in checks.values() if cb.get_active()]
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        if len(chosen) < 2:
            self.append_system_note("a room needs at least two buddies")
            return
        self.create_room(chosen)

    def create_room(self, chosen_infos):
        session = self.app.session
        members = {session.self_node: {"node": session.self_node,
                                       "pubkey": session.client.pubkey_hex,
                                       "screen": session.self_screen}}
        for info in chosen_infos:
            members[info["node"]] = {"node": info["node"], "pubkey": info["pubkey"],
                                     "screen": info.get("screen", "")}
        conv = protocol.room_id(sorted(members.keys()))
        session.cache.ensure_room(conv, members)
        self.sync_sidebar()
        thread = self.threads.get(conv)
        if thread and "row" in thread:
            self.thread_list.select_row(thread["row"])

    def refresh_presence(self, presence):
        for conv, thread in self.threads.items():
            if thread.get("is_room"):
                pb = {}
                for n in thread["members"]:
                    if n == self.app.session.self_node:
                        continue
                    p = presence.get(n, {})
                    away = None
                    if p.get("status_payload"):
                        try:
                            st = self.app.session.client.decrypt_status(p["status_payload"])
                            if st.get("away"):
                                away = st["away"]
                        except (ValueError, KeyError):
                            pass
                    pb[n] = {"online": p.get("online", False), "away": away}
                thread["presence_by_node"] = pb
                thread["online"] = any(v["online"] for v in pb.values())
                thread["away"] = None
            else:
                p = presence.get(conv, {})
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
            self.update_thread_row(conv)
        sel = self.selected
        if sel is not None and sel.get("is_room"):
            self.conversation_header.set_markup(_room_header_markup(sel, self.app.session.self_node))
            self._render_member_chips(sel)

    def append_thread_row(self, node, thread):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_border_width(10)

        title = Gtk.Label()
        title.set_markup(_sidebar_title_markup(thread, self.app.session.self_node))
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
        w["title"].set_markup(_sidebar_title_markup(thread, self.app.session.self_node))
        muted = thread.get("is_room") and self.app.session.cache.is_conversation_muted(node)
        if muted:
            w["subtitle"].set_text("muted")
        else:
            w["subtitle"].set_text(thread["away"] if thread["away"] else thread["preview"])
        row_style = thread["row"].get_style_context()
        if muted:
            row_style.add_class("aimless-muted")
        else:
            row_style.remove_class("aimless-muted")
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
            self.delete_btn.hide()
            self.mute_btn.hide()
            self._render_member_chips(None)
            return
        conv = next((c for c, t in self.threads.items() if t.get("row") is row), None)
        if conv is None:
            return
        thread = self.threads[conv]
        self.selected = thread
        thread["unread"] = 0
        self.update_thread_row(conv)

        if thread.get("is_room"):
            self.conversation_header.set_markup(_room_header_markup(thread, self.app.session.self_node))
            self.delete_btn.show()
            self.mute_btn.show()
            self.mute_btn.set_label("Unmute room…" if self.app.session.cache.is_conversation_muted(conv)
                                    else "Mute room…")
            self._render_member_chips(thread)
        else:
            self.delete_btn.hide()
            self.mute_btn.hide()
            self._render_member_chips(None)
            self.conversation_header.set_markup(
                f"<big><b>{GLib.markup_escape_text(thread['screen'])}</b></big>"
                f"  <span size='small' foreground='#8c8c8c'>{conv[:16]}…</span>")
        clear_children(self.conversation)
        for m in sorted(self.app.session.cache.msgs(conv), key=lambda m: (m["ts"], min(m["seqs"].values()))):
            self.append_bubble(m["dir"] == "out", m["text"], m["ts"],
                               sender=None if m["dir"] == "out" else self._sender_label(thread, m))
        self.stack.set_visible_child_name("conversation")
        scroll_to_bottom(self.conversation_scroll)
        self.load_history_async(conv)

    def _sender_label(self, thread, m):
        sender = m.get("sender")
        if not sender or sender == "self":
            return None
        info = thread.get("members", {}).get(sender)
        if info:
            return info.get("screen") or sender[:8]
        contact = thread.get("contact")
        if contact and contact.get("node") == sender:
            return thread.get("screen")
        return sender[:8]

    def load_history_async(self, conv):
        if self._history_busy:
            return
        self._history_busy = True
        session = self.app.session
        thread = self.threads.get(conv)
        nodes = (list(thread["members"].keys()) if thread and thread.get("is_room") else [conv])

        def worker():
            # the daemon journals everything a buddy ever sent us in one stream per
            # sender; each conversation scans that stream and keeps only its own
            return {n: session.client.history(n, session.cache.scan_last(conv, n)) for n in nodes}

        def done(hists):
            self._history_busy = False
            self._history_loaded(conv, hists)

        def fail(e):
            self._history_busy = False
            self._history_failed(e)

        run_async(worker, on_done=done, on_error=fail)

    def _history_loaded(self, conv, hists):
        if self.selected is None or self.selected.get("conv") != conv:
            return False
        thread = self.selected
        for member, hist in hists.items():
            pre_scan = self.app.session.cache.scan_last(conv, member)
            max_seen = 0
            for m in hist.get("msgs", []):
                max_seen = max(max_seen, m["seq"])
                try:
                    opened = protocol.open_message(self.app.session.identity, m["payload"])
                except (ValueError, KeyError):
                    continue
                msg_conv = opened.get("conv") or member
                if msg_conv != conv:
                    continue
                self.app.session.cache.add_recv(conv, member, m["seq"], opened["ts"], opened["text"])
            if max_seen:
                self.app.session.cache.set_scan_last(conv, member, max_seen)
            oldest = hist.get("oldest", 0)
            if pre_scan and oldest and pre_scan + 1 < oldest:
                self.append_system_note(
                    f"gap — messages from {self._sender_label(thread, {'sender': member}) or member[:8]} "
                    f"before seq {oldest} were dropped by retention")
        clear_children(self.conversation)
        for m in sorted(self.app.session.cache.msgs(conv), key=lambda m: (m["ts"], min(m["seqs"].values()))):
            self.append_bubble(m["dir"] == "out", m["text"], m["ts"],
                               sender=None if m["dir"] == "out" else self._sender_label(thread, m))
        scroll_to_bottom(self.conversation_scroll)
        return False

    def _history_failed(self, e):
        self.append_system_note(f"history unavailable: {e}")
        return False

    def append_bubble(self, outgoing, text, ts, sender=None):
        stamp = datetime.fromtimestamp(ts / 1000).strftime("%H:%M") if ts else ""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for attr, val in (("margin-start", 8), ("margin-end", 8), ("margin-top", 8), ("margin-bottom", 1)):
            box.set_property(attr, val)
        box.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        if sender and not outgoing:
            who = Gtk.Label(label=sender)
            who.set_xalign(0.0)
            who.get_style_context().add_class("muted")
            box.pack_start(who, False, False, 0)
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

    def _confirm_clear(self, title):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Clear history with {title}?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "This conversation is emptied here and the existing history is dismissed — "
            "only messages that arrive after this are shown.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _confirm_delete(self, title):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Delete the room {title}?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "Messages are removed from this device and old history is dismissed — "
            "if someone sends to the room again, it reappears with only the new messages.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def on_delete_room(self, *_):
        thread = self.selected
        if thread is None or not thread.get("is_room"):
            return
        title = thread["screen"]
        if not self._confirm_delete(title):
            return
        conv = thread["conv"]
        session = self.app.session
        nodes = sorted(n for n in thread["members"] if n != session.self_node)

        def worker():
            return {n: session.client.history(n, 0).get("latest", 0) for n in nodes}

        def done(latests):
            session.cache.delete_room(conv, latests)
            gone = self.threads.pop(conv, None)
            if gone and "row" in gone:
                gone["row"].destroy()
            if self.selected is thread:
                self.selected = None
                self.stack.set_visible_child_name("placeholder")
                self.delete_btn.hide()
                self.mute_btn.hide()
            self.app.activity.log(f"deleted room {title}")

        def fail(e):
            self.append_system_note(f"delete failed: {e}")
            self.app.activity.log(f"delete failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def on_clear_history(self, *_):
        if self.selected is None:
            return
        thread = self.selected
        title = thread["screen"]
        if not self._confirm_clear(title):
            return
        session = self.app.session
        conv = thread["conv"]
        nodes = (sorted(n for n in thread["members"] if n != session.self_node)
                 if thread.get("is_room") else [conv])

        def worker():
            return {n: session.client.history(n, 0).get("latest", 0) for n in nodes}

        def done(latests):
            session.cache.clear_history(conv)
            for n, latest in latests.items():
                if latest:
                    session.cache.set_scan_last(conv, n, latest)
            thread["preview"] = ""
            if self.selected is thread:
                clear_children(self.conversation)
            self.update_thread_row(conv)
            self.app.activity.log(f"cleared history with {title}")

        def fail(e):
            self.append_system_note(f"clear failed: {e}")
            self.app.activity.log(f"clear failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def _confirm_add_member(self, screen):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                text=f"Add {screen} as a buddy?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "You'll be able to message them directly. Their identity is as claimed by "
            "whoever added them to this room — invites exchanged directly are stronger.")
        dlg.set_default_response(Gtk.ResponseType.OK)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _render_member_chips(self, thread):
        chips = self.member_chips
        for child in chips.get_children():
            chips.remove(child)
        if not thread or not thread.get("is_room"):
            chips.hide()
            return
        session = self.app.session
        contact_nodes = {info["node"] for info in session.contacts().values()}
        pb = thread.get("presence_by_node", {})
        entries = sorted((m.get("screen") or n[:8], n) for n, m in thread["members"].items()
                         if n != session.self_node)
        for screen, n in entries:
            p = pb.get(n, {})
            color = "#a6e3a1" if p.get("online") else ("#fab387" if p.get("away") else "#6c7086")
            known = n in contact_nodes
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            lbl = Gtk.Label()
            lbl.set_markup(f"<span foreground='{color}'>●</span> {GLib.markup_escape_text(screen)}")
            lbl.set_xalign(0.0)
            btn.add(lbl)
            btn.get_style_context().add_class("aimless-chip")
            btn.set_tooltip_text("Open conversation" if known else "Add as buddy")
            btn.connect("clicked", self.on_member_chip, n, screen, known)
            chips.add(btn)
            btn.show()
        chips.show()

    def on_member_chip(self, _btn, node, screen, known):
        if known:
            dm = self.threads.get(node)
            if dm and "row" in dm:
                self.thread_list.select_row(dm["row"])
            return
        if not self._confirm_add_member(screen):
            return
        info = (self.selected or {}).get("members", {}).get(node)
        if not info:
            return
        petname = _add_contact_from_roster(node, info.get("pubkey"), screen)
        self.app.session.cache.unmute(node)
        self.app.contacts.refresh()
        self.sync_sidebar()
        self.app.activity.log(f"added {petname} from the room")
        dm = self.threads.get(node)
        if dm and "row" in dm:
            self.thread_list.select_row(dm["row"])

    def on_toggle_mute(self, *_):
        thread = self.selected
        if thread is None or not thread.get("is_room"):
            return
        conv = thread["conv"]
        cache = self.app.session.cache
        if cache.is_conversation_muted(conv):
            cache.unmute_conversation(conv)
            self.app.activity.log(f"unmuted {thread['screen']}")
        else:
            cache.mute_conversation(conv)
            self.app.activity.log(f"muted {thread['screen']}")
        self.mute_btn.set_label("Unmute room…" if cache.is_conversation_muted(conv) else "Mute room…")
        self.update_thread_row(conv)

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
        thread = self.selected
        conv = thread["conv"]
        ts = int(time.time() * 1000)
        self._send_in_flight = True
        self.send_button.set_sensitive(False)
        buf.set_text("")

        if thread.get("is_room"):
            members = list(thread["members"].values())

            def worker():
                return self.app.session.client.send_room(members, conv, text, ts)

            def done(seqs):
                self._send_in_flight = False
                self.send_button.set_sensitive(True)
                self.app.session.cache.add_sent(conv, seqs, ts, text)
                self.append_bubble(True, text, ts)
                self.selected["preview"] = text
                self.update_thread_row(conv)
                scroll_to_bottom(self.conversation_scroll)
        else:
            contact = thread["contact"]

            def worker():
                return self.app.session.client.send(contact["pubkey"], contact["node"], text, ts)

            def done(resp):
                self._send_in_flight = False
                self.send_button.set_sensitive(True)
                self.app.session.cache.add_sent(conv, {contact["node"]: resp.get("seq", 0)}, ts, text)
                self.append_bubble(True, text, ts)
                self.selected["preview"] = text
                self.update_thread_row(conv)
                scroll_to_bottom(self.conversation_scroll)

        def fail(e):
            self._send_in_flight = False
            self.send_button.set_sensitive(True)
            self.append_system_note(f"⚠ send failed: {e} — the message was not queued")
            self.app.activity.log(f"send failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def incoming(self, ev):
        node = ev.get("from")
        try:
            opened = self.app.session.client.decrypt_recv(ev)
        except (ValueError, KeyError):
            return
        session = self.app.session
        conv = opened.get("conv") or node
        if opened.get("conv"):
            members = {}
            for m in opened.get("members", []):
                members[m["node"]] = {"node": m["node"], "pubkey": m["pubkey"],
                                      "screen": m.get("screen", "")}
            if members:
                session.cache.ensure_room(conv, members)
        if session.cache.is_conversation_muted(conv):
            session.cache.add_recv(conv, node, ev.get("seq", 0), opened["ts"], opened["text"])
            thread = self.threads.get(conv)
            if self.selected is thread and thread is not None:
                self.append_bubble(False, opened["text"], opened["ts"],
                                   sender=self._sender_label(thread, {"sender": node}))
                scroll_to_bottom(self.conversation_scroll)
            return
        contact_nodes = {info["node"] for info in session.contacts().values()}
        if node not in contact_nodes:
            if session.cache.is_muted(node):
                return
            session.cache.add_pending({
                "node": node, "pubkey": opened.get("from"),
                "screen": opened.get("screen") or node[:8],
                "conv": opened.get("conv"), "members": opened.get("members") or [],
                "seq": ev.get("seq", 0), "ts": opened["ts"], "text": opened["text"],
            })
            self.app.surface_pending_requests()
            return
        thread = self.threads.get(conv)
        if thread is None:
            self.sync_sidebar()
            thread = self.threads.get(conv)
            if thread is None:
                return
        session.cache.add_recv(conv, node, ev.get("seq", 0), opened["ts"], opened["text"])
        thread["preview"] = opened["text"]
        if self.selected is thread:
            self.append_bubble(False, opened["text"], opened["ts"],
                               sender=self._sender_label(thread, {"sender": node}))
            scroll_to_bottom(self.conversation_scroll)
        else:
            thread["unread"] += 1
        self.update_thread_row(conv)


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
    def __init__(self, session, supervisor, app_ref=None):
        super().__init__(title=APP_NAME)
        self.session = session
        self.supervisor = supervisor
        self.app_ref = app_ref
        self.get_style_context().add_class("aimless-window")
        self.set_default_icon_name(first_icon("user-available-symbolic", "phone", "applications-internet"))
        self.prefs = load_prefs()
        self.set_default_size(self.prefs.get("window_width", 1008), self.prefs.get("window_height", 723))
        self.connect("delete-event", self.on_delete)

        header_bar = Gtk.HeaderBar()
        header_bar.set_show_close_button(True)
        header_bar.set_title(APP_NAME)
        header_bar.set_subtitle(f"v{client_version}")
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

        self.away_banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.away_banner.set_border_width(8)
        self.away_banner.get_style_context().add_class("aimless-away-banner")
        self.away_icon = Gtk.Image.new_from_icon_name("weather-clear-night-symbolic", Gtk.IconSize.MENU)
        self.away_banner.pack_start(self.away_icon, False, False, 0)
        self.away_label = Gtk.Label(label="")
        self.away_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.away_label.set_xalign(0.0)
        self.away_banner.pack_start(self.away_label, True, True, 0)
        away_back = Gtk.Button(label="I'm back")
        away_back.set_relief(Gtk.ReliefStyle.NONE)
        away_back.connect("clicked", lambda *_: self.set_away(None))
        self.away_banner.pack_start(away_back, False, False, 0)
        self.away_banner.set_no_show_all(True)
        self.away_banner.hide()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.pack_start(self.away_banner, False, False, 0)
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
        away_item = Gtk.MenuItem(label="Set away …")
        away_item.connect("activate", self.on_set_away)
        options_menu.append(away_item)
        avail_item = Gtk.MenuItem(label="Available")
        avail_item.connect("activate", lambda *_: self.set_away(None))
        options_menu.append(avail_item)
        options_menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Close window")
        quit_item.connect("activate", lambda *_: self.close())
        options_menu.append(quit_item)
        options_menu.show_all()
        menu_button.set_popup(options_menu)

        self.connect("destroy", self.on_destroy)
        self._presence_busy = False
        self._status_busy = False
        self._request_open = False
        self._daemon_user_stopped = False
        saved_away = self.prefs.get("away", "")
        if saved_away:
            self._apply_away_banner(saved_away)
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

        self._push_status(self.prefs.get("away") or None)
        GLib.timeout_add_seconds(STATUS_REASSERT_SECONDS, self._reassert_status)
        GLib.idle_add(self.surface_pending_requests)

    def _ask_request(self, req):
        is_room = bool(req.get("conv"))
        if is_room:
            others = [m.get("screen") or m["node"][:8] for m in req.get("members", [])
                      if m["node"] != self.session.self_node]
            text = f"{req['screen']} invited you to a conversation with {', '.join(others)}"
        else:
            text = f"{req['screen']} wants to chat with you"
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text=text)
        dlg.format_secondary_text((req.get("text") or "")[:300])
        dlg.add_buttons("Deny", Gtk.ResponseType.REJECT, "Accept", Gtk.ResponseType.ACCEPT)
        dlg.set_default_response(Gtk.ResponseType.ACCEPT)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.ACCEPT

    def surface_pending_requests(self):
        if getattr(self, "_request_open", False):
            return GLib.SOURCE_REMOVE
        req = self.session.cache.pending_pop()
        if req is None:
            return GLib.SOURCE_REMOVE
        self._request_open = True
        try:
            accepted = self._ask_request(req)
        finally:
            self._request_open = False
        if accepted:
            petname = _add_contact_from_roster(req["node"], req["pubkey"], req.get("screen"))
            self.session.cache.unmute(req["node"])
            if req.get("conv"):
                members = {m["node"]: {"node": m["node"], "pubkey": m["pubkey"],
                                       "screen": m.get("screen", "")}
                           for m in req.get("members", [])}
                if members:
                    self.session.cache.ensure_room(req["conv"], members)
            self.session.cache.add_recv(req.get("conv") or req["node"], req["node"],
                                        req.get("seq", 0), req.get("ts", 0), req.get("text", ""))
            self.contacts.refresh()
            self.messages.sync_sidebar()
            self.activity.log(f"added {petname}")
        else:
            self.session.cache.mute(req["node"])
            self.activity.log(f"denied {req.get('screen') or req['node'][:8]}")
        GLib.idle_add(self.surface_pending_requests)
        return GLib.SOURCE_REMOVE

    def on_view_changed(self, stack, param):
        if stack.get_visible_child_name() == "contacts":
            self.contacts.refresh()

    def on_set_away(self, *_):
        away = ask_text(self, "Away message", "Away message (empty = available):")
        self.set_away(away.strip() if away and away.strip() else None)

    def set_away(self, away):
        self._apply_away_banner(away)
        self.prefs["away"] = away or ""
        save_prefs(self.prefs)
        self._push_status(away, log_status=True)

    def _push_status(self, away, log_status=False):
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
            if log_status:
                self.activity.log("away: " + away if away else "available")

        run_async(worker, on_done=done)

    def _reassert_status(self):
        """Status is ephemeral daemon RAM on both ends — re-announce the current
        status so buddies converge after any restart (ours or theirs)."""
        self._push_status(self.prefs.get("away") or None)
        return GLib.SOURCE_CONTINUE

    def _apply_away_banner(self, away):
        if away:
            self.away_icon.set_from_icon_name("weather-clear-night-symbolic", Gtk.IconSize.MENU)
            self.away_label.set_markup(
                f"<b>Away</b> — {GLib.markup_escape_text(away)}  "
                f"<span size='small'>(buddies see this as your away message)</span>")
            for child in self.away_banner.get_children():
                child.show()
            self.away_banner.show()
        else:
            self.away_banner.hide()

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
        if self.app_ref is not None and self.app_ref.tray is not None and self.app_ref.tray.have_tray:
            self.hide()
            return True
        return False

    def on_destroy(self, *_):
        self.save_geometry()
        if self.app_ref is not None:
            self.app_ref.quit()
        else:
            Gtk.main_quit()

    def save_geometry(self):
        w, h = self.get_size()
        self.prefs["window_width"] = w
        self.prefs["window_height"] = h
        save_prefs(self.prefs)


def ask_passphrase(parent):
    dlg = Gtk.Dialog(title="AIMless — passphrase", transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Unlock", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
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
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(420, 100)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label=label))
    entry = Gtk.Entry(activates_default=True)
    box.add(entry)
    dlg.show_all()
    resp = dlg.run()
    text = entry.get_text()
    dlg.destroy()
    if resp == Gtk.ResponseType.OK:
        return text
    return None


def _make_excepthook(gui_log):
    def hook(et, ev, tb):
        try:
            gui_log("uncaught: " + "".join(traceback.format_exception(et, ev, tb)))
        except Exception:
            pass
        sys.__excepthook__(et, ev, tb)
    return hook


def _make_thread_hook(gui_log):
    def hook(args):
        try:
            gui_log("thread crash: " + "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass
    return hook


def app_log_path():
    return os.path.join(CONFIG_DIR, "app.log")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_app_lock():
    """Single-instance guard. Returns (fh, None) when the lock was taken — hold the file
    handle for the process lifetime — or (None, holder_pid) when another instance runs."""
    import fcntl
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fh = open(APP_PID_FILE, "a+")
    except OSError:
        return None, -1
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None, read_pid(APP_PID_FILE) or -1
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh, None


class TrayIcon:
    def __init__(self, app):
        self.app = app
        self.have_tray = False
        self.menu = Gtk.Menu()
        mi_open = Gtk.MenuItem(label="Open AIMless")
        mi_open.connect("activate", self.on_open)
        self.menu.append(mi_open)
        self.menu.append(Gtk.SeparatorMenuItem())
        mi_quit = Gtk.MenuItem(label="Quit — shuts down AIMless")
        mi_quit.connect("activate", self.on_quit)
        self.menu.append(mi_quit)
        self.menu.show_all()
        try:
            self.icon = Gtk.StatusIcon()
            self.icon.set_from_icon_name(first_icon("user-available-symbolic", "phone"))
            self.icon.set_title(APP_NAME)
            self.icon.set_tooltip_text(f"{APP_NAME} — running\nLeft-click to open Messages")
            self.icon.connect("activate", self.on_open)
            self.icon.connect("popup-menu", self.on_popup)
            self.icon.set_visible(True)
            self.have_tray = True
        except Exception as e:
            app.log(f"tray icon unavailable ({e!r}) — running as a plain window app")

    def on_open(self, *_):
        try:
            self.app.open_window()
        except Exception as e:
            self.app.log(f"open failed: {e!r}")

    def on_quit(self, *_):
        try:
            self.app.quit()
        except Exception as e:
            self.app.log(f"shutdown error: {e!r}")

    def on_popup(self, icon, button, t):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, t)


class AimlessApp:
    """One process: messages window + tray icon + the aimlessd daemon."""

    def __init__(self):
        self.log = logging.log_fn(app_log_path())
        self.supervisor = DaemonSupervisor()
        self.session = None
        self.passphrase = None
        self.window = None
        self.tray = None
        self.lock_fh = None
        self.quitting = False
        self._unlocking = False

    def start(self, open_window):
        rc = self._setup(open_window)
        if rc is not None:
            return rc
        Gtk.main()
        return 0

    def _setup(self, open_window):
        sys.excepthook = _make_excepthook(self.log)
        threading.excepthook = _make_thread_hook(self.log)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.lock_fh, holder = acquire_app_lock()
        if self.lock_fh is None:
            self.log(f"another instance is running (pid {holder}) — presenting its window")
            if holder and holder > 0:
                try:
                    os.kill(holder, signal.SIGUSR1)
                except OSError:
                    pass
            return 0

        try:
            self.supervisor.ensure(log=self.log)
        except RuntimeError as e:
            err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=str(e))
            err.run()
            err.destroy()
            return 1

        self.tray = TrayIcon(self)
        if open_window or not self.tray.have_tray:
            self.open_window()

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self.on_open_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)
        GLib.timeout_add_seconds(5, self.poll)
        self.log(f"app started (pid {os.getpid()}, tray={self.tray.have_tray})")
        return None

    def on_open_signal(self, *_):
        self.open_window()
        return GLib.SOURCE_REMOVE

    def open_window(self):
        if self.window:
            self.window.deiconify()
            self.window.present()
            return
        if self._unlocking:
            return
        self._unlocking = True
        try:
            self._open_window_unlocked()
        finally:
            self._unlocking = False

    def _open_window_unlocked(self):
        session = None
        passphrase = self.passphrase
        if passphrase:
            try:
                session = Session(passphrase)
            except ValueError:
                passphrase = None
            except OSError as e:
                session = None
                self.log(f"daemon unreachable during unlock ({e}) — retrying")
        if not passphrase:
            for _attempt in range(3):
                passphrase = ask_passphrase(None)
                if not passphrase:
                    return
                try:
                    session = Session(passphrase)
                    break
                except ValueError:
                    err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                            text="wrong passphrase or corrupted identity — try again")
                    err.run()
                    err.destroy()
                except OSError as e:
                    err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                            text=f"daemon not reachable: {e}\nretry after starting aimless")
                    err.run()
                    err.destroy()
                    return
            else:
                return
        self.passphrase = passphrase
        if session.cache_recovered:
            self.log(f"cache recovery: {session.cache_recovered}")
        self.session = session
        self.window = AimlessWindow(session, self.supervisor, app_ref=self)
        self.window.show_all()

    def poll(self):
        if not self.quitting and not self.supervisor.is_running():
            self.log("aimlessd died — restarting")
            try:
                self.supervisor.ensure(log=self.log)
            except RuntimeError as e:
                self.log(f"restart failed: {e}")
                return GLib.SOURCE_CONTINUE
            self.rewatch()
        return GLib.SOURCE_CONTINUE

    def rewatch(self):
        if not self.session:
            return
        contacts = list(self.session.contacts().values())

        def worker():
            for info in contacts:
                try:
                    self.session.client.add_contact(info["node"])
                except Exception:
                    pass

        run_async(worker)

    def quit(self, *_):
        if self.quitting:
            return GLib.SOURCE_REMOVE
        self.quitting = True
        self.log("shutting down — stopping aimlessd")
        if self.window:
            try:
                self.window.save_geometry()
            except Exception:
                pass
        try:
            self.supervisor.stop()
        except Exception:
            pass
        if self.lock_fh:
            try:
                self.lock_fh.close()
            except Exception:
                pass
        try:
            os.remove(APP_PID_FILE)
        except OSError:
            pass
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE


def run_app(open_window=True):
    app = AimlessApp()
    return app.start(open_window=open_window)

def daemon_pid_from_socket():
    try:
        d = DaemonClient(sock_path())
        who = d.request("whoami", timeout=3)
        d.close()
        return int(who.get("pid", 0)) or None
    except Exception:
        return None


def daemon_pid_from_procs():
    """Last-resort stop() fallback: an aimlessd process using our datadir
    (covers daemons still booting, whose API socket is not up yet)."""
    target = data_dir()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cmdline = f.read().decode(errors="replace").split("\x00")
            except Exception:
                continue
            name = os.path.basename(cmdline[0])
            if not name.startswith("aimlessd"):
                continue
            if "-datadir" in cmdline:
                if target in cmdline:
                    return int(entry)
            elif target == os.path.expanduser("~/.local/share/aimless"):
                return int(entry)
    except Exception:
        pass
    return None


def read_pid(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None




def install_autostart():
    autostart_dir = os.path.join(os.path.dirname(CONFIG_DIR), "autostart")
    desktop_path = os.path.join(autostart_dir, "aimless-tray.desktop")

    exec_line = None
    aimless_bin = shutil.which("aimless") or os.path.expanduser("~/.local/bin/aimless")
    if os.path.exists(aimless_bin):
        exec_line = f"{aimless_bin} tray"
    else:
        for d in (os.path.dirname(os.path.abspath(sys.argv[0])), os.getcwd(),
                  os.path.expanduser("~/.local/bin")):
            pyz = os.path.join(d, "aimless.pyz")
            if os.path.exists(pyz):
                exec_line = f"{sys.executable} {pyz} tray"
                break
    if not exec_line:
        return None

    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AIMless\n"
        "Comment=AIMless tray + daemon — messages are received in the background\n"
        f"Exec={exec_line}\n"
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
    pid = read_pid(APP_PID_FILE)
    if pid and pid != os.getpid() and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(f"app (pid {pid})")
        except OSError:
            pass
        deadline = time.time() + 10
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.1)
    legacy_sup = read_pid(os.path.join(CONFIG_DIR, "tray.pid"))
    if legacy_sup and legacy_sup != pid and pid_alive(legacy_sup):
        try:
            os.kill(legacy_sup, signal.SIGTERM)
            stopped.append(f"tray supervisor (pid {legacy_sup})")
        except OSError:
            pass
    for legacy in ("tray.pid", "gui.pid", "session.json"):
        try:
            os.remove(os.path.join(CONFIG_DIR, legacy))
        except OSError:
            pass
    supervisor = DaemonSupervisor()
    if supervisor.is_running():
        supervisor.stop()
        stopped.append("aimlessd")
    subprocess.run(["pkill", "-x", "aimlessd"], capture_output=True)
    subprocess.run(["pkill", "-f", "aimless.cli gui"], capture_output=True)
    subprocess.run(["pkill", "-f", "aimless.cli tray"], capture_output=True)
    return stopped


def main():
    args = sys.argv[1:]
    if "autostart" in args:
        print(install_autostart())
    else:
        sys.exit(run_app(open_window="tray" not in args))


if __name__ == "__main__":
    main()
