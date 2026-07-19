from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from . import gemini_usage
from ._utils import (
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
    ensure_tool,
    log_red,
    log_yellow,
)
from .codex_usage import UsageWindow, format_usage_window

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonDict: TypeAlias = dict[str, JsonValue]
Claims: TypeAlias = dict[str, str | int | bool | None]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


BOLD = "\033[1m"
CYAN = "\033[1;36m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

HELP = """agy-accounts — manage multiple Antigravity OAuth profiles

USAGE
  agy-accounts who                   Show the selected Antigravity account
  agy-accounts current               Alias for `who`
  agy-accounts save <name>           Save the current login as a reusable profile
  agy-accounts list                  List saved profiles (table view)
  agy-accounts switch [<name>]       Switch by name; no name = interactive picker
  agy-accounts remove <name>         Delete a saved profile
  agy-accounts refresh [<name>]      Let agy refresh a session and its quota;
                                     no name = refresh active session + sync it back
  agy-accounts refresh --all         Refresh every saved profile
  agy-accounts sync                  Copy the active auth back to its matching profile
  agy-accounts login-switch <name>   Antigravity Google login + save as <name>
  agy-accounts -h | --help           Show this help

EXAMPLES
  agy-accounts login-switch personal
  agy-accounts login-switch work
  agy-accounts list
  agy-accounts switch
  agy-accounts switch personal
  agy-accounts refresh --all
  agy-accounts who

Profiles live under ~/.codexbar/antigravity/accounts/<name>.json.
Treat that directory as secrets — saved profiles contain Google OAuth tokens.
"""


# ── paths ─────────────────────────────────────────────────────────────────
def _antigravity_dir() -> Path:
    return Path(
        os.environ.get(
            "ANTIGRAVITY_HOME", str(Path.home() / ".codexbar" / "antigravity")
        )
    )


def _account_dir() -> Path:
    return Path(
        os.environ.get("ANTIGRAVITY_ACCOUNT_DIR", str(_antigravity_dir() / "accounts"))
    )


def _auth_file() -> Path:
    return Path(
        os.environ.get(
            "ANTIGRAVITY_OAUTH_JSON", str(_antigravity_dir() / "oauth_creds.json")
        )
    )


_KEYCHAIN_SERVICE = "gemini"
_KEYCHAIN_ACCOUNT = "antigravity"
_KEYRING_PREFIX = "go-keyring-base64:"


def _read_cli_keyring_secret() -> str | None:
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-s",
            _KEYCHAIN_SERVICE,
            "-a",
            _KEYCHAIN_ACCOUNT,
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _auth_from_keyring_secret(secret: str) -> JsonDict | None:
    if not secret.startswith(_KEYRING_PREFIX):
        return None
    try:
        payload = json.loads(base64.b64decode(secret.removeprefix(_KEYRING_PREFIX)))
    except (ValueError, UnicodeDecodeError):
        return None
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, dict):
        return None
    auth = dict(token)
    expiry = auth.pop("expiry", None)
    if isinstance(expiry, str):
        try:
            auth["expiry_date"] = int(
                datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
                * 1000
            )
        except ValueError:
            pass
    auth["auth_method"] = payload.get("auth_method", "consumer")
    return auth


def _keyring_secret_from_auth(auth: JsonDict) -> str | None:
    access_token = _string(auth.get("access_token"))
    refresh_token = _string(auth.get("refresh_token"))
    if not access_token or not refresh_token:
        return None
    token: JsonDict = {
        "access_token": access_token,
        "token_type": _string(auth.get("token_type")) or "Bearer",
        "refresh_token": refresh_token,
    }
    expiry_ms = auth.get("expiry_date")
    if isinstance(expiry_ms, int | float):
        token["expiry"] = datetime.fromtimestamp(
            expiry_ms / 1000
        ).astimezone().isoformat()
    payload = {
        "token": token,
        "auth_method": _string(auth.get("auth_method")) or "consumer",
    }
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return _KEYRING_PREFIX + encoded.decode("ascii")


def _store_keychain_secret(secret: str) -> bool:
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            _KEYCHAIN_SERVICE,
            "-a",
            _KEYCHAIN_ACCOUNT,
            "-w",
            secret,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _write_cli_auth_text(auth_text: str) -> bool:
    try:
        auth = json.loads(auth_text)
    except ValueError:
        return False
    if not isinstance(auth, dict):
        return False
    secret = _keyring_secret_from_auth(auth)
    if secret is None or not _store_keychain_secret(secret):
        return False
    auth_path = _auth_file()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(json.dumps(auth, indent=2) + "\n", encoding="utf-8")
    auth_path.chmod(0o600)
    return True


def _delete_cli_auth() -> bool:
    result = subprocess.run(
        [
            "security",
            "delete-generic-password",
            "-s",
            _KEYCHAIN_SERVICE,
            "-a",
            _KEYCHAIN_ACCOUNT,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _restore_cli_auth(auth_text: str | None) -> None:
    if auth_text is None:
        _delete_cli_auth()
    else:
        _write_cli_auth_text(auth_text)


def _profile_file(name: str) -> Path | None:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not safe:
        log_red("❌ Profile name cannot be empty")
        return None
    return _account_dir() / f"{safe}.json"


def _current_profile_marker() -> Path:
    return _account_dir() / ".current-profile"


def _marked_profile() -> Path | None:
    marker = _current_profile_marker()
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or re.sub(r"[^a-zA-Z0-9._-]", "_", name) != name:
        return None
    profile = _account_dir() / f"{name}.json"
    return profile if profile.is_file() else None


def _set_current_profile(profile: Path) -> None:
    marker = _current_profile_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(profile.stem, encoding="utf-8")
    marker.chmod(0o600)


# ── JWT claim decoding (no raw tokens ever printed) ─────────────────────────


def _decode_jwt_payload(token: str | None) -> JsonDict | None:
    if not token or "." not in token:
        return None
    try:
        payload = token.split(".")[1]
        padded = payload.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        result = json.loads(base64.b64decode(padded).decode("utf-8"))
        return result if isinstance(result, dict) else None
    except Exception:
        return None


def _find_deep(obj: JsonValue, keys: tuple[str, ...]) -> str | None:
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    for value in obj.values():
        found = _find_deep(value, keys)
        if found:
            return found
    return None


def _format_unix_time(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _claims_from_auth(auth: JsonDict) -> Claims:
    """Extract non-secret account claims from parsed Antigravity credentials.

    Identity comes from the ``id_token`` JWT (email / name / Google ``sub``).
    Token expiry comes from ``expiry_date`` (epoch **milliseconds**, Google's
    convention), falling back to the id_token's ``exp`` (seconds)."""
    claims = _decode_jwt_payload(_string(auth.get("id_token"))) or {}

    exp = None
    expiry_ms = auth.get("expiry_date")
    if isinstance(expiry_ms, (int, float)):
        exp = int(expiry_ms / 1000)
    else:
        token_expiry = claims.get("exp")
        if isinstance(token_expiry, int | float):
            exp = int(token_expiry)

    return {
        "email": _find_deep(claims, ("email", "preferred_username"))
        or _string(auth.get("email")),
        "name": _find_deep(claims, ("name", "given_name")),
        "account_id": _string(claims.get("sub")),
        "hosted_domain": _string(claims.get("hd")),
        "issuer": _string(claims.get("iss")),
        "expires_epoch": exp,
        "expires_str": _format_unix_time(exp),
        "refreshable": bool(_string(auth.get("refresh_token"))),
    }


def _claims_from_text(text: str) -> Claims | None:
    try:
        return _claims_from_auth(json.loads(text))
    except Exception:
        return None


def _read_claims(auth_path: Path) -> Claims | None:
    if not auth_path.is_file():
        return None
    try:
        return _claims_from_auth(json.loads(auth_path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _token_key_from_auth(auth: JsonDict) -> str | None:
    for key in ("refresh_token", "access_token"):
        value = auth.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _token_key_from_path(path: Path) -> str | None:
    try:
        return _token_key_from_auth(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _read_active_auth_text() -> str | None:
    secret = _read_cli_keyring_secret()
    if secret is None:
        return None
    auth = _auth_from_keyring_secret(secret)
    if auth is None:
        return None
    mirror = _auth_file()
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_text(json.dumps(auth, indent=2) + "\n", encoding="utf-8")
    mirror.chmod(0o600)
    return json.dumps(auth)


def _read_active_claims() -> Claims | None:
    text = _read_active_auth_text()
    return _claims_from_text(text) if text else None


def _identity_label(claims: Claims | None) -> str:
    if not claims:
        return "(unreadable)"
    email = _string(claims.get("email"))
    name = _string(claims.get("name"))
    if name and email:
        return f"{name} <{email}>"
    return email or name or "(unknown)"


def _identity_key(claims: Claims | None) -> str | None:
    """Key used to decide whether a saved profile matches the live auth file."""
    if not claims:
        return None
    return _string(claims.get("account_id")) or _string(claims.get("email"))


def _active_profile(active_text: str | None = None) -> Path | None:
    text = active_text if active_text is not None else _read_active_auth_text()
    if text is None:
        return None
    active_token = _token_key_from_auth(json.loads(text)) if text else None
    active_identity = _identity_key(_claims_from_text(text))

    marked = _marked_profile()
    if marked is not None:
        if active_token is not None and _token_key_from_path(marked) == active_token:
            return marked
        if (
            active_identity is not None
            and _identity_key(_read_claims(marked)) == active_identity
        ):
            return marked
        if active_identity is None:
            return marked

    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if active_token is not None:
        token_matches = [p for p in profiles if _token_key_from_path(p) == active_token]
        if len(token_matches) == 1:
            return token_matches[0]
    identity_matches = [
        p
        for p in profiles
        if active_identity is not None
        and _identity_key(_read_claims(p)) == active_identity
    ]
    return identity_matches[0] if len(identity_matches) == 1 else None


def _copy_active_auth_to(dest: Path) -> None:
    """Copy the active Antigravity auth to dest. Refuses to write when dest already
    holds a DIFFERENT account — fold-back/sync callers only ever sync the same
    account, and a cross-account write would destroy dest's only token copy."""
    text = _read_active_auth_text()
    if text is None:
        return
    if dest.is_file():
        dest_key = _identity_key(_read_claims(dest))
        text_key = _identity_key(_claims_from_text(text))
        if dest_key is not None and text_key is not None and dest_key != text_key:
            log_yellow(
                f"⚠️  Not syncing active auth into {dest.name}: it belongs to a different account."
            )
            return
    active_auth = json.loads(text)
    if dest.is_file():
        saved_auth = json.loads(dest.read_text(encoding="utf-8"))
        saved_auth.update(active_auth)
        active_auth = saved_auth
    dest.write_text(json.dumps(active_auth, indent=2) + "\n", encoding="utf-8")
    dest.chmod(0o600)


# ── terminal rendering ───────────────────────────────────────────────────


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _panel(title: str, lines: list[str], accent: str = CYAN, width: int = 64) -> None:
    """Bordered header/footer rule around left-aligned content — legible even
    with embedded ANSI color codes since only the header/footer are measured."""
    width = max(width, _visible_len(title) + 8)
    top_dashes = width - _visible_len(title) - 4
    print(f"{accent}┌─ {BOLD}{title}{RESET}{accent} {'─' * top_dashes}┐{RESET}")
    for line in lines or [f"{DIM}(none){RESET}"]:
        print(f"{accent}│{RESET}  {line}")
    print(f"{accent}└{'─' * (width - 1)}┘{RESET}")


def _expiry_status(claims: Claims | None) -> tuple[str, str]:
    """(display text, color) — color carries meaning but text never relies on it alone."""
    if not claims or not claims.get("expires_str"):
        return "—", DIM
    now = datetime.now().timestamp()
    exp = claims["expires_epoch"]
    if not isinstance(exp, int):
        return "—", DIM
    if exp <= now:
        return f"{claims['expires_str']} (EXPIRED)", RED
    if exp - now < 24 * 3600:
        return f"{claims['expires_str']} (soon)", YELLOW
    return _string(claims["expires_str"]) or "—", GREEN


def _claims_lines(claims: Claims | None) -> list[str]:
    if claims is None:
        return [
            f"{YELLOW}No auth file found.{RESET} Run: {BOLD}agy-accounts login-switch <name>{RESET}"
        ]

    has_any = any(
        claims.get(k)
        for k in (
            "email",
            "name",
            "account_id",
            "hosted_domain",
            "issuer",
            "expires_str",
        )
    )
    if not has_any:
        return [f"{YELLOW}No readable account claims found.{RESET}"]

    lines = [f"{BOLD}Account{RESET}       : {_identity_label(claims)}"]
    if claims.get("account_id"):
        lines.append(f"{DIM}Google ID{RESET}     : {claims['account_id']}")
    if claims.get("hosted_domain"):
        lines.append(f"{DIM}Workspace{RESET}     : {claims['hosted_domain']}")
    if claims.get("issuer"):
        lines.append(f"{DIM}Issuer{RESET}        : {claims['issuer']}")
    if claims.get("refreshable"):
        lines.append(f"{DIM}Session{RESET}       : {GREEN}Refreshable by agy{RESET}")
    else:
        expires_text, color = _expiry_status(claims)
        lines.append(f"{DIM}Access token{RESET}  : {color}{expires_text}{RESET}")
    return lines


def _short_id(value: str | None) -> str:
    if not value:
        return f"{DIM}—{RESET}"
    if len(value) <= 14:
        return value
    return f"{value[:8]}…{value[-4:]}"


def _usage_cell(window: UsageWindow | None) -> str:
    if window is None:
        return f"{DIM}—{RESET}"
    color = (
        RED + BOLD
        if window.percentage >= 80
        else YELLOW
        if window.percentage >= 50
        else GREEN
    )
    percent = f"{color}{window.percentage}%{RESET}"
    if window.reset_time is None:
        return percent
    window_kind = "5h" if window.window_minutes == 5 * 60 else "weekly"
    return format_usage_window(window, window_kind, percent)


def _print_accounts_table(rows: list[dict[str, str]]) -> None:
    headers = [
        "PROFILE",
        "ACCOUNT",
        "PLAN",
        "GEMINI WEEK",
        "GEMINI 5H",
        "CLAUDE/GPT WEEK",
        "CLAUDE/GPT 5H",
        "UPDATED",
        "STATE",
    ]
    keys = [
        "profile",
        "account",
        "plan",
        "gemini_weekly",
        "gemini_5h",
        "other_weekly",
        "other_5h",
        "usage_updated",
        "status",
    ]
    widths = [
        max(_visible_len(h), max((_visible_len(r[k]) for r in rows), default=0))
        for h, k in zip(headers, keys)
    ]

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def row(cells: list[str]) -> str:
        parts = [
            f" {cell}{' ' * (w - _visible_len(cell))} "
            for cell, w in zip(cells, widths)
        ]
        return "│" + "│".join(parts) + "│"

    print(rule("┌", "┬", "┐"))
    print(row([f"{BOLD}{h}{RESET}" for h in headers]))
    print(rule("├", "┼", "┤"))
    for r in rows:
        print(row([r[k] for k in keys]))
    print(rule("└", "┴", "┘"))


# ── commands ─────────────────────────────────────────────────────────────


def cmd_who() -> int:
    active_text = _read_active_auth_text()
    active_profile = _active_profile(active_text)
    claims = (
        _read_claims(active_profile)
        if active_profile is not None
        else _claims_from_text(active_text or "")
    )
    active_email = _string((claims or {}).get("email"))

    status_lines = []
    if active_text is not None:
        status_lines.append(f"{GREEN}Logged in through agy keyring{RESET}")
    else:
        status_lines.append(
            f"{RED}Not logged in{RESET}  {DIM}(run `agy-accounts login-switch <name>`){RESET}"
        )
    status_lines.append(f"{DIM}Active account{RESET}: {active_email or '—'}")
    _panel("Antigravity Login Status", status_lines)

    print()
    _panel("Current Auth Claims", _claims_lines(claims))
    return 0


def _save_profile_auth(name: str, auth_text: str) -> int:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1

    _account_dir().mkdir(parents=True, exist_ok=True)
    _account_dir().chmod(0o700)
    profile_file.write_text(auth_text, encoding="utf-8")
    profile_file.chmod(0o600)

    _auth_file().parent.mkdir(parents=True, exist_ok=True)
    _auth_file().write_text(auth_text, encoding="utf-8")
    _auth_file().chmod(0o600)
    _set_current_profile(profile_file)

    print(f"{GREEN}✅ Saved Antigravity profile:{RESET} {BOLD}{name}{RESET}")
    print(f"{DIM}   → {profile_file}{RESET}\n")
    _panel(f"Profile: {name}", _claims_lines(_read_claims(profile_file)), accent=GREEN)
    return 0


def cmd_save(name: str) -> int:
    auth_text = _read_active_auth_text()
    if auth_text is None:
        log_red(f"❌ No Antigravity auth file found: {_auth_file()}")
        log_yellow("   Run: agy-accounts login-switch <profile_name>")
        return 1
    return _save_profile_auth(name, auth_text)


def cmd_list(*, fetch_usage: bool = True) -> int:
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Antigravity profiles.")
        print(
            f"{DIM}   Add one with: agy-accounts save <profile_name>{RESET}",
            file=sys.stderr,
        )
        return 0

    active_text = _read_active_auth_text()
    profile_claims = [(p, _read_claims(p)) for p in profiles]
    active_profile = _active_profile(active_text)

    identity_groups: dict[str, list[Path]] = {}
    for profile_path, claims in profile_claims:
        identity = _identity_key(claims)
        if identity is not None:
            identity_groups.setdefault(identity, []).append(profile_path)
    same_account_profiles: set[Path] = set()
    for group in identity_groups.values():
        if len(group) > 1:
            primary = active_profile if active_profile in group else group[0]
            same_account_profiles.update(p for p in group if p != primary)

    empty_usage = gemini_usage.UsageSnapshot(
        None, None, None, None, None, None, None, None
    )
    rows: list[dict[str, str]] = []
    restore_text = active_text
    try:
        for profile_path, claims in profile_claims:
            name = profile_path.stem
            is_active = profile_path == active_profile
            status = (
                f"{GREEN}{BOLD}ACTIVE{RESET}"
                if is_active
                else f"{YELLOW}SAME ACCT{RESET}"
                if profile_path in same_account_profiles
                else f"{DIM}—{RESET}"
            )
            usage = empty_usage
            if fetch_usage:
                profile_text = profile_path.read_text(encoding="utf-8")
                if _write_cli_auth_text(profile_text):
                    usage = gemini_usage.fetch_usage(timeout=8)
                    refreshed_text = _read_active_auth_text()
                    if refreshed_text is not None:
                        refreshed = json.loads(refreshed_text)
                        saved = json.loads(profile_text)
                        saved.update(refreshed)
                        if usage.email:
                            saved["email"] = usage.email
                        profile_path.write_text(
                            json.dumps(saved, indent=2) + "\n", encoding="utf-8"
                        )
                        profile_path.chmod(0o600)
                        if is_active:
                            restore_text = json.dumps(saved)
            rows.append(
                {
                    "profile": f"{GREEN}{BOLD}{name}{RESET}" if is_active else name,
                    "account": usage.email
                    or (_identity_label(claims) if claims else f"{RED}(unreadable){RESET}"),
                    "plan": usage.plan or f"{DIM}—{RESET}",
                    "gemini_weekly": _usage_cell(usage.gemini_weekly),
                    "gemini_5h": _usage_cell(usage.gemini_session),
                    "other_weekly": _usage_cell(usage.other_weekly),
                    "other_5h": _usage_cell(usage.other_session),
                    "usage_updated": gemini_usage.format_refreshed_at(usage),
                    "status": status,
                }
            )
    finally:
        _restore_cli_auth(restore_text)

    print(f"{BOLD}Saved Antigravity profiles{RESET}  {DIM}({len(rows)}){RESET}")
    _print_accounts_table(rows)
    return 0


def cmd_switch(name: str) -> int:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1
    if not profile_file.is_file():
        log_red(f"❌ Profile not found: {name}")
        print()
        cmd_list()
        return 1

    active_text = _read_active_auth_text()
    if active_text is not None:
        # Fold any token rotation on the active account back into its own saved
        # profile before overwriting, so a later switch back restores fresh
        # tokens. Pure local file op; no network.
        outgoing_profile = _active_profile(_read_active_auth_text())
        if outgoing_profile is not None:
            _copy_active_auth_to(outgoing_profile)

    if not _write_cli_auth_text(profile_file.read_text(encoding="utf-8")):
        log_red("❌ Could not update the agy CLI keyring session")
        return 1
    _set_current_profile(profile_file)

    print(f"{GREEN}✅ Switched Antigravity profile to:{RESET} {BOLD}{name}{RESET}")
    print(f"{DIM}   agy will use this account on its next launch.{RESET}")
    print()
    return cmd_who()


def cmd_switch_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Antigravity profiles available to switch.")
        return 1

    print(f"{BOLD}Choose an Antigravity profile:{RESET}")
    for index, profile in enumerate(profiles, start=1):
        print(
            f"  {index}) {profile.stem}  {DIM}{_identity_label(_read_claims(profile))}{RESET}"
        )

    try:
        selection = input("Select account number: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        log_yellow("Switch cancelled.")
        return 1

    if not selection.isdecimal():
        log_red("❌ Enter one of the account numbers shown above.")
        return 1
    selected_index = int(selection) - 1
    if selected_index < 0 or selected_index >= len(profiles):
        log_red("❌ Enter one of the account numbers shown above.")
        return 1
    return cmd_switch(profiles[selected_index].stem)


def cmd_remove(name: str) -> int:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1
    if not profile_file.is_file():
        log_red(f"❌ Profile not found: {name}")
        return 1
    was_current = _marked_profile() == profile_file
    profile_file.unlink()
    if was_current:
        _current_profile_marker().unlink(missing_ok=True)
    print(f"{GREEN}✅ Removed Antigravity profile:{RESET} {name}")
    return 0


def _refresh_one_profile(name: str, *, show_summary: bool = True) -> tuple[int, str | None]:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1, None
    if not profile_file.is_file():
        log_red(f"❌ Profile not found: {name}")
        print()
        cmd_list()
        return 1, None

    original_text = _read_active_auth_text()
    is_active = _active_profile(original_text) == profile_file
    profile_text = profile_file.read_text(encoding="utf-8")
    if not _write_cli_auth_text(profile_text):
        log_red(f"❌ Could not activate agy profile: {name}")
        return 1, "keyring"
    usage = gemini_usage.fetch_usage()
    refreshed_text = _read_active_auth_text()
    if refreshed_text is not None:
        saved = json.loads(profile_text)
        saved.update(json.loads(refreshed_text))
        if usage.email:
            saved["email"] = usage.email
        profile_file.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        profile_file.chmod(0o600)
        refreshed_text = json.dumps(saved)
    if is_active and refreshed_text is not None:
        _restore_cli_auth(refreshed_text)
    else:
        _restore_cli_auth(original_text)
    if usage.error:
        log_red(f"❌ agy could not refresh quota for {name}: {usage.error}")
        return 1, "agy"

    if show_summary:
        print(f"{GREEN}✅ Refreshed Antigravity profile:{RESET} {BOLD}{name}{RESET}")
    if show_summary:
        print()
        _panel(
            f"Profile: {name}", _claims_lines(_read_claims(profile_file)), accent=GREEN
        )
    return 0, None


def _refresh_all_profiles() -> int:
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Antigravity profiles to refresh.")
        return 0

    failed = []
    for profile_path in profiles:
        rc, kind = _refresh_one_profile(profile_path.stem)
        if rc != 0:
            failed.append(profile_path.stem)

    cmd_list()
    if failed:
        log_red(f"❌ agy refresh failed: {', '.join(failed)}")
        return 1
    print(f"{GREEN}✅ All {len(profiles)} profile(s) refreshed.{RESET}")
    return 0


def _refresh_active_auth() -> int:
    active_text = _read_active_auth_text()
    if active_text is None:
        log_red("❌ No Antigravity CLI session found in the keyring")
        log_yellow("   Run: agy-accounts login-switch <profile_name>")
        return 1

    profile_path = _active_profile(active_text)
    usage = gemini_usage.fetch_usage()
    if usage.error:
        log_red(f"❌ agy refresh failed: {usage.error}")
        return 1
    refreshed_text = _read_active_auth_text()
    print(f"{GREEN}✅ Refreshed active Antigravity auth.{RESET}")

    if profile_path is not None and refreshed_text is not None:
        saved = json.loads(profile_path.read_text(encoding="utf-8"))
        saved.update(json.loads(refreshed_text))
        if usage.email:
            saved["email"] = usage.email
        profile_path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        profile_path.chmod(0o600)
        _set_current_profile(profile_path)
        print(f"{DIM}   (synced back to profile: {profile_path.stem}){RESET}")
    else:
        log_yellow(
            "⚠️  No unambiguous current profile — run: agy-accounts switch <name>"
        )

    print()
    _panel("Current Auth Claims", _claims_lines(_read_claims(_auth_file())), accent=GREEN)
    return 0


def cmd_refresh(target: str | None) -> int:
    if target == "--all":
        return _refresh_all_profiles()
    if target is None:
        return _refresh_active_auth()
    return _refresh_one_profile(target)[0]


def cmd_sync() -> int:
    active_text = _read_active_auth_text()
    if active_text is None:
        log_red("❌ No Antigravity CLI session found in the keyring")
        log_yellow("   Run: agy-accounts login-switch <profile_name>")
        return 1

    profile_path = _active_profile(active_text)
    if profile_path is None:
        log_red("❌ No unambiguous current profile.")
        log_yellow("   Select it first with: agy-accounts switch <name>")
        return 1

    _copy_active_auth_to(profile_path)
    _set_current_profile(profile_path)
    print(
        f"{GREEN}✅ Synced active auth → profile:{RESET} {BOLD}{profile_path.stem}{RESET}"
    )
    print()
    _panel(
        f"Profile: {profile_path.stem}",
        _claims_lines(_read_claims(profile_path)),
        accent=GREEN,
    )
    return 0


def cmd_login_switch(name: str) -> int:
    if not ensure_tool("agy"):
        return 1
    if _profile_file(name) is None:
        return 1
    outgoing_text = _read_active_auth_text()
    outgoing_profile = _active_profile(outgoing_text)
    if outgoing_profile is not None:
        _copy_active_auth_to(outgoing_profile)
    _delete_cli_auth()
    print(
        f"{DIM}Launching the official agy login. Complete browser sign-in, then exit agy with Ctrl+D twice.{RESET}"
    )
    try:
        login = subprocess.run(["agy"], check=False)
    except KeyboardInterrupt:
        login = subprocess.CompletedProcess(["agy"], 130)
    auth_text = _read_active_auth_text()
    if login.returncode != 0 or auth_text is None:
        _restore_cli_auth(outgoing_text)
        log_yellow("Login cancelled. Your previous agy session was restored.")
        return login.returncode or 1

    usage = gemini_usage.fetch_usage()
    refreshed_text = _read_active_auth_text() or auth_text
    auth = json.loads(refreshed_text)
    if usage.email:
        auth["email"] = usage.email
    return _save_profile_auth(name, json.dumps(auth, indent=2) + "\n")


# ── entry point ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(HELP)
        return 0

    command, *rest = argv

    if command in ("who", "current"):
        return cmd_who()
    if command == "save":
        if not rest:
            log_red("Usage: agy-accounts save <profile_name>")
            return 1
        return cmd_save(rest[0])
    if command == "list":
        return cmd_list()
    if command == "switch":
        if not rest:
            return cmd_switch_interactive()
        return cmd_switch(rest[0])
    if command == "remove":
        if not rest:
            log_red("Usage: agy-accounts remove <profile_name>")
            return 1
        return cmd_remove(rest[0])
    if command == "refresh":
        return cmd_refresh(rest[0] if rest else None)
    if command == "sync":
        return cmd_sync()
    if command == "login-switch":
        if not rest:
            log_red("Usage: agy-accounts login-switch <profile_name>")
            return 1
        return cmd_login_switch(rest[0])

    log_red(f"❌ Unknown command: {command}")
    print(HELP)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
