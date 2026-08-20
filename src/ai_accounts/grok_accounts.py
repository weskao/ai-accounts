from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config_menu as cm
from ._present import accounts_table, choose_and_run, choose_profile, ok, panel, success_panel
from ._utils import (
    BOLD,
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    email_local_part,
    log_red,
    log_yellow,
    resolve_account_dir,
)
from .usage_format import print_no_active_account

JsonDict = dict[str, Any]

HELP = """grok-accounts — manage multiple Grok Build CLI login profiles

USAGE
  grok-accounts who                   Show the current logged-in Grok account
  grok-accounts current               Alias for `who`
  grok-accounts save [<name>]         Save the current login; no name = derive from email
  grok-accounts list                  List saved profiles
  grok-accounts usage                 Show only the active account (session & expiry)
  grok-accounts switch [<name>]       Switch by name; no name = interactive picker
  grok-accounts remove [<name>]       Delete a saved profile; no name = interactive picker
  grok-accounts refresh [<name>]      Let Grok refresh the active/profile session
  grok-accounts refresh --all         Refresh every saved profile through Grok
  grok-accounts sync                  Copy the active auth back to its matching profile
  grok-accounts autoswitch            Report that grok has no quota API to switch on
  grok-accounts login-switch <name>   Fresh Grok OAuth login + save as <name>
  grok-accounts config                Interactive config menu shared by every ai-accounts CLI
  grok-accounts config get [key]      Print the shared auto-switch config (or one key)
  grok-accounts config set <k> <v>    Set one shared config key (rejects unknown keys)
  grok-accounts -h | --help | help    Show this help

EXAMPLES
  grok-accounts login-switch personal
  grok-accounts login-switch work
  grok-accounts list
  grok-accounts switch
  grok-accounts switch personal
  grok-accounts refresh --all
  grok-accounts who

MODEL
  grok-4.5 (flagship, 500k context) — agentic tool calling, minimal
  hallucinations, configurable reasoning; xAI's pick for code and everything
  else. API: $2.00 / 1M input tokens, $6.00 / 1M output tokens.
  Consumer plans: Free ($0/mo), SuperGrok ($30/mo, unlocks Grok 4.5 + higher
  limits). Grok Build CLI docs: docs.x.ai/build/

Profiles live under ~/.ai-accounts/grok/accounts/<name>.json (override with
$GROK_ACCOUNT_DIR). Treat that directory as secrets — profiles contain OAuth
tokens. `refresh` runs `grok models`, allowing the official CLI to refresh and
rotate credentials instead of ai-accounts calling a private OAuth endpoint.
"""


def _grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok")))


def _auth_file() -> Path:
    return Path(os.environ.get("GROK_AUTH_JSON", str(_grok_home() / "auth.json")))


def _account_dir() -> Path:
    return resolve_account_dir(
        "GROK_ACCOUNT_DIR",
        Path.home() / ".ai-accounts" / "grok" / "accounts",
        _grok_home() / "accounts",
    )


def _profile_file(name: str) -> Path | None:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not safe:
        log_red("❌ Profile name cannot be empty")
        return None
    return _account_dir() / f"{safe}.json"


def _marker_file() -> Path:
    return _account_dir() / ".current-profile"


def _backup_dir() -> Path:
    return _account_dir().parent / "backups"


def _read_json(path: Path) -> JsonDict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value else None


def _write_json(path: Path, payload: JsonDict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
        return True
    except OSError as exc:
        log_red(f"❌ Could not write {path}: {exc}")
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return False


def _set_marker(profile: Path) -> None:
    marker = _marker_file()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(profile.stem, encoding="utf-8")
    marker.chmod(0o600)


def _record(payload: JsonDict) -> JsonDict | None:
    for value in payload.values():
        if isinstance(value, dict) and (
            value.get("auth_mode") == "oauth"
            or "refresh_token" in value
            or "email" in value
        ):
            return value
    return None


def _claims(payload: JsonDict | None) -> JsonDict:
    record = _record(payload or {})
    if record is None:
        return {}
    return {
        "email": str(record.get("email") or "—"),
        "name": str(record.get("first_name") or "").strip(),
        "principal_id": str(record.get("principal_id") or record.get("user_id") or "—"),
        "principal_type": str(record.get("principal_type") or "—"),
        "team_id": str(record.get("team_id") or "—"),
        "created_at": str(record.get("create_time") or ""),
        "expires_at": str(record.get("expires_at") or ""),
        "auth_mode": str(record.get("auth_mode") or "—").upper(),
        "refreshable": bool(record.get("refresh_token")),
        "retention_opt_out": record.get("coding_data_retention_opt_out"),
    }


def _identity(payload: JsonDict | None) -> tuple[str, str, str]:
    claims = _claims(payload)
    return (
        str(claims.get("principal_id", "")),
        str(claims.get("email", "")),
        str(claims.get("team_id", "")),
    )


def _active_profile(active: JsonDict | None = None) -> Path | None:
    active = active if active is not None else _read_json(_auth_file())
    if active is None:
        return None
    marker = _marker_file()
    try:
        marked = _profile_file(marker.read_text(encoding="utf-8").strip())
    except OSError:
        marked = None
    if marked is not None and _identity(_read_json(marked)) == _identity(active):
        return marked
    identity = _identity(active)
    if "—" in identity or not any(identity):
        return None
    matches = [
        path
        for path in _account_dir().glob("*.json")
        if _identity(_read_json(path)) == identity
    ]
    return matches[0] if len(matches) == 1 else None


def _expiry_status(value: str) -> str:
    if not value:
        return "—"
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:19]
    seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if seconds < 0:
        return "EXPIRED"
    if seconds < 60 * 60:
        return "<1h"
    if seconds < 24 * 60 * 60:
        hours, remainder = divmod(int(seconds), 60 * 60)
        return f"{hours}h {remainder // 60:02d}m"
    return when.astimezone().strftime("%b %d %H:%M")


def _timestamp(value: str) -> str:
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%b %d %H:%M")
        )
    except ValueError:
        return "—"


def _short_id(value: object) -> str:
    if not value or value == "—":
        return f"{DIM}—{RESET}"
    text = str(value)
    return f"{text[:8]}…{text[-4:]}" if len(text) > 16 else text


def _retention(value: object) -> str:
    return "opt-out" if value is True else "standard" if value is False else "—"


def _identity_label(claims: JsonDict) -> str | None:
    """DIM sublabel for the interactive picker — None when the profile has no
    readable email (matches choose_profile's "sublabel None is acceptable")."""
    email = claims.get("email")
    if not email or email == "—":
        return None
    name = claims.get("name")
    return f"{name} <{email}>" if name else email


def _claims_lines(claims: JsonDict, profile: Path | None) -> list[str]:
    if not claims:
        return [f"{YELLOW}No readable account claims found.{RESET}"]
    name = claims["name"]
    account = f"{name} <{claims['email']}>" if name else claims["email"]
    session = f"{GREEN}refreshable{RESET}" if claims["refreshable"] else "browser login"
    return [
        f"{BOLD}Account{RESET}       : {account}",
        f"{DIM}Principal{RESET}     : {claims['principal_id']}",
        f"{DIM}Team{RESET}          : {claims['team_id']}",
        f"{DIM}Session{RESET}       : {session}",
        f"{DIM}Expires{RESET}       : {_expiry_status(claims['expires_at'])}",
        f"{DIM}Profile{RESET}       : {profile.stem if profile else 'untracked'}",
    ]


def cmd_who() -> int:
    payload = _read_json(_auth_file())
    claims = _claims(payload) if payload else {}
    profile = _active_profile(payload) if payload else None

    if claims:
        status_lines = [
            f"{GREEN}Logged in through Grok Build CLI{RESET}",
            f"{DIM}Active account{RESET}: {claims.get('email') or '—'}",
        ]
    else:
        status_lines = [
            f"{RED}Not logged in{RESET}  {DIM}(run `grok login --oauth`){RESET}"
        ]
    panel("Grok Login Status", status_lines)

    print()
    panel("Current Auth Claims", _claims_lines(claims, profile))
    return 0 if claims else 1


def cmd_save(name: str | None = None) -> int:
    payload = _read_json(_auth_file())
    claims = _claims(payload)
    if name is None:
        email = claims.get("email")
        if not email or email == "—":
            log_red("❌ No valid Grok OAuth login found. Run: grok login --oauth")
            return 1
        name = email_local_part(str(email))
    profile = _profile_file(name)
    if profile is None or payload is None or not claims:
        log_red("❌ No valid Grok OAuth login found. Run: grok login --oauth")
        return 1
    if not _write_json(profile, payload):
        return 1
    _set_marker(profile)
    success_panel(
        "Saved Grok profile",
        profile.stem,
        _claims_lines(claims, profile),
        title=f"Profile: {profile.stem}",
        details=(f"→ {profile}",),
    )
    return 0


_TABLE_COLUMNS = [
    ("PROFILE", "profile"),
    ("ACCOUNT", "account"),
    ("TYPE", "type"),
    ("ID", "id"),
    ("TEAM", "team"),
    ("CREATED", "created"),
    ("EXPIRES", "expires"),
    ("DATA", "data"),
    ("SESSION", "session"),
    ("STATE", "state"),
]


def cmd_list(*, only_active: bool = False) -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Grok profiles.")
        print(
            f"{DIM}   Add one with: grok-accounts save <profile_name>{RESET}",
            file=sys.stderr,
        )
        return 0
    active = _active_profile()
    if only_active:
        if active is None:
            print_no_active_account("Grok", "grok-accounts")
            return 0
        profiles = [active]
    rows = []
    for path in profiles:
        claims = _claims(_read_json(path))
        account = claims.get("email", "unreadable")
        if claims.get("name"):
            account = f"{claims['name']} <{account}>"
        is_active = path == active
        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{path.stem}{RESET}" if is_active else path.stem,
                "account": account,
                "type": claims.get("principal_type", "—"),
                "id": _short_id(claims.get("principal_id")),
                "team": _short_id(claims.get("team_id")),
                "created": _timestamp(str(claims.get("created_at", ""))),
                "expires": _expiry_status(str(claims.get("expires_at", ""))),
                "data": _retention(claims.get("retention_opt_out")),
                "session": f"{claims.get('auth_mode', '—')} · {'refresh' if claims['refreshable'] else 'browser'}",
                "state": f"{GREEN}{BOLD}ACTIVE{RESET}" if is_active else f"{DIM}—{RESET}",
            }
        )

    if only_active:
        print(f"{BOLD}Current Grok account{RESET}")
    else:
        print(f"{BOLD}Saved Grok profiles{RESET}  {DIM}({len(rows)}){RESET}")
    accounts_table(rows, _TABLE_COLUMNS)
    return 0


def _backup_active() -> bool:
    active = _read_json(_auth_file())
    if active is None:
        return True
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _write_json(_backup_dir() / f"auth.backup-{stamp}.json", active)


def cmd_switch(name: str) -> int:
    profile = _profile_file(name)
    payload = _read_json(profile) if profile is not None else None
    if profile is None or payload is None or not _claims(payload):
        log_red(f"❌ Profile is unreadable or missing: {name}")
        return 1
    if not _backup_active():
        return 1
    if not _write_json(_auth_file(), payload):
        return 1
    _set_marker(profile)
    ok("Switched Grok profile to", profile.stem)
    print(f"{DIM}   Grok Build CLI will use this account on its next launch.{RESET}")
    print()
    return cmd_who()


def cmd_switch_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Grok profiles.")
        return 1
    items = [
        (profile.stem, _identity_label(_claims(_read_json(profile))))
        for profile in profiles
    ]
    chosen = choose_profile("a Grok", items)
    if chosen is None:
        return 1
    return cmd_switch(chosen)


def cmd_remove(name: str) -> int:
    profile = _profile_file(name)
    if profile is None or not profile.is_file():
        log_red(f"❌ Profile not found: {name}")
        return 1
    try:
        profile.unlink()
    except OSError as exc:
        log_red(f"❌ Could not remove profile: {exc}")
        return 1
    if _active_profile() is None:
        _marker_file().unlink(missing_ok=True)
    ok("Removed Grok profile", profile.stem, bold=False)
    return 0


def cmd_remove_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Grok profiles.")
        return 1
    items = [
        (profile.stem, _identity_label(_claims(_read_json(profile))))
        for profile in profiles
    ]
    return choose_and_run("a Grok", items, cmd_remove, cancel_message="Remove cancelled.")


def _run_grok_refresh() -> int:
    executable = shutil.which("grok")
    if executable is None:
        log_red(
            "❌ Grok Build CLI is required. Install it: curl -fsSL https://x.ai/cli/install.sh | bash"
        )
        return 1
    result = subprocess.run([executable, "models"], capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        log_red(f"❌ Grok could not refresh this session{f': {detail}' if detail else ''}")
        return result.returncode
    return 0


def _refresh_profile(profile: Path) -> int:
    original = _read_json(_auth_file())
    payload = _read_json(profile)
    if payload is None:
        log_red(f"❌ Profile is unreadable: {profile.stem}")
        return 1
    try:
        if not _write_json(_auth_file(), payload):
            return 1
        status = _run_grok_refresh()
        refreshed = _read_json(_auth_file())
        if (
            status == 0
            and refreshed is not None
            and not _write_json(profile, refreshed)
        ):
            return 1
        return status
    finally:
        if original is not None:
            _write_json(_auth_file(), original)


def _refresh_one_profile(name: str, *, show_summary: bool = True) -> int:
    profile = _profile_file(name)
    if profile is None:
        return 1
    if not profile.is_file():
        log_red(f"❌ Profile not found: {name}")
        return 1

    status = _refresh_profile(profile)
    if status == 0 and show_summary:
        success_panel(
            "Refreshed Grok profile",
            name,
            _claims_lines(_claims(_read_json(profile)), profile),
            title=f"Profile: {name}",
        )
    return status


def cmd_refresh(target: str | None) -> int:
    if target == "--all":
        profiles = (
            sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
        )
        if not profiles:
            log_yellow("⚠️  No saved Grok profiles.")
            return 0
        failed = []
        for profile in profiles:
            if _refresh_one_profile(profile.stem) != 0:
                failed.append(profile.stem)
        cmd_list()
        if failed:
            log_red(f"❌ Grok refresh failed: {', '.join(failed)}")
            return 1
        ok(f"All {len(profiles)} profile(s) refreshed.")
        return 0
    if target:
        return _refresh_one_profile(target)
    if _read_json(_auth_file()) is None:
        log_red("❌ No Grok login found. Run: grok login --oauth")
        return 1
    status = _run_grok_refresh()
    profile = _active_profile()
    refreshed = _read_json(_auth_file())
    if status != 0:
        return status
    if profile is not None and refreshed is not None:
        _write_json(profile, refreshed)
        details = (f"(synced back to profile: {profile.stem})",)
    else:
        log_yellow("⚠️  No unambiguous current profile — run: grok-accounts switch <name>")
        details = ()
    success_panel(
        "Refreshed active Grok auth.",
        None,
        _claims_lines(_claims(refreshed), profile),
        title="Current Auth Claims",
        details=details,
    )
    return status


def cmd_sync() -> int:
    payload = _read_json(_auth_file())
    profile = _active_profile(payload)
    if payload is None or profile is None:
        log_yellow("⚠️  No unambiguous current profile — run: grok-accounts switch <name>")
        return 1
    if not _write_json(profile, payload):
        return 1
    _set_marker(profile)
    success_panel(
        "Synced active auth → profile",
        profile.stem,
        _claims_lines(_claims(payload), profile),
        title=f"Profile: {profile.stem}",
    )
    return 0


def cmd_login_switch(name: str) -> int:
    executable = shutil.which("grok")
    if executable is None:
        log_red(
            "❌ Grok Build CLI is required. Install it: curl -fsSL https://x.ai/cli/install.sh | bash"
        )
        return 1
    subprocess.run([executable, "logout"])
    result = subprocess.run([executable, "login", "--oauth"])
    return cmd_save(name) if result.returncode == 0 else result.returncode


def cmd_autoswitch() -> int:
    """Say plainly that grok cannot participate in auto-switching.

    xAI ships no quota endpoint and the Grok Build CLI exposes none, so there
    is no usage figure to compare against a threshold — nothing to guess at,
    and nothing worth faking. Exits 0 so the `ai-accounts autoswitch` fan-out
    over all five providers is not reported as a failure over this one.
    """
    print("autoswitch unsupported for grok: no quota API")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    command, *rest = argv
    if command == "config":
        return cm.cmd_config(rest, prog="grok-accounts")
    if command in ("who", "current"):
        return cmd_who()
    if command == "save":
        return cmd_save(rest[0] if rest else None)
    if command == "list":
        return cmd_list()
    if command == "usage":
        return cmd_list(only_active=True)
    if command == "switch":
        return cmd_switch(rest[0]) if rest else cmd_switch_interactive()
    if command == "remove":
        return cmd_remove(rest[0]) if rest else cmd_remove_interactive()
    if command == "refresh":
        return cmd_refresh(rest[0] if rest else None)
    if command == "sync":
        return cmd_sync()
    if command == "autoswitch":
        return cmd_autoswitch()
    if command == "login-switch" and rest:
        return cmd_login_switch(rest[0])
    log_red(f"❌ Unknown or incomplete command: {command}")
    print(HELP)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
