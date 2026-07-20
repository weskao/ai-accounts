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

from ._utils import DIM, GREEN, RESET, log_red, log_yellow, resolve_account_dir

BOLD = "\033[1m"
CYAN = "\033[1;36m"
JsonDict = dict[str, Any]

HELP = """grok-accounts — manage multiple Grok Build CLI login profiles

USAGE
  grok-accounts who                   Show the current logged-in Grok account
  grok-accounts current               Alias for `who`
  grok-accounts save <name>           Save the current login as a reusable profile
  grok-accounts list                  List saved profiles
  grok-accounts switch [<name>]       Switch by name; no name = interactive picker
  grok-accounts remove <name>         Delete a saved profile
  grok-accounts refresh [<name>]      Let Grok refresh the active/profile session
  grok-accounts refresh --all         Refresh every saved profile through Grok
  grok-accounts sync                  Copy the active auth back to its matching profile
  grok-accounts login-switch <name>   Fresh Grok OAuth login + save as <name>
  grok-accounts -h | --help           Show this help

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
        log_red("Profile name cannot be empty")
        return None
    return _account_dir() / f"{safe}.json"


def _marker_file() -> Path:
    return _account_dir() / ".current-profile"


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
        log_red(f"Could not write {path}: {exc}")
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
    text = str(value or "—")
    return f"{text[:8]}…{text[-4:]}" if len(text) > 16 else text


def _retention(value: object) -> str:
    return "opt-out" if value is True else "standard" if value is False else "—"


def _panel(title: str, lines: list[tuple[str, str]]) -> None:
    width = max(
        [len(title) + 4, *(len(label) + len(value) + 5 for label, value in lines)]
    )
    print(f"{CYAN}┌{'─' * width}┐{RESET}")
    print(
        f"{CYAN}│{RESET} {BOLD}{title}{RESET}{' ' * (width - len(title) - 1)}{CYAN}│{RESET}"
    )
    print(f"{CYAN}├{'─' * width}┤{RESET}")
    for label, value in lines:
        text = f" {label}: {value}"
        print(f"{CYAN}│{RESET}{text}{' ' * (width - len(text))}{CYAN}│{RESET}")
    print(f"{CYAN}└{'─' * width}┘{RESET}")


def cmd_who() -> int:
    payload = _read_json(_auth_file())
    if payload is None:
        log_yellow("No Grok login found. Run: grok login --oauth")
        return 1
    claims = _claims(payload)
    if not claims:
        log_red(f"Grok auth file has no recognized OAuth entry: {_auth_file()}")
        return 1
    profile = _active_profile(payload)
    name = claims["name"]
    account = f"{name} <{claims['email']}>" if name else claims["email"]
    _panel(
        "Current Grok Auth",
        [
            ("Account", account),
            ("Principal", claims["principal_id"]),
            ("Team", claims["team_id"]),
            ("Session", "refreshable" if claims["refreshable"] else "browser login"),
            ("Expires", _expiry_status(claims["expires_at"])),
            ("Profile", profile.stem if profile else "untracked"),
        ],
    )
    return 0


def cmd_save(name: str) -> int:
    profile = _profile_file(name)
    payload = _read_json(_auth_file())
    if profile is None or payload is None or not _claims(payload):
        log_red("No valid Grok OAuth login found. Run: grok login --oauth")
        return 1
    if not _write_json(profile, payload):
        return 1
    _set_marker(profile)
    print(f"{GREEN}Saved Grok profile:{RESET} {BOLD}{profile.stem}{RESET}")
    return 0


def cmd_list() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("No saved Grok profiles.")
        print(
            f"{DIM}   Add one with: grok-accounts save <profile_name>{RESET}",
            file=sys.stderr,
        )
        return 0
    active = _active_profile()
    rows = []
    for path in profiles:
        claims = _claims(_read_json(path))
        account = claims.get("email", "unreadable")
        if claims.get("name"):
            account = f"{claims['name']} <{account}>"
        rows.append(
            (
                path.stem,
                account,
                claims.get("principal_type", "—"),
                _short_id(claims.get("principal_id")),
                _short_id(claims.get("team_id")),
                _timestamp(str(claims.get("created_at", ""))),
                _expiry_status(str(claims.get("expires_at", ""))),
                f"{claims.get('auth_mode', '—')} · {'refresh' if claims['refreshable'] else 'browser'}",
                _retention(claims.get("retention_opt_out")),
                "ACTIVE" if path == active else "—",
            )
        )
    headers = (
        "PROFILE",
        "ACCOUNT",
        "TYPE",
        "ID",
        "TEAM",
        "CREATED",
        "EXPIRES",
        "SESSION",
        "DATA",
        "STATE",
    )
    widths = [
        max(len(headers[index]), *(len(str(row[index])) for row in rows))
        for index in range(len(headers))
    ]
    border = "┼".join("─" * (width + 2) for width in widths)
    print(f"Saved Grok profiles  ({len(rows)})")
    print(f"┌{border.replace('┼', '┬')}┐")
    print(
        "│"
        + "│".join(f" {headers[i]:<{widths[i]}} " for i in range(len(headers)))
        + "│"
    )
    print(f"├{border}┤")
    for row in rows:
        print(
            "│"
            + "│".join(f" {str(row[i]):<{widths[i]}} " for i in range(len(headers)))
            + "│"
        )
    print(f"└{border.replace('┼', '┴')}┘")
    return 0


def _backup_active() -> bool:
    active = _read_json(_auth_file())
    if active is None:
        return True
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _write_json(_auth_file().with_name(f"auth.backup-{stamp}.json"), active)


def cmd_switch(name: str) -> int:
    profile = _profile_file(name)
    payload = _read_json(profile) if profile is not None else None
    if profile is None or payload is None or not _claims(payload):
        log_red(f"Profile is unreadable or missing: {name}")
        return 1
    if not _backup_active():
        return 1
    if not _write_json(_auth_file(), payload):
        return 1
    _set_marker(profile)
    print(f"{GREEN}Switched Grok profile:{RESET} {BOLD}{profile.stem}{RESET}")
    return cmd_who()


def cmd_switch_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("No saved Grok profiles.")
        return 1
    for index, profile in enumerate(profiles, start=1):
        print(f"  {index}. {profile.stem}")
    try:
        selected = input("Choose Grok profile: ").strip()
        index = int(selected) - 1
    except (EOFError, ValueError):
        log_red("Invalid selection")
        return 1
    return cmd_switch(profiles[index].stem) if 0 <= index < len(profiles) else 1


def cmd_remove(name: str) -> int:
    profile = _profile_file(name)
    if profile is None or not profile.is_file():
        log_red(f"Profile not found: {name}")
        return 1
    try:
        profile.unlink()
    except OSError as exc:
        log_red(f"Could not remove profile: {exc}")
        return 1
    if _active_profile() is None:
        _marker_file().unlink(missing_ok=True)
    print(f"Removed Grok profile: {profile.stem}")
    return 0


def _run_grok_refresh() -> int:
    executable = shutil.which("grok")
    if executable is None:
        log_red(
            "Grok Build CLI is required. Install it: curl -fsSL https://x.ai/cli/install.sh | bash"
        )
        return 1
    result = subprocess.run([executable, "models"], capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        log_red(f"Grok could not refresh this session{f': {detail}' if detail else ''}")
        return result.returncode
    return 0


def _refresh_profile(profile: Path) -> int:
    original = _read_json(_auth_file())
    payload = _read_json(profile)
    if payload is None:
        log_red(f"Profile is unreadable: {profile.stem}")
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


def cmd_refresh(target: str | None) -> int:
    if target == "--all":
        profiles = (
            sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
        )
        if not profiles:
            log_yellow("No saved Grok profiles.")
            return 0
        status = 0
        for profile in profiles:
            print(f"Refreshing Grok profile: {profile.stem}")
            status = _refresh_profile(profile) or status
        return status
    if target:
        profile = _profile_file(target)
        return (
            _refresh_profile(profile)
            if profile is not None and profile.is_file()
            else 1
        )
    if _read_json(_auth_file()) is None:
        log_red("No Grok login found. Run: grok login --oauth")
        return 1
    status = _run_grok_refresh()
    profile = _active_profile()
    refreshed = _read_json(_auth_file())
    if status == 0 and profile is not None and refreshed is not None:
        _write_json(profile, refreshed)
        print(
            f"{GREEN}Refreshed and synced Grok profile:{RESET} {BOLD}{profile.stem}{RESET}"
        )
    return status


def cmd_sync() -> int:
    payload = _read_json(_auth_file())
    profile = _active_profile(payload)
    if payload is None or profile is None:
        log_yellow("No unambiguous current profile — run: grok-accounts switch <name>")
        return 1
    if not _write_json(profile, payload):
        return 1
    _set_marker(profile)
    print(f"{GREEN}Synced active auth → profile:{RESET} {BOLD}{profile.stem}{RESET}")
    return 0


def cmd_login_switch(name: str) -> int:
    executable = shutil.which("grok")
    if executable is None:
        log_red(
            "Grok Build CLI is required. Install it: curl -fsSL https://x.ai/cli/install.sh | bash"
        )
        return 1
    subprocess.run([executable, "logout"])
    result = subprocess.run([executable, "login", "--oauth"])
    return cmd_save(name) if result.returncode == 0 else result.returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(HELP)
        return 0
    command, *rest = argv
    if command in ("who", "current"):
        return cmd_who()
    if command == "save" and rest:
        return cmd_save(rest[0])
    if command == "list":
        return cmd_list()
    if command == "switch":
        return cmd_switch(rest[0]) if rest else cmd_switch_interactive()
    if command == "remove" and rest:
        return cmd_remove(rest[0])
    if command == "refresh":
        return cmd_refresh(rest[0] if rest else None)
    if command == "sync":
        return cmd_sync()
    if command == "login-switch" and rest:
        return cmd_login_switch(rest[0])
    log_red(f"Unknown or incomplete command: {command}")
    print(HELP)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
