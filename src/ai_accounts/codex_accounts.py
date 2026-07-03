"""codex-accounts — manage multiple Codex CLI login profiles.

Python port of the zsh ``codex_account_manager`` block, redesigned with a
color-coded, tabular terminal UI so accounts are easy to tell apart at a
glance (which profile is saved, which one is currently active, when a
token expires). Never prints raw tokens — only decoded, non-secret claims.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ._utils import DIM, GREEN, RED, RESET, YELLOW, ensure_tool, have, log_red, log_yellow

BOLD = "\033[1m"
CYAN = "\033[1;36m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

HELP = """codex-accounts — manage multiple Codex CLI login profiles

USAGE
  codex-accounts who                   Show the current logged-in Codex account
  codex-accounts current               Alias for `who`
  codex-accounts save <name>           Save the current login as a reusable profile
  codex-accounts list                  List saved profiles (table view)
  codex-accounts switch <name>         Switch to a saved profile
  codex-accounts remove <name>         Delete a saved profile
  codex-accounts login-switch <name>   codex logout + codex login + save as <name>
  codex-accounts -h | --help           Show this help

EXAMPLES
  codex-accounts login-switch personal
  codex-accounts login-switch work
  codex-accounts list
  codex-accounts switch personal
  codex-accounts who

Profiles live under ~/.codex/accounts/<name>.json (override with $CODEX_ACCOUNT_DIR).
Treat that directory as secrets — saved profiles contain Codex auth tokens.
"""


# ── paths ─────────────────────────────────────────────────────────────────

def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def _account_dir() -> Path:
    return Path(os.environ.get("CODEX_ACCOUNT_DIR", str(_codex_home() / "accounts")))


def _auth_file() -> Path:
    return Path(os.environ.get("CODEX_AUTH_JSON", str(_codex_home() / "auth.json")))


def _profile_file(name: str) -> Path | None:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not safe:
        log_red("❌ Profile name cannot be empty")
        return None
    return _account_dir() / f"{safe}.json"


# ── JWT claim decoding (no raw tokens ever printed) ─────────────────────────

def _decode_jwt_payload(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        payload = token.split(".")[1]
        padded = payload.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        return json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception:
        return None


def _find_deep(obj, keys: tuple[str, ...]):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    for value in obj.values():
        found = _find_deep(value, keys)
        if found not in (None, ""):
            return found
    return None


def _format_unix_time(value) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _read_claims(auth_path: Path) -> dict | None:
    """Read non-secret account claims from a Codex auth file. None if missing/unreadable."""
    if not auth_path.is_file():
        return None
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    tokens = auth.get("tokens") or {}
    # access_token expires in days; id_token expires in ~1 hour (identity-only).
    # Prefer access_token so the displayed expiry reflects when re-login is actually needed.
    jwt = (
        tokens.get("access_token")
        or auth.get("access_token")
        or tokens.get("id_token")
        or auth.get("id_token")
    )
    claims = _decode_jwt_payload(jwt) or {}

    exp = claims.get("exp")
    return {
        "email": _find_deep(claims, ("email", "preferred_username", "upn")),
        "name": _find_deep(claims, ("name", "given_name")),
        "account_id": tokens.get("account_id")
        or auth.get("account_id")
        or _find_deep(claims, ("account_id", "accountId", "sub")),
        "organization_id": tokens.get("organization_id")
        or auth.get("organization_id")
        or _find_deep(claims, ("organization_id", "org_id")),
        "issuer": claims.get("iss"),
        "expires_epoch": exp,
        "expires_str": _format_unix_time(exp),
    }


def _identity_label(claims: dict | None) -> str:
    if not claims:
        return "(unreadable)"
    email, name = claims.get("email"), claims.get("name")
    if name and email:
        return f"{name} <{email}>"
    return email or name or "(unknown)"


def _identity_key(claims: dict | None) -> str | None:
    """Key used to decide whether a saved profile matches the live auth file."""
    if not claims:
        return None
    return claims.get("account_id") or claims.get("email")


def _expiry_status(claims: dict | None) -> tuple[str, str]:
    """(display text, color) — color carries meaning but text never relies on it alone."""
    if not claims or not claims.get("expires_str"):
        return "—", DIM
    now = datetime.now().timestamp()
    exp = claims["expires_epoch"]
    if exp <= now:
        return f"{claims['expires_str']} (EXPIRED)", RED
    if exp - now < 24 * 3600:
        return f"{claims['expires_str']} (soon)", YELLOW
    return claims["expires_str"], GREEN


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
    print(f"{accent}└{'─' * width}┘{RESET}")


def _claims_lines(claims: dict | None) -> list[str]:
    if claims is None:
        return [f"{YELLOW}No auth file found.{RESET} Run: {BOLD}codex login{RESET}"]

    has_any = any(
        claims.get(k) for k in ("email", "name", "account_id", "organization_id", "issuer", "expires_str")
    )
    if not has_any:
        return [f"{YELLOW}No readable account claims found.{RESET}"]

    lines = [f"{BOLD}Account{RESET}       : {_identity_label(claims)}"]
    if claims.get("account_id"):
        lines.append(f"{DIM}Account ID{RESET}    : {claims['account_id']}")
    if claims.get("organization_id"):
        lines.append(f"{DIM}Organization{RESET}  : {claims['organization_id']}")
    if claims.get("issuer"):
        lines.append(f"{DIM}Issuer{RESET}        : {claims['issuer']}")
    expires_text, color = _expiry_status(claims)
    lines.append(f"{DIM}Expires{RESET}       : {color}{expires_text}{RESET}")
    return lines


def _print_accounts_table(rows: list[dict]) -> None:
    headers = ["PROFILE", "ACCOUNT", "ACCOUNT ID", "EXPIRES", "STATUS"]
    keys = ["profile", "account", "account_id", "expires", "status"]
    widths = [
        max(_visible_len(h), max((_visible_len(r[k]) for r in rows), default=0))
        for h, k in zip(headers, keys)
    ]

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def row(cells: list[str]) -> str:
        parts = [f" {cell}{' ' * (w - _visible_len(cell))} " for cell, w in zip(cells, widths)]
        return "│" + "│".join(parts) + "│"

    print(rule("┌", "┬", "┐"))
    print(row([f"{BOLD}{h}{RESET}" for h in headers]))
    print(rule("├", "┼", "┤"))
    for r in rows:
        print(row([r[k] for k in keys]))
    print(rule("└", "┴", "┘"))


# ── commands ─────────────────────────────────────────────────────────────

def cmd_who() -> int:
    status_lines: list[str]
    if have("codex"):
        result = subprocess.run(["codex", "login", "status"], capture_output=True, text=True)
        text = (result.stdout or result.stderr or "").strip()
        status_lines = text.splitlines() if text else [f"{DIM}(no output){RESET}"]
    else:
        status_lines = [f"{RED}codex command not found{RESET}  {DIM}(install: npm install -g @openai/codex){RESET}"]
    _panel("Codex Login Status", status_lines)

    print()
    _panel("Current Auth Claims", _claims_lines(_read_claims(_auth_file())))
    return 0


def cmd_save(name: str) -> int:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1

    auth_path = _auth_file()
    if not auth_path.is_file():
        log_red(f"❌ No Codex auth file found: {auth_path}")
        log_yellow("   Run: codex login")
        return 1

    _account_dir().mkdir(parents=True, exist_ok=True)
    shutil.copy2(auth_path, profile_file)
    _account_dir().chmod(0o700)
    profile_file.chmod(0o600)

    print(f"{GREEN}✅ Saved Codex profile:{RESET} {BOLD}{name}{RESET}")
    print(f"{DIM}   → {profile_file}{RESET}\n")
    _panel(f"Profile: {name}", _claims_lines(_read_claims(profile_file)), accent=GREEN)
    return 0


def cmd_list() -> int:
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Codex profiles.")
        print(f"{DIM}   Add one with: codex-accounts save <profile_name>{RESET}", file=sys.stderr)
        return 0

    active_key = _identity_key(_read_claims(_auth_file()))

    rows = []
    for profile_path in profiles:
        name = profile_path.stem
        claims = _read_claims(profile_path)
        is_active = active_key is not None and _identity_key(claims) == active_key
        expires_text, color = _expiry_status(claims)
        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{name}{RESET}" if is_active else name,
                "account": _identity_label(claims) if claims else f"{RED}(unreadable){RESET}",
                "account_id": (claims or {}).get("account_id") or f"{DIM}—{RESET}",
                "expires": f"{color}{expires_text}{RESET}",
                "status": f"{GREEN}{BOLD}ACTIVE{RESET}" if is_active else f"{DIM}—{RESET}",
            }
        )

    print(f"{BOLD}Saved Codex profiles{RESET}  {DIM}({len(rows)}){RESET}")
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

    auth_path = _auth_file()
    auth_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path = None
    if auth_path.is_file():
        backup_path = auth_path.with_name(f"{auth_path.name}.backup.{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(auth_path, backup_path)
        backup_path.chmod(0o600)

    shutil.copy2(profile_file, auth_path)
    auth_path.chmod(0o600)

    print(f"{GREEN}✅ Switched Codex profile to:{RESET} {BOLD}{name}{RESET}")
    if backup_path:
        print(f"{DIM}   (previous auth backed up to {backup_path}){RESET}")
    print()
    return cmd_who()


def cmd_remove(name: str) -> int:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1
    if not profile_file.is_file():
        log_red(f"❌ Profile not found: {name}")
        return 1
    profile_file.unlink()
    print(f"{GREEN}✅ Removed Codex profile:{RESET} {name}")
    return 0


def cmd_login_switch(name: str) -> int:
    if not ensure_tool("codex"):
        return 1
    subprocess.run(["codex", "logout"])
    login = subprocess.run(["codex", "login"])
    if login.returncode != 0:
        log_red("❌ codex login did not complete successfully")
        return login.returncode
    return cmd_save(name)


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
            log_red("Usage: codex-accounts save <profile_name>")
            return 1
        return cmd_save(rest[0])
    if command == "list":
        return cmd_list()
    if command == "switch":
        if not rest:
            log_red("Usage: codex-accounts switch <profile_name>")
            print()
            cmd_list()
            return 1
        return cmd_switch(rest[0])
    if command == "remove":
        if not rest:
            log_red("Usage: codex-accounts remove <profile_name>")
            return 1
        return cmd_remove(rest[0])
    if command == "login-switch":
        if not rest:
            log_red("Usage: codex-accounts login-switch <profile_name>")
            return 1
        return cmd_login_switch(rest[0])

    log_red(f"❌ Unknown command: {command}")
    print(HELP)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
