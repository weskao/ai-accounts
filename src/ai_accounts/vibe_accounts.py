from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
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
    keychain_read,
    keychain_write,
    log_red,
    log_yellow,
    resolve_account_dir,
)
from .config_schema import mask_secret
from .usage_format import print_no_active_account

JsonDict = dict[str, Any]

HELP = """vibe-accounts — manage multiple Mistral Vibe CLI login profiles

USAGE
  vibe-accounts who                   Show the current logged-in Vibe account
  vibe-accounts current               Alias for `who`
  vibe-accounts save [<name>]         Save the current login; no name = derive from the key
  vibe-accounts list                  List saved profiles
  vibe-accounts usage                 Show only the active account
  vibe-accounts switch [<name>]       Switch by name; no name = interactive picker
  vibe-accounts remove [<name>]       Delete a saved profile; no name = interactive picker
  vibe-accounts refresh [<name>]      Verify/refresh the active/profile session (not needed for static keys)
  vibe-accounts refresh --all         Refresh every saved profile (not needed for static keys)
  vibe-accounts sync                  Copy the active auth back to its matching profile
  vibe-accounts autoswitch            Report that vibe has no quota API to switch on
  vibe-accounts login-switch <name>   Fresh Vibe configuration/setup + save as <name>
  vibe-accounts config                Interactive config menu shared by every ai-accounts CLI
  vibe-accounts config get [key]      Print the shared auto-switch config (or one key)
  vibe-accounts config set <k> <v>    Set one shared config key (rejects unknown keys)
  vibe-accounts -h | --help | help    Show this help

EXAMPLES
  vibe-accounts login-switch personal
  vibe-accounts login-switch work
  vibe-accounts list
  vibe-accounts switch
  vibe-accounts switch personal
  vibe-accounts who

Profiles live under ~/.ai-accounts/vibe/accounts/<name>.json (override with
$VIBE_ACCOUNT_DIR). Treat that directory as secrets — profiles contain API
keys.

Vibe keeps its live key in the OS keyring (macOS: login keychain, service
"ai.mistral.vibe"), falling back to $VIBE_HOME/.env; these commands read and
write whichever store vibe itself would use.
"""


def _vibe_home() -> Path:
    return Path(os.environ.get("VIBE_HOME", str(Path.home() / ".vibe")))


def _auth_file() -> Path:
    return _vibe_home() / ".env"


def _account_dir() -> Path:
    return resolve_account_dir(
        "VIBE_ACCOUNT_DIR",
        Path.home() / ".ai-accounts" / "vibe" / "accounts",
        _vibe_home() / "accounts",
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


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    res = {}
    try:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if val.startswith(('"', "'")) and val.endswith(val[0]):
                    val = val[1:-1]
                res[key] = val
    except OSError:
        pass
    return res


def _write_env(path: Path, env_dict: dict[str, str]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(f"{k}={v}\n" for k, v in env_dict.items())
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return True
    except OSError as exc:
        log_red(f"❌ Could not write {path}: {exc}")
        return False


def _set_marker(profile: Path) -> None:
    marker = _marker_file()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(profile.stem, encoding="utf-8")
    marker.chmod(0o600)


# ── the live credential ─────────────────────────────────────────────────────
# Vibe stores its API key in the OS keyring — on macOS the login keychain under
# service "ai.mistral.vibe", account = the provider's env-var name — and DELETES
# the plaintext $VIBE_HOME/.env copy once that write lands (see
# vibe/setup/auth/api_key_persistence.persist_api_key). A store that reads only
# .env therefore sees "not logged in" after any successful `vibe --setup`, which
# is why save/who/list/login-switch all came up empty.
#
# Read order below mirrors vibe's own resolution (vibe/setup/auth/auth_state.py):
# .env is loaded into the process env at startup and outranks the keyring.

_KEYCHAIN_SERVICE = "ai.mistral.vibe"
_LEGACY_KEYCHAIN_SERVICES = ("vibe",)
# Provider env-var names vibe may key on: mistral's own, plus the var used for
# OpenAI-compatible provider configs.
_KEY_ENV_VARS = ("MISTRAL_API_KEY", "OPENAI_API_KEY")


def _api_key(payload: dict[str, str] | None) -> str:
    payload = payload or {}
    return payload.get("MISTRAL_API_KEY") or payload.get("OPENAI_API_KEY") or ""


def _credential(payload: dict[str, str] | None) -> dict[str, str]:
    """Just the API-key entries of a payload — never unrelated .env vars, so a
    switch can't clobber a proxy setting captured at save time."""
    payload = payload or {}
    return {var: payload[var] for var in _KEY_ENV_VARS if payload.get(var)}


def _read_keychain_credential() -> dict[str, str]:
    for var in _KEY_ENV_VARS:
        for service in (_KEYCHAIN_SERVICE, *_LEGACY_KEYCHAIN_SERVICES):
            secret = keychain_read(service, var)
            if secret:
                return {var: secret}
    return {}


def _read_active() -> dict[str, str]:
    """The credential vibe will actually use: .env if it has one, else keyring."""
    return _credential(_read_env(_auth_file())) or _read_keychain_credential()


def _active_source() -> str:
    """Where the live key comes from — the one fact that explains a stale switch."""
    if _credential(_read_env(_auth_file())):
        return f"{_auth_file()}"
    return "OS keyring" if _read_keychain_credential() else "—"


def _write_active(payload: dict[str, str] | None) -> bool:
    """Install a saved profile's key as the live vibe credential.

    Mirrors vibe's own persist order: keyring first, .env only as the fallback
    (non-macOS, or a keychain we can't write). On keyring success any plaintext
    .env copy is dropped — it would outrank the keyring and shadow the switch.
    """
    credential = _credential(payload)
    if not credential:
        log_red("❌ No Mistral/OpenAI API key in that profile")
        return False
    env_var, api_key = next(iter(credential.items()))
    env = _read_env(_auth_file())
    if keychain_write(_KEYCHAIN_SERVICE, env_var, api_key):
        # Pop every key var, not just the one we wrote: a leftover
        # OPENAI_API_KEY line would keep winning over the keyring.
        dropped = [var for var in _KEY_ENV_VARS if env.pop(var, None) is not None]
        return _write_env(_auth_file(), env) if dropped else True
    return _write_env(_auth_file(), {**env, env_var: api_key})


def _claims(payload: dict[str, str] | None) -> dict[str, str]:
    api_key = _api_key(payload)
    if not api_key:
        return {"env_var": "—", "api_key": "—", "key_exists": "no"}
    credential = _credential(payload)
    return {
        "env_var": next(iter(credential), "—"),
        "api_key": mask_secret(api_key),
        "key_exists": "yes",
    }


def _derived_name(payload: dict[str, str] | None) -> str:
    """Profile name for a keyless `save`. Vibe's account API (/api/vibe/whoami)
    reports only a plan, never an email, so there is no address to name a
    profile after — fall back to a stable digest of the key itself, which at
    least stays unique per account instead of colliding on one shared label."""
    digest = hashlib.sha256(_api_key(payload).encode("utf-8")).hexdigest()
    return f"vibe-{digest[:8]}"


def _identity(payload: dict[str, str] | None) -> str:
    return _api_key(payload)


def _active_profile(active: dict[str, str] | None = None) -> Path | None:
    active = active if active is not None else _read_active()
    if not active:
        return None
    marker = _marker_file()
    try:
        marked = _profile_file(marker.read_text(encoding="utf-8").strip())
    except OSError:
        marked = None
    if marked is not None and _identity(_read_json(marked)) == _identity(active):
        return marked
    identity = _identity(active)
    if not identity:
        return None
    matches = [
        path
        for path in _account_dir().glob("*.json")
        if _identity(_read_json(path)) == identity
    ]
    return matches[0] if len(matches) == 1 else None


def _claims_lines(claims: dict[str, str], profile: Path | None) -> list[str]:
    if not claims or claims.get("key_exists") == "no":
        return [f"{YELLOW}No Vibe API key found — run: vibe --setup{RESET}"]
    return [
        f"{BOLD}API key{RESET}: {claims['api_key']}  {DIM}({claims['env_var']}){RESET}",
        f"{DIM}Profile{RESET}: {profile.stem if profile else 'untracked'}",
    ]


def cmd_who() -> int:
    payload = _read_active()
    claims = _claims(payload) if payload else {}
    profile = _active_profile(payload) if payload else None

    if claims and claims.get("key_exists") == "yes":
        status_lines = [
            f"{GREEN}Logged in through Mistral Vibe{RESET}",
            f"{DIM}Credential store{RESET}: {_active_source()}",
        ]
    else:
        status_lines = [
            f"{RED}Not logged in{RESET}  "
            f"{DIM}(no API key in the Vibe keyring or {_auth_file()}){RESET}"
        ]
    panel("Vibe Login Status", status_lines)

    print()
    panel("Current Auth Claims", _claims_lines(claims, profile))
    return 0 if claims and claims.get("key_exists") == "yes" else 1


def cmd_save(name: str | None = None) -> int:
    payload = _read_active()
    claims = _claims(payload)
    if claims.get("key_exists") == "no":
        log_red("❌ No valid Mistral API key found. Run: vibe --setup")
        return 1
    profile = _profile_file(_derived_name(payload) if name is None else name)
    if profile is None:
        return 1
    # Store only the credential: profiles are secrets, not .env snapshots.
    if not _write_json(profile, _credential(payload)):
        return 1
    _account_dir().chmod(0o700)  # the store holds raw API keys
    _set_marker(profile)
    success_panel(
        "Saved Vibe profile",
        profile.stem,
        _claims_lines(claims, profile),
        title=f"Profile: {profile.stem}",
        details=(f"→ {profile}",),
    )
    return 0


_TABLE_COLUMNS = [
    ("PROFILE", "profile"),
    ("ENV VAR", "env_var"),
    ("API KEY", "api_key"),
    ("STATE", "state"),
]


def cmd_list(*, only_active: bool = False) -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Vibe profiles.")
        print(
            f"{DIM}   Add one with: vibe-accounts save <profile_name>{RESET}",
            file=sys.stderr,
        )
        return 0
    active = _active_profile()
    if only_active:
        if active is None:
            print_no_active_account("Vibe", "vibe-accounts")
            return 0
        profiles = [active]
    rows = []
    for path in profiles:
        claims = _claims(_read_json(path))
        is_active = path == active
        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{path.stem}{RESET}" if is_active else path.stem,
                "env_var": claims.get("env_var", "—"),
                "api_key": claims.get("api_key", "—"),
                "state": f"{GREEN}{BOLD}ACTIVE{RESET}" if is_active else f"{DIM}—{RESET}",
            }
        )

    if only_active:
        print(f"{BOLD}Current Vibe account{RESET}")
    else:
        print(f"{BOLD}Saved Vibe profiles{RESET}  {DIM}({len(rows)}){RESET}")
    accounts_table(rows, _TABLE_COLUMNS)
    return 0


def _backup_active() -> bool:
    active = _read_active()
    if not active:
        return True
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _write_json(_backup_dir() / f".env.backup-{stamp}.json", active)


def cmd_switch(name: str) -> int:
    profile = _profile_file(name)
    payload = _read_json(profile) if profile is not None else None
    if profile is None or payload is None:
        log_red(f"❌ Profile is unreadable or missing: {name}")
        return 1
    if not _backup_active():
        return 1
    if not _write_active(payload):
        return 1
    _set_marker(profile)
    ok("Switched Vibe profile to", profile.stem)
    print(f"{DIM}   Vibe CLI will use this account/key on its next launch.{RESET}")
    print()
    return cmd_who()


def _picker_items(profiles: list[Path]) -> list[tuple[str, str | None]]:
    """(name, masked-key) pairs so the picker can tell two profiles apart."""
    items = []
    for path in profiles:
        masked = _claims(_read_json(path)).get("api_key")
        items.append((path.stem, None if masked == "—" else masked))
    return items


def cmd_switch_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Vibe profiles.")
        return 1
    items = _picker_items(profiles)
    chosen = choose_profile("a Vibe", items)
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
    ok("Removed Vibe profile", profile.stem, bold=False)
    return 0


def cmd_remove_interactive() -> int:
    profiles = sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []
    if not profiles:
        log_yellow("⚠️  No saved Vibe profiles.")
        return 1
    items = _picker_items(profiles)
    return choose_and_run("a Vibe", items, cmd_remove, cancel_message="Remove cancelled.")


def cmd_sync() -> int:
    payload = _read_active()
    profile = _active_profile(payload)
    if not payload or profile is None:
        log_yellow("⚠️  No unambiguous current profile — run: vibe-accounts switch <name>")
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
    executable = shutil.which("vibe")
    if executable is None:
        log_red(
            "❌ Mistral Vibe CLI is required. Install it: uv tool install mistral-vibe"
        )
        return 1
    result = subprocess.run([executable, "--setup"])
    return cmd_save(name) if result.returncode == 0 else result.returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    command, *rest = argv
    if command == "config":
        return cm.cmd_config(rest, prog="vibe-accounts")
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
        print("refresh not needed: vibe uses static API keys")
        return 0
    if command == "sync":
        return cmd_sync()
    if command == "autoswitch":
        print("autoswitch unsupported for vibe: no quota API")
        return 0
    if command == "login-switch":
        if not rest:
            log_red("Usage: vibe-accounts login-switch <profile_name>")
            return 1
        return cmd_login_switch(rest[0])
    log_red(f"❌ Unknown or incomplete command: {command}")
    print(HELP)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
