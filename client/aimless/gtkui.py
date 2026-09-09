#!/usr/bin/env python3
"""gtkui.py - aimless GTK desktop app.

One process: messages window + tray icon + the aimlessd daemon.

Modes:
  aimless            everything: tray icon, daemon, messages window
  aimless tray       starts hidden - tray icon only, window opens on first click (autostart)
  aimless gui        same as running aimless with no arguments
  aimless autostart  install login autostart entry for `aimless tray`
"""

import base64
import hashlib
import json
import os
import re
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
from gi.repository import Gtk, GLib, Gdk, Pango, GdkPixbuf, Gio

from . import crypto, protocol, logging
from .daemon import DaemonClient, Client, DaemonError
from .ssh_tunnel import SSHTunnel
from . import __version__ as client_version
from . import MIN_DAEMON_BUILD

APP_NAME = "AIMless"
CONFIG_DIR = os.environ.get("AIMLESS_CONFIG") or os.path.expanduser("~/.config/aimless")
APP_PID_FILE = os.path.join(CONFIG_DIR, "app.pid")
AIMLESSD_PID_FILE = os.path.join(CONFIG_DIR, "aimlessd.pid")
STATUS_REASSERT_SECONDS = 60
# Shown to buddies while no GUI client is attached to the daemon (the
# always-on remote-daemon / SSH case). No emdash.
DEFAULT_OFFLINE_STATUS = "away - client offline"
def prefs_file():
    return os.path.join(CONFIG_DIR, "gtk.json")


def data_dir():
    return os.environ.get("AIMLESS_HOME") or os.path.expanduser("~/.local/share/aimless")


def attachments_dir():
    return os.path.join(data_dir(), "attachments")


def sanitize_filename(name):
    """Strict allowlist so a sender-controlled filename can't traverse paths."""
    clean = re.sub(r"[^A-Za-z0-9 ._-]", "", name or "")
    clean = clean.strip(" .")
    if not clean:
        clean = "file"
    return clean[:64]


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# Only http/https can ever become a link: the character class rules out the
# markup-breaking bytes (<, >, ", ') up front, and everything a peer can type
# that is NOT a matched URL is passed through markup_escape_text, so literal
# markup in a message can never produce a live anchor.
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"']+")
_TRAILING_PUNCT = ".,;:!?)']"


def linkify(text: str) -> str:
    """Escape `text` into Pango markup, turning bare http(s) URLs into anchors.
    `https://x.example.` drops the trailing period; everything else - including
    <a href=...> the sender literally typed - is escaped to inert text."""
    out = []
    pos = 0
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(_TRAILING_PUNCT)
        if not url:
            continue
        out.append(GLib.markup_escape_text(text[pos:m.start()]))
        esc = GLib.markup_escape_text(url)
        out.append(f'<a href="{esc}">{esc}</a>')
        # advance past the SHORTENED url so stripped trailing punctuation is not
        # silently dropped from the message - it's picked up by the next segment
        pos = m.start() + len(url)
    out.append(GLib.markup_escape_text(text[pos:]))
    return "".join(out)


def ssh_prefs():
    """Normalized SSH config from prefs. A configured host IS remote mode: the
    legacy 'enabled' flag is gone - old prefs that disabled SSH ({enabled:false})
    normalize to no config (local), and enabled+host just means host."""
    prefs = load_prefs()
    ssh = prefs.get("ssh") if isinstance(prefs.get("ssh"), dict) else {}
    ssh = dict(ssh)
    if ssh.get("enabled") is False:
        return {}
    if not ssh.get("host"):
        return {}
    if not ssh.get("remote_socket"):
        return {}
    return ssh


def ssh_tunnel():
    """SSHTunnel for the configured remote daemon, or None when SSH mode is off."""
    ssh = ssh_prefs()
    if not ssh:
        return None
    local = ssh.get("local_socket") or os.path.join(CONFIG_DIR, "remote-api.sock")
    return SSHTunnel(ssh["host"], ssh["remote_socket"], local,
                     identity=ssh.get("identity") or None)


def sock_path():
    env = os.environ.get("AIMLESS_SOCK")
    if env:
        return env
    if ssh_tunnel() is not None:
        # SSH mode: the tunnel creates this local socket (remote-api.sock),
        # never the local daemon's api.sock, so the two can't collide.
        return ssh_tunnel().local_socket
    return os.path.join(data_dir(), "api.sock")


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
.aimless-bubble-in { background-color: #31343d; }
.aimless-bubble-out { background-color: #8ab4f8; }
.aimless-chat row .aimless-bubble-in { color: #e8eaf0; }
.aimless-chat row .aimless-bubble-out { color: #10131a; }
.aimless-bubble-out link, .aimless-bubble-out link:visited { color: #0b3d91; }
.aimless-bubble-in link, .aimless-bubble-in link:visited { color: #8ab4f8; }

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
    padding: 2px 8px;
    margin: 1px;
    border-radius: 11px;
    background-color: #22252e;
}

.aimless-chip:hover {
    background-color: #2c313d;
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
    state = {"upper": None}

    def _settle():
        try:
            adj = scrolled.get_vadjustment()
            if adj is None:
                return False
            upper = adj.get_upper()
            adj.set_value(upper - adj.get_page_size())
            # A row added just before this call is allocated by the frame clock,
            # which fires a display tick AFTER this timeout runs - so the first
            # read of `upper` is one row behind. Re-settle on a tick boundary
            # until the size stops changing and the view sits at the true bottom.
            if upper == state["upper"]:
                return False
            state["upper"] = upper
        except Exception:
            return False
        return True

    GLib.timeout_add(30, _settle)
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
        self.tunnel = ssh_tunnel()
        # Snapshot of the ssh config this supervisor was built from, so callers
        # can detect a later change and rebuild instead of reusing a stale
        # connection (e.g. SSH configured from the first-run/unlock dialogs).
        self.ssh_cfg = ssh_prefs()

    def stale(self):
        """True when the current prefs no longer match this supervisor."""
        return ssh_prefs() != self.ssh_cfg

    @property
    def remote(self):
        return self.tunnel is not None

    def binary(self):
        return daemon_binary()

    def is_running(self):
        # Cheap pre-check for remote mode: if the tunnel itself isn't even up,
        # don't pay for a round trip. But a bare connect through a live tunnel
        # proves nothing about the daemon behind it - SSH creates the local
        # forward listener as soon as it authenticates, even if the remote
        # socket path is wrong or nothing answers there. So "is it working"
        # always requires a genuine whoami reply, local and remote alike.
        if self.remote and not self.tunnel.is_ready():
            return False
        d = None
        try:
            d = DaemonClient(self.sock)
            d.request("whoami", timeout=5)
            return True
        except Exception:
            return False
        finally:
            if d is not None:
                d.close()

    def status(self):
        d = None
        try:
            d = DaemonClient(self.sock)
            who = d.request("whoami", timeout=5)
            st = d.request("status", timeout=5)
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
        finally:
            if d is not None:
                d.close()

    def spawn(self):
        binary = self.binary()
        if not binary:
            raise RuntimeError(
                "aimlessd not found - put aimlessd-linux-amd64 (or aimlessd) next to "
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
        if daemon_log is not subprocess.DEVNULL:
            daemon_log.close()  # the child inherited its own copy
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
        if self.remote:
            if log:
                log(f"connecting to remote daemon via ssh ({self.tunnel.host})")
            try:
                self.tunnel.start(log=log)
            except RuntimeError as e:
                if log:
                    log(f"ssh tunnel failed: {e}")
                raise
            if self.is_running():
                if log:
                    log("remote daemon connected")
                return True
            raise RuntimeError("ssh tunnel is up but the remote daemon is not answering")
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
        if self.remote:
            self.tunnel.stop()
            try:
                os.remove(AIMLESSD_PID_FILE)
            except OSError:
                pass
            return
        # Prefer the process we spawned (instant, no network round trip).
        # daemon_pid_from_socket() does a whoami - never use that as the
        # liveness check after SIGTERM, because a dying daemon can't answer and
        # the request stalls up to timeout+grace (the 10s hang on quit).
        if self.child is not None:
            pid = self.child.pid
        else:
            pid = daemon_pid_from_socket()
            if pid is None:
                pid = read_pid(AIMLESSD_PID_FILE)
            if pid is None:
                pid = daemon_pid_from_procs()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        def alive():
            if self.child is not None:
                return self.child.poll() is None
            try:
                os.kill(pid, 0)  # signal 0 = existence probe, instant
                return True
            except OSError:
                return False

        deadline = time.time() + 5
        stopped = False
        while time.time() < deadline:
            if not alive():
                stopped = True
                break
            time.sleep(0.2)
        if not stopped and pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)
        if self.child is not None:
            try:
                self.child.wait(timeout=2)
            except Exception:
                pass
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
        self._record_pair()

    def contacts(self):
        allc = protocol.load_contacts(contacts_path())
        return {k: v for k, v in allc.items() if k != "_self"}

    def save_contacts(self, contacts, self_info):
        contacts["_self"] = self_info
        protocol.save_contacts(contacts_path(), contacts)

    def my_invite(self):
        who = self.client.whoami()
        return protocol.make_invite(self.identity, who["key"], self.self_screen)

    def _record_pair(self):
        """Remember the (client identity, daemon node key) pair just used, so a
        later connect can detect an address change that would strand contacts."""
        try:
            prefs = load_prefs()
            prefs["last_node_key"] = self.self_node
            save_prefs(prefs)
        except Exception:
            pass

    def node_key_mismatch(self):
        """True when this identity was last seen on a different daemon node AND
        the user has contacts - i.e. their buddies' invites point at the old
        address, so messages won't reach them here."""
        try:
            last = load_prefs().get("last_node_key")
        except Exception:
            last = None
        if not last or last == self.self_node:
            return False
        if not any(k != "_self" for k in self.contacts()):
            return False
        return True


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
        self._catchup_busy = False
        self._file_bufs = {}

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

        new_room_btn = Gtk.Button()
        new_room_btn.set_relief(Gtk.ReliefStyle.NONE)
        new_room_btn.set_halign(Gtk.Align.START)
        new_room_btn.set_margin_start(6)
        new_room_btn.set_margin_bottom(4)
        new_room_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        new_room_box.pack_start(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        new_room_box.pack_start(Gtk.Label(label="New room…"), False, False, 0)
        new_room_btn.add(new_room_box)
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
        self.block_btn = Gtk.Button(label="Block…")
        self.block_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.block_btn.set_valign(Gtk.Align.START)
        self.block_btn.get_style_context().add_class("muted")
        self.block_btn.set_no_show_all(True)
        self.block_btn.connect("clicked", self.on_block)
        header_box.pack_end(self.block_btn, False, False, 0)
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
        self.attach_button = Gtk.Button(label="Attach")
        self.attach_button.set_valign(Gtk.Align.END)
        self.attach_button.get_style_context().add_class("muted")
        self.attach_button.connect("clicked", self.on_attach)
        composer_box.pack_start(self.attach_button, False, False, 0)
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
            self.app.activity.log("a room needs at least two buddies - add more people first")
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
        muted = self.app.session.cache.is_conversation_muted(node)
        if muted:
            w["subtitle"].set_text("muted")
        else:
            w["subtitle"].set_text(thread["away"] if thread["away"] else thread["preview"])
        row_style = thread["row"].get_style_context()
        if muted:
            row_style.add_class("aimless-muted")
        else:
            row_style.remove_class("aimless-muted")
        if thread["unread"] > 0:
            if not w["badge"]:
                badge = Gtk.Label()
                badge.set_markup(f"<b>{thread['unread']}</b>")
                badge.set_halign(Gtk.Align.END)
                badge.get_style_context().add_class("aimless-badge")
                meta_box = thread["row"].get_child().get_children()[-1]
                meta_box.pack_start(badge, False, False, 0)
                badge.show()
                w["badge"] = badge
            else:
                w["badge"].set_markup(f"<b>{thread['unread']}</b>")
        elif thread["unread"] == 0 and w["badge"]:
            w["badge"].destroy()
            w["badge"] = None

    def catchup_unread(self):
        """One-time startup sweep: count messages that arrived while no client was
        connected (the daemon keeps running between app sessions in the container,
        and recv events broadcast to nobody are lost) as unread.

        Fetches history past each conversation's scan cursor and adds anything new
        to the cache WITHOUT advancing the cursor - so opening a thread still
        fetches and shows the messages (and clears the badge)."""
        if self._catchup_busy:
            return
        self._catchup_busy = True
        session = self.app.session
        jobs = []
        for conv, thread in list(self.threads.items()):
            nodes = list(thread.get("members", {}).keys()) if thread.get("is_room") else [conv]
            for n in nodes:
                jobs.append((conv, n))

        def worker():
            return [(conv, n, session.client.history(n, session.cache.scan_last(conv, n)))
                    for conv, n in jobs]

        def done(results):
            self._catchup_busy = False
            new_by_conv = {}
            for conv, member, hist in results:
                if not hist or not hist.get("msgs"):
                    continue
                count = 0
                for m in hist["msgs"]:
                    try:
                        opened = protocol.open_message(session.identity, m["payload"])
                    except (ValueError, KeyError):
                        continue
                    msg_conv = opened.get("conv") or member
                    if msg_conv != conv:
                        continue
                    if session.cache.add_recv(conv, member, m["seq"], opened["ts"], opened["text"]):
                        count += 1
                if count:
                    new_by_conv[conv] = new_by_conv.get(conv, 0) + count
            for conv, count in new_by_conv.items():
                thread = self.threads.get(conv)
                if thread is None or self.selected is thread:
                    continue
                thread["unread"] += count
                self.update_thread_row(conv)
            if new_by_conv:
                self.app.activity.log(
                    f"unread sweep: {sum(new_by_conv.values())} message(s) arrived while you were away")

        def fail(_e):
            self._catchup_busy = False

        run_async(worker, on_done=done, on_error=fail)

    def on_thread_selected(self, listbox, row):
        if row is None:
            self.stack.set_visible_child_name("placeholder")
            self.selected = None
            self.delete_btn.hide()
            self.mute_btn.hide()
            self.block_btn.hide()
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
            self.mute_btn.set_label("Unmute…" if self.app.session.cache.is_conversation_muted(conv) else "Mute…")
            self.block_btn.hide()
            self._render_member_chips(thread)
        else:
            self.delete_btn.hide()
            self.mute_btn.show()
            self.mute_btn.set_label("Unmute…" if self.app.session.cache.is_conversation_muted(conv) else "Mute…")
            self.block_btn.show()
            self.block_btn.set_label("Block…")
            self._render_member_chips(None)
            self.conversation_header.set_markup(
                f"<big><b>{GLib.markup_escape_text(thread['screen'])}</b></big>"
                f"  <span size='small' foreground='#8c8c8c'>{conv[:16]}…</span>")
        clear_children(self.conversation)
        for m in sorted(self.app.session.cache.msgs(conv), key=lambda m: (m["ts"], min(m["seqs"].values()))):
            self.append_bubble(m["dir"] == "out", m["text"], m["ts"],
                               sender=None if m["dir"] == "out" else self._sender_label(thread, m),
                               attachment=m.get("attachment"))
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
                    f"gap - messages from {self._sender_label(thread, {'sender': member}) or member[:8]} "
                    f"before seq {oldest} were dropped by retention")
        clear_children(self.conversation)
        for m in sorted(self.app.session.cache.msgs(conv), key=lambda m: (m["ts"], min(m["seqs"].values()))):
            self.append_bubble(m["dir"] == "out", m["text"], m["ts"],
                               sender=None if m["dir"] == "out" else self._sender_label(thread, m),
                               attachment=m.get("attachment"))
        scroll_to_bottom(self.conversation_scroll)
        return False

    def _history_failed(self, e):
        self.append_system_note(f"history unavailable: {e}")
        return False

    def append_bubble(self, outgoing, text, ts, sender=None, attachment=None):
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
        if attachment:
            self._render_attachment_box(box, outgoing, attachment)
        else:
            bubble = Gtk.Label()
            bubble.set_markup(linkify(text))
            bubble.connect("activate-link", self.on_link_activated)
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

    def on_link_activated(self, label, uri):
        try:
            ok = Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as e:
            self.app.activity.log(f"couldn't open link: {e}")
            return True
        if not ok:
            self.app.activity.log(f"couldn't open link: no default handler for {uri}")
        return True

    def _render_attachment_box(self, box, outgoing, attachment):
        path = attachment.get("path", "")
        filename = attachment.get("filename", "file")
        mime_hint = attachment.get("mime_hint", "")
        size = attachment.get("size", 0)
        name_lbl = Gtk.Label(label=filename)
        name_lbl.set_xalign(0.0)
        name_lbl.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        box.pack_start(name_lbl, False, False, 0)
        if mime_hint == "image" and os.path.exists(path):
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 280, -1, True)
                img = Gtk.Image.new_from_pixbuf(pix)
                img.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
                eb = Gtk.EventBox()
                eb.add(img)
                eb.connect("button-press-event", self.on_expand_image, path, filename)
                box.pack_start(eb, False, False, 0)
            except GLib.Error:
                pass
        size_lbl = Gtk.Label(label=_human_size(size))
        size_lbl.set_xalign(0.0)
        size_lbl.get_style_context().add_class("muted")
        box.pack_start(size_lbl, False, False, 0)
        save = Gtk.Button(label="Save")
        save.set_relief(Gtk.ReliefStyle.NONE)
        save.get_style_context().add_class("muted")
        save.connect("clicked", self.on_save_attachment, path, filename)
        box.pack_start(save, False, False, 0)

    def on_expand_image(self, _w, _ev, path, filename):
        win = Gtk.Window(title=filename)
        win.set_default_size(900, 700)
        sc = Gtk.ScrolledWindow()
        win.add(sc)
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file(path)
            img = Gtk.Image.new_from_pixbuf(pix)
            sc.add(img)
        except GLib.Error:
            pass
        win.show_all()
        return False

    def on_save_attachment(self, _btn, path, filename):
        dlg = Gtk.FileChooserDialog(
            title="Save attachment", transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.SAVE,
            buttons=("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK))
        dlg.set_current_name(filename)
        resp = dlg.run()
        dest = dlg.get_filename()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not dest:
            return
        try:
            shutil.copyfile(path, dest)
            self.app.activity.log(f"saved {filename}")
        except OSError as e:
            self.app.activity.log(f"save failed: {e}")

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
            "This conversation is emptied here and the existing history is dismissed - "
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
            "Messages are removed from this device and old history is dismissed - "
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
                self.block_btn.hide()
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
            "whoever added them to this room - invites exchanged directly are stronger.")
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
            glyph = "●" if known else "○"
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            lbl = Gtk.Label()
            lbl.set_markup(f"<span foreground='{color}'>{glyph}</span> {GLib.markup_escape_text(screen)}")
            lbl.set_xalign(0.0)
            btn.add(lbl)
            btn.get_style_context().add_class("aimless-chip")
            btn.set_tooltip_text("Open conversation - your buddy" if known
                                 else "Add as buddy (not your buddy yet)")
            btn.connect("clicked", self.on_member_chip, n, screen, known)
            chips.add(btn)
            btn.show_all()
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
        self.app.daemon_block_set(node, blocked=False)
        self.app.contacts.refresh()
        self.sync_sidebar()
        self.app.activity.log(f"added {petname} from the room")
        dm = self.threads.get(node)
        if dm and "row" in dm:
            self.thread_list.select_row(dm["row"])

    def on_toggle_mute(self, *_):
        thread = self.selected
        if thread is None:
            return
        conv = thread["conv"]
        cache = self.app.session.cache
        if cache.is_conversation_muted(conv):
            cache.unmute_conversation(conv)
            self.app.activity.log(f"unmuted {thread['screen']}")
        else:
            cache.mute_conversation(conv)
            self.app.activity.log(f"muted {thread['screen']}")
        self.mute_btn.set_label("Unmute…" if cache.is_conversation_muted(conv) else "Mute…")
        self.update_thread_row(conv)

    def on_block(self, *_):
        thread = self.selected
        if thread is None or thread.get("is_room"):
            return
        node = thread["conv"]
        screen = thread["screen"]
        if not self._confirm_block(screen):
            return
        cache = self.app.session.cache
        cache.mute(node)
        cache.set_blocked_screen(node, screen)
        allc = protocol.load_contacts(contacts_path())
        for key in list(allc):
            if key != "_self" and allc[key].get("node") == node:
                del allc[key]
        protocol.save_contacts(contacts_path(), allc)
        gone = self.threads.pop(node, None)
        if gone and "row" in gone:
            gone["row"].destroy()
        if self.selected is thread:
            self.selected = None
            self.stack.set_visible_child_name("placeholder")
            self.delete_btn.hide()
            self.mute_btn.hide()
            self.block_btn.hide()
        self.app.daemon_block_set(node, blocked=True, on_done=self.app.contacts.refresh)
        self.app.activity.log(f"blocked {screen}")

    def _confirm_block(self, screen):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Block and remove {screen}?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "They won't be able to message you. You can unblock them later from the Contacts list.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

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
            self.append_system_note(f"⚠ send failed: {e} - the message was not queued")
            self.app.activity.log(f"send failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def on_attach(self, *_):
        thread = self.selected
        if thread is None:
            return
        dlg = Gtk.FileChooserDialog(
            title="Attach a file", transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN,
            buttons=("Cancel", Gtk.ResponseType.CANCEL, "Attach", Gtk.ResponseType.OK))
        resp = dlg.run()
        path = dlg.get_filename()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not path:
            return
        try:
            size = os.path.getsize(path)
            protocol.validate_send_size(size)
        except (OSError, ValueError) as e:
            self.append_system_note(f"attach failed: {e}")
            return
        filename = os.path.basename(path)
        if thread.get("is_room") and size > 5 * 1024 * 1024 and len(thread.get("members", {})) > 3:
            if not self._confirm_large_room_send(filename, size, len(thread["members"])):
                return
        self._send_file(thread, path, filename, size)

    def _confirm_large_room_send(self, filename, size, members):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Send {filename} ({_human_size(size)}) to {members} members?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "Every member receives a full copy - a large transfer to a big room uses "
            "real bandwidth, just like text does, scaled per file.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _send_file(self, thread, path, filename, size):
        conv = thread["conv"]
        is_room = thread.get("is_room")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            self.append_system_note(f"attach failed: {e}")
            return
        tid = protocol.new_transfer_id()
        sha = hashlib.sha256(data).hexdigest()
        mime_hint = ("image" if filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
                     else "application")
        pieces = [data[i:i + protocol.FILE_CHUNK_SIZE] for i in range(0, len(data), protocol.FILE_CHUNK_SIZE)]
        total = len(pieces)
        session = self.app.session
        client = session.client
        row = self._append_status_row(f"Sending {filename} - 0/{total}")
        attachment = {"path": path, "filename": filename, "mime_hint": mime_hint, "size": size}

        def worker():
            seqs = {}
            for i, piece in enumerate(pieces):
                chunk = protocol.make_chunk(tid, i, total, filename, mime_hint, sha, size, piece,
                                            conv=conv if is_room else None)
                if is_room:
                    seqs.update(client.send_file_room(list(thread["members"].values()), conv, chunk))
                else:
                    payload = protocol.build_file_payload(
                        session.identity, thread["contact"]["pubkey"], chunk)
                    resp = client.send_file(conv, payload)
                    seqs[conv] = resp.get("seq", 0)
                GLib.idle_add(_progress, i + 1)
            return seqs

        def _progress(done):
            self._update_status_row(row, f"Sending {filename} - {done}/{total}")
            return False

        def done(seqs):
            if row is not None:
                row.destroy()
            ts = int(time.time() * 1000)
            self.append_bubble(True, filename, ts, attachment=attachment)
            session.cache.add_sent(conv, seqs, ts, filename, attachment=attachment)
            if thread is self.selected:
                thread["preview"] = filename
                self.update_thread_row(conv)
            scroll_to_bottom(self.conversation_scroll)

        def fail(e):
            if row is not None:
                self._update_status_row(row, f"{filename} - send failed: {e}")
            self.app.activity.log(f"send failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def incoming(self, ev):
        if ev.get("type") == "file":
            self._incoming_file(ev)
            return
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

    def _incoming_file(self, ev):
        node = ev.get("from")
        try:
            parsed = protocol.parse_file_payload(ev["payload"])
            chunk = protocol.open_file_chunk(self.app.session.identity, parsed["sealed"])
        except Exception:
            return
        if chunk.get("transfer_id") != parsed["tid"].hex() \
                or chunk.get("index") != parsed["index"] \
                or chunk.get("total") != parsed["total"]:
            return  # routing header does not match the sealed body
        conv = chunk.get("conv") or node
        key = (conv, chunk["transfer_id"])
        buf = self._file_bufs.get(key)
        if buf is None:
            buf = {"total": chunk["total"], "meta": chunk, "chunks": {}, "row": None, "failed": False}
            self._file_bufs[key] = buf
            if self.selected is not None and self.selected.get("conv") == conv:
                buf["row"] = self._append_status_row(
                    f"Receiving {chunk.get('filename') or 'file'} - 1/{chunk['total']}")
        if buf.get("failed"):
            return
        buf["chunks"][chunk["index"]] = base64.b64decode(chunk["data"])
        have = len(buf["chunks"])
        if buf["row"] is not None:
            self._update_status_row(buf["row"], f"Receiving {chunk.get('filename') or 'file'} - {have}/{chunk['total']}")
            scroll_to_bottom(self.conversation_scroll)
        if have == buf["total"]:
            if not self._finalize_attachment(conv, node, buf, ev.get("seq", 0), ev.get("ts", 0)):
                self._mark_file_failed(buf)
            del self._file_bufs[key]

    def _append_status_row(self, text):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0.5)
        lbl.get_style_context().add_class("muted")
        row.add(lbl)
        self.conversation.add(row)
        row.show_all()
        return row

    def _update_status_row(self, row, text):
        lbl = row.get_child()
        if isinstance(lbl, Gtk.Label):
            lbl.set_text(text)

    def _mark_file_failed(self, buf):
        buf["failed"] = True
        meta = buf.get("meta") or {}
        if buf["row"] is not None:
            self._update_status_row(buf["row"], f"{meta.get('filename') or 'file'} - failed to receive")

    def _store_attachment(self, conv, tid, filename, data):
        d = os.path.join(attachments_dir(), conv)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{tid}-{sanitize_filename(filename)}")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path

    def _finalize_attachment(self, conv, node, buf, seq, ts):
        chunk = buf["meta"]
        rebuilt = protocol.reassemble_file(buf["chunks"], buf["total"])
        if rebuilt is None:
            return False
        if hashlib.sha256(rebuilt).hexdigest() != chunk.get("sha256", ""):
            return False
        filename = chunk.get("filename") or "file"
        path = self._store_attachment(conv, chunk["transfer_id"], filename, rebuilt)
        attachment = {"path": path, "filename": filename,
                      "mime_hint": chunk.get("mime_hint", ""), "size": len(rebuilt)}
        self.app.session.cache.add_recv(conv, node, seq, ts, filename, attachment=attachment)
        if buf["row"] is not None:
            buf["row"].destroy()
        thread = self.threads.get(conv)
        if thread is not None:
            thread["preview"] = filename
            if self.selected is thread:
                self.append_bubble(False, filename, ts,
                                   sender=self._sender_label(thread, {"sender": node}),
                                   attachment=attachment)
                scroll_to_bottom(self.conversation_scroll)
            else:
                thread["unread"] += 1
            self.update_thread_row(conv)
        run_async(lambda: self.app.session.client.ack_attachment(node, chunk["transfer_id"]))
        return True

    def _process_fetched_transfer(self, node, tid, chunks):
        """Reassemble a transfer fetched from the daemon (startup sweep)."""
        buf = {"total": 0, "meta": {}, "chunks": {}, "row": None, "failed": False}
        seq, ts = 0, 0
        for entry in chunks:
            try:
                parsed = protocol.parse_file_payload(entry["payload"])
                chunk = protocol.open_file_chunk(self.app.session.identity, parsed["sealed"])
            except Exception:
                continue
            if chunk.get("transfer_id") != parsed["tid"].hex() \
                    or chunk.get("index") != parsed["index"] \
                    or chunk.get("total") != parsed["total"]:
                continue
            buf["total"] = chunk["total"]
            buf["meta"] = chunk
            buf["chunks"][chunk["index"]] = base64.b64decode(chunk["data"])
            if entry.get("seq", 0) > seq:
                seq = entry["seq"]
            if entry.get("ts", 0) > ts:
                ts = entry["ts"]
        if not buf["chunks"]:
            return
        conv = (buf["meta"].get("conv") or node)
        if self._finalize_attachment(conv, node, buf, seq, ts):
            pass

    def catchup_attachments(self):
        """Startup sweep: discover complete-but-unconsumed transfers on the daemon
        and reassemble them, mirroring catchup_unread for text."""
        if self._catchup_busy:
            return
        self._catchup_busy = True
        session = self.app.session
        nodes = set()
        for info in session.contacts().values():
            if info.get("node"):
                nodes.add(info["node"])
        for conv in session.cache.rooms():
            for n in session.cache.members(conv):
                if n != session.self_node:
                    nodes.add(n)

        def worker():
            jobs = []
            for n in nodes:
                try:
                    transfers = session.client.pending_attachments(n)
                except DaemonError:
                    continue
                for t in transfers:
                    try:
                        chunks = session.client.fetch_attachment(n, t["tid"])
                    except DaemonError:
                        continue
                    jobs.append((n, t["tid"], chunks))
            return jobs

        def done(jobs):
            self._catchup_busy = False
            for node, tid, chunks in jobs:
                self._process_fetched_transfer(node, tid, chunks)

        def fail(_e):
            self._catchup_busy = False

        run_async(worker, on_done=done, on_error=fail)


class ContactsView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app = app
        self.set_border_width(14)
        self.get_style_context().add_class("aimless-contacts")

        invite_frame = Gtk.Frame(label="Your invite - send this to a friend")
        invite_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        invite_box.set_border_width(10)
        self.invite_entry = Gtk.Entry(editable=False)
        invite_box.pack_start(self.invite_entry, False, False, 0)
        copy_btn = Gtk.Button(label="Copy to clipboard")
        copy_btn.connect("clicked", self.on_copy_invite)
        invite_box.pack_start(copy_btn, False, False, 0)
        invite_frame.add(invite_box)
        self.pack_start(invite_frame, False, False, 0)

        add_frame = Gtk.Frame(label="Add a buddy - paste their invite")
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

    def _render_buddy_rows(self, blocked):
        clear_children(self.buddy_list)
        self._buddy_rows = {}
        contacts = self.app.session.contacts()
        contact_nodes = {info["node"] for info in contacts.values()}
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
            if info["node"] in blocked:
                title.get_style_context().add_class("muted")
                labels.get_style_context().add_class("muted")
                btn = Gtk.Button(label="Unblock")
                btn.connect("clicked", self.on_unblock, info["node"])
            else:
                btn = Gtk.Button(label="Remove")
                btn.connect("clicked", self.on_remove, petname)
            box.pack_start(btn, False, False, 0)
            row.add(box)
            self.buddy_list.add(row)
            row.show_all()
            self._buddy_rows[petname] = title
        for node in sorted(blocked):
            if node in contact_nodes:
                continue
            name = self.app.session.cache.blocked_screen(node)
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_border_width(8)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label(label=name or f"{node[:20]}…")
            title.set_xalign(0.0)
            title.get_style_context().add_class("muted")
            labels.pack_start(title, False, False, 0)
            sub = Gtk.Label(label=f"{node[:20]}…" if name else "blocked")
            sub.set_xalign(0.0)
            sub.get_style_context().add_class("muted")
            labels.pack_start(sub, False, False, 0)
            box.pack_start(labels, True, True, 0)
            ub = Gtk.Button(label="Unblock")
            ub.connect("clicked", self.on_unblock, node)
            box.pack_start(ub, False, False, 0)
            row.add(box)
            self.buddy_list.add(row)
            row.show_all()
            self._buddy_rows[node] = title
        self.refresh_presence(getattr(self, "_cached_presence", {}))

    def refresh(self):
        # The buddy list renders immediately from local state (no daemon call on
        # the UI thread); the blocklist is fetched in the worker and only re-renders
        # if it changed, so a slow daemon can never stall the interface.
        self._render_buddy_rows(getattr(self, "_blocked", set()))
        self.refresh_presence(getattr(self, "_cached_presence", {}))

        def worker():
            invite = self.app.session.my_invite()
            presence = {p["key"]: p for p in self.app.session.client.presence(timeout=3)}
            try:
                blocked = set(self.app.session.client.blocklist())
            except DaemonError:
                blocked = set()
            return invite, presence, blocked

        def done(result):
            invite, presence, blocked = result
            self._cached_presence = presence
            self.invite_entry.set_text(invite)
            if blocked != getattr(self, "_blocked", set()):
                self._blocked = blocked
                self._render_buddy_rows(blocked)
            self.refresh_presence(presence)

        run_async(worker, on_done=done)

    def refresh_presence(self, presence):
        contacts = self.app.session.contacts()
        for petname, title in getattr(self, "_buddy_rows", {}).items():
            info = contacts.get(petname, {})
            node = info.get("node")
            label = info.get("screen", petname)
            if not info:
                # synthetic blocked-stranger row: prefer the stored screen name
                node = petname
                label = self.app.session.cache.blocked_screen(petname) or label
            p = presence.get(node, {})
            color = "#a6e3a1" if p.get("online") else "#9aa0ad"
            state = "online" if p.get("online") else "offline"
            title.set_markup(
                f"<b>{GLib.markup_escape_text(label)}</b> "
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
            self.add_status.set_text("error: that's your own invite - send it to a friend")
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

        def add_worker():
            try:
                self.app.session.client.unblock(node_hex)
            except DaemonError:
                pass
            return self.app.session.client.add_contact(node_hex)

        run_async(add_worker)
        self.add_invite_entry.set_text("")
        self.add_petname_entry.set_text("")
        self.add_status.set_text(f"added {screen}")
        self.refresh()
        self.app.messages.sync_sidebar()
        win = getattr(self.app, "window", None)
        if win is None and hasattr(self.app, "_push_detached"):
            win = self.app
        if win is not None:
            try:
                win._push_detached()
            except Exception:
                pass

    def on_remove(self, btn, petname):
        allc = protocol.load_contacts(contacts_path())
        allc.pop(petname, None)
        protocol.save_contacts(contacts_path(), allc)
        self.add_status.set_text(f"removed {petname}")
        self.refresh()
        self.app.messages.sync_sidebar()

    def on_unblock(self, btn, node):
        self.app.session.cache.unmute(node)
        self.app.session.cache.clear_blocked_screen(node)

        def worker():
            return self.app.session.client.unblock(node)

        def done(_r):
            self.refresh()

        def fail(e):
            self.add_status.set_text(f"unblock failed: {e}")
            self.refresh()

        run_async(worker, on_done=done, on_error=fail)


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
            self.info_label.set_markup("<span foreground='#f38ba8'>●  offline - daemon not reachable</span>")
            return
        build = st.get("build", "")
        version_note = ""
        if not build:
            version_note = ("\n<span foreground='#f38ba8'>this daemon is an old build - "
                            "run `aimless stop`, then reopen aimless to update</span>")
            build = "unknown"
        else:
            try:
                dv = tuple(int(x) for x in build.split("/", 1)[1].split("."))
            except (IndexError, ValueError):
                dv = None
            if dv is not None and dv < MIN_DAEMON_BUILD:
                need = ".".join(str(x) for x in MIN_DAEMON_BUILD)
                version_note = ("\n<span foreground='#f38ba8'>daemon {b} is too old for this client "
                                "(needs ≥ {n}) - update aimlessd-linux-amd64, then `aimless stop` and "
                                "reopen</span>".format(b=build, n=need))
                if not getattr(self, "_version_warned", False):
                    self._version_warned = True
                    self.log(f"⚠ daemon {build} is too old for this client (needs ≥ {need}) "
                             f" -  update aimlessd-linux-amd64, then `aimless stop` and reopen")
        state = ("<span foreground='#a6e3a1'>●  you are online</span>" if st["peers_up"] > 0
                 else "<span foreground='#fab387'>●  connecting - no Yggdrasil peers yet</span>")
        self.info_label.set_markup(
            f"{state}  -  address <b>{st['address']}</b>  ·  peers {st['peers_up']}/{st['peers_total']}\n"
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
        self.ssh_label = Gtk.Label(label="", xalign=0.0)
        self.ssh_label.set_no_show_all(True)
        self.ssh_label.hide()
        route_bar.pack_start(self.ssh_label, False, False, 0)
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
        ssh_item = Gtk.MenuItem(label="Remote daemon (SSH) …")
        ssh_item.connect("activate", self.on_ssh_settings)
        options_menu.append(ssh_item)
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
        GLib.timeout_add(1200, self._catchup_once)
        GLib.timeout_add(2500, self._catchup_attachments_once)

        def watch_all():
            for info in self.session.contacts().values():
                try:
                    self.session.client.add_contact(info["node"])
                except DaemonError:
                    pass

        run_async(watch_all)

        self._push_status(self.prefs.get("away") or None)
        self._push_detached()
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
        dlg.add_buttons("Deny", Gtk.ResponseType.REJECT,
                        "Accept", Gtk.ResponseType.ACCEPT,
                        "Block", Gtk.ResponseType.NO)
        dlg.set_default_response(Gtk.ResponseType.ACCEPT)
        resp = dlg.run()
        dlg.destroy()
        return resp

    def daemon_block_set(self, node, blocked, on_done=None):
        """Best-effort daemon-side block/unblock (fire-and-forget). Against a
        pre-0.3.4 daemon the op is unknown; fail quietly, leaving the client-side
        mute in charge."""
        if blocked:
            def worker():
                return self.session.client.block(node)
        else:
            def worker():
                return self.session.client.unblock(node)

        def fail(e):
            self.activity.log(f"daemon {'block' if blocked else 'unblock'} failed: {e} - client-side only")

        def done(_r):
            if on_done is not None:
                on_done()

        run_async(worker, on_done=done, on_error=fail)

    def surface_pending_requests(self):
        if getattr(self, "_request_open", False):
            return GLib.SOURCE_REMOVE
        req = self.session.cache.pending_pop()
        if req is None:
            return GLib.SOURCE_REMOVE
        self._request_open = True
        try:
            choice = self._ask_request(req)
        finally:
            self._request_open = False
        if choice == Gtk.ResponseType.ACCEPT:
            petname = _add_contact_from_roster(req["node"], req["pubkey"], req.get("screen"))
            self.session.cache.unmute(req["node"])
            self.daemon_block_set(req["node"], blocked=False)
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
        elif choice == Gtk.ResponseType.NO:
            self.session.cache.mute(req["node"])
            self.session.cache.set_blocked_screen(req["node"], req.get("screen") or req["node"][:8])
            self.daemon_block_set(req["node"], blocked=True, on_done=self.contacts.refresh)
            self.activity.log(f"blocked {req.get('screen') or req['node'][:8]}")
        else:
            # Deny is a one-time soft decline: this request is discarded and the
            # sender stays a stranger, so their next message re-prompts. Block is
            # the persistent option.
            self.activity.log(f"denied {req.get('screen') or req['node'][:8]}")
        GLib.idle_add(self.surface_pending_requests)
        return GLib.SOURCE_REMOVE

    def on_view_changed(self, stack, param):
        if stack.get_visible_child_name() == "contacts":
            self.contacts.refresh()

    def on_set_away(self, *_):
        # In remote (SSH) mode the daemon keeps running with no GUI attached, so
        # buddies see the offline status while you're away. Only offer that field
        # when it's meaningful.
        if self.supervisor.remote:
            self._on_set_away_remote()
        else:
            away = ask_text(self, "Away message", "Away message (empty = available):")
            self.set_away(away.strip() if away and away.strip() else None)

    def _on_set_away_remote(self):
        dlg = Gtk.Dialog(title="Away & offline", transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_default_size(420, 140)
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)
        box.add(Gtk.Label(label="Away message (empty = available)", xalign=0.0))
        away = Gtk.Entry()
        away.set_text(self.prefs.get("away") or "")
        box.add(away)
        box.add(Gtk.Label(
            label="When no GUI is connected to the remote daemon, buddies see this:",
            xalign=0.0))
        offline = Gtk.Entry()
        offline.set_text(self.prefs.get("offline_status") or DEFAULT_OFFLINE_STATUS)
        box.add(offline)
        dlg.show_all()
        resp = dlg.run()
        away_val = away.get_text().strip()
        offline_val = offline.get_text().strip()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        prefs = load_prefs()  # merge - never clobber other writers
        prefs["away"] = away_val
        prefs["offline_status"] = offline_val or DEFAULT_OFFLINE_STATUS
        save_prefs(prefs)
        self.prefs = prefs
        self.set_away(away_val or None)
        self._push_detached(self.prefs.get("offline_status") or DEFAULT_OFFLINE_STATUS)

    def on_ssh_settings(self, *_):
        if run_ssh_settings_dialog(self, prompt_restart=True,
                                   on_migrate=getattr(self, "do_migrate_node", None)):
            # the dialog writes prefs to disk directly; resync the window's
            # cached copy so later save_geometry()/set_away() can't clobber it.
            self.prefs = load_prefs()

    def do_migrate_node(self, *_):
        """Move the local identity (node key + outbound journal) to the
        configured remote daemon. Async; prompts for the daemon restart, then
        verifies the remote answers with the migrated node key. host,
        remote_socket and identity can be passed from the settings dialog (the
        user may not have saved yet); otherwise the saved ssh config is used."""
        if len(_) >= 3 and _[0]:
            host, remote_socket, ident = _[0], _[1], _[2] or None
            ssh = {"host": host, "remote_socket": remote_socket}
            if ident:
                ssh["identity"] = ident
        else:
            ssh = ssh_prefs()
        if not ssh or not ssh.get("host") or not ssh.get("remote_socket"):
            # No remote daemon configured - the user needs to set it up first.
            self.activity.log("migrate node key: no remote daemon configured")
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK,
                text="No remote daemon is configured.",
                secondary_text="Set up the remote daemon (SSH) first - enter a host "
                               "and click 'Find daemon on host' - then move your "
                               "local identity to it.")
            dlg.run()
            dlg.destroy()
            return
        # Persist the config so the app is consistent with the migration target.
        prefs = load_prefs()
        prefs["ssh"] = {"host": ssh["host"], "remote_socket": ssh["remote_socket"]}
        if ssh.get("identity"):
            prefs["ssh"]["identity"] = ssh["identity"]
        save_prefs(prefs)
        self.prefs = prefs
        host = ssh["host"]
        identity = ssh.get("identity") or None
        datadir = remote_datadir()
        path = ssh["remote_socket"]
        expected = node_key_public_hex()
        if expected is None:
            self.activity.log("migrate node key: no local node.key to migrate")
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK,
                text="No local daemon state found on this machine.",
                secondary_text="There is no local node.key to move. This action moves "
                               "an existing local identity to the remote daemon.")
            dlg.run()
            dlg.destroy()
            return

        # A live LOCAL daemon must be stopped first: two daemons with the same
        # node key collide on the mesh. This is part of moving - offer to stop it.
        local_pid = daemon_pid_from_procs()
        if local_pid is not None:
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.NONE,
                text=f"A local daemon is running (pid {local_pid}).",
                secondary_text="It must be stopped before your identity can move to "
                               "the remote daemon - two daemons with the same node "
                               "key would conflict. Stop it and continue?")
            dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dlg.add_button("Stop it and continue", Gtk.ResponseType.APPLY)
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.APPLY:
                self.activity.log("migrate node key: cancelled - local daemon still running")
                return
            # Take the local daemon out of this app's supervision first: the app
            # started in local mode and its poll() respawns the daemon every 5s,
            # so killing it would be a losing race. The supervisor becomes the
            # remote one (the migration destination anyway). The actual kill
            # happens inside migrate_node_stage, right before its guard, so
            # nothing can respawn in between.
            if not self.supervisor.remote:
                self.supervisor = DaemonSupervisor()
                self.activity.log("local daemon handed over - supervising the remote daemon now")

        # --- stage the files (async) ---
        dlg = Gtk.Dialog(title="Move local identity to remote",
                         transient_for=self, modal=True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.set_default_size(460, 140)
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)
        label = Gtk.Label(label="Copying your identity and outbound journal to "
                                f"{host} ...", xalign=0.0, wrap=True)
        box.add(label)
        dlg.show_all()

        def stage_done(result):
            dlg.destroy()
            self._migrate_restart_prompt(host, identity, datadir, path, expected)

        def stage_fail(exc):
            dlg.destroy()
            self.activity.log(f"migrate node key failed: {exc}")
            err = Gtk.MessageDialog(
                transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="Moving your identity to the remote daemon failed.",
                secondary_text=str(exc))
            err.run()
            err.destroy()

        def worker():
            backup = migrate_node_stage(host, identity, datadir,
                                        stop_pid=local_pid)
            return backup

        run_async(worker, on_done=stage_done, on_error=stage_fail)

    def _migrate_restart_prompt(self, host, identity, datadir, path, expected):
        """Ask the user to restart the remote daemon (auto docker restart if
        possible, else show the command), then verify the node key took. One
        dialog for the whole flow; Cancel always stays usable."""
        container = detect_remote_container(host, identity, datadir)
        dlg = Gtk.Dialog(title="Restart the server's daemon", transient_for=self, modal=True)
        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_border_width(10)
        msg = Gtk.Label(label="", xalign=0.0, wrap=True)
        box.add(msg)
        status = Gtk.Label(label="", xalign=0.0, wrap=True)
        box.add(status)

        buttons = []

        def set_message(text):
            msg.set_text(text)

        if container:
            btn_restart = Gtk.Button(label="Restart the daemon now")
            buttons.append(btn_restart)
            box.add(btn_restart)
        btn_manual = Gtk.Button(label="I've restarted it")
        buttons.append(btn_manual)
        box.add(btn_manual)
        btn_cancel = Gtk.Button(label="Cancel")
        buttons.append(btn_cancel)
        box.add(btn_cancel)
        dlg.show_all()
        set_message("Your identity is copied to the server's daemon. Restart it "
                    "so it comes back as your node."
                    + ("\n\nI will try to restart it for you." if container else ""))

        def start_verify(transition_msg):
            # reuse the same dialog for the verify step; only Cancel stays
            set_message(transition_msg)
            for w in buttons:
                w.hide()
            btn_cancel.show()
            btn_cancel.set_label("Cancel verification")
            btn_cancel.set_sensitive(True)
            self._migrate_verify(host, identity, path, expected, dlg, status,
                                 cancel_handler=btn_cancel)

        def restart_now(*_):
            if not container:
                status.set_text(
                    "Could not find the container. Run on the server yourself:\n"
                    "  sudo docker restart <container>\n"
                    "then click 'I've restarted it'.")
                return
            btn_restart.set_sensitive(False)  # only this button - Cancel stays live
            status.set_text(f"restarting {container} ...")

            def worker():
                return ssh_run(host, identity, f"docker restart {container}", timeout=60)

            def done(res):
                rc, out, err = res
                if rc != 0:
                    btn_restart.set_sensitive(True)
                    status.set_text(
                        f"Automatic restart failed (sudo needed?). Run this on the "
                        f"server yourself:\n  sudo docker restart {container}\n"
                        f"then click 'I've restarted it'.\n\n{err.strip()}")
                    return
                status.set_text(f"{container} restarted.")
                start_verify("Restarted. Waiting for the server to come back as your node ...")

            run_async(worker, on_done=done)

        def restarted(*_):
            start_verify("Waiting for the server to come back as your node ...")

        def cancelled(*_):
            dlg.destroy()
            self.activity.log("migrate node key: cancelled - the remote daemon still "
                              "has its old key; new files are staged (restart it later "
                              "to finish).")

        if container:
            btn_restart.connect("clicked", restart_now)
        btn_manual.connect("clicked", restarted)
        btn_cancel.connect("clicked", cancelled)

    def _migrate_verify(self, host, identity, path, expected, dlg, status,
                        cancel_handler=None):
        """Poll the remote daemon until whoami reports the migrated node key,
        reusing the open dialog (Cancel stays live). Then record the pair +
        relocation and clear the mismatch banner."""
        cancelled = {"v": False}

        def on_cancel(*_):
            cancelled["v"] = True
            dlg.destroy()
            self.activity.log("migrate node key: verification cancelled - files "
                              "are staged; restart the daemon later to finish.")

        if cancel_handler is not None:
            cancel_handler.connect("clicked", on_cancel)

        status.set_text("Checking ...")

        def worker():
            return verify_remote_node_key(host, identity, path, expected, timeout=180)

        def done(ok):
            if cancelled["v"]:
                return
            if ok:
                dlg.destroy()
                prefs = load_prefs()
                prefs["last_node_key"] = expected
                prefs["node_relocated"] = {
                    "key": expected, "host": host,
                    "remote_socket": path, "ts": int(time.time()),
                }
                save_prefs(prefs)
                self.prefs = prefs
                self._mismatch_notified = True  # suppress the banner
                self.activity.log("node key migrated - the remote daemon now answers "
                                  "with your identity. Restart the app to reconnect "
                                  "through the remote daemon.")
            else:
                status.set_text("The server did not come back with your node. "
                                "It may not have restarted - check it, then click "
                                "'I've restarted it' to retry, or Cancel.")
                if cancel_handler is not None:
                    cancel_handler.set_sensitive(True)

        def fail(exc):
            if cancelled["v"]:
                return
            status.set_text(f"Verification failed: {exc}")
            if cancel_handler is not None:
                cancel_handler.set_sensitive(True)

        run_async(worker, on_done=done, on_error=fail)



    def set_away(self, away):
        self._apply_away_banner(away)
        prefs = load_prefs()  # merge into fresh prefs - never clobber other writers
        prefs["away"] = away or ""
        save_prefs(prefs)
        self.prefs = prefs
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
        """Status is ephemeral daemon RAM on both ends - re-announce the current
        status so buddies converge after any restart (ours or theirs)."""
        self._push_status(self.prefs.get("away") or None)
        return GLib.SOURCE_CONTINUE

    def _push_detached(self, text=None):
        """Pre-seal the offline status for every contact and hand it to the
        daemon. Buddies see this text when no GUI client is attached (remote /
        daemon-only mode). Re-sent on connect and when contacts change."""
        text = (text if text is not None
                else (self.prefs.get("offline_status") or DEFAULT_OFFLINE_STATUS))
        contacts = list(self.session.contacts().values())

        def worker():
            for info in contacts:
                try:
                    self.session.client.set_detached(info["pubkey"], info["node"], text)
                except Exception:
                    pass

        run_async(worker)

    def _apply_away_banner(self, away):
        if away:
            self.away_icon.set_from_icon_name("weather-clear-night-symbolic", Gtk.IconSize.MENU)
            self.away_label.set_markup(
                f"<b>Away</b> - {GLib.markup_escape_text(away)}  "
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

    def _catchup_once(self):
        self.messages.catchup_unread()
        return GLib.SOURCE_REMOVE

    def _catchup_attachments_once(self):
        self.messages.catchup_attachments()
        return GLib.SOURCE_REMOVE




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
        # SSH status badge: only meaningful in remote mode.
        try:
            tunnel = getattr(self.supervisor, "tunnel", None)
            if tunnel is not None and getattr(self.supervisor, "remote", False):
                host = getattr(tunnel, "host", "?")
                if st and st.get("peers_up") is not None:
                    color, dot = "#a6e3a1", "●"   # tunnel up, daemon answered
                elif tunnel.is_ready():
                    color, dot = "#fab387", "●"   # tunnel up, daemon silent
                else:
                    color, dot = "#f38ba8", "○"   # tunnel down
                self.ssh_label.set_markup(
                    f"<span foreground='{color}'>SSH {dot} {GLib.markup_escape_text(host)}</span>")
                self.ssh_label.show()
            else:
                self.ssh_label.set_markup("")
                self.ssh_label.hide()
        except Exception:
            self.ssh_label.set_markup("")
            self.ssh_label.hide()
        if not st:
            self.route_label.set_markup("<span foreground='#f38ba8'>●  offline - daemon not reachable</span>")
        elif st["peers_up"] == 0:
            self.route_label.set_markup(
                "<span foreground='#fab387'>●  connecting - no Yggdrasil peers yet</span>")
        else:
            self.route_label.set_markup(
                f"<span foreground='#a6e3a1'>●  online</span>  -  {st['address']}  ·  "
                f"peers {st['peers_up']}/{st['peers_total']}")
        try:
            if (self.session is not None and self.session.node_key_mismatch()
                    and not getattr(self, "_mismatch_notified", False)):
                self._mismatch_notified = True
                dlg = Gtk.MessageDialog(
                    transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                    buttons=Gtk.ButtonsType.NONE,
                    text="Your contacts know you at a different daemon address.",
                    secondary_text="Messages won't reach you here. Move your node key "
                                   "to this daemon, or re-share your invite.")
                dlg.add_button("Dismiss", Gtk.ResponseType.CLOSE)
                dlg.add_button("Move local identity to remote", Gtk.ResponseType.APPLY)
                resp = dlg.run()
                dlg.destroy()
                if resp == Gtk.ResponseType.APPLY:
                    self.do_migrate_node()
        except Exception:
            pass

    def on_delete(self, *_):
        self.save_geometry()
        if self.app_ref is not None and self.app_ref.tray is not None and self.app_ref.tray.is_embedded():
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
        prefs = load_prefs()  # merge into fresh prefs - never clobber other writers
        prefs["window_width"] = w
        prefs["window_height"] = h
        save_prefs(prefs)
        self.prefs = prefs


def ask_passphrase(parent):
    dlg = Gtk.Dialog(title="AIMless - passphrase", transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Unlock", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(360, 100)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label="Enter your passphrase to unlock your identity"))
    entry = Gtk.Entry(visibility=False, activates_default=True)
    box.add(entry)
    ssh_btn = Gtk.Button(label="Remote daemon (SSH) …")
    ssh_btn.connect("clicked", lambda *_: run_ssh_settings_dialog(dlg, prompt_restart=False))
    box.add(ssh_btn)
    dlg.show_all()
    resp = dlg.run()
    text = entry.get_text()
    dlg.destroy()
    if resp == Gtk.ResponseType.OK and text:
        return text
    return None


def ask_where_live(parent):
    """First-run question: where does the user's aimless live? Returns 'local',
    'remote', or None (cancelled)."""
    dlg = Gtk.Dialog(title=f"{APP_NAME} - set up", transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL)
    dlg.set_default_size(420, 140)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label="Where does your aimless live?"))
    box.add(Gtk.Label(label=("Your daemon holds your address and stores messages while you're "
                             "away. It can run on this machine, or on a server you reach over "
                             "SSH so it stays online even when your computer is off."), xalign=0.0, wrap=True))
    local_btn = Gtk.Button(label="On this machine")
    remote_btn = Gtk.Button(label="On a server I SSH into")
    result = {"value": None}

    def pick(val):
        result["value"] = val
        dlg.response(Gtk.ResponseType.OK)

    local_btn.connect("clicked", lambda *_: pick("local"))
    remote_btn.connect("clicked", lambda *_: pick("remote"))
    box.add(local_btn)
    box.add(remote_btn)
    dlg.show_all()
    resp = dlg.run()
    dlg.destroy()
    return result["value"] if resp == Gtk.ResponseType.OK else None


def ask_create_identity(parent):
    """Ask for passphrase + confirm + screen name to create a brand-new identity.
    Returns None (cancelled / empty), ("__mismatch__",) or (passphrase, screen)."""
    dlg = Gtk.Dialog(title=f"{APP_NAME} - create your identity", transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create identity", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(380, 120)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label="No identity on this machine yet - set one up here."))
    box.add(Gtk.Label(label="Passphrase (protects your keys; re-enter it later to unlock)"))
    pw = Gtk.Entry(visibility=False, activates_default=True)
    box.add(pw)
    box.add(Gtk.Label(label="Confirm passphrase"))
    pw2 = Gtk.Entry(visibility=False, activates_default=True)
    box.add(pw2)
    box.add(Gtk.Label(label="Screen name"))
    screen = Gtk.Entry(activates_default=True)
    box.add(screen)
    ssh_btn = Gtk.Button(label="Remote daemon (SSH) …")
    ssh_btn.connect("clicked", lambda *_: run_ssh_settings_dialog(dlg, prompt_restart=False))
    box.add(ssh_btn)
    dlg.show_all()
    resp = dlg.run()
    p1, p2, sn = pw.get_text(), pw2.get_text(), screen.get_text()
    dlg.destroy()
    if resp != Gtk.ResponseType.OK or not p1:
        return None
    if p1 != p2:
        return ("__mismatch__",)
    return (p1, sn)


def create_identity(passphrase, screen):
    """Persist a fresh identity and self contact entry. Mirrors `aimless init`."""
    identity = crypto.new_identity()
    crypto.save_identity(identity_path(), identity, passphrase)
    contacts = protocol.load_contacts(contacts_path())
    contacts["_self"] = {"screen": screen or "anonymous",
                         "pubkey": bytes(identity.verify_key).hex()}
    protocol.save_contacts(contacts_path(), contacts)
    return passphrase


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
    """Single-instance guard. Returns (fh, None) when the lock was taken - hold the file
    handle for the process lifetime - or (None, holder_pid) when another instance runs."""
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
        mi_quit = Gtk.MenuItem(label="Quit - shuts down AIMless")
        mi_quit.connect("activate", self.on_quit)
        self.menu.append(mi_quit)
        self.menu.show_all()
        try:
            self.icon = Gtk.StatusIcon()
            self.icon.set_from_icon_name(first_icon("user-available-symbolic", "phone"))
            self.icon.set_title(APP_NAME)
            self.icon.set_tooltip_text(f"{APP_NAME} - running\nLeft-click to open Messages")
            self.icon.connect("activate", self.on_open)
            self.icon.connect("popup-menu", self.on_popup)
            self.icon.set_visible(True)
            self.have_tray = True
        except Exception as e:
            app.log(f"tray icon unavailable ({e!r}) - running as a plain window app")

    def is_embedded(self):
        """True only when a real system-tray host adopted the icon.

        Gtk.StatusIcon() does not raise when no tray host exists (X11 simply shows
        nothing), so `have_tray` alone would make a headless/container run believe
        it owns a tray and hide away to a tray that is not there.
        """
        return self.have_tray and self.icon.is_embedded()

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

    def _guard_local_startup_after_migration(self):
        """If this node.key was moved to a remote daemon, starting a LOCAL daemon
        with it would collide on the mesh and silently lose text (stale outbound
        seq vs what buddies have seen). Warn before starting local; offer to
        restore the SSH mode."""
        if self.supervisor.remote:
            return
        prefs = load_prefs()
        rel = prefs.get("node_relocated") if isinstance(prefs.get("node_relocated"), dict) else None
        if not rel:
            return
        local_pub = node_key_public_hex()
        if local_pub is None or local_pub != rel.get("key"):
            return  # different identity locally - no conflict
        host = rel.get("host") or "?"
        dlg = Gtk.MessageDialog(
            transient_for=None, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"This node currently lives on {host}.",
            secondary_text="Running it locally as well would conflict on the network "
                           "and your outgoing messages wouldn't be delivered.")
        dlg.add_button("Start local anyway", Gtk.ResponseType.ACCEPT)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Use SSH mode", Gtk.ResponseType.APPLY)
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.APPLY:
            # restore the stored SSH config and reconnect to the remote daemon;
            # _setup continues to _ensure_daemon_with_recovery() with the new
            # remote supervisor.
            prefs["ssh"] = {"host": rel["host"],
                            "remote_socket": rel.get("remote_socket") or ""}
            save_prefs(prefs)
            self.prefs = prefs
            self.supervisor = DaemonSupervisor()
            return
        if resp == Gtk.ResponseType.CANCEL:
            raise SystemExit(1)

    def _ensure_daemon_with_recovery(self):
        """Bring up the daemon (local spawn or remote SSH tunnel), retrying
        after the 'Disable remote daemon and retry' escape hatch clears a bad
        SSH config. Raises SystemExit if the user chose Quit."""
        while True:
            try:
                self.supervisor.ensure(log=self.log)
                return
            except RuntimeError as e:
                err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                        buttons=Gtk.ButtonsType.NONE, text=str(e))
                if self.supervisor.remote:
                    err.add_button("Disable remote daemon and retry", Gtk.ResponseType.APPLY)
                err.add_button("Quit", Gtk.ResponseType.CLOSE)
                resp = err.run()
                err.destroy()
                if resp == Gtk.ResponseType.APPLY:
                    # Recovery from a bad SSH config: the window/menu don't exist
                    # yet (that's why we're here), so the only way back to the
                    # settings dialog is to drop the remote daemon and start
                    # normally. Loop, not recursion - the app lock is already
                    # held, so re-entering _setup would re-take it and fail.
                    prefs = load_prefs()
                    prefs["ssh"] = {}  # clear the bad SSH config entirely
                    save_prefs(prefs)
                    self.log("ssh config cleared by startup recovery - retrying with the local daemon")
                    try:
                        self.supervisor.stop()  # tear down the failed remote tunnel before discarding it
                    except Exception:
                        pass
                    self.supervisor = DaemonSupervisor()  # re-read prefs; local spawn
                    continue
                raise SystemExit(1)

    def _rebuild_supervisor_if_stale(self):
        """If the ssh config changed since this supervisor was built (e.g. the
        user configured SSH from the first-run/unlock dialogs), stop the old
        connection and reconnect with the new one before a Session is created."""
        if not self.supervisor.stale():
            return
        self.log("ssh config changed - reconnecting to the daemon")
        try:
            self.supervisor.stop()
        except Exception:
            pass
        self.supervisor = DaemonSupervisor()
        self._ensure_daemon_with_recovery()

    def _setup(self, open_window):
        sys.excepthook = _make_excepthook(self.log)
        threading.excepthook = _make_thread_hook(self.log)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.lock_fh, holder = acquire_app_lock()
        if self.lock_fh is None:
            self.log(f"another instance is running (pid {holder}) - presenting its window")
            if holder and holder > 0:
                try:
                    os.kill(holder, signal.SIGUSR1)
                except OSError:
                    pass
            return 0

        try:
            self._guard_local_startup_after_migration()
        except SystemExit:
            return 1

        try:
            self._ensure_daemon_with_recovery()
        except SystemExit:
            return 1

        self.tray = TrayIcon(self)
        if open_window or not self.tray.have_tray:
            self.open_window()
        if self._no_window_headless():
            self.log("no window after setup (no usable tray) - exiting for the supervisor to restart")
            return 0

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
        if not os.path.exists(identity_path()):
            # First run on this machine: ask the one question that matters
            # before identity creation, so a remote daemon is configured BEFORE
            # the identity exists - otherwise the invite would embed the wrong
            # (local) node address.
            if not ssh_prefs():
                where = ask_where_live(None)
                if where is None:
                    self._cancel_or_quit()
                    return
                if where == "remote":
                    run_ssh_settings_dialog(None, prompt_restart=False)
                    # after SSH setup the supervisor may need rebuilding; the
                    # stale-check right before Session() handles it.
            for _create in range(3):
                created = ask_create_identity(None)
                if created is None:
                    self._cancel_or_quit()
                    return
                if created[0] == "__mismatch__":
                    err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                            text="passphrases do not match - try again")
                    err.run()
                    err.destroy()
                    continue
                new_pw, screen = created
                try:
                    create_identity(new_pw, screen)
                    passphrase = new_pw
                    self._rebuild_supervisor_if_stale()
                    session = Session(passphrase)
                    break
                except (OSError, DaemonError) as e:
                    err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                            text=f"could not create identity: {e}")
                    err.run()
                    err.destroy()
                    return
            else:
                return
        else:
            if passphrase:
                try:
                    self._rebuild_supervisor_if_stale()
                    session = Session(passphrase)
                except ValueError:
                    passphrase = None
                except (OSError, DaemonError) as e:
                    session = None
                    self.log(f"daemon unreachable during unlock ({e}) - retrying")
            if not passphrase:
                for _attempt in range(3):
                    passphrase = ask_passphrase(None)
                    if not passphrase:
                        self._cancel_or_quit()
                        return
                    try:
                        self._rebuild_supervisor_if_stale()
                        session = Session(passphrase)
                        break
                    except ValueError:
                        err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                                text="wrong passphrase or corrupted identity - try again")
                        err.run()
                        err.destroy()
                    except (OSError, DaemonError) as e:
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

    def _cancel_or_quit(self):
        """User cancelled and there is no usable tray: log it so the caller (a
        supervisor/container) is expected to restart us. With a real tray the
        desktop behaviour is preserved: keep running, hidden in the tray."""
        if self.tray is not None and self.tray.is_embedded():
            self.log("cancel - keeping app in the system tray")
            return
        self.log("cancel - no usable tray (headless/container) - nothing to show")

    def _no_window_headless(self):
        """True right after setup when there is no window and no usable tray  - 
        the app has nothing to show and (in a container) must exit so the
        supervisor restarts it, instead of lingering on a black screen."""
        return self.window is None and not (self.tray is not None and self.tray.is_embedded())

    def poll(self):
        if not self.quitting and not self.supervisor.is_running():
            self.log("aimlessd died - restarting")
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
        self.log("shutting down - stopping aimlessd")
        if self.window:
            try:
                self.window.save_geometry()
            except Exception:
                pass
        # Drop the daemon connection BEFORE stopping the supervisor: in remote
        # mode the session's DaemonClient holds the tunnel socket open, which
        # keeps ssh from exiting promptly and makes quit() stall on
        # child.wait(). Closing it first lets the tunnel tear down instantly.
        if self.session is not None:
            try:
                self.session.daemon.close()
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
    d = None
    try:
        d = DaemonClient(sock_path())
        who = d.request("whoami", timeout=3)
        return int(who.get("pid", 0)) or None
    except Exception:
        return None
    finally:
        if d is not None:
            d.close()


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
        "Comment=AIMless tray + daemon - messages are received in the background\n"
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


def probe_remote_daemon(host, path, identity=None):
    """Open a real SSH tunnel to `path` and get daemon info through it. Returns
    (build, address) - a genuine whoami+status round trip, so 'connected' means
    the daemon actually answered. Raises on any failure so callers surface the
    layer-specific message."""
    t = SSHTunnel(host, path, os.path.join(CONFIG_DIR, "remote-api.sock.tmp"),
                  identity=identity or None)
    try:
        t.start()
        c = DaemonClient(t.local_socket)
        try:
            info = c.request("whoami", timeout=5)
            st = c.request("status", timeout=5)
        finally:
            c.close()
        return st.get("build", "?"), info.get("address", "?")
    finally:
        t.stop()


def _ssh_common(identity=None):
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=accept-new"]
    if identity:
        cmd += ["-i", os.path.expanduser(identity)]
    return cmd


def _scp_common(identity=None):
    cmd = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=accept-new"]
    if identity:
        cmd += ["-i", os.path.expanduser(identity)]
    return cmd


def scp_transfer(host, identity, src, dst, put=True, timeout=60):
    """Copy one file between local and the remote host. put=True: local src to
    remote dst; put=False: remote src to local dst. Raises RuntimeError on
    failure."""
    cmd = _scp_common(identity)
    if put:
        cmd += [src, f"{host}:{dst}"]
    else:
        cmd += [f"{host}:{src}", dst]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("scp timed out")
    if r.returncode != 0:
        raise RuntimeError(f"scp failed: {r.stderr.strip() or f'exit {r.returncode}'}")


def ssh_run(host, identity, remote_cmd, timeout=30):
    """Run a command on the remote host over ssh. Returns (rc, stdout, stderr)."""
    cmd = _ssh_common(identity) + [host, remote_cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"no answer from {host} (ssh timed out)")
    return r.returncode, r.stdout, r.stderr


def migrate_node_stage(host, identity, datadir, log=None, stop_pid=None):
    """Back up the remote node.key and copy the local daemon state (node.key,
    outbound journal, inbox, contacts) onto the remote daemon's datadir. This is
    the 'move my node key' step: the outbound journal carries the per-buddy seq
    counters so recipients don't deduplicate text as replays. The daemon must be
    restarted after this for the new key to take effect."""
    local_key = os.path.join(data_dir(), "node.key")
    if not os.path.exists(local_key):
        raise RuntimeError("no local node.key to migrate - is a local daemon configured?")
    # Stop the local daemon here, in the worker thread, immediately before the
    # guard: the app's poll() can respawn it on the main thread, so the kill
    # and the guard must be as close together as possible. Loop until none is
    # left (the supervisor swap in do_migrate_node removed the respawner, but
    # belt and suspenders).
    if stop_pid is not None:
        try:
            os.kill(stop_pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(stop_pid, 0)
            except OSError:
                break
            time.sleep(0.2)
        else:
            try:
                os.kill(stop_pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)
    # Guard: a live local daemon using this key would collide with the remote.
    if daemon_pid_from_procs() is not None:
        raise RuntimeError("a local daemon is running - stop it before migrating the node key")
    if log:
        log("backing up the remote node.key ...")
    ts = int(time.time())
    backup = f"{datadir}/node.key.pre-migration-{ts}"
    rc, _, err = ssh_run(host, identity, f"test -f {datadir}/node.key && cp -p {datadir}/node.key {backup} && echo ok")
    if rc != 0:
        raise RuntimeError(f"could not back up the remote node.key: {err.strip() or f'exit {rc}'}")
    if log:
        log("copying node.key, journal, inbox and contacts to the remote daemon ...")
    # node.key
    scp_transfer(host, identity, local_key, f"{datadir}/node.key", put=True)
    # journal/ (per-buddy outbox: *.jsonl + *.seq) - carries the seq counters
    local_journal = os.path.join(data_dir(), "journal")
    remote_journal = f"{datadir}/journal"
    ssh_run(host, identity, f"mkdir -p {remote_journal}")
    if os.path.isdir(local_journal):
        for name in os.listdir(local_journal):
            scp_transfer(host, identity, os.path.join(local_journal, name),
                         f"{remote_journal}/{name}", put=True)
    # inbox/ (received-while-away)
    local_inbox = os.path.join(data_dir(), "inbox")
    if os.path.isdir(local_inbox):
        remote_inbox = f"{datadir}/inbox"
        ssh_run(host, identity, f"mkdir -p {remote_inbox}")
        for name in os.listdir(local_inbox):
            scp_transfer(host, identity, os.path.join(local_inbox, name),
                         f"{remote_inbox}/{name}", put=True)
    # contacts.json (watch list - presence resumes instantly)
    local_contacts = os.path.join(data_dir(), "contacts.json")
    if os.path.exists(local_contacts):
        scp_transfer(host, identity, local_contacts, f"{datadir}/contacts.json", put=True)
    # tighten perms on everything we wrote
    ssh_run(host, identity,
            f"chmod 600 {datadir}/node.key {remote_journal}/* 2>/dev/null; "
            f"chmod 600 {datadir}/contacts.json 2>/dev/null || true")
    if os.path.isdir(local_inbox):
        ssh_run(host, identity, f"chmod 600 {remote_inbox}/* 2>/dev/null || true")
    return backup


def detect_remote_container(host, identity, datadir):
    """Best-effort name of the docker container running the remote daemon. The
    datadir path is dirname(api.sock); the compose dir is its parent's parent
    (e.g. .../deploy/docker/aimless-data/state -> .../deploy/docker), and the
    compose convention names the container 'aimless-webtop'. We look for the
    container by name, then by a bind mount containing the datadir. Returns the
    name or None."""
    rc, out, _ = ssh_run(host, identity, "docker ps --format '{{.Names}}'", timeout=20)
    if rc != 0:
        return None
    names = [n.strip() for n in out.splitlines() if n.strip()]
    if not names:
        return None
    # prefer the conventional name
    if "aimless-webtop" in names:
        return "aimless-webtop"
    # else any container whose mount path is an ancestor of the datadir
    for name in names:
        rc2, out2, _ = ssh_run(
            host, identity, f"docker inspect -f '{{{{range .Mounts}}}}{{{{.Source}}}} {{{{end}}}}' {name}",
            timeout=20)
        if rc2 == 0 and datadir.startswith(out2.strip()):
            return name
    return names[0] if "aimless" in " ".join(names) else None


def verify_remote_node_key(host, identity, path, expected_hex, timeout=30, log=None):
    """Open a tunnel to the remote daemon and poll whoami until it reports
    `expected_hex` as its node key (the migration took effect after restart).
    Returns True on match, False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = SSHTunnel(host, path, os.path.join(CONFIG_DIR, "remote-api.sock.tmp"),
                      identity=identity or None)
        try:
            t.start()
            c = DaemonClient(t.local_socket)
            try:
                who = c.request("whoami", timeout=5)
            finally:
                c.close()
            if who.get("key") == expected_hex:
                return True
        except Exception:
            pass
        finally:
            t.stop()
        if log:
            log("waiting for the remote daemon to come up with the new node key ...")
        time.sleep(2)
    return False


def remote_datadir():
    """The daemon's datadir on the remote host, derived from the socket path we
    already know (dirname of api.sock)."""
    ssh = ssh_prefs()
    return os.path.dirname(ssh["remote_socket"])


def node_key_public_hex():
    """The public key of the local daemon's node.key (the address buddies know),
    derived from the 64-byte ed25519 seed stored hex-encoded. Used to verify a
    migrated daemon answers with the right identity."""
    path = os.path.join(data_dir(), "node.key")
    try:
        with open(path) as f:
            seed = bytes.fromhex(f.read().strip())
    except Exception:
        return None
    import nacl.signing
    try:
        return bytes(nacl.signing.SigningKey(seed).verify_key).hex()
    except Exception:
        return None


def discover_remote_socket(host, identity=None):
    """Find a live aimlessd api.sock on the ssh host. A configured host IS remote
    mode, so the socket path should never have to be typed by hand: list api.sock
    candidates under $HOME, then verify each through a real tunnel+whoami. Returns
    (path, build, address) or raises RuntimeError with an actionable message."""
    cmd = _ssh_common(identity) + [host,
        "find $HOME -maxdepth 6 -name api.sock -type s 2>/dev/null"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"no answer from {host} (ssh timed out)")
    if r.returncode != 0:
        raise RuntimeError(f"could not reach {host}: {r.stderr.strip() or f'exit {r.returncode}'}")
    candidates = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if not candidates:
        raise RuntimeError(f"no aimless daemon socket found on {host} - is the daemon running there?")
    for path in candidates:
        try:
            build, addr = probe_remote_daemon(host, path, identity)
            return path, build, addr
        except Exception:
            continue
    raise RuntimeError(f"found socket(s) on {host} but none answered whoami - check the daemon is up")


def run_ssh_settings_dialog(parent, prompt_restart=True, on_migrate=None):
    """Standalone 'Remote daemon (SSH)' settings dialog. A configured host IS
    remote mode; the daemon socket is discovered automatically (advanced
    override available). The dialog makes the choice explicit with a Local /
    Remote mode selector. Returns True if the config changed, False if
    cancelled/unchanged. When prompt_restart, shows a restart note on change
    (mid-session); the first-run/unlock callers pass False and reconnect live
    instead. on_migrate, if given, is called when 'Move local identity to remote'
    is clicked."""
    ssh = ssh_prefs()
    dlg = Gtk.Dialog(title="Remote daemon (SSH)", transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)

    # Mode selector: explicit Local / Remote choice instead of an implied
    # 'clear the host'. Internal model stays host-based (local = no host).
    mode_local = Gtk.RadioButton.new_with_label_from_widget(None, "Local daemon")
    mode_remote = Gtk.RadioButton.new_with_label_from_widget(mode_local,
                                                             "Remote daemon (SSH)")
    if ssh:
        mode_remote.set_active(True)
    else:
        mode_local.set_active(True)
    box.add(mode_local)
    box.add(mode_remote)

    def row(label, placeholder, default):
        h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        h.pack_start(Gtk.Label(label=label, xalign=0.0, width_chars=18), False, False, 0)
        e = Gtk.Entry()
        e.set_placeholder_text(placeholder)
        e.set_text(default or "")
        e.set_hexpand(True)
        h.pack_start(e, True, True, 0)
        box.add(h)
        return e

    host = row("Host", "user@server", ssh.get("host") or "")
    ident = row("Identity key (optional)", "~/.ssh/id_ed25519", ssh.get("identity") or "")
    hint = Gtk.Label(label=("The daemon socket is found automatically on the host - "
                            "no need to type a path."),
                     xalign=0.0, wrap=True)
    box.add(hint)

    find_btn = Gtk.Button(label="Find daemon on host")
    box.add(find_btn)

    result_label = Gtk.Label(label="", xalign=0.0)
    result_label.set_line_wrap(True)
    box.add(result_label)

    # Socket path is a plain field (filled automatically by discovery; the
    # 'advanced override' lives in the field itself, no empty expander).
    remote = row("Socket path", "/abs/path/to/api.sock", ssh.get("remote_socket") or "")

    test_btn = Gtk.Button(label="Test connection")
    box.add(test_btn)
    test_label = Gtk.Label(label="", xalign=0.0)
    test_label.set_line_wrap(True)
    box.add(test_label)

    # "Move local identity to remote" is available whenever a local daemon state
    # exists and remote mode is selected (the anytime 'move to a VPS' operation).
    # Pass the live dialog values - the user may not have saved yet.
    migrate_btn = None
    if node_key_public_hex() is not None:
        migrate_btn = Gtk.Button(label="Move local identity to remote ...")
        migrate_btn.connect(
            "clicked",
            lambda *_: (on_migrate(host.get_text().strip(),
                                   remote.get_text().strip(),
                                   ident.get_text().strip())
                        if on_migrate else None))
        migrate_btn.set_no_show_all(True)
        migrate_btn.hide()
        box.add(migrate_btn)

    # Hard gate: saving a remote config requires a successful live test (a
    # wrong host means the app silently can't reach its mailbox after a restart).
    # Going local needs no test.
    test_ok = {"value": False}
    save_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)

    def remote_mode():
        return mode_remote.get_active()

    def set_remote_fields_sensitive(sensitive):
        for w in (host, ident, remote, find_btn, result_label, test_btn, test_label, hint):
            try:
                w.set_sensitive(sensitive)
            except Exception:
                pass
        if migrate_btn is not None:
            if sensitive:
                migrate_btn.show()
            else:
                migrate_btn.hide()

    def update_gate():
        if save_btn is None:
            return
        save_btn.set_sensitive((not remote_mode()) or bool(test_ok["value"]))

    def on_field_changed(*_):
        test_ok["value"] = False
        update_gate()

    def on_mode_changed(*_):
        if remote_mode():
            set_remote_fields_sensitive(True)
        else:
            test_ok["value"] = False
            set_remote_fields_sensitive(False)
        update_gate()

    host.connect("changed", on_field_changed)
    ident.connect("changed", on_field_changed)
    remote.connect("changed", on_field_changed)
    mode_local.connect("toggled", on_mode_changed)
    mode_remote.connect("toggled", on_mode_changed)
    set_remote_fields_sensitive(remote_mode())
    update_gate()

    def do_find(*_):
        h = host.get_text().strip()
        i = ident.get_text().strip()
        if not h:
            result_label.set_text("enter a host first")
            return
        find_btn.set_sensitive(False)
        result_label.set_text(f"searching {h} …")

        def worker():
            return discover_remote_socket(h, i or None)

        def done(res):
            find_btn.set_sensitive(True)
            path, build, addr = res
            remote.set_text(path)
            result_label.set_text("")  # stop showing "searching …" once found
            test_label.set_text(f"found daemon at {path} - {build}, {addr}")
            test_ok["value"] = True  # discovery verified with a real round trip
            update_gate()

        def fail(exc):
            find_btn.set_sensitive(True)
            result_label.set_text(str(exc))
            test_ok["value"] = False
            update_gate()

        run_async(worker, on_done=done, on_error=fail)

    find_btn.connect("clicked", do_find)

    def do_test(*_):
        h = host.get_text().strip()
        i = ident.get_text().strip()
        r = remote.get_text().strip()
        if not h:
            test_label.set_text("enter a host first")
            return
        if not r:
            test_label.set_text("no socket path - click 'Find daemon on host' first")
            return
        test_btn.set_sensitive(False)
        test_label.set_text(f"connecting to {h} …")

        def worker():
            # same probe discovery uses - a real whoami+status round trip, so
            # the daemon build/address show up instead of '?'
            build, addr = probe_remote_daemon(h, r, i or None)
            return f"connected - daemon {build} · {addr}"

        def done(msg):
            test_btn.set_sensitive(True)
            test_label.set_text(msg)
            test_ok["value"] = True
            update_gate()

        def fail(exc):
            test_btn.set_sensitive(True)
            test_label.set_text(str(exc))
            test_ok["value"] = False
            update_gate()

        run_async(worker, on_done=done, on_error=fail)

    test_btn.connect("clicked", do_test)
    dlg.show_all()
    resp = dlg.run()
    # capture entry values BEFORE destroying the dialog - get_text() on a
    # destroyed Gtk.Entry returns "".
    remote_selected = mode_remote.get_active()
    host_val = host.get_text().strip()
    ident_val = ident.get_text().strip()
    remote_val = remote.get_text().strip()
    dlg.destroy()
    if resp != Gtk.ResponseType.OK:
        return False
    old = ssh_prefs()
    if not remote_selected:
        new_ssh = {}  # going local
    else:
        if not host_val or not remote_val:
            return False  # remote selected but nothing valid to save
        new_ssh = {"host": host_val, "remote_socket": remote_val}
        if ident_val:
            new_ssh["identity"] = ident_val
    changed = (old.get("host") != new_ssh.get("host")
               or old.get("remote_socket") != new_ssh.get("remote_socket")
               or old.get("identity") != new_ssh.get("identity"))
    if changed:
        prefs = load_prefs()
        prefs["ssh"] = new_ssh
        save_prefs(prefs)
        if prompt_restart:
            dlg2 = Gtk.MessageDialog(transient_for=parent, modal=True,
                                     message_type=Gtk.MessageType.INFO,
                                     buttons=Gtk.ButtonsType.OK,
                                     text="Saved. The running app is still using the "
                                          "previous daemon connection - restart to "
                                          "switch.")
            dlg2.run()
            dlg2.destroy()
    return changed
