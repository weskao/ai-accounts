"""One-time setup is shared by the command and interactive config prompt."""

from __future__ import annotations

import unittest
import subprocess
from unittest import mock

import pytest

from ai_accounts import autoswitch_setup as setup
from ai_accounts import autoswitch, autoswitch_hooks as hooks, autoswitch_timer as timer
from ai_accounts import _utils as u


@pytest.mark.parametrize("platform", ["macos", "linux", "windows"])
def test_installed_status_is_read_only(platform, tmp_path, monkeypatch):
    monkeypatch.setattr(u, "IS_MACOS", platform == "macos")
    monkeypatch.setattr(u, "IS_LINUX", platform == "linux")
    monkeypatch.setattr(u, "IS_WINDOWS", platform == "windows")
    monkeypatch.setattr(u, "go_keyring_available", lambda: (True, ""))
    for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / variable))
    monkeypatch.setenv("AI_ACCOUNTS_CONFIG_JSON", str(tmp_path / "config.json"))
    launchd = tmp_path / "timer.plist"
    systemd = tmp_path / "timer.timer"
    monkeypatch.setattr(timer, "_launchd_plist_path", lambda: launchd)
    monkeypatch.setattr(timer, "_systemd_timer_path", lambda: systemd)
    if platform == "macos":
        launchd.touch()
    if platform == "linux":
        systemd.touch()
    autoswitch.save_config({"enabled": True})
    hooks.install()
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def query(args, **kwargs):
        assert args == ["schtasks", "/Query", "/TN", timer.LABEL]
        return subprocess.CompletedProcess(args, 0)

    with mock.patch.object(u, "run", side_effect=query), mock.patch.object(
        autoswitch, "_write_private", side_effect=AssertionError("status must not write")
    ):
        assert autoswitch.load_config()["enabled"] is True
        assert setup.is_installed() is True
        assert timer.status() == "installed"
        # A timer alone must not count as a complete installation.
        hooks._paths()["codex"].unlink()
        assert setup.is_installed() is False
    removed = hooks._paths()["codex"]
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == {
        p: value for p, value in before.items() if p != removed
    }


class AutoswitchSetupTest(unittest.TestCase):
    def test_install_sets_up_both_the_event_hook_and_fallback_timer(self) -> None:
        with mock.patch("ai_accounts.autoswitch_timer.install") as install_timer, mock.patch(
            "ai_accounts.autoswitch_hooks.install"
        ) as install_hooks:
            setup.install()

        install_timer.assert_called_once_with()
        install_hooks.assert_called_once_with()

    def test_status_requires_both_parts(self) -> None:
        with mock.patch("ai_accounts.autoswitch_timer.status", return_value="installed"), mock.patch(
            "ai_accounts.autoswitch_hooks.is_installed", return_value=True
        ):
            self.assertTrue(setup.is_installed())
        with mock.patch("ai_accounts.autoswitch_timer.status", return_value="not installed"), mock.patch(
            "ai_accounts.autoswitch_hooks.is_installed", return_value=True
        ):
            self.assertFalse(setup.is_installed())
