from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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
    oauth_token_refresh,
    resolve_account_dir,
)
from .usage_format import credential_status_prefix, print_no_active_account

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
  grok-accounts refresh [<name>]      Renew the active/named session's token
  grok-accounts refresh --all         Renew every saved profile's token
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
tokens. `refresh` performs a standard OIDC refresh grant against the token
endpoint discovered from the credential's own issuer — nothing is hardcoded. It
falls back to running `grok models` (letting the official CLI rotate the
credential) when that grant needs a client secret ai-accounts does not hold.
`switch` refreshes in place when the restored token is expired or within 5
minutes of expiring.
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
    if not payload:
        return {}
    record = _record(payload)
    if record is None:
        # The file parses as JSON but none of its values look like a Grok
        # OAuth record (auth_mode/refresh_token/email) — e.g. an Antigravity
        # credential misfiled into this dir. Distinct from "no file at all"
        # so callers can report it as malformed rather than as expired/blank.
        return {"malformed": True}
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
        "malformed": False,
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


def _expiry_status(claims: JsonDict) -> tuple[str, str]:
    """(display text, color) — refresh-token health first, mirroring
    gemini_accounts._list_expiry_status. A live refresh_token means the
    account is healthy regardless of the (1h-lived) access token's own
    expiry; RED is reserved for what actually needs a human: no refresh
    token present, a malformed record, or (once refresh lands) one revoked
    by x.ai."""
    prefix = credential_status_prefix(claims)
    if prefix is not None:
        return prefix
    value = str(claims.get("expires_at") or "")
    if not value:
        return "—", DIM
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:19], DIM
    seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if seconds < 0:
        return "EXPIRED", RED
    if seconds < 60 * 60:
        return "<1h", YELLOW
    if seconds < 24 * 60 * 60:
        hours, remainder = divmod(int(seconds), 60 * 60)
        return f"{hours}h {remainder // 60:02d}m", YELLOW
    return when.astimezone().strftime("%b %d %H:%M"), GREEN


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
    if claims.get("malformed"):
        return [
            f"{YELLOW}Malformed credential file — not a recognizable Grok "
            f"OAuth record.{RESET}",
            f"{DIM}Profile{RESET}       : {profile.stem if profile else 'untracked'}",
        ]
    name = claims["name"]
    account = f"{name} <{claims['email']}>" if name else claims["email"]
    session = f"{GREEN}refreshable{RESET}" if claims["refreshable"] else "browser login"
    expires_text, expires_color = _expiry_status(claims)
    return [
        f"{BOLD}Account{RESET}       : {account}",
        f"{DIM}Principal{RESET}     : {claims['principal_id']}",
        f"{DIM}Team{RESET}          : {claims['team_id']}",
        f"{DIM}Session{RESET}       : {session}",
        f"{DIM}Expires{RESET}       : {expires_color}{expires_text}{RESET}",
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
        malformed = claims.get("malformed")
        account = f"{YELLOW}malformed{RESET}" if malformed else claims.get("email", "unreadable")
        if not malformed and claims.get("name"):
            account = f"{claims['name']} <{account}>"
        is_active = path == active
        expires_text, expires_color = _expiry_status(claims)
        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{path.stem}{RESET}" if is_active else path.stem,
                "account": account,
                "type": claims.get("principal_type", "—"),
                "id": _short_id(claims.get("principal_id")),
                "team": _short_id(claims.get("team_id")),
                "created": _timestamp(str(claims.get("created_at", ""))),
                "expires": f"{expires_color}{expires_text}{RESET}",
                "data": _retention(claims.get("retention_opt_out")),
                "session": f"{claims.get('auth_mode', '—')} · {'refresh' if claims.get('refreshable') else 'browser'}",
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
    if _token_expired_or_soon(_claims(payload)):
        status = _recover_switched_auth(profile, name)
        if status != 0:
            return status
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


# ── direct OIDC refresh ──────────────────────────────────────────────────────

# x.ai access tokens live ~1h, so the proactive window is minutes — not the 24h
# codex uses for its 240h token.
_REFRESH_SKEW_SECONDS = 300

# The Grok CLI keeps the access token under "key". Only fields the record
# already carries are ever rewritten, so an unfamiliar credential shape falls
# back to the vendor CLI instead of getting an invented field.
_ACCESS_TOKEN_FIELDS = ("key", "access_token")

_TOKEN_ENDPOINTS: dict[str, str] = {}


def _token_endpoint(issuer: str) -> str | None:
    """Discover the OIDC token endpoint for `issuer`, cached per process.

    Nothing is hardcoded: the issuer comes out of the credential itself, and
    `<issuer>/.well-known/openid-configuration` names the endpoint.
    """
    if issuer in _TOKEN_ENDPOINTS:
        return _TOKEN_ENDPOINTS[issuer]
    import urllib.request  # keeps http.client/ssl off the import path

    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            document = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    endpoint = document.get("token_endpoint") if isinstance(document, dict) else None
    if not isinstance(endpoint, str) or not endpoint:
        return None
    _TOKEN_ENDPOINTS[issuer] = endpoint
    return endpoint


def _http_error_message(code: int, body: str) -> str:
    """Grok's classifier for the shared refresh helper. Three outcomes, kept
    deliberately distinct: "revoked:" (only a browser login helps),
    "client-auth:" (this OIDC client wants a secret we do not have — fall back
    to the vendor CLI, which holds one) and everything else (transient)."""
    error = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            error = str(parsed.get("error") or "")
    except ValueError:
        pass
    if error == "invalid_grant":
        return "revoked: refresh token rejected (invalid_grant)"
    if error in ("invalid_client", "unauthorized_client"):
        return f"client-auth: token endpoint requires client authentication ({error})"
    if code == 401 and not error:
        return "client-auth: token endpoint returned HTTP 401 with no OAuth error"
    return f"HTTP {code} from token endpoint{f' ({error})' if error else ''}"


def _is_revoked_error(error: str | None) -> bool:
    """The refresh token itself is dead — a fresh login is the only fix."""
    return (error or "").startswith("revoked")


def _needs_client_secret(error: str | None) -> bool:
    """Not a revocation and not a hiccup: the client must authenticate, and no
    secret is available to us. The vendor CLI has one — hand off to it."""
    return (error or "").startswith("client-auth")


def _oidc_refresh(record: JsonDict) -> tuple[JsonDict | None, str | None]:
    """Standard OIDC refresh grant against the endpoint discovered from the
    record's own issuer, attempted *without* a client secret (public/PKCE
    clients need none). (response, None) on success, (None, error) otherwise —
    never raises, never logs a token."""
    refresh_token = record.get("refresh_token")
    issuer = str(record.get("oidc_issuer") or "")
    client_id = str(record.get("oidc_client_id") or "")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None, "unsupported: credential carries no refresh token"
    if not issuer or not client_id:
        return None, "unsupported: credential carries no OIDC issuer/client id"
    endpoint = _token_endpoint(issuer)
    if endpoint is None:
        return None, "discovery failed: no token_endpoint for this issuer"
    return oauth_token_refresh(
        endpoint,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        form_encoded=True,
        headers={"Accept": "application/json"},
        http_error=_http_error_message,
    )


def _apply_refreshed(payload: JsonDict, refreshed: JsonDict) -> JsonDict | None:
    """Merge a token response into a copy of the credential payload, or None
    when the record has no access-token field to update."""
    updated = copy.deepcopy(payload)
    record = _record(updated)
    access = refreshed.get("access_token")
    if record is None or not isinstance(access, str) or not access:
        return None
    fields = [field for field in _ACCESS_TOKEN_FIELDS if field in record]
    if not fields:
        return None
    for field in fields:
        record[field] = access
    rotated = refreshed.get("refresh_token")
    if isinstance(rotated, str) and rotated:
        # Unlike Google's, an OIDC refresh grant may rotate the refresh token —
        # keep the new one when the endpoint sends it, the old one when it does not.
        record["refresh_token"] = rotated
    expires_in = refreshed.get("expires_in")
    if isinstance(expires_in, (int, float)):
        record["expires_at"] = (
            (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    return updated


def _direct_refresh(payload: JsonDict) -> tuple[JsonDict | None, str | None]:
    """One HTTPS round trip in place of a CLI launch. Returns the refreshed
    credential payload, or (None, error) for the caller to classify."""
    record = _record(payload)
    if record is None:
        return None, "unsupported: not a recognizable Grok OAuth record"
    refreshed, error = _oidc_refresh(record)
    if refreshed is None:
        return None, error
    updated = _apply_refreshed(payload, refreshed)
    if updated is None:
        return None, "unsupported: no access-token field to update"
    return updated, None


def _token_expired_or_soon(claims: JsonDict) -> bool:
    """True when the access token is gone or inside the skew window. An
    unreadable expiry returns False — no point forcing a call we can't judge."""
    value = str(claims.get("expires_at") or "")
    if not value:
        return False
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds() < _REFRESH_SKEW_SECONDS


def _recover_switched_auth(profile: Path, name: str) -> int:
    """Refresh a just-restored but (nearly) expired token in place and mirror
    the rotation back into the profile — the self-heal codex/claude already do
    on switch. Only a genuine revocation escalates to a browser login."""
    payload = _read_json(_auth_file())
    if payload is None:
        return 0
    updated, error = _direct_refresh(payload)
    if updated is not None:
        if _write_json(_auth_file(), updated):
            _write_json(profile, updated)
            print(f"{DIM}   (token was expired — refreshed in place){RESET}")
        return 0
    if _is_revoked_error(error):
        log_yellow(f"⚠️  Saved token for '{name}' is revoked — re-logging in via browser…")
        return cmd_login_switch(name)
    log_yellow(f"⚠️  Could not refresh after switch ({error}); Grok will refresh on next use.")
    return 0


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
    """Direct OIDC refresh first; the `grok models` subprocess only when the
    direct grant cannot resolve it (client secret required, unknown credential
    shape, transient failure). A revoked refresh token stops here — spawning
    the CLI cannot revive it."""
    payload = _read_json(profile)
    if payload is None:
        log_red(f"❌ Profile is unreadable: {profile.stem}")
        return 1
    is_active = _active_profile() == profile
    updated, error = _direct_refresh(payload)
    if updated is not None:
        if not _write_json(profile, updated):
            return 1
        return 0 if not is_active or _write_json(_auth_file(), updated) else 1
    if _is_revoked_error(error):
        log_red(f"❌ Refresh token revoked for {profile.stem}: {error}")
        log_yellow(f"   Re-login with: grok-accounts login-switch {profile.stem}")
        return 1
    log_yellow(f"⚠️  Direct refresh unavailable for {profile.stem} ({error}) — using the Grok CLI.")
    return _refresh_profile_via_cli(profile)


def _refresh_profile_via_cli(profile: Path) -> int:
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
    payload = _read_json(_auth_file())
    if payload is None:
        log_red("❌ No Grok login found. Run: grok login --oauth")
        return 1
    updated, error = _direct_refresh(payload)
    if updated is None and _is_revoked_error(error):
        log_red(f"❌ Refresh token revoked for the active Grok auth: {error}")
        log_yellow("   Re-login with: grok-accounts login-switch <name>")
        return 1
    if updated is not None:
        status = 0 if _write_json(_auth_file(), updated) else 1
    else:
        log_yellow(f"⚠️  Direct refresh unavailable ({error}) — using the Grok CLI.")
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
