"""Single source of truth for every on-disk path aimless uses.

Everything lives under one root (``AIMLESS_HOME``, default
``~/.local/share/aimless``), split by owner::

    <root>/client/   identity, contacts, history, prefs, attachments
    <root>/daemon/   node key, yggdrasil config, journal, API socket
    <root>/logs/     app.log, daemon.log, tunnel.log
    <root>/run/      pid files

The one exception is the freedesktop autostart entry, which the spec pins to
``$XDG_CONFIG_HOME/autostart``.
"""

import os


def root() -> str:
    return os.environ.get("AIMLESS_HOME") or os.path.expanduser("~/.local/share/aimless")


def client_dir() -> str:
    return os.path.join(root(), "client")


def daemon_dir() -> str:
    return os.path.join(root(), "daemon")


def logs_dir() -> str:
    return os.path.join(root(), "logs")


def run_dir() -> str:
    return os.path.join(root(), "run")


def identity_path() -> str:
    return os.path.join(client_dir(), "identity.json")


def contacts_path() -> str:
    return os.path.join(client_dir(), "contacts.json")


def cache_path() -> str:
    return os.path.join(client_dir(), "state.db")


def prefs_path() -> str:
    return os.path.join(client_dir(), "prefs.json")


def attachments_dir() -> str:
    return os.path.join(client_dir(), "attachments")


def sock_path() -> str:
    return os.environ.get("AIMLESS_SOCK") or os.path.join(daemon_dir(), "api.sock")


def app_log_path() -> str:
    return os.path.join(logs_dir(), "app.log")


def daemon_log_path() -> str:
    return os.path.join(logs_dir(), "daemon.log")


def tunnel_log_path() -> str:
    return os.path.join(logs_dir(), "tunnel.log")


def app_pid_path() -> str:
    return os.path.join(run_dir(), "app.pid")


def daemon_pid_path() -> str:
    return os.path.join(run_dir(), "aimlessd.pid")


def tunnel_pid_path() -> str:
    return os.path.join(run_dir(), "tunnel.pid")


def config_home() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def autostart_path() -> str:
    return os.path.join(config_home(), "autostart", "aimless-tray.desktop")


def ensure_dirs() -> None:
    """Create the whole tree, private to the user."""
    for d in (root(), client_dir(), daemon_dir(), logs_dir(), run_dir()):
        os.makedirs(d, mode=0o700, exist_ok=True)


def all_paths() -> dict:
    """Resolved paths, for `aimless paths` and diagnostics."""
    return {
        "root": root(),
        "client": client_dir(),
        "daemon": daemon_dir(),
        "logs": logs_dir(),
        "run": run_dir(),
        "identity": identity_path(),
        "contacts": contacts_path(),
        "history": cache_path(),
        "prefs": prefs_path(),
        "attachments": attachments_dir(),
        "socket": sock_path(),
        "app_log": app_log_path(),
        "daemon_log": daemon_log_path(),
        "autostart": autostart_path(),
    }
