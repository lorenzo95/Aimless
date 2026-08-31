import argparse
import json
import os
import sys

import pytest

tkinter = pytest.importorskip("tkinter")

from aimless import cli, crypto, protocol


def _setup_home(tmp_path, monkeypatch, passphrase="testpass"):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AIMLESS_HOME", str(home))
    monkeypatch.setenv("AIMLESS_SOCK", str(home / "nonexistent.sock"))
    identity = crypto.new_identity()
    crypto.save_identity(str(home / "identity.json"), identity, passphrase)
    monkeypatch.setattr(cli, "get_passphrase", lambda confirm=False: passphrase)
    monkeypatch.setattr(cli, "prompt_passphrase", lambda confirm=False: passphrase)
    return home, identity


def test_add_rejects_own_invite(tmp_path, monkeypatch, capsys):
    home, identity = _setup_home(tmp_path, monkeypatch)
    contacts_path = str(home / "client-contacts.json")
    protocol.save_contacts(contacts_path, {"_self": {"screen": "Alice", "pubkey": bytes(identity.verify_key).hex()}})

    invite = protocol.make_invite(identity, "ab" * 32, "Alice")
    with pytest.raises(SystemExit):
        cli.cmd_add(argparse.Namespace(invite=invite, petname=None))
    out = capsys.readouterr()
    assert "own invite" in out.err
    contacts = protocol.load_contacts(contacts_path)
    assert "Alice" not in contacts


def test_add_self_invite_migrates_pubkey_from_identity(tmp_path, monkeypatch, capsys):
    home, identity = _setup_home(tmp_path, monkeypatch)
    contacts_path = str(home / "client-contacts.json")
    protocol.save_contacts(contacts_path, {"_self": {"screen": "Alice"}})

    invite = protocol.make_invite(identity, "ab" * 32, "Alice")
    with pytest.raises(SystemExit):
        cli.cmd_add(argparse.Namespace(invite=invite, petname=None))
    contacts = protocol.load_contacts(contacts_path)
    assert contacts["_self"]["pubkey"] == bytes(identity.verify_key).hex()


def test_add_accepts_real_buddy(tmp_path, monkeypatch):
    home, identity = _setup_home(tmp_path, monkeypatch)
    contacts_path = str(home / "client-contacts.json")
    protocol.save_contacts(contacts_path, {"_self": {"screen": "Alice", "pubkey": bytes(identity.verify_key).hex()}})

    buddy = crypto.new_identity()
    invite = protocol.make_invite(buddy, "cd" * 32, "Bob")
    cli.cmd_add(argparse.Namespace(invite=invite, petname=None))
    contacts = protocol.load_contacts(contacts_path)
    assert contacts["Bob"]["pubkey"] == bytes(buddy.verify_key).hex()
    assert contacts["Bob"]["node"] == "cd" * 32
