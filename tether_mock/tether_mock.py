#!/usr/bin/env python3
"""tether_mock.py - single-file Tether-style messages UI with SimpleCal-style tray daemon.

tether_mock.py                  open the messages window
tether_mock.py --demo           open with the reference conversation loaded
tether_mock.py --daemon         run the tray daemon (pid file + heartbeat)
tether_mock.py --install-autostart
tether_mock.py --stop
"""

import json
import os
import signal
import subprocess
import sys
import warnings
from datetime import datetime

warnings.filterwarnings("ignore", category=DeprecationWarning)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango

APP_NAME = "Tether"
CONFIG_DIR = os.path.expanduser("~/.config/tether-mock")
PID_FILE = os.path.join(CONFIG_DIR, "daemon.pid")
PREFS_FILE = os.path.join(CONFIG_DIR, "gtk.json")
SCRIPT = os.path.abspath(sys.argv[0])

CSS = """
headerbar {
    background-image: none;
    background-color: #14161d;
    color: #e8eaf0;
    border-bottom: 1px solid #0d0e13;
    min-height: 40px;
}

.tether-window {
    background-color: #191b22;
}

.tether-window button {
    background-image: none;
    background-color: #262a35;
    color: #dfe3ec;
    border: 1px solid #3a3e4a;
    border-radius: 8px;
}

.tether-window button:hover { background-color: #2e3240; }
.tether-window button:active { background-color: #33363f; }
.tether-window button:checked { background-color: #33363f; }
.tether-window button:disabled { opacity: 0.5; }

.tether-send {
    padding: 10px 20px;
}

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

menu {
    background-color: #1e212b;
    color: #dfe3ec;
    border: 1px solid #3a3e4a;
    border-radius: 6px;
}

menuitem { color: #dfe3ec; }
menuitem:hover { background-color: #33363f; }

.muted {
    opacity: 0.62;
    font-size: 90%;
}

.tether-sidebar scrolledwindow,
.tether-sidebar list,
.tether-sidebar row {
    background-color: #21242e;
}

.tether-sidebar row:hover { background-color: #262a35; }
.tether-sidebar row:selected { background-color: #323748; }

.tether-chat,
.tether-chat stack,
.tether-chat scrolledwindow,
.tether-chat list,
.tether-chat row {
    background-color: #191b22;
}

.tether-chat separator {
    background-color: #2a2d37;
    min-height: 1px;
}

.tether-bubble {
    padding: 8px 12px;
    border-radius: 14px;
}

.tether-bubble-in {
    background-color: #31343d;
    color: #e8eaf0;
}

.tether-bubble-out {
    background-color: #8ab4f8;
    color: #10131a;
}

.tether-badge {
    background-color: #7fa8f0;
    color: #10131a;
    border-radius: 10px;
    padding: 0 8px;
    font-size: 85%;
}

.tether-composer-frame {
    background-color: #1e212b;
    border: 1px solid #3a3e4a;
    border-radius: 6px;
}

.tether-composer-frame textview,
.tether-composer-frame textview text {
    background-color: transparent;
    color: #e8eaf0;
    caret-color: #e8eaf0;
}

.tether-window entry {
    background-color: #1e212b;
    color: #e8eaf0;
    border: 1px solid #3a3e4a;
    border-radius: 6px;
    padding: 6px 10px;
}

.tether-window entry:focus { border-color: #4a5060; }

.tether-route-bar {
    background-color: #16181f;
    border-top: 1px solid #2a2d37;
    color: #aab0bd;
}

.tether-route-bar image { color: #aab0bd; }
"""

DEMO_THREADS = [
    {
        "name": "Johnny Appleseed",
        "preview": "Send me the link, I want it …",
        "unread": 2,
        "messages": [
            (False, "Did the orchard photos come through?", "17:49"),
            (True, "Yeah, all 40 of them. My phone tried to email them to me one at a time.", "17:52"),
            (False, "That is the Apple way", "17:55"),
            (True, "Dropped them on the Linux box straight from the phone instead. Took four seconds.", "17:58"),
            (False, "Wait, you can do that now?", "21:17"),
            (False, "Send me the link, I want it on mine tonight 🍎", "21:19"),
        ],
    },
    {"name": "274624", "preview": "Your verification code is 41…", "unread": 1, "messages": []},
    {"name": "Mom", "preview": "Bring the pie 🥧", "unread": 1, "messages": []},
    {"name": "Dana Chen", "preview": "Sure. I'll draft them tonight and …", "unread": 0, "messages": []},
    {"name": "Sam Rivera", "preview": "Nice. Ping me when you're clos…", "unread": 0, "messages": []},
]


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


def read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def check_daemon_running():
    import fcntl
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fh = open(PID_FILE, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            try:
                with open(PID_FILE) as f:
                    return int(f.read().strip())
            except Exception:
                return -1
        fh.close()
        return None
    except Exception:
        return None


def install_autostart():
    autostart_dir = os.path.expanduser("~/.config/autostart")
    desktop_path = os.path.join(autostart_dir, "tether-mock.desktop")
    script_path = os.path.abspath(__file__)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Tether Daemon\n"
        "Comment=Tether tray daemon\n"
        f"Exec={sys.executable} {script_path} --daemon\n"
        "Icon=phone\n"
        "Categories=Utility;\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-XFCE-Autostart-Override=true\n"
        "Hidden=false\n"
        "NoDisplay=false\n"
    )
    try:
        os.makedirs(autostart_dir, exist_ok=True)
        with open(desktop_path, "w") as f:
            f.write(content)
        return desktop_path
    except Exception:
        return None


def stop_daemon():
    pid = read_pid()
    if pid is None:
        print("Daemon is not running")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to {pid}")
    except OSError as e:
        print(f"Could not stop daemon: {e}")


def launch_ui():
    subprocess.Popen(
        [sys.executable, SCRIPT],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


TRAY_SCRIPT_TEMPLATE = """
import gi, sys, os, signal, subprocess
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

pid_file  = {pid_file!r}
script    = {script!r}

def _get_daemon_pid():
    try:
        with open(pid_file) as f: return int(f.read().strip())
    except: return None

def open_messages(*_):
    subprocess.Popen([sys.executable, script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def stop_daemon(*_):
    pid = _get_daemon_pid()
    if pid:
        try: os.kill(pid, signal.SIGTERM)
        except: pass
    Gtk.main_quit()

def on_activate(icon, *_):
    open_messages()

def on_popup(icon, button, t):
    menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, t)

menu = Gtk.Menu()
mi_open = Gtk.MenuItem(label='Open Messages')
mi_open.connect('activate', open_messages)
menu.append(mi_open)
menu.append(Gtk.SeparatorMenuItem())
mi_stop = Gtk.MenuItem(label='Stop Daemon')
mi_stop.connect('activate', stop_daemon)
menu.append(mi_stop)
menu.show_all()

icon = Gtk.StatusIcon()
icon.set_from_icon_name('phone')
icon.set_title('Tether')
icon.set_tooltip_text('Tether \\u2014 daemon running\\nLeft-click to open Messages')
icon.connect('activate', on_activate)
icon.connect('popup-menu', on_popup)
icon.set_visible(True)

def _heartbeat():
    pid = _get_daemon_pid()
    alive = False
    if pid is not None:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    if not alive:
        Gtk.main_quit()
        return False
    return True
GLib.timeout_add_seconds(10, _heartbeat)

Gtk.main()
"""


def spawn_tray():
    try:
        script_code = TRAY_SCRIPT_TEMPLATE.format(pid_file=PID_FILE, script=SCRIPT)
        return subprocess.Popen(
            [sys.executable, "-c", script_code],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[Tether daemon] Tray unavailable: {e}")
        return None


def run_daemon():
    import fcntl
    os.makedirs(CONFIG_DIR, exist_ok=True)

    pid_fh = open(PID_FILE, "a+")
    try:
        fcntl.flock(pid_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[Tether daemon] Another daemon already holds the lock, exiting")
        return
    pid_fh.seek(0)
    pid_fh.truncate()
    pid_fh.write(str(os.getpid()))
    pid_fh.flush()
    print(f"[Tether daemon] PID {os.getpid()} — tray supervision active")

    tray_proc = spawn_tray()
    loop = GLib.MainLoop()

    def cleanup(*_):
        print("[Tether daemon] Shutting down")
        if tray_proc:
            try:
                tray_proc.terminate()
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


class MessagesView(Gtk.Box):
    def __init__(self, demo=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.get_style_context().add_class("tether-chat")
        self.threads = [dict(t, widgets=None) for t in DEMO_THREADS] if demo else []
        self.selected = None

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)

        thread_side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        thread_side.get_style_context().add_class("tether-sidebar")
        thread_side.set_size_request(240, -1)

        self.new_message_btn = Gtk.Button(label="New Message")
        self.new_message_btn.set_image(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
        self.new_message_btn.set_hexpand(True)
        for attr, val in (("margin-top", 8), ("margin-bottom", 8), ("margin-start", 8), ("margin-end", 8)):
            self.new_message_btn.set_property(attr, val)
        self.new_message_btn.connect("clicked", lambda *_: self.open_compose())
        thread_side.pack_start(self.new_message_btn, False, False, 0)

        self.thread_scroll = Gtk.ScrolledWindow()
        self.thread_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.thread_list = Gtk.ListBox()
        self.thread_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.thread_list.connect("row-selected", self.on_thread_selected)
        self.thread_scroll.add(self.thread_list)
        thread_side.pack_start(self.thread_scroll, True, True, 0)

        paned.pack1(thread_side, False, False)
        paned.set_position(240)

        self.placeholder_stack = Gtk.Stack()

        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder_icon = Gtk.Image.new_from_icon_name(
            first_icon("mail-unread-symbolic", "dialog-information-symbolic"), Gtk.IconSize.DIALOG)
        placeholder_label = Gtk.Label(label="Select a conversation")
        placeholder_label.get_style_context().add_class("muted")
        placeholder.pack_start(placeholder_icon, False, False, 0)
        placeholder.pack_start(placeholder_label, False, False, 0)
        self.placeholder_stack.add_titled(placeholder, "placeholder", "placeholder")

        conversation_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.compose_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.compose_bar.set_border_width(8)
        self.compose_bar.pack_start(Gtk.Label(label="To:"), False, False, 0)
        self.compose_entry = Gtk.Entry()
        self.compose_entry.set_placeholder_text("Phone number or email")
        self.compose_entry.connect("activate", lambda *_: self.create_thread())
        self.compose_bar.pack_start(self.compose_entry, True, True, 0)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.set_valign(Gtk.Align.CENTER)
        cancel_btn.connect("clicked", lambda *_: self.compose_bar.hide())
        self.compose_bar.pack_start(cancel_btn, False, False, 0)
        self.compose_bar.set_no_show_all(True)
        conversation_box.pack_start(self.compose_bar, False, False, 0)

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
        composer_frame = Gtk.Frame()
        composer_frame.set_shadow_type(Gtk.ShadowType.NONE)
        composer_frame.get_style_context().add_class("tether-composer-frame")
        composer_frame.add(self.composer)
        composer_box.pack_start(composer_frame, True, True, 0)
        self.send_button = Gtk.Button(label="Send")
        self.send_button.set_valign(Gtk.Align.END)
        self.send_button.get_style_context().add_class("tether-send")
        self.send_button.connect("clicked", lambda *_: self.send_message())
        composer_box.pack_start(self.send_button, False, False, 0)
        conversation_box.pack_start(composer_box, False, False, 0)

        self.placeholder_stack.add_titled(conversation_box, "conversation", "conversation")
        self.placeholder_stack.set_visible_child_name("placeholder")

        paned.pack2(self.placeholder_stack, True, True)
        self.pack_start(paned, True, True, 0)

        for thread in self.threads:
            self.append_thread_row(thread)

    def open_compose(self):
        self.compose_bar.show()
        self.compose_entry.grab_focus()

    def create_thread(self):
        name = self.compose_entry.get_text().strip()
        if not name:
            return
        thread = dict(name=name, preview="", unread=0, messages=[], widgets=None)
        self.threads.append(thread)
        self.append_thread_row(thread)
        self.compose_entry.set_text("")
        self.compose_bar.hide()
        if thread.get("widgets"):
            self.thread_list.select_row(thread["widgets"]["row"])

    def append_thread_row(self, thread):
        row = Gtk.ListBoxRow()

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_border_width(10)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        title = Gtk.Label()
        title.set_markup(f"<b>{GLib.markup_escape_text(thread['name'])}</b>")
        title.set_xalign(0.0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        labels.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(label=thread["preview"])
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
            badge.get_style_context().add_class("tether-badge")
            meta.pack_start(badge, False, False, 0)

        box.pack_start(meta, False, False, 0)
        row.add(box)
        self.thread_list.add(row)
        thread["widgets"] = {"row": row, "title": title, "subtitle": subtitle, "meta": meta, "badge": badge}
        row.show_all()

    def on_thread_selected(self, listbox, row):
        if row is None:
            self.placeholder_stack.set_visible_child_name("placeholder")
            self.selected = None
            return
        thread = next((t for t in self.threads if t.get("widgets") and t["widgets"]["row"] is row), None)
        if thread is None:
            return
        self.selected = thread
        if thread["unread"] > 0:
            thread["unread"] = 0
            widgets = thread["widgets"]
            if widgets["badge"]:
                widgets["badge"].destroy()
                widgets["badge"] = None
        self.conversation_header.set_markup(
            f"<big><b>{GLib.markup_escape_text(thread['name'])}</b></big>")
        clear_children(self.conversation)
        for outgoing, body, stamp in thread["messages"]:
            self.append_message_row(outgoing, body, stamp)
        self.placeholder_stack.set_visible_child_name("conversation")
        scroll_to_bottom(self.conversation_scroll)

    def append_message_row(self, outgoing, body, stamp):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for attr, val in (("margin-start", 8), ("margin-end", 8), ("margin-top", 8), ("margin-bottom", 1)):
            box.set_property(attr, val)
        box.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)

        bubble = Gtk.Label()
        bubble.set_markup(GLib.markup_escape_text(body))
        bubble.set_line_wrap(True)
        bubble.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        bubble.set_max_width_chars(48)
        bubble.set_xalign(0.0)
        bubble.set_selectable(True)
        bubble.set_halign(Gtk.Align.END if outgoing else Gtk.Align.START)
        style = bubble.get_style_context()
        style.add_class("tether-bubble")
        style.add_class("tether-bubble-out" if outgoing else "tether-bubble-in")
        box.pack_start(bubble, False, False, 0)

        if stamp:
            time_label = Gtk.Label(label=stamp)
            time_label.set_xalign(1.0 if outgoing else 0.0)
            time_label.get_style_context().add_class("muted")
            box.pack_start(time_label, False, False, 0)

        row.add(box)
        self.conversation.add(row)
        row.show_all()

    def send_message(self):
        if self.selected is None:
            return
        buffer = self.composer.get_buffer()
        start, end = buffer.get_bounds()
        text = buffer.get_text(start, end, True).strip()
        if not text:
            return
        stamp = datetime.now().strftime("%H:%M")
        self.selected["messages"].append((True, text, stamp))
        self.append_message_row(True, text, stamp)
        self.selected["preview"] = text
        widgets = self.selected.get("widgets")
        if widgets:
            widgets["subtitle"].set_text(text)
        buffer.set_text("")
        scroll_to_bottom(self.conversation_scroll)


class TetherWindow(Gtk.Window):
    def __init__(self, demo=False):
        super().__init__(title=APP_NAME)
        self.get_style_context().add_class("tether-window")
        self.set_default_icon_name(first_icon("phone", "smartphone", "applications-internet"))
        self.prefs = load_prefs()
        width = self.prefs.get("window_width", 1008)
        height = self.prefs.get("window_height", 723)
        self.set_default_size(width, height)
        self.connect("delete-event", self.on_delete)

        header_bar = Gtk.HeaderBar()
        header_bar.set_show_close_button(True)
        header_bar.set_title(APP_NAME)
        self.set_titlebar(header_bar)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        devices_view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        devices_view.set_valign(Gtk.Align.CENTER)
        devices_view.set_halign(Gtk.Align.CENTER)
        devices_icon = Gtk.Image.new_from_icon_name(
            first_icon("bluetooth-disabled-symbolic", "dialog-information-symbolic"), Gtk.IconSize.DIALOG)
        devices_label = Gtk.Label(label="No devices discovered")
        devices_label.get_style_context().add_class("muted")
        devices_view.pack_start(devices_icon, False, False, 0)
        devices_view.pack_start(devices_label, False, False, 0)
        self.stack.add_titled(devices_view, "devices", "Devices")

        self.messages_view = MessagesView(demo=demo)
        self.stack.add_titled(self.messages_view, "messages", "Messages")

        notifications_view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        notifications_view.set_valign(Gtk.Align.CENTER)
        notifications_view.set_halign(Gtk.Align.CENTER)
        notifications_icon = Gtk.Image.new_from_icon_name(
            first_icon("preferences-system-notifications-symbolic", "dialog-information-symbolic"),
            Gtk.IconSize.DIALOG)
        notifications_label = Gtk.Label(label="No notifications")
        notifications_label.get_style_context().add_class("muted")
        notifications_view.pack_start(notifications_icon, False, False, 0)
        notifications_view.pack_start(notifications_label, False, False, 0)
        self.stack.add_titled(notifications_view, "notifications", "Notifications")

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        header_bar.set_custom_title(switcher)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        self.refresh_button.connect("clicked", lambda *_: None)
        header_bar.pack_start(self.refresh_button)

        menu_button = Gtk.MenuButton()
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        header_bar.pack_end(menu_button)

        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.root.pack_start(self.stack, True, True, 0)

        route_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        route_bar.set_border_width(6)
        route_bar.get_style_context().add_class("tether-route-bar")

        wifi_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        wifi_box.pack_start(
            Gtk.Image.new_from_icon_name("network-wireless-signal-excellent-symbolic", Gtk.IconSize.MENU),
            False, False, 0)
        wifi_box.pack_start(Gtk.Label(label="Wi-Fi: connected"), False, False, 0)
        route_bar.pack_start(wifi_box, False, False, 0)

        bt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bt_box.pack_start(
            Gtk.Image.new_from_icon_name(first_icon("bluetooth-active-symbolic", "dialog-information-symbolic"),
                                         Gtk.IconSize.MENU),
            False, False, 0)
        bt_box.pack_start(Gtk.Label(label="Bluetooth: connected"), False, False, 0)
        route_bar.pack_start(bt_box, False, False, 0)

        self.root.pack_start(route_bar, False, False, 0)
        self.add(self.root)

        self.stack.connect("notify::visible-child-name", self.on_view_changed)

        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        accel.connect(Gdk.keyval_from_name("n"), Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE,
                      lambda *_: (self.stack.set_visible_child(self.messages_view),
                                  self.messages_view.open_compose()) and False)
        for key, view in (("1", "devices"), ("2", "messages"), ("3", "notifications")):
            accel.connect(Gdk.keyval_from_name(key), Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE,
                          lambda *_, v=view: self.stack.set_visible_child_name(v))
        accel.connect(Gdk.keyval_from_name("w"), Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE,
                      lambda *_: self.close())

        options_menu = Gtk.Menu()

        daemon_hdr = Gtk.MenuItem()
        daemon_hdr_label = Gtk.Label()
        daemon_hdr_label.set_markup("<b>DAEMON</b>")
        daemon_hdr_label.set_halign(Gtk.Align.START)
        daemon_hdr.add(daemon_hdr_label)
        daemon_hdr.set_sensitive(False)
        options_menu.append(daemon_hdr)

        self.daemon_status = Gtk.MenuItem()
        self.daemon_status_label = Gtk.Label()
        self.daemon_status_label.set_halign(Gtk.Align.START)
        self.daemon_status.add(self.daemon_status_label)
        self.daemon_status.set_sensitive(False)
        options_menu.append(self.daemon_status)

        self.daemon_stop = Gtk.MenuItem(label="Stop Daemon")
        self.daemon_stop.connect("activate", lambda *_: stop_daemon())
        options_menu.append(self.daemon_stop)

        self.autostart_status = Gtk.MenuItem()
        self.autostart_status_label = Gtk.Label()
        self.autostart_status_label.set_halign(Gtk.Align.START)
        self.autostart_status.add(self.autostart_status_label)
        self.autostart_status.set_sensitive(False)
        options_menu.append(self.autostart_status)

        self.autostart_btn = Gtk.MenuItem(label="Enable Autostart on Login")
        self.autostart_btn.connect("activate", self.on_install_autostart)
        options_menu.append(self.autostart_btn)

        options_menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: self.destroy())
        options_menu.append(quit_item)
        options_menu.show_all()
        options_menu.connect("show", lambda *_: self.refresh_daemon_menu())
        menu_button.set_popup(options_menu)

        self.connect("destroy", self.on_destroy)
        if demo and self.messages_view.threads:
            GLib.idle_add(self._select_demo_thread)

    def _select_demo_thread(self):
        first = self.messages_view.threads[0]
        widgets = first.get("widgets")
        if widgets:
            self.messages_view.thread_list.select_row(widgets["row"])
        return False

    def on_view_changed(self, stack, param):
        name = stack.get_visible_child_name() or ""
        self.refresh_button.set_visible(name == "devices")

    def refresh_daemon_menu(self):
        pid = check_daemon_running()
        if pid:
            self.daemon_status_label.set_markup(
                f"<span foreground='#a6e3a1'>✓  Daemon active  (PID {pid})</span>")
            self.daemon_stop.set_sensitive(True)
        else:
            self.daemon_status_label.set_markup(
                "<span foreground='#9aa0ad'>○  No daemon — launching one now…</span>")
            self.daemon_stop.set_sensitive(False)
            try:
                subprocess.Popen(
                    [sys.executable, SCRIPT, "--daemon"],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        autostart_desktop = os.path.expanduser("~/.config/autostart/tether-mock.desktop")
        if os.path.exists(autostart_desktop):
            self.autostart_status_label.set_markup(
                "<span foreground='#94e2d5'>⚑  Autostart on login enabled</span>")
            self.autostart_btn.set_sensitive(False)
        else:
            self.autostart_status_label.set_markup(
                "<span foreground='#9aa0ad'>  Not in XFCE autostart</span>")
            self.autostart_btn.set_sensitive(True)

    def on_install_autostart(self, *_):
        path = install_autostart()
        if path:
            self.autostart_status_label.set_markup(
                f"<span foreground='#a6e3a1'>✓  Autostart installed: {path}</span>")
            self.autostart_btn.set_sensitive(False)

    def on_delete(self, *_):
        self.save_geometry()
        return False

    def on_destroy(self, *_):
        self.save_geometry()
        Gtk.main_quit()

    def save_geometry(self):
        width, height = self.get_size()
        self.prefs["window_width"] = width
        self.prefs["window_height"] = height
        save_prefs(self.prefs)


def run_ui(demo):
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS.encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    win = TetherWindow(demo=demo)
    win.show_all()
    win.stack.set_visible_child_name("messages")
    win.messages_view.compose_bar.hide()
    Gtk.main()


def main():
    args = sys.argv[1:]
    if "--install-autostart" in args:
        path = install_autostart()
        print(f"Installed {path}" if path else "Could not install autostart")
    elif "--stop" in args:
        stop_daemon()
    elif "--daemon" in args:
        run_daemon()
    else:
        run_ui("--demo" in args)


if __name__ == "__main__":
    main()
