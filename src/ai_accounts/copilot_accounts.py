"""Manage multiple GitHub Copilot CLI login profiles.

Structure mirrors ``vibe_accounts`` (the previous provider added): same
subcommand surface, same profile store, same ``--json`` shape.

Where the Copilot CLI keeps its login (verified against a real ``/login`` on
macOS): ``~/.copilot/config.json`` is JSONC — leading ``//`` comment lines,
then JSON — and names the signed-in account under ``lastLoggedInUser``
(``{"host", "login"}``) with no token in it; the OAuth token itself is a
Keychain generic-password item, service ``copilot-cli``, account
``<host>:<login>``. ``switch`` therefore writes that keyring item and points
``lastLoggedInUser`` at the profile. The remaining unverified pieces (env-var
precedence, the quota endpoint, non-macOS stores) still carry an
``# ASSUMPTION:`` comment and fall through quietly rather than raise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config_menu as cm
from . import copilot_usage
from . import i18n
from ._present import (
    accounts_table,
    choose_and_run,
    choose_profile,
    format_help,
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
    email_local_part,
    fetch_parallel,
    keychain_read,
    keychain_write,
    log_red,
    log_yellow,
    resolve_account_dir,
)
from .config_schema import mask_secret
from .usage_format import (
    align_usage_cells,
    format_usage_window,
    json_empty_list,
    print_no_active_account,
    usage_window_to_json,
)

JsonDict = dict[str, Any]

HELP = """copilot-accounts — manage multiple GitHub Copilot CLI login profiles

USAGE
  copilot-accounts who                   Show the current logged-in Copilot account
  copilot-accounts current               Alias for `who`
  copilot-accounts save [<name>]         Save the current login; no name = derive from
                                          the GitHub account
  copilot-accounts list [--json]         List saved profiles with premium-request quota;
                                          --json prints one JSON array instead of the table
  copilot-accounts usage [--json]        Show only the active account; --json prints
                                          one JSON array instead of the table
  copilot-accounts switch [<name>]       Switch by name; no name = interactive picker
  copilot-accounts remove [<name>]       Delete a saved profile; no name = interactive picker
  copilot-accounts refresh [<name>]      Verify the profile's token is still valid
  copilot-accounts refresh --all         Verify every saved profile's token
  copilot-accounts sync                  Copy the active auth back to its matching profile
  copilot-accounts autoswitch            Report Copilot's autoswitch support
  copilot-accounts login-switch <name>   Fresh Copilot CLI login + save as <name>
  copilot-accounts config                Interactive config menu shared by every ai-accounts CLI
  copilot-accounts config get [key]      Print the shared auto-switch config (or one key)
  copilot-accounts config set <k> <v>    Set one shared config key (rejects unknown keys)
  copilot-accounts -h | --help | help    Show this help

EXAMPLES
  copilot-accounts login-switch personal
  copilot-accounts login-switch work
  copilot-accounts list
  copilot-accounts switch
  copilot-accounts switch personal
  copilot-accounts who

Profiles live under ~/.ai-accounts/copilot/accounts/<name>.json (override with
$COPILOT_ACCOUNT_DIR). Treat that directory as secrets — profiles contain
GitHub tokens.

The signed-in login comes from the Copilot CLI's config.json (~/.copilot, honoring
$XDG_CONFIG_HOME) and its token from the OS keyring, then $COPILOT_GITHUB_TOKEN /
$GH_TOKEN / $GITHUB_TOKEN. An exported token wins over anything these commands write, so
`switch` warns when one is set.
"""


# ── where the Copilot CLI keeps its login ───────────────────────────────────
# Verified against a real `/login` on macOS: the config dir is ~/.copilot
# (COPILOT_HOME / $XDG_CONFIG_HOME override, like most GitHub tooling), its
# config.json is JSONC (leading `//` comment lines) whose `lastLoggedInUser`
# is {"host", "login"} with *no token*, and the token is a Keychain
# generic-password item: service "copilot-cli", account "<host>:<login>".
# ASSUMPTION: Linux/Windows stores are unverified — keychain_read is a no-op
# there, so those platforms read as "not logged in" until someone checks.

def _copilot_home() -> Path:
    explicit = os.environ.get("COPILOT_HOME")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "copilot"
    return Path.home() / ".copilot"


_CONFIG_FILENAME = "config.json"
_DEFAULT_HOST = "https://github.com"
_KEYCHAIN_SERVICE = "copilot-cli"
# ASSUMPTION: env-var precedence. The Copilot CLI is documented to read
# $GITHUB_TOKEN / $GH_TOKEN; $COPILOT_GITHUB_TOKEN is the Copilot-specific
# override reported to outrank both. Exact order unconfirmed — the warning in
# `switch`/`who` exists precisely because any of them can shadow a switch.
_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

_USER_URL = "https://api.github.com/user"
_API_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ai-accounts"}


def _account_dir() -> Path:
    return resolve_account_dir(
        "COPILOT_ACCOUNT_DIR",
        Path.home() / ".ai-accounts" / "copilot" / "accounts",
        _copilot_home() / "accounts",
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


def _split_jsonc(text: str) -> tuple[str, str]:
    """(header, body): the Copilot CLI's config.json opens with `//` comment
    lines ("This file is managed automatically") that json.loads rejects.
    Split them off so the body parses and the header can be written back."""
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines) and lines[index].lstrip().startswith("//"):
        index += 1
    return "".join(lines[:index]), "".join(lines[index:])


def _read_json(path: Path) -> JsonDict | None:
    try:
        value = json.loads(_split_jsonc(path.read_text(encoding="utf-8"))[1])
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value else None


def _write_json(path: Path, payload: JsonDict, *, header: str = "") -> bool:
    """Atomically write `payload`; `header` (the CLI's `//` comment lines) is
    kept verbatim in front so a managed file still looks like its own."""
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
            handle.write(header)
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


def _config_path() -> Path:
    return _copilot_home() / _CONFIG_FILENAME


def _logged_in_user(document: JsonDict | None = None) -> tuple[str, str] | None:
    """(host, login) the CLI is signed in as — config.json's lastLoggedInUser."""
    document = _read_json(_config_path()) if document is None else document
    user = (document or {}).get("lastLoggedInUser")
    if not isinstance(user, dict):
        return None
    host, login = user.get("host"), user.get("login")
    if isinstance(host, str) and host and isinstance(login, str) and login:
        return host, login
    return None


def _keychain_account(host: str, login: str) -> str:
    return f"{host}:{login}"


def _read_keychain_token() -> str | None:
    user = _logged_in_user()
    if user is None:
        return None
    secret = keychain_read(_KEYCHAIN_SERVICE, _keychain_account(*user))
    return secret.strip() if secret and secret.strip() else None


def _env_token() -> tuple[str, str] | None:
    """(var, token) for the first exported GitHub token, if any."""
    for var in _ENV_VARS:
        token = (os.environ.get(var) or "").strip()
        if token:
            return var, token
    return None


def _read_active() -> str | None:
    """The token the Copilot CLI would use: the keyring item for config.json's
    signed-in login, then an exported env var as the last resort."""
    from_keychain = _read_keychain_token()
    if from_keychain:
        return from_keychain
    env = _env_token()
    return env[1] if env else None


def _active_source() -> str:
    """Where the live token comes from — the one fact that explains a stale switch."""
    user = _logged_in_user()
    if user is not None and _read_keychain_token():
        return f"OS keyring ({_KEYCHAIN_SERVICE} · {_keychain_account(*user)})"
    env = _env_token()
    return f"${env[0]}" if env else "—"


def _warn_env_shadow() -> None:
    env = _env_token()
    if env is None:
        return
    log_yellow(f"⚠️  ${env[0]} is exported — it can shadow the account you switch to.")
    print(f"{DIM}   Unset it for these commands to take effect: unset {env[0]}{RESET}", file=sys.stderr)


def _write_active(token: str, host: str, login: str) -> bool:
    """Install a saved profile as the live Copilot credential: its token goes
    into the keyring item the CLI reads for `<host>:<login>`, then config.json's
    `lastLoggedInUser` is pointed at that account (and it is added to
    `loggedInUsers`), keeping the `//` header the CLI writes."""
    if not token.strip():
        # Never persist an empty secret — an empty keychain item reads back as
        # a successful login and logs the user out of the real one.
        log_red("❌ Refusing to install an empty Copilot token")
        return False
    token = token.strip()

    if not keychain_write(_KEYCHAIN_SERVICE, _keychain_account(host, login), token):
        log_red("❌ Could not write the Copilot token to the OS keyring")
        return False

    path = _config_path()
    try:
        header, _ = _split_jsonc(path.read_text(encoding="utf-8"))
    except OSError:
        header = ""
    document = _read_json(path) or {}
    user = {"host": host, "login": login}
    document["lastLoggedInUser"] = user
    users = document.get("loggedInUsers")
    users = [u for u in users if isinstance(u, dict)] if isinstance(users, list) else []
    if user not in users:
        users.append(user)
    document["loggedInUsers"] = users
    # The env-shadow warning is left to cmd_who, which every switch ends with —
    # warning here too printed it twice for one `switch`.
    return _write_json(path, document, header=header)


# ── GitHub identity ─────────────────────────────────────────────────────────

def _fetch_identity(token: str, *, timeout: float = 20) -> JsonDict | None:
    """``login``/``email`` for a token, or None when the call fails. Only ever
    used to label a profile, so every failure is silent."""
    request = urllib.request.Request(
        _USER_URL,
        headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _token(payload: JsonDict | None) -> str:
    value = (payload or {}).get("oauth_token")
    return value if isinstance(value, str) else ""


def _label(payload: JsonDict | None) -> str:
    payload = payload or {}
    for key in ("login", "email"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "—"


def _claims(payload: JsonDict | None) -> dict[str, str]:
    token = _token(payload)
    if not token:
        return {"account": "—", "token": "—", "token_exists": "no"}
    return {"account": _label(payload), "token": mask_secret(token), "token_exists": "yes"}


def _derived_name(payload: JsonDict) -> str:
    """Profile name for a keyless `save`: the GitHub login, else the email's
    local part, else a stable digest of the token (never a shared label two
    accounts would collide on)."""
    login = payload.get("login")
    if isinstance(login, str) and login:
        return login
    email = payload.get("email")
    if isinstance(email, str) and email:
        return email_local_part(email)
    digest = hashlib.sha256(_token(payload).encode("utf-8")).hexdigest()
    return f"copilot-{digest[:8]}"


def _active_profile(token: str | None = None) -> Path | None:
    token = token if token is not None else _read_active()
    if not token:
        return None
    marker = _marker_file()
    try:
        marked = _profile_file(marker.read_text(encoding="utf-8").strip())
    except OSError:
        marked = None
    if marked is not None and _token(_read_json(marked)) == token:
        return marked
    matches = [path for path in _profiles() if _token(_read_json(path)) == token]
    return matches[0] if len(matches) == 1 else None


def _profiles() -> list[Path]:
    account_dir = _account_dir()
    return sorted(account_dir.glob("*.json")) if account_dir.is_dir() else []


def _claims_lines(claims: dict[str, str], profile: Path | None) -> list[str]:
    if not claims or claims.get("token_exists") == "no":
        return [f"{YELLOW}No Copilot token found — run: copilot-accounts login-switch <name>{RESET}"]
    return [
        f"{BOLD}Account{RESET}: {claims['account']}",
        f"{BOLD}Token{RESET}: {claims['token']}",
        f"{DIM}Profile{RESET}: {profile.stem if profile else 'untracked'}",
    ]


# ── commands ────────────────────────────────────────────────────────────────

def cmd_who() -> int:
    token = _read_active()
    profile = _active_profile(token) if token else None
    payload = _read_json(profile) if profile else None
    if payload is None and token:
        # Untracked login: still name the account from config.json so `who`
        # does not show "—" for a signed-in user who just has no profile yet.
        payload = {"oauth_token": token}
        user = _logged_in_user()
        if user is not None:
            payload["host"], payload["login"] = user
    claims = _claims(payload or {})

    if token:
        status_lines = [
            f"{GREEN}Logged in through GitHub Copilot{RESET}",
            f"{DIM}Credential store{RESET}: {_active_source()}",
        ]
    else:
        status_lines = [
            f"{RED}Not logged in{RESET}  "
            f"{DIM}(no lastLoggedInUser in {_config_path()}, no keyring item, or ${_ENV_VARS[0]}){RESET}"
        ]
    panel("Copilot Login Status", status_lines)

    print()
    panel("Current Auth Claims", _claims_lines(claims, profile))
    if token:
        _warn_env_shadow()
    return 0 if token else 1


def cmd_save(name: str | None = None) -> int:
    token = _read_active()
    if not token:
        log_red("❌ No GitHub Copilot login found. Run: copilot-accounts login-switch <name>")
        return 1
    payload: JsonDict = {"oauth_token": token}
    user = _logged_in_user()
    if user is not None:
        # config.json's login is what keys the keyring item, so it outranks the
        # /user lookup (which only fills in email/id, or login on an env token).
        payload["host"], payload["login"] = user
    identity = _fetch_identity(token) or {}
    for key in ("login", "email", "id"):
        value = identity.get(key)
        if value and key not in payload:
            payload[key] = value

    profile = _profile_file(_derived_name(payload) if name is None else name)
    if profile is None:
        return 1
    if not _write_json(profile, payload):
        return 1
    _account_dir().chmod(0o700)  # the store holds raw GitHub tokens
    _set_marker(profile)
    success_panel(
        "Saved Copilot profile",
        profile.stem,
        _claims_lines(_claims(payload), profile),
        title=f"Profile: {profile.stem}",
        details=(f"→ {profile}",),
    )
    return 0


_TABLE_COLUMNS = [
    ("PROFILE", "profile"),
    ("ACCOUNT", "account"),
    ("PLAN", "plan"),
    ("PREMIUM", "usage_premium"),
    ("UPDATED", "usage_updated"),
    ("STATE", "state"),
]


def _has_quota(usage: copilot_usage.UsageSnapshot) -> bool:
    return any((usage.premium, usage.chat, usage.completions))


def _usage_cell(window) -> str:
    if window is None:
        return f"{DIM}—{RESET}"
    color = usage_color(window.percentage)
    return format_usage_window(window, "1month", f"{color}{window.percentage}%{RESET}")


def cmd_list(*, fetch_usage: bool = True, only_active: bool = False, json_output: bool = False) -> int:
    profiles = _profiles()
    if not profiles:
        if json_empty_list(json_output):
            return 0
        log_yellow("⚠️  No saved Copilot profiles.")
        print(
            f"{DIM}   Add one with: copilot-accounts save <profile_name>{RESET}",
            file=sys.stderr,
        )
        return 0

    active = _active_profile()
    if only_active:
        if active is None:
            if json_empty_list(json_output):
                return 0
            print_no_active_account("Copilot", "copilot-accounts")
            return 0
        profiles = [active]

    empty = copilot_usage.empty_usage()
    if fetch_usage:
        spinner = Spinner("Fetching Copilot quota…")
        with spinner:
            usages = fetch_parallel(
                profiles,
                lambda path: copilot_usage.fetch_usage(_token(_read_json(path))),
                spinner,
                "Fetching Copilot quota…",
                labels=[path.stem for path in profiles],
            )
    else:
        usages = [empty] * len(profiles)

    if json_output:
        # Same envelope every other provider prints. `quota_snapshots` is an
        # undocumented endpoint, so a profile whose quota could not be read
        # degrades to grok/vibe's no-quota shape instead of failing the command.
        print(
            json.dumps(
                [
                    {
                        "name": path.stem,
                        "active": path == active,
                        "usage": {
                            "premium": usage_window_to_json(usage.premium),
                            "chat": usage_window_to_json(usage.chat),
                            "completions": usage_window_to_json(usage.completions),
                            "plan": usage.plan,
                            "refreshed_at": usage.refreshed_at,
                            "error": usage.error,
                        }
                        if _has_quota(usage)
                        else None,
                        "no_quota_api": not _has_quota(usage),
                    }
                    for path, usage in zip(profiles, usages)
                ]
            )
        )
        return 0

    rows = []
    for path, usage in zip(profiles, usages):
        payload = _read_json(path)
        is_active = path == active
        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{path.stem}{RESET}" if is_active else path.stem,
                "account": _label(payload),
                "plan": usage.plan or f"{DIM}—{RESET}",
                "usage_premium": _usage_cell(usage.premium),
                "usage_updated": copilot_usage.format_refreshed_at(usage),
                "state": f"{GREEN}{BOLD}ACTIVE{RESET}" if is_active else f"{DIM}—{RESET}",
            }
        )
    align_usage_cells(rows, "usage_premium")

    if only_active:
        print(f"{BOLD}Current Copilot account{RESET}")
    else:
        print(f"{BOLD}Saved Copilot profiles{RESET}  {DIM}({len(rows)}){RESET}")
    accounts_table(rows, _TABLE_COLUMNS)
    return 0


def _backup_active() -> bool:
    token = _read_active()
    if not token:
        return True
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _write_json(_backup_dir() / f"token.backup-{stamp}.json", {"oauth_token": token})


def cmd_switch(name: str) -> int:
    profile = _profile_file(name)
    payload = _read_json(profile) if profile is not None else None
    if profile is None or payload is None:
        log_red(f"❌ Profile is unreadable or missing: {name}")
        return 1
    if not _token(payload):
        log_red(f"❌ Profile has no Copilot token: {name}")
        return 1
    login = payload.get("login")
    if not isinstance(login, str) or not login:
        log_red(
            "❌ Profile has no GitHub login to key the keyring item on — "
            f"re-save it: copilot-accounts save {name}"
        )
        return 1
    host = payload.get("host")
    host = host if isinstance(host, str) and host else _DEFAULT_HOST
    if not _backup_active():
        return 1
    if not _write_active(_token(payload), host, login):
        return 1
    _set_marker(profile)
    ok("Switched Copilot profile to", profile.stem)
    print(f"{DIM}   Copilot CLI will use this account on its next launch.{RESET}")
    print()
    return cmd_who()


def _picker_items(profiles: list[Path]) -> list[tuple[str, str | None]]:
    """(name, account) pairs so the picker can tell two profiles apart."""
    items = []
    for path in profiles:
        label = _label(_read_json(path))
        items.append((path.stem, None if label == "—" else label))
    return items


def cmd_switch_interactive() -> int:
    profiles = _profiles()
    if not profiles:
        log_yellow("⚠️  No saved Copilot profiles.")
        return 1
    chosen = choose_profile("a Copilot", _picker_items(profiles))
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
    ok("Removed Copilot profile", profile.stem, bold=False)
    return 0


def cmd_remove_interactive() -> int:
    profiles = _profiles()
    if not profiles:
        log_yellow("⚠️  No saved Copilot profiles.")
        return 1
    items = _picker_items(profiles)
    return choose_and_run("a Copilot", items, cmd_remove, cancel_message="Remove cancelled.")


def cmd_sync() -> int:
    token = _read_active()
    profile = _active_profile(token)
    if not token or profile is None:
        log_yellow("⚠️  No unambiguous current profile — run: copilot-accounts switch <name>")
        return 1
    payload = _read_json(profile) or {}
    payload["oauth_token"] = token
    user = _logged_in_user()
    if user is not None:
        payload["host"], payload["login"] = user
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


def cmd_refresh(name: str | None = None, *, everything: bool = False) -> int:
    """Verify a token is still accepted by GitHub.

    ASSUMPTION: Copilot CLI tokens are long-lived GitHub OAuth tokens with no
    refresh token, so — like vibe — there is nothing to renew; the useful
    answer is whether the saved token still authenticates.
    """
    if everything:
        profiles = _profiles()
    else:
        target = _profile_file(name) if name else _active_profile()
        profiles = [target] if target is not None and target.is_file() else []
    if not profiles:
        log_yellow("⚠️  No Copilot profile to verify.")
        return 1

    failed = 0
    for path in profiles:
        token = _token(_read_json(path))
        if token and _fetch_identity(token) is not None:
            ok("Token still valid", path.stem, bold=False)
            continue
        failed += 1
        log_yellow(f"⚠️  Token for '{path.stem}' is not accepted by GitHub — re-login required.")
        log_yellow(f"   Re-login with: copilot-accounts login-switch {path.stem}")
    return 1 if failed else 0


def cmd_login_switch(name: str) -> int:
    executable = shutil.which("copilot")
    if executable is None:
        log_red("❌ GitHub Copilot CLI is required. Install it: npm install -g @github/copilot")
        return 1
    # ASSUMPTION: `copilot login` is the non-interactive login entry point. If
    # this build has no such subcommand it exits non-zero and the hint below
    # points at the in-REPL `/login` flow instead.
    result = subprocess.run([executable, "login"])
    if result.returncode != 0:
        log_yellow("⚠️  `copilot login` failed — log in with `/login` inside `copilot`, then:")
        log_yellow(f"   copilot-accounts save {name}")
        return result.returncode
    return cmd_save(name)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(format_help(i18n.t("help.copilot", default=HELP)))
        return 0
    command, *rest = argv
    if command == "config":
        return cm.cmd_config(rest, prog="copilot-accounts")
    if command in ("who", "current"):
        return cmd_who()
    if command == "save":
        return cmd_save(rest[0] if rest else None)
    if command == "list":
        return cmd_list(json_output="--json" in rest)
    if command == "usage":
        return cmd_list(only_active=True, json_output="--json" in rest)
    if command == "switch":
        return cmd_switch(rest[0]) if rest else cmd_switch_interactive()
    if command == "remove":
        return cmd_remove(rest[0]) if rest else cmd_remove_interactive()
    if command == "refresh":
        if "--all" in rest:
            return cmd_refresh(everything=True)
        return cmd_refresh(rest[0] if rest else None)
    if command == "sync":
        return cmd_sync()
    if command == "autoswitch":
        # The quota endpoint copilot_usage reads is undocumented and unverified,
        # so no auto-switch decision is wired to it yet.
        print("autoswitch unsupported for copilot: quota API unverified")
        return 0
    if command == "login-switch":
        if not rest:
            log_red("Usage: copilot-accounts login-switch <profile_name>")
            return 1
        return cmd_login_switch(rest[0])
    log_red(f"❌ Unknown or incomplete command: {command}")
    print(format_help(i18n.t("help.copilot", default=HELP)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
