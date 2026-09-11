from aimless import cli


def test_cli_unblock_clears_mute_without_daemon(tmp_path, monkeypatch):
    identity_file = str(tmp_path / "identity.json")
    cache_file = str(tmp_path / "cache.json.enc")
    node = "ab" * 32

    cache = cli.Store(cache_file, "pw")
    cache.mute(node)
    cache.set_blocked_screen(node, "Spammy")

    monkeypatch.setattr(cli, "identity_path", lambda: identity_file)
    monkeypatch.setattr(cli, "cache_path", lambda: cache_file)
    monkeypatch.setattr(cli, "socket_path", lambda: str(tmp_path / "missing.sock"))
    monkeypatch.setattr(cli, "get_passphrase", lambda *a, **k: "pw")
    cli.crypto.save_identity(identity_file, cli.crypto.new_identity(), "pw")

    cli.cmd_unblock(type("A", (), {"node": node})())

    cache2 = cli.Store(cache_file, "pw")
    assert not cache2.is_muted(node), "client mute must be cleared even without the daemon"
    assert cache2.blocked_screen(node) is None, "stored blocked name must be cleared"
    cache2.close()