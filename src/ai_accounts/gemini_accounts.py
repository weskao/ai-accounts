"""agy-accounts — manage multiple Gemini CLI login profiles.

Sibling of ``codex-accounts``, adapted to the Gemini CLI's auth model. Gemini
stores its OAuth credentials as a plain file (``~/.gemini/oauth_creds.json``)
and tracks the active/previous Google account emails in
``~/.gemini/google_accounts.json`` — no macOS keychain or API key. Quota comes
from Gemini's OAuth-backed Code Assist endpoint. Never prints raw tokens — only
decoded, non-secret ``id_token`` claims.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
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
    have,
    log_red,
    log_yellow,
)
from .codex_usage import UsageWindow, format_usage_window

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonDict: TypeAlias = dict[str, JsonValue]
Claims: TypeAlias = dict[str, str | int | None]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


BOLD = "\033[1m"
CYAN = "\033[1;36m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

HELP = """agy-accounts — manage multiple Gemini CLI login profiles

USAGE
  agy-accounts who                   Show the current logged-in Gemini account
  agy-accounts current               Alias for `who`
  agy-accounts save <name>           Save the current login as a reusable profile
  agy-accounts list                  List saved profiles (table view)
  agy-accounts switch [<name>]       Switch by name; no name = interactive picker
  agy-accounts remove <name>         Delete a saved profile
  agy-accounts refresh [<name>]      Refresh tokens via OAuth (no browser, no logout);
                                     no name = refresh active auth + sync it back
  agy-accounts refresh --all         Refresh every saved profile
  agy-accounts sync                  Copy the active auth back to its matching profile
  agy-accounts login-switch <name>   Isolated gemini login + save as <name>
  agy-accounts -h | --help           Show this help

EXAMPLES
  agy-accounts login-switch personal
  agy-accounts login-switch work
  agy-accounts list
  agy-accounts switch
  agy-accounts switch personal
  agy-accounts refresh --all
  agy-accounts who

Profiles live under ~/.gemini/accounts/<name>.json (override with $GEMINI_ACCOUNT_DIR).
Treat that directory as secrets — saved profiles contain Gemini OAuth tokens.
"""


# ── paths ─────────────────────────────────────────────────────────────────
# Gemini resolves its config dir as ``homedir()/.gemini`` where ``homedir()``
# honours ``$GEMINI_CLI_HOME`` before the OS home (verified against the
# installed @google/gemini-cli bundle). We mirror that exactly so an isolated
# login (login-switch) and the live config point at the same files.


def _gemini_dir() -> Path:
    home = Path(os.environ.get("GEMINI_CLI_HOME", str(Path.home())))
    return home / ".gemini"


def _account_dir() -> Path:
    return Path(os.environ.get("GEMINI_ACCOUNT_DIR", str(_gemini_dir() / "accounts")))


def _auth_file() -> Path:
    return Path(
        os.environ.get("GEMINI_OAUTH_JSON", str(_gemini_dir() / "oauth_creds.json"))
    )


def _google_accounts_file() -> Path:
    return _gemini_dir() / "google_accounts.json"


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
    """Extract non-secret account claims from a parsed Gemini oauth_creds object.

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
        "email": _find_deep(claims, ("email", "preferred_username")),
        "name": _find_deep(claims, ("name", "given_name")),
        "account_id": _string(claims.get("sub")),
        "hosted_domain": _string(claims.get("hd")),
        "issuer": _string(claims.get("iss")),
        "expires_epoch": exp,
        "expires_str": _format_unix_time(exp),
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
    """Raw active-auth JSON text from oauth_creds.json (file is the sole source
    of truth for Gemini — no keychain to reconcile against)."""
    auth_path = _auth_file()
    if not auth_path.is_file():
        return None
    try:
        return auth_path.read_text(encoding="utf-8")
    except OSError:
        return None


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
    """Copy the active Gemini auth to dest. Refuses to write when dest already
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
    dest.write_text(text, encoding="utf-8")
    dest.chmod(0o600)


# ── google_accounts.json (Gemini's own active/previous account tracker) ─────


def _set_active_google_account(email: str | None) -> None:
    """Keep ~/.gemini/google_accounts.json in step with the profile we just
    activated, so Gemini's own UI shows the right account. Best-effort — a
    write failure here never fails the switch/save."""
    if not email:
        return
    path = _google_accounts_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    previous = data.get("active")
    raw_old = data.get("old")
    old = (
        [value for value in raw_old if isinstance(value, str)]
        if isinstance(raw_old, list)
        else []
    )
    if previous and previous != email and previous not in old:
        old.append(previous)
    old = [e for e in old if e != email]
    data["active"] = email
    data["old"] = old
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


# ── OAuth token refresh (Google installed-app flow, gemini-cli client) ──────
_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _oauth_client_credentials() -> tuple[str, str] | None:
    executable = shutil.which("gemini")
    if executable is None:
        return None
    resolved = Path(executable).resolve()
    package_root = next(
        (
            path
            for path in (resolved.parent, *resolved.parents)
            if path.name == "gemini-cli"
        ),
        None,
    )
    if package_root is None:
        return None

    oauth_file = Path("dist/src/code_assist/oauth2.js")
    candidates = [
        package_root / oauth_file,
        package_root / "node_modules/@google/gemini-cli-core" / oauth_file,
        *sorted((package_root / "bundle").glob("*.js")),
    ]
    id_pattern = re.compile(
        r"(?:const|let|var)?\s*OAUTH_CLIENT_ID\s*=\s*['\"]([\w.\-]+)['\"]"
    )
    secret_pattern = re.compile(
        r"(?:const|let|var)?\s*OAUTH_CLIENT_SECRET\s*=\s*['\"]([\w\-]+)['\"]"
    )
    for path in candidates:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        client_id = id_pattern.search(content)
        client_secret = secret_pattern.search(content)
        if client_id and client_secret:
            return client_id.group(1), client_secret.group(1)
    return None


def _oauth_refresh(refresh_token: str) -> tuple[JsonDict | None, str | None]:
    """Exchange a refresh_token for fresh tokens against Google's token
    endpoint. Returns (response, None) on success, (None, error) on failure —
    never raises, never logs tokens."""
    credentials = _oauth_client_credentials()
    if credentials is None:
        return None, "Gemini CLI OAuth configuration not found"
    client_id, client_secret = credentials
    request = urllib.request.Request(
        _OAUTH_TOKEN_URL,
        data=urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return (
            None,
            f"HTTP {exc.code} from token endpoint (refresh token may be expired or revoked)",
        )
    except urllib.error.URLError as exc:
        return None, f"network error: {exc.reason}"
    except Exception as exc:  # malformed JSON response, etc.
        return None, str(exc)


def _apply_refreshed_tokens(path: Path, refreshed: JsonDict) -> None:
    """Write refreshed token fields into an oauth_creds JSON file in place.
    Google does not return a new refresh_token on refresh, so the existing one
    is kept when the response omits it."""
    auth = json.loads(path.read_text(encoding="utf-8"))
    for key in ("access_token", "id_token", "refresh_token", "scope", "token_type"):
        if refreshed.get(key):
            auth[key] = refreshed[key]
    expires_in = refreshed.get("expires_in")
    if isinstance(expires_in, int | float):
        auth["expiry_date"] = int((time.time() + expires_in) * 1000)
    path.write_text(json.dumps(auth, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _is_revoked_error(error: str | None) -> bool:
    """True when the token endpoint outright rejected the refresh_token (HTTP
    4xx) — a genuine revocation, versus a transient network/5xx hiccup."""
    return (error or "").startswith("HTTP 4")


def _read_refresh_token(path: Path) -> str | None:
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return auth.get("refresh_token")


def _refresh_file(
    path: Path, label: str, relogin_hint: str | None = None
) -> tuple[JsonDict | None, str | None]:
    """Refresh the tokens stored in one oauth_creds file.

    Returns (response, None) on success. On failure returns (None, kind):
    "revoked" (missing or HTTP-4xx-rejected refresh_token — only a fresh login
    helps) or "transient" (network / HTTP 5xx / timeout — safe to retry)."""
    hint = relogin_hint or f"Re-login with: agy-accounts login-switch {label}"
    refresh_token = _read_refresh_token(path)
    if not refresh_token:
        log_red(f"❌ No refresh_token found in {label}")
        log_yellow(f"   {hint}")
        return None, "revoked"
    refreshed, error = _oauth_refresh(refresh_token)
    if refreshed is None:
        if _is_revoked_error(error):
            log_red(f"❌ Refresh token revoked/dead for {label}: {error}")
            log_yellow(f"   {hint}")
            return None, "revoked"
        log_red(f"❌ Refresh failed for {label}: {error}")
        if error == "Gemini CLI OAuth configuration not found":
            log_yellow("   Update or reinstall Gemini CLI, then retry.")
        else:
            log_yellow("   Token endpoint unreachable — retry later.")
        return None, "transient"
    _apply_refreshed_tokens(path, refreshed)
    return refreshed, None


def _sync_refreshed_profile(profile_path: Path) -> bool:
    if _active_profile() != profile_path:
        return False
    auth_path = _auth_file()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(profile_path, auth_path)
    auth_path.chmod(0o600)
    _set_current_profile(profile_path)
    return True


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
            f"{YELLOW}No auth file found.{RESET} Run: {BOLD}gemini{RESET} and log in"
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
    expires_text, color = _expiry_status(claims)
    lines.append(f"{DIM}Token expiry{RESET}  : {color}{expires_text}{RESET}")
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
    return format_usage_window(window, "daily", percent)


def _print_accounts_table(rows: list[dict[str, str]]) -> None:
    headers = [
        "PROFILE",
        "ACCOUNT",
        "PRO USED",
        "FLASH USED",
        "LITE USED",
        "UPDATED",
        "AUTH",
        "STATE",
    ]
    keys = [
        "profile",
        "account",
        "usage_pro",
        "usage_flash",
        "usage_lite",
        "usage_updated",
        "expires",
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
    active_email = None
    ga = _google_accounts_file()
    if ga.is_file():
        try:
            active_email = json.loads(ga.read_text(encoding="utf-8")).get("active")
        except (OSError, ValueError):
            active_email = None

    status_lines = []
    if _auth_file().is_file():
        status_lines.append(f"{GREEN}Logged in{RESET}  {DIM}({_auth_file()}){RESET}")
    else:
        status_lines.append(
            f"{RED}Not logged in{RESET}  {DIM}(no oauth_creds.json — run `gemini` and log in){RESET}"
        )
    status_lines.append(
        f"{DIM}Active account{RESET}: {active_email or '—'}"
        + (f"  {DIM}(not installed){RESET}" if not have("gemini") else "")
    )
    _panel("Gemini Login Status", status_lines)

    print()
    _panel("Current Auth Claims", _claims_lines(_read_active_claims()))
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
    _set_active_google_account(
        _string((_claims_from_text(auth_text) or {}).get("email"))
    )

    print(f"{GREEN}✅ Saved Gemini profile:{RESET} {BOLD}{name}{RESET}")
    print(f"{DIM}   → {profile_file}{RESET}\n")
    _panel(f"Profile: {name}", _claims_lines(_read_claims(profile_file)), accent=GREEN)
    return 0


def cmd_save(name: str) -> int:
    auth_text = _read_active_auth_text()
    if auth_text is None:
        log_red(f"❌ No Gemini auth file found: {_auth_file()}")
        log_yellow("   Run: gemini  (and log in)")
        return 1
    return _save_profile_auth(name, auth_text)


def cmd_list(*, fetch_usage: bool = True) -> int:
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Gemini profiles.")
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

    empty_usage = gemini_usage.UsageSnapshot(None, None, None, None, None)
    rows = []
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
        expires_text, color = _expiry_status(claims)
        usage = empty_usage
        if fetch_usage:
            usage = gemini_usage.fetch_usage(
                _auth_file() if is_active else profile_path
            )
        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{name}{RESET}" if is_active else name,
                "account": _identity_label(claims)
                if claims
                else f"{RED}(unreadable){RESET}",
                "usage_pro": _usage_cell(usage.pro),
                "usage_flash": _usage_cell(usage.flash),
                "usage_lite": _usage_cell(usage.flash_lite),
                "usage_updated": gemini_usage.format_refreshed_at(usage),
                "expires": f"{color}{expires_text}{RESET}",
                "status": status,
            }
        )

    print(f"{BOLD}Saved Gemini profiles{RESET}  {DIM}({len(rows)}){RESET}")
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
        # Fold any token rotation on the active account back into its own saved
        # profile before overwriting, so a later switch back restores fresh
        # tokens. Pure local file op; no network.
        outgoing_profile = _active_profile(_read_active_auth_text())
        if outgoing_profile is not None:
            _copy_active_auth_to(outgoing_profile)

        backup_path = auth_path.with_name(f"{auth_path.name}.backup")
        shutil.copy2(auth_path, backup_path)
        backup_path.chmod(0o600)

    shutil.copy2(profile_file, auth_path)
    auth_path.chmod(0o600)
    _set_current_profile(profile_file)
    _set_active_google_account(_string((_read_claims(profile_file) or {}).get("email")))

    print(f"{GREEN}✅ Switched Gemini profile to:{RESET} {BOLD}{name}{RESET}")
    if backup_path:
        print(f"{DIM}   (previous auth backed up to {backup_path}){RESET}")
    # ponytail: no self-heal-on-switch refresh — Gemini auto-refreshes its own
    # short-lived access token on next use; run `agy-accounts refresh` to force it.

    print()
    return cmd_who()


def cmd_switch_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Gemini profiles available to switch.")
        return 1

    print(f"{BOLD}Choose a Gemini profile:{RESET}")
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
    print(f"{GREEN}✅ Removed Gemini profile:{RESET} {name}")
    return 0


def _refresh_one_profile(
    name: str, *, show_summary: bool = True
) -> tuple[int, str | None]:
    """Refresh one saved profile. Returns (exit_code, failure_kind)."""
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1, None
    if not profile_file.is_file():
        log_red(f"❌ Profile not found: {name}")
        print()
        cmd_list()
        return 1, None

    refreshed, kind = _refresh_file(profile_file, name)
    if refreshed is None:
        return 1, kind

    if show_summary:
        print(f"{GREEN}✅ Refreshed Gemini profile:{RESET} {BOLD}{name}{RESET}")
    synced_active = _sync_refreshed_profile(profile_file)
    if show_summary and synced_active:
        print(f"{DIM}   (same account is active — oauth_creds.json updated too){RESET}")

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
        log_yellow("⚠️  No saved Gemini profiles to refresh.")
        return 0

    revoked = []
    transient = []
    for profile_path in profiles:
        rc, kind = _refresh_one_profile(profile_path.stem)
        if rc != 0:
            (revoked if kind == "revoked" else transient).append(profile_path.stem)

    cmd_list()
    if revoked:
        log_red(f"❌ Revoked (re-login required): {', '.join(revoked)}")
    if transient:
        log_yellow(f"⚠️  Transient failure, retry later: {', '.join(transient)}")
    if revoked or transient:
        return 1
    print(f"{GREEN}✅ All {len(profiles)} profile(s) refreshed.{RESET}")
    return 0


def _refresh_active_auth() -> int:
    auth_path = _auth_file()
    if not auth_path.is_file():
        log_red(f"❌ No Gemini auth file found: {auth_path}")
        log_yellow("   Run: gemini  (and log in)")
        return 1

    profile_path = _active_profile(_read_active_auth_text())
    refreshed, _kind = _refresh_file(
        auth_path, "the active auth", relogin_hint="Re-login with: gemini  (and log in)"
    )
    if refreshed is None:
        return 1
    print(f"{GREEN}✅ Refreshed active Gemini auth.{RESET}")

    if profile_path is not None:
        _copy_active_auth_to(profile_path)
        _set_current_profile(profile_path)
        print(f"{DIM}   (synced back to profile: {profile_path.stem}){RESET}")
    else:
        log_yellow(
            "⚠️  No unambiguous current profile — run: agy-accounts switch <name>"
        )

    print()
    _panel("Current Auth Claims", _claims_lines(_read_claims(auth_path)), accent=GREEN)
    return 0


def cmd_refresh(target: str | None) -> int:
    if target == "--all":
        return _refresh_all_profiles()
    if target is None:
        return _refresh_active_auth()
    return _refresh_one_profile(target)[0]


def cmd_sync() -> int:
    auth_path = _auth_file()
    if not auth_path.is_file():
        log_red(f"❌ No Gemini auth file found: {auth_path}")
        log_yellow("   Run: gemini  (and log in)")
        return 1

    profile_path = _active_profile(_read_active_auth_text())
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


@contextmanager
def _suppress_interrupt_echo():
    try:
        import termios

        fd = sys.stdin.fileno()
        if not os.isatty(fd) or not hasattr(termios, "ECHOCTL"):
            yield
            return
        original = termios.tcgetattr(fd)
        if not original[3] & termios.ECHOCTL:
            yield
            return
        quiet = original.copy()
        quiet[3] &= ~termios.ECHOCTL
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
    except (ImportError, OSError):
        yield
        return

    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)


def _run_isolated_login() -> tuple[str | None, int]:
    """Drive a fresh Gemini login into an isolated GEMINI_CLI_HOME so the
    current profile is untouched. Gemini has no headless `login` subcommand —
    it authenticates on interactive launch — so this starts `gemini` in the
    temp home; complete the Google login, then exit gemini to harvest the
    credentials it wrote."""
    with tempfile.TemporaryDirectory(prefix="agy-accounts-login-") as temp_dir:
        login_home = Path(temp_dir)
        env = os.environ.copy()
        env["GEMINI_CLI_HOME"] = str(login_home)
        env.pop("GEMINI_ACCOUNT_DIR", None)
        env.pop("GEMINI_OAUTH_JSON", None)
        print(
            f"{DIM}Launching gemini for login — complete the browser sign-in, then exit gemini.{RESET}"
        )
        try:
            with _suppress_interrupt_echo():
                login = subprocess.run(["gemini"], env=env)
        except KeyboardInterrupt:
            log_yellow("Login cancelled. Your current profile was not changed.")
            return None, 130
        if login.returncode != 0:
            log_red("❌ gemini login did not complete successfully")
            return None, login.returncode
        creds = login_home / ".gemini" / "oauth_creds.json"
        try:
            return creds.read_text(encoding="utf-8"), 0
        except OSError as error:
            log_red(
                f"❌ gemini login completed without a readable oauth_creds.json: {error}"
            )
            return None, 1


def cmd_login_switch(name: str) -> int:
    if not ensure_tool("gemini"):
        return 1
    if _profile_file(name) is None:
        return 1
    outgoing_profile = _active_profile(_read_active_auth_text())
    if outgoing_profile is not None:
        _copy_active_auth_to(outgoing_profile)
    auth_text, returncode = _run_isolated_login()
    if auth_text is None:
        return returncode
    return _save_profile_auth(name, auth_text)


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
