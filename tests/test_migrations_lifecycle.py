"""Upgrade lifecycle around the migration engine: multi-version chains,
integration repair, the first-run check, `update`, `migrate` and `doctor`.

Integrations are fakes (recorders) unless a test drives the real hook writer
inside the throwaway HOME; no LaunchAgent, systemd unit, crontab or
scheduled task is ever touched.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import update_kit
from test_migrations import (  # tests/ is on sys.path
    ENV_OVERRIDES,
    files_in,
    make_store,
    state,
)

from ai_accounts import _utils as u
from ai_accounts import ai_accounts, autoswitch_hooks, autoswitch_timer, doctor
from ai_accounts import migrations as mig
from ai_accounts import update_check as uc
from ai_accounts.providers import PROVIDERS


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AI_ACCOUNTS_CONFIG_JSON", str(home / ".ai-accounts" / "config.json"))
    monkeypatch.setattr(u, "go_keyring_available", lambda: (False, "test"))
    monkeypatch.delenv(mig.PATH_RUNTIME_ENV, raising=False)
    # This test runtime plays the installed `ai-accounts` unless a test says otherwise.
    monkeypatch.setattr(mig, "_is_path_runtime", lambda: True)
    return home


class Integrations:
    """Recorder standing in for the OS scheduler and the hook writer."""

    def __init__(self, monkeypatch, *, timer=True, hooks=True):
        self.timer, self.hooks = timer, hooks
        self.calls: list[str] = []
        self.hooks_verify = True
        self.timer_error: Exception | None = None
        monkeypatch.setattr(autoswitch_timer, "status", lambda: "installed" if self.timer else "not installed")
        monkeypatch.setattr(autoswitch_timer, "install", self._install_timer)
        monkeypatch.setattr(autoswitch_timer, "_tcc_blocked_source", lambda: None)
        monkeypatch.setattr(autoswitch_hooks, "any_installed", lambda: self.hooks)
        monkeypatch.setattr(autoswitch_hooks, "install", lambda: self.calls.append("hooks"))
        monkeypatch.setattr(autoswitch_hooks, "is_installed", lambda: self.hooks_verify)

    def _install_timer(self, interval_sec=None):
        self.calls.append(f"timer:{interval_sec}")
        if self.timer_error:
            raise self.timer_error


@pytest.fixture
def integ(monkeypatch):
    monkeypatch.setattr(mig, "_is_path_runtime", lambda: True)  # this runtime is "installed"
    return Integrations(monkeypatch)


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(mig, "_interactive", lambda: True)
    monkeypatch.setattr(mig, "_is_path_runtime", lambda: True)


# ── cross-version upgrades ───────────────────────────────────────────────────

@pytest.mark.parametrize("source", range(0, mig.CURRENT_SCHEMA))
def test_direct_upgrade_from_every_historical_schema(home, integ, source):
    files = make_store(home, ".codex/accounts")
    if source:
        mig.migrations_dir().mkdir(parents=True)
        mig.state_path().write_text(json.dumps({"schema_version": source}), encoding="utf-8")
    assert mig.run() == 0
    assert state()["schema_version"] == mig.CURRENT_SCHEMA
    assert files_in(home / ".ai-accounts/codex/accounts") == files


def _chain(home: Path):
    def mover(n):
        def plan():
            src = home / f"v{n}-src"
            ops = [{"kind": "move", "provider": f"p{n}", "src": str(src), "dst": str(home / f"v{n}-dst")}]
            return (ops if src.exists() else []), []
        return mig.Migration(n, f"step-{n}", plan)

    for n in (1, 2, 3):
        (home / f"v{n}-src").mkdir()
        (home / f"v{n}-src" / "data.json").write_text(f'{{"n": {n}}}', encoding="utf-8")
    return tuple(mover(n) for n in (1, 2, 3))


def test_skipping_releases_runs_every_migration_in_order(home, integ, monkeypatch):
    monkeypatch.setattr(mig, "MIGRATIONS", _chain(home))
    monkeypatch.setattr(mig, "CURRENT_SCHEMA", 3)
    assert mig.run() == 0
    assert [h["version"] for h in state()["history"]] == [1, 2, 3]
    assert all((home / f"v{n}-dst" / "data.json").exists() for n in (1, 2, 3))


def test_crash_mid_chain_resumes_from_the_checkpoint_without_rerunning(home, integ, monkeypatch):
    monkeypatch.setattr(mig, "MIGRATIONS", _chain(home))
    monkeypatch.setattr(mig, "CURRENT_SCHEMA", 3)
    real = mig._move

    class Crash(BaseException):
        pass

    def move(src, dst):
        if "v2-src" in str(src):
            raise Crash
        real(src, dst)

    monkeypatch.setattr(mig, "_move", move)
    with pytest.raises(Crash):
        mig.run()
    assert state()["schema_version"] == 1
    monkeypatch.setattr(mig, "_move", real)
    assert mig.run() == 0
    assert [h["version"] for h in state()["history"]] == [1, 2, 3]


def test_user_configuration_is_byte_identical_after_migration(home, integ):
    config = home / ".ai-accounts" / "config.json"
    config.parent.mkdir(parents=True)
    raw = '{"enabled": false, "token_refresh": false, "timer_interval_min": 15, "notify": "none"}\n'
    config.write_text(raw, encoding="utf-8")
    make_store(home, ".claude/accounts")
    assert mig.run() == 0
    assert config.read_text(encoding="utf-8") == raw
    assert integ.calls == ["hooks", "timer:None"]  # interval comes from that config


# ── integration repair ───────────────────────────────────────────────────────

def test_present_integrations_are_reinstalled_and_recorded(home, integ):
    assert mig.run() == 0
    assert integ.calls == ["hooks", "timer:None"]
    record = state()["integrations"]
    assert record["status"] == "ok" and record["executable"] == sys.executable


def test_absent_integrations_are_never_installed(home, monkeypatch):
    integ = Integrations(monkeypatch, timer=False, hooks=False)
    assert mig.run() == 0
    assert integ.calls == []
    assert state()["integrations"]["status"] == "absent"


def test_a_removed_timer_stays_removed(home, monkeypatch):
    integ = Integrations(monkeypatch, timer=False, hooks=True)
    assert mig.run() == 0
    assert integ.calls == ["hooks"]


def test_failed_hook_installation_fails_with_retry_hint(home, integ, capsys):
    integ.hooks_verify = False
    assert mig.run() == 1
    assert "ai-accounts migrate" in capsys.readouterr().err
    assert state()["integrations"]["status"] == "failed"
    assert mig.status() == 1
    assert mig.summary()["ok"] is False


def test_failed_scheduler_registration_fails_but_hooks_still_repair(home, integ, capsys):
    integ.timer_error = subprocess.CalledProcessError(5, ["launchctl", "load"])
    assert mig.run() == 1
    assert integ.calls == ["hooks", "timer:None"]
    assert "launchctl" in capsys.readouterr().err


def test_protected_editable_source_is_reported_not_claimed_healthy(home, integ, monkeypatch, capsys):
    monkeypatch.setattr(u, "IS_MACOS", True)
    monkeypatch.setattr(autoswitch_timer, "_tcc_blocked_source", lambda: "Documents")
    assert mig.run() == 1
    assert "~/Documents" in capsys.readouterr().err


def test_missing_vendor_cli_is_reported_and_never_installed(home, integ, monkeypatch, capsys):
    monkeypatch.setattr(u, "have", lambda cmd: cmd != "grok")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or pytest.fail("no installs"))
    assert mig.run() == 0
    assert "grok" in capsys.readouterr().err
    assert calls == []


def test_real_hook_repair_keeps_user_hooks_and_updates_stale_interpreter(home, monkeypatch):
    monkeypatch.setattr(autoswitch_timer, "status", lambda: "not installed")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    user_hook = {"type": "command", "command": "echo mine"}
    stale = {"type": "command", "command": "/gone/python -m ai_accounts.autoswitch_hooks run claude"}
    settings.write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [
        {"hooks": [user_hook]}, {"hooks": [stale]},
    ]}}), encoding="utf-8")
    assert mig.run() == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert "echo mine" in commands and data["theme"] == "dark"
    managed = [c for c in commands if "ai_accounts.autoswitch_hooks" in c]
    assert managed == [autoswitch_hooks.command("claude")]


# ── first-run check ──────────────────────────────────────────────────────────

def test_external_install_migrates_and_repairs_once_on_first_command(home, integ, tty):
    files = make_store(home, ".codex/accounts")
    mig.first_run(["list"])
    assert files_in(home / ".ai-accounts/codex/accounts") == files
    assert integ.calls == ["hooks", "timer:None"]
    mig.first_run(["list"])
    mig.first_run(["switch"])
    assert integ.calls == ["hooks", "timer:None"]


def test_current_state_is_a_fast_noop(home, integ, tty, monkeypatch, capsys):
    mig.first_run(["list"])
    capsys.readouterr()
    monkeypatch.setattr(mig, "_pending", lambda *_: pytest.fail("fast path must not plan"))
    monkeypatch.setattr(mig, "_integrations_present", lambda: pytest.fail("fast path must not probe"))
    mig.first_run(["list"])
    assert capsys.readouterr().err == ""


def test_background_runs_migrate_data_but_never_repair(home, integ, tty, monkeypatch):
    monkeypatch.setenv(mig.BACKGROUND_ENV, "1")
    make_store(home, ".vibe/accounts")
    mig.first_run(["autoswitch"])
    assert (home / ".ai-accounts/vibe/accounts").is_dir()
    assert integ.calls == []


def test_non_interactive_runs_never_repair(home, integ, monkeypatch):
    monkeypatch.setattr(mig, "_interactive", lambda: False)
    monkeypatch.setattr(mig, "_is_path_runtime", lambda: True)
    mig.first_run(["list"])
    assert state()["schema_version"] == mig.CURRENT_SCHEMA
    assert integ.calls == []


def test_another_live_runtime_owns_the_integrations(home, integ, tty, tmp_path):
    other = tmp_path / "other-python"
    other.write_text("", encoding="utf-8")
    mig.migrations_dir().mkdir(parents=True)
    mig.state_path().write_text(json.dumps({
        "schema_version": mig.CURRENT_SCHEMA,
        "integrations": {"version": "0.0.1", "executable": str(other), "status": "ok"},
    }), encoding="utf-8")
    mig.first_run(["list"])
    assert integ.calls == []


def test_a_deleted_old_interpreter_is_replaced(home, integ, tty, tmp_path):
    mig.migrations_dir().mkdir(parents=True)
    mig.state_path().write_text(json.dumps({
        "schema_version": mig.CURRENT_SCHEMA,
        "integrations": {"version": "0.0.1", "executable": str(tmp_path / "gone"), "status": "ok"},
    }), encoding="utf-8")
    mig.first_run(["list"])
    assert integ.calls == ["hooks", "timer:None"]


def test_newer_recorded_integrations_are_not_downgraded(home, integ, tty):
    mig.migrations_dir().mkdir(parents=True)
    mig.state_path().write_text(json.dumps({
        "schema_version": mig.CURRENT_SCHEMA,
        "integrations": {"version": "999.0.0", "executable": sys.executable, "status": "ok"},
    }), encoding="utf-8")
    mig.first_run(["list"])
    assert integ.calls == []


def test_unknown_future_schema_leaves_everything_alone_on_first_run(home, integ, tty):
    files = make_store(home, ".codex/accounts")
    mig.migrations_dir().mkdir(parents=True)
    raw = json.dumps({"schema_version": 42})
    mig.state_path().write_text(raw, encoding="utf-8")
    mig.first_run(["list"])
    assert mig.state_path().read_text(encoding="utf-8") == raw
    assert files_in(home / ".codex/accounts") == files
    assert integ.calls == []


@pytest.mark.parametrize("command", ["uninstall-timer", "install-timer", "autoswitch"])
def test_integration_commands_get_data_migration_only(home, integ, tty, command):
    make_store(home, ".grok/accounts")
    mig.first_run([command])
    assert (home / ".ai-accounts/grok/accounts").is_dir()
    assert integ.calls == []


@pytest.mark.parametrize("command", ["migrate", "doctor", "--version"])
def test_diagnostic_commands_skip_the_first_run_check(home, integ, tty, command):
    make_store(home, ".grok/accounts")
    mig.first_run([command])
    assert not mig.state_path().exists()
    assert (home / ".grok/accounts").is_dir()


def test_opt_out_env_skips_the_check(home, integ, tty, monkeypatch):
    monkeypatch.setenv(mig.DISABLE_ENV, "1")
    mig.first_run(["list"])
    assert not mig.state_path().exists()


def test_first_run_failure_warns_and_never_raises(home, monkeypatch, capsys):
    monkeypatch.setattr(mig, "run", lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))
    mig.first_run(["list"])
    assert "ai-accounts migrate" in capsys.readouterr().err


def test_every_entry_point_runs_the_first_run_check_once(home, monkeypatch):
    seen = []
    monkeypatch.delenv(uc._CLAIMED_ENV, raising=False)
    monkeypatch.setattr(uc, "start_check", lambda: None)
    monkeypatch.setattr(uc, "maybe_hint", lambda: None)
    monkeypatch.setattr(mig, "first_run", lambda argv: seen.append(argv))

    def command(argv=None):
        return 7

    wrapped = u.quiet_keyboard_interrupt(command)
    assert wrapped(["list"]) == 7
    assert seen == [["list"]]
    assert wrapped(["list"]) == 7  # nested children of a claimed tree skip it
    assert seen == [["list"]]


def test_timer_tick_migrates_in_background_mode_first(home, monkeypatch):
    calls = []
    monkeypatch.setattr(mig, "first_run", lambda argv: calls.append(("first", os.environ.get(mig.BACKGROUND_ENV))))
    monkeypatch.setattr(autoswitch_timer, "run_once", lambda: calls.append(("tick", None)) or 0)
    assert autoswitch_timer.main(["run"]) == 0
    assert calls == [("first", "1"), ("tick", None)]


def test_vendor_hook_children_run_in_background_mode(home, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(autoswitch_hooks.subprocess, "run",
                        lambda *a, **k: seen.append(os.environ.get(mig.BACKGROUND_ENV)))
    assert autoswitch_hooks.main(["run", "claude"]) == 0
    assert seen == ["1"]


# ── update / migrate / doctor ────────────────────────────────────────────────

def test_post_update_runs_the_new_runtimes_migrate(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/opt/uv/bin/ai-accounts")
    calls = []
    monkeypatch.setattr(uc.subprocess, "run", lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    assert uc._post_update_setup() == 0
    assert calls == [["/opt/uv/bin/ai-accounts", "migrate"]]


def test_update_reports_install_and_migration_as_separate_outcomes(monkeypatch, capsys):
    monkeypatch.setattr(update_kit, "update", lambda **kw: 0)
    monkeypatch.setattr("shutil.which", lambda name: "/opt/uv/bin/ai-accounts")
    monkeypatch.setattr(uc.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1))
    assert uc.run_update("codex-accounts") == 1
    err = capsys.readouterr().err
    assert "installed" in err.lower() and "not rolled back" in err.lower()
    assert "ai-accounts migrate" in err


@pytest.mark.parametrize("module", [p.module for p in PROVIDERS] + ["ai_accounts.ai_accounts"])
def test_every_update_entry_point_shares_one_implementation(monkeypatch, module):
    seen = []
    monkeypatch.setattr(uc, "run_update", lambda cmd: seen.append(cmd) or 0)
    main = importlib.import_module(module).main
    assert main(["update"]) == 0
    assert seen == [uc.command_name(module)]


def test_migrate_cli_dispatch(home, monkeypatch, capsys):
    monkeypatch.setattr(mig, "_integrations_present", lambda: {"timer": False, "hooks": False})
    assert ai_accounts.main(["migrate", "--dry-run"]) == 0
    assert not mig.state_path().exists()
    assert ai_accounts.main(["migrate", "status"]) == 1
    assert ai_accounts.main(["migrate"]) == 0
    assert ai_accounts.main(["migrate", "status"]) == 0
    assert ai_accounts.main(["migrate", "now"]) == 2
    assert "Usage" in capsys.readouterr().err


def test_doctor_reports_migration_state_without_changing_it(home, monkeypatch, capsys):
    make_store(home, ".codex/accounts")
    monkeypatch.setattr(doctor, "_check_timer", lambda: doctor._Check(True, "installed"))
    assert doctor.run_doctor(True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["migrations"]["ok"] is False and "pending" in report["migrations"]["detail"]
    assert not mig.state_path().exists()


def test_autoswitch_setup_records_its_runtime_as_owner(home, integ, monkeypatch):
    from ai_accounts import autoswitch_setup

    monkeypatch.setattr(autoswitch_setup, "install", lambda: None)
    monkeypatch.setattr(autoswitch_setup, "is_installed", lambda: True)
    assert ai_accounts.cmd_autoswitch_setup() == 0
    record = state()["integrations"]
    assert record["executable"] == sys.executable and record["status"] == "ok"


def test_explicit_migrate_from_a_foreign_runtime_never_repoints_integrations(home, integ, monkeypatch, capsys):
    """A scratch venv or `uv run` checkout running `migrate` must not take over
    the hooks/timer the installed `ai-accounts` owns."""
    monkeypatch.setattr(mig, "_is_path_runtime", lambda: False)
    assert mig.run() == 0
    assert integ.calls == []
    assert "ai-accounts autoswitch setup" in capsys.readouterr().err
    assert state().get("integrations") is None


def test_update_marks_its_child_as_the_installed_runtime(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/opt/uv/bin/ai-accounts")
    envs = []
    monkeypatch.setattr(uc.subprocess, "run",
                        lambda cmd, **k: envs.append(k.get("env") or {}) or subprocess.CompletedProcess(cmd, 0))
    assert uc._post_update_setup() == 0
    assert envs[0].get(mig.PATH_RUNTIME_ENV) == "1"


def test_path_runtime_marker_is_trusted(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv(mig.PATH_RUNTIME_ENV, "1")
    assert mig._is_path_runtime() is True


def test_dry_run_says_a_foreign_runtime_would_leave_integrations_alone(home, integ, monkeypatch, capsys):
    monkeypatch.setattr(mig, "_is_path_runtime", lambda: False)
    assert mig.run(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "another ai-accounts installation" in out and "Re-install" not in out
