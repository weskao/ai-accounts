"""`<cmd> update` / `--update` and the interactive "newer release" prompt after other commands."""

from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path

import pytest
import update_kit

from ai_accounts import update_check as uc


def _console_scripts() -> dict[str, str]:
    """``[project.scripts]`` from pyproject.toml: name → ``module:func``."""
    import re

    text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    return dict(re.findall(r'^([\w-]+)\s*=\s*"([^"]+)"', section, re.MULTILINE))


def _main(target: str):
    module, func = target.split(":")
    return getattr(importlib.import_module(module), func)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ACCOUNTS_CONFIG_JSON", str(tmp_path / "config.json"))
    monkeypatch.delenv(uc._CLAIMED_ENV, raising=False)
    monkeypatch.setattr(uc, "_check", None)
    monkeypatch.setattr(uc, "_latest", [])
    # The test venv is itself an editable (dev) install: without this, `update` would
    # really reinstall the user's ai-accounts tool from GitHub.
    monkeypatch.setattr(update_kit, "install_kind", lambda dist: ("uv", "/tools/ai-accounts"))


# ── `<cmd> update` / `<cmd> --update` ────────────────────────────────────────


@pytest.mark.parametrize("flag", ["update", "--update"])
def test_every_script_routes_update_to_update_kit(monkeypatch, flag):
    scripts = _console_scripts()
    assert "ai-accounts" in scripts and len(scripts) == 9
    for name, target in scripts.items():
        calls: list[dict] = []
        monkeypatch.setattr(update_kit, "update", lambda **kw: calls.append(kw) or 7)
        assert _main(target)([flag]) == 7, name
        assert len(calls) == 1, name
        kw = calls[0]
        assert kw["app"] == name
        assert kw["repo"] == "weskao/ai-accounts"
        assert kw["dist"] == "ai-accounts"
        assert kw["lang"] == "en"
        assert kw["out"] is sys.stderr


def test_update_follows_the_configured_language(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"language": "zh-TW"}), encoding="utf-8")
    from ai_accounts import i18n

    i18n.refresh()
    calls: list[dict] = []
    monkeypatch.setattr(update_kit, "update", lambda **kw: calls.append(kw) or 0)
    try:
        assert _main(_console_scripts()["codex-accounts"])(["update"]) == 0
    finally:
        i18n.refresh()
    assert calls[0]["lang"] == "zh-TW"


def test_update_starts_no_background_check(monkeypatch):
    monkeypatch.setattr(update_kit, "update", lambda **kw: 0)
    monkeypatch.setattr(uc, "start_check", lambda: pytest.fail("update must not start the hint check"))
    assert _main(_console_scripts()["ai-accounts"])(["update"]) == 0


def test_dev_install_goes_through_update_kit_too(monkeypatch):
    """update-kit (>= 0.2) replaces a local checkout with the newest tag; ai-accounts never
    refuses or special-cases it."""
    monkeypatch.setattr(update_kit, "install_kind", lambda dist: ("dev", "/work/ai-accounts"))
    calls: list[dict] = []
    monkeypatch.setattr(update_kit, "update", lambda **kw: calls.append(kw) or 0)
    assert _main(_console_scripts()["codex-accounts"])(["update"]) == 0
    assert [kw["app"] for kw in calls] == ["codex-accounts"]


def test_update_is_not_forwarded_to_providers(monkeypatch):
    from ai_accounts import ai_accounts

    monkeypatch.setattr(update_kit, "update", lambda **kw: 0)
    monkeypatch.setattr(ai_accounts, "cmd_forward", lambda argv: pytest.fail("forwarded"))
    assert ai_accounts.main(["--update"]) == 0


# ── help ─────────────────────────────────────────────────────────────────────


def test_every_help_lists_update_in_en_and_zh_tw():
    from ai_accounts import i18n

    for name, target in _console_scripts().items():
        module = importlib.import_module(target.split(":")[0])
        assert f"{name} update | --update" in module.HELP, name
    for msgid, table in i18n.MESSAGES.items():
        if msgid.startswith("help."):
            assert " update | --update" in table["zh-TW"], msgid


# ── the interactive update prompt and its cache ─────────────────────────────


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ACCOUNTS_CONFIG_JSON", str(tmp_path / "config.json"))
    return tmp_path / "update-check.json"


def test_version_tuple():
    assert uc.version_tuple("v0.12.0") == (0, 12, 0)
    assert uc.version_tuple("0.10.1rc1") == (0, 10, 1)
    assert uc.version_tuple("0.10.0") > uc.version_tuple("v0.9.9")
    assert uc.version_tuple("junk") is None


def _latest_is(monkeypatch, tag):
    """Make update_kit.check report *tag* (None = up to date / offline)."""
    monkeypatch.setattr(update_kit, "check", lambda **kw: tag)


def test_newer_release_found(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    seen: list[dict] = []
    monkeypatch.setattr(update_kit, "check", lambda **kw: seen.append(kw) or "v0.13.0")
    assert uc.newer_release("0.12.0") == "v0.13.0"
    assert seen == [{"repo": "weskao/ai-accounts", "current": "0.12.0"}]


def test_no_newer_release_is_no_hint(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    _latest_is(monkeypatch, None)  # update-kit only returns a tag that is newer
    assert uc.newer_release("0.12.0") is None


def test_junk_tag_is_silent(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    _latest_is(monkeypatch, "junk")
    assert uc.newer_release("0.12.0") is None


def _fake_tty(monkeypatch):
    monkeypatch.setattr(uc.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(uc, "_check", None)
    monkeypatch.setattr(uc, "_latest", [])


def test_background_fetch_feeds_hint(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    _fake_tty(monkeypatch)
    _latest_is(monkeypatch, "v9.9.0")
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
    _cfg(tmp_path, monkeypatch)
    _latest_is(monkeypatch, "v9.9.0")
    _fake_tty(monkeypatch)
    uc.start_check()
    uc.maybe_hint()
    err = capsys.readouterr().err
    assert "ai-accounts 9.9.0 is available" in err
    assert "git+https://github.com/weskao/ai-accounts.git@v9.9.0 ai-accounts" in err


def test_hint_respects_config_off(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    _latest_is(monkeypatch, "v9.9.0")
    (tmp_path / "config.json").write_text('{"update_check": false}')
    _fake_tty(monkeypatch)
    uc.start_check()
    uc.maybe_hint()
    assert capsys.readouterr().err == ""


def test_no_hint_without_a_tty(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    _latest_is(monkeypatch, "v9.9.0")
    monkeypatch.setattr(uc, "_check", None)
    uc.start_check()  # pytest's captured stderr is not a TTY
    uc.maybe_hint()
    assert capsys.readouterr().err == ""


# ── skip_version: remembered, never clobbering other cache keys ─────────────


def test_skip_version_is_merged_into_the_existing_cache(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    cache.write_text(json.dumps({"other": 123}))
    uc.skip_version(cache, "v9.9.0")
    assert json.loads(cache.read_text()) == {"other": 123, "skipped": "v9.9.0"}


def test_newer_release_is_silent_for_a_skipped_version(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    uc.skip_version(cache, "v9.9.0")
    _latest_is(monkeypatch, "v9.9.0")
    assert uc.newer_release("0.12.0") is None
    _latest_is(monkeypatch, "v9.8.0")  # older than the skipped one: still silent
    assert uc.newer_release("0.12.0") is None


def test_newer_release_still_fires_for_a_version_past_the_skipped_one(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    uc.skip_version(cache, "v9.9.0")
    _latest_is(monkeypatch, "v9.9.1")
    assert uc.newer_release("0.12.0") == "v9.9.1"


# ── the interactive prompt: shown only on a real keyboard-and-screen TTY ────


def _fake_full_tty(monkeypatch):
    monkeypatch.setattr(uc.kr, "is_interactive_tty", lambda: True)
    monkeypatch.setattr(uc.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(uc, "_check", None)
    monkeypatch.setattr(uc, "_latest", [])


def test_maybe_hint_asks_interactively_on_a_full_tty(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    _fake_full_tty(monkeypatch)
    _latest_is(monkeypatch, "v9.9.0")
    monkeypatch.setattr(uc, "update_prompt", lambda current, latest: uc.SKIP)
    uc.start_check()
    uc.maybe_hint()
    # the interactive path never prints the flat stderr hint
    assert capsys.readouterr().err == ""


def test_maybe_hint_falls_back_to_the_flat_hint_off_a_full_tty(tmp_path, monkeypatch, capsys):
    _cfg(tmp_path, monkeypatch)
    _latest_is(monkeypatch, "v9.9.0")
    monkeypatch.setattr(uc.kr, "is_interactive_tty", lambda: False)
    monkeypatch.setattr(uc.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(uc, "_check", None)
    uc.start_check()
    uc.maybe_hint()
    assert "ai-accounts 9.9.0 is available" in capsys.readouterr().err


def test_choosing_skip_version_persists_it(tmp_path, monkeypatch):
    cache = _cfg(tmp_path, monkeypatch)
    _fake_full_tty(monkeypatch)
    _latest_is(monkeypatch, "v9.9.0")
    monkeypatch.setattr(uc, "update_prompt", lambda current, latest: uc.SKIP_VERSION)
    uc.start_check()
    uc.maybe_hint()
    assert json.loads(cache.read_text())["skipped"] == "v9.9.0"


def test_choosing_update_now_runs_the_install_command(tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    _fake_full_tty(monkeypatch)
    _latest_is(monkeypatch, "v9.9.0")
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


def test_claim_only_once():
    assert uc.claim() is True
    assert uc.claim() is False


def test_every_script_starts_and_reports_the_check(monkeypatch):
    """--help, help and no args get the update prompt too. A new console
    script whose ``main`` skips ``quiet_keyboard_interrupt`` fails here."""
    for name, target in _console_scripts().items():
        for argv in (["--help"], ["help"], []):
            seen: list[str] = []
            monkeypatch.delenv(uc._CLAIMED_ENV, raising=False)
            monkeypatch.setattr(uc, "start_check", lambda seen=seen: seen.append("start"))
            monkeypatch.setattr(uc, "maybe_hint", lambda seen=seen: seen.append("hint"))
            _main(target)(argv)
            assert seen == ["start", "hint"], (name, argv)


@pytest.mark.parametrize("argv", [["update", "--help"], ["--update", "extra"]])
@pytest.mark.parametrize("lang", ["en", "zh-TW"])
def test_update_with_extra_args_is_refused_with_exit_2(tmp_path, monkeypatch, capsys, argv, lang):
    from ai_accounts import i18n

    (tmp_path / "config.json").write_text(json.dumps({"language": lang}), encoding="utf-8")
    i18n.refresh()
    monkeypatch.setattr(update_kit, "update", lambda **kw: pytest.fail("must not install"))
    try:
        assert _main(_console_scripts()["codex-accounts"])(argv) == 2
    finally:
        i18n.refresh()
    err = capsys.readouterr().err
    assert "Unknown command" not in err
    assert ("takes no arguments" if lang == "en" else "不接受任何參數") in err
    assert "codex-accounts update" in err


def test_ctrl_c_during_update_exits_130_without_traceback(monkeypatch):
    def interrupted(**kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(update_kit, "update", interrupted)
    assert _main(_console_scripts()["codex-accounts"])(["update"]) == 130


def test_console_script_reads_argv_from_sys_argv(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(update_kit, "update", lambda **kw: calls.append(kw) or 0)
    monkeypatch.setattr(sys, "argv", ["codex-accounts", "--update"])
    assert _main(_console_scripts()["codex-accounts"])() == 0
    assert calls[0]["app"] == "codex-accounts"


# ── hint timing / Ctrl-C / language source ───────────────────────────────────

def test_ctrl_c_during_the_hint_wait_is_swallowed(monkeypatch):
    class Waiting:
        def join(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(uc, "_check", Waiting())
    uc.maybe_hint()  # must not raise


def test_ctrl_c_during_the_hint_wait_keeps_the_exit_status(monkeypatch):
    class Waiting:
        def join(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(uc, "start_check", lambda: setattr(uc, "_check", Waiting()))
    assert _main(_console_scripts()["codex-accounts"])(["--help"]) == 0


def test_language_never_reads_the_keychain(tmp_path, monkeypatch, capsys):
    from ai_accounts import i18n, secrets_store

    i18n.refresh()  # current_language() is cached; a warm cache would hide the read
    monkeypatch.setattr(secrets_store, "get", lambda *a, **k: pytest.fail("keychain read"))
    monkeypatch.setattr(update_kit, "update", lambda **kw: 0)
    try:
        _main(_console_scripts()["codex-accounts"])(["update"])
        i18n.refresh()
        uc.update_prompt("0.12.0", "v9.9.0", read=iter([]).__next__, out=io.StringIO())
        i18n.refresh()
        _latest_is(monkeypatch, "v9.9.0")
        _fake_tty(monkeypatch)
        uc.start_check()
        uc.maybe_hint()  # the flat (non-interactive) hint
    finally:
        i18n.refresh()
