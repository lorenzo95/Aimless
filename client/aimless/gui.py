import os
import sys
import time
import tkinter as tk
from tkinter import scrolledtext, simpledialog, messagebox

from . import crypto, protocol
from .daemon import DaemonClient, Client, DaemonError

BG = "#ECE9D8"
PANEL = "#F5F4EA"
ENTRY_BG = "#FFFFFF"
SELF_COL = "#CC0000"
BUDDY_COL = "#0000CC"
AWAY_COL = "#B8860B"
OFFLINE_COL = "#A9A9A9"
SYS_COL = "#555555"
TS_COL = "#8C8C8C"
FONT = ("Helvetica", 10)
FONT_BOLD = ("Helvetica", 10, "bold")
FONT_TITLE = ("Helvetica", 12, "bold")

POLL_MS = 3000
EVENT_MS = 150


def client_paths():
    base = os.environ.get("AIMLESS_HOME") or os.path.expanduser("~/.local/share/aimless")
    os.makedirs(base, exist_ok=True)
    return {
        "identity": os.path.join(base, "identity.json"),
        "contacts": os.path.join(base, "client-contacts.json"),
        "cache": os.path.join(base, "cache.json.enc"),
        "sock": os.environ.get("AIMLESS_SOCK") or os.path.join(base, "api.sock"),
    }


class IMWindow:
    def __init__(self, app, petname, contact):
        self.app = app
        self.petname = petname
        self.contact = contact
        self.win = tk.Toplevel(app.root)
        self.win.title(f"{contact['screen']} - Instant Message")
        self.win.configure(bg=BG)
        self.win.geometry("480x380")
        self.win.minsize(360, 300)

        bottom = tk.Frame(self.win, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=6, pady=(2, 8))
        self.entry = tk.Entry(bottom, bg=ENTRY_BG, font=FONT, relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", self._send)
        send_btn = tk.Button(bottom, text="Send", command=self._send,
                             bg=BG, font=FONT_BOLD, relief="groove", padx=10)
        send_btn.pack(side="right", padx=(6, 0))

        header = tk.Label(self.win, text=f"{contact['screen']} ({petname})", bg=BG, fg=BUDDY_COL, font=FONT_BOLD)
        header.pack(side="top", fill="x", padx=6, pady=(6, 2))

        self.log = scrolledtext.ScrolledText(
            self.win, bg=ENTRY_BG, fg="#222222", font=FONT, relief="flat",
            state="disabled", wrap="word", padx=6, pady=4, height=8)
        self.log.pack(side="top", fill="both", expand=True, padx=6, pady=2)

        self.entry.focus_set()

        for m in sorted(app.cache.msgs(contact["node"]), key=lambda m: (m["ts"], m["seq"])):
            who = app.self_screen if m["dir"] == "out" else contact["screen"]
            self._append(who, m["text"], m["ts"], m["dir"] == "out")

    def _append(self, who, text, ts, self_msg):
        stamp = time.strftime("%H:%M", time.localtime(ts / 1000))
        color = SELF_COL if self_msg else BUDDY_COL
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] ", ("ts",))
        self.log.insert("end", f"{who}: ", ("who",))
        self.log.insert("end", text + "\n", ("msg",))
        self.log.tag_config("ts", foreground=TS_COL)
        self.log.tag_config("who", foreground=color, font=FONT_BOLD)
        self.log.tag_config("msg", foreground=color)
        self.log.see("end")
        self.log.configure(state="disabled")

    def incoming(self, text, ts):
        self._append(self.contact["screen"], text, ts, False)
        self.win.deiconify()
        self.win.lift()

    def _send(self, event=None):
        text = self.entry.get().strip()
        if not text:
            return
        ts = int(time.time() * 1000)
        try:
            resp = self.app.client.send(self.contact["pubkey"], self.contact["node"], text, ts)
        except DaemonError as e:
            messagebox.showerror("aimless", f"send failed: {e}", parent=self.win)
            return
        self.app.cache.add_sent(self.contact["node"], resp.get("seq", 0), ts, text)
        self._append(self.app.self_screen, text, ts, True)
        self.entry.delete(0, "end")


class BuddyListWindow:
    def __init__(self, root, app):
        self.app = app
        self.root = root
        self.win = root
        root.title("AIMless  —  " + app.self_screen)
        root.configure(bg=BG)
        root.geometry("260x420")
        root.minsize(220, 300)

        self.status = tk.Label(root, text="online", bg=BG, fg=SYS_COL, font=FONT, anchor="w")
        self.status.pack(side="bottom", fill="x", padx=8, pady=(0, 6))

        buttons = tk.Frame(root, bg=BG)
        buttons.pack(side="bottom", fill="x", padx=6, pady=(0, 4))
        tk.Button(buttons, text="IM", command=self._open_selected, bg=BG, font=FONT, relief="groove", width=6).pack(side="left")
        tk.Button(buttons, text="Away…", command=self._away, bg=BG, font=FONT, relief="groove", width=6).pack(side="left", padx=4)
        tk.Button(buttons, text="Available", command=self._available, bg=BG, font=FONT, relief="groove", width=8).pack(side="left")

        tk.Label(root, text="◈ AIMless", bg=BG, fg=BUDDY_COL, font=FONT_TITLE).pack(side="top", fill="x", padx=8, pady=(8, 0))
        self.self_label = tk.Label(root, text=f"signed on: {app.self_screen}", bg=BG, fg=SYS_COL, font=FONT)
        self.self_label.pack(side="top", fill="x", padx=8, pady=(0, 4))

        tk.Frame(root, bg="#B0AFA5", height=1).pack(side="top", fill="x")

        self.listbox = tk.Listbox(root, bg=ENTRY_BG, font=FONT, relief="flat",
                                  selectbackground="#C8D8F0", activestyle="none", height=10)
        self.listbox.pack(side="top", fill="both", expand=True, padx=4, pady=4)
        self.listbox.bind("<Double-Button-1>", self._open_selected)

        self.away_msg = None
        self._refresh()
        root.after(POLL_MS, self._poll_presence)

    def _refresh(self):
        contacts = protocol.load_contacts(self.app.contacts_path)
        self.app.contacts = {k: v for k, v in contacts.items() if k != "_self"}
        self.listbox.delete(0, "end")
        self._rows = {}
        presence = {p["key"]: p for p in self.app.safe_presence()}
        for petname, info in sorted(self.app.contacts.items()):
            p = presence.get(info["node"], {})
            online = p.get("online", False)
            screen, away = info.get("screen", petname), None
            if p.get("status_payload"):
                try:
                    st = self.app.client.decrypt_status(p["status_payload"])
                    screen = st.get("screen") or screen
                    away = st.get("away")
                except (ValueError, KeyError):
                    pass
            if online:
                label = f"{screen}"
                if away:
                    label += f"  (away: {away})"
                color = AWAY_COL if away else BUDDY_COL
            else:
                label, color = f"{screen}  (offline)", OFFLINE_COL
            idx = self.listbox.size()
            self.listbox.insert("end", label)
            self.listbox.itemconfig(idx, {"fg": color})
            self._rows[idx] = (petname, info)

    def _poll_presence(self):
        try:
            self._refresh()
        except Exception:
            pass
        self.root.after(POLL_MS, self._poll_presence)

    def _selected_contact(self):
        sel = self.listbox.curselection()
        if not sel:
            return None
        row = self._rows.get(sel[0])
        if not row:
            return None
        return row

    def _open_selected(self, event=None):
        row = self._selected_contact()
        if row:
            self.app.open_im(*row)

    def _away(self):
        msg = simpledialog.askstring("Away message", "Away message:", parent=self.win)
        self.app.set_away(msg.strip() if msg and msg.strip() else None)
        if msg and msg.strip():
            self.away_msg = msg.strip()
            self.status.config(text=f"away: {self.away_msg}")
        else:
            self.away_msg = None
            self.status.config(text="online")

    def _available(self):
        self.app.set_away(None)
        self.away_msg = None
        self.status.config(text="online")


class App:
    def __init__(self, root):
        self.root = root
        paths = client_paths()
        if not os.path.exists(paths["identity"]):
            messagebox.showerror("aimless", "No identity found — run `aimless init` first.")
            root.destroy()
            return
        passphrase = simpledialog.askstring("aimless", "Passphrase:", show="*", parent=root)
        if not passphrase:
            root.destroy()
            return
        try:
            self.identity = crypto.load_identity(paths["identity"], passphrase)
        except ValueError as e:
            messagebox.showerror("aimless", str(e))
            root.destroy()
            return
        self.cache = crypto.Cache(paths["cache"], passphrase)
        self.contacts_path = paths["contacts"]
        self.daemon = DaemonClient(paths["sock"])
        contacts = protocol.load_contacts(paths["contacts"])
        self.self_screen = contacts.get("_self", {}).get("screen", "anonymous")
        self.client = Client(self.daemon, self.identity, self.self_screen)
        self.contacts = {}
        self.im_windows = {}

        self.buddylist = BuddyListWindow(root, self)
        root.after(EVENT_MS, self._poll_events)
        for petname, info in self.contacts.items():
            try:
                self.client.add_contact(info["node"])
            except DaemonError:
                pass

    def safe_presence(self):
        try:
            return self.client.presence()
        except DaemonError:
            return []

    def set_away(self, away):
        for info in self.contacts.values():
            try:
                self.client.set_status(info["pubkey"], info["node"], away)
            except DaemonError:
                pass

    def open_im(self, petname, contact):
        win = self.im_windows.get(petname)
        if win and win.win.winfo_exists():
            win.win.deiconify()
            win.win.lift()
            win.entry.focus_set()
            return
        self.im_windows[petname] = IMWindow(self, petname, contact)

    def _poll_events(self):
        while True:
            ev = self.daemon.next_event(timeout=0)
            if ev is None:
                break
            if ev.get("op") != "recv":
                continue
            for petname, info in self.contacts.items():
                if info["node"] != ev.get("from"):
                    continue
                try:
                    opened = self.client.decrypt_recv(ev)
                except (ValueError, KeyError):
                    continue
                self.cache.add_recv(info["node"], ev.get("seq", 0), opened["ts"], opened["text"])
                win = self.im_windows.get(petname)
                if win and win.win.winfo_exists():
                    win.incoming(opened["text"], opened["ts"])
                else:
                    self.open_im(petname, info)
                    self.im_windows[petname].incoming(opened["text"], opened["ts"])
                break
        self.buddylist.root.after(EVENT_MS, self._poll_events)


def main():
    root = tk.Tk()
    root.withdraw()
    app = App(root)
    if not getattr(app, "daemon", None):
        return
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
