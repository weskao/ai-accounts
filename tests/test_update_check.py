"""GitHub update hint: version compare, daily cache, and silent failure."""

from __future__ import annotations

import json

from ai_accounts import update_check as uc


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ACCOUNTS_CONFIG_JSON", str(tmp_path / "config.json"))
    return tmp_path / "update-check.json"


def test_version_tuple():
    assert uc.version_tuple("v0.12.0") == (0, 12, 0)
    assert uc.version_tuple("0.10.1rc1") == (0, 10, 1)
    assert uc.version_tuple("0.10.0") > uc.version_tuple("v0.9.9")
    assert uc.version_tuple("junk") is None


def test_newer_release_found_and_cached(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    calls = []

    def fetch():
        calls.append(1)
        return "v0.13.0"

    assert uc.newer_release("0.12.0", now=1000, fetch=fetch) == "v0.13.0"
    assert json.loads(cache.read_text())["latest"] == "v0.13.0"
    # Within the TTL: answered from the cache, no second request.
    assert uc.newer_release("0.12.0", now=2000, fetch=fetch) == "v0.13.0"
    assert calls == [1]


def test_same_or_older_release_is_no_hint(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert uc.newer_release("0.12.0", now=0, fetch=lambda: "v0.12.0") is None


def test_offline_is_silent_and_cached(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    calls = []

    def fetch():
        calls.append(1)
        raise OSError("offline")

    assert uc.newer_release("0.12.0", now=0, fetch=fetch) is None
    assert uc.newer_release("0.12.0", now=10, fetch=fetch) is None
    assert calls == [1]  # one timeout per day, not per command


def test_claim_only_once(monkeypatch):
    monkeypatch.delenv(uc._CLAIMED_ENV, raising=False)
    assert uc.claim() is True
    assert uc.claim() is False


def _fake_tty(monkeypatch):
    monkeypatch.setattr(uc.sys.stderr, "isatty", lambda: True, raising=False)


def test_hint_printed_on_a_tty(tmp_path, monkeypatch, capsys):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"checked_at": uc.time.time(), "latest": "v9.9.0"}))
    _fake_tty(monkeypatch)
    uc.maybe_hint()
    err = capsys.readouterr().err
    assert "ai-accounts 9.9.0 is available" in err
    assert "git+https://github.com/weskao/ai-accounts.git@v9.9.0 ai-accounts" in err


def test_hint_respects_config_off(tmp_path, monkeypatch, capsys):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"checked_at": uc.time.time(), "latest": "v9.9.0"}))
    (tmp_path / "config.json").write_text('{"update_check": false}')
    _fake_tty(monkeypatch)
    uc.maybe_hint()
    assert capsys.readouterr().err == ""


def test_no_hint_without_a_tty(tmp_path, monkeypatch, capsys):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"checked_at": uc.time.time(), "latest": "v9.9.0"}))
    uc.maybe_hint()  # pytest's captured stderr is not a TTY
    assert capsys.readouterr().err == ""
