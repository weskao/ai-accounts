"""Post-upgrade integration repair must be automatic and safe across CLIs."""

from __future__ import annotations

import subprocess
from unittest import mock

import update_kit

from ai_accounts import ai_accounts, autoswitch_setup
from ai_accounts import update_check as uc


def test_successful_update_runs_integration_repair(monkeypatch):
    calls = []
    monkeypatch.setattr(update_kit, "update", lambda **kw: 0)
    monkeypatch.setattr(uc, "_post_update_setup", lambda: calls.append("repair") or 0, raising=False)

    assert uc.run_update("claude-accounts") == 0
    assert calls == ["repair"]


def test_failed_package_update_must_not_install_integrations(monkeypatch):
    monkeypatch.setattr(update_kit, "update", lambda **kw: 1)
    monkeypatch.setattr(
        uc, "_post_update_setup", lambda: (_ for _ in ()).throw(AssertionError("unexpected repair")),
        raising=False,
    )

    assert uc.run_update("ai-accounts") == 1


def test_failed_post_update_repair_is_reported_as_failure(monkeypatch):
    monkeypatch.setattr(update_kit, "update", lambda **kw: 0)
    monkeypatch.setattr(uc, "_post_update_setup", lambda: 1, raising=False)

    assert uc.run_update("ai-accounts") == 1


def test_interactive_update_also_repairs_integrations(monkeypatch):
    repairs = []
    monkeypatch.setattr(
        uc.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0),
    )
    monkeypatch.setattr(uc, "_post_update_setup", lambda: repairs.append("repair") or 0, raising=False)

    uc._run_upgrade("v1.2.3")
    assert repairs == ["repair"]


def test_interactive_failed_install_does_not_repair(monkeypatch):
    repairs = []
    monkeypatch.setattr(
        uc.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1),
    )
    monkeypatch.setattr(uc, "_post_update_setup", lambda: repairs.append("repair") or 0, raising=False)

    uc._run_upgrade("v1.2.3")
    assert repairs == []


def test_post_update_invokes_newly_installed_cli_not_old_python(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/opt/uv/bin/ai-accounts" if name == "ai-accounts" else None)
    with mock.patch.object(
        uc.subprocess, "run",
        return_value=subprocess.CompletedProcess(["ai-accounts"], 0),
    ) as run:
        assert uc._post_update_setup() == 0

    assert run.call_args.args[0] == ["/opt/uv/bin/ai-accounts", "migrate"]


def test_missing_installed_cli_is_actionable_and_fails(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with mock.patch.object(uc.subprocess, "run") as run:
        assert uc._post_update_setup() == 1
    run.assert_not_called()
    assert "ai-accounts migrate" in capsys.readouterr().err


def test_repair_command_failure_reports_manual_retry(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: "/opt/uv/bin/ai-accounts")
    with mock.patch.object(
        uc.subprocess, "run",
        return_value=subprocess.CompletedProcess(["ai-accounts"], 1, stderr="setup failed"),
    ):
        assert uc._post_update_setup() == 1
    assert "ai-accounts migrate" in capsys.readouterr().err


def test_setup_command_detects_partial_install(monkeypatch, capsys):
    monkeypatch.setattr(autoswitch_setup, "install", lambda: None)
    monkeypatch.setattr(autoswitch_setup, "is_installed", lambda: False)
    assert ai_accounts.cmd_autoswitch_setup() == 1
    assert "not" in capsys.readouterr().err.lower()


def test_setup_command_accepts_verified_install(monkeypatch, capsys):
    monkeypatch.setattr(autoswitch_setup, "install", lambda: None)
    monkeypatch.setattr(autoswitch_setup, "is_installed", lambda: True)
    assert ai_accounts.cmd_autoswitch_setup() == 0
