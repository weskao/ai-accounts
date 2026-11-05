"""Versioned local-data migrations: plan, backup, journal, rollback, lock.

Every test runs against a throwaway HOME (``home`` fixture). Nothing here may
touch a real profile store, credential store, LaunchAgent or scheduled task:
integrations are replaced by recorders and the credential-store backends
raise if anything calls them.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ai_accounts import migrations as mig
from ai_accounts import profile_secrets, secrets_store

POSIX = os.name != "nt"
SECRET = "fake-refresh-token-NEVER-PRINT-0123456789"

# (provider key, legacy path under HOME, default path under HOME)
RELOCATED = (
    ("codex", ".codex/accounts", ".ai-accounts/codex/accounts"),
    ("claude", ".claude/accounts", ".ai-accounts/claude/accounts"),
    ("agy", ".codexbar/antigravity", ".ai-accounts/antigravity"),
    ("grok", ".grok/accounts", ".ai-accounts/grok/accounts"),
    ("vibe", ".vibe/accounts", ".ai-accounts/vibe/accounts"),
    ("copilot", ".copilot/accounts", ".ai-accounts/copilot/accounts"),
)
ENV_OVERRIDES = (
    "CODEX_HOME", "CODEX_ACCOUNT_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_ACCOUNT_DIR",
    "ANTIGRAVITY_HOME", "ANTIGRAVITY_ACCOUNT_DIR", "GROK_HOME", "GROK_ACCOUNT_DIR",
    "VIBE_HOME", "VIBE_ACCOUNT_DIR", "COPILOT_HOME", "COPILOT_ACCOUNT_DIR",
    "DEVIN_ACCOUNT_DIR", "OPENCODE_ACCOUNT_DIR", "OPENCODE_DATA_DIR", "XDG_DATA_HOME",
    "XDG_CONFIG_HOME", "GEMINI_HOME", "AI_ACCOUNTS_NO_AUTO_MIGRATE", "AI_ACCOUNTS_BACKGROUND",
)


class Crash(BaseException):
    """A hard stop (power loss, SIGKILL): nothing after it runs in-process."""


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AI_ACCOUNTS_CONFIG_JSON", str(home / ".ai-accounts" / "config.json"))

    def forbidden(*_a, **_k):
        raise AssertionError("migration must not touch a credential store")

    for name in ("keychain_read", "keychain_write", "go_keyring_read", "go_keyring_write"):
        monkeypatch.setattr(secrets_store.u, name, forbidden, raising=False)
    monkeypatch.setattr(profile_secrets, "save", forbidden, raising=False)
    # No managed integration is present unless a test says so.
    monkeypatch.setattr(mig, "_integrations_present", lambda: {"timer": False, "hooks": False})
    return home


def make_store(home: Path, rel: str, profiles=("work", "home"), active="work") -> dict[str, bytes]:
    store = home / rel
    store.mkdir(parents=True)
    files: dict[str, bytes] = {}
    for name in profiles:
        data = json.dumps({"tokens": {"refresh_token": f"{SECRET}-{name}"}, "email": f"{name}@example.com"})
        (store / f"{name}.json").write_text(data, encoding="utf-8")
        if POSIX:
            (store / f"{name}.json").chmod(0o600)
        files[f"{name}.json"] = data.encode()
    (store / ".current-profile").write_text(active, encoding="utf-8")
    files[".current-profile"] = active.encode()
    return files


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def files_in(path: Path) -> dict[str, bytes]:
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def state() -> dict:
    return json.loads(mig.state_path().read_text(encoding="utf-8"))


def fail_on(real, *, src_has: str = "", dst_has: str = "", error=None):
    """A ``_move`` that raises when the source/destination path contains a marker."""
    error = error or PermissionError(errno.EACCES, "Permission denied")

    def move(src, dst):
        if (src_has and src_has in str(src)) or (dst_has and dst_has in str(dst)):
            raise error
        real(src, dst)

    return move


# ── registry ─────────────────────────────────────────────────────────────────

def test_registry_is_ordered_and_contiguous():
    assert [m.version for m in mig.MIGRATIONS] == list(range(1, mig.CURRENT_SCHEMA + 1))
    assert len({m.name for m in mig.MIGRATIONS}) == len(mig.MIGRATIONS)


# ── fresh install / upgrade ──────────────────────────────────────────────────

def test_fresh_install_marks_current_schema_without_creating_provider_dirs(home):
    assert mig.run() == 0
    assert state()["schema_version"] == mig.CURRENT_SCHEMA
    for _key, legacy, default in RELOCATED:
        assert not (home / legacy).exists()
        assert not (home / default).exists()
    assert not mig.backups_dir().exists()


def test_upgrade_moves_legacy_codex_store_and_keeps_active_profile(home, capsys):
    files = make_store(home, ".codex/accounts")
    assert mig.run() == 0
    moved = home / ".ai-accounts/codex/accounts"
    assert files_in(moved) == files
    assert not (home / ".codex/accounts").exists()
    assert (moved / ".current-profile").read_text(encoding="utf-8") == "work"
    if POSIX:
        assert (moved / "work.json").stat().st_mode & 0o777 == 0o600
    entry = state()["history"][-1]
    assert entry["version"] == 1 and entry["app_version"]
    assert "codex" in capsys.readouterr().err


@pytest.mark.parametrize(("key", "legacy", "default"), RELOCATED)
def test_every_relocatable_provider_store_moves(home, key, legacy, default):
    files = make_store(home, legacy)
    assert any(op["provider"] == key for op in mig.plan_ops())
    assert mig.run() == 0
    assert files_in(home / default) == files
    assert not (home / legacy).exists()


def test_devin_and_opencode_have_no_relocation_and_stay_untouched(home):
    devin = home / ".local/share/devin"
    devin.mkdir(parents=True)
    (devin / "credentials.toml").write_text('api_key = "x"\n', encoding="utf-8")
    opencode = home / ".local/share/opencode"
    opencode.mkdir(parents=True)
    (opencode / "auth.json").write_text("{}", encoding="utf-8")
    before = tree(home / ".local")
    assert all(op["provider"] not in ("devin", "opencode") for op in mig.plan_ops())
    assert mig.run() == 0
    assert tree(home / ".local") == before


def test_successful_run_removes_its_backup_and_journal(home):
    make_store(home, ".codex/accounts")
    assert mig.run() == 0
    assert state().get("journal") is None
    assert not mig.backups_dir().exists() or not any(mig.backups_dir().iterdir())


# ── idempotence / no-op ──────────────────────────────────────────────────────

def test_second_run_is_a_noop_and_never_copies(home, monkeypatch):
    make_store(home, ".codex/accounts")
    assert mig.run() == 0
    state_rel = str(mig.state_path().relative_to(home))
    snapshot = {k: v for k, v in tree(home).items() if k != state_rel}
    monkeypatch.setattr(shutil, "copytree", lambda *a, **k: pytest.fail("no backup on a no-op"))
    assert mig.run() == 0
    assert mig.run() == 0
    assert {k: v for k, v in tree(home).items() if k != state_rel} == snapshot
    assert [h["version"] for h in state()["history"]] == [1]


def test_env_override_store_is_left_where_the_user_put_it(home, monkeypatch):
    files = make_store(home, ".codex/accounts")
    monkeypatch.setenv("CODEX_ACCOUNT_DIR", str(home / "elsewhere"))
    assert mig.run() == 0
    assert files_in(home / ".codex/accounts") == files
    assert not (home / ".ai-accounts/codex/accounts").exists()


def test_both_locations_populated_is_ambiguous_and_left_untouched(home, capsys):
    legacy = make_store(home, ".codex/accounts", profiles=("old",), active="old")
    current = make_store(home, ".ai-accounts/codex/accounts", profiles=("new",), active="new")
    assert mig.run() == 0
    assert files_in(home / ".codex/accounts") == legacy
    assert files_in(home / ".ai-accounts/codex/accounts") == current
    err = capsys.readouterr().err
    assert "codex" in err and ".codex" in err
    assert state()["history"][-1]["notes"]


def test_status_rechecks_a_conflict_the_user_has_since_resolved(home, capsys):
    make_store(home, ".codexbar/antigravity", profiles=("old",), active="old")
    make_store(home, ".ai-accounts/antigravity", profiles=("new",), active="new")
    assert mig.run() == 0
    capsys.readouterr()
    assert mig.status() == 0
    assert ".codexbar" in capsys.readouterr().out
    shutil.rmtree(home / ".codexbar/antigravity")
    assert mig.status() == 0
    assert ".codexbar" not in capsys.readouterr().out
    assert state()["history"][-1]["notes"]  # the record of what happened stays


def test_empty_default_dir_does_not_block_the_move(home):
    files = make_store(home, ".claude/accounts")
    (home / ".ai-accounts/claude/accounts").mkdir(parents=True)
    assert mig.run() == 0
    assert files_in(home / ".ai-accounts/claude/accounts") == files


def test_corrupt_profile_json_moves_byte_for_byte_and_is_reported(home, capsys):
    store = home / ".grok/accounts"
    store.mkdir(parents=True)
    (store / "broken.json").write_bytes(b"{not json")
    assert mig.run() == 0
    assert (home / ".ai-accounts/grok/accounts/broken.json").read_bytes() == b"{not json"
    assert "broken.json" in capsys.readouterr().err


# ── dry-run / status ─────────────────────────────────────────────────────────

def test_dry_run_lists_the_plan_and_changes_nothing(home, capsys):
    make_store(home, ".codex/accounts")
    before = tree(home)
    assert mig.run(dry_run=True) == 0
    assert tree(home) == before
    assert not mig.migrations_dir().exists()
    out = capsys.readouterr().out
    assert "codex" in out and ".codex" in out


def test_status_reports_pending_then_current_without_changes(home, capsys):
    make_store(home, ".codex/accounts")
    before = tree(home)
    assert mig.status() == 1
    assert tree(home) == before
    assert f"0 → {mig.CURRENT_SCHEMA}" in capsys.readouterr().out
    assert mig.run() == 0
    capsys.readouterr()
    assert mig.status() == 0
    assert str(mig.CURRENT_SCHEMA) in capsys.readouterr().out


# ── newer / corrupt state ────────────────────────────────────────────────────

def test_newer_schema_refuses_and_never_downgrades(home, capsys):
    files = make_store(home, ".codex/accounts")
    mig.migrations_dir().mkdir(parents=True)
    raw = json.dumps({"schema_version": 99, "history": []})
    mig.state_path().write_text(raw, encoding="utf-8")
    assert mig.run() == 1
    assert mig.state_path().read_text(encoding="utf-8") == raw
    assert files_in(home / ".codex/accounts") == files
    assert "99" in capsys.readouterr().err
    assert mig.status() == 1


def test_corrupt_state_is_set_aside_and_migration_redetects(home):
    make_store(home, ".codex/accounts")
    mig.migrations_dir().mkdir(parents=True)
    mig.state_path().write_text("{garbage", encoding="utf-8")
    assert mig.run() == 0
    assert state()["schema_version"] == mig.CURRENT_SCHEMA
    kept = list(mig.migrations_dir().glob("state.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{garbage"
    assert (home / ".ai-accounts/codex/accounts/work.json").exists()


# ── failure, rollback and crash recovery ─────────────────────────────────────

def test_insufficient_disk_space_fails_before_any_change(home, monkeypatch, capsys):
    files = make_store(home, ".codex/accounts")
    monkeypatch.setattr(mig.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(1, 1, 0))
    assert mig.run() == 1
    assert files_in(home / ".codex/accounts") == files
    assert not (home / ".ai-accounts/codex").exists()
    assert "disk" in capsys.readouterr().err.lower()
    assert state()["schema_version"] == 0
    assert state()["last_failure"]


def test_permission_denied_midway_rolls_back_earlier_moves(home, monkeypatch, capsys):
    codex = make_store(home, ".codex/accounts")
    make_store(home, ".claude/accounts")
    monkeypatch.setattr(mig, "_move", fail_on(mig._move, src_has=".claude"))
    assert mig.run() == 1
    assert files_in(home / ".codex/accounts") == codex
    assert not (home / ".ai-accounts/codex/accounts").exists()
    err = capsys.readouterr().err
    assert "Permission denied" in err and "rolled back" in err.lower()
    assert state()["schema_version"] == 0
    assert state().get("journal") is None
    # Fully restored: no stray plaintext copy is left behind.
    assert not mig.backups_dir().exists() or not any(mig.backups_dir().iterdir())


def test_rollback_failure_names_what_was_not_restored_and_keeps_backup(home, monkeypatch, capsys):
    make_store(home, ".codex/accounts")
    make_store(home, ".claude/accounts")
    # claude's move fails, and so does the undo of codex's.
    monkeypatch.setattr(mig, "_move", fail_on(mig._move, src_has=".claude", dst_has=".codex"))
    assert mig.run() == 1
    failure = state()["last_failure"]
    assert any("codex" in item for item in failure["unrestored"])
    backup = Path(failure["backup"])
    assert (backup / "codex" / "work.json").exists()
    err = capsys.readouterr().err
    assert "codex" in err and str(backup) in err


def test_validation_mismatch_rolls_back(home, monkeypatch):
    files = make_store(home, ".vibe/accounts")
    real = mig._manifest

    def manifest(path):
        result = real(path)
        return {**result, "ghost": ("file", 0, "x")} if ".ai-accounts" in str(path) else result

    monkeypatch.setattr(mig, "_manifest", manifest)
    assert mig.run() == 1
    assert files_in(home / ".vibe/accounts") == files
    assert not (home / ".ai-accounts/vibe/accounts").exists()


def test_crash_after_first_move_is_recovered_on_next_run(home, monkeypatch):
    codex = make_store(home, ".codex/accounts")
    claude = make_store(home, ".claude/accounts")
    real = mig._move
    monkeypatch.setattr(mig, "_move", fail_on(real, src_has=".claude", error=Crash()))
    with pytest.raises(Crash):
        mig.run()
    assert state()["journal"]["ops"]
    monkeypatch.setattr(mig, "_move", real)
    assert mig.run() == 0
    assert files_in(home / ".ai-accounts/codex/accounts") == codex
    assert files_in(home / ".ai-accounts/claude/accounts") == claude
    assert state().get("journal") is None


def test_crash_between_staged_cross_device_writes_is_recovered(home, monkeypatch):
    files = make_store(home, ".grok/accounts")
    real_rename = os.rename

    def rename(src, dst):
        if ".grok" in str(src) and ".ai-accounts" in str(dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        if ".migrating" in str(src):
            raise Crash  # staging copy written, final rename never happened
        real_rename(src, dst)

    monkeypatch.setattr(mig.os, "rename", rename)
    with pytest.raises(Crash):
        mig.run()
    monkeypatch.setattr(mig.os, "rename", real_rename)
    assert mig.run() == 0
    assert files_in(home / ".ai-accounts/grok/accounts") == files
    assert not list((home / ".ai-accounts/grok").glob(".*migrating*"))
    assert not (home / ".grok/accounts").exists()


def test_cross_device_move_copies_then_removes_source(home, monkeypatch):
    files = make_store(home, ".copilot/accounts")
    real_rename = os.rename

    def rename(src, dst):
        if ".copilot" in str(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(mig.os, "rename", rename)
    assert mig.run() == 0
    assert files_in(home / ".ai-accounts/copilot/accounts") == files
    assert not (home / ".copilot/accounts").exists()


def test_rollback_keeps_a_newer_credential_generation_written_after_the_move(home, monkeypatch):
    make_store(home, ".codex/accounts")
    make_store(home, ".claude/accounts")
    real = mig._move
    newer = b'{"tokens": {"refresh_token": "rotated"}}'

    def move(src, dst):
        if ".claude" in str(src):
            # Another process rotated codex's saved profile after it moved.
            (home / ".ai-accounts/codex/accounts/work.json").write_bytes(newer)
            raise OSError(errno.EIO, "I/O error")
        real(src, dst)

    monkeypatch.setattr(mig, "_move", move)
    assert mig.run() == 1
    assert (home / ".codex/accounts/work.json").read_bytes() == newer


def test_vendor_credentials_rotated_mid_migration_are_never_touched(home, monkeypatch):
    make_store(home, ".codex/accounts")
    auth = home / ".codex/auth.json"
    auth.write_text('{"gen": 1}', encoding="utf-8")
    real = mig._move

    def move(src, dst):
        real(src, dst)
        auth.write_text('{"gen": 2}', encoding="utf-8")  # codex CLI refreshed meanwhile

    monkeypatch.setattr(mig, "_move", move)
    assert mig.run() == 0
    assert auth.read_text(encoding="utf-8") == '{"gen": 2}'


def test_migration_never_needs_a_credential_store(home):
    """Locked, missing or unavailable keychain/keyring: the fixture makes every
    backend raise, and the relocation still completes."""
    make_store(home, ".vibe/accounts")
    assert mig.run() == 0


# ── security ─────────────────────────────────────────────────────────────────

def test_no_secret_reaches_output_or_state_on_success_or_failure(home, monkeypatch, capsys):
    make_store(home, ".codex/accounts")
    make_store(home, ".claude/accounts")
    assert mig.run(dry_run=True) == 0
    assert mig.status() == 1
    real = mig._move
    monkeypatch.setattr(mig, "_move", fail_on(real, src_has=".claude", error=OSError(errno.EIO, "I/O")))
    assert mig.run() == 1
    monkeypatch.setattr(mig, "_move", real)
    assert mig.run() == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    for path in mig.migrations_dir().glob("*.json"):
        assert SECRET not in path.read_text(encoding="utf-8")


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_state_and_kept_backup_are_private(home, monkeypatch):
    make_store(home, ".codex/accounts")
    make_store(home, ".claude/accounts")
    monkeypatch.setattr(mig, "_move", fail_on(mig._move, src_has=".claude", dst_has=".codex"))
    assert mig.run() == 1
    assert mig.migrations_dir().stat().st_mode & 0o077 == 0
    assert mig.state_path().stat().st_mode & 0o777 == 0o600
    backup = Path(state()["last_failure"]["backup"])
    assert backup.stat().st_mode & 0o077 == 0
    assert (backup / "codex" / "work.json").stat().st_mode & 0o777 == 0o600


# ── concurrency ──────────────────────────────────────────────────────────────

def _env_for(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ENV_OVERRIDES}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8="1",
               AI_ACCOUNTS_CONFIG_JSON=str(home / ".ai-accounts" / "config.json"))
    return env


def test_concurrent_migration_processes_apply_it_exactly_once(home):
    files = make_store(home, ".codex/accounts")
    script = "from ai_accounts import migrations as m; raise SystemExit(m.run(repair=False))"
    procs = [
        subprocess.Popen([sys.executable, "-c", script], env=_env_for(home),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(4)
    ]
    codes = [p.wait(timeout=60) for p in procs]
    assert codes == [0, 0, 0, 0], [p.stderr.read() for p in procs]
    assert files_in(home / ".ai-accounts/codex/accounts") == files
    assert [h["version"] for h in state()["history"]] == [1]


def test_lock_held_by_another_process_times_out_without_changes(home, capsys):
    files = make_store(home, ".codex/accounts")
    mig.migrations_dir().mkdir(parents=True, exist_ok=True)
    holder = textwrap.dedent(f"""
        import sys
        from ai_accounts import _utils as u
        with u.file_lock({str(mig.lock_path())!r}, timeout=5) as ok:
            print("locked" if ok else "busy", flush=True)
            sys.stdin.read()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], env=_env_for(home),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert mig.run(lock_timeout=0.3) == 1
        assert files_in(home / ".codex/accounts") == files
        assert "another" in capsys.readouterr().err.lower()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_lock_left_by_a_dead_process_is_not_stale(home):
    """The OS releases the lock with its holder, so a crash never blocks forever."""
    make_store(home, ".codex/accounts")
    mig.migrations_dir().mkdir(parents=True, exist_ok=True)
    holder = f"from ai_accounts import _utils as u\nimport os\nwith u.file_lock({str(mig.lock_path())!r}, timeout=5):\n    os._exit(0)\n"
    subprocess.run([sys.executable, "-c", holder], env=_env_for(home), check=True, timeout=60)
    assert mig.lock_path().exists()
    assert mig.run(lock_timeout=1) == 0


# ── untrusted journal contents ───────────────────────────────────────────────

@pytest.mark.parametrize("backup", ["", "OUTSIDE"])
def test_recovery_never_deletes_a_backup_path_outside_its_own_folder(home, tmp_path, monkeypatch, backup):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")
    monkeypatch.chdir(victim)
    mig.migrations_dir().mkdir(parents=True)
    path = str(victim) if backup == "OUTSIDE" else backup
    mig.state_path().write_text(json.dumps({
        "schema_version": 0,
        "journal": {"version": 1, "name": "relocate-legacy-profile-stores", "backup": path, "ops": []},
    }), encoding="utf-8")
    assert mig.run() == 0
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_unwritable_state_folder_fails_cleanly(home, monkeypatch, capsys):
    def boom(_path):
        raise PermissionError(errno.EACCES, "Permission denied", "migrations")

    monkeypatch.setattr(mig, "_private_dir", boom)
    assert mig.cmd_migrate([]) == 1
    assert "Permission denied" in capsys.readouterr().err


# ── review follow-ups ────────────────────────────────────────────────────────

def test_a_store_moved_by_another_process_mid_run_is_not_moved_back(home, monkeypatch):
    """A lazy move (doctor, an old runtime) wins the race: our rename fails,
    and rollback must not drag the store back to the legacy path."""
    files = make_store(home, ".codex/accounts")
    real = mig._move

    def move(src, dst):
        real(src, dst)  # the other process
        os.rename(src, dst)  # ours: source is gone now

    monkeypatch.setattr(mig, "_move", move)
    assert mig.run() == 1
    assert files_in(home / ".ai-accounts/codex/accounts") == files
    assert not (home / ".codex/accounts").exists()


def test_ctrl_c_mid_migration_rolls_back_and_still_cancels(home, monkeypatch):
    codex = make_store(home, ".codex/accounts")
    make_store(home, ".claude/accounts")
    monkeypatch.setattr(mig, "_move", fail_on(mig._move, src_has=".claude", error=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        mig.run()
    assert files_in(home / ".codex/accounts") == codex
    assert state().get("journal") is None


def test_a_kept_backup_is_still_reported_after_a_later_success(home, monkeypatch, capsys):
    make_store(home, ".codex/accounts")
    make_store(home, ".claude/accounts")
    real = mig._move
    monkeypatch.setattr(mig, "_move", fail_on(real, src_has=".claude", dst_has=".codex"))
    assert mig.run() == 1
    backup = state()["last_failure"]["backup"]
    monkeypatch.setattr(mig, "_move", real)
    assert mig.run() == 0
    capsys.readouterr()
    assert mig.status() == 1
    assert backup in capsys.readouterr().out
    assert mig.summary()["ok"] is False
    shutil.rmtree(backup)
    assert mig.status() == 0


@pytest.mark.skipif(not POSIX, reason="symlink creation needs privileges on Windows")
def test_symlinked_store_moved_across_filesystems_stays_a_link(home, tmp_path, monkeypatch):
    target = tmp_path / "real-store"
    target.mkdir()
    (target / "work.json").write_text("{}", encoding="utf-8")
    (home / ".grok").mkdir()
    (home / ".grok/accounts").symlink_to(target)
    real_rename = os.rename

    def rename(src, dst):
        if ".grok" in str(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(src, dst)

    monkeypatch.setattr(mig.os, "rename", rename)
    assert mig.run() == 0
    moved = home / ".ai-accounts/grok/accounts"
    assert moved.is_symlink() and moved.resolve() == target.resolve()
    assert (target / "work.json").exists()
    assert not (home / ".grok/accounts").exists() and not (home / ".grok/accounts").is_symlink()
