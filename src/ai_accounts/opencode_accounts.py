"""opencode-accounts — a view and switcher over opencode's OWN login store.

opencode v2 already keeps several logins per integration (SQLite
``opencode.db``, table ``credential``) and switches between them itself, so
this tool never copies tokens around: it reads the store strictly read-only
and delegates every change to ``opencode auth switch|logout|login``. It never
refreshes a token — OpenAI refresh tokens are single-use and shared with
codex-accounts/claude-accounts, so a refresh here could log the user out there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, NamedTuple

from . import autoswitch
from . import config_menu as cm
from . import i18n
from . import opencode_usage
from ._present import accounts_table, choose_and_run, choose_profile, format_help, ok, panel, usage_color
from ._utils import (
    BOLD,
    DIM,
    GREEN,
    RED,
    RESET,
    Spinner,
    fetch_parallel,
    has_control_chars,
    log_red,
    log_yellow,
    normalize_command_argv,
    quiet_keyboard_interrupt,
    resolve_account_dir,
)
from .usage_format import (
    UsageWindow,
    format_usage_window,
    json_empty_list,
    print_no_active_account,
    usage_window_to_json,
)

HELP = """opencode-accounts — switch between the logins opencode already stores

USAGE
  opencode-accounts who                   Show the active login of every integration
  opencode-accounts current               Alias for `who`
  opencode-accounts list [--json]         List every stored login, grouped by integration,
                                           with OpenAI/Claude/Copilot quota usage; --json
                                           prints one JSON array (Zen/Go/API-key logins:
                                           usage: null, no_quota_api: true)
  opencode-accounts usage [--json]        Same, active logins only
  opencode-accounts switch [<name>]       Make <integration>:<label> the active login
                                           (runs `opencode auth switch`); no name = picker
  opencode-accounts remove [<name>]       Log out one stored login (`opencode auth logout`)
  opencode-accounts save [<name>]         opencode stores every login itself — prints how
                                           to add one
  opencode-accounts login-switch <name>   Run `opencode auth login` to add a login
  opencode-accounts refresh [--all]       No-op: opencode refreshes its own tokens
  opencode-accounts sync                  No-op: opencode refreshes its own tokens
  opencode-accounts autoswitch            Per integration, switch away from the active
                                           login once its quota is past the threshold
  opencode-accounts config                Interactive config menu shared by every ai-accounts CLI
  opencode-accounts config get [key]      Print the shared auto-switch config (or one key)
  opencode-accounts config set <k> <v>    Set one shared config key (rejects unknown keys)
  opencode-accounts -h | --help | help    Show this help

EXAMPLES
  opencode-accounts list
  opencode-accounts switch openai:work
  opencode-accounts autoswitch

Logins are named <integration>:<label> (the credential id when the label is
empty). They are read from opencode's own store,
$XDG_DATA_HOME/opencode/opencode.db (default ~/.local/share/opencode;
override with $OPENCODE_DATA_DIR), read-only — falling back to the legacy
auth.json, which holds one login per integration and so cannot switch.
"""

_NO_REFRESH = (
    "opencode refreshes its own tokens — nothing to do. (ai-accounts never "
    "refreshes them: OpenAI refresh tokens are single-use and shared with "
    "codex-accounts/claude-accounts.)"
)


_CLI_TIMEOUT_SEC = 60
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]*")


class Login(NamedTuple):
    name: str  # "<integration>:<label>", unique
    integration: str
    cred_id: str | None  # None for a legacy auth.json login
    record: dict[str, Any] | None
    active: bool


def _data_dir() -> Path:
    override = os.environ.get("OPENCODE_DATA_DIR")
    if override:
        return Path(override)
    # ASSUMPTION: opencode resolves its data dir with xdg-basedir on every OS,
    # Windows included, so ~/.local/share is the default there too.
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "opencode"


def _account_dir() -> Path:
    # Nothing is stored here (opencode keeps the logins); defined for doctor's
    # per-provider check. Legacy == default, so nothing is ever migrated.
    default = Path.home() / ".ai-accounts" / "opencode" / "accounts"
    return resolve_account_dir("OPENCODE_ACCOUNT_DIR", default, default)


def _parse(value: object) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value) if isinstance(value, (str, bytes)) else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _db_rows(db: Path) -> list[tuple] | None:
    """The credential rows, or None when the DB has no credential table.

    Opened ``mode=ro``: the live store is a WAL database opencode is writing to.
    """
    uri = db.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'credential'"
        ).fetchone():
            return None
        return con.execute(
            "SELECT id, integration_id, label, value, active, time_created FROM credential"
            " WHERE integration_id IS NOT NULL"  # opencode skips such rows
            # MCP server OAuth tokens (e.g. Notion) share the table but are not AI logins.
            " AND integration_id NOT LIKE 'mcp\\_%' ESCAPE '\\'"
            " ORDER BY integration_id, time_created, id"
        ).fetchall()


def _logins() -> tuple[list[Login], bool]:
    """Every stored login, plus whether they came from the legacy auth.json."""
    db = _data_dir() / "opencode.db"
    rows = None
    if db.is_file():
        try:
            rows = _db_rows(db)
        except sqlite3.Error as exc:
            log_red(f"❌ Could not read {db}: {exc}")
            return [], False
    if rows is not None:
        logins: list[Login] = []
        seen: set[str] = set()
        # opencode's rule: per integration the LAST row by (active, time_created, id)
        # ascending is the active one; SQLite sorts NULL first, so NULL < 0 < 1.
        newest = {}
        for cred_id, integration, _label, _value, active, created in rows:
            key = (-1 if active is None else active, -1 if created is None else created, str(cred_id))
            if integration not in newest or key > newest[integration][0]:
                newest[integration] = (key, cred_id)
        for cred_id, integration, label, value, _active, _created in rows:
            usable = label and not has_control_chars(str(label))
            name = f"{integration}:{label if usable else cred_id}"
            # Unique even when a label equals another credential's id: a duplicate
            # name would let `remove` log out the wrong credential.
            if name in seen:
                name = f"{integration}:{cred_id}"
            base, suffix = name, 2
            while name in seen:
                name, suffix = f"{base}#{suffix}", suffix + 1
            seen.add(name)
            logins.append(Login(name, str(integration), str(cred_id), _parse(value), newest[integration][1] == cred_id))
        return logins, False
    try:
        legacy = json.loads((_data_dir() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], False
    if not isinstance(legacy, dict):
        return [], False
    return [
        Login(f"{integration}:default", integration, None, record if isinstance(record, dict) else None, True)
        for integration, record in sorted(legacy.items())
    ], True


def _has_quota_api(login: Login) -> bool:
    return (
        opencode_usage.normalize(login.integration) in opencode_usage.QUOTA_INTEGRATIONS
        and isinstance(login.record, dict)
        and login.record.get("type") == "oauth"
    )


def _fetch(login: Login) -> UsageWindow | None:
    return opencode_usage.fetch_usage(login.integration, login.record) if _has_quota_api(login) else None


def _find(name: str) -> tuple[Login | None, bool]:
    logins, legacy = _logins()
    return next((login for login in logins if login.name == name), None), legacy


def _opencode() -> str | None:
    executable = shutil.which("opencode")
    if executable is None:
        log_red("❌ The opencode CLI is required: https://opencode.ai")
    return executable


# ── who / list / usage ───────────────────────────────────────────────────────


def cmd_who() -> int:
    active = [login for login in _logins()[0] if login.active]
    if not active:
        panel("opencode Logins", [f"{RED}Not logged in{RESET}  {DIM}(run `opencode auth login`){RESET}"])
        return 1
    width = max(len(login.integration) for login in active)
    panel(
        "opencode Logins",
        [f"{DIM}{login.integration:<{width}}{RESET} : {BOLD}{login.name}{RESET}" for login in active],
    )
    return 0


def _usage_cell(window: UsageWindow | None) -> str:
    if window is None:
        return f"{DIM}—{RESET}"
    kind = "5h" if window.window_minutes is not None and window.window_minutes <= 300 else "1w"
    return format_usage_window(window, kind, f"{usage_color(window.percentage)}{window.percentage}%{RESET}")


_TABLE_COLUMNS = [
    ("PROFILE", "profile"),
    ("INTEGRATION", "integration"),
    ("TYPE", "type"),
    ("USED", "usage"),
    ("STATE", "state"),
]


def cmd_list(*, only_active: bool = False, json_output: bool = False) -> int:
    logins = _logins()[0]
    if only_active:
        logins = [login for login in logins if login.active]
    if not logins:
        if json_empty_list(json_output):
            return 0
        if only_active:
            print_no_active_account("opencode", "opencode-accounts")
        else:
            log_yellow("⚠️  No opencode logins found.")
            print(f"{DIM}   Add one with: opencode auth login{RESET}", file=sys.stderr)
        return 0
    spinner = Spinner("Fetching opencode quota…")
    with spinner:
        windows = fetch_parallel(
            logins, _fetch, spinner, "Fetching opencode quota…", labels=[login.name for login in logins]
        )

    if json_output:
        print(json.dumps([
            {
                "name": login.name,
                "integration": login.integration,
                "active": login.active,
                "usage": {"window": usage_window_to_json(window)} if _has_quota_api(login) else None,
                "no_quota_api": not _has_quota_api(login),
            }
            for login, window in zip(logins, windows)
        ]))
        return 0

    rows = [
        {
            "profile": f"{GREEN}{BOLD}{login.name}{RESET}" if login.active else login.name,
            "integration": login.integration,
            "type": str((login.record or {}).get("type") or f"{DIM}—{RESET}"),
            "usage": _usage_cell(window),
            "state": f"{GREEN}{BOLD}ACTIVE{RESET}" if login.active else f"{DIM}—{RESET}",
        }
        for login, window in zip(logins, windows)
    ]
    title = "Active opencode logins" if only_active else "opencode logins"
    print(f"{BOLD}{title}{RESET}  {DIM}({len(rows)}){RESET}")
    accounts_table(rows, _TABLE_COLUMNS, align_keys=("usage",))
    return 0


# ── switch / remove / save / login-switch ────────────────────────────────────


def _delegate(verb: str, name: str) -> int:
    """``opencode auth <verb> <integration> <credential id>`` for login *name*."""
    login, legacy = _find(name)
    if legacy:
        log_red(
            f"❌ Cannot {verb}: this opencode keeps one login per integration in the "
            "legacy auth.json. Upgrade opencode to manage several logins."
        )
        return 1
    if login is None or login.cred_id is None:
        log_red(f"❌ No opencode login named {name} (see: opencode-accounts list)")
        return 1
    # DB-controlled ids go into argv: a leading "-" would read as an option, and on
    # Windows an npm .cmd shim runs argv through cmd.exe, where & | ^ % would execute.
    if not all(_SAFE_ID.fullmatch(part) for part in (login.integration, login.cred_id)):
        log_red(f"❌ Refusing to {verb} {name}: its integration or credential id has unsafe characters")
        return 1
    executable = _opencode()
    if executable is None:
        return 1
    # stdin closed: with both arguments given opencode must never fall back to its
    # picker; the timeout keeps a wedged CLI from hanging the autoswitch timer poll.
    try:
        result = subprocess.run(
            [executable, "auth", verb, login.integration, login.cred_id],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CLI_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log_red(f"❌ opencode auth {verb} timed out after {_CLI_TIMEOUT_SEC}s for {name}")
        return 1
    if result.returncode != 0:
        # Third-party text: first line only, no escape codes, bounded.
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        detail = "".join(ch for ch in (lines[0] if lines else "") if ch.isprintable())[:300]
        log_red(f"❌ opencode auth {verb} failed for {name}" + (f": {detail}" if detail else ""))
        return 1
    return 0


def cmd_switch(name: str) -> int:
    if _delegate("switch", name) != 0:
        return 1
    ok("Switched opencode login to", name)
    return 0


def _picker_items() -> list[tuple[str, str | None]]:
    return [(login.name, "active" if login.active else None) for login in _logins()[0]]


def cmd_switch_interactive() -> int:
    items = _picker_items()
    if not items:
        log_yellow("⚠️  No opencode logins found.")
        return 1
    chosen = choose_profile("an opencode", items)
    return 1 if chosen is None else cmd_switch(chosen)


def cmd_remove(name: str) -> int:
    if _delegate("logout", name) != 0:
        return 1
    ok("Logged out opencode login", name, bold=False)
    return 0


def cmd_remove_interactive() -> int:
    items = _picker_items()
    if not items:
        log_yellow("⚠️  No opencode logins found.")
        return 1
    return choose_and_run("an opencode", items, cmd_remove, cancel_message="Remove cancelled.")


def cmd_save() -> int:
    print(
        "opencode keeps every login itself — there is nothing to save.\n"
        f"{DIM}   Add another login with: opencode auth login\n"
        f"   then pick it with:      opencode-accounts switch <integration>:<label>{RESET}"
    )
    return 0


def cmd_login_switch(name: str) -> int:
    executable = _opencode()
    if executable is None:
        return 1
    result = subprocess.run([executable, "auth", "login"])
    if result.returncode != 0:
        return result.returncode
    print(f"{DIM}opencode names its logins itself ({name!r} is not applied); the new login:{RESET}")
    return cmd_who()


def cmd_refresh() -> int:
    print(_NO_REFRESH)
    return 0


# ── autoswitch ───────────────────────────────────────────────────────────────


def _autoswitch_resume() -> bool:
    """Start a NEW opencode session continuing the last conversation."""
    executable = shutil.which("opencode")
    if executable is None:
        log_yellow("⚠️  opencode is not on PATH — cannot resume the session automatically.")
        return False
    return subprocess.run([executable, "--continue"]).returncode == 0


def _render(integration: str, outcome: autoswitch.SwitchOutcome) -> None:
    label = f"opencode {integration}"
    if outcome.reason == "no_active":
        log_yellow(f"⚠️  No active {label} login — nothing to auto-switch from.")
    elif outcome.reason == "unknown":
        log_yellow(f"⚠️  Could not determine usage for {outcome.from_profile}.")
    elif outcome.reason == "below_threshold":
        print(f"{outcome.from_profile} is at {outcome.used_pct}% used — below the switch threshold.")
    elif outcome.reason == "no_candidate":
        log_yellow(
            f"⚠️  {outcome.from_profile} is at {outcome.used_pct}% used and no other "
            f"{label} login is available to switch to."
        )
    elif outcome.reason == "switch_failed":
        log_red(f"❌ {outcome.error}")
    else:  # "switched"
        ok(f"Auto-switched {label} login", f"{outcome.from_profile} → {outcome.to_profile}")


def _expired_spares_line(group: dict[str, Login], active: str | None) -> str | None:
    names = [n for n, login in group.items() if n != active and _has_quota_api(login) and opencode_usage.is_expired(login.record)]
    if not names:
        return None
    return (
        f"⚠️  Spare login(s) {', '.join(names)} have an expired token — open opencode with them once "
        "(`opencode-accounts switch <name>`) so opencode refreshes it, then autoswitch can consider them."
    )


def _probe(login: Login) -> UsageWindow | None:
    # An expired access token is rejected anyway: skip the call. Never refreshed here.
    return None if opencode_usage.is_expired(login.record) else _fetch(login)


def cmd_autoswitch() -> int:
    logins = _logins()[0]
    if not logins:
        log_yellow("⚠️  No opencode logins found — nothing to auto-switch.")
        return 0
    # `opencode --continue` blocks until that session ends, so the engine gets no
    # restart hook: every integration switches first, then the resume runs once.
    restart = autoswitch.build_restart("opencode", _autoswitch_resume, interactive=sys.stdout.isatty())
    switched: list[autoswitch.SwitchOutcome] = []
    failed = False
    for integration in opencode_usage.QUOTA_INTEGRATIONS:
        group = {
            login.name: login for login in logins if opencode_usage.normalize(login.integration) == integration
        }
        if not group:
            continue
        active = next((login.name for login in group.values() if login.active), None)
        outcome = autoswitch.run_autoswitch(
            # The integration in the label keeps notify_once's "<provider>:no-candidate"
            # keys and notification titles apart per integration.
            f"opencode/{integration}",
            list(group),
            active,
            lambda name, group=group: _probe(group[name]),  # read-only, access token only
            lambda name: cmd_switch(name) == 0,
        )
        if outcome.reason == "disabled":  # global setting: same answer for every integration
            log_yellow("⚠️  Auto-switch is disabled (see ~/.ai-accounts/config.json: enabled).")
            break
        _render(integration, outcome)
        if outcome.reason == "unknown" and (cur := group.get(active or "")) and _has_quota_api(cur) and opencode_usage.is_expired(cur.record):
            log_yellow(f"   {active} has an expired token, so its usage was not looked up — open opencode with it once.")
        if outcome.reason in ("no_candidate", "unknown") and (line := _expired_spares_line(group, active)):
            log_yellow(line)
        failed |= outcome.reason == "switch_failed"
        if outcome.switched:
            switched.append(outcome)
    resumed = bool(switched) and restart is not None and autoswitch._restarted(restart)  # pyright: ignore[reportPrivateUsage]  # the engine's never-raise wrapper
    for outcome in switched:
        if not resumed:  # no resume ran (no TTY, or it failed): tell the user
            log_yellow(
                autoswitch.manual_restart_message(
                    "opencode", outcome.from_profile or "?", outcome.to_profile or "?"
                )
            )
    return 1 if failed else 0


@quiet_keyboard_interrupt
def main(argv: list[str] | None = None) -> int:
    argv = normalize_command_argv(list(sys.argv[1:] if argv is None else argv))
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(format_help(i18n.t("help.opencode", default=HELP)))
        return 0
    command, *rest = argv
    if command == "config":
        return cm.cmd_config(rest, prog="opencode-accounts")
    if command in ("who", "current"):
        return cmd_who()
    if command == "save":
        return cmd_save()
    if command == "list":
        return cmd_list(json_output="--json" in rest)
    if command == "usage":
        return cmd_list(only_active=True, json_output="--json" in rest)
    if command == "switch":
        return cmd_switch(rest[0]) if rest else cmd_switch_interactive()
    if command == "remove":
        return cmd_remove(rest[0]) if rest else cmd_remove_interactive()
    if command in ("refresh", "sync"):
        return cmd_refresh()
    if command == "autoswitch":
        return cmd_autoswitch()
    if command == "login-switch" and rest:
        return cmd_login_switch(rest[0])
    log_red(f"❌ Unknown or incomplete command: {command}")
    print(format_help(i18n.t("help.opencode", default=HELP)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
