"""claude-accounts — manage multiple Claude Code login profiles.

A color-coded, tabular terminal UI over the Claude Code OAuth credentials so
multiple accounts are easy to tell apart at a glance (which profile is saved,
which is active, when a token expires, current usage). Refreshes tokens via
OAuth with no browser and no logout. Never prints raw tokens — only decoded,
non-secret fields (plan, scopes, expiry).

Claude Code stores its OAuth credentials in ``~/.claude/.credentials.json``
(override the dir with ``$CLAUDE_CONFIG_DIR``) under a ``claudeAiOauth`` key,
mirrored on macOS into the login keychain (service "Claude Code-credentials",
account = the current user). That file also holds unrelated ``mcpOAuth`` server
tokens, so profiles capture ONLY the ``claudeAiOauth`` object and a switch
merges it into the live file — the MCP tokens are never touched.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Final

from . import claude_usage
from .usage_format import (
    capitalize_first,
    format_unix_time_compact,
    format_usage_window,
    print_no_active_account,
)
from ._present import (
    _ANSI_RE as _ANSI_RE,
    accounts_table,
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
    RED,
    RESET,
    YELLOW,
    Spinner,
    ensure_tool,
    fetch_parallel,
    have,
    log_red,
    log_yellow,
    plan_tier_color,
    resolve_account_dir,
)

# Claude paid tiers, low → high (Free is left uncolored by the caller).
_PLAN_TIERS = ("pro", "team", "max")

_OAUTH_KEY: Final = "claudeAiOauth"
_IDENTITY_KEY: Final = "ai-accountsAccount"  # non-secret identity snapshot (email/name)

HELP = """claude-accounts — manage multiple Claude Code login profiles

USAGE
  claude-accounts who                   Show the current logged-in Claude account
  claude-accounts current               Alias for `who`
  claude-accounts save <name>           Save the current login as a reusable profile
  claude-accounts list                  List profiles with usage (never refreshes tokens)
  claude-accounts usage                 Show only the active account's usage row
  claude-accounts switch [<name>]       Switch by name; no name = interactive picker
  claude-accounts remove <name>         Delete a saved profile
  claude-accounts refresh [<name>]      Refresh tokens via OAuth (no browser, no logout);
                                        no name = refresh active auth + sync it back
  claude-accounts refresh --all         Refresh every saved profile
  claude-accounts sync                  Copy the active auth back to its matching profile
  claude-accounts login-switch <name>   `claude auth login` + save as <name>
  claude-accounts -h | --help           Show this help

EXAMPLES
  claude-accounts login-switch personal
  claude-accounts login-switch work
  claude-accounts list
  claude-accounts switch
  claude-accounts switch personal
  claude-accounts refresh --all
  claude-accounts who

Profiles live under ~/.ai-accounts/claude/accounts/<name>.json (override with
$CLAUDE_ACCOUNT_DIR); a store at the old ~/.claude/accounts location is moved
there automatically.
Treat that directory as secrets — saved profiles contain Claude OAuth tokens.
"""


# ── paths ─────────────────────────────────────────────────────────────────

def _claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _account_dir() -> Path:
    return resolve_account_dir(
        "CLAUDE_ACCOUNT_DIR",
        Path.home() / ".ai-accounts" / "claude" / "accounts",
        _claude_home() / "accounts",
    )


def _creds_file() -> Path:
    return Path(os.environ.get("CLAUDE_CREDENTIALS_JSON", str(_claude_home() / ".credentials.json")))


def _config_json() -> Path:
    """Claude Code's global config file, holding the live account's identity.
    Lives inside CLAUDE_CONFIG_DIR when set, else at ~/.claude.json."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(env) / ".claude.json") if env else (Path.home() / ".claude.json")


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


# ── credential envelope (claudeAiOauth) ─────────────────────────────────────
# The active store may be the full credentials file ({mcpOAuth, claudeAiOauth})
# or a bare OAuth blob (in the keychain item). Profiles always hold the bare
# blob. _extract_oauth reads the account out of either shape; _inject_oauth
# writes an updated account back into whatever shape a store already uses, so
# unrelated keys (mcpOAuth) survive a switch untouched.

def _extract_oauth(obj: object) -> dict | None:
    if not isinstance(obj, dict):
        return None
    inner = obj.get(_OAUTH_KEY)
    if isinstance(inner, dict):
        return inner
    if "accessToken" in obj:
        return obj
    return None


def _inject_oauth(container_text: str | None, oauth: dict) -> dict:
    parsed: object = {}
    if container_text:
        try:
            parsed = json.loads(container_text)
        except ValueError:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if "accessToken" in parsed and _OAUTH_KEY not in parsed:
        return dict(oauth)  # store keeps a bare blob — replace it wholesale
    parsed[_OAUTH_KEY] = oauth
    return parsed


# ── macOS keychain mirror ───────────────────────────────────────────────────
# Claude Code on macOS reads its credentials from the login keychain (service
# "Claude Code-credentials", account = the login user) in preference to the
# file, so a switch that rewrites only the file is silently ignored. We mirror
# every active write into that keychain item — update-only: we never fabricate
# a keychain store Claude Code wasn't already using.

_KEYCHAIN_SERVICE: Final = "Claude Code-credentials"


def _keychain_account() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        return getpass.getuser()
    except Exception:
        return None


def _read_keychain_creds() -> str | None:
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
    # `security -w` hex-encodes secrets containing bytes it deems "non-clean"
    # (e.g. newlines). Decode that back to the original JSON text.
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", secret):
        try:
            secret = bytes.fromhex(secret).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
    return secret


def _write_keychain_creds(content: str) -> bool:
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


def _mirror_active_oauth_to_keychain(oauth: dict) -> None:
    if _keychain_account() is None:
        return
    current = _read_keychain_creds()
    if current is None:
        return  # not keychain-backed here; the file is the source of truth
    merged = _inject_oauth(current, oauth)
    # Compact, newline-free JSON avoids `security` storing a hex-encoded value.
    if not _write_keychain_creds(json.dumps(merged, separators=(",", ":"))):
        log_yellow(
            "⚠️  Could not update the macOS keychain; Claude Code may keep using "
            "the previous account until its next login."
        )


# ── non-secret claims ───────────────────────────────────────────────────────
# Claude's OAuth access token is opaque (no JWT identity), so unlike codex/agy
# the token carries no email or name — only the plan tier, scopes, and expiry.
# The account's email/name lives in Claude Code's ~/.claude.json (`oauthAccount`),
# which only ever describes the *live* account; we snapshot it into each profile
# at save time (when it provably matches) so `list` can show it per-profile.

def _format_unix_time(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _fingerprint(oauth: dict) -> str | None:
    """Short one-way hash of the token — a non-secret visual handle so accounts
    are distinguishable; rotates when the token does, so it is not a stable id."""
    token = oauth.get("refreshToken") or oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _claims_from_oauth(oauth: dict) -> dict:
    scopes = oauth.get("scopes")
    expires_ms = oauth.get("expiresAt")
    exp = int(expires_ms / 1000) if isinstance(expires_ms, (int, float)) else None
    return {
        "plan": oauth.get("subscriptionType") or oauth.get("rateLimitTier"),
        "subscription_type": oauth.get("subscriptionType"),
        "rate_limit_tier": oauth.get("rateLimitTier"),
        "scopes": scopes if isinstance(scopes, list) else None,
        "fingerprint": _fingerprint(oauth),
        "expires_epoch": exp,
        "expires_str": _format_unix_time(exp),
        "refreshable": bool(oauth.get("refreshToken")),
    }


def _claims_from_text(text: str) -> dict | None:
    try:
        oauth = _extract_oauth(json.loads(text))
    except ValueError:
        return None
    return _claims_from_oauth(oauth) if oauth is not None else None


def _read_claims(path: Path) -> dict | None:
    oauth = _read_profile_oauth(path)
    return _claims_from_oauth(oauth) if oauth is not None else None


def _read_active_claims() -> dict | None:
    oauth = _read_active_oauth()
    return _claims_from_oauth(oauth) if oauth is not None else None


def _read_active_identity() -> dict | None:
    """Email/name of the live account, from ~/.claude.json's `oauthAccount`.
    None when the config file is missing/unreadable or carries no identity."""
    try:
        data = json.loads(_config_json().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return None
    ident = {"email": account.get("emailAddress"), "name": account.get("displayName")}
    return ident if ident["email"] or ident["name"] else None


def _read_profile_identity(path: Path) -> dict | None:
    """Identity snapshot stored in a profile at save time (None for older, bare
    profiles saved before this was captured — they backfill on next save)."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    ident = obj.get(_IDENTITY_KEY) if isinstance(obj, dict) else None
    return ident if isinstance(ident, dict) else None


def _identity_label(ident: dict | None) -> str | None:
    """`name <email>` (or whichever is present) for the ACCOUNT column. None
    when no identity is known, so the column can hide when empty everywhere."""
    if not ident:
        return None
    email, name = ident.get("email"), ident.get("name")
    if name and email:
        return f"{name} <{email}>"
    return email or name or None


def _plan_label(claims: dict | None) -> str:
    plan = (claims or {}).get("plan")
    return capitalize_first(str(plan)) if plan else "(Claude account)"


def _rate_multiplier(claims: dict | None) -> str | None:
    """Compact rate signal from the OAuth ``rateLimitTier`` — the ``Nx`` seat
    multiplier only, dropping the ``default_claude_max_`` boilerplate
    (``default_claude_max_5x`` → ``5x``). ``None`` when the tier carries no
    multiplier (e.g. a plain ``pro`` tier) or is absent."""
    tier = (claims or {}).get("rate_limit_tier")
    if not isinstance(tier, str):
        return None
    match = re.search(r"(\d+)x\b", tier)
    return f"{match.group(1)}x" if match else None


def _plan_cell(claims: dict | None) -> str:
    """PLAN column value: the subscription tier plus its rate multiplier when
    the token carries one (e.g. ``team · 5x``), so seats on the same plan with
    different rate allotments are told apart at a glance."""
    plan = _plan_label(claims)
    mult = _rate_multiplier(claims)
    return f"{plan} · {mult}" if mult else plan


def _plan_row_cell(claims: dict | None) -> str:
    """Colored PLAN column value for the list table: Free (or an unreadable
    token) stays uncolored, paid tiers escalate pro → team → max."""
    if not claims:
        return f"{RED}(unreadable){RESET}"
    text = _plan_cell(claims)
    plan = _plan_label(claims)
    if plan.lower() == "free" or plan == "(Claude account)":
        return text
    return f"{plan_tier_color(plan, _PLAN_TIERS)}{text}{RESET}"


# ── token identity ──────────────────────────────────────────────────────────
# No stable account id exists, so a profile is matched to the active session by
# exact token equality only (there is no same-account grouping like codex/agy).

def _token_key_from_oauth(oauth: dict) -> str | None:
    token = oauth.get("refreshToken") or oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def _token_key_from_path(path: Path) -> str | None:
    oauth = _read_profile_oauth(path)
    return _token_key_from_oauth(oauth) if oauth is not None else None


def _read_profile_oauth(path: Path) -> dict | None:
    try:
        return _extract_oauth(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


# ── active credential store (keychain-first on macOS, then file) ────────────

def _read_active_creds_text() -> str | None:
    secret = _read_keychain_creds()
    if secret:
        try:
            if _extract_oauth(json.loads(secret)) is not None:
                return secret
        except ValueError:
            pass
    creds = _creds_file()
    if creds.is_file():
        try:
            return creds.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def _read_active_oauth() -> dict | None:
    text = _read_active_creds_text()
    if text is None:
        return None
    try:
        return _extract_oauth(json.loads(text))
    except ValueError:
        return None


def _write_active_oauth(oauth: dict) -> None:
    """Set the live account to `oauth`, in both the file and the keychain mirror,
    preserving any unrelated keys (mcpOAuth) already in each store."""
    creds = _creds_file()
    creds.parent.mkdir(parents=True, exist_ok=True)
    current = creds.read_text(encoding="utf-8") if creds.is_file() else None
    merged = _inject_oauth(current, oauth)
    creds.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    creds.chmod(0o600)
    _mirror_active_oauth_to_keychain(oauth)


def _active_profile(active_oauth: dict | None = None) -> Path | None:
    oauth = active_oauth if active_oauth is not None else _read_active_oauth()
    if oauth is None:
        return None
    active_token = _token_key_from_oauth(oauth)

    marked = _marked_profile()
    if marked is not None:
        if active_token is not None and _token_key_from_path(marked) == active_token:
            return marked
        if active_token is None:
            return marked

    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    matches = [p for p in profiles if _token_key_from_path(p) == active_token]
    return matches[0] if active_token is not None and len(matches) == 1 else None


def _write_profile(profile_file: Path, oauth: dict, identity: dict | None = None) -> None:
    _account_dir().mkdir(parents=True, exist_ok=True)
    _account_dir().chmod(0o700)
    # Wrapped shape so the non-secret identity rides alongside the token; a fresh
    # `identity` wins, else any previously stored one survives token rotation
    # (refresh/fold pass none — they must not drop the email captured at save).
    container: dict = {_OAUTH_KEY: oauth}
    ident = identity or _read_profile_identity(profile_file)
    if ident:
        container[_IDENTITY_KEY] = ident
    profile_file.write_text(json.dumps(container, indent=2) + "\n", encoding="utf-8")
    profile_file.chmod(0o600)


def _fold_active_into_profile(profile: Path) -> None:
    """Copy the live account back into its own saved profile before it is
    overwritten, so a later switch back restores the freshest rotated tokens.
    Callers only ever pass the token-matched active profile. No network."""
    oauth = _read_active_oauth()
    if oauth is not None:
        _write_profile(profile, oauth)


# ── OAuth token refresh (no browser, no logout) ─────────────────────────────

_OAUTH_TOKEN_URL: Final = "https://platform.claude.com/v1/oauth/token"
_OAUTH_CLIENT_ID: Final = os.environ.get(
    "CLAUDE_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
)
_OAUTH_USER_AGENT: Final = "claude-cli/1.0 (ai-accounts, cli)"


def _oauth_refresh(refresh_token: str) -> tuple[dict | None, str | None]:
    """Exchange a refresh_token for fresh tokens. (response, None) on success;
    (None, error) on failure — never raises, never logs tokens. An error that
    starts with "revoked" means only a fresh login helps; anything else is
    transient and safe to retry."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _OAUTH_CLIENT_ID,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # Cloudflare (error 1010) blocks the default Python-urllib UA before
            # the request ever reaches the OAuth endpoint — send a real one.
            "User-Agent": _OAUTH_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        if exc.code in (400, 401) and "invalid_grant" in detail:
            return None, "revoked: refresh token rejected (invalid_grant)"
        # A 403 without an OAuth error body is an edge/WAF block (e.g. Cloudflare
        # 1010), not a revoked token — that's transient, retry-able, not relogin.
        if exc.code == 403 and "invalid_" not in detail:
            return None, "HTTP 403 blocked before token endpoint (edge/WAF, not the token)"
        if exc.code in (401, 403):  # endpoint reached but rejected the token — relogin, not retry
            return None, f"revoked: token endpoint returned HTTP {exc.code}"
        return None, f"HTTP {exc.code} from token endpoint"
    except urllib.error.URLError as exc:
        return None, f"network error: {exc.reason}"
    except Exception as exc:  # malformed JSON response, etc.
        return None, str(exc)


def _is_revoked_error(error: str | None) -> bool:
    return (error or "").startswith("revoked")


def _apply_refreshed_tokens(oauth: dict, refreshed: dict) -> dict:
    out = dict(oauth)
    if refreshed.get("access_token"):
        out["accessToken"] = refreshed["access_token"]
    if refreshed.get("refresh_token"):  # optional — the endpoint may reuse the old one
        out["refreshToken"] = refreshed["refresh_token"]
    expires_in = refreshed.get("expires_in")
    if isinstance(expires_in, (int, float)):
        out["expiresAt"] = int(time.time() * 1000) + int(expires_in) * 1000
    return out


def _refresh_oauth(oauth: dict, label: str, relogin_hint: str) -> tuple[dict | None, str | None]:
    """Refresh one OAuth blob. (new_oauth, None) on success, else (None, kind)
    where kind is "revoked" or "transient"."""
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        log_red(f"❌ No refresh token found in {label}")
        log_yellow(f"   {relogin_hint}")
        return None, "revoked"
    refreshed, error = _oauth_refresh(refresh_token)
    if refreshed is None:
        if _is_revoked_error(error):
            log_red(f"❌ Refresh token revoked/dead for {label}: {error}")
            log_yellow(f"   {relogin_hint}")
            return None, "revoked"
        log_red(f"❌ Refresh failed for {label}: {error}")
        log_yellow("   Token endpoint unreachable — retry later.")
        return None, "transient"
    return _apply_refreshed_tokens(oauth, refreshed), None


def _token_expired_or_soon(claims: dict | None) -> bool:
    if not claims or not claims.get("expires_epoch"):
        return False
    return claims["expires_epoch"] - datetime.now().timestamp() < 24 * 3600


# ── terminal rendering ───────────────────────────────────────────────────

def _expiry_status(claims: dict | None) -> tuple[str, str]:
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
    return str(claims["expires_str"]), GREEN


def _list_expiry_status(claims: dict | None) -> tuple[str, str]:
    if not claims or not claims.get("expires_epoch"):
        return "—", DIM
    # A refresh token means Claude Code auto-renews the short-lived access token,
    # so its imminent expiry is not a session problem — mirror the `who` panel
    # (and agy-accounts) and report the session as refreshable instead of "soon".
    if claims.get("refreshable"):
        return "refreshable", GREEN
    now = datetime.now().timestamp()
    exp = claims["expires_epoch"]
    text = format_unix_time_compact(int(exp))
    if exp <= now:
        return f"{text} expired", RED
    if exp - now < 24 * 3600:
        return f"{text} soon", YELLOW
    return text, GREEN


def _claims_lines(claims: dict | None) -> list[str]:
    if claims is None:
        return [f"{YELLOW}No Claude credentials found.{RESET} Run: {BOLD}claude auth login{RESET}"]

    lines = [f"{BOLD}Plan{RESET}          : {_plan_label(claims)}"]
    if claims.get("rate_limit_tier") and claims.get("rate_limit_tier") != claims.get("plan"):
        lines.append(f"{DIM}Rate tier{RESET}     : {claims['rate_limit_tier']}")
    if claims.get("scopes"):
        lines.append(f"{DIM}Scopes{RESET}        : {', '.join(str(s) for s in claims['scopes'])}")
    if claims.get("fingerprint"):
        lines.append(f"{DIM}Token{RESET}         : {claims['fingerprint']}…")
    expires_text, color = _expiry_status(claims)
    session = f"{GREEN}refreshable{RESET}" if claims.get("refreshable") else f"{color}{expires_text}{RESET}"
    lines.append(f"{DIM}Expires{RESET}       : {color}{expires_text}{RESET}")
    lines.append(f"{DIM}Session{RESET}       : {session}")
    return lines


def _usage_cell(window: claude_usage.UsageWindow | None, window_kind: str) -> str:
    if window is None:
        return f"{DIM}—{RESET}"
    percent = f"{usage_color(window.percentage)}{window.percentage}%{RESET}"
    return format_usage_window(window, window_kind, percent)


# Hide the usage/account columns entirely when every row renders "—" (usage
# unfetchable, or no profile has an identity snapshot yet).
_TABLE_COLUMNS = [
    ("PROFILE", "profile"),
    ("ACCOUNT", "account"),
    ("PLAN", "plan"),
    ("5H USED", "usage_5h"),
    ("1W USED", "usage_1week"),
    ("UPDATED", "usage_updated"),
    ("EXPIRES", "expires"),
    ("STATE", "status"),
]


def _print_accounts_table(rows: list[dict]) -> None:
    accounts_table(
        rows,
        _TABLE_COLUMNS,
        optional_columns={"usage_5h", "usage_1week", "account"},
        align_keys=("usage_5h", "usage_1week"),
    )


# ── commands ─────────────────────────────────────────────────────────────

def cmd_who() -> int:
    if have("claude"):
        result = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True)
        text = (result.stdout or result.stderr or "").strip()
        status_lines = text.splitlines() if text else [f"{DIM}(no output){RESET}"]
    else:
        status_lines = [
            f"{RED}claude command not found{RESET}  "
            f"{DIM}(install: curl -fsSL https://claude.ai/install.sh | bash){RESET}"
        ]
    panel("Claude Login Status", status_lines)

    print()
    panel("Current Auth Claims", _claims_lines(_read_active_claims()))
    return 0


def _save_profile_oauth(name: str, oauth: dict, identity: dict | None = None) -> int:
    profile_file = _profile_file(name)
    if profile_file is None:
        return 1
    _write_profile(profile_file, oauth, identity)
    # Converge the live stores so the file and keychain mirror match this profile
    # (a fresh `claude auth login` may have written only one of them).
    _write_active_oauth(oauth)
    _set_current_profile(profile_file)

    success_panel(
        "Saved Claude profile",
        name,
        _claims_lines(_read_claims(profile_file)),
        title=f"Profile: {name}",
        details=(f"→ {profile_file}",),
    )
    return 0


def cmd_save(name: str) -> int:
    oauth = _read_active_oauth()
    if oauth is None:
        log_red(f"❌ No Claude credentials found: {_creds_file()}")
        log_yellow("   Run: claude auth login")
        return 1
    # The user is already running as this account, so ~/.claude.json's identity
    # provably matches — the one moment its email/name can be trusted outright.
    return _save_profile_oauth(name, oauth, _read_active_identity())


def cmd_list(*, fetch_usage: bool = True, only_active: bool = False) -> int:
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Claude profiles.")
        print(f"{DIM}   Add one with: claude-accounts save <profile_name>{RESET}", file=sys.stderr)
        return 0

    active_profile = _active_profile()
    empty_usage = claude_usage.UsageSnapshot(None, None, None, None, None)

    # Read each profile's oauth/claims once; both the parallel fetch and the row
    # build below reuse it.
    profile_oauth = [(p, _read_profile_oauth(p) or {}) for p in profiles]
    if only_active:
        if active_profile is None:
            print_no_active_account("Claude", "claude-accounts")
            return 0
        # Filter before fetching so only the active account's usage is queried.
        profile_oauth = [(p, o) for p, o in profile_oauth if p == active_profile]

    def _fetch(item: tuple[Path, dict]) -> claude_usage.UsageSnapshot:
        # Independent per account: HTTP call with that account's token, no disk
        # write, so these run concurrently without racing the credentials file.
        _profile_path, oauth = item
        access_token = oauth.get("accessToken")
        if not (isinstance(access_token, str) and access_token):
            return empty_usage
        claims = _claims_from_oauth(oauth) if oauth else None
        usage = claude_usage.fetch_usage(access_token, plan=(claims or {}).get("plan"))
        if usage.error and usage.error.startswith(("HTTP 401", "HTTP 403")):
            return empty_usage
        return usage

    if fetch_usage:
        spinner = Spinner("Fetching Claude usage…")
        with spinner:
            usages = fetch_parallel(
                profile_oauth,
                _fetch,
                spinner,
                "Fetching Claude usage…",
                labels=[p.stem for p, _ in profile_oauth],
            )
    else:
        usages = [empty_usage] * len(profiles)

    rows = []
    for (profile_path, oauth), usage in zip(profile_oauth, usages):
        name = profile_path.stem
        claims = _claims_from_oauth(oauth) if oauth else None
        is_active = profile_path == active_profile
        status = f"{GREEN}{BOLD}ACTIVE{RESET}" if is_active else f"{DIM}—{RESET}"
        expires_text, color = _list_expiry_status(claims)

        # Stored snapshot wins (captured when .claude.json provably matched). Live
        # identity is only a fallback for an active profile with none yet — a
        # pre-feature profile backfills on next save.
        # ponytail: live fallback can misattribute if switched-without-relaunch;
        # self-corrects on next save/login-switch.
        identity = _read_profile_identity(profile_path)
        if identity is None and is_active:
            identity = _read_active_identity()
        account_label = _identity_label(identity)

        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{name}{RESET}" if is_active else name,
                "account": account_label if account_label else f"{DIM}—{RESET}",
                "plan": _plan_row_cell(claims),
                "usage_5h": _usage_cell(usage.five_hour, "5h"),
                "usage_1week": _usage_cell(usage.seven_day, "1week"),
                "usage_updated": claude_usage.format_refreshed_at(usage),
                "expires": f"{color}{expires_text}{RESET}",
                "status": status,
            }
        )

    if only_active:
        print(f"{BOLD}Current Claude account{RESET}")
    else:
        print(f"{BOLD}Saved Claude profiles{RESET}  {DIM}({len(rows)}){RESET}")
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

    oauth = _read_profile_oauth(profile_file)
    if oauth is None:
        log_red(f"❌ Profile is unreadable: {name}")
        return 1

    # Fold any token rotation on the outgoing account back into its own profile
    # before overwriting, so a later switch back restores fresh tokens.
    outgoing = _active_profile()
    if outgoing is not None and outgoing != profile_file:
        _fold_active_into_profile(outgoing)

    _write_active_oauth(oauth)
    _set_current_profile(profile_file)

    ok("Switched Claude profile to", name)
    print(f"{DIM}   Claude Code will use this account on its next launch.{RESET}")

    # Self-heal an expired snapshot in place so Claude Code starts with a live
    # token. Only fires when the restored token is expired/near-expiry.
    if _token_expired_or_soon(_read_claims(profile_file)):
        rc = _recover_switched_auth(profile_file, name)
        if rc != 0:
            return rc

    print()
    return cmd_who()


def _recover_switched_auth(profile_file: Path, name: str) -> int:
    """Refresh a just-restored but expired token in place, mirroring the rotated
    result back into the profile. Only a genuine revocation escalates to login."""
    oauth = _read_active_oauth() or {}
    new_oauth, error = _refresh_oauth(oauth, name, f"Re-login with: claude-accounts login-switch {name}")
    if new_oauth is not None:
        _write_active_oauth(new_oauth)
        _write_profile(profile_file, new_oauth)
        print(f"{DIM}   (token was expired — refreshed in place){RESET}")
        return 0
    if not _is_revoked_error(error):
        log_yellow(f"⚠️  Could not refresh after switch ({error}); Claude Code will retry on next use.")
        return 0
    log_yellow(f"⚠️  Saved token for '{name}' is revoked — re-logging in via browser…")
    return cmd_login_switch(name)


def cmd_switch_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Claude profiles available to switch.")
        return 1

    chosen = choose_profile(
        "a Claude",
        [(profile.stem, _plan_label(_read_claims(profile))) for profile in profiles],
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
    ok("Removed Claude profile", name, bold=False)
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

    oauth = _read_profile_oauth(profile_file)
    if oauth is None:
        log_red(f"❌ Profile is unreadable: {name}")
        return 1, "revoked"
    new_oauth, kind = _refresh_oauth(oauth, name, f"Re-login with: claude-accounts login-switch {name}")
    if new_oauth is None:
        return 1, kind

    _write_profile(profile_file, new_oauth)
    synced_active = False
    if _active_profile() == profile_file:
        _write_active_oauth(new_oauth)
        _set_current_profile(profile_file)
        synced_active = True

    if show_summary:
        details = (
            ("(same account is active — live credentials updated too)",)
            if synced_active
            else ()
        )
        success_panel(
            "Refreshed Claude profile",
            name,
            _claims_lines(_read_claims(profile_file)),
            title=f"Profile: {name}",
            details=details,
        )
    return 0, None


def _refresh_all_profiles() -> int:
    account_dir = _account_dir()
    profiles = sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Claude profiles to refresh.")
        return 0

    revoked, transient = [], []
    refreshed_tokens: set[str] = set()
    for profile_path in profiles:
        token = _token_key_from_path(profile_path)
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
    ok(f"All {len(profiles)} profile(s) refreshed.")
    return 0


def _refresh_active_auth() -> int:
    oauth = _read_active_oauth()
    if oauth is None:
        log_red(f"❌ No Claude credentials found: {_creds_file()}")
        log_yellow("   Run: claude auth login")
        return 1

    profile_path = _active_profile(oauth)
    new_oauth, _kind = _refresh_oauth(oauth, "the active auth", "Re-login with: claude auth login")
    if new_oauth is None:
        return 1
    _write_active_oauth(new_oauth)
    if profile_path is not None:
        _write_profile(profile_path, new_oauth)
        _set_current_profile(profile_path)
        details = (f"(synced back to profile: {profile_path.stem})",)
    else:
        log_yellow("⚠️  No unambiguous current profile — run: claude-accounts switch <name>")
        details = ()

    success_panel(
        "Refreshed active Claude auth.",
        None,
        _claims_lines(_read_active_claims()),
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
    oauth = _read_active_oauth()
    if oauth is None:
        log_red(f"❌ No Claude credentials found: {_creds_file()}")
        log_yellow("   Run: claude auth login")
        return 1

    profile_path = _active_profile(oauth)
    if profile_path is None:
        log_red("❌ No unambiguous current profile.")
        log_yellow("   Select it first with: claude-accounts switch <name>")
        return 1

    _write_profile(profile_path, oauth)
    _set_current_profile(profile_path)
    success_panel(
        "Synced active auth → profile",
        profile_path.stem,
        _claims_lines(_read_claims(profile_path)),
        title=f"Profile: {profile_path.stem}",
    )
    return 0


def cmd_login_switch(name: str) -> int:
    if not ensure_tool("claude"):
        return 1
    if _profile_file(name) is None:
        return 1

    outgoing_oauth = _read_active_oauth()
    outgoing_profile = _active_profile(outgoing_oauth)
    if outgoing_profile is not None:
        _fold_active_into_profile(outgoing_profile)

    # Snapshot the identity BEFORE the browser login. `claude auth login` may not
    # refresh ~/.claude.json's oauthAccount until Claude Code's next session, so
    # only trust the post-login identity if it actually changed — otherwise it is
    # the outgoing account's stale email and must not be saved (backfills later).
    identity_before = _read_active_identity()

    print(
        f"{DIM}Launching `claude auth login`. Complete the browser sign-in; "
        f"the new account will be saved as '{name}'.{RESET}"
    )
    try:
        login = subprocess.run(["claude", "auth", "login"])
    except KeyboardInterrupt:
        _restore_active_oauth(outgoing_oauth)
        log_yellow("Login cancelled. Your previous Claude session was restored.")
        return 130
    if login.returncode != 0:
        _restore_active_oauth(outgoing_oauth)
        log_red("❌ claude auth login did not complete successfully")
        return login.returncode or 1

    new_oauth = _read_active_oauth()
    if new_oauth is None:
        _restore_active_oauth(outgoing_oauth)
        log_red("❌ Login completed without readable Claude credentials")
        return 1
    identity_after = _read_active_identity()
    identity = identity_after if identity_after and identity_after != identity_before else None
    return _save_profile_oauth(name, new_oauth, identity)


def _restore_active_oauth(oauth: dict | None) -> None:
    if oauth is not None:
        _write_active_oauth(oauth)


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
            log_red("Usage: claude-accounts save <profile_name>")
            return 1
        return cmd_save(rest[0])
    if command == "list":
        return cmd_list()
    if command == "usage":
        return cmd_list(only_active=True)
    if command == "switch":
        if not rest:
            return cmd_switch_interactive()
        return cmd_switch(rest[0])
    if command == "remove":
        if not rest:
            log_red("Usage: claude-accounts remove <profile_name>")
            return 1
        return cmd_remove(rest[0])
    if command == "refresh":
        return cmd_refresh(rest[0] if rest else None)
    if command == "sync":
        return cmd_sync()
    if command == "login-switch":
        if not rest:
            log_red("Usage: claude-accounts login-switch <profile_name>")
            return 1
        return cmd_login_switch(rest[0])

    log_red(f"❌ Unknown command: {command}")
    print(HELP)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
