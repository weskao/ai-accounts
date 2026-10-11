from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import autoswitch
from . import config_menu as cm
from . import devin_usage
from . import i18n
from . import profile_secrets
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
    log_unknown_command,
    BOLD,
    DIM,
    GREEN,
    RED,
    RESET,
    Spinner,
    email_local_part,
    fetch_parallel,
    has_control_chars,
    log_red,
    log_yellow,
    normalize_command_argv,
    plan_tier_color,
    atomic_write_text,
    quiet_keyboard_interrupt,
    resolve_account_dir,
)
from .config_schema import mask_secret
from .usage_format import (
    UsageWindow,
    format_refreshed_at,
    format_usage_window,
    json_empty_list,
    print_no_active_account,
    usage_window_to_json,
)

JsonDict = dict[str, Any]

HELP = """devin-accounts — manage multiple Devin CLI login profiles

USAGE
  devin-accounts who                   Show the current logged-in Devin account
  devin-accounts current               Alias for `who`
  devin-accounts save [<name>]         Save the current login; no name = derive from email
  devin-accounts list [--json]         List saved profiles with plan and daily/weekly
                                        usage; --json prints one JSON array instead of
                                        the table (degrades to usage: null,
                                        no_quota_api: true if the quota endpoint fails)
  devin-accounts usage [--json]        Show only the active account (plan & usage);
                                        --json prints one JSON array instead of the table
  devin-accounts switch [<name>]       Switch by name; no name = interactive picker
  devin-accounts remove [<name>]       Delete a saved profile; no name = interactive picker
  devin-accounts refresh [<name>]      Check the active/named profile's API key is still valid
  devin-accounts refresh --all         Check every saved profile's API key
  devin-accounts sync                  Copy the live login back to its matching profile
  devin-accounts autoswitch            Switch away from the active profile if its quota
                                        is past the threshold
  devin-accounts login-switch <name>   Fresh `devin auth login` + save as <name>
  devin-accounts config                Interactive config menu shared by every ai-accounts CLI
  devin-accounts config get [key]      Print the shared auto-switch config (or one key)
  devin-accounts config set <k> <v>    Set one shared config key (rejects unknown keys)
  devin-accounts -h | --help | help    Show this help

EXAMPLES
  devin-accounts login-switch personal
  devin-accounts login-switch work
  devin-accounts list
  devin-accounts switch work
  devin-accounts refresh --all

The live login is credentials.toml in the Devin data directory:
$XDG_DATA_HOME/devin (default ~/.local/share/devin) on macOS/Linux,
%APPDATA%\\devin on Windows; override the directory with $DEVIN_DATA_DIR.

Profiles live under ~/.ai-accounts/devin/accounts/<name>.json (override with
$DEVIN_ACCOUNT_DIR). Treat that directory as sensitive — a profile's API key
lives in the OS credential store when one is available, or inline as a
plaintext fallback otherwise. Devin API keys do not expire on their own, so
`refresh` only verifies each key is still accepted and never modifies a login.
"""

_SECRET_FIELDS = ("windsurf_api_key",)
# Profile-only fields: never written back to credentials.toml.
_META_FIELDS = ("email", "key_id")


def _data_dir() -> Path:
    """Devin's data dir. $DEVIN_DATA_DIR is an ai-accounts-only override (tests)."""
    override = os.environ.get("DEVIN_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":  # Windows: %APPDATA%\devin
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "devin"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "devin"


def _auth_file() -> Path:
    return _data_dir() / "credentials.toml"


def _account_dir() -> Path:
    # Legacy == default: there is no older location to migrate from, and a legacy
    # path inside Devin's own data dir would be moved out from under Devin.
    default = Path.home() / ".ai-accounts" / "devin" / "accounts"
    return resolve_account_dir("DEVIN_ACCOUNT_DIR", default, default)


def _profile_file(name: str) -> Path | None:
    if has_control_chars(name):
        log_red("❌ Profile name contains invalid characters (control characters)")
        return None
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not safe:
        log_red("❌ Profile name cannot be empty")
        return None
    return _account_dir() / f"{safe}.json"


def _marker_file() -> Path:
    return _account_dir() / ".current-profile"


def _profiles() -> list[Path]:
    return sorted(_account_dir().glob("*.json")) if _account_dir().is_dir() else []


def _set_marker(profile: Path) -> None:
    """Record the active profile. Only a hint (the live key digest is the
    truth), so a failed write warns instead of aborting a finished switch."""
    marker = _marker_file()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(profile.stem, encoding="utf-8")
        marker.chmod(0o600)
    except OSError as exc:
        log_yellow(f"⚠️  Could not record the active profile marker: {exc}")


# ── credentials.toml ─────────────────────────────────────────────────────────

_TOML_LINE = re.compile(r"^([A-Za-z0-9_-]+)\s*=\s*(.*)$")


def _parse_toml(text: str) -> dict[str, str]:
    """Top-level ``key = "string"`` pairs of a TOML file.

    shortcut: only string values before the first [table] are read (all that
    credentials.toml holds; python>=3.10 has no tomllib) — upgrade to tomllib
    if the file ever carries other types.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            break
        match = _TOML_LINE.match(line)
        if not match:
            continue
        key, rest = match.groups()
        if rest.startswith('"'):
            try:
                value, _end = json.JSONDecoder().raw_decode(rest)
            except ValueError:
                continue
        elif rest.startswith("'") and rest.find("'", 1) > 0:
            value = rest[1 : rest.find("'", 1)]
        else:
            continue
        if isinstance(value, str):
            result[key] = value
    return result


def _read_creds() -> JsonDict | None:
    """The live login, or None when the file is missing or holds no API key."""
    try:
        creds = _parse_toml(_auth_file().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return creds if creds.get("windsurf_api_key") else None


def _backup_live() -> None:
    """Keep the RAW live credentials.toml (every key/table, not the parsed
    subset) as the one-deep pre-switch backup. No file = nothing to keep."""
    try:
        raw = _auth_file().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if raw.strip():
        profile_secrets.backup("devin", raw)


def _write_creds(path: Path, creds: JsonDict) -> bool:
    """Atomically write flat ``key = "value"`` lines, 0600 on POSIX.

    Contract: exactly the top-level string keys of *creds* (the saved profile,
    minus its email/key_id metadata) are written — no allow-list. Non-string
    values and ``[tables]`` in the previous file are NOT carried over; callers
    keep the raw pre-switch text via ``profile_secrets.backup`` (one-deep) so
    nothing is lost for good. A JSON
    string is a valid TOML basic string; ``ensure_ascii=False`` keeps non-BMP
    characters raw (``\\ud83d\\ude00`` surrogate escapes are invalid TOML).
    """
    text = "".join(f"{k} = {json.dumps(str(v), ensure_ascii=False)}\n" for k, v in creds.items())
    try:
        atomic_write_text(path, text)
        return True
    except (OSError, ValueError) as exc:
        log_red(f"❌ Could not write {path}: {exc}")
        return False


# ── profiles ─────────────────────────────────────────────────────────────────

def _key(creds: JsonDict | None) -> str | None:
    value = (creds or {}).get("windsurf_api_key")
    return value if isinstance(value, str) and value else None


def _key_id(key: str) -> str:
    """Short digest identifying a key without revealing it (non-secret metadata)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _load_profile_metadata(path: Path) -> JsonDict:
    """Metadata-only read (email/key_id) — never touches the secret store."""
    return profile_secrets.load_metadata(path) or {}


def _load_profile(path: Path) -> JsonDict | None:
    return profile_secrets.load(path, "devin", _SECRET_FIELDS)


def _save_profile(path: Path, creds: JsonDict, email: str | None = None) -> bool:
    key = _key(creds)
    if key is None:
        return False
    # A re-save (sync/refresh) keeps the email learned earlier.
    email = email or _load_profile_metadata(path).get("email")
    payload = {k: v for k, v in creds.items() if k not in _META_FIELDS}
    payload["key_id"] = _key_id(key)
    if email:
        payload["email"] = email
    try:
        profile_secrets.save(path, "devin", payload, _SECRET_FIELDS)
        return True
    except OSError as exc:
        log_red(f"❌ Could not write {path}: {exc}")
        return False


def _live_creds(profile: JsonDict) -> JsonDict:
    return {k: v for k, v in profile.items() if k not in _META_FIELDS}


def _active_profile(live: JsonDict | None = None) -> Path | None:
    """The profile whose key digest matches the live login (metadata only)."""
    live = live if live is not None else _read_creds()
    key = _key(live)
    if key is None:
        return None
    key_id = _key_id(key)
    try:
        marked = _profile_file(_marker_file().read_text(encoding="utf-8").strip())
    except OSError:
        marked = None
    if marked is not None and _load_profile_metadata(marked).get("key_id") == key_id:
        return marked
    matches = [p for p in _profiles() if _load_profile_metadata(p).get("key_id") == key_id]
    return matches[0] if len(matches) == 1 else None


def _identity_label(path: Path) -> str | None:
    return _load_profile_metadata(path).get("email") or None


# ── who / save ───────────────────────────────────────────────────────────────

def _plan_cell(plan: str | None) -> str:
    if not plan:
        return f"{DIM}—{RESET}"
    if plan.lower() == "free":
        return plan
    return f"{plan_tier_color(plan)}{plan}{RESET}"


def _claims_lines(
    creds: JsonDict,
    profile: Path | None,
    usage: devin_usage.UsageSnapshot | None = None,
    email: str | None = None,
) -> list[str]:
    key = _key(creds) or ""
    account = (usage.email if usage else None) or email or "—"
    return [
        f"{BOLD}Account{RESET}       : {account}",
        f"{DIM}Plan{RESET}          : {_plan_cell(usage.plan if usage else None)}",
        f"{DIM}API key{RESET}       : {mask_secret(key)}",
        f"{DIM}API server{RESET}    : {creds.get('api_server_url') or '—'}",
        f"{DIM}Profile{RESET}       : {profile.stem if profile else 'untracked'}",
    ]


def cmd_who() -> int:
    creds = _read_creds()
    if creds is None:
        panel("Devin Login Status", [f"{RED}Not logged in{RESET}  {DIM}(run `devin auth login`){RESET}"])
        return 1
    usage = devin_usage.fetch_usage(_key(creds))
    panel(
        "Devin Login Status",
        [
            f"{GREEN}Logged in through Devin CLI{RESET}",
            f"{DIM}Active account{RESET}: {usage.email or '—'}",
        ],
    )
    print()
    panel("Current Devin Login", _claims_lines(creds, _active_profile(creds), usage))
    return 0


def cmd_save(name: str | None = None) -> int:
    creds = _read_creds()
    if creds is None:
        log_red("❌ No Devin login found. Run: devin auth login")
        return 1
    key = _key(creds) or ""
    email = devin_usage.fetch_usage(key).email
    if name is None:
        name = email_local_part(email) if email else f"devin-{_key_id(key)[:8]}"
    profile = _profile_file(name)
    if profile is None or not _save_profile(profile, creds, email):
        return 1
    _set_marker(profile)
    success_panel(
        "Saved Devin profile",
        profile.stem,
        _claims_lines(creds, profile, email=email),
        title=f"Profile: {profile.stem}",
        details=(f"→ {profile}",),
    )
    return 0


# ── list / usage ─────────────────────────────────────────────────────────────

def _usage_cell(window: UsageWindow | None, kind: str) -> str:
    if window is None:
        return f"{DIM}—{RESET}"
    percent = f"{usage_color(window.percentage)}{window.percentage}%{RESET}"
    return format_usage_window(window, kind, percent)


def _fetch_profile(path: Path) -> devin_usage.UsageSnapshot:
    return devin_usage.fetch_usage(_key(_load_profile(path)))


_TABLE_COLUMNS = [
    ("PROFILE", "profile"),
    ("ACCOUNT", "account"),
    ("PLAN", "plan"),
    ("1D USED", "usage_1d"),
    ("1W USED", "usage_1w"),
    ("UPDATED", "usage_updated"),
    ("STATE", "state"),
]


def cmd_list(*, only_active: bool = False, json_output: bool = False) -> int:
    profiles = _profiles()
    if not profiles:
        if json_empty_list(json_output):
            return 0
        log_yellow("⚠️  No saved Devin profiles.")
        print(f"{DIM}   Add one with: devin-accounts save <profile_name>{RESET}", file=sys.stderr)
        return 0
    active = _active_profile()
    if only_active:
        if active is None:
            if json_empty_list(json_output):
                return 0
            print_no_active_account("Devin", "devin-accounts")
            return 0
        profiles = [active]

    spinner = Spinner("Fetching Devin quota…")
    with spinner:
        usages = fetch_parallel(
            profiles, _fetch_profile, spinner, "Fetching Devin quota…", labels=[p.stem for p in profiles]
        )

    if json_output:
        entries: list[JsonDict] = []
        for path, usage in zip(profiles, usages):
            has_quota = usage.daily is not None or usage.weekly is not None
            entry: JsonDict = {"name": path.stem, "active": path == active}
            if has_quota:
                entry["plan"] = usage.plan
                entry["usage"] = {
                    "daily": usage_window_to_json(usage.daily),
                    "weekly": usage_window_to_json(usage.weekly),
                    "plan": usage.plan,
                    "refreshed_at": usage.refreshed_at,
                    "error": usage.error,
                }
            else:
                entry["usage"] = None
            entry["no_quota_api"] = not has_quota
            entries.append(entry)
        print(json.dumps(entries))
        return 0

    rows = []
    for path, usage in zip(profiles, usages):
        is_active = path == active
        rows.append(
            {
                "profile": f"{GREEN}{BOLD}{path.stem}{RESET}" if is_active else path.stem,
                "account": usage.email or _identity_label(path) or "—",
                "plan": _plan_cell(usage.plan),
                "usage_1d": _usage_cell(usage.daily, "1d"),
                "usage_1w": _usage_cell(usage.weekly, "1w"),
                "usage_updated": format_refreshed_at(usage),
                "state": f"{GREEN}{BOLD}ACTIVE{RESET}" if is_active else f"{DIM}—{RESET}",
            }
        )
    print(
        f"{BOLD}Current Devin account{RESET}"
        if only_active
        else f"{BOLD}Saved Devin profiles{RESET}  {DIM}({len(rows)}){RESET}"
    )
    accounts_table(rows, _TABLE_COLUMNS, align_keys=("usage_1d", "usage_1w"))
    return 0


# ── switch / remove / sync ───────────────────────────────────────────────────

def cmd_switch(name: str) -> int:
    profile = _profile_file(name)
    if profile is None:
        return 1
    payload = _load_profile(profile)
    if payload is None or _key(payload) is None:
        log_red(f"❌ Profile is unreadable or missing: {name}")
        return 1
    _backup_live()
    if not _write_creds(_auth_file(), _live_creds(payload)):
        return 1
    _set_marker(profile)
    ok("Switched Devin profile to", profile.stem)
    print(f"{DIM}   Devin CLI will use this account on its next launch.{RESET}")
    print()
    return cmd_who()


def _picker_items() -> list[tuple[str, str | None]]:
    return [(p.stem, _identity_label(p)) for p in _profiles()]


def cmd_switch_interactive() -> int:
    items = _picker_items()
    if not items:
        log_yellow("⚠️  No saved Devin profiles.")
        return 1
    chosen = choose_profile("a Devin", items)
    return 1 if chosen is None else cmd_switch(chosen)


def cmd_remove(name: str) -> int:
    profile = _profile_file(name)
    if profile is None:
        return 1
    if not profile.is_file():
        log_red(f"❌ Profile not found: {name}")
        return 1
    try:
        profile_secrets.delete(profile, "devin")
    except OSError as exc:
        log_red(f"❌ Could not remove profile: {exc}")
        return 1
    # Drop the marker only when it names the removed profile; a marker for
    # another surviving profile stays (the live login just isn't saved yet).
    try:
        if _marker_file().read_text(encoding="utf-8").strip() == profile.stem:
            _marker_file().unlink(missing_ok=True)
    except OSError:
        pass
    ok("Removed Devin profile", profile.stem, bold=False)
    return 0


def cmd_remove_interactive() -> int:
    items = _picker_items()
    if not items:
        log_yellow("⚠️  No saved Devin profiles.")
        return 1
    return choose_and_run("a Devin", items, cmd_remove, cancel_message="Remove cancelled.")


def cmd_sync() -> int:
    creds = _read_creds()
    profile = _active_profile(creds)
    if creds is None or profile is None:
        log_yellow("⚠️  No unambiguous current profile — run: devin-accounts switch <name>")
        return 1
    if not _save_profile(profile, creds):
        return 1
    _set_marker(profile)
    success_panel(
        "Synced live login → profile",
        profile.stem,
        _claims_lines(creds, profile, email=_load_profile_metadata(profile).get("email")),
        title=f"Profile: {profile.stem}",
    )
    return 0


# ── refresh (verify only) ────────────────────────────────────────────────────

def _key_status(key: str | None) -> str:
    """"valid", "revoked", "unreachable" or "missing". Only an explicit 401/403
    proves a key dead — a network blip must not fire a re-login alert."""
    if not key:
        return "missing"
    error = devin_usage.fetch_usage(key).error
    if error is None:
        return "valid"
    if error.startswith(("HTTP 401", "HTTP 403")):
        return "revoked"
    # Only a parseable-but-reshaped quota proves the key was accepted; any other
    # failure ("invalid response" e.g. a captive portal's 200, "network error")
    # proves nothing.
    return "valid" if error == "unexpected quota shape" else "unreachable"


def _verify(label: str, key: str | None, relogin: str) -> bool:
    # The ❌ line's wording is what refresh_report and autoswitch_timer's
    # _REVOKED_MARKERS key on — reword it and the scheduled alert goes silent.
    status = _key_status(key)
    if status == "valid":
        ok("Token still valid", label, bold=False)
        return True
    if status == "revoked":
        log_red(f"❌ Token revoked for {label}: Devin rejected the API key")
        log_yellow(f"   Re-login with: {relogin}")
    elif status == "missing":
        log_yellow(f"⚠️  No API key readable for '{label}'.")
    else:
        log_yellow(f"⚠️  Could not reach Devin to verify '{label}' — try again later.")
    return False


def cmd_refresh(target: str | None) -> int:
    if target == "--all":
        profiles = _profiles()
        if not profiles:
            log_yellow("⚠️  No saved Devin profiles.")
            return 0
        results = [
            _verify(p.stem, _key(_load_profile(p)), f"devin-accounts login-switch {p.stem}")
            for p in profiles
        ]
        return 0 if all(results) else 1
    if target:
        profile = _profile_file(target)
        if profile is None:
            return 1
        if not profile.is_file():
            log_red(f"❌ Profile not found: {target}")
            return 1
        return 0 if _verify(profile.stem, _key(_load_profile(profile)), f"devin-accounts login-switch {profile.stem}") else 1
    creds = _read_creds()
    if creds is None:
        log_red("❌ No Devin login found. Run: devin auth login")
        return 1
    active = _active_profile(creds)
    relogin = f"devin-accounts login-switch {active.stem if active else '<name>'}"
    return 0 if _verify(active.stem if active else "the active Devin login", _key(creds), relogin) else 1


# ── login-switch ─────────────────────────────────────────────────────────────

def cmd_login_switch(name: str) -> int:
    executable = shutil.which("devin")
    if executable is None:
        log_red("❌ Devin CLI is required. Install it: https://cli.devin.ai")
        return 1
    if _profile_file(name) is None:  # fail before logout / the browser login
        return 1
    _backup_live()  # logout destroys an unsaved login; a cancelled login leaves none
    subprocess.run([executable, "auth", "logout"])
    result = subprocess.run([executable, "auth", "login"])
    return cmd_save(name) if result.returncode == 0 else result.returncode


# ── autoswitch ───────────────────────────────────────────────────────────────

def _autoswitch_probe(name: str) -> UsageWindow | None:
    """One saved profile's decisive window (daily = the short window, weekly =
    the default per `switch_window`), read with its OWN key — never the live
    credentials.toml, so probing a spare cannot change the active account."""
    path = _profile_file(name)
    payload = _load_profile(path) if path is not None else None
    usage = devin_usage.fetch_usage(_key(payload))
    return None if usage.error else autoswitch.pick_window(usage.daily, usage.weekly)


def _autoswitch_switch(name: str) -> bool:
    return cmd_switch(name) == 0


def _autoswitch_resume() -> bool:
    """Start a NEW devin session continuing the last conversation. Nothing
    running is touched; build_restart only grants this to an interactive run."""
    executable = shutil.which("devin")
    if executable is None:
        log_yellow("⚠️  devin is not on PATH — cannot resume the session automatically.")
        return False
    return subprocess.run([executable, "--continue"]).returncode == 0


def cmd_autoswitch() -> int:
    active = _active_profile()
    outcome = autoswitch.run_autoswitch(
        "devin",
        [path.stem for path in _profiles()],
        active.stem if active is not None else None,
        _autoswitch_probe,
        _autoswitch_switch,
        # SAFETY: no terminal, no restart — an unattended timer poll spawns nothing.
        autoswitch.build_restart("devin", _autoswitch_resume, interactive=sys.stdout.isatty()),
    )
    if outcome.reason == "disabled":
        log_yellow("⚠️  Auto-switch is disabled (see ~/.ai-accounts/config.json: enabled).")
    elif outcome.reason == "no_active":
        log_yellow("⚠️  No active Devin profile — nothing to auto-switch from.")
    elif outcome.reason == "unknown":
        log_yellow(f"⚠️  Could not determine usage for {outcome.from_profile}.")
    elif outcome.reason == "below_threshold":
        print(f"{outcome.from_profile} is at {outcome.used_pct}% used — below the switch threshold.")
    elif outcome.reason == "no_candidate":
        log_yellow(
            f"⚠️  {outcome.from_profile} is at {outcome.used_pct}% used and no other "
            "Devin account is available to switch to."
        )
    elif outcome.reason == "switch_failed":
        log_red(f"❌ {outcome.error}")
    else:  # "switched"
        ok("Auto-switched Devin account", f"{outcome.from_profile} → {outcome.to_profile}")
        if outcome.restarted is not True:
            log_yellow(
                autoswitch.manual_restart_message(
                    "devin", outcome.from_profile or "?", outcome.to_profile or "?"
                )
            )
    return 1 if outcome.reason == "switch_failed" else 0


@quiet_keyboard_interrupt
def main(argv: list[str] | None = None) -> int:
    argv = normalize_command_argv(list(sys.argv[1:] if argv is None else argv))
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(format_help(i18n.t("help.devin", default=HELP)))
        return 0
    command, *rest = argv
    if command == "config":
        return cm.cmd_config(rest, prog="devin-accounts")
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
        return cmd_refresh(rest[0] if rest else None)
    if command == "sync":
        return cmd_sync()
    if command == "autoswitch":
        return cmd_autoswitch()
    if command == "login-switch":
        if not rest:
            log_red("Usage: devin-accounts login-switch <profile_name>")
            return 1
        return cmd_login_switch(rest[0])
    log_unknown_command(command)
    print(format_help(i18n.t("help.devin", default=HELP)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
