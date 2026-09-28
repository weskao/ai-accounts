"""profile_secrets: store-backed profile secrets with a plaintext fallback."""

from __future__ import annotations

import json
import os
import stat

import pytest

from ai_accounts import profile_secrets as ps

CODEX_FIELDS = ("tokens.refresh_token", "tokens.access_token", "tokens.id_token")
CLAUDE_FIELDS = ("claudeAiOauth.refreshToken", "claudeAiOauth.accessToken")


def _codex_profile() -> dict:
    return {
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "id_token": "fake-id-token",
            "account_id": "acct_test_001",
        },
        "last_refresh": "2026-01-01T00:00:00Z",
        "email": "user@example.com",
    }


def _claude_profile() -> dict:
    return {
        "claudeAiOauth": {
            "accessToken": "fake-access-token",
            "refreshToken": "fake-refresh-token",
            "expiresAt": 1700000000000,
            "scopes": ["user:inference"],
        }
    }


def _write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_store_key_sanitises_and_separates_lookalikes():
    assert ps.store_key("codex", "work") == "codex--work"
    dotted, underscored = ps.store_key("codex", "a.b"), ps.store_key("codex", "a_b")
    assert dotted != underscored
    assert all(ch.isalnum() or ch in "_-" for ch in dotted + ps.store_key("codex", "測試"))


def test_inline_nested_secrets_migrate_once_and_idempotently(tmp_path, profile_store, capsys):
    path = tmp_path / "accounts" / "work.json"
    _write(path, _codex_profile())

    assert ps.load(path, "codex", CODEX_FIELDS) == _codex_profile()
    on_disk = json.loads(path.read_text())
    assert "access_token" not in on_disk["tokens"] and "refresh_token" not in on_disk["tokens"]
    assert on_disk["tokens"]["account_id"] == "acct_test_001"
    assert on_disk[ps.SECRETS_KEY]["key"] == "codex--work"
    assert "fake-access-token" not in path.read_text()
    if os.name == "posix":
        assert _mode(path) == 0o600
    assert capsys.readouterr().err.count("Moved secrets") == 1

    migrated = path.read_bytes()
    assert ps.load(path, "codex", CODEX_FIELDS) == _codex_profile()
    assert path.read_bytes() == migrated
    assert "Moved secrets" not in capsys.readouterr().err


def test_unavailable_store_leaves_file_byte_identical_with_one_warning(tmp_path, profile_store, capsys):
    profile_store.unavailable = True
    path = tmp_path / "accounts" / "work.json"
    _write(path, _claude_profile())
    before = path.read_bytes()

    assert ps.load(path, "claude", CLAUDE_FIELDS) == _claude_profile()
    assert ps.load(path, "claude", CLAUDE_FIELDS) == _claude_profile()
    assert ps.save(tmp_path / "accounts" / "other.json", "claude", _claude_profile(), CLAUDE_FIELDS) is False

    assert path.read_bytes() == before
    assert json.loads((tmp_path / "accounts" / "other.json").read_text()) == _claude_profile()
    if os.name == "posix":
        assert _mode(tmp_path / "accounts" / "other.json") == 0o600
    out = capsys.readouterr()
    assert out.err.count("No OS credential store") == 1


def test_failed_readback_keeps_file_untouched(tmp_path, profile_store):
    profile_store.get = lambda key: "corrupted"  # store "accepts" but reads back wrong
    path = tmp_path / "accounts" / "work.json"
    _write(path, _codex_profile())
    before = path.read_bytes()
    assert ps.load(path, "codex", CODEX_FIELDS) == _codex_profile()
    assert path.read_bytes() == before
    assert profile_store.slots == {}  # the bad write was cleaned up


def test_save_load_round_trip_and_metadata(tmp_path, profile_store):
    path = tmp_path / "accounts" / "work.json"
    assert ps.save(path, "claude", _claude_profile(), CLAUDE_FIELDS) is True
    assert ps.load(path, "claude", CLAUDE_FIELDS) == _claude_profile()

    meta = ps.load_metadata(path)
    assert meta["claudeAiOauth"]["expiresAt"] == 1700000000000
    marker = meta[ps.SECRETS_KEY]
    assert marker["store"] == "ai-accounts"
    assert marker["has_refresh_token"] is True
    assert marker["fingerprint"] == ps.fingerprint("fake-refresh-token")
    assert "fake-" not in path.read_text()
    if os.name == "posix":
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700


def test_no_refresh_token_marker_false(tmp_path, profile_store):
    path = tmp_path / "accounts" / "api.json"
    payload = {"claudeAiOauth": {"accessToken": "fake-access-token", "expiresAt": 1}}
    ps.save(path, "claude", payload, CLAUDE_FIELDS)
    marker = ps.load_metadata(path)[ps.SECRETS_KEY]
    assert marker["has_refresh_token"] is False
    assert marker["fingerprint"] == ps.fingerprint("fake-access-token")


def test_load_metadata_never_reads_store(tmp_path, profile_store):
    path = tmp_path / "accounts" / "work.json"
    ps.save(path, "codex", _codex_profile(), CODEX_FIELDS)
    reads = profile_store.gets
    assert ps.load_metadata(path)[ps.SECRETS_KEY]["key"] == "codex--work"
    assert profile_store.gets == reads


def test_missing_store_entry_returns_none(tmp_path, profile_store):
    path = tmp_path / "accounts" / "work.json"
    ps.save(path, "codex", _codex_profile(), CODEX_FIELDS)
    profile_store.slots.clear()
    ps._cache.clear()
    assert ps.load(path, "codex", CODEX_FIELDS) is None


def test_delete_removes_file_and_store_entry(tmp_path, profile_store):
    path = tmp_path / "accounts" / "work.json"
    ps.save(path, "codex", _codex_profile(), CODEX_FIELDS)
    assert profile_store.slots
    ps.delete(path, "codex")
    assert not path.exists()
    assert profile_store.slots == {}


def test_memo_caches_reads_and_invalidates_on_save(tmp_path, profile_store):
    path = tmp_path / "accounts" / "work.json"
    ps.save(path, "codex", _codex_profile(), CODEX_FIELDS)
    ps.load(path, "codex", CODEX_FIELDS)
    reads = profile_store.gets
    ps.load(path, "codex", CODEX_FIELDS)
    assert profile_store.gets == reads  # served from the memo

    rotated = _codex_profile()
    rotated["tokens"]["access_token"] = "fake-rotated-token"
    ps.save(path, "codex", rotated, CODEX_FIELDS)
    assert ps.load(path, "codex", CODEX_FIELDS)["tokens"]["access_token"] == "fake-rotated-token"
    ps.delete(path, "codex")
    assert ps._cache.get("codex--work") is None


def test_chunked_values_round_trip_and_shrink(tmp_path, profile_store, monkeypatch):
    monkeypatch.setattr(ps, "_chunk_size", lambda: 40)
    path = tmp_path / "accounts" / "big.json"
    big = _codex_profile()
    big["tokens"]["id_token"] = "fake-" + "x" * 300
    ps.save(path, "codex", big, CODEX_FIELDS)
    assert len(profile_store.slots) > 2
    assert all(len(v) <= 45 for v in profile_store.slots.values())  # "10:0:" + 40
    ps._cache.clear()
    assert ps.load(path, "codex", CODEX_FIELDS) == big

    ps.save(path, "codex", _codex_profile(), CODEX_FIELDS)  # shorter value drops stale parts
    ps._cache.clear()
    assert ps.load(path, "codex", CODEX_FIELDS) == _codex_profile()
    assert "x" * 40 not in "".join(profile_store.slots.values())


def test_torn_reassembly_is_refused_not_returned(tmp_path, profile_store):
    """A store read racing another process's write, or a dropped/corrupted
    chunk, can still reassemble into syntactically valid JSON with the wrong
    bytes inside — the fingerprint check must catch that rather than hand
    back corrupted tokens."""
    path = tmp_path / "accounts" / "work.json"
    ps.save(path, "codex", _codex_profile(), CODEX_FIELDS)
    ps._cache.clear()

    key = "codex--work"
    profile_store.slots[key] = profile_store.slots[key].replace(
        "fake-refresh-token", "fake-refresh-toke0")  # same length — still valid JSON

    assert ps.load(path, "codex", CODEX_FIELDS) is None


_OLD = {"token": "fake-old-" + "a" * 150}
_NEW = {"token": "fake-new-" + "b" * 150}


class _Crash(Exception):
    pass


def _fail_on(profile_store, monkeypatch, nth_set, *, crash):
    """Make the *nth_set*-th store write from now on crash (raise) or fail (False)."""
    real_set, calls = profile_store.set, []

    def flaky_set(key, value):
        calls.append(key)
        if len(calls) == nth_set:
            if crash:
                raise _Crash(key)
            return False
        return real_set(key, value)

    monkeypatch.setattr(profile_store, "set", flaky_set)
    return calls


@pytest.fixture
def chunked(monkeypatch):
    monkeypatch.setattr(ps, "_chunk_size", lambda: 40)


@pytest.mark.parametrize("nth_set", [1, 3, 5])  # 4 new parts + head: part 1, part 3, the head
def test_crash_mid_rewrite_keeps_old_value(profile_store, monkeypatch, chunked, nth_set):
    assert ps._put("k", _OLD)
    calls = _fail_on(profile_store, monkeypatch, nth_set, crash=True)
    with pytest.raises(_Crash):
        ps._put("k", _NEW)
    assert calls[-1] == ("k" if nth_set == 5 else f"part{nth_set}g1--k")
    ps._cache.clear()  # a fresh process after the crash
    assert ps._read("k") == _OLD


@pytest.mark.parametrize("nth_set", [2, 5])  # a part write, the head flip
def test_failed_multichunk_rewrite_cleans_new_parts(profile_store, monkeypatch, chunked, nth_set):
    assert ps._put("k", _OLD)
    before = dict(profile_store.slots)
    _fail_on(profile_store, monkeypatch, nth_set, crash=False)
    assert ps._put("k", _NEW) is False
    assert profile_store.slots == before
    ps._cache.clear()
    assert ps._read("k") == _OLD


def test_generation_flips_and_drops_old_parts(profile_store, chunked):
    for gen, value in [(0, _OLD), (1, _NEW), (0, _OLD)]:
        assert ps._put("k", value)
        assert profile_store.slots["k"].startswith(f"5:{gen}:")
        assert all(k == "k" or f"g{gen}--" in k for k in profile_store.slots)
        ps._cache.clear()
        assert ps._read("k") == value
    ps._drop("k")
    assert profile_store.slots == {}


@pytest.mark.parametrize("head", ["", "junk", "0:0:{}", "65:0:{}", "2:0:{\"a\":", "x:1:{}"])
def test_malformed_heads_read_as_none(profile_store, head):
    profile_store.slots["k"] = head
    assert ps._read("k") is None


def test_value_over_part_cap_is_refused(profile_store, chunked):
    assert ps._put("k", "x" * 40 * 65) is False
    assert profile_store.slots == {}


def test_legacy_head_loads_and_migrates_to_generations(profile_store, chunked):
    profile_store.slots.update({"k": '2:{"token":', "part1--k": '"fake-legacy"}'})  # ff17eee layout
    assert ps._read("k") == {"token": "fake-legacy"}
    assert ps._put("k", _NEW)
    assert "part1--k" not in profile_store.slots
    ps._cache.clear()
    assert ps._read("k") == _NEW


def test_plaintext_fallback_drops_stale_store_copy(tmp_path, profile_store, monkeypatch, capsys):
    path = tmp_path / "accounts" / "work.json"
    assert ps.save(path, "codex", _codex_profile(), CODEX_FIELDS) is True
    assert ps.backup("demo", "fake-old-backup") is True
    monkeypatch.setattr(profile_store, "set", lambda key, value: False)  # store up, writes fail

    rotated = _codex_profile()
    rotated["tokens"]["refresh_token"] = "fake-rotated-token"
    assert ps.save(path, "codex", rotated, CODEX_FIELDS) is False
    assert ps.backup("demo", "fake-new-backup") is False
    assert profile_store.slots == {}
    assert json.loads(path.read_text()) == rotated
    assert ps.read_backup("demo") == "fake-new-backup"


def test_backup_keeps_one_and_prunes_plaintext(tmp_path, profile_store):
    backups = tmp_path / "ai-accounts-root" / "demo" / "backups"
    backups.mkdir(parents=True)
    for name in ("auth.json.backup", "auth.backup-20260101.json"):
        (backups / name).write_text("{}")

    assert ps.backup("demo", '{"token": "fake-one"}\n') is True
    assert ps.backup("demo", '{"token": "fake-two"}') is True
    assert ps.read_backup("demo") == '{"token": "fake-two"}'
    assert list(profile_store.slots) == ["backup--demo"]
    assert list(backups.iterdir()) == []
    if os.name == "posix":
        assert _mode(backups) == 0o700


def test_backup_falls_back_to_single_latest_file(tmp_path, profile_store, capsys):
    profile_store.unavailable = True
    assert ps.backup("demo", "fake-one") is False
    assert ps.backup("demo", "fake-two") is False
    latest = tmp_path / "ai-accounts-root" / "demo" / "backups" / "latest"
    assert [p.name for p in latest.parent.iterdir()] == ["latest"]
    if os.name == "posix":
        assert _mode(latest) == 0o600
    assert ps.read_backup("demo") == "fake-two"
    assert capsys.readouterr().err.count("No OS credential store") == 1


@pytest.mark.parametrize("unavailable, label_is_none", [(False, False), (True, True)])
def test_status(monkeypatch, profile_store, unavailable, label_is_none):
    profile_store.unavailable = unavailable
    # The real label reads the host's backend — "none" on CI runners without one.
    monkeypatch.setattr(ps.telegram_kit, "backend_label", lambda: "Fake store")
    label, reason = ps.status()
    assert (label == "none") is label_is_none
    assert bool(reason) is unavailable
