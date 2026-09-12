#!/usr/bin/env python3
"""gtkui.py — aimless GTK desktop app.

One process: messages window + tray icon + the aimlessd daemon.

Modes:
  aimless            everything: tray icon, daemon, messages window
  aimless tray       starts hidden — tray icon only, window opens on first click (autostart)
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
from datetime import datetime, timedelta

warnings.filterwarnings("ignore", category=DeprecationWarning)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango, GdkPixbuf, Gio

from . import crypto, protocol, logging
from . import keys as keysmod
from .daemon import DaemonClient, Client, DaemonError
from .service import Sync
from .store import Store
from . import __version__ as client_version
from . import MIN_DAEMON_BUILD

APP_NAME = "AIMless"
CONFIG_DIR = os.environ.get("AIMLESS_CONFIG") or os.path.expanduser("~/.config/aimless")
APP_PID_FILE = os.path.join(CONFIG_DIR, "app.pid")
AIMLESSD_PID_FILE = os.path.join(CONFIG_DIR, "aimlessd.pid")
STATUS_REASSERT_SECONDS = 60
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
    `https://x.example.` drops the trailing period; everything else — including
    <a href=...> the sender literally typed — is escaped to inert text."""
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
        # silently dropped from the message — it's picked up by the next segment
        pos = m.start() + len(url)
    out.append(GLib.markup_escape_text(text[pos:]))
    return "".join(out)


def sock_path():
    return os.environ.get("AIMLESS_SOCK") or os.path.join(data_dir(), "api.sock")


def contacts_path():
    return os.path.join(data_dir(), "client-contacts.json")


def identity_path():
    return os.path.join(data_dir(), "identity.json")


def cache_path():
    return os.path.join(data_dir(), "state.db")

# --- theme palettes ---------------------------------------------------------
# Every colour the UI uses lives here (CSS *and* the Pango markup strings), so
# switching a theme is one dict swap + a CSS reload. Keys are semantic roles.

THEMES = {
    "dark": {
        "name": "Aimless Dark",
        "bg": "#191b22", "header": "#14161d", "surface": "#1e212b",
        "sidebar": "#21242e", "sidebar_hover": "#262a35", "sidebar_selected": "#323748",
        "button": "#262a35", "button_hover": "#2e3240", "button_active": "#33363f",
        "border": "#3a3e4a", "border_dark": "#0d0e13", "sep": "#2a2d37",
        "text": "#e8eaf0", "text2": "#dfe3ec", "text_bright": "#f2f4f8",
        "muted": "#9aa0ad", "muted2": "#aab0bd", "subtitle": "#8c8c8c",
        "bubble_in": "#31343d", "bubble_in_fg": "#e8eaf0",
        "bubble_out": "#8ab4f8", "bubble_out_fg": "#10131a", "out_link": "#0b3d91",
        "badge_bg": "#7fa8f0", "badge_fg": "#10131a", "focus": "#4a5060",
        "log_text": "#c8cdd8",
        "away_bg": "#3a3020", "away_border": "#5a4a28", "away_text": "#f5d78e",
        "away_icon": "#f5d36b", "chip": "#22252e", "chip_hover": "#2c313d",
        "online": "#a6e3a1", "away": "#fab387", "offline": "#6c7086",
        "danger": "#f38ba8", "link": "#8ab4f8",
    },
    "mocha": {
        "name": "Mocha", "mono": False,
        "bg": "#1e1e2e", "header": "#11111b", "surface": "#181825",
        "sidebar": "#181825", "sidebar_hover": "#1e1e2e", "sidebar_selected": "#313244",
        "button": "#313244", "button_hover": "#45475a", "button_active": "#45475a",
        "border": "#45475a", "border_dark": "#11111b", "sep": "#313244",
        "text": "#cdd6f4", "text2": "#cdd6f4", "text_bright": "#ffffff",
        "muted": "#7f849c", "muted2": "#6c7086", "subtitle": "#6c7086",
        "bubble_in": "#313244", "bubble_in_fg": "#cdd6f4",
        "bubble_out": "#89b4fa", "bubble_out_fg": "#1e1e2e", "out_link": "#11111b",
        "badge_bg": "#89b4fa", "badge_fg": "#11111b", "focus": "#6c7086",
        "log_text": "#cdd6f4",
        "away_bg": "#313244", "away_border": "#fab387", "away_text": "#f9e2af",
        "away_icon": "#f9e2af", "chip": "#313244", "chip_hover": "#45475a",
        "online": "#a6e3a1", "away": "#fab387", "offline": "#6c7086",
        "danger": "#f38ba8", "link": "#89b4fa",
    },
    "nord": {
        "name": "Nord", "mono": False,
        "bg": "#2e3440", "header": "#242933", "surface": "#2e3440",
        "sidebar": "#2e3440", "sidebar_hover": "#3b4252", "sidebar_selected": "#434c5e",
        "button": "#3b4252", "button_hover": "#434c5e", "button_active": "#4c566a",
        "border": "#4c566a", "border_dark": "#1b1f27", "sep": "#434c5e",
        "text": "#eceff4", "text2": "#e5e9f0", "text_bright": "#ffffff",
        "muted": "#7b8ea1", "muted2": "#9caabb", "subtitle": "#4c566a",
        "bubble_in": "#3b4252", "bubble_in_fg": "#eceff4",
        "bubble_out": "#88c0d0", "bubble_out_fg": "#2e3440", "out_link": "#2e3440",
        "badge_bg": "#88c0d0", "badge_fg": "#2e3440", "focus": "#4c566a",
        "log_text": "#d8dee9",
        "away_bg": "#3b4252", "away_border": "#d08770", "away_text": "#ebcb8b",
        "away_icon": "#ebcb8b", "chip": "#3b4252", "chip_hover": "#434c5e",
        "online": "#a3be8c", "away": "#ebcb8b", "offline": "#4c566a",
        "danger": "#bf616a", "link": "#88c0d0",
    },
    "tokyo": {
        "name": "Tokyo Night", "mono": False,
        "bg": "#1a1b26", "header": "#16161e", "surface": "#1f2335",
        "sidebar": "#1a1b26", "sidebar_hover": "#24283b", "sidebar_selected": "#292e42",
        "button": "#24283b", "button_hover": "#292e42", "button_active": "#3b4261",
        "border": "#3b4261", "border_dark": "#0f0f14", "sep": "#292e42",
        "text": "#c0caf5", "text2": "#c0caf5", "text_bright": "#ffffff",
        "muted": "#565f89", "muted2": "#737aa2", "subtitle": "#565f89",
        "bubble_in": "#24283b", "bubble_in_fg": "#c0caf5",
        "bubble_out": "#7aa2f7", "bubble_out_fg": "#1a1b26", "out_link": "#1a1b26",
        "badge_bg": "#7aa2f7", "badge_fg": "#1a1b26", "focus": "#565f89",
        "log_text": "#a9b1d6",
        "away_bg": "#24283b", "away_border": "#e0af68", "away_text": "#e0af68",
        "away_icon": "#e0af68", "chip": "#24283b", "chip_hover": "#292e42",
        "online": "#9ece6a", "away": "#e0af68", "offline": "#414868",
        "danger": "#f7768e", "link": "#7aa2f7",
    },
    "latte": {
        "name": "Latte (light)", "mono": False,
        "bg": "#eff1f5", "header": "#e6e9ef", "surface": "#ffffff",
        "sidebar": "#e6e9ef", "sidebar_hover": "#dce0e8", "sidebar_selected": "#ccd0da",
        "button": "#ccd0da", "button_hover": "#bcc0cc", "button_active": "#acb0be",
        "border": "#bcc0cc", "border_dark": "#dce0e8", "sep": "#ccd0da",
        "text": "#4c4f69", "text2": "#4c4f69", "text_bright": "#11111b",
        "muted": "#7c7f93", "muted2": "#9ca0b0", "subtitle": "#8c8fa1",
        "bubble_in": "#e6e9ef", "bubble_in_fg": "#4c4f69",
        "bubble_out": "#1e66f5", "bubble_out_fg": "#eff1f5", "out_link": "#eff1f5",
        "badge_bg": "#1e66f5", "badge_fg": "#eff1f5", "focus": "#9ca0b0",
        "log_text": "#4c4f69",
        "away_bg": "#f2e9de", "away_border": "#df8e1d", "away_text": "#df8e1d",
        "away_icon": "#df8e1d", "chip": "#e6e9ef", "chip_hover": "#dce0e8",
        "online": "#40a02b", "away": "#df8e1d", "offline": "#9ca0b0",
        "danger": "#d20f39", "link": "#1e66f5",
    },
    "dawn": {
        "name": "Dawn (light)", "mono": False,
        "bg": "#faf4ed", "header": "#f2e9de", "surface": "#fffaf3",
        "sidebar": "#f2e9de", "sidebar_hover": "#e4dfde", "sidebar_selected": "#dfdad9",
        "button": "#e4dfde", "button_hover": "#dfdad9", "button_active": "#cecacd",
        "border": "#cecacd", "border_dark": "#e4dfde", "sep": "#dfdad9",
        "text": "#575279", "text2": "#575279", "text_bright": "#26233a",
        "muted": "#9893a5", "muted2": "#797593", "subtitle": "#9893a5",
        "bubble_in": "#f2e9de", "bubble_in_fg": "#575279",
        "bubble_out": "#907aa9", "bubble_out_fg": "#faf4ed", "out_link": "#faf4ed",
        "badge_bg": "#907aa9", "badge_fg": "#faf4ed", "focus": "#9893a5",
        "log_text": "#575279",
        "away_bg": "#f4ede8", "away_border": "#ea9d34", "away_text": "#ea9d34",
        "away_icon": "#ea9d34", "chip": "#f2e9de", "chip_hover": "#e4dfde",
        "online": "#56949f", "away": "#ea9d34", "offline": "#9893a5",
        "danger": "#b4637a", "link": "#907aa9",
    },
    "matrix": {
        "name": "Matrix", "mono": True,
        "bg": "#000000", "header": "#001100", "surface": "#001a0d",
        "sidebar": "#001100", "sidebar_hover": "#032006", "sidebar_selected": "#063a0d",
        "button": "#03310f", "button_hover": "#064a15", "button_active": "#0a5a1e",
        "border": "#0a5a1e", "border_dark": "#001100", "sep": "#03310f",
        "text": "#b8ffb8", "text2": "#b8ffb8", "text_bright": "#e8ffe8",
        "muted": "#0a8a2a", "muted2": "#0a8a2a", "subtitle": "#0a8a2a",
        "bubble_in": "#071f0d", "bubble_in_fg": "#b8ffb8",
        "bubble_out": "#00ff41", "bubble_out_fg": "#001100", "out_link": "#00260a",
        "badge_bg": "#00ff41", "badge_fg": "#001100", "focus": "#0a8a2a",
        "log_text": "#00c22e",
        "away_bg": "#1a1200", "away_border": "#ffb000", "away_text": "#ffb000",
        "away_icon": "#ffb000", "chip": "#03310f", "chip_hover": "#064a15",
        "online": "#00ff41", "away": "#ffb000", "offline": "#1f5a1f",
        "danger": "#ff2a2a", "link": "#39ff88",
    },
    "amber": {
        "name": "Amber CRT", "mono": True,
        "bg": "#0a0700", "header": "#120d00", "surface": "#1a1200",
        "sidebar": "#120d00", "sidebar_hover": "#241a00", "sidebar_selected": "#332500",
        "button": "#241a00", "button_hover": "#332500", "button_active": "#4a3600",
        "border": "#5a4200", "border_dark": "#120d00", "sep": "#332500",
        "text": "#ffc95c", "text2": "#ffc95c", "text_bright": "#ffe9b0",
        "muted": "#a8791f", "muted2": "#a8791f", "subtitle": "#a8791f",
        "bubble_in": "#241a00", "bubble_in_fg": "#ffc95c",
        "bubble_out": "#ffb000", "bubble_out_fg": "#1a1200", "out_link": "#4a3000",
        "badge_bg": "#ffb000", "badge_fg": "#1a1200", "focus": "#a8791f",
        "log_text": "#d9a23a",
        "away_bg": "#241a00", "away_border": "#ffb000", "away_text": "#ffe9b0",
        "away_icon": "#ffb000", "chip": "#241a00", "chip_hover": "#332500",
        "online": "#ffb000", "away": "#ff8c00", "offline": "#5a4200",
        "danger": "#ff5544", "link": "#ffd27f",
    },
}

THEME_ENTRIES = [
    ("system", "System", "Match the desktop"),
    ("dark", "Aimless Dark", "Default"),
    ("mocha", "Mocha", "Catppuccin · dark"),
    ("nord", "Nord", "Arctic · dark"),
    ("tokyo", "Tokyo Night", "Deep blue · dark"),
    ("latte", "Latte", "Catppuccin · light"),
    ("dawn", "Dawn", "Rosé Pine · light"),
    ("matrix", "Matrix", "Hacker green · monospace"),
    ("amber", "Amber CRT", "Phosphor amber · monospace"),
]


def detect_system_palette():
    """Desktop dark/light -> the palette 'System' resolves to."""
    try:
        s = Gtk.Settings.get_default()
        name = (s.get_property("gtk-theme-name") or "").lower()
        dark = bool(s.get_property("gtk-application-prefer-dark-theme")) or any(
            k in name for k in ("dark", "black", "night", "midnight"))
        return "dark" if dark else "latte"
    except Exception:
        return "dark"


def get_theme(name):
    if name == "system" or name not in THEMES:
        name = detect_system_palette()
    return THEMES[name]


_ENV_THEME = os.environ.get("AIMLESS_THEME")
CURRENT_THEME = _ENV_THEME if (_ENV_THEME in THEMES or _ENV_THEME == "system") else "system"
C = get_theme(CURRENT_THEME)
CSS_PROVIDER = None


def build_css(C):
    mono = "font-family: monospace;" if C.get("mono") else ""
    return f"""
* {{ {mono} }}
headerbar {{
    background-image: none;
    background-color: {C['header']};
    color: {C['text']};
    border-bottom: 1px solid {C['border_dark']};
    min-height: 40px;
}}

.aimless-window {{ background-image: none; background-color: {C['bg']}; color: {C['text']}; }}
.aimless-window label {{ color: {C['text']}; }}
.aimless-window .muted {{ color: {C['muted']}; }}

.aimless-window button {{
    background-image: none;
    background-color: {C['button']};
    color: {C['text2']};
    border: 1px solid {C['border']};
    border-radius: 8px;
}}
.aimless-window button:hover {{ background-color: {C['button_hover']}; }}
.aimless-window button:active {{ background-color: {C['button_active']}; }}
.aimless-window button:checked {{ background-color: {C['button_active']}; }}
.aimless-window button:disabled {{ opacity: 0.5; }}

.aimless-send {{ padding: 10px 20px; }}

stackswitcher {{ background-color: {C['surface']}; border-radius: 8px; }}
stackswitcher > button {{
    background-image: none; background-color: transparent; border: none; box-shadow: none;
    color: {C['muted2']}; padding: 5px 14px; margin: 2px; border-radius: 6px; outline: none;
}}
stackswitcher > button:checked {{ background-color: {C['button_active']}; color: {C['text_bright']}; }}

menu {{ background-color: {C['surface']}; color: {C['text2']}; border: 1px solid {C['border']}; border-radius: 6px; }}
menuitem {{ color: {C['text2']}; }}
menuitem:hover {{ background-color: {C['button_active']}; }}

.muted {{ color: {C['muted']}; font-size: 90%; }}

.aimless-sidebar scrolledwindow,
.aimless-sidebar list,
.aimless-sidebar row {{ background-color: {C['sidebar']}; }}
.aimless-sidebar row:hover {{ background-color: {C['sidebar_hover']}; }}
.aimless-sidebar row:selected {{ background-color: {C['sidebar_selected']}; }}
.aimless-sidebar row label {{ color: {C['text2']}; }}

.aimless-chat row label {{ color: {C['text']}; }}
.aimless-chat,
.aimless-chat stack,
.aimless-chat scrolledwindow,
.aimless-chat list,
.aimless-chat row {{ background-color: {C['bg']}; }}
.aimless-chat separator {{ background-color: {C['sep']}; min-height: 1px; }}

.aimless-bubble {{ padding: 8px 12px; border-radius: 14px; }}
.aimless-bubble-in {{ background-color: {C['bubble_in']}; }}
.aimless-bubble-out {{ background-color: {C['bubble_out']}; }}
.aimless-chat row .aimless-bubble-in {{ color: {C['bubble_in_fg']}; }}
.aimless-chat row .aimless-bubble-out {{ color: {C['bubble_out_fg']}; }}
.aimless-bubble-out link, .aimless-bubble-out link:visited {{ color: {C['out_link']}; }}
.aimless-bubble-in link, .aimless-bubble-in link:visited {{ color: {C['link']}; }}

.aimless-badge {{
    background-color: {C['badge_bg']}; color: {C['badge_fg']};
    border-radius: 10px; padding: 0 8px; font-size: 85%;
}}

.aimless-composer-frame {{ background-color: {C['surface']}; border: 1px solid {C['border']}; border-radius: 6px; }}
.aimless-composer-frame textview,
.aimless-composer-frame textview text {{ background-color: transparent; color: {C['text']}; caret-color: {C['text']}; }}

.aimless-window entry {{
    background-color: {C['surface']}; color: {C['text']};
    border: 1px solid {C['border']}; border-radius: 6px; padding: 6px 10px;
}}
.aimless-window entry:focus {{ border-color: {C['focus']}; }}

.aimless-log text,
.aimless-log textview,
.aimless-log textview text {{ background-color: {C['header']}; color: {C['log_text']}; }}

.aimless-away-banner {{
    background-color: {C['away_bg']};
    border-top: 1px solid {C['away_border']};
    border-bottom: 1px solid {C['away_border']};
    color: {C['away_text']};
}}
.aimless-away-banner image {{ color: {C['away_icon']}; }}

.aimless-route-bar {{ background-color: {C['header']}; border-top: 1px solid {C['sep']}; color: {C['muted2']}; }}
.aimless-route-bar image {{ color: {C['muted2']}; }}

.aimless-contacts frame {{ border-color: {C['border']}; }}
.aimless-muted {{ opacity: 0.55; }}

.aimless-chip {{ padding: 2px 8px; margin: 1px; border-radius: 11px; background-color: {C['chip']}; }}
.aimless-chip:hover {{ background-color: {C['chip_hover']}; }}

.aimless-jump {{
    background-color: {C['badge_bg']}; color: {C['badge_fg']};
    border-radius: 14px; padding: 4px 12px;
}}
"""


def install_css_provider():
    """Create the single app CSS provider and load the current palette."""
    global CSS_PROVIDER
    CSS_PROVIDER = Gtk.CssProvider()
    CSS_PROVIDER.load_from_data(build_css(C).encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), CSS_PROVIDER, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def set_theme(name):
    """Swap the active palette and reload the CSS in place (no re-add needed)."""
    global CURRENT_THEME, C
    CURRENT_THEME = name
    C = get_theme(name)
    if CSS_PROVIDER is not None:
        CSS_PROVIDER.load_from_data(build_css(C).encode())
    return C


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


DEFAULT_PREFS = {
    "remember_position": True,
    "notifications": True,
    "notification_sound": "single",
    "enter_to_send": True,
    "time_format": "24h",
    "theme": "system",           # used by the 0.8.3 theming work
    "seen_onboarding": False,
}


def pref(prefs, key):
    return prefs.get(key, DEFAULT_PREFS.get(key))


def format_time(ts, time_format="24h"):
    dt = datetime.fromtimestamp(ts / 1000)
    if time_format == "12h":
        return dt.strftime("%I:%M %p").lstrip("0")
    return dt.strftime("%H:%M")


def date_label(ts, now=None):
    """Divider label for a message timestamp: Today / Yesterday / date."""
    day = datetime.fromtimestamp(ts / 1000).date()
    today = (now or datetime.now()).date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if day.year == today.year:
        return day.strftime("%A, %d %b")
    return day.strftime("%d %b %Y")


def clamp_to_workarea(x, y, w, h, areas, margin=40):
    """Nudge a saved top-left so some of the window is visible.

    ``areas`` is an iterable of monitor workareas (ax, ay, aw, ah). If the point
    already lands on a monitor it is kept; otherwise it is clamped into the
    first (primary) workarea so a window closed on a since-removed monitor
    reopens on-screen."""
    areas = list(areas)
    if not areas:
        return x, y
    for ax, ay, aw, ah in areas:
        if (ax - w + margin <= x <= ax + aw - margin
                and ay - h + margin <= y <= ay + ah - margin):
            return x, y
    ax, ay, aw, ah = areas[0]
    return (min(max(x, ax), ax + max(0, aw - w)),
            min(max(y, ay), ay + max(0, ah - h)))


# --- desktop notifications (best-effort) ------------------------------------
# libnotify bindings if present, else notify-send, else silently nothing.
_NOTIFY = {"ready": None, "mod": None}


def _notify_init():
    if _NOTIFY["ready"] is not None:
        return _NOTIFY["ready"]
    _NOTIFY["ready"] = False
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify
        Notify.init(APP_NAME)
        _NOTIFY["mod"] = Notify
        _NOTIFY["ready"] = True
    except Exception:
        _NOTIFY["mod"] = None
    return _NOTIFY["ready"]


def notify_new_message(sender, text):
    body = (text or "")[:200]
    if _notify_init():
        try:
            _NOTIFY["mod"].Notification.new(sender, body, "mail-unread").show()
            return
        except Exception:
            pass
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "--app-name", APP_NAME, sender, body])
        except Exception:
            pass


def should_notify(window_visible, muted, blocked, prefs):
    if not pref(prefs, "notifications"):
        return False
    if muted or blocked:
        return False
    return not window_visible


# --- notification sounds (synthesised; no audio files shipped) ---------------
# A short tone via speaker-test (as SimpleCal does), with paplay of a system
# event file and the display bell as fallbacks. Everything is best-effort and
# silent when there is no sound backend (e.g. in the container).

SOUND_OPTIONS = ("off", "single", "double", "triple", "long")
SOUND_MIN_GAP = 2.0
_BEEP = "timeout {dur}s speaker-test -t sine -f 800 -l 1"
_LAST_SOUND = [0.0]


def sound_command(kind):
    """The shell sequence for a sound option ("" for off/unknown). Pure."""
    if kind not in SOUND_OPTIONS or kind == "off":
        return ""
    if kind == "long":
        return _BEEP.format(dur="0.5")
    parts = []
    for i in range({"single": 1, "double": 2, "triple": 3}.get(kind, 0)):
        if i:
            parts.append("sleep 0.12")
        parts.append(_BEEP.format(dur="0.2"))
    return "; ".join(parts)


def sound_enabled(prefs):
    """Effective sound choice: AIMLESS_SOUND env overrides the saved pref."""
    env = os.environ.get("AIMLESS_SOUND")
    if env in SOUND_OPTIONS:
        return env
    kind = pref(prefs, "notification_sound")
    return kind if kind in SOUND_OPTIONS else "single"


def should_play_sound(window_visible, muted, blocked, prefs):
    if muted or blocked or window_visible:
        return False
    return sound_enabled(prefs) != "off"


def _run_beep(shell_cmd):
    if shutil.which("speaker-test") and shell_cmd:
        try:
            subprocess.Popen(["bash", "-c", shell_cmd], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except OSError:
            pass
    if shutil.which("paplay"):
        for path in ("/usr/share/sounds/freedesktop/stereo/message.oga",
                     "/usr/share/sounds/freedesktop/stereo/bell.oga"):
            if os.path.exists(path):
                try:
                    subprocess.Popen(["paplay", path], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
                    return
                except OSError:
                    pass
    try:
        display = Gdk.Display.get_default()
        if display is not None and hasattr(display, "beep"):
            display.beep()
    except Exception:
        pass


def play_notification_sound(kind, force=False):
    if kind not in SOUND_OPTIONS or kind == "off":
        return
    now = time.monotonic()
    if not force and now - _LAST_SOUND[0] < SOUND_MIN_GAP:
        return
    _LAST_SOUND[0] = now
    _run_beep(sound_command(kind))


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
            # which fires a display tick AFTER this timeout runs — so the first
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
    return os.path.join(data_dir(), "state.db")


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
            self.store = Store(cache_path(), passphrase)
        except Exception as e:
            try:
                os.replace(cache_path(), cache_path() + ".bad")
            except OSError:
                pass
            self.cache_recovered = f"corrupted state recovered as .bad ({e})"
            self.store = Store(cache_path(), passphrase)
        # Transitional alias: the GTK layer still speaks the cache vocabulary.
        self.cache = self.store
        self.daemon = DaemonClient(sock_path())
        self.self_node = self.daemon.request("whoami")["key"]
        contacts = protocol.load_contacts(contacts_path())
        for info in contacts.values():
            node = info.get("node")
            if node and self.store.is_muted(node):
                self.store.unmute(node)
        self.self_screen = contacts.get("_self", {}).get("screen", "anonymous")
        self.client = Client(self.daemon, self.identity, self.self_screen)
        self.sync = Sync(self.client, self.store)
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

    def refresh_node(self):
        """Re-read the daemon's node key after a restart (address may change)."""
        self.self_node = self.daemon.request("whoami")["key"]
        return self.self_node


def _room_dots_markup(members, presence_by_node, exclude):
    """One presence dot per room member (sorted like the title), excluding self."""
    parts = []
    for screen, node in sorted((m.get("screen") or n[:8], n) for n, m in members.items()
                               if n != exclude):
        p = presence_by_node.get(node, {})
        color = C["online"] if p.get("online") else (C["away"] if p.get("away") else C["offline"])
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
        dot_color = C["online"] if online else C["offline"]
        return (f"<span foreground='{dot_color}'>●</span>  "
                f"<span size='small' foreground='{C['subtitle']}'>{online}/{len(others)}</span>  "
                f"<b>{esc(thread['screen'])}</b>")
    dot_color = C["online"] if thread["online"] else (C["away"] if thread["away"] else C["offline"])
    return f"<span foreground='{dot_color}'>●</span>  <b>{esc(thread['screen'])}</b>"


def _room_header_markup(thread, self_node):
    dots = _room_dots_markup(thread.get("members", {}), thread.get("presence_by_node", {}), self_node)
    others = [n for n in thread.get("members", {}) if n != self_node]
    pb = thread.get("presence_by_node", {})
    online = sum(1 for n in others if pb.get(n, {}).get("online"))
    return (f"<big><b>{GLib.markup_escape_text(thread['screen'])}</b></big>  {dots}  "
            f"<span size='small' foreground='{C['subtitle']}'>{online}/{len(others)} online</span>")


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
        # Sent-file delivery tracking: (node, seq) -> delivery record, plus a
        # small buffer of already-seen acks so an ack that lands while a send is
        # still being enqueued is not lost.
        self._pending_files = {}
        self._acked_seen = set()
        self._bubble_status = {}   # outgoing message id -> status Gtk.Label
        self._last_msg_date = None
        self._at_bottom = True

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

        self.sidebar_empty = Gtk.Label(
            label="No conversations yet.\nAdd a buddy in Contacts and share your invite.")
        self.sidebar_empty.set_xalign(0.0)
        self.sidebar_empty.set_line_wrap(True)
        self.sidebar_empty.set_margin_start(10)
        self.sidebar_empty.set_margin_end(10)
        self.sidebar_empty.set_margin_top(8)
        self.sidebar_empty.set_margin_bottom(10)
        self.sidebar_empty.get_style_context().add_class("muted")
        self.sidebar_empty.set_no_show_all(True)
        sidebar.pack_start(self.sidebar_empty, False, False, 0)

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
        ph_hint = Gtk.Label(label="Add a buddy in Contacts and share your invite to start chatting.")
        ph_hint.get_style_context().add_class("muted")
        ph_hint.set_justify(Gtk.Justification.CENTER)
        ph_hint.set_line_wrap(True)
        ph_hint.set_max_width_chars(40)
        placeholder.pack_start(ph_icon, False, False, 0)
        placeholder.pack_start(ph_label, False, False, 0)
        placeholder.pack_start(ph_hint, False, False, 0)
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

        self.jump_btn = Gtk.Button(label="↓ New messages")
        self.jump_btn.set_halign(Gtk.Align.CENTER)
        self.jump_btn.set_valign(Gtk.Align.END)
        self.jump_btn.set_margin_bottom(6)
        self.jump_btn.set_no_show_all(True)
        self.jump_btn.get_style_context().add_class("aimless-jump")
        self.jump_btn.connect("clicked", lambda *_: self.jump_to_bottom())
        conversation_box.pack_start(self.jump_btn, False, False, 0)
        self.conversation_scroll.get_vadjustment().connect("value-changed", self._on_scrolled)

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
        if self.threads:
            self.sidebar_empty.hide()
        else:
            self.sidebar_empty.show()

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
        self.app.refresh_unread_indicator()

    def catchup_unread(self):
        """Startup sweep: route everything the daemon journaled while no client
        was connected, then count what arrived per conversation.

        Uses the single per-peer sync path (the daemon keeps running between app
        sessions, and recv events broadcast to nobody are lost)."""
        if self._catchup_busy:
            return
        self._catchup_busy = True
        session = self.app.session
        peers = set()
        for conv, thread in list(self.threads.items()):
            nodes = list(thread.get("members", {}).keys()) if thread.get("is_room") else [conv]
            peers.update(nodes)

        def worker():
            return session.sync.fetch(peers)

        def done(hists):
            self._catchup_busy = False
            before = {c: len(session.store.messages(c)) for c in self.threads}
            for peer, resp in hists.items():
                session.sync.apply(peer, resp)
            arrived = 0
            for conv, thread in self.threads.items():
                delta = len(session.store.messages(conv)) - before.get(conv, 0)
                if delta > 0:
                    arrived += delta
                    if self.selected is not thread:
                        thread["unread"] += delta
                self.update_thread_row(conv)
            if arrived:
                self.app.activity.log(
                    f"unread sweep: {arrived} message(s) arrived while you were away")

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
        self.app.session.store.mark_read(conv)
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
                f"  <span size='small' foreground='{C['subtitle']}'>{conv[:16]}…</span>")
        self._render_messages(conv, thread)
        self.stack.set_visible_child_name("conversation")
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
            # one stream per sender; every entry is decrypted once and routed to
            # its own conversation, so a single cursor per peer covers all rooms.
            return session.sync.fetch(nodes)

        def done(hists):
            self._history_busy = False
            for peer, resp in hists.items():
                session.sync.apply(peer, resp)
            self._history_loaded(conv)

        def fail(e):
            self._history_busy = False
            self._history_failed(e)

        run_async(worker, on_done=done, on_error=fail)

    def _history_loaded(self, conv):
        if self.selected is None or self.selected.get("conv") != conv:
            return False
        self._render_messages(conv, self.selected)
        return False

    def refresh_conversation(self, conv):
        """Redraw an open conversation after messages were stored outside the
        live-event path (e.g. a message released by Accept on a request popup)."""
        if self.selected is not None and self.selected.get("conv") == conv:
            self._history_loaded(conv)

    def _history_failed(self, e):
        self.append_system_note(f"history unavailable: {e}")
        return False

    def append_bubble(self, outgoing, text, ts, sender=None, attachment=None, msg=None):
        stamp = format_time(ts, pref(self.app.prefs, "time_format")) if ts else ""
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
        if outgoing and msg is not None and not attachment:
            total = msg.get("delivery_total")
            if total:
                acked = msg.get("delivered_count", 0)
                status = Gtk.Label()
                status.set_xalign(1.0)
                status.get_style_context().add_class("muted")
                if acked >= total:
                    status.set_text("✓ delivered")
                elif acked:
                    status.set_text(f"• {acked}/{total} delivered")
                else:
                    status.set_text("• sending…")
                box.pack_start(status, False, False, 0)
                self._bubble_status[msg["id"]] = status
        if stamp:
            time_label = Gtk.Label(label=stamp)
            time_label.set_xalign(1.0 if outgoing else 0.0)
            time_label.get_style_context().add_class("muted")
            box.pack_start(time_label, False, False, 0)
        row.add(box)
        self.conversation.add(row)
        row.show_all()

    # -- scrolling / date dividers ----------------------------------------
    def _on_scrolled(self, adj):
        self._at_bottom = self._near_bottom(adj)
        if self._at_bottom:
            self.jump_btn.hide()

    def _near_bottom(self, adj, margin=40):
        try:
            return adj.get_value() >= adj.get_upper() - adj.get_page_size() - margin
        except Exception:
            return True

    def jump_to_bottom(self):
        self.jump_btn.hide()
        self._at_bottom = True
        scroll_to_bottom(self.conversation_scroll)

    def _maybe_scroll(self):
        if self._at_bottom:
            scroll_to_bottom(self.conversation_scroll)
        else:
            self.jump_btn.show()

    def _maybe_date_divider(self, ts):
        day = datetime.fromtimestamp(ts / 1000).date()
        if day == self._last_msg_date:
            return
        self._last_msg_date = day
        self.append_date_divider(date_label(ts))

    def append_date_divider(self, text):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0.5)
        lbl.get_style_context().add_class("muted")
        row.add(lbl)
        self.conversation.add(row)
        row.show_all()

    def _render_messages(self, conv, thread):
        clear_children(self.conversation)
        self._bubble_status = {}
        self._last_msg_date = None
        store = self.app.session.store
        for m in store.messages(conv):
            self._maybe_date_divider(m["ts"])
            self.append_bubble(m["dir"] == "out", m["text"], m["ts"],
                               sender=None if m["dir"] == "out" else self._sender_label(thread, m),
                               attachment=m.get("attachment"), msg=m)
            if m["dir"] == "out" and m.get("attachment") and m.get("delivered") is not None:
                total, _done = store.delivery_members(m["id"])
                if total:
                    row = self._append_status_row("")
                    rec = {"row": row, "filename": m["text"], "members_total": total,
                           "keys": set(store.undelivered(m["id"]))}
                    for k in rec["keys"]:
                        self._pending_files[k] = rec
                    self._set_delivery_text(rec)
        self._at_bottom = True
        scroll_to_bottom(self.conversation_scroll)

    def _append_outgoing(self, conv, seqs, ts, text, attachment=None, delivery_keys=None):
        store = self.app.session.store
        store.add_sent(conv, seqs, ts, text, attachment, delivery_keys)
        mid = store.out_id(seqs)
        total, done = store.delivery_members(mid)
        msg = {"id": mid, "dir": "out", "ts": ts, "text": text,
               "attachment": attachment,
               "delivered": (done == total) if total else False,
               "delivery_total": total or None, "delivered_count": done}
        self._maybe_date_divider(ts)
        self.append_bubble(True, text, ts, attachment=attachment, msg=msg)
        return mid

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
        if os.path.exists(path):
            openb = Gtk.Button(label="Open")
            openb.set_relief(Gtk.ReliefStyle.NONE)
            openb.get_style_context().add_class("muted")
            openb.connect("clicked", self.on_open_attachment, path)
            box.pack_start(openb, False, False, 0)
        save = Gtk.Button(label="Save")
        save.set_relief(Gtk.ReliefStyle.NONE)
        save.get_style_context().add_class("muted")
        save.connect("clicked", self.on_save_attachment, path, filename)
        box.pack_start(save, False, False, 0)

    def on_open_attachment(self, _btn, path):
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path, None), None)
        except (GLib.Error, OSError) as e:
            self.app.activity.log(f"couldn't open file: {e}")

    def on_expand_image(self, _w, _ev, path, filename):
        win = Gtk.Window(title=filename)
        win.set_default_size(900, 700)
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(filename)
        save = Gtk.Button(label="Save")
        save.connect("clicked", lambda *_: self.on_save_attachment(None, path, filename))
        hb.pack_end(save)
        win.set_titlebar(hb)
        sc = Gtk.ScrolledWindow()
        win.add(sc)
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file(path)
            img = Gtk.Image.new_from_pixbuf(pix)
            sc.add(img)
        except GLib.Error:
            pass

        def on_key(w, e):
            if e.keyval == Gdk.KEY_Escape:
                w.close()
                return True
            return False
        win.connect("key-press-event", on_key)
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
        self._maybe_scroll()

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
            color = C["online"] if p.get("online") else (C["away"] if p.get("away") else C["offline"])
            known = n in contact_nodes
            glyph = "●" if known else "○"
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            lbl = Gtk.Label()
            lbl.set_markup(f"<span foreground='{color}'>{glyph}</span> {GLib.markup_escape_text(screen)}")
            lbl.set_xalign(0.0)
            btn.add(lbl)
            btn.get_style_context().add_class("aimless-chip")
            btn.set_tooltip_text("Open conversation — your buddy" if known
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
        if event.keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        enter_sends = pref(self.app.prefs, "enter_to_send")
        if (enter_sends and not shift) or (not enter_sends and ctrl):
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
                self._append_outgoing(conv, seqs, ts, text)
                self.selected["preview"] = text
                self.update_thread_row(conv)
                self._maybe_scroll()
        else:
            contact = thread["contact"]

            def worker():
                return self.app.session.client.send(contact["pubkey"], contact["node"], text, ts)

            def done(resp):
                self._send_in_flight = False
                self.send_button.set_sensitive(True)
                self._append_outgoing(conv, {contact["node"]: resp.get("seq", 0)}, ts, text)
                self.selected["preview"] = text
                self.update_thread_row(conv)
                self._maybe_scroll()

        def fail(e):
            self._send_in_flight = False
            self.send_button.set_sensitive(True)
            self.append_system_note(f"⚠ send failed: {e} — the message was not queued")
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
            "Every member receives a full copy — a large transfer to a big room uses "
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
        row = self._append_status_row(f"Sending {filename} — 0/{total}")
        attachment = {"path": path, "filename": filename, "mime_hint": mime_hint, "size": size}

        def worker():
            seqs = {}
            expected = set()
            for i, piece in enumerate(pieces):
                chunk = protocol.make_chunk(tid, i, total, filename, mime_hint, sha, size, piece,
                                            conv=conv if is_room else None)
                if is_room:
                    chunk_seqs = client.send_file_room(list(thread["members"].values()), conv, chunk)
                    seqs.update(chunk_seqs)
                    for n, s in chunk_seqs.items():
                        expected.add((n, int(s)))
                else:
                    payload = protocol.build_file_payload(
                        session.identity, thread["contact"]["pubkey"], chunk)
                    resp = client.send_file(conv, payload)
                    seq = int(resp.get("seq", 0))
                    seqs[conv] = seq
                    expected.add((conv, seq))
                GLib.idle_add(_progress, i + 1)
            return seqs, expected

        def _progress(done):
            self._update_status_row(row, f"Sending {filename} — {done}/{total}")
            return False

        def done(result):
            seqs, expected = result
            if row is not None:
                row.destroy()
            ts = int(time.time() * 1000)
            self._append_outgoing(conv, seqs, ts, filename, attachment=attachment,
                                  delivery_keys=expected)
            if thread is self.selected:
                thread["preview"] = filename
                self.update_thread_row(conv)
            # Persistent indicator under the bubble: the daemon reports an ack
            # per chunk once the peer has stored it, so we can show delivery
            # progress instead of the file silently vanishing into the queue.
            deliver_row = self._append_status_row("")
            self.register_file_delivery(deliver_row, filename, expected)
            self._maybe_scroll()

        def fail(e):
            if row is not None:
                self._update_status_row(row, f"{filename} — send failed: {e}")
            self.app.activity.log(f"send failed: {e}")

        run_async(worker, on_done=done, on_error=fail)

    def register_file_delivery(self, row, filename, keys):
        """Begin tracking delivery of a sent file. ``keys`` is the set of
        (node, seq) chunk acks still outstanding; progress is reported per
        recipient (a recipient is done when all their chunks are acked)."""
        keys = {(n, int(s)) for n, s in keys if int(s)}
        members_total = len({n for n, _ in keys})
        pending = keys - (keys & self._acked_seen)
        rec = {"row": row, "filename": filename, "keys": pending,
               "members_total": members_total}
        for k in pending:
            self._pending_files[k] = rec
        self._set_delivery_text(rec)

    def note_acked(self, node, seq):
        """A sent chunk was delivered (the daemon saw the peer's ACK). Returns
        True when it belonged to a tracked file transfer, so callers can keep
        the activity log for ordinary text acks without spamming it per chunk."""
        key = (node, int(seq or 0))
        self._acked_seen.add(key)
        if len(self._acked_seen) > 4096:
            self._acked_seen.clear()
        store = self.app.session.store
        mid = store.mark_delivered(node, seq)
        hit_file = False
        rec = self._pending_files.pop(key, None)
        if rec is not None:
            rec["keys"].discard(key)
            self._set_delivery_text(rec)
            hit_file = True
        if mid is not None:
            lbl = self._bubble_status.get(mid)
            if lbl is not None:
                total, done = store.delivery_members(mid)
                if total and done >= total:
                    lbl.set_text("✓ delivered")
                elif total:
                    lbl.set_text(f"• {done}/{total} delivered")
        return hit_file

    def _set_delivery_text(self, rec):
        total = rec.get("members_total", 0)
        left = len({n for n, _ in rec["keys"]})
        done = total - left
        if total == 0:
            text = f"Sent {rec['filename']}"
        elif left == 0:
            text = f"Delivered {rec['filename']} ✓"
        elif total == 1:
            text = f"Sending {rec['filename']}…"
        else:
            text = f"Sending {rec['filename']} — {done}/{total} delivered"
        self._update_status_row(rec["row"], text)

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
                self._maybe_scroll()
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
            self._maybe_scroll()
        else:
            thread["unread"] += 1
        self.update_thread_row(conv)
        visible, muted, blocked = self._alert_state(conv, node)
        if should_notify(visible, muted, blocked, self.app.prefs):
            notify_new_message(opened.get("screen") or node[:8], opened["text"])
        if should_play_sound(visible, muted, blocked, self.app.prefs):
            play_notification_sound(sound_enabled(self.app.prefs))

    def _alert_state(self, conv, node):
        """(visible, muted, blocked) for alerting on a message from ``node``."""
        visible = (self.app.is_active()
                   and self.app.stack.get_visible_child_name() == "messages"
                   and self.selected is not None and self.selected.get("conv") == conv)
        return (visible,
                self.app.session.cache.is_conversation_muted(conv),
                self.app.session.cache.is_muted(node))

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
                    f"Receiving {chunk.get('filename') or 'file'} — 1/{chunk['total']}")
        if buf.get("failed"):
            return
        buf["chunks"][chunk["index"]] = base64.b64decode(chunk["data"])
        have = len(buf["chunks"])
        if buf["row"] is not None:
            self._update_status_row(buf["row"], f"Receiving {chunk.get('filename') or 'file'} — {have}/{chunk['total']}")
            self._maybe_scroll()
        if have == buf["total"]:
            if not self._finalize_attachment(conv, node, buf, ev.get("seq", 0), ev.get("ts", 0),
                                              notify=True):
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
            self._update_status_row(buf["row"], f"{meta.get('filename') or 'file'} — failed to receive")

    def _store_attachment(self, conv, tid, filename, data):
        d = os.path.join(attachments_dir(), conv)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{tid}-{sanitize_filename(filename)}")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path

    def _finalize_attachment(self, conv, node, buf, seq, ts, notify=False):
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
                self._maybe_scroll()
            else:
                thread["unread"] += 1
            self.update_thread_row(conv)
        run_async(lambda: self.app.session.client.ack_attachment(node, chunk["transfer_id"]))
        if notify:
            visible, muted, blocked = self._alert_state(conv, node)
            if should_notify(visible, muted, blocked, self.app.prefs):
                notify_new_message(self._sender_label(self.threads.get(conv) or {}, {"sender": node})
                                   or node[:8], f"file: {filename}")
            if should_play_sound(visible, muted, blocked, self.app.prefs):
                play_notification_sound(sound_enabled(self.app.prefs))
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
            color = C["online"] if p.get("online") else C["muted"]
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

    def _confirm_remove(self, petname):
        dlg = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=f"Remove {petname}?",
                                buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(
            "This removes them from your list on this device. They can still send to you "
            "and will prompt you again if they do.")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def on_remove(self, btn, petname):
        if not self._confirm_remove(petname):
            return
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
        self.info_label.set_markup(f"<span foreground='{C['muted']}'>○  checking daemon …</span>")

    def refresh_info(self, st):
        if not st:
            self.info_label.set_markup(
                f"<span foreground='{C['danger']}'>●  offline — daemon not reachable</span>")
            return
        build = st.get("build", "")
        version_note = ""
        if not build:
            version_note = (f"\n<span foreground='{C['danger']}'>this daemon is an old build — "
                            "run `aimless stop`, then reopen aimless to update</span>")
            build = "unknown"
        else:
            try:
                dv = tuple(int(x) for x in build.split("/", 1)[1].split("."))
            except (IndexError, ValueError):
                dv = None
            if dv is not None and dv < MIN_DAEMON_BUILD:
                need = ".".join(str(x) for x in MIN_DAEMON_BUILD)
                version_note = (f"\n<span foreground='{C['danger']}'>daemon {build} is too old for this client "
                                f"(needs ≥ {need}) — update aimlessd-linux-amd64, then `aimless stop` and "
                                "reopen</span>")
                if not getattr(self, "_version_warned", False):
                    self._version_warned = True
                    self.log(f"⚠ daemon {build} is too old for this client (needs ≥ {need}) "
                             f"— update aimlessd-linux-amd64, then `aimless stop` and reopen")
        state = (f"<span foreground='{C['online']}'>●  you are online</span>" if st["peers_up"] > 0
                 else f"<span foreground='{C['away']}'>●  connecting — no Yggdrasil peers yet</span>")
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

        if not pref(self.prefs, "seen_onboarding") and not session.contacts():
            info = Gtk.InfoBar()
            info.set_message_type(Gtk.MessageType.INFO)
            info.get_content_area().add(Gtk.Label(
                label="New to AIMless? Open Contacts to copy your invite and add a buddy."))

            def on_info(bar, resp):
                bar.hide()
                self.prefs["seen_onboarding"] = True
                save_prefs(self.prefs)
                if resp == Gtk.ResponseType.OK:
                    self.stack.set_visible_child_name("contacts")
            info.add_button("Go to Contacts", Gtk.ResponseType.OK)
            info.add_button("Dismiss", Gtk.ResponseType.CLOSE)
            info.connect("response", on_info)
            root.pack_start(info, False, False, 0)

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
        keys_item = Gtk.MenuItem(label="Keys & Identity …")
        keys_item.connect("activate", self.on_keys)
        options_menu.append(keys_item)
        prefs_item = Gtk.MenuItem(label="Preferences …")
        prefs_item.connect("activate", self.on_preferences)
        options_menu.append(prefs_item)
        options_menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Close window")
        quit_item.connect("activate", lambda *_: self.close())
        options_menu.append(quit_item)
        options_menu.show_all()
        menu_button.set_popup(options_menu)

        self.connect("destroy", self.on_destroy)
        self._geometry_restoring = False
        self._geometry_timer = 0
        self._want_geometry = False
        self.connect("configure-event", self.on_configure)
        self.connect("map-event", self._on_map)
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
        GLib.timeout_add_seconds(STATUS_REASSERT_SECONDS, self._reassert_status)
        GLib.idle_add(self.surface_pending_requests)

        self.apply_theme(os.environ.get("AIMLESS_THEME") or pref(self.prefs, "theme"))
        try:
            settings = Gtk.Settings.get_default()
            for prop in ("gtk-theme-name", "gtk-application-prefer-dark-theme"):
                settings.connect(f"notify::{prop}", self._on_system_theme)
        except Exception:
            pass

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
            self.activity.log(f"daemon {'block' if blocked else 'unblock'} failed: {e} — client-side only")

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
            conv = req.get("conv") or req["node"]
            if req.get("conv"):
                members = {m["node"]: {"node": m["node"], "pubkey": m["pubkey"],
                                       "screen": m.get("screen", "")}
                           for m in req.get("members", [])}
                if members:
                    self.session.cache.ensure_room(req["conv"], members)
            self.session.cache.add_recv(conv, req["node"],
                                        req.get("seq", 0), req.get("ts", 0), req.get("text", ""))
            self.contacts.refresh()
            self.messages.sync_sidebar()
            # The withheld message is now stored: redraw it if its conversation
            # is already open, otherwise it only appears after switching threads.
            self.messages.refresh_conversation(conv)
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
                    if not self.messages.note_acked(ev.get("to"), ev.get("seq")):
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
        if not st:
            self.route_label.set_markup(
                f"<span foreground='{C['danger']}'>●  offline — daemon not reachable</span>")
        elif st["peers_up"] == 0:
            self.route_label.set_markup(
                f"<span foreground='{C['away']}'>●  connecting — no Yggdrasil peers yet</span>")
        else:
            self.route_label.set_markup(
                f"<span foreground='{C['online']}'>●  online</span>  —  {st['address']}  ·  "
                f"peers {st['peers_up']}/{st['peers_total']}")

    # -- Keys & Identity --------------------------------------------------
    def _info_dialog(self, title, text, secondary=None):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                text=title, buttons=Gtk.ButtonsType.OK)
        if secondary:
            dlg.format_secondary_text(secondary)
        dlg.run()
        dlg.destroy()

    def _confirm(self, title, secondary):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.WARNING,
                                text=title, buttons=Gtk.ButtonsType.OK_CANCEL)
        dlg.format_secondary_text(secondary)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.OK

    def _daemon_restart(self):
        sup = self.supervisor
        try:
            sup.stop()
        except Exception:
            pass
        for _ in range(50):
            if not sup.is_running():
                break
            time.sleep(0.1)
        try:
            sup.ensure(log=(self.app_ref.log if self.app_ref else None))
        except Exception:
            pass
        for _ in range(150):
            if sup.is_running():
                break
            time.sleep(0.1)

    def _apply_node_seed(self, seed):
        keysmod.write_node_key(data_dir(), seed)
        self._daemon_restart()
        try:
            self.session.refresh_node()
        except Exception:
            pass
        self.messages.sync_sidebar()

    def on_keys(self, *_):
        st = self.supervisor.status() or {}
        dlg = Gtk.Dialog(title="Keys & Identity", transient_for=self, modal=True)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.set_default_size(620, -1)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)

        def section(text):
            frame = Gtk.Frame(label=text)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            inner.set_border_width(10)
            frame.add(inner)
            box.pack_start(frame, False, False, 0)
            return inner

        # --- Yggdrasil node key ---
        node = section("Yggdrasil node key — your address")
        node_status = Gtk.Label(label=f"current: {st.get('address', '?')}")
        node_status.set_xalign(0.0)
        node_status.set_line_wrap(True)
        node.pack_start(node_status, False, False, 0)
        node_entry = Gtk.Entry(placeholder_text="paste a 64- or 128-hex node key")
        node.pack_start(node_entry, False, False, 0)
        node_preview = Gtk.Label(label="")
        node_preview.set_xalign(0.0)
        node_preview.set_line_wrap(True)
        node_preview.get_style_context().add_class("muted")
        node.pack_start(node_preview, False, False, 0)

        def preview_node(*_):
            try:
                _seed, pub = keysmod.parse_node_key(node_entry.get_text())
            except ValueError as e:
                node_preview.set_text(f"error: {e}")
                return
            node_preview.set_text(f"address: {keysmod.yggdrasil_address(pub)}\n"
                                  f"pubkey: {pub.hex()}")

        def apply_node(*_):
            try:
                seed, pub = keysmod.parse_node_key(node_entry.get_text())
            except ValueError as e:
                node_preview.set_text(f"error: {e}")
                return
            addr = keysmod.yggdrasil_address(pub)
            if not self._confirm("Replace your Yggdrasil node key?",
                                 f"Your address becomes {addr}. Anyone who saved your old "
                                 "invite must add you again. The daemon restarts now."):
                return
            self._apply_node_seed(seed)
            node_status.set_text(f"current: {addr}")
            self._info_dialog("Node key applied", f"Your address is now {addr}.")

        def gen_node(*_):
            seed_hex, addr = keysmod.random_node_key()
            node_entry.set_text(seed_hex)
            node_preview.set_text(f"address: {addr}")

        def reset_node(*_):
            if not self._confirm("Reset to a fresh node key?",
                                 "Your current address is discarded and a new one is generated; "
                                 "old invites stop working. The daemon restarts now."):
                return
            keysmod.remove_node_key(data_dir())
            self._daemon_restart()
            try:
                self.session.refresh_node()
            except Exception:
                pass
            self.messages.sync_sidebar()
            node_status.set_text(
                f"current: {(self.supervisor.status() or {}).get('address', '?')}")

        nbtn = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, cb in (("Preview", preview_node), ("Apply", apply_node),
                          ("Generate random", gen_node), ("Reset to default", reset_node)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            nbtn.pack_start(b, False, False, 0)
        node.pack_start(nbtn, False, False, 0)

        # --- client identity ---
        ident = section("Client identity — your encryption key")
        id_status = Gtk.Label(label=f"current pubkey: {self.session.client.pubkey_hex}")
        id_status.set_xalign(0.0)
        id_status.set_line_wrap(True)
        ident.pack_start(id_status, False, False, 0)
        id_entry = Gtk.Entry(placeholder_text="paste a 64-hex identity seed to replace it")
        ident.pack_start(id_entry, False, False, 0)
        id_warn = Gtk.Label(
            label="Replacing your identity changes the key friends encrypt to; existing "
                  "conversations break and everyone must re-add your invite.")
        id_warn.set_xalign(0.0)
        id_warn.set_line_wrap(True)
        id_warn.get_style_context().add_class("muted")
        ident.pack_start(id_warn, False, False, 0)

        def import_identity(*_):
            try:
                seed = bytes.fromhex(id_entry.get_text().strip())
                if len(seed) != 32:
                    raise ValueError
            except ValueError:
                id_warn.set_text("error: expected a 64-hex identity seed")
                return
            if not self._confirm("Replace your client identity?",
                                 "This breaks existing conversations and cannot be undone here. "
                                 "Restart aimless afterwards to use it."):
                return
            pw = self.app_ref.passphrase if self.app_ref else None
            if not pw:
                self._info_dialog("Passphrase unavailable",
                                  "Reopen aimless to import an identity.")
                return
            crypto.save_identity(identity_path(), keysmod.signing_key(seed), pw)
            self._info_dialog("Identity written",
                              "Quit and reopen aimless to use the new identity.")
        idbtn = Gtk.Button(label="Import identity")
        idbtn.connect("clicked", import_identity)
        ident.pack_start(idbtn, False, False, 0)

        # --- backup / restore ---
        bk = section("Backup & restore")
        bk_status = Gtk.Label(label="")
        bk_status.set_xalign(0.0)
        bk_status.set_line_wrap(True)
        bk_status.get_style_context().add_class("muted")
        bk.pack_start(bk_status, False, False, 0)

        def export_backup(*_):
            pw = ask_secret(self, "Backup passphrase", confirm=True)
            if not pw:
                return
            node_seed = None
            try:
                with open(os.path.join(data_dir(), "node.key")) as f:
                    node_seed = f.read().strip()
            except OSError:
                pass
            payload = {
                "identitySeed": bytes(self.session.identity).hex(),
                "nodeSeed": node_seed,
                "screen": self.session.self_screen,
                "pubkey": self.session.client.pubkey_hex,
                "contacts": protocol.load_contacts(contacts_path()),
            }
            chooser = Gtk.FileChooserDialog(
                title="Export backup", transient_for=self, action=Gtk.FileChooserAction.SAVE,
                buttons=("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK))
            chooser.set_current_name(f"aimless-backup-{datetime.now():%Y%m%d}.json")
            resp = chooser.run()
            path = chooser.get_filename()
            chooser.destroy()
            if resp != Gtk.ResponseType.OK or not path:
                return
            try:
                keysmod.save_bundle(path, pw, payload)
                bk_status.set_text(f"backup written to {path}")
            except OSError as e:
                bk_status.set_text(f"backup failed: {e}")

        def import_backup(*_):
            chooser = Gtk.FileChooserDialog(
                title="Import backup", transient_for=self, action=Gtk.FileChooserAction.OPEN,
                buttons=("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK))
            resp = chooser.run()
            path = chooser.get_filename()
            chooser.destroy()
            if resp != Gtk.ResponseType.OK or not path:
                return
            pw = ask_secret(self, "Backup passphrase")
            if not pw:
                return
            try:
                data = keysmod.load_bundle(path, pw)
                seed = bytes.fromhex(data.get("identitySeed", ""))
                if len(seed) != 32:
                    raise ValueError("missing identity seed")
            except (ValueError, OSError, KeyError) as e:
                bk_status.set_text(f"import failed: {e}")
                return
            addr = "?"
            if data.get("nodeSeed"):
                try:
                    addr = keysmod.yggdrasil_address(
                        keysmod.parse_node_key(data["nodeSeed"])[1])
                except ValueError:
                    addr = "?"
            n = len([k for k in (data.get("contacts") or {}) if k != "_self"])
            if not self._confirm(
                    "Restore this backup?",
                    f"identity {data.get('pubkey', '?')[:16]}…\nnode address {addr}\n"
                    f"{n} contact(s)\n\nThis overwrites your current keys; restart aimless after."):
                return
            app_pw = self.app_ref.passphrase if self.app_ref else None
            if not app_pw:
                self._info_dialog("Passphrase unavailable",
                                  "Reopen aimless to import a backup.")
                return
            crypto.save_identity(identity_path(), keysmod.signing_key(seed), app_pw)
            if data.get("nodeSeed"):
                keysmod.write_node_key(data_dir(), bytes.fromhex(data["nodeSeed"]))
            if data.get("contacts"):
                protocol.save_contacts(contacts_path(), data["contacts"])
            self._daemon_restart()
            self._info_dialog("Backup restored",
                              "Quit and reopen aimless to use the restored identity.")

        bbtn = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        eb = Gtk.Button(label="Export encrypted backup")
        eb.connect("clicked", export_backup)
        ib = Gtk.Button(label="Import backup")
        ib.connect("clicked", import_backup)
        bbtn.pack_start(eb, False, False, 0)
        bbtn.pack_start(ib, False, False, 0)
        bk.pack_start(bbtn, False, False, 0)

        box.show_all()
        dlg.run()
        dlg.destroy()

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

    def on_configure(self, *_):
        # Debounced: configure-event fires continuously while dragging/resizing.
        if self._geometry_restoring:
            return False
        if not self._geometry_timer:
            self._geometry_timer = GLib.timeout_add(1000, self._save_geometry_now)
        return False

    def _save_geometry_now(self):
        self._geometry_timer = 0
        self.save_geometry()
        return False

    def _workareas(self):
        areas = []
        try:
            display = Gdk.Display.get_default()
            if display is not None:
                for i in range(display.get_n_monitors()):
                    geo = display.get_monitor(i).get_workarea()
                    areas.append((geo.x, geo.y, geo.width, geo.height))
        except Exception:
            pass
        if not areas:
            try:
                screen = Gdk.Screen.get_default()
                if screen is not None:
                    areas.append((0, 0, screen.get_width(), screen.get_height()))
            except Exception:
                pass
        return areas

    def save_geometry(self):
        self.prefs["window_width"], self.prefs["window_height"] = self.get_size()
        if pref(self.prefs, "remember_position"):
            try:
                gw = self.get_window()
                x, y = self.get_position()
                if (x, y) != (0, 0):  # (0,0) == "unknown" (Wayland)
                    self.prefs["window_x"], self.prefs["window_y"] = x, y
                if gw is not None:
                    self.prefs["window_maximized"] = bool(
                        gw.get_state() & Gdk.WindowState.MAXIMIZED)
            except Exception:
                pass
        save_prefs(self.prefs)

    def restore_geometry(self):
        if not pref(self.prefs, "remember_position"):
            return
        # Apply now (works as the initial-position hint on X11), and again once
        # the window is mapped — most window managers ignore pre-map move
        # requests and only honour one made after the window is shown.
        self._want_geometry = True
        self._apply_saved_position()

    def _apply_saved_position(self):
        x, y = self.prefs.get("window_x"), self.prefs.get("window_y")
        w, h = self.get_size()
        self._geometry_restoring = True
        try:
            if isinstance(x, int) and isinstance(y, int) and (x, y) != (0, 0):
                cx, cy = clamp_to_workarea(x, y, w, h, self._workareas())
                self.move(cx, cy)
            if self.prefs.get("window_maximized"):
                self.maximize()
        except Exception:
            pass
        finally:
            self._geometry_restoring = False

    def _on_map(self, *_):
        if getattr(self, "_want_geometry", False):
            self._want_geometry = False
            GLib.idle_add(self._apply_saved_position)
        return False

    def reset_geometry(self):
        for k in ("window_x", "window_y", "window_maximized"):
            self.prefs.pop(k, None)
        save_prefs(self.prefs)

    def refresh_unread_indicator(self):
        messages = getattr(self, "messages", None)
        if messages is None:
            return
        n = sum(t.get("unread", 0) for t in messages.threads.values())
        self.set_title(f"{APP_NAME} ({n})" if n else APP_NAME)
        tray = getattr(self.app_ref, "tray", None) if self.app_ref else None
        if tray is not None and getattr(tray, "have_tray", False):
            try:
                tray.icon.set_tooltip_text(
                    f"{APP_NAME} — {n} unread\nLeft-click to open Messages" if n
                    else f"{APP_NAME} — running\nLeft-click to open Messages")
            except Exception:
                pass

    def apply_theme(self, name):
        # AIMLESS_THEME is a session override and wins over the saved choice.
        env = os.environ.get("AIMLESS_THEME")
        if env in THEMES or env == "system":
            name = env
        if name != "system" and name not in THEMES:
            name = "system"
        set_theme(name)
        # Palette colours also live in Pango markup outside the CSS, so redraw
        # the widgets that embed them.
        try:
            self.messages.sync_sidebar()
            if self.messages.selected is not None:
                self.messages._render_messages(self.messages.selected["conv"],
                                               self.messages.selected)
            self.contacts.refresh()
            self.poll_status()
        except Exception:
            pass

    def _on_system_theme(self, *_):
        if CURRENT_THEME == "system":
            self.apply_theme("system")

    def on_preferences(self, *_):
        dlg = Gtk.Dialog(title="Preferences", transient_for=self, modal=True)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.set_default_size(440, -1)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)

        def row(label, widget):
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0.0)
            lbl.set_line_wrap(True)
            r.pack_start(lbl, True, True, 0)
            r.pack_end(widget, False, False, 0)
            box.pack_start(r, False, False, 0)
            return r

        def switch(key):
            sw = Gtk.Switch()
            sw.set_active(bool(pref(self.prefs, key)))
            sw.set_valign(Gtk.Align.CENTER)

            def on_toggle(w, _p, k=key):
                self.prefs[k] = w.get_active()
                if k == "remember_position" and not w.get_active():
                    self.reset_geometry()
                save_prefs(self.prefs)
            sw.connect("notify::active", on_toggle)
            return sw

        row("Remember window position and size", switch("remember_position"))
        reset_btn = Gtk.Button(label="Reset window position")
        reset_btn.set_halign(Gtk.Align.START)
        reset_btn.get_style_context().add_class("muted")
        reset_btn.connect("clicked", lambda *_: (self.reset_geometry(), self.unmaximize(),
                                                 self.activity.log("window position reset")))
        box.pack_start(reset_btn, False, False, 0)
        row("Desktop notifications for new messages", switch("notifications"))

        snd = Gtk.ComboBoxText()
        for value, label in (("off", "Off"), ("single", "Single beep"),
                             ("double", "Double beep"), ("triple", "Triple beep"),
                             ("long", "Long beep")):
            snd.append(value, label)
        snd.set_active_id(pref(self.prefs, "notification_sound"))

        def on_snd(w):
            self.prefs["notification_sound"] = w.get_active_id() or "single"
            save_prefs(self.prefs)
        snd.connect("changed", on_snd)
        snd_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        snd_box.pack_start(snd, False, False, 0)
        test_btn = Gtk.Button(label="Test")
        test_btn.get_style_context().add_class("muted")
        test_btn.connect("clicked", lambda *_: play_notification_sound(
            sound_enabled(self.prefs), force=True))
        snd_box.pack_start(test_btn, False, False, 0)
        row("Notification sound", snd_box)
        if os.environ.get("AIMLESS_SOUND"):
            note = Gtk.Label(label="AIMLESS_SOUND is set and overrides this choice.")
            note.set_xalign(0.0)
            note.get_style_context().add_class("muted")
            box.pack_start(note, False, False, 0)

        row("Press Enter to send (Shift+Enter for a new line)", switch("enter_to_send"))

        fmt = Gtk.ComboBoxText()
        fmt.append("24h", "24-hour")
        fmt.append("12h", "12-hour")
        fmt.set_active_id(pref(self.prefs, "time_format"))

        def on_fmt(w):
            self.prefs["time_format"] = w.get_active_id() or "24h"
            save_prefs(self.prefs)
            if self.messages.selected is not None:
                self.messages._history_loaded(self.messages.selected["conv"])
        fmt.connect("changed", on_fmt)
        row("Timestamp format", fmt)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        theme_combo = Gtk.ComboBoxText()
        for tid, label, _desc in THEME_ENTRIES:
            theme_combo.append(tid, label)
        theme_combo.set_active_id(os.environ.get("AIMLESS_THEME") or pref(self.prefs, "theme"))

        def on_theme(w):
            name = w.get_active_id() or "system"
            self.prefs["theme"] = name
            save_prefs(self.prefs)
            self.apply_theme(name)
        theme_combo.connect("changed", on_theme)
        row("Theme", theme_combo)
        if os.environ.get("AIMLESS_THEME"):
            env_note = Gtk.Label(label="AIMLESS_THEME is set and overrides this choice.")
            env_note.set_xalign(0.0)
            env_note.get_style_context().add_class("muted")
            box.pack_start(env_note, False, False, 0)

        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        keys_btn = Gtk.Button(label="Keys & identity…")
        keys_btn.set_halign(Gtk.Align.START)
        keys_btn.connect("clicked", lambda *_: self.on_keys())
        box.pack_start(keys_btn, False, False, 0)

        box.show_all()
        dlg.run()
        dlg.destroy()


def ask_secret(parent, title, confirm=False):
    dlg = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(380, -1)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    lbl = Gtk.Label(label=title)
    lbl.set_xalign(0.0)
    box.add(lbl)
    e1 = Gtk.Entry()
    e1.set_visibility(False)
    box.add(e1)
    e2 = None
    if confirm:
        e2 = Gtk.Entry()
        e2.set_visibility(False)
        e2.set_placeholder_text("confirm")
        box.add(e2)
    box.show_all()
    resp = dlg.run()
    pw = e1.get_text()
    conf = e2.get_text() if e2 is not None else pw
    dlg.destroy()
    if resp != Gtk.ResponseType.OK or not pw or pw != conf:
        return None
    return pw


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


def ask_create_identity(parent):
    """Ask for passphrase + confirm + screen name to create a brand-new identity.
    Returns None (cancelled / empty), ("__mismatch__",) or (passphrase, screen)."""
    dlg = Gtk.Dialog(title=f"{APP_NAME} — create your identity", transient_for=parent, modal=True)
    dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create identity", Gtk.ResponseType.OK)
    dlg.set_default_response(Gtk.ResponseType.OK)
    dlg.set_default_size(380, 120)
    box = dlg.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.add(Gtk.Label(label="No identity on this machine yet — set one up here."))
    box.add(Gtk.Label(label="Passphrase (protects your keys; re-enter it later to unlock)"))
    pw = Gtk.Entry(visibility=False, activates_default=True)
    box.add(pw)
    box.add(Gtk.Label(label="Confirm passphrase"))
    pw2 = Gtk.Entry(visibility=False, activates_default=True)
    box.add(pw2)
    box.add(Gtk.Label(label="Screen name"))
    screen = Gtk.Entry(activates_default=True)
    box.add(screen)
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

    def _setup(self, open_window):
        sys.excepthook = _make_excepthook(self.log)
        threading.excepthook = _make_thread_hook(self.log)

        install_css_provider()

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
        if self._no_window_headless():
            self.log("no window after setup (no usable tray) — exiting for the supervisor to restart")
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
            for _create in range(3):
                created = ask_create_identity(None)
                if created is None:
                    self._cancel_or_quit()
                    return
                if created[0] == "__mismatch__":
                    err = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                            text="passphrases do not match — try again")
                    err.run()
                    err.destroy()
                    continue
                new_pw, screen = created
                try:
                    create_identity(new_pw, screen)
                    passphrase = new_pw
                    session = Session(passphrase)
                    break
                except OSError as e:
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
                        self._cancel_or_quit()
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
        self.window.restore_geometry()
        self.window.show_all()

    def _cancel_or_quit(self):
        """User cancelled and there is no usable tray: log it so the caller (a
        supervisor/container) is expected to restart us. With a real tray the
        desktop behaviour is preserved: keep running, hidden in the tray."""
        if self.tray is not None and self.tray.is_embedded():
            self.log("cancel — keeping app in the system tray")
            return
        self.log("cancel — no usable tray (headless/container) — nothing to show")

    def _no_window_headless(self):
        """True right after setup when there is no window and no usable tray —
        the app has nothing to show and (in a container) must exit so the
        supervisor restarts it, instead of lingering on a black screen."""
        return self.window is None and not (self.tray is not None and self.tray.is_embedded())

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
