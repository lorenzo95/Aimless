import getpass
import os
import sys
import time

from . import crypto, protocol
from .daemon import DaemonClient, Client, DaemonError


def client_dir() -> str:
    base = os.environ.get("AIMLESS_HOME") or os.path.expanduser("~/.local/share/aimless")
    os.makedirs(base, exist_ok=True)
    return base


def socket_path() -> str:
    return os.environ.get("AIMLESS_SOCK") or os.path.join(client_dir(), "api.sock")


def identity_path() -> str:
    return os.path.join(client_dir(), "identity.json")


def contacts_path() -> str:
    return os.path.join(client_dir(), "client-contacts.json")


def cache_path() -> str:
    return os.path.join(client_dir(), "cache.json.enc")


def prompt_passphrase(confirm: bool) -> str:
    pw = getpass.getpass("passphrase: ")
    if not pw:
        print("passphrase must not be empty", file=sys.stderr)
        sys.exit(1)
    if confirm:
        pw2 = getpass.getpass("confirm passphrase: ")
        if pw != pw2:
            print("passphrases do not match", file=sys.stderr)
            sys.exit(1)
    return pw


_PASSPHRASE = None


def get_passphrase(confirm: bool = False) -> str:
    global _PASSPHRASE
    if _PASSPHRASE is None:
        _PASSPHRASE = prompt_passphrase(confirm)
    return _PASSPHRASE


def load_or_exit():
    path = identity_path()
    if not os.path.exists(path):
        print("no identity found — run: aimless init", file=sys.stderr)
        sys.exit(1)
    return crypto.load_identity(path, get_passphrase())


def get_client(daemon: DaemonClient) -> Client:
    identity = load_or_exit()
    contacts = protocol.load_contacts(contacts_path())
    screen = contacts.get("_self", {}).get("screen", "anonymous")
    return Client(daemon, identity, screen)


def cmd_init(args):
    path = identity_path()
    if os.path.exists(path):
        print("identity already exists at", path, file=sys.stderr)
        sys.exit(1)
    pw = prompt_passphrase(confirm=True)
    identity = crypto.new_identity()
    crypto.save_identity(path, identity, pw)
    screen = input("screen name: ").strip() or "anonymous"
    contacts = protocol.load_contacts(contacts_path())
    contacts["_self"] = {"screen": screen, "pubkey": bytes(identity.verify_key).hex()}
    protocol.save_contacts(contacts_path(), contacts)
    print("identity created")
    print("  pubkey:", bytes(identity.verify_key).hex())
    try:
        daemon = DaemonClient(socket_path())
        node_hex = daemon.request("whoami")["key"]
        daemon.close()
        print("  invite:", protocol.make_invite(identity, node_hex, screen))
    except (OSError, DaemonError, KeyError, IndexError):
        print("  (start aimlessd, then run `aimless invite` to get your full invite string)")


def cmd_invite(args):
    path = identity_path()
    if not os.path.exists(path):
        print("no identity found — run: aimless init", file=sys.stderr)
        sys.exit(1)
    pw = get_passphrase()
    identity = crypto.load_identity(path, pw)
    contacts = protocol.load_contacts(contacts_path())
    screen = contacts.get("_self", {}).get("screen", "anonymous")
    try:
        daemon = connect_daemon()
        node_hex = daemon.request("whoami")["key"]
    except (DaemonError, KeyError, IndexError):
        print("warning: daemon unreachable — invite will lack routing key", file=sys.stderr)
        print("hint: start aimlessd, then re-run aimless invite", file=sys.stderr)
        sys.exit(1)
    print(protocol.make_invite(identity, node_hex, screen))


def cmd_add(args):
    try:
        client_hex, node_hex, screen = protocol.parse_invite(args.invite)
    except ValueError as e:
        print("error:", e, file=sys.stderr)
        sys.exit(1)
    contacts = protocol.load_contacts(contacts_path())
    self_pk = contacts.get("_self", {}).get("pubkey")
    if not self_pk and os.path.exists(identity_path()):
        self_pk = bytes(crypto.load_identity(identity_path(), get_passphrase()).verify_key).hex()
        contacts.setdefault("_self", {})["pubkey"] = self_pk
        protocol.save_contacts(contacts_path(), contacts)
    if self_pk and client_hex == self_pk:
        print("that's your own invite — send it to a friend, not to yourself", file=sys.stderr)
        sys.exit(1)
    petname = args.petname or screen
    for k, c in contacts.items():
        if k != "_self" and c.get("pubkey") == client_hex:
            if c.get("node") != node_hex:
                c["node"] = node_hex
                protocol.save_contacts(contacts_path(), contacts)
                print(f"updated routing key for {petname}")
            else:
                print("contact already known")
            sys.exit(0)
    contacts[petname] = {"pubkey": client_hex, "node": node_hex, "screen": screen}
    protocol.save_contacts(contacts_path(), contacts)
    print(f"added {screen} as {petname}")


def cmd_remove(args):
    contacts = protocol.load_contacts(contacts_path())
    if args.petname not in contacts:
        print(f"unknown contact: {args.petname}", file=sys.stderr)
        sys.exit(1)
    del contacts[args.petname]
    protocol.save_contacts(contacts_path(), contacts)
    print(f"removed {args.petname}")


def resolve_buddy(name: str) -> dict:
    contacts = protocol.load_contacts(contacts_path())
    if name not in contacts:
        print(f"unknown contact: {name} — add them first", file=sys.stderr)
        sys.exit(1)
    return contacts[name]


def connect_daemon() -> DaemonClient:
    try:
        return DaemonClient(socket_path())
    except (OSError, FileNotFoundError) as e:
        print(f"cannot reach daemon at {socket_path()}: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    contacts = protocol.load_contacts(contacts_path())
    daemon = connect_daemon()
    client = get_client(daemon)
    presence = {p["key"]: p for p in client.presence()}
    print("screen name:", client.screen_name)
    for petname, info in sorted(contacts.items()):
        if petname == "_self":
            continue
        p = presence.get(info["node"], {})
        state = "online" if p.get("online") else "offline"
        screen = info.get("screen", "?")
        away = ""
        if p.get("status_payload"):
            try:
                st = client.decrypt_status(p["status_payload"])
                if st.get("away"):
                    away = f" | away: {st['away']}"
                if st.get("screen"):
                    screen = st["screen"]
            except (ValueError, KeyError):
                pass
        print(f"  {petname:<16} {state:<8} {screen}{away}")


def cmd_send(args):
    buddy = resolve_buddy(args.petname)
    daemon = connect_daemon()
    client = get_client(daemon)
    cache = crypto.Cache(cache_path(), _passphrase_for_cache())
    ts = int(time.time() * 1000)
    resp = client.send(buddy["pubkey"], buddy["node"], args.text, ts)
    cache.add_sent(buddy["node"], resp.get("seq", 0), ts, args.text)
    print(f"queued (seq {resp.get('seq')})")


def _passphrase_for_cache() -> str:
    if not os.path.exists(identity_path()):
        print("no identity found — run: aimless init", file=sys.stderr)
        sys.exit(1)
    return get_passphrase()


def cmd_away(args):
    buddy_map = {k: v for k, v in protocol.load_contacts(contacts_path()).items() if k != "_self"}
    if not buddy_map:
        print("no contacts", file=sys.stderr)
        sys.exit(1)
    daemon = connect_daemon()
    client = get_client(daemon)
    away = " ".join(args.message) if args.message else None
    for petname, info in buddy_map.items():
        try:
            client.set_status(info["pubkey"], info["node"], away)
        except DaemonError as e:
            print(f"warn: {petname}: {e}", file=sys.stderr)
    print("away" if away else "back")


def cmd_chat(args):
    buddy = resolve_buddy(args.petname)
    buddy_client, buddy_node = buddy["pubkey"], buddy["node"]
    buddy_screen = buddy.get("screen", args.petname)
    daemon = connect_daemon()
    client = get_client(daemon)
    pw = _passphrase_for_cache()
    cache = crypto.Cache(cache_path(), pw)

    resp = client.history(buddy_node, cache.recv_last(buddy_node))
    oldest, latest = resp.get("oldest", 0), resp.get("latest", 0)
    if latest and cache.recv_last(buddy_node) and cache.recv_last(buddy_node) + 1 < oldest:
        print(f"[gap: history before seq {oldest} no longer held by daemon]")
    for m in resp.get("msgs", []):
        try:
            opened = protocol.open_message(client.identity, m["payload"])
            cache.add_recv(buddy_node, m["seq"], opened["ts"], opened["text"])
        except (ValueError, KeyError):
            continue

    client.add_contact(buddy_node)
    try:
        client.set_status(buddy_client, buddy_node, None)
    except DaemonError:
        pass

    print(f"── chat with {buddy_screen} ({args.petname}) ──  /away <msg> /back /quit")
    for m in sorted(cache.msgs(buddy_node), key=lambda m: (m["ts"], m["seq"])):
        who = client.screen_name if m["dir"] == "out" else buddy_screen
        stamp = time.strftime("%H:%M", time.localtime(m["ts"] / 1000))
        print(f"[{stamp}] {who}: {m['text']}")

    while True:
        line = input("> ")
        if not line:
            continue
        if line == "/quit":
            break
        if line.startswith("/away"):
            away = line[5:].strip() or None
            client.set_status(buddy_client, buddy_node, away)
            print(f"[you are {'away: ' + away if away else 'back'}]")
            continue
        if line == "/back":
            client.set_status(buddy_client, buddy_node, None)
            print("[you are back]")
            continue
        ts = int(time.time() * 1000)
        resp = client.send(buddy_client, buddy_node, line, ts)
        cache.add_sent(buddy_node, resp.get("seq", 0), ts, line)
        stamp = time.strftime("%H:%M")
        print(f"[{stamp}] {client.screen_name}: {line}")
        while True:
            ev = client.daemon.next_event(timeout=0.2)
            if ev is None:
                break
            _handle_event(client, cache, buddy_node, buddy_screen, ev)


def _handle_event(client, cache, buddy_node, buddy_screen, ev):
    if ev.get("op") != "recv" or ev.get("from") != buddy_node:
        return
    try:
        opened = client.decrypt_recv(ev)
        cache.add_recv(buddy_node, ev.get("seq", 0), opened["ts"], opened["text"])
        stamp = time.strftime("%H:%M", time.localtime(opened["ts"] / 1000))
        print(f"\r[{stamp}] {buddy_screen}: {opened['text']}")
        print("> ", end="", flush=True)
    except (ValueError, KeyError):
        pass


def cmd_gui(args):
    try:
        from . import gtkui
    except ImportError as e:
        print(f"GUI unavailable ({e}) — install the GTK stack: sudo apt install python3-gi", file=sys.stderr)
        sys.exit(1)
    gtkui.main()


def cmd_tray(args):
    try:
        from . import gtkui
    except ImportError as e:
        print(f"GUI unavailable ({e}) — install the GTK stack: sudo apt install python3-gi", file=sys.stderr)
        sys.exit(1)
    gtkui.run_tray()


def cmd_autostart(args):
    try:
        from . import gtkui
    except ImportError as e:
        print(f"GUI unavailable ({e}) — install the GTK stack: sudo apt install python3-gi", file=sys.stderr)
        sys.exit(1)
    print(gtkui.install_autostart())


def cmd_stop(args):
    try:
        from . import gtkui
    except ImportError as e:
        print(f"GUI unavailable ({e}) — install the GTK stack: sudo apt install python3-gi", file=sys.stderr)
        sys.exit(1)
    stopped = gtkui.stop_all()
    print("stopped: " + (", ".join(stopped) if stopped else "nothing was running"))


def main():
    import argparse

    parser = argparse.ArgumentParser(prog="aimless", description="aimless — serverless chat with an AIM heart")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create identity")

    sub.add_parser("invite", help="print your invite string")

    p_add = sub.add_parser("add", help="add a buddy from their invite")
    p_add.add_argument("invite")
    p_add.add_argument("petname", nargs="?", default=None)

    sub.add_parser("list", help="list buddies and presence")

    p_rm = sub.add_parser("remove", help="remove a buddy")
    p_rm.add_argument("petname")

    p_send = sub.add_parser("send", help="send one message")
    p_send.add_argument("petname")
    p_send.add_argument("text")

    p_chat = sub.add_parser("chat", help="interactive chat")
    p_chat.add_argument("petname")

    p_away = sub.add_parser("away", help="set away message (no arg = back)")
    p_away.add_argument("message", nargs="*")

    sub.add_parser("gui", help="launch the AIMless GTK app (manages the daemon, tray on close)")
    sub.add_parser("tray", help="run the tray daemon: supervises aimlessd, lives in the notification area")
    sub.add_parser("autostart", help="install autostart entry for `aimless tray`")
    sub.add_parser("stop", help="stop the tray supervisor and aimlessd")

    args = parser.parse_args()
    cmds = {
        "init": cmd_init,
        "invite": cmd_invite,
        "add": cmd_add,
        "list": cmd_list,
        "remove": cmd_remove,
        "send": cmd_send,
        "chat": cmd_chat,
        "away": cmd_away,
        "gui": cmd_gui,
        "tray": cmd_tray,
        "autostart": cmd_autostart,
        "stop": cmd_stop,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
