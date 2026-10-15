"""`<cmd> update` / `--update` (update-kit) and the one-line TTY-only update hint."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
import update_kit

from ai_accounts import __version__
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
    monkeypatch.setattr(uc, "start_check", lambda cmd: pytest.fail("update must not start the hint check"))
    assert _main(_console_scripts()["ai-accounts"])(["update"]) == 0


def test_dev_install_is_refused_with_exit_2(monkeypatch, capsys):
    monkeypatch.setattr(update_kit, "install_kind", lambda dist: ("dev", "/work/ai-accounts"))
    assert _main(_console_scripts()["ai-accounts"])(["update"]) == 2
    assert "git pull" in capsys.readouterr().err


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


# ── the one-line hint ────────────────────────────────────────────────────────


def _tty(monkeypatch, on=True):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: on, raising=False)


def _run_hint(monkeypatch, latest, cmd="codex-accounts"):
    seen: list[dict] = []
    monkeypatch.setattr(update_kit, "check", lambda **kw: seen.append(kw) or latest)
    uc.start_check(cmd)
    uc.maybe_hint()
    return seen


def test_hint_is_one_line_on_stderr_naming_the_invoked_command(monkeypatch, capsys):
    _tty(monkeypatch)
    seen = _run_hint(monkeypatch, "v9.9.0")
    err = capsys.readouterr().err
    assert err.strip() == update_kit.hint("codex-accounts", "9.9.0", __version__)
    assert "`codex-accounts update`" in err
    assert seen[0]["repo"] == "weskao/ai-accounts"
    assert seen[0]["current"] == __version__


def test_hint_is_localized(tmp_path, monkeypatch, capsys):
    from ai_accounts import i18n

    (tmp_path / "config.json").write_text(json.dumps({"language": "zh-TW"}), encoding="utf-8")
    i18n.refresh()
    _tty(monkeypatch)
    try:
        _run_hint(monkeypatch, "v9.9.0")
    finally:
        i18n.refresh()
    assert capsys.readouterr().err.strip() == update_kit.hint(
        "codex-accounts", "9.9.0", __version__, "zh-TW"
    )


def test_no_hint_when_nothing_newer(monkeypatch, capsys):
    _tty(monkeypatch)
    _run_hint(monkeypatch, None)
    assert capsys.readouterr().err == ""


def test_no_hint_and_no_check_without_a_tty(monkeypatch, capsys):
    _tty(monkeypatch, on=False)
    assert _run_hint(monkeypatch, "v9.9.0") == []  # not even the network check
    assert capsys.readouterr().err == ""


def test_no_hint_when_config_turns_it_off(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.json").write_text('{"update_check": false}', encoding="utf-8")
    _tty(monkeypatch)
    assert _run_hint(monkeypatch, "v9.9.0") == []
    assert capsys.readouterr().err == ""


def test_hint_without_start_is_silent(capsys):
    uc.maybe_hint()
    assert capsys.readouterr().err == ""


def test_claim_only_once():
    assert uc.claim() is True
    assert uc.claim() is False


def test_a_failing_check_never_breaks_the_command(monkeypatch, capsys):
    _tty(monkeypatch)

    def boom(**kw):
        raise RuntimeError("offline")

    monkeypatch.setattr(update_kit, "check", boom)
    uc.start_check("codex-accounts")
    uc.maybe_hint()
    assert capsys.readouterr().err == ""


def test_every_script_starts_and_reports_the_check(monkeypatch):
    """--help, help and no args get the hint too. A new console script whose
    ``main`` skips ``quiet_keyboard_interrupt`` fails here."""
    for name, target in _console_scripts().items():
        for argv in (["--help"], ["help"], []):
            seen: list[object] = []
            monkeypatch.delenv(uc._CLAIMED_ENV, raising=False)
            monkeypatch.setattr(uc, "start_check", lambda cmd, seen=seen: seen.append(cmd))
            monkeypatch.setattr(uc, "maybe_hint", lambda seen=seen: seen.append("hint"))
            _main(target)(argv)
            assert seen == [name, "hint"], (name, argv)


def test_update_with_extra_args_is_not_an_update(monkeypatch):
    monkeypatch.setattr(update_kit, "update", lambda **kw: pytest.fail("must not install"))
    _main(_console_scripts()["codex-accounts"])(["update", "--help"])


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
