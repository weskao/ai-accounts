"""``ai-accounts doctor`` — one offline health check per provider.

Umbrella-only subcommand (no per-provider equivalent — see ``ai_accounts.py``'s
dispatch), so every check here runs in-process rather than shelling out to
each provider's own CLI the way ``list``/``usage --json`` do.

Per :data:`ai_accounts.providers.PROVIDERS` entry, checks:

  (a) the CLI binary (``provider.binary``) is on ``PATH``
  (b) the OS credential store is reachable — reuses
      :func:`ai_accounts._utils.go_keyring_available` (already handles macOS/
      Windows keychain vs Linux ``secret-tool``) rather than re-deriving the
      per-OS branching; only ``agy`` hard-requires it (see README's "Platform
      notes" — codex/claude/vibe fall back to a plaintext file, grok never
      touches a credential store at all)
  (c) every saved profile under that provider's account dir is well-formed
      JSON and, if it carries a recognizable expiry field, not already
      expired — a lightweight, fully offline heuristic scan, never a live
      quota/HTTP call
  (d) the auto-switch timer's install state, via
      :func:`ai_accounts.autoswitch_timer.status`

Never raises: a missing binary, unreachable credential store, corrupt
profile, or a timer-status probe that itself blows up (e.g. ``crontab``
missing on a barebones Linux box) becomes a FAIL/unknown row, not a
traceback — a fresh/empty account setup is the normal case this command
exists to report on.
"""

from __future__ import annotations

import importlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import _present
from ._utils import GREEN, RED, RESET, go_keyring_available, have
from .providers import PROVIDERS, Provider

# ponytail: only `agy` hard-requires the OS credential store (its live session
# lives there with no file fallback — see gemini_accounts.py's "agy-accounts
# needs access to the OS credential store" guard). codex/claude/vibe treat it
# as optional (file-based fallback exists); grok never touches it. Upgrade
# path: move this onto Provider itself if a future provider's requirement
# stops being a two-way split.
_STORE_REQUIRED = frozenset({"agy"})

# Key names that show up, across the five providers' saved profile JSON, as an
# ACCESS token's absolute expiry moment. Not exhaustive by design (this is an
# offline, best-effort scan, not a per-provider JWT/claims decoder) — a
# profile with none of these is reported as "no expiry field to check", not
# as expired.
_EXPIRY_KEYS = frozenset(
    {"expires_epoch", "expires_at", "expiry_date", "expiresAt", "expiry", "exp"}
)
# A past REFRESH-token/grant expiry is unconditionally a dead profile (no
# refresh_token can rescue it once the grant itself is gone). Separate from
# _EXPIRY_KEYS because a past *access*-token expiry alone is the normal,
# expected state of a saved snapshot (it's renewed on demand) — see
# _REFRESH_TOKEN_KEYS below for why that alone must never flag "expired".
_REFRESH_EXPIRY_KEYS = frozenset({"refreshTokenExpiresAt", "refresh_expires_epoch"})
# Presence of a non-empty refresh token means an expired *access* token is not
# a problem — every provider's own refresh flow renews it silently on next
# use (confirmed against real saved profiles: grok/claude/agy all carry an
# access-token expiry that is routinely in the past between uses, precisely
# because refresh is on-demand, not eager). Only flag "expired" when the
# access token is stale AND nothing here can refresh it.
_REFRESH_TOKEN_KEYS = frozenset({"refresh_token", "refreshToken"})
_MAX_SCAN_DEPTH = 3


@dataclass
class _Check:
    ok: bool
    detail: str

    def to_json(self) -> dict[str, object]:
        return {"ok": self.ok, "detail": self.detail}


def _mark(check: _Check) -> str:
    color = GREEN if check.ok else RED
    label = "PASS" if check.ok else "FAIL"
    return f"{color}{label}{RESET} {check.detail}"


def _check_binary(provider: Provider) -> _Check:
    if have(provider.binary):
        return _Check(True, f"`{provider.binary}` on PATH")
    return _Check(False, f"`{provider.binary}` not found on PATH")


def _check_credential_store(provider: Provider) -> _Check:
    try:
        reachable, reason = go_keyring_available()
    except Exception as exc:  # pragma: no cover - defensive, go_keyring_available doesn't shell out
        return _Check(False, f"probe failed: {exc}")
    if reachable:
        return _Check(True, "reachable")
    if provider.key in _STORE_REQUIRED:
        return _Check(False, reason or "unreachable")
    return _Check(True, f"unreachable but not required ({reason or 'file-based fallback in use'})")


def _account_dir_for(provider: Provider) -> Path | None:
    """The same directory the provider's own CLI reads/writes profiles in.

    Imported dynamically and read off the module's private ``_account_dir()``
    rather than re-deriving the env-var/default/legacy mapping here — that
    mapping already lives once per provider module and this is the only way
    to stay in sync with it without a second copy. ``None`` (not raised) if
    resolution itself fails (e.g. a legacy-dir migration hits a permission
    error) — doctor reports that as "no profiles to check", not a crash.
    """
    try:
        module = importlib.import_module(provider.module)
        return module._account_dir()  # noqa: SLF001 - see docstring
    except Exception:
        return None


def _normalize_epoch(value: object) -> float | None:
    """*value* as a Unix epoch (seconds), or ``None`` if unrecognizable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value > 10**12:  # milliseconds
            return value / 1000
        if value > 10**9:  # already seconds
            return float(value)
        return None  # too small to be an absolute timestamp (e.g. a duration)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


@dataclass
class _ScanResult:
    access_expiries: list[float]
    refresh_expiries: list[float]
    has_refresh_token: bool


def _scan_expiries(node: object, depth: int = 0) -> _ScanResult:
    """Recognizable expiry timestamps (+ refresh-token presence) in *node*."""
    result = _ScanResult([], [], False)
    if depth > _MAX_SCAN_DEPTH:
        return result
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _EXPIRY_KEYS:
                epoch = _normalize_epoch(value)
                if epoch is not None:
                    result.access_expiries.append(epoch)
            elif key in _REFRESH_EXPIRY_KEYS:
                epoch = _normalize_epoch(value)
                if epoch is not None:
                    result.refresh_expiries.append(epoch)
            elif key in _REFRESH_TOKEN_KEYS and isinstance(value, str) and value:
                result.has_refresh_token = True
            if isinstance(value, (dict, list)):
                nested = _scan_expiries(value, depth + 1)
                result.access_expiries.extend(nested.access_expiries)
                result.refresh_expiries.extend(nested.refresh_expiries)
                result.has_refresh_token = result.has_refresh_token or nested.has_refresh_token
    elif isinstance(node, list):
        for item in node:
            nested = _scan_expiries(item, depth + 1)
            result.access_expiries.extend(nested.access_expiries)
            result.refresh_expiries.extend(nested.refresh_expiries)
            result.has_refresh_token = result.has_refresh_token or nested.has_refresh_token
    return result


def _check_profile_file(path: Path) -> tuple[str, str | None]:
    """One of ``"ok"``, ``"malformed"``, ``"expired"`` for *path*, plus detail.

    "expired" fires on a past REFRESH-token/grant expiry (unconditionally —
    nothing rescues that), or on a past ACCESS-token expiry with no
    refresh_token present to renew it. A past access-token expiry with a
    refresh_token present is "ok" — that's the normal, expected state of a
    saved snapshot between uses (see the module docstring's ``_REFRESH_TOKEN_KEYS``
    note).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "malformed", f"unreadable: {exc}"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return "malformed", f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return "malformed", "not a JSON object"
    now = time.time()
    scan = _scan_expiries(data)
    if scan.refresh_expiries and max(scan.refresh_expiries) <= now:
        return "expired", None
    if scan.access_expiries and max(scan.access_expiries) <= now and not scan.has_refresh_token:
        return "expired", None
    return "ok", None


def _check_profiles(provider: Provider) -> _Check:
    account_dir = _account_dir_for(provider)
    if account_dir is None or not account_dir.is_dir():
        return _Check(True, "no saved profiles")
    try:
        profile_files = sorted(account_dir.glob("*.json"))
    except OSError as exc:
        return _Check(False, f"cannot list profiles: {exc}")
    if not profile_files:
        return _Check(True, "no saved profiles")

    malformed: list[str] = []
    expired: list[str] = []
    for profile_file in profile_files:
        status, detail = _check_profile_file(profile_file)
        if status == "malformed":
            # Surface *why* it's malformed (invalid JSON / not an object /
            # unreadable) — the filename alone isn't the diagnostic a user
            # asking "what's wrong with this profile" actually wants.
            malformed.append(f"{profile_file.stem} ({detail})")
        elif status == "expired":
            expired.append(profile_file.stem)

    ok = not malformed and not expired
    if ok:
        return _Check(True, f"{len(profile_files)} profile(s) checked")
    parts = []
    if malformed:
        parts.append(f"malformed: {', '.join(malformed)}")
    if expired:
        parts.append(f"expired: {', '.join(expired)}")
    return _Check(False, f"{len(profile_files)} profile(s) checked, " + "; ".join(parts))


def _check_timer() -> _Check:
    try:
        from . import autoswitch_timer

        state = autoswitch_timer.status()
    except Exception as exc:
        return _Check(False, f"status probe failed: {exc}")
    return _Check(True, state)


def _run_checks() -> tuple[list[dict[str, object]], _Check]:
    rows: list[dict[str, object]] = []
    for provider in PROVIDERS:
        binary = _check_binary(provider)
        store = _check_credential_store(provider)
        profiles = _check_profiles(provider)
        rows.append(
            {
                "provider": provider,
                "binary": binary,
                "credential_store": store,
                "profiles": profiles,
                "ok": binary.ok and store.ok and profiles.ok,
            }
        )
    return rows, _check_timer()


def _print_table(rows: list[dict[str, object]], timer: _Check) -> None:
    table_rows = [
        {
            "provider": row["provider"].label,
            "binary": _mark(row["binary"]),
            "credential_store": _mark(row["credential_store"]),
            "profiles": _mark(row["profiles"]),
            "status": _mark(_Check(row["ok"], "OK" if row["ok"] else "issues found")),
        }
        for row in rows
    ]
    columns = [
        ("Provider", "provider"),
        ("Binary", "binary"),
        ("Credential store", "credential_store"),
        ("Profiles", "profiles"),
        # "STATE" (not "Status") matches every other `accounts_table` caller
        # (codex/claude/agy/grok/vibe/copilot _accounts.py) — it's the header
        # name `_present._accounts_cards` looks for to trail the status on the
        # identity line in narrow mode instead of stacking it as its own field.
        ("STATE", "status"),
    ]
    _present.accounts_table(table_rows, columns)
    print(f"\nAutoswitch timer: {_mark(timer)}")


def _print_json(rows: list[dict[str, object]], timer: _Check) -> None:
    merged: dict[str, object] = {
        "providers": {
            row["provider"].label: {
                "binary": row["binary"].to_json(),
                "credential_store": row["credential_store"].to_json(),
                "profiles": row["profiles"].to_json(),
                "ok": row["ok"],
            }
            for row in rows
        },
        "autoswitch_timer": timer.to_json(),
    }
    merged["ok"] = timer.ok and all(row["ok"] for row in rows)
    print(json.dumps(merged))


def run_doctor(json_output: bool) -> int:
    """Run every provider's health check and print the result.

    Returns 0 always — doctor reports problems, it doesn't fail the CLI
    invocation itself (mirrors ``timer-status``, which is informational too).
    """
    rows, timer = _run_checks()
    if json_output:
        _print_json(rows, timer)
    else:
        _print_table(rows, timer)
    return 0
