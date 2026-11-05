"""Versioned, crash-safe migration of ai-accounts' own local data, plus the
post-upgrade repair of its managed hooks and scheduler.

``run()`` is the whole upgrade lifecycle after the package itself is
installed: lock → preflight → backup → journaled apply → validate → commit →
repair integrations → report. Every entry point reaches it: ``<cmd> update``
(through ``update_check._post_update_setup``, which launches the *newly
installed* ``ai-accounts migrate``), ``ai-accounts migrate``, and
:func:`first_run`, which every command calls so an upgrade done by
``uv tool install`` / ``pipx`` (bypassing ``update``) still migrates before the
command reads any data.

State lives in ``migrations/state.json`` beside ``config.json`` (so
``$AI_ACCOUNTS_CONFIG_JSON`` moves it too), owner-only. ``schema_version`` is
the data schema, separate from the app version: it only changes when a
migration lands. A newer schema than this build knows is refused, never
downgraded.

Adding a migration: append ``Migration(N + 1, "<name>", plan)`` to
:data:`MIGRATIONS`. ``plan()`` only *detects* (no writes) and returns
``(ops, notes)``; ops use a kind :func:`_apply_op` / :func:`_undo_op` know
("move" today). A provider joins store relocation by defining
``_store_paths()`` in its own module — no edit here.

Deliberately **not** migrated here:

- Devin and OpenCode define no ``_store_paths()``: their legacy location *is*
  the default, so there is nothing to move.
- Inline (plaintext) tokens in saved profiles and the legacy plaintext
  ``telegram_bot_token`` in ``config.json`` move into the OS credential store
  lazily, on the owning code's next read/save (``profile_secrets``,
  ``autoswitch.save_config``). Doing it here would mean credential-store writes
  from a background job (a Keychain prompt in a LaunchAgent, a locked
  keyring) and a plaintext rollback copy of each secret.
- Vendor CLI data (``~/.codex/auth.json``, Keychain/keyring items, opencode's
  ``auth.json``) is never read or written; tokens are never refreshed here.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import _utils as u
from . import i18n

CURRENT_SCHEMA = 1
DISABLE_ENV = "AI_ACCOUNTS_NO_AUTO_MIGRATE"
BACKGROUND_ENV = "AI_ACCOUNTS_BACKGROUND"
#: Set by `update` on the `ai-accounts migrate` it launches from PATH: that
#: child is the installed runtime by construction (Windows launchers included).
PATH_RUNTIME_ENV = "AI_ACCOUNTS_PATH_RUNTIME"
LOCK_TIMEOUT = 30.0
_BACKGROUND_LOCK_TIMEOUT = 10.0
_SPACE_MARGIN = 1 << 20
# Commands that must not trigger the first-run check: read-only diagnostics
# and `migrate` itself (it runs the same work explicitly).
_NO_FIRST_RUN = frozenset({"migrate", "doctor", "help", "-h", "--help", "version", "--version"})
# Commands that manage integrations themselves: data migration only.
_SELF_MANAGED = frozenset({"autoswitch", "install-timer", "uninstall-timer", "timer-status"})

Op = dict[str, object]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    plan: Callable[[], tuple[list[Op], list[str]]]


class _CorruptState(Exception):
    pass


class _ValidationError(Exception):
    pass


# ── paths and state ──────────────────────────────────────────────────────────

def migrations_dir() -> Path:
    from . import autoswitch

    return autoswitch.config_path().parent / "migrations"


def state_path() -> Path:
    return migrations_dir() / "state.json"


def lock_path() -> Path:
    return migrations_dir() / "lock"


def backups_dir() -> Path:
    return migrations_dir() / "backups"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":  # Windows: inherited ACLs, no POSIX mode
        path.chmod(0o700)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_state() -> dict:
    """The stored state, ``{}`` before the first migration; raises ``_CorruptState``."""
    try:
        raw = state_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("schema_version", 0), int):
        raise _CorruptState
    return data


def _write_state(state: dict) -> None:
    _private_dir(migrations_dir())
    for attempt in range(20):
        try:
            u.atomic_write_json(state_path(), state, durable=True)
            return
        except PermissionError:
            # Windows refuses to replace a file another process has open; the
            # lock-free first-run read holds it for microseconds.
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.05)


def _owned_backup(value: object) -> Path | None:
    """A journal's backup path, only if it is inside our backups folder.

    The journal is read back from disk: an empty or edited value must never
    reach ``rmtree`` (``Path("")`` is the current directory).
    """
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    try:
        inside = path.resolve().is_relative_to(backups_dir().resolve()) and path.resolve() != backups_dir().resolve()
    except OSError:
        return None
    return path if inside else None


def _load_for_write() -> dict:
    """:func:`read_state`, setting a corrupt file aside (kept for diagnosis).

    Safe because every plan re-detects from the filesystem: an unknown
    state just means "re-check everything".
    """
    try:
        return read_state()
    except _CorruptState:
        aside = state_path().with_name(f"state.json.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        os.replace(state_path(), aside)
        _say("migrate.corrupt_state", path=aside)
        return {}


# ── messages ─────────────────────────────────────────────────────────────────

def _lang() -> str:
    from .update_check import _language

    return _language()


def _say(msgid: str, *, out=None, **fields: object) -> None:
    print(i18n.t(msgid, _lang(), **fields), file=out or sys.stderr)


# ── migration 1: relocate legacy profile stores ──────────────────────────────

def _plan_relocations() -> tuple[list[Op], list[str]]:
    """Move each provider's store from its old dotdir location into ~/.ai-accounts.

    Same rule as ``_utils.resolve_data_dir`` (which still does it lazily for
    a process that skipped this): an env override wins and nothing moves; an
    occupied destination is never merged into.
    """
    from .providers import PROVIDERS

    lang = _lang()
    ops: list[Op] = []
    notes: list[str] = []
    for provider in PROVIDERS:
        paths = getattr(importlib.import_module(provider.module), "_store_paths", None)
        if paths is None:
            continue
        env, default, legacy = paths()
        if legacy == default or not (legacy.exists() or legacy.is_symlink()):
            continue
        fields = {"provider": provider.key, "legacy": legacy, "current": default, "env": env}
        if os.environ.get(env):
            notes.append(i18n.t("migrate.note.env_override", lang, **fields))
        elif not legacy.is_dir():
            notes.append(i18n.t("migrate.note.not_dir", lang, **fields))
        elif (default.exists() or default.is_symlink()) and not _is_empty_dir(default):
            notes.append(i18n.t("migrate.note.ambiguous", lang, **fields))
        else:
            ops.append({"kind": "move", "provider": provider.key, "src": str(legacy), "dst": str(default)})
    return ops, notes


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "relocate-legacy-profile-stores", _plan_relocations),
)


def _pending(schema: int) -> list[Migration]:
    return [m for m in MIGRATIONS if m.version > schema]


def plan_ops() -> list[Op]:
    """Every operation the pending migrations would apply now (read-only)."""
    try:
        schema = read_state().get("schema_version", 0)
    except _CorruptState:
        schema = 0
    return [op for m in _pending(schema) for op in m.plan()[0]]


# ── filesystem operations ────────────────────────────────────────────────────

def _is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not any(path.iterdir())


def _staging(dst: Path) -> Path:
    return dst.with_name(f".{dst.name}.migrating")


def _move(src: Path, dst: Path) -> None:
    """Rename *src* to *dst*; across filesystems, copy to a staging sibling,
    rename that into place, then remove the source."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if _is_empty_dir(dst):
        dst.rmdir()
    try:
        os.rename(src, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    staging = _staging(dst)
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging, symlinks=True)
    os.rename(staging, dst)
    shutil.rmtree(src)


def _manifest(root: Path) -> dict[str, tuple]:
    """Every entry under *root*: kind plus size and sha256 for files."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_file():
                out[rel] = ("file", path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                out[rel] = ("dir",)
    return out


def _size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def _existing_ancestor(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _preflight(ops: list[Op]) -> str | None:
    """Why the ops cannot run (no write access, not enough disk), else None."""
    lang = _lang()
    need = _SPACE_MARGIN
    for op in ops:
        src, dst = Path(str(op["src"])), Path(str(op["dst"]))
        for target in (src, src.parent, _existing_ancestor(dst.parent)):
            if not os.access(target, os.W_OK):
                return i18n.t("migrate.no_permission", lang, path=target)
        need += 2 * _size(src)  # the backup, plus a copy if the move crosses filesystems
    free = shutil.disk_usage(_existing_ancestor(migrations_dir())).free
    if free < need:
        return i18n.t("migrate.no_space", lang, need=need, free=free)
    return None


def _apply_op(op: Op) -> None:
    _move(Path(str(op["src"])), Path(str(op["dst"])))


def _undo_op(op: Op) -> str | None:
    """Put a move back however far it got; the reason when it cannot.

    Moving the *current* destination back (not restoring the backup) keeps
    anything written there after the move, e.g. a rotated token.
    """
    src, dst = Path(str(op["src"])), Path(str(op["dst"]))
    try:
        staging = _staging(dst)
        if staging.exists():
            shutil.rmtree(staging)
        if src.exists() and (not dst.exists() or _is_empty_dir(dst)):
            return None
        if dst.exists() and not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            _move(dst, src)
            return None
    except OSError as exc:
        return f"{op['provider']}: {dst} → {src}: {exc}"
    if src.exists():
        return f"{op['provider']}: {src} + {dst}"
    return f"{op['provider']}: {src}"


# ── one migration ────────────────────────────────────────────────────────────

def _rollback(ops: list[Op]) -> list[str]:
    return [problem for op in reversed(ops) if (problem := _undo_op(op))]


def _fail(state: dict, migration: Migration, error: str, unrestored: list[str], backup: Path | None) -> None:
    backup = _owned_backup(str(backup)) if backup is not None else None
    keep = bool(unrestored) and backup is not None and backup.exists()
    if backup is not None and not keep:
        shutil.rmtree(backup, ignore_errors=True)
    state.setdefault("schema_version", 0)
    state["journal"] = None
    state["last_failure"] = {
        "at": _now(), "version": migration.version, "name": migration.name,
        "error": error, "unrestored": unrestored, "backup": str(backup) if keep else None,
    }
    _write_state(state)
    _say("migrate.failed", version=migration.version, name=migration.name, error=error)
    if not unrestored:
        _say("migrate.rolled_back")
    for item in unrestored:
        _say("migrate.unrestored", item=item)
    if keep:
        _say("migrate.backup_kept", path=backup)


def _json_notes(ops: list[Op]) -> list[str]:
    lang = _lang()
    notes = []
    for op in ops:
        for path in sorted(Path(str(op["dst"])).rglob("*.json")):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                notes.append(i18n.t("migrate.note.invalid_json", lang, provider=op["provider"], file=path.name))
    return notes


def _commit(state: dict, migration: Migration, notes: list[str]) -> None:
    state["schema_version"] = migration.version
    state["journal"] = None
    state["last_failure"] = None
    state.setdefault("history", []).append({
        "version": migration.version, "name": migration.name,
        "app_version": u.package_version(), "completed_at": _now(), "notes": notes,
    })
    _write_state(state)
    for note in notes:
        _say("migrate.note", note=note)


def _apply(state: dict, migration: Migration) -> bool:
    ops, notes = migration.plan()
    if not ops:
        _commit(state, migration, notes)
        return True
    problem = _preflight(ops)
    if problem:
        _fail(state, migration, problem, [], None)
        return False
    backup = backups_dir() / f"{datetime.now().strftime('%Y%m%d%H%M%S')}-v{migration.version}"
    journal_ops = [dict(op) for op in ops]
    state["journal"] = {
        "version": migration.version, "name": migration.name, "started_at": _now(),
        "backup": str(backup), "ops": journal_ops,
    }
    _write_state(state)
    try:
        _private_dir(backup)
        manifests = {}
        for op in journal_ops:
            src = Path(str(op["src"]))
            manifests[op["src"]] = _manifest(src)
            shutil.copytree(src, backup / str(op["provider"]), symlinks=True)
        for op in journal_ops:
            _apply_op(op)
            op["done"] = True
            _write_state(state)
        for op in journal_ops:
            if _manifest(Path(str(op["dst"]))) != manifests[op["src"]]:
                raise _ValidationError(i18n.t("migrate.validation", _lang(), provider=op["provider"]))
    except (Exception, KeyboardInterrupt) as exc:  # a hard crash is handled by _recover instead
        _fail(state, migration, str(exc), _rollback(journal_ops), backup)
        return False
    for op in journal_ops:
        _say("migrate.moved", provider=op["provider"], src=op["src"], dst=op["dst"])
    _commit(state, migration, notes + _json_notes(journal_ops))
    shutil.rmtree(backup, ignore_errors=True)
    return True


def _recover(state: dict) -> bool:
    """Roll back a migration a crash left half-applied. False if it cannot."""
    journal = state["journal"]
    backup = _owned_backup(journal.get("backup"))
    unrestored = _rollback([op for op in journal.get("ops") or [] if op.get("kind") == "move"])
    migration = next(
        (m for m in MIGRATIONS if m.version == journal.get("version")),
        Migration(int(journal.get("version") or 0), str(journal.get("name")), lambda: ([], [])),
    )
    if unrestored:
        _fail(state, migration, i18n.t("migrate.interrupted", _lang()), unrestored, backup)
        return False
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    state["journal"] = None
    _write_state(state)
    _say("migrate.recovered", version=migration.version)
    return True


# ── integrations ─────────────────────────────────────────────────────────────

def _integrations_present() -> dict[str, bool]:
    from . import autoswitch_hooks, autoswitch_timer

    return {"timer": autoswitch_timer.status() == "installed", "hooks": autoswitch_hooks.any_installed()}


def record_integrations(status: str, problems: list[str] | None = None) -> None:
    """Remember which runtime last installed the hooks/timer (never raises)."""
    try:
        with u.file_lock(lock_path(), timeout=LOCK_TIMEOUT) as locked:
            if not locked:
                return
            try:
                state = read_state()
            except _CorruptState:
                return
            state["integrations"] = {
                "version": u.package_version(), "executable": sys.executable,
                "status": status, "problems": problems or [], "at": _now(),
            }
            _write_state(state)
    except OSError:
        pass


def _is_path_runtime() -> bool:
    """Whether this interpreter is the one behind ``ai-accounts`` on PATH.

    shortcut: Windows uv/pipx launchers are copies, not symlinks, so this is
    False there unless `update` launched us (``PATH_RUNTIME_ENV``); repair
    then waits for the next `update`.
    """
    if os.environ.get(PATH_RUNTIME_ENV):
        return True
    found = shutil.which("ai-accounts")
    if not found:
        return False
    here = Path(sys.executable).absolute().parent
    return Path(found).resolve().parent in (here, here.resolve())


def _owns(state: dict) -> bool:
    """Whether this runtime may (re)write the hooks/timer.

    Yes for the installed `ai-accounts` launched by `update`, the runtime that
    last installed them, or any runtime once that one is gone. Never for
    another live interpreter: a `uv run` checkout or a scratch venv must not
    re-point the user's installed hooks and timer at itself.
    """
    if os.environ.get(PATH_RUNTIME_ENV):
        return True
    owner = (state.get("integrations") or {}).get("executable")
    if owner:
        return owner == sys.executable or not Path(str(owner)).exists()
    return _is_path_runtime()


def _owner_label(state: dict) -> str:
    """Which installation the hooks/timer belong to, for the "left unchanged" note."""
    return str((state.get("integrations") or {}).get("executable") or shutil.which("ai-accounts") or "?")


def _repair_due(state: dict) -> bool:
    """First-run repair: once per new version (never for an older one), by the owner."""
    from .update_check import version_tuple

    record = state.get("integrations") or {}
    recorded = version_tuple(str(record.get("version") or ""))
    mine = version_tuple(u.package_version())
    if recorded and mine and recorded >= mine:
        return False
    return _owns(state)


def _repair(state: dict, *, quiet: bool) -> int:
    """Re-install whichever managed integrations are present, then verify them.

    Absent ones stay absent: the user never set them up or removed them, and
    a repair must not enable a feature without consent.
    """
    from . import autoswitch_hooks, autoswitch_timer
    from .providers import PROVIDERS

    present = _integrations_present()
    problems: list[str] = []
    if any(present.values()) and not _owns(state):
        owner = _owner_label(state)
        _say("migrate.integrations.foreign", owner=owner)
        return 0
    if not any(present.values()):
        status = "absent"
        if not quiet:
            _say("migrate.integrations.absent")
    else:
        if present["hooks"]:
            autoswitch_hooks.install()  # logs and keeps going on a per-file error
            if not autoswitch_hooks.is_installed():
                problems.append(i18n.t("migrate.integrations.hooks_failed", _lang()))
        if present["timer"]:
            try:
                autoswitch_timer.install()  # keeps the configured interval
            except Exception as exc:  # noqa: BLE001 - CalledProcessError, OSError, ValueError
                problems.append(f"timer: {exc}")
            else:
                if autoswitch_timer.status() != "installed":
                    problems.append(i18n.t("migrate.integrations.timer_failed", _lang()))
                elif u.IS_MACOS and (folder := autoswitch_timer._tcc_blocked_source()):
                    problems.append(i18n.t("migrate.integrations.tcc", _lang(), folder=folder))
        status = "failed" if problems else "ok"
        if not problems:
            done = [name for name, on in present.items() if on]
            _say("migrate.integrations.ok", items=", ".join(done))
        for problem in problems:
            _say("migrate.integrations.problem", problem=problem)
        if problems:
            _say("migrate.retry")
        missing = [p.binary for p in PROVIDERS if not u.have(p.binary)]
        if missing and not quiet:
            _say("migrate.integrations.missing_cli", names=", ".join(missing))
    state["integrations"] = {
        "version": u.package_version(), "executable": sys.executable,
        "status": status, "problems": problems, "at": _now(),
    }
    _write_state(state)
    return 1 if problems else 0


# ── entry points ─────────────────────────────────────────────────────────────

def _newer(schema: int) -> bool:
    if schema <= CURRENT_SCHEMA:
        return False
    _say("migrate.newer", found=schema, supported=CURRENT_SCHEMA)
    return True


def _run_locked(*, repair: bool, auto: bool) -> int:
    state = _load_for_write()
    if _newer(int(state.get("schema_version", 0))):
        return 1
    if state.get("journal") and not _recover(state):
        return 1
    before = int(state.get("schema_version", 0))
    for migration in _pending(before):
        if not _apply(state, migration):
            return 1
    after = int(state.get("schema_version", 0))
    if after != before:
        _say("migrate.done", old=before, new=after)
    elif not auto:
        _say("migrate.current", version=after)
    if repair and (not auto or _repair_due(state)):
        return _repair(state, quiet=auto)
    return 0


def _dry_run() -> int:
    out = sys.stdout
    try:
        state = read_state()
    except _CorruptState:
        state = {}
        _say("migrate.status.corrupt", out=out)
    schema = int(state.get("schema_version", 0))
    if _newer(schema):
        return 1
    _say("migrate.plan.header", out=out)
    pending = _pending(schema)
    if not pending:
        _say("migrate.plan.none", out=out)
    for migration in pending:
        ops, notes = migration.plan()
        _say("migrate.plan.migration", out=out, version=migration.version, name=migration.name)
        for op in ops:
            _say("migrate.plan.move", out=out, provider=op["provider"], src=op["src"], dst=op["dst"])
        for note in notes:
            _say("migrate.note", out=out, note=note)
    present = [name for name, on in _integrations_present().items() if on]
    if present and not _owns(state):
        owner = _owner_label(state)
        _say("migrate.integrations.foreign", out=out, owner=owner)
    elif present:
        _say("migrate.plan.repair", out=out, items=", ".join(present))
    else:
        _say("migrate.integrations.absent", out=out)
    return 0


def run(*, dry_run: bool = False, repair: bool = True, lock_timeout: float = LOCK_TIMEOUT, auto: bool = False) -> int:
    """Apply every pending migration, then repair integrations. 0 ok, 1 failed.

    *auto* is the first-run check: quiet when nothing happens, and integration
    repair only when :func:`_repair_due`.
    """
    if dry_run:
        return _dry_run()
    try:
        _private_dir(migrations_dir())
        with u.file_lock(lock_path(), timeout=lock_timeout) as locked:
            if not locked:
                _say("migrate.busy")
                return 1
            return _run_locked(repair=repair, auto=auto)
    except OSError as exc:  # state folder unwritable, disk full while journaling
        _say("migrate.failed_io", error=exc)
        return 1


def status() -> int:
    """Print schema and integration state (read-only). 0 healthy, 1 needs action."""
    out = sys.stdout
    try:
        state = read_state()
    except _CorruptState:
        _say("migrate.status.corrupt", out=out)
        return 1
    except OSError as exc:
        _say("migrate.failed_io", error=exc)
        return 1
    schema = int(state.get("schema_version", 0))
    healthy = schema == CURRENT_SCHEMA and not state.get("journal") and not state.get("last_failure")
    if schema > CURRENT_SCHEMA:
        _say("migrate.newer", out=out, found=schema, supported=CURRENT_SCHEMA)
    elif schema < CURRENT_SCHEMA:
        names = ", ".join(m.name for m in _pending(schema))
        _say("migrate.status.pending", out=out, old=schema, new=CURRENT_SCHEMA, names=names)
    else:
        _say("migrate.status.current", out=out, version=schema)
    if history := state.get("history"):
        last = history[-1]
        _say("migrate.status.last", out=out, version=last.get("version"), name=last.get("name"),
             app=last.get("app_version"), at=last.get("completed_at"))
        for note in last.get("notes") or []:
            _say("migrate.note", out=out, note=note)
    if journal := state.get("journal"):
        _say("migrate.status.interrupted", out=out, version=journal.get("version"))
    if failure := state.get("last_failure"):
        _say("migrate.failed", out=out, version=failure.get("version"), name=failure.get("name"),
             error=failure.get("error"))
        for item in failure.get("unrestored") or []:
            _say("migrate.unrestored", out=out, item=item)
        if failure.get("backup"):
            _say("migrate.backup_kept", out=out, path=failure["backup"])
    record = state.get("integrations")
    if record:
        _say("migrate.status.integrations", out=out, status=record.get("status"),
             app=record.get("version"), exe=record.get("executable"))
        for problem in record.get("problems") or []:
            _say("migrate.integrations.problem", out=out, problem=problem)
        healthy = healthy and record.get("status") != "failed"
    else:
        _say("migrate.status.integrations_unknown", out=out)
    return 0 if healthy else 1


def summary() -> dict[str, object]:
    """Doctor's read-only view: ``{"ok": bool, "detail": str}``."""
    try:
        state = read_state()
    except _CorruptState:
        return {"ok": False, "detail": "state file unreadable — run: ai-accounts migrate"}
    except OSError as exc:
        return {"ok": False, "detail": f"state unreadable: {exc}"}
    schema = int(state.get("schema_version", 0))
    if schema > CURRENT_SCHEMA:
        return {"ok": False, "detail": f"schema {schema} is newer than this build ({CURRENT_SCHEMA})"}
    if state.get("journal"):
        return {"ok": False, "detail": "interrupted migration — run: ai-accounts migrate"}
    if state.get("last_failure"):
        return {"ok": False, "detail": "last migration failed — see: ai-accounts migrate status"}
    if schema < CURRENT_SCHEMA:
        return {"ok": False, "detail": f"schema {schema} → {CURRENT_SCHEMA} pending — run: ai-accounts migrate"}
    if (state.get("integrations") or {}).get("status") == "failed":
        return {"ok": False, "detail": f"schema {schema}; integration repair failed — run: ai-accounts migrate"}
    return {"ok": True, "detail": f"schema {schema} (current)"}


def _interactive() -> bool:
    from . import _keyreader

    return _keyreader.is_interactive_tty()


def first_run(argv: list[str]) -> None:
    """Called before every command: migrate data an external upgrade left
    behind, and repair integrations once per new version. Cheap when current
    (one small file read); never raises and never changes the exit code.

    Background and non-interactive runs (timer tick, vendor-CLI hook, a
    script piping output) migrate data only: re-registering launchd from
    inside its own job would unload the running tick, and a script expects
    no OS-level side effects. Repair then waits for the next terminal
    command, `migrate`, or `update`.
    """
    if os.environ.get(DISABLE_ENV) or (argv[:1] and argv[0] in _NO_FIRST_RUN):
        return
    try:
        background = bool(os.environ.get(BACKGROUND_ENV))
        repair = not background and _interactive() and not (argv[:1] and argv[0] in _SELF_MANAGED)
        try:
            state = read_state()
        except _CorruptState:
            state = None
        if state is not None:
            schema = int(state.get("schema_version", 0))
            if schema > CURRENT_SCHEMA:
                return  # `migrate`/`doctor` explain; every command nagging would not help
            data_due = schema < CURRENT_SCHEMA or bool(state.get("journal"))
            if not data_due and not (repair and _repair_due(state)):
                return
        timeout = _BACKGROUND_LOCK_TIMEOUT if background else LOCK_TIMEOUT
        run(repair=repair, lock_timeout=timeout, auto=True)
    except Exception as exc:  # noqa: BLE001 - the user's command must still run
        _say("migrate.first_run_failed", error=exc)


def cmd_migrate(args: list[str]) -> int:
    """``ai-accounts migrate [status | --dry-run]``."""
    if not args:
        return run()
    if args == ["status"]:
        return status()
    if args == ["--dry-run"]:
        return run(dry_run=True)
    _say("migrate.usage")
    return 2
