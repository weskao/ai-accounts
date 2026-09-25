"""GitHub update hint: version compare, short-TTL cache, background check, silent failure."""

from __future__ import annotations

import io
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
    assert uc.newer_release("0.12.0", now=1000 + uc.TTL_SECONDS - 1, fetch=fetch) == "v0.13.0"
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


def test_new_release_seen_after_ttl(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    assert uc.newer_release("0.12.0", now=0, fetch=lambda: "v0.12.0") is None
    # Several releases can land in one day: the next one is seen within the hour.
    assert uc.newer_release("0.12.0", now=uc.TTL_SECONDS, fetch=lambda: "v0.13.0") == "v0.13.0"
    assert uc.TTL_SECONDS <= 3600


def _fake_tty(monkeypatch):
    monkeypatch.setattr(uc.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(uc, "_check", None)
    monkeypatch.setattr(uc, "_latest", [])


def test_background_fetch_feeds_hint(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    _fake_tty(monkeypatch)
    real = uc.newer_release
    monkeypatch.setattr(uc, "newer_release", lambda current: real(current, fetch=lambda: "v9.9.0"))
    uc.start_check()
    assert uc._check is not None
    uc.maybe_hint()
    assert "ai-accounts 9.9.0 is available" in capsys.readouterr().err


def test_hint_without_start_is_silent(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    _fake_tty(monkeypatch)
    uc.maybe_hint()
    assert capsys.readouterr().err == ""


def test_hint_printed_on_a_tty(tmp_path, monkeypatch, capsys):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"checked_at": uc.time.time(), "latest": "v9.9.0"}))
    _fake_tty(monkeypatch)
    uc.start_check()
    uc.maybe_hint()
    err = capsys.readouterr().err
    assert "ai-accounts 9.9.0 is available" in err
    assert "git+https://github.com/weskao/ai-accounts.git@v9.9.0 ai-accounts" in err


def test_hint_respects_config_off(tmp_path, monkeypatch, capsys):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"checked_at": uc.time.time(), "latest": "v9.9.0"}))
    (tmp_path / "config.json").write_text('{"update_check": false}')
    _fake_tty(monkeypatch)
    uc.start_check()
    uc.maybe_hint()
    assert capsys.readouterr().err == ""


def test_no_hint_without_a_tty(tmp_path, monkeypatch, capsys):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"checked_at": uc.time.time(), "latest": "v9.9.0"}))
    monkeypatch.setattr(uc, "_check", None)
    uc.start_check()  # pytest's captured stderr is not a TTY
    uc.maybe_hint()
    assert capsys.readouterr().err == ""


# ── skip_version: remembered across refetches, never clobbered ──────────────


def test_skip_version_is_merged_into_the_existing_cache(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"checked_at": 123, "latest": "v9.9.0"}))
    uc.skip_version(cache, "v9.9.0")
    saved = json.loads(cache.read_text())
    assert saved["skipped"] == "v9.9.0"
    assert saved["checked_at"] == 123
    assert saved["latest"] == "v9.9.0"


def test_newer_release_is_silent_for_a_skipped_version(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    uc.skip_version(cache, "v9.9.0")
    assert uc.newer_release("0.12.0", now=0, fetch=lambda: "v9.9.0") is None


def test_newer_release_still_fires_for_a_version_past_the_skipped_one(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    uc.skip_version(cache, "v9.9.0")
    assert uc.newer_release("0.12.0", now=0, fetch=lambda: "v9.9.1") == "v9.9.1"


def test_a_refetch_after_ttl_preserves_the_skipped_version(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    uc.skip_version(cache, "v9.9.0")
    # TTL expiry triggers a refetch that still reports the same, already-skipped tag.
    assert uc.newer_release("0.12.0", now=uc.TTL_SECONDS, fetch=lambda: "v9.9.0") is None
    assert json.loads(cache.read_text())["skipped"] == "v9.9.0"


# ── the interactive prompt: shown only on a real keyboard-and-screen TTY ────


def _fake_full_tty(monkeypatch):
    monkeypatch.setattr(uc.kr, "is_interactive_tty", lambda: True)
    monkeypatch.setattr(uc.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(uc, "_check", None)
    monkeypatch.setattr(uc, "_latest", [])


def test_maybe_hint_asks_interactively_on_a_full_tty(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    _fake_full_tty(monkeypatch)
    real = uc.newer_release
    monkeypatch.setattr(uc, "newer_release", lambda current: real(current, fetch=lambda: "v9.9.0"))
    monkeypatch.setattr(uc, "update_prompt", lambda current, latest: uc.SKIP)
    uc.start_check()
    uc.maybe_hint()
    # the interactive path never prints the flat stderr hint
    assert capsys.readouterr().err == ""


def test_maybe_hint_falls_back_to_the_flat_hint_off_a_full_tty(tmp_path, monkeypatch, capsys):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"checked_at": uc.time.time(), "latest": "v9.9.0"}))
    monkeypatch.setattr(uc.kr, "is_interactive_tty", lambda: False)
    monkeypatch.setattr(uc.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(uc, "_check", None)
    uc.start_check()
    uc.maybe_hint()
    assert "ai-accounts 9.9.0 is available" in capsys.readouterr().err


def test_choosing_skip_version_persists_it(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    _fake_full_tty(monkeypatch)
    real = uc.newer_release
    monkeypatch.setattr(uc, "newer_release", lambda current: real(current, fetch=lambda: "v9.9.0"))
    monkeypatch.setattr(uc, "update_prompt", lambda current, latest: uc.SKIP_VERSION)
    uc.start_check()
    uc.maybe_hint()
    assert json.loads(cache.read_text())["skipped"] == "v9.9.0"


def test_choosing_update_now_runs_the_install_command(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    _fake_full_tty(monkeypatch)
    real = uc.newer_release
    monkeypatch.setattr(uc, "newer_release", lambda current: real(current, fetch=lambda: "v9.9.0"))
    monkeypatch.setattr(uc, "update_prompt", lambda current, latest: uc.UPDATE_NOW)
    calls = []
    monkeypatch.setattr(uc.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())
    uc.start_check()
    uc.maybe_hint()
    assert calls and calls[0][:3] == ["uv", "tool", "install"]
    assert f"git+https://github.com/{uc.REPO}.git@v9.9.0" in calls[0]


# ── the rendered prompt: aicp's text layout, this project's own cursor ──────


def test_update_lines_combine_the_header_into_one_line(monkeypatch):
    lines = [uc._present.strip_ansi(ln) for ln in uc._update_lines("en", "0.12.0", "v9.9.0", 1)]
    header = [ln for ln in lines if "9.9.0" in ln and "0.12.0" in ln]
    assert len(header) == 1


def test_update_lines_has_no_dash_separator_rows(monkeypatch):
    lines = [uc._present.strip_ansi(ln) for ln in uc._update_lines("en", "0.12.0", "v9.9.0", 1)]
    assert not any(set(ln.strip()) == {"─"} for ln in lines if ln.strip())


def test_update_lines_columns_are_aligned(monkeypatch):
    lines = [uc._present.strip_ansi(ln) for ln in uc._update_lines("en", "0.12.0", "v9.9.0", 1)]
    targets = [
        ("Update now", "uv tool install"),
        ("Skip until next version", "ask again once a newer version ships"),
        ("Skip", "ask again next run"),
    ]
    offsets = []
    for label, detail in targets:
        row = next(ln for ln in lines if label in ln and detail in ln)
        offsets.append(row.index(detail))
    assert len(set(offsets)) == 1


def test_update_lines_option_three_states_no_specific_version(monkeypatch):
    lines = [uc._present.strip_ansi(ln) for ln in uc._update_lines("en", "0.12.0", "v9.9.0", 1)]
    row = next(ln for ln in lines if "Skip until next version" in ln)
    assert "9.9.0" not in row


def test_update_lines_shows_the_projects_own_cursor_glyph(monkeypatch):
    lines = [uc._present.strip_ansi(ln) for ln in uc._update_lines("en", "0.12.0", "v9.9.0", 2)]
    assert any(f"{uc._CURSOR_MARK}Skip" in ln for ln in lines)


def test_update_lines_match_config_menus_own_style_no_numbers(monkeypatch):
    """ai-accounts' own interactive settings menu (``_field_row`` in
    config_menu.py) marks the selected row with a bare cursor glyph and
    never numbers it — this prompt uses the same raw-mode arrow-key
    interaction, so it should match that, not aicp's ``N)`` convention."""
    lines = [uc._present.strip_ansi(ln) for ln in uc._update_lines("en", "0.12.0", "v9.9.0", 1)]
    assert any(f"{uc._CURSOR_MARK}Update now" in ln for ln in lines)
    assert not any(f"{i})" in ln for ln in lines for i in (1, 2, 3))


def test_update_prompt_enter_on_first_row_is_update_now():
    answer = uc.update_prompt(
        "0.12.0", "v9.9.0", read=iter([uc.kr.KeyEvent(uc.kr.Key.ENTER)]).__next__, out=io.StringIO()
    )
    assert answer == uc.UPDATE_NOW


def test_update_prompt_down_then_enter_is_skip():
    events = iter([uc.kr.KeyEvent(uc.kr.Key.DOWN), uc.kr.KeyEvent(uc.kr.Key.ENTER)])
    answer = uc.update_prompt("0.12.0", "v9.9.0", read=events.__next__, out=io.StringIO())
    assert answer == uc.SKIP


def test_update_prompt_up_from_top_wraps_to_skip_version():
    events = iter([uc.kr.KeyEvent(uc.kr.Key.UP), uc.kr.KeyEvent(uc.kr.Key.ENTER)])
    answer = uc.update_prompt("0.12.0", "v9.9.0", read=events.__next__, out=io.StringIO())
    assert answer == uc.SKIP_VERSION


def test_update_prompt_q_is_skip():
    answer = uc.update_prompt(
        "0.12.0", "v9.9.0", read=iter([uc.kr.KeyEvent(uc.kr.Key.CHAR, "q")]).__next__, out=io.StringIO()
    )
    assert answer == uc.SKIP


def test_update_prompt_ctrl_c_is_skip():
    answer = uc.update_prompt(
        "0.12.0", "v9.9.0", read=iter([uc.kr.KeyEvent(uc.kr.Key.CTRL_C)]).__next__, out=io.StringIO()
    )
    assert answer == uc.SKIP


def test_update_prompt_exhausted_source_is_skip():
    answer = uc.update_prompt("0.12.0", "v9.9.0", read=iter([]).__next__, out=io.StringIO())
    assert answer == uc.SKIP
