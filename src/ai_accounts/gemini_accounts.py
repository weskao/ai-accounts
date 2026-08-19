from __future__ import annotations

import base64
from contextlib import nullcontext
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import TypeAlias

from . import autoswitch, gemini_usage
from . import config_menu as cm
from ._present import (
    _ANSI_RE as _ANSI_RE,
    accounts_table,
    choose_and_run,
    choose_profile,
    ok,
    panel,
    success_panel,
    usage_color,
)
from ._utils import (
    BOLD,
    DIM,
    GREEN,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
    Spinner,
    atomic_write_json,
    email_local_part,
    ensure_tool,
    have,
    log_red,
    log_yellow,
    oauth_token_refresh,
    plan_tier_color,
    resolve_data_dir,
)
from .usage_format import (
    UsageWindow,
    capitalize_first,
    credential_status_prefix,
    format_unix_time_compact,
    format_usage_window,
    print_no_active_account,
)

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonDict: TypeAlias = dict[str, JsonValue]
Claims: TypeAlias = dict[str, str | int | bool | None]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


HELP = """agy-accounts — manage multiple Antigravity OAuth profiles

PLATFORM
  macOS only (official agy session is stored in macOS Keychain)

USAGE
  agy-accounts who                   Show the selected Antigravity account
  agy-accounts current               Alias for `who`
  agy-accounts save [<name>]         Save the current login as a reusable profile;
                                     no name = derive it from the active account's
                                     email (needs one quota lookup)
  agy-accounts list                  List saved profiles (table view)
  agy-accounts usage                 Show only the active account's quota row
  agy-accounts switch [<name>]       Switch by name; no name = interactive picker
  agy-accounts remove [<name>]       Delete by name; no name = interactive picker
  agy-accounts refresh [<name>]      Renew tokens via the Google OAuth refresh grant
                                     (no browser, no agy launch; falls back to agy);
                                     no name = refresh active session + sync it back
  agy-accounts refresh --all         Refresh every saved profile
  agy-accounts sync                  Copy the active auth back to its matching profile
  agy-accounts autoswitch            Leave the active account when its quota runs out
                                     (only the live session's quota is readable, so
                                     switching needs "agy_blind_switch": true)
  agy-accounts login-switch <name>   Antigravity Google login + save as <name>
  agy-accounts config                Interactive config menu shared by every ai-accounts CLI
                                     (works on every platform, not just macOS)
  agy-accounts config get [key]      Print the shared auto-switch config (or one key)
  agy-accounts config set <k> <v>    Set one shared config key (rejects unknown keys)
  agy-accounts -h | --help | help    Show this help

EXAMPLES
  agy-accounts login-switch personal
  agy-accounts login-switch work
  agy-accounts list
  agy-accounts switch
  agy-accounts switch personal
  agy-accounts save
  agy-accounts remove
  agy-accounts refresh --all
  agy-accounts who

Profiles live under ~/.ai-accounts/antigravity/accounts/<name>.json.
Treat that directory as secrets — saved profiles contain Google OAuth tokens.
"""


# ── paths ─────────────────────────────────────────────────────────────────
def _antigravity_dir() -> Path:
    return resolve_data_dir(
        "ANTIGRAVITY_HOME",
        Path.home() / ".ai-accounts" / "antigravity",
        Path.home() / ".codexbar" / "antigravity",
        label="Antigravity store",
    )


def _account_dir() -> Path:
    override = os.environ.get("ANTIGRAVITY_ACCOUNT_DIR")
    return Path(override) if override else _antigravity_dir() / "accounts"


def _auth_file() -> Path:
    return Path(
        os.environ.get(
            "ANTIGRAVITY_OAUTH_JSON", str(_antigravity_dir() / "oauth_creds.json")
        )
    )


_KEYCHAIN_SERVICE = "gemini"
_KEYCHAIN_ACCOUNT = "antigravity"
_KEYRING_PREFIX = "go-keyring-base64:"

# Upper bound on how long login-switch waits for a genuinely new credential.
# A running Antigravity IDE re-writes the deleted keyring session instantly, so
# without a ceiling the poll loop would spin forever waiting for a login the
# CLI can't trigger on its own.
_LOGIN_TIMEOUT_SECONDS = 180


def _read_cli_keyring_secret() -> str | None:
    try:
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
    except OSError:
        return None
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
    try:
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
    except OSError:
        return False
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
    try:
        atomic_write_json(_auth_file(), auth)
    except OSError:
        return False
    return True


def _delete_cli_auth() -> bool:
    try:
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
    except OSError:
        return False
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


# A genuine Antigravity/Google OAuth credential always carries at least one
# of these top-level keys (written by both the keyring round-trip and the
# OAuth refresh response). A file that parses as JSON but has none of them is
# a misfiled/foreign credential (e.g. an x.ai record dropped in this dir by
# mistake) rather than a merely-expired one — see plan.md §2.4.
_EXPECTED_OAUTH_KEYS = ("access_token", "refresh_token", "expiry_date")


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
        "malformed": not any(key in auth for key in _EXPECTED_OAUTH_KEYS),
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


# ── direct Google OAuth refresh (no agy subprocess) ─────────────────────────

# Antigravity's access token lives ~1h, so the proactive window has to be small:
# Codex's 24h skew would mark every healthy Antigravity token "about to expire".
_REFRESH_SKEW_SECONDS = 300

_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# The desktop client's OAuth id/secret ship in plain text inside the app bundle;
# they are a public-client credential pair, not a user secret, but they are still
# never printed — only handed to the token endpoint.
_APP_MAIN_JS = "Contents/Resources/app/out/main.js"
_CLIENT_ID_RE = re.compile(r"\d+-[A-Za-z0-9]{20,}\.apps\.googleusercontent\.com")
_CLIENT_SECRET_RE = re.compile(r"GOCSPX-[A-Za-z0-9_-]{20,}")

# Not an error the user should act on: it only means "use the agy CLI path".
_NO_CLIENT_CREDENTIALS = "no Google client credentials"


def _antigravity_app_paths() -> tuple[Path, ...]:
    return (
        Path("/Applications/Antigravity.app"),
        Path.home() / "Applications" / "Antigravity.app",
    )


@cache
def _client_credentials() -> tuple[str, str] | None:
    """(client_id, client_secret) for the Antigravity OAuth client, or None.

    Env override first, then discovery from the installed app bundle. Cached:
    `refresh --all` must not re-scan a 12 MB bundle once per profile. Returns
    None — never raises, never logs a value — when neither source resolves, and
    callers fall back to the agy CLI refresh."""
    client_id = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret
    for app in _antigravity_app_paths():
        try:
            blob = (app / _APP_MAIN_JS).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found_id = _CLIENT_ID_RE.search(blob)
        found_secret = _CLIENT_SECRET_RE.search(blob)
        if found_id and found_secret:
            return found_id.group(0), found_secret.group(0)
    return None


def _http_error_message(code: int, detail: str) -> str:
    """Antigravity's classifier for the shared refresh helper. Google answers
    400 for both a malformed request and a dead refresh token, so the JSON body
    decides — only ``invalid_grant`` means the token itself is gone."""
    error = ""
    try:
        body = json.loads(detail)
        if isinstance(body, dict):
            error = _string(body.get("error")) or ""
    except ValueError:
        pass
    if error == "invalid_grant":
        return "revoked: refresh token rejected (invalid_grant)"
    if error:
        # invalid_client / unauthorized_client etc. mean our *client* credentials
        # are wrong — a fresh user login would not fix that, so not "revoked".
        return f"HTTP {code} from token endpoint ({error})"
    return f"HTTP {code} from token endpoint"


def _is_revoked_error(error: str | None) -> bool:
    """True only when Google rejected the refresh token itself — the one case a
    browser re-login fixes. Network errors, 5xx and client-credential problems
    are transient/fallback-worthy, and must never pop a login."""
    return (error or "").startswith("revoked")


def _oauth_refresh(refresh_token: str) -> tuple[dict | None, str | None]:
    """Exchange a refresh_token for a fresh access token against Google.
    (response, None) on success, (None, error) on failure — never raises, never
    logs tokens. Google does not rotate the refresh token on this grant."""
    credentials = _client_credentials()
    if credentials is None:
        return None, _NO_CLIENT_CREDENTIALS
    client_id, client_secret = credentials
    return oauth_token_refresh(
        _OAUTH_TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        form_encoded=True,
        http_error=_http_error_message,
    )


def _apply_refreshed_auth(auth: JsonDict, refreshed: dict) -> JsonDict:
    """Merge a token response into a credential dict. ``expiry_date`` is Google's
    epoch-**milliseconds** field, recomputed from ``expires_in`` seconds."""
    updated = dict(auth)
    access_token = _string(refreshed.get("access_token"))
    if access_token:
        updated["access_token"] = access_token
    token_type = _string(refreshed.get("token_type"))
    if token_type:
        updated["token_type"] = token_type
    expires_in = refreshed.get("expires_in")
    if isinstance(expires_in, int | float):
        updated["expiry_date"] = int((time.time() + int(expires_in)) * 1000)
    return updated


def _direct_refresh_auth(auth: JsonDict) -> tuple[JsonDict | None, str | None]:
    """Refresh one parsed credential straight against Google.
    (updated credential, None) on success; (None, error) otherwise, where the
    error is `_NO_CLIENT_CREDENTIALS`, a "revoked: …" message, or transient."""
    refresh_token = _string(auth.get("refresh_token"))
    if not refresh_token:
        return None, "revoked: no refresh_token in the saved credential"
    refreshed, error = _oauth_refresh(refresh_token)
    if refreshed is None:
        return None, error
    return _apply_refreshed_auth(auth, refreshed), None


def _direct_refresh_profile(profile_file: Path) -> tuple[JsonDict | None, str | None]:
    """`_direct_refresh_auth` for a saved profile, persisting the result.

    On success the profile file is rewritten atomically and — when it is the
    live account — mirrored into the keyring blob (`expiry_date` ms → the
    keyring's ISO-8601 `expiry`) so agy sees the fresh token too."""
    try:
        auth = json.loads(profile_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable credential file"
    if not isinstance(auth, dict):
        return None, "unreadable credential file"
    updated, error = _direct_refresh_auth(auth)
    if updated is None:
        return None, error
    try:
        atomic_write_json(profile_file, updated)
    except OSError as exc:
        return None, f"could not write {profile_file.name}: {exc.strerror}"
    if _active_profile() == profile_file:
        _write_cli_auth_text(json.dumps(updated))
    return updated, None


def _token_expired_or_soon(claims: Claims | None) -> bool:
    """True when the access token is expired or inside `_REFRESH_SKEW_SECONDS`
    of expiring. Unknown expiry returns False — no network call on a token we
    cannot judge."""
    if not claims:
        return False
    expires_epoch = claims.get("expires_epoch")
    if not isinstance(expires_epoch, int):
        return False
    return expires_epoch - datetime.now().timestamp() < _REFRESH_SKEW_SECONDS


def _recover_switched_auth(profile_file: Path, name: str) -> None:
    """Bring a just-activated but expired token back to life without a browser.

    Best-effort by design — the switch itself already succeeded, so nothing here
    fails the command: a revoked refresh token escalates to a login hint, and a
    transient failure (or unresolvable client credentials) leaves the tokens in
    place for the next use to retry."""
    updated, error = _direct_refresh_profile(profile_file)
    if updated is not None:
        print(f"{DIM}   (token was expired — refreshed in place){RESET}")
        return
    if _is_revoked_error(error):
        log_yellow(f"⚠️  Saved token for '{name}' is revoked — a fresh login is needed.")
        print(f"{DIM}   Run: agy-accounts login-switch {name}{RESET}")
        return
    if error == _NO_CLIENT_CREDENTIALS:
        log_yellow(
            f"⚠️  '{name}' has an expired access token and the Google client "
            f"credentials could not be resolved."
        )
        print(f"{DIM}   Run: agy-accounts refresh {name}{RESET}")
        return
    log_yellow(f"⚠️  Could not refresh after switch ({error}); agy will retry on next use.")


# ── terminal rendering ───────────────────────────────────────────────────


def _expiry_status(claims: Claims | None) -> tuple[str, str]:
    """(display text, color) — color carries meaning but text never relies on
    it alone. Refreshable-first, matching ``_list_expiry_status``: a live
    refresh token means the account is healthy regardless of the access
    token's own expiry."""
    prefix = credential_status_prefix(claims)
    if prefix is not None:
        return prefix
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


def _list_expiry_status(claims: Claims | None) -> tuple[str, str]:
    prefix = credential_status_prefix(claims)
    if prefix is not None:
        return prefix
    if not claims:
        return "—", DIM
    expires_epoch = claims.get("expires_epoch")
    if not isinstance(expires_epoch, int):
        return "—", DIM
    text = format_unix_time_compact(expires_epoch)
    now = datetime.now().timestamp()
    if expires_epoch <= now:
        return f"{text} expired", RED
    if expires_epoch - now < 24 * 3600:
        return f"{text} soon", YELLOW
    return text, GREEN


def _claims_lines(claims: Claims | None) -> list[str]:
    if claims is None:
        return [
            f"{YELLOW}No auth file found.{RESET} Run: {BOLD}agy-accounts login-switch <name>{RESET}"
        ]
    if claims.get("malformed"):
        return [
            f"{YELLOW}Malformed credential file — not a recognizable "
            f"Antigravity/Google OAuth record.{RESET}"
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


def _plan_cell(plan: str | None) -> str:
    """Colored PLAN column value. Paid tier names aren't enumerable here (see
    ``gemini_usage._plan_label``), so — unlike claude/codex — every paid plan
    gets a single accent color rather than a fabricated rank; Free stays
    uncolored."""
    text = capitalize_first(plan)
    if not text:
        return f"{DIM}—{RESET}"
    if text.lower() == "free":
        return text
    return f"{plan_tier_color(text)}{text}{RESET}"


def _usage_cell(window: UsageWindow | None) -> str:
    if window is None:
        return f"{DIM}—{RESET}"
    percent = f"{usage_color(window.percentage)}{window.percentage}%{RESET}"
    if window.reset_time is None:
        return percent
    window_kind = "5h" if window.window_minutes == 5 * 60 else "weekly"
    return format_usage_window(window, window_kind, percent)


# Optional columns hide entirely when every row renders "—" for that key; the
# four usage columns right-align via the shared renderer's align_keys. The
# shared renderer owns borders/widths/padding.
_TABLE_COLUMNS = [
    ("PROFILE", "profile"),
    ("ACCOUNT", "account"),
    ("ID", "account_id"),
    ("PLAN", "plan"),
    ("GEMINI 5H USED", "gemini_5h"),
    ("GEMINI 1W USED", "gemini_weekly"),
    ("CLAUDE/GPT 5H USED", "other_5h"),
    ("CLAUDE/GPT 1W USED", "other_weekly"),
    ("UPDATED", "usage_updated"),
    ("SESSION", "expires"),
    ("STATE", "status"),
]
_OPTIONAL_COLUMNS = frozenset(
    {"account_id", "gemini_5h", "gemini_weekly", "other_5h", "other_weekly"}
)
_ALIGN_KEYS = ("gemini_5h", "gemini_weekly", "other_5h", "other_weekly")


def _print_accounts_table(rows: list[dict[str, str]]) -> None:
    accounts_table(
        rows, _TABLE_COLUMNS, optional_columns=_OPTIONAL_COLUMNS, align_keys=_ALIGN_KEYS
    )


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
    panel("Antigravity Login Status", status_lines)

    print()
    panel("Current Auth Claims", _claims_lines(claims))
    return 0


def _backfill_email(profile_file: Path, email: str | None = None) -> None:
    """agy's keyring token carries no id_token or email, so a freshly saved
    profile has no identity — the panel/table would show "(unknown)". Email only
    ever comes from the usage RPC (the same source `list` and `refresh` use);
    fetch it now and write it into the profile. Best-effort: a transient fetch
    failure just leaves the email to be backfilled on the next list/refresh.

    ``email`` short-circuits that fetch for callers that already know it —
    name-less ``save`` has to look the email up *before* it can pick a filename,
    and must not pay for the same RPC twice."""
    if not email:
        email = gemini_usage.fetch_usage(timeout=8).email
    if not email:
        return
    try:
        saved = json.loads(profile_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict) or saved.get("email") == email:
        return
    saved["email"] = email
    text = json.dumps(saved, indent=2) + "\n"
    for path in (profile_file, _auth_file()):
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)


def _save_profile_auth(name: str, auth_text: str, known_email: str | None = None) -> int:
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

    # Name-less `save` already paid for the identity lookup; skip both the second
    # RPC and the spinner that would advertise one.
    with nullcontext() if known_email else Spinner("Fetching account identity…"):
        _backfill_email(profile_file, known_email)

    success_panel(
        "Saved Antigravity profile",
        name,
        _claims_lines(_read_claims(profile_file)),
        title=f"Profile: {name}",
        details=(f"→ {profile_file}",),
    )
    return 0


def cmd_save(name: str | None = None) -> int:
    auth_text = _read_active_auth_text()
    if auth_text is None:
        log_red(f"❌ No Antigravity auth file found: {_auth_file()}")
        log_yellow("   Run: agy-accounts login-switch <profile_name>")
        return 1
    known_email: str | None = None
    if name is None:
        # Unlike the other providers, agy's session carries no email, so the
        # filename can only come from the usage RPC — fetch before writing
        # anything (a failed lookup must leave no half-named profile behind) and
        # hand the result to _save_profile_auth so the backfill doesn't re-fetch.
        with Spinner("Fetching account identity…"):
            known_email = gemini_usage.fetch_usage(timeout=8).email
        if not known_email:
            log_red(
                "❌ Could not derive a name from the active account (no email found) "
                "-- pass a name explicitly."
            )
            return 1
        name = email_local_part(known_email)
    return _save_profile_auth(name, auth_text, known_email)


def _validated_usage(
    usage: gemini_usage.UsageSnapshot, claims: Claims | None
) -> gemini_usage.UsageSnapshot:
    expected = _string((claims or {}).get("email"))
    if expected and usage.email and expected.casefold() != usage.email.casefold():
        return gemini_usage.UsageSnapshot(
            None, None, None, None, None, None, None, "re-login required"
        )
    return usage


def cmd_list(*, fetch_usage: bool = True, only_active: bool = False) -> int:
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
    if only_active:
        if active_profile is None:
            print_no_active_account("Antigravity", "agy-accounts")
            return 0
        # Filter before the fetch loop so only the active profile's session is
        # activated and queried (the loop temporarily writes each profile's auth).
        profile_claims = [(p, c) for p, c in profile_claims if p == active_profile]

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
    spinner = Spinner("Fetching Antigravity usage…")
    try:
        with spinner if fetch_usage else nullcontext():
            for index, (profile_path, claims) in enumerate(profile_claims, 1):
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
                    spinner.update(
                        f"Fetching Antigravity usage… {DIM}({index}/{len(profile_claims)}){RESET} {MAGENTA}{name}{RESET}"
                    )
                    profile_text = profile_path.read_text(encoding="utf-8")
                    if _write_cli_auth_text(profile_text):
                        usage = _validated_usage(
                            gemini_usage.fetch_usage(timeout=8), claims
                        )
                        refreshed_text = (
                            _read_active_auth_text() if usage.error is None else None
                        )
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
                expires_text, expires_color = _list_expiry_status(claims)
                rows.append(
                    {
                        "profile": f"{GREEN}{BOLD}{name}{RESET}" if is_active else name,
                        "account": _identity_label(claims)
                        if claims
                        else usage.email or f"{RED}(unreadable){RESET}",
                        "account_id": _short_id(_string((claims or {}).get("account_id"))),
                        "plan": _plan_cell(usage.plan),
                        "gemini_5h": _usage_cell(usage.gemini_session),
                        "gemini_weekly": _usage_cell(usage.gemini_weekly),
                        "other_5h": _usage_cell(usage.other_session),
                        "other_weekly": _usage_cell(usage.other_weekly),
                        "usage_updated": gemini_usage.format_refreshed_at(usage),
                        "expires": f"{expires_color}{expires_text}{RESET}",
                        "status": status,
                    }
                )
    finally:
        _restore_cli_auth(restore_text)

    if only_active:
        print(f"{BOLD}Current Antigravity account{RESET}")
    else:
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

    ok("Switched Antigravity profile to", name)
    print(f"{DIM}   agy will use this account on its next launch.{RESET}")

    # Self-heal an expired snapshot in place so agy starts with a live token —
    # a profile parked for over an hour always has a dead access token.
    if _token_expired_or_soon(_read_claims(profile_file)):
        _recover_switched_auth(profile_file, name)

    print()
    return cmd_who()


def cmd_switch_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Antigravity profiles available to switch.")
        return 1

    chosen = choose_profile(
        "an Antigravity",
        [(profile.stem, _identity_label(_read_claims(profile))) for profile in profiles],
    )
    if chosen is None:
        return 1
    return cmd_switch(chosen)


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
    ok("Removed Antigravity profile", name, bold=False)
    return 0


def cmd_remove_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Antigravity profiles available to remove.")
        return 1

    return choose_and_run(
        "an Antigravity",
        [(profile.stem, _identity_label(_read_claims(profile))) for profile in profiles],
        cmd_remove,
        cancel_message="Remove cancelled.",
    )


def _refresh_one_profile(name: str, *, show_summary: bool = True) -> tuple[int, str | None]:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1, None
    if not profile_file.is_file():
        log_red(f"❌ Profile not found: {name}")
        print()
        cmd_list()
        return 1, None

    # Fast path: one HTTPS POST to Google. Only a transient failure or
    # unresolvable client credentials falls through to the agy subprocess below.
    updated, error = _direct_refresh_profile(profile_file)
    if updated is not None:
        if show_summary:
            success_panel(
                "Refreshed Antigravity profile",
                name,
                _claims_lines(_read_claims(profile_file)),
                title=f"Profile: {name}",
            )
        return 0, None
    if _is_revoked_error(error):
        log_red(f"❌ Refresh token revoked/dead for {name}: {error}")
        log_yellow(f"   Re-login with: agy-accounts login-switch {name}")
        return 1, "revoked"
    if error != _NO_CLIENT_CREDENTIALS:
        log_yellow(f"⚠️  Direct Google refresh failed for {name} ({error}) — trying agy.")

    original_text = _read_active_auth_text()
    is_active = _active_profile(original_text) == profile_file
    profile_text = profile_file.read_text(encoding="utf-8")
    if not _write_cli_auth_text(profile_text):
        log_red(f"❌ Could not activate agy profile: {name}")
        return 1, "keyring"
    usage = _validated_usage(gemini_usage.fetch_usage(), _read_claims(profile_file))
    refreshed_text = _read_active_auth_text() if usage.error is None else None
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
        success_panel(
            "Refreshed Antigravity profile",
            name,
            _claims_lines(_read_claims(profile_file)),
            title=f"Profile: {name}",
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
    ok(f"All {len(profiles)} profile(s) refreshed.")
    return 0


def _refresh_active_auth() -> int:
    active_text = _read_active_auth_text()
    if active_text is None:
        log_red("❌ No Antigravity CLI session found in the keyring")
        log_yellow("   Run: agy-accounts login-switch <profile_name>")
        return 1

    profile_path = _active_profile(active_text)

    # Fast path: refresh the live credential straight against Google, then fold
    # it back into its profile. Falls through to agy only when that can't run.
    updated, error = _direct_refresh_auth(json.loads(active_text))
    if updated is not None:
        if not _write_cli_auth_text(json.dumps(updated)):
            log_red("❌ Could not update the agy CLI keyring session")
            return 1
        if profile_path is not None:
            saved = json.loads(profile_path.read_text(encoding="utf-8"))
            saved.update(updated)
            atomic_write_json(profile_path, saved)
            _set_current_profile(profile_path)
            details = (f"(synced back to profile: {profile_path.stem})",)
        else:
            log_yellow(
                "⚠️  No unambiguous current profile — run: agy-accounts switch <name>"
            )
            details = ()
        success_panel(
            "Refreshed active Antigravity auth.",
            None,
            _claims_lines(_read_claims(_auth_file())),
            title="Current Auth Claims",
            details=details,
        )
        return 0
    if _is_revoked_error(error):
        log_red(f"❌ Refresh token revoked/dead: {error}")
        log_yellow("   Re-login with: agy-accounts login-switch <profile_name>")
        return 1
    if error != _NO_CLIENT_CREDENTIALS:
        log_yellow(f"⚠️  Direct Google refresh failed ({error}) — trying agy.")

    claims = _read_claims(profile_path) if profile_path is not None else None
    usage = _validated_usage(gemini_usage.fetch_usage(), claims)
    if usage.error:
        if usage.error == "re-login required":
            _restore_cli_auth(active_text)
        log_red(f"❌ agy refresh failed: {usage.error}")
        return 1
    refreshed_text = _read_active_auth_text()

    if profile_path is not None and refreshed_text is not None:
        saved = json.loads(profile_path.read_text(encoding="utf-8"))
        saved.update(json.loads(refreshed_text))
        if usage.email:
            saved["email"] = usage.email
        profile_path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        profile_path.chmod(0o600)
        _set_current_profile(profile_path)
        details = (f"(synced back to profile: {profile_path.stem})",)
    else:
        log_yellow(
            "⚠️  No unambiguous current profile — run: agy-accounts switch <name>"
        )
        details = ()

    success_panel(
        "Refreshed active Antigravity auth.",
        None,
        _claims_lines(_read_claims(_auth_file())),
        title="Current Auth Claims",
        details=details,
    )
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
    success_panel(
        "Synced active auth → profile",
        profile_path.stem,
        _claims_lines(_read_claims(profile_path)),
        title=f"Profile: {profile_path.stem}",
    )
    return 0


def _terminate_login(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def cmd_login_switch(name: str) -> int:
    if not ensure_tool("agy"):
        return 1
    if _profile_file(name) is None:
        return 1
    outgoing_text = _read_active_auth_text()
    outgoing_profile = _active_profile(outgoing_text)
    if outgoing_profile is not None:
        _copy_active_auth_to(outgoing_profile)
    outgoing_key = _token_key_from_auth(json.loads(outgoing_text)) if outgoing_text else None
    _delete_cli_auth()
    print(
        f"{DIM}Launching the official agy login. Complete browser sign-in; the profile will save automatically.{RESET}"
    )
    # A real login must yield a credential whose token differs from the outgoing
    # one. Breaking on *any* keyring write instead captures the session a running
    # Antigravity IDE restores the instant we delete it — silently re-saving the
    # current account under the new name. Require a genuinely new token, bounded
    # by a timeout so a restore-only keyring can't hang us.
    new_auth_text = None
    deadline = time.monotonic() + _LOGIN_TIMEOUT_SECONDS
    login = subprocess.Popen(["agy"])
    try:
        while login.poll() is None and time.monotonic() < deadline:
            candidate = _read_active_auth_text()
            if candidate is not None:
                candidate_key = _token_key_from_auth(json.loads(candidate))
                if candidate_key is not None and candidate_key != outgoing_key:
                    new_auth_text = candidate
                    break
            time.sleep(0.25)
    except KeyboardInterrupt:
        _terminate_login(login)
        _restore_cli_auth(outgoing_text)
        log_yellow("Login cancelled. Your previous agy session was restored.")
        return 130

    if new_auth_text is not None:
        _terminate_login(login)
        return _save_profile_auth(name, new_auth_text)

    agy_exit = login.poll()
    _terminate_login(login)
    _restore_cli_auth(outgoing_text)
    log_red("❌ Login did not complete — still on the current account.")
    log_yellow(
        "   This agy build has no CLI login, and a running Antigravity IDE restores the old session on delete."
    )
    log_yellow(
        f"   Quit the Antigravity app and retry, or sign in via the IDE and run: agy-accounts save {name}"
    )
    return agy_exit or 1


def _live_used_pct(snapshot: gemini_usage.UsageSnapshot) -> int | None:
    """Worst used percentage across *snapshot*'s windows — None when unknown.

    The account is throttled as soon as any one window runs out, so the
    fullest window is the one that decides whether it is time to move.
    """
    used = [
        window.percentage
        for window in (
            snapshot.gemini_weekly,
            snapshot.gemini_session,
            snapshot.other_weekly,
            snapshot.other_session,
        )
        if window is not None
    ]
    return max(used) if used else None


def _antigravity_ide_running() -> bool:
    """Whether an Antigravity IDE process is up — a read-only process query.

    The IDE re-writes the keyring session instantly and has no programmatic
    resume, so a switch made while it runs must degrade to manual-restart
    (docs/autoswitch-hot-reload-spike.md, agy section, footnote 2). Never
    signals, kills or otherwise touches a process: pgrep only reads the process
    table. Fails CLOSED — with no way to look, assume the IDE is there.
    """
    # ponytail: matches the macOS .app bundle path; widen if the IDE ships
    # under a different process name elsewhere.
    if not have("pgrep"):
        return True
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Antigravity.app"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return True
    return result.returncode == 0


def _autoswitch_resume() -> bool:
    """Start a NEW agy CLI session continuing the last conversation.

    A "restart" here is only ever an addition: `agy --continue`
    (docs/autoswitch-hot-reload-spike.md, agy section) opens a fresh process
    that picks up the previous conversation. No running process is signalled,
    terminated or otherwise touched. Only reached on the auto-restart rung,
    which autoswitch.build_restart withholds from an unattended run and from
    one with the Antigravity IDE up.
    """
    if not have("agy"):
        log_yellow("⚠️  agy is not on PATH — cannot resume the session automatically.")
        return False
    return subprocess.run(["agy", "--continue"]).returncode == 0


def cmd_autoswitch() -> int:
    """Move off the active account when its quota runs out.

    Antigravity answers quota questions only for the session that is *live*,
    and making a candidate live means writing its credential into the single
    shared keychain slot the running agy process reads — which would corrupt
    an in-flight request on the user's real session. So candidates are never
    probed here; only the active account is.

    That leaves every switch blind: the target may be just as exhausted as the
    account being left. Which is why it is opt-in through ``agy_blind_switch``
    and off by default — without it this command reports and stops.
    """
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    active = _active_profile()
    # config_flag, not bool(): a hand-edited `"agy_blind_switch": "false"`
    # is a truthy string, and this gate must fail closed on anything odd.
    blind = autoswitch.config_flag("agy_blind_switch")
    probe_error: list[str] = []

    def probe(name: str) -> UsageWindow | None:
        if active is None or name != active.stem:
            # Never activate a candidate (see the docstring). Blind mode takes
            # the unknown as "has quota"; otherwise it stays unknown, and the
            # engine finds nothing to switch to.
            return UsageWindow(0, None, None) if blind else None
        snapshot = gemini_usage.fetch_usage(timeout=8)
        if snapshot.error:
            probe_error.append(snapshot.error)
        used = _live_used_pct(snapshot)
        return None if used is None else UsageWindow(used, None, None)

    def switch(name: str) -> bool:
        # cmd_switch falls back to cmd_list() when the profile file is gone,
        # and cmd_list activates every saved profile in turn to read its quota
        # — the very keychain hijack this command exists to avoid. The target
        # can disappear during the live quota fetch (a concurrent `remove`),
        # so re-check here rather than let it reach that branch.
        profile = _profile_file(name)
        if profile is None or not profile.is_file():
            log_red(f"❌ Profile disappeared before the switch: {name}")
            return False
        return cmd_switch(name) == 0

    # SAFETY: no terminal, no restart — build_restart hands back None for a
    # non-interactive run, so an unattended timer poll spawns nothing.
    interactive = sys.stdout.isatty()
    outcome = autoswitch.run_autoswitch(
        # "agy", not "antigravity" — the engine keys PROVIDER_VERDICTS and its
        # IDE-running downgrade off this exact string.
        "agy",
        [profile.stem for profile in profiles],
        active.stem if active is not None else None,
        probe,
        switch,
        autoswitch.build_restart(
            "agy",
            _autoswitch_resume,
            interactive=interactive,
            # Asked only when the answer can still change something: an
            # unattended run is pinned to manual-restart anyway, and scanning
            # the process table costs a process.
            agy_ide_running=interactive and _antigravity_ide_running(),
        ),
    )
    _render_autoswitch(outcome, probe_error, others=len(profiles) - 1)
    return 1 if outcome.reason == "switch_failed" else 0


def _render_autoswitch(
    outcome: autoswitch.SwitchOutcome, probe_error: list[str], *, others: int
) -> None:
    """Say on the terminal what the engine just decided, and why."""
    name = outcome.from_profile or "—"
    if outcome.reason == "disabled":
        log_yellow("⚠️  Auto-switching is off.")
        print(
            f"{DIM}   Turn it on with \"enabled\": true in "
            f"{autoswitch.config_path()}{RESET}"
        )
    elif outcome.reason == "no_active":
        print_no_active_account("Antigravity", "agy-accounts")
    elif outcome.reason == "unknown":
        detail = probe_error[0] if probe_error else "no quota data returned"
        log_yellow(f"⚠️  Could not read {name}'s quota: {detail}")
    elif outcome.reason == "below_threshold":
        ok(f"{name} is at {outcome.used_pct}% used — nothing to switch.")
    elif outcome.reason == "no_candidate":
        log_yellow(f"⚠️  {name} is at {outcome.used_pct}% used.")
        if others < 1:
            print(f"{DIM}   No other saved Antigravity profile to switch to.{RESET}")
        else:
            print(
                f"{DIM}   agy reports quota only for the live session — "
                f"cannot verify candidate quota, skipping.{RESET}"
            )
            print(
                f"{DIM}   Set \"agy_blind_switch\": true in "
                f"{autoswitch.config_path()} to switch anyway.{RESET}"
            )
    elif outcome.reason == "switch_failed":
        log_red(f"❌ {outcome.error}")
    else:
        log_yellow(
            f"⚠️  {outcome.to_profile}'s own quota was NOT verified before "
            f"switching — agy only reports quota for the live session."
        )
        if outcome.restarted is not True:
            # The switch already happened; a restart that never ran (unattended
            # poll, IDE running) or failed must not undo that — hand the session
            # back to the user instead of reporting a failure.
            log_yellow(
                autoswitch.manual_restart_message(
                    "agy", outcome.from_profile or "?", outcome.to_profile or "?"
                )
            )


# ── entry point ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0

    # `config` edits the shared ~/.ai-accounts/config.json, not anything
    # Keychain-backed — handle it before the macOS gate so it works on every
    # platform, same as every other ai-accounts CLI.
    if argv[0] == "config":
        return cm.cmd_config(argv[1:], prog="agy-accounts")

    if sys.platform != "darwin":
        log_red("❌ agy-accounts currently requires macOS Keychain.")
        log_yellow("   The other ai-accounts commands remain available on this platform.")
        return 1

    command, *rest = argv

    if command in ("who", "current"):
        return cmd_who()
    if command == "save":
        return cmd_save(rest[0] if rest else None)
    if command == "list":
        return cmd_list()
    if command == "usage":
        return cmd_list(only_active=True)
    if command == "switch":
        if not rest:
            return cmd_switch_interactive()
        return cmd_switch(rest[0])
    if command == "remove":
        return cmd_remove(rest[0]) if rest else cmd_remove_interactive()
    if command == "refresh":
        return cmd_refresh(rest[0] if rest else None)
    if command == "sync":
        return cmd_sync()
    if command == "autoswitch":
        return cmd_autoswitch()
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
