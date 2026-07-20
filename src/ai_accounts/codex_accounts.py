"""codex-accounts — manage multiple Codex CLI login profiles.

Python port of the zsh ``codex_account_manager`` block, redesigned with a
color-coded, tabular terminal UI so accounts are easy to tell apart at a
glance (which profile is saved, which one is currently active, when a
token expires). Never prints raw tokens — only decoded, non-secret claims.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from . import codex_usage
from ._utils import DIM, GREEN, RED, RESET, YELLOW, ensure_tool, have, log_red, log_yellow

BOLD = "\033[1m"
CYAN = "\033[1;36m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_USAGE_CELL_RE: Final = re.compile(
    r"^(?P<color>(?:\033\[[0-9;]*m)*)(?P<percent>\d+)%(?P<reset>\033\[0m)? · "
    r"(?:(?P<days>\d+)d )?(?P<hours>\d+)h (?P<minutes>\d+)m$"
)

HELP = """codex-accounts — manage multiple Codex CLI login profiles

USAGE
  codex-accounts who                   Show the current logged-in Codex account
  codex-accounts current               Alias for `who`
  codex-accounts save <name>           Save the current login as a reusable profile
  codex-accounts list                  List profiles with usage (never refreshes tokens)
  codex-accounts switch [<name>]       Switch by name; no name = interactive picker
  codex-accounts remove <name>         Delete a saved profile
  codex-accounts refresh [<name>]      Refresh tokens via OAuth (no browser, no logout);
                                       no name = refresh active auth + sync it back
  codex-accounts refresh --all         Refresh every saved profile
  codex-accounts sync                  Copy the active auth back to its matching profile
  codex-accounts login-switch <name>   Isolated codex login + save as <name>
  codex-accounts -h | --help           Show this help

EXAMPLES
  codex-accounts login-switch personal
  codex-accounts login-switch work
  codex-accounts list
  codex-accounts switch
  codex-accounts switch personal
  codex-accounts refresh --all
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


# ── macOS keychain mirror ───────────────────────────────────────────────────
# Modern Codex CLI stores its OAuth credentials in the login keychain and reads
# them in preference to auth.json (only falling back to the file when the item
# is absent). So a switch that rewrites only auth.json is silently ignored by
# codex. We mirror every active-auth write into that keychain item, matching the
# derivation codex uses: service "Codex Auth", account "cli|<first-16-hex of
# sha256(realpath(CODEX_HOME))>". Verified against cockpit-core codex_account.rs.

_KEYCHAIN_SERVICE = "Codex Auth"


def _keychain_account() -> str | None:
    """Keychain account name codex derives from the resolved CODEX_HOME.
    None off macOS (no keychain-backed store there)."""
    if platform.system() != "Darwin":
        return None
    try:
        resolved = os.path.realpath(_codex_home())
    except OSError:
        resolved = str(_codex_home())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return f"cli|{digest[:16]}"


def _read_keychain_auth() -> str | None:
    """Return the keychain-stored auth JSON string, or None if absent/off-macOS."""
    account = _keychain_account()
    if account is None:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    secret = (result.stdout or "").strip()
    if not secret:
        return None
    # `security -w` hex-encodes the secret when it contains bytes it deems
    # "non-clean" (e.g. newlines). Decode that back to the original JSON text.
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", secret):
        try:
            secret = bytes.fromhex(secret).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
    return secret


def _write_keychain_auth(content: str) -> bool:
    """Update the keychain item's secret in place. Returns True on success."""
    account = _keychain_account()
    if account is None:
        return False
    try:
        result = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", _KEYCHAIN_SERVICE, "-a", account, "-w", content],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _is_default_auth(auth_path: Path) -> bool:
    """True when auth_path is the codex-managed active auth (CODEX_HOME/auth.json)."""
    return auth_path == _codex_home() / "auth.json"


def _mirror_active_auth_to_keychain(auth_path: Path) -> None:
    """Mirror the active auth.json into codex's keychain item so codex actually
    picks up the switch. Best-effort and *update-only*: we never fabricate a
    keychain credential store codex wasn't already using — if no item exists,
    auth.json is authoritative and we leave the keychain untouched."""
    if not _is_default_auth(auth_path) or _keychain_account() is None:
        return
    if _read_keychain_auth() is None:
        return  # codex isn't keychain-backed here; auth.json is the source of truth
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    # Compact, newline-free JSON — matches codex/cockpit and avoids `security`
    # storing a value that reads back hex-encoded.
    content = json.dumps(auth, separators=(",", ":"))
    if not _write_keychain_auth(content):
        log_yellow(
            "⚠️  Could not update the macOS keychain; codex may keep using the "
            "previous account until its next login."
        )


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


def _claims_from_auth(auth: dict) -> dict:
    """Extract non-secret account claims from a parsed Codex auth object."""
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
        # ChatGPT plan tier ("plus" / "pro" / "team" / …), namespaced under the
        # https://api.openai.com/auth claim. No rate multiplier exists for
        # Codex — the plan type itself is the whole tier signal.
        "plan": _find_deep(claims, ("chatgpt_plan_type",)),
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


def _claims_from_text(text: str) -> dict | None:
    """Claims from a raw auth-JSON string (e.g. a keychain secret). None if unparseable."""
    try:
        return _claims_from_auth(json.loads(text))
    except Exception:
        return None


def _read_claims(auth_path: Path) -> dict | None:
    """Read non-secret account claims from a Codex auth file. None if missing/unreadable."""
    if not auth_path.is_file():
        return None
    try:
        return _claims_from_auth(json.loads(auth_path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _token_key_from_auth(auth: dict) -> str | None:
    tokens = auth.get("tokens") or {}
    if not isinstance(tokens, dict):
        tokens = {}
    return tokens.get("refresh_token") or tokens.get("access_token") or auth.get("access_token")


def _token_key_from_text(text: str | None) -> str | None:
    if text is None:
        return None
    try:
        return _token_key_from_auth(json.loads(text))
    except ValueError:
        return None


def _token_key_from_path(path: Path) -> str | None:
    try:
        return _token_key_from_auth(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _credential_aliases(profile: Path) -> list[Path]:
    token = _token_key_from_path(profile)
    identity = _identity_key(_read_claims(profile))
    if profile.parent != _account_dir() or token is None or identity is None:
        return [profile]
    return [
        candidate
        for candidate in sorted(_account_dir().glob("*.json"))
        if _token_key_from_path(candidate) == token
        and _identity_key(_read_claims(candidate)) == identity
    ]


def _active_profile(active_text: str | None = None) -> Path | None:
    text = active_text if active_text is not None else _read_active_auth_text()
    if text is None:
        return None
    active_token = _token_key_from_text(text)
    active_identity = _identity_key(_claims_from_text(text))
    marked = _marked_profile()
    if marked is not None:
        if active_token is not None and _token_key_from_path(marked) == active_token:
            return marked
        if active_identity is not None and _identity_key(_read_claims(marked)) == active_identity:
            return marked

    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    token_matches = [
        profile
        for profile in profiles
        if _token_key_from_path(profile) == active_token
        and (
            active_identity is None
            or _identity_key(_read_claims(profile)) is None
            or _identity_key(_read_claims(profile)) == active_identity
        )
    ]
    if active_token is not None and len(token_matches) == 1:
        return token_matches[0]
    identity_matches = [
        profile
        for profile in profiles
        if active_identity is not None and _identity_key(_read_claims(profile)) == active_identity
    ]
    return identity_matches[0] if len(identity_matches) == 1 else None


def _read_codex_auth_text() -> str | None:
    secret = _read_keychain_auth()
    if secret:
        try:
            json.loads(secret)
            return secret
        except ValueError:
            pass
    auth_path = _auth_file()
    if not auth_path.is_file():
        return None
    try:
        return auth_path.read_text(encoding="utf-8")
    except OSError:
        return None


def _read_active_claims() -> dict | None:
    text = _read_codex_auth_text()
    return _claims_from_text(text) if text else None


def _parse_last_refresh(text: str) -> float | None:
    """Epoch seconds of an auth JSON's last_refresh stamp; None if absent/bad.
    codex rewrites this stamp on every login and token refresh, so it orders
    the keychain item and auth.json by true freshness."""
    try:
        value = json.loads(text)["last_refresh"]
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _read_active_auth_text() -> str | None:
    """Raw active-auth JSON text — the NEWEST of the keychain item and
    auth.json, ordered by last_refresh. Neither source is trustworthy alone:
    codex rotates tokens keychain-only during normal use (auth.json goes
    stale), but `codex login` writes auth.json without touching the keychain
    item (keychain goes stale — a blind keychain-first read here overwrote
    fresh logins with pre-login tokens, observed live). Ties and unstamped
    sources prefer the keychain, codex's primary read source."""
    secret = _read_keychain_auth()
    if secret:
        try:
            json.loads(secret)
        except ValueError:
            secret = None
    file_text = None
    auth_path = _auth_file()
    if auth_path.is_file():
        try:
            file_text = auth_path.read_text(encoding="utf-8")
        except OSError:
            file_text = None
    if secret and file_text:
        file_ts = _parse_last_refresh(file_text)
        keychain_ts = _parse_last_refresh(secret)
        if file_ts is not None and (keychain_ts is None or file_ts > keychain_ts):
            return file_text
        return secret
    return secret or file_text


def _copy_active_auth_to(dest: Path) -> None:
    """Copy the active codex auth to dest.
    Falls back to a plain auth.json file copy if the active auth can't be
    read as text at all. Refuses to write when dest already holds a DIFFERENT
    account — fold-back/sync callers only ever sync the same account, and a
    cross-account write destroys dest's only token copy (observed live when
    a stale keychain diverged from auth.json)."""
    text = _read_active_auth_text()
    if text is None:
        for alias in _credential_aliases(dest):
            shutil.copy2(_auth_file(), alias)
            alias.chmod(0o600)
        return
    if dest.is_file():
        dest_key = _identity_key(_read_claims(dest))
        text_key = _identity_key(_claims_from_text(text))
        if dest_key is not None and text_key is not None and dest_key != text_key:
            log_yellow(
                f"⚠️  Not syncing active auth into {dest.name}: it belongs to a different account."
            )
            return
    for alias in _credential_aliases(dest):
        alias.write_text(text, encoding="utf-8")
        alias.chmod(0o600)


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


def _list_expiry_status(claims: dict | None) -> tuple[str, str]:
    if not claims or not claims.get("expires_epoch"):
        return "—", DIM
    now = datetime.now().timestamp()
    exp = claims["expires_epoch"]
    text = codex_usage.format_unix_time_compact(int(exp))
    if exp <= now:
        return f"{text} expired", RED
    if exp - now < 24 * 3600:
        return f"{text} soon", YELLOW
    return text, GREEN


def _token_expired_or_soon(claims: dict | None) -> bool:
    """True when the access_token is expired or within 24h of expiry. Unknown
    expiry returns False — we won't force a network call on a token we can't
    judge (and revocation, unlike expiry, isn't visible in the claims anyway)."""
    if not claims or not claims.get("expires_epoch"):
        return False
    return claims["expires_epoch"] - datetime.now().timestamp() < 24 * 3600


# ── OAuth token refresh (mirrors codex-rs login/src/auth/manager.rs) ────────

# Verified against openai/codex codex-rs/login/src/auth/manager.rs:
# REFRESH_TOKEN_URL + public CLIENT_ID; refresh requests carry no scope.
_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _oauth_refresh(refresh_token: str) -> tuple[dict | None, str | None]:
    """Exchange a refresh_token for fresh tokens. Returns (response, None) on
    success, (None, error message) on failure — never raises, never logs tokens."""
    request = urllib.request.Request(
        _OAUTH_TOKEN_URL,
        data=json.dumps(
            {
                "client_id": _OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from token endpoint (refresh token may be expired or revoked)"
    except urllib.error.URLError as exc:
        return None, f"network error: {exc.reason}"
    except Exception as exc:  # malformed JSON response, etc.
        return None, str(exc)


def _read_refresh_token(path: Path) -> str | None:
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    tokens = auth.get("tokens") or {}
    return tokens.get("refresh_token") or auth.get("refresh_token")


def _apply_refreshed_tokens(path: Path, refreshed: dict) -> None:
    """Write refreshed token fields into an auth-format JSON file in place."""
    auth = json.loads(path.read_text(encoding="utf-8"))
    tokens = auth.setdefault("tokens", {})
    for key in ("id_token", "access_token", "refresh_token"):
        if refreshed.get(key):
            tokens[key] = refreshed[key]
    # Same shape codex writes (chrono Utc::now() → RFC3339 with Z suffix).
    auth["last_refresh"] = (
        datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    path.write_text(json.dumps(auth, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    # Keep codex's keychain in lock-step when we just rewrote the active auth
    # (no-op for profile files and off-macOS).
    _mirror_active_auth_to_keychain(path)


def _is_revoked_error(error: str | None) -> bool:
    """True when the token endpoint outright rejected the refresh_token (HTTP
    4xx) — a genuine revocation, as opposed to a transient network/server
    hiccup (URLError, HTTP 5xx, timeout)."""
    return (error or "").startswith("HTTP 4")


def _refresh_file(path: Path, label: str, relogin_hint: str | None = None) -> tuple[dict | None, str | None]:
    """Refresh the tokens stored in one auth-format file.

    Returns (response, None) on success. On failure returns (None, kind):
    kind is "revoked" when the refresh_token is missing or was rejected
    outright (HTTP 4xx) — only a fresh login helps — or "transient" for a
    network/server hiccup (URLError, HTTP 5xx, timeout) — safe to retry later.
    relogin_hint overrides the default per-profile guidance (the active auth
    isn't a profile name, so `login-switch {label}` would be nonsensical there).
    """
    hint = relogin_hint or f"Re-login with: codex-accounts login-switch {label}"
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
        log_yellow("   Token endpoint unreachable — retry later.")
        return None, "transient"
    aliases = _credential_aliases(path)
    _apply_refreshed_tokens(path, refreshed)
    for alias in aliases:
        if alias != path:
            shutil.copy2(path, alias)
            alias.chmod(0o600)
    return refreshed, None


def _sync_refreshed_profile(profile_path: Path) -> bool:
    if _active_profile() != profile_path:
        return False
    auth_path = _auth_file()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(profile_path, auth_path)
    auth_path.chmod(0o600)
    _mirror_active_auth_to_keychain(auth_path)
    _set_current_profile(profile_path)
    return True


def _recover_switched_auth(auth_path: Path, profile_file: Path, name: str) -> int:
    """Bring a just-restored but expired token back to life without a browser:
    OAuth-refresh in place and mirror the rotated result into the profile so it
    stays live. Fallback ladder for the cases refresh can't fix:

    - refresh_token revoked/invalid (auth rejection, HTTP 4xx) or absent → only
      a fresh login helps, so escalate to `login-switch` (interactive browser
      flow; Codex-scoped, does not affect the ChatGPT web session).
    - network / server hiccup (URLError, HTTP 5xx) → non-fatal: leave the tokens
      in place and let codex self-refresh on next use. A transient blip must not
      trigger a disruptive re-login.

    Returns the process exit code to propagate from `switch`."""
    refresh_token = _read_refresh_token(auth_path)
    if refresh_token:
        refreshed, error = _oauth_refresh(refresh_token)
        if refreshed is not None:
            _apply_refreshed_tokens(auth_path, refreshed)
            _copy_active_auth_to(profile_file)
            print(f"{DIM}   (token was expired — refreshed in place){RESET}")
            return 0
        # Only a genuine auth rejection (4xx) means the refresh_token is dead;
        # network errors and 5xx are transient and must not pop a browser.
        if not _is_revoked_error(error):
            log_yellow(f"⚠️  Could not refresh after switch ({error}); codex will retry on next use.")
            return 0

    log_yellow(f"⚠️  Saved token for '{name}' is revoked — re-logging in via browser…")
    return cmd_login_switch(name)


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


def _claims_lines(claims: dict | None) -> list[str]:
    if claims is None:
        return [f"{YELLOW}No auth file found.{RESET} Run: {BOLD}codex login{RESET}"]

    has_any = any(
        claims.get(k)
        for k in ("email", "name", "plan", "account_id", "organization_id", "issuer", "expires_str")
    )
    if not has_any:
        return [f"{YELLOW}No readable account claims found.{RESET}"]

    lines = [f"{BOLD}Account{RESET}       : {_identity_label(claims)}"]
    if claims.get("plan"):
        lines.append(f"{DIM}Plan{RESET}          : {claims['plan']}")
    if claims.get("account_id"):
        lines.append(f"{DIM}Account ID{RESET}    : {claims['account_id']}")
    if claims.get("organization_id"):
        lines.append(f"{DIM}Organization{RESET}  : {claims['organization_id']}")
    if claims.get("issuer"):
        lines.append(f"{DIM}Issuer{RESET}        : {claims['issuer']}")
    expires_text, color = _expiry_status(claims)
    lines.append(f"{DIM}Expires{RESET}       : {color}{expires_text}{RESET}")
    return lines


def _short_id(value: str | None) -> str:
    if not value:
        return f"{DIM}—{RESET}"
    if len(value) <= 14:
        return value
    return f"{value[:8]}…{value[-4:]}"


def _usage_color(percentage: int) -> str:
    if percentage >= 80:
        return RED + BOLD
    if percentage >= 50:
        return YELLOW
    return GREEN


def _usage_cell(window: codex_usage.UsageWindow | None, window_kind: str) -> str:
    if window is None:
        return f"{DIM}—{RESET}"
    percent = f"{_usage_color(window.percentage)}{window.percentage}%{RESET}"
    return codex_usage.format_usage_window(window, window_kind, percent)


def _align_usage_cells(rows: list[dict[str, str]], key: str) -> None:
    matches = [match for row in rows if (match := _USAGE_CELL_RE.match(row[key]))]
    if not matches:
        return
    widths = {
        name: max(len(match.group(name) or "") for match in matches)
        for name in ("percent", "days", "hours", "minutes")
    }
    for row, match in zip(rows, (_USAGE_CELL_RE.match(row[key]) for row in rows), strict=True):
        if match is None:
            continue
        days = f"{(match.group('days') or '').rjust(widths['days'])}d " if widths["days"] else ""
        row[key] = (
            f"{match.group('color')}{match.group('percent').rjust(widths['percent'])}%"
            f"{match.group('reset') or ''} · {days}"
            f"{match.group('hours').rjust(widths['hours'])}h "
            f"{match.group('minutes').rjust(widths['minutes'])}m"
        )


def _print_accounts_table(rows: list[dict]) -> None:
    headers = ["PROFILE", "ACCOUNT", "PLAN", "ID", "5H USED", "1W USED", "UPDATED", "AUTH", "STATE"]
    keys = ["profile", "account", "plan", "account_id", "usage_5h", "usage_1week", "usage_updated", "expires", "status"]
    _align_usage_cells(rows, "usage_5h")
    _align_usage_cells(rows, "usage_1week")
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
    _panel("Current Auth Claims", _claims_lines(_read_active_claims()))
    return 0


def _save_profile_auth(name: str, auth_text: str) -> int:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1

    _account_dir().mkdir(parents=True, exist_ok=True)
    _account_dir().chmod(0o700)
    for alias in _credential_aliases(profile_file):
        alias.write_text(auth_text, encoding="utf-8")
        alias.chmod(0o600)

    # Converge auth.json AND the keychain mirror to the same content:
    # `codex login` writes auth.json without updating the keychain item, so
    # without the mirror the next keychain-first read (ours or codex's own)
    # would resurrect pre-login tokens.
    _auth_file().write_text(auth_text, encoding="utf-8")
    _auth_file().chmod(0o600)
    _mirror_active_auth_to_keychain(_auth_file())
    _set_current_profile(profile_file)

    print(f"{GREEN}✅ Saved Codex profile:{RESET} {BOLD}{name}{RESET}")
    print(f"{DIM}   → {profile_file}{RESET}\n")
    _panel(f"Profile: {name}", _claims_lines(_read_claims(profile_file)), accent=GREEN)
    return 0


def cmd_save(name: str) -> int:
    auth_text = _read_active_auth_text()
    if auth_text is None:
        log_red(f"❌ No Codex auth file found: {_auth_file()}")
        log_yellow("   Run: codex login")
        return 1
    return _save_profile_auth(name, auth_text)


def cmd_list(*, fetch_usage: bool = True) -> int:
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Codex profiles.")
        print(f"{DIM}   Add one with: codex-accounts save <profile_name>{RESET}", file=sys.stderr)
        return 0

    active_text = _read_active_auth_text()
    profile_claims = [(profile_path, _read_claims(profile_path)) for profile_path in profiles]
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
            same_account_profiles.update(profile for profile in group if profile != primary)

    empty_usage = codex_usage.UsageSnapshot(
        hourly=None,
        weekly=None,
        refreshed_at=None,
        error=None,
    )
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
        expires_text, color = _list_expiry_status(claims)
        usage = empty_usage
        if fetch_usage:
            usage_path = profile_path
            if (
                is_active
                and active_text is not None
                and _token_key_from_path(_auth_file()) == _token_key_from_text(active_text)
            ):
                usage_path = _auth_file()
            usage = codex_usage.fetch_usage(usage_path)
            if usage.error and usage.error.startswith(("HTTP 401", "HTTP 403")):
                usage = empty_usage
        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{name}{RESET}" if is_active else name,
                "account": _identity_label(claims) if claims else f"{RED}(unreadable){RESET}",
                "plan": (claims or {}).get("plan") or f"{DIM}—{RESET}",
                "account_id": _short_id((claims or {}).get("account_id")),
                "usage_5h": _usage_cell(usage.hourly, "5h"),
                "usage_1week": _usage_cell(usage.weekly, "1week"),
                "usage_updated": codex_usage.format_refreshed_at(usage),
                "expires": f"{color}{expires_text}{RESET}",
                "status": status,
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
        # Before overwriting, fold any token rotation codex performed on the
        # active account back into its own saved profile. Codex rotates (and
        # revokes) the refresh_token during normal use; without this, the
        # profile keeps the revoked token, and a later `switch` back to it
        # restores dead credentials — codex then fails MCP startup with
        # HTTP 401 "token_revoked". Keeping the profile current makes switch
        # idempotent. Pure local file op; no network.
        outgoing_profile = _active_profile(_read_active_auth_text())
        if outgoing_profile is not None:
            _copy_active_auth_to(outgoing_profile)

        backup_path = auth_path.with_name(f"{auth_path.name}.backup")
        shutil.copy2(auth_path, backup_path)
        backup_path.chmod(0o600)

    shutil.copy2(profile_file, auth_path)
    auth_path.chmod(0o600)
    # codex reads the keychain before auth.json on macOS — without this mirror
    # the copy above is silently ignored and codex keeps the old account.
    _mirror_active_auth_to_keychain(auth_path)
    _set_current_profile(profile_file)

    print(f"{GREEN}✅ Switched Codex profile to:{RESET} {BOLD}{name}{RESET}")
    if backup_path:
        print(f"{DIM}   (previous auth backed up to {backup_path}){RESET}")

    # Self-heal an expired snapshot before codex (and its MCP clients like
    # codex_apps) start with a dead access_token. Only fires when the restored
    # token is expired/near-expiry, so a normal switch stays offline and instant.
    if _token_expired_or_soon(_read_claims(auth_path)):
        rc = _recover_switched_auth(auth_path, profile_file, name)
        if rc != 0:
            return rc

    print()
    return cmd_who()


def cmd_switch_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    now = datetime.now().timestamp()
    valid_profiles = []
    for profile in profiles:
        claims = _read_claims(profile)
        expires_epoch = claims.get("expires_epoch") if claims else None
        if expires_epoch is not None and expires_epoch > now:
            valid_profiles.append((profile, claims))

    if not valid_profiles:
        log_yellow("⚠️  No unexpired Codex profiles available to switch.")
        return 1

    print(f"{BOLD}Choose a Codex profile:{RESET}")
    for index, (profile, claims) in enumerate(valid_profiles, start=1):
        print(f"  {index}) {profile.stem}  {DIM}{_identity_label(claims)}{RESET}")

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
    if selected_index < 0 or selected_index >= len(valid_profiles):
        log_red("❌ Enter one of the account numbers shown above.")
        return 1
    return cmd_switch(valid_profiles[selected_index][0].stem)


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
    print(f"{GREEN}✅ Removed Codex profile:{RESET} {name}")
    return 0


def _refresh_one_profile(name: str, *, show_summary: bool = True) -> tuple[int, str | None]:
    """Refresh one saved profile. Returns (exit_code, failure_kind) — kind is
    None on success, else "revoked" or "transient" (see _refresh_file)."""
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
        print(f"{GREEN}✅ Refreshed Codex profile:{RESET} {BOLD}{name}{RESET}")
    synced_active = _sync_refreshed_profile(profile_file)
    if show_summary and synced_active:
        print(f"{DIM}   (same account is active — auth.json updated too){RESET}")

    if show_summary:
        print()
        _panel(f"Profile: {name}", _claims_lines(_read_claims(profile_file)), accent=GREEN)
    return 0, None


def _refresh_all_profiles() -> int:
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Codex profiles to refresh.")
        return 0

    revoked = []
    transient = []
    profile_tokens = [(profile_path, _token_key_from_path(profile_path)) for profile_path in profiles]
    refreshed_tokens: set[str] = set()
    for profile_path, token in profile_tokens:
        if token is not None and token in refreshed_tokens:
            continue
        if token is not None:
            refreshed_tokens.add(token)
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
        log_red(f"❌ No Codex auth file found: {auth_path}")
        log_yellow("   Run: codex login")
        return 1

    profile_path = _active_profile(_read_active_auth_text())
    refreshed, _kind = _refresh_file(
        auth_path, "the active auth", relogin_hint="Re-login with: codex login"
    )
    if refreshed is None:
        return 1
    print(f"{GREEN}✅ Refreshed active Codex auth.{RESET}")

    if profile_path is not None:
        _copy_active_auth_to(profile_path)
        _set_current_profile(profile_path)
        print(f"{DIM}   (synced back to profile: {profile_path.stem}){RESET}")
    else:
        log_yellow("⚠️  No unambiguous current profile — run: codex-accounts switch <name>")

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
        log_red(f"❌ No Codex auth file found: {auth_path}")
        log_yellow("   Run: codex login")
        return 1

    profile_path = _active_profile(_read_active_auth_text())
    if profile_path is None:
        log_red("❌ No unambiguous current profile.")
        log_yellow("   Select it first with: codex-accounts switch <name>")
        return 1

    _copy_active_auth_to(profile_path)
    _set_current_profile(profile_path)
    print(f"{GREEN}✅ Synced active auth → profile:{RESET} {BOLD}{profile_path.stem}{RESET}")
    print()
    _panel(f"Profile: {profile_path.stem}", _claims_lines(_read_claims(profile_path)), accent=GREEN)
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
    with tempfile.TemporaryDirectory(prefix="codex-accounts-login-") as temp_dir:
        login_home = Path(temp_dir)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(login_home)
        env.pop("CODEX_ACCOUNT_DIR", None)
        env.pop("CODEX_AUTH_JSON", None)
        try:
            with _suppress_interrupt_echo():
                login = subprocess.run(
                    ["codex", "login", "-c", 'cli_auth_credentials_store="file"'],
                    env=env,
                )
        except KeyboardInterrupt:
            log_yellow("Login cancelled. Your current profile was not changed.")
            return None, 130
        if login.returncode != 0:
            log_red("❌ codex login did not complete successfully")
            return None, login.returncode
        try:
            return (login_home / "auth.json").read_text(encoding="utf-8"), 0
        except OSError as error:
            log_red(f"❌ codex login completed without a readable auth.json: {error}")
            return None, 1


def cmd_login_switch(name: str) -> int:
    if not ensure_tool("codex"):
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
            log_red("Usage: codex-accounts save <profile_name>")
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
            log_red("Usage: codex-accounts remove <profile_name>")
            return 1
        return cmd_remove(rest[0])
    if command == "refresh":
        return cmd_refresh(rest[0] if rest else None)
    if command == "sync":
        return cmd_sync()
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
