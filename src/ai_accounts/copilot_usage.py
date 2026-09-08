"""GitHub Copilot quota lookup for copilot-accounts.

``fetch_usage`` only — every table/JSON formatter is shared in
``usage_format`` (same split as ``claude_usage``/``gemini_usage``).

Nothing here is verified against a live Copilot CLI login: the endpoint,
its auth header form and its JSON shape are all reported-but-unconfirmed,
so each is marked ``# ASSUMPTION:`` below and every parse step is written
to *return None rather than raise*. A wrong guess therefore degrades to
the grok/vibe "no quota API" path instead of breaking ``list``/``usage``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from .usage_format import UsageWindow, format_unix_time_compact

# ASSUMPTION: undocumented endpoint, reported to serve Copilot quota for the
# authenticated user. Unconfirmed — it may 404, move, or change shape at any
# time, which is exactly why every failure here is non-fatal.
USAGE_URL: Final = "https://api.github.com/copilot_internal/user"

# ASSUMPTION: a Copilot CLI OAuth token is accepted as a plain GitHub bearer
# token. `token <t>` is the older equivalent form; if `Bearer` is rejected the
# call returns HTTP 401 and degrades, it does not crash.
# ASSUMPTION: the Editor-Version / Editor-Plugin-Version pair is required by
# copilot_internal routes (it is by the token-exchange route); sending it to an
# endpoint that ignores it is harmless.
_HEADERS: Final = {
    "Accept": "application/json",
    "User-Agent": "ai-accounts",  # GitHub's API rejects requests with no UA
    "Editor-Version": "vscode/1.100.0",
    "Editor-Plugin-Version": "copilot/1.0.0",
}

# Copilot quotas reset monthly; the window length is only used for display
# rounding, so a 30-day nominal month is close enough.
_MONTH_MINUTES: Final = 30 * 24 * 60

# ASSUMPTION: `quota_snapshots` carries these three keys, each an object with
# `entitlement` / `remaining` / `percent_remaining` / `unlimited`, plus a
# `quota_reset_date` either per-snapshot or at the top level.
_PREMIUM_KEY: Final = "premium_interactions"
_CHAT_KEY: Final = "chat"
_COMPLETIONS_KEY: Final = "completions"


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    premium: UsageWindow | None
    chat: UsageWindow | None
    completions: UsageWindow | None
    plan: str | None
    refreshed_at: int | None
    error: str | None


_EMPTY: Final = UsageSnapshot(None, None, None, None, None, None)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _reset_epoch(value: Any) -> int | None:
    """Epoch seconds for a ``quota_reset_date`` (``"2026-10-01"``, or a full
    ISO-8601 stamp). Anything else reads as "unknown reset"."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _window(data: Any, reset_fallback: Any) -> UsageWindow | None:
    if not isinstance(data, dict):
        return None
    reset_time = _reset_epoch(data.get("quota_reset_date")) or _reset_epoch(reset_fallback)

    if data.get("unlimited") is True:
        used = 0.0
    else:
        remaining_percent = _number(data.get("percent_remaining"))
        if remaining_percent is not None:
            used = 100.0 - remaining_percent
        else:
            # Fall back to the raw counters when the percentage is absent.
            entitlement = _number(data.get("entitlement"))
            remaining = _number(data.get("remaining"))
            if entitlement is None or remaining is None or entitlement <= 0:
                return None
            used = 100.0 * (entitlement - remaining) / entitlement

    return UsageWindow(
        percentage=max(0, min(100, round(used))),
        reset_time=reset_time,
        window_minutes=_MONTH_MINUTES,
    )


def _request(token: str, *, timeout: float) -> dict[str, Any] | str:
    request = urllib.request.Request(
        USAGE_URL,
        headers={**_HEADERS, "Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404 = the undocumented endpoint is gone/renamed; the caller treats
        # any error the same way (no quota API) but the code is kept for the
        # table's "ERR 404" cell.
        return f"HTTP {exc.code} from quota endpoint"
    except (urllib.error.URLError, OSError):
        return "network error"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid response"
    if isinstance(raw, dict):
        return raw
    return "quota endpoint returned non-object JSON"


def fetch_usage(token: str | None, *, timeout: float = 20) -> UsageSnapshot:
    """Premium/chat/completion quota windows for one Copilot OAuth token.

    Never raises: an unreachable endpoint, a rejected token or an unexpected
    JSON shape all come back as a snapshot with ``error`` set and every window
    ``None``, which callers render as "no quota API".
    """
    if not token:
        return UsageSnapshot(None, None, None, None, None, "missing token")
    result = _request(token, timeout=timeout)
    if isinstance(result, str):
        return UsageSnapshot(None, None, None, None, None, result)

    snapshots = result.get("quota_snapshots")
    if not isinstance(snapshots, dict):
        return UsageSnapshot(None, None, None, None, None, "unexpected quota shape")

    reset_fallback = result.get("quota_reset_date")
    plan = result.get("copilot_plan")
    return UsageSnapshot(
        premium=_window(snapshots.get(_PREMIUM_KEY), reset_fallback),
        chat=_window(snapshots.get(_CHAT_KEY), reset_fallback),
        completions=_window(snapshots.get(_COMPLETIONS_KEY), reset_fallback),
        plan=plan if isinstance(plan, str) and plan else None,
        refreshed_at=int(time.time()),
        error=None,
    )


def empty_usage() -> UsageSnapshot:
    """The all-``None`` snapshot used when a profile has no token to query."""
    return _EMPTY


def _format_error(error: str) -> str:
    if error.startswith("HTTP "):
        return "ERR " + error.split()[1]
    if error == "network error":
        return "ERR network"
    if error == "missing token":
        return "ERR auth"
    return "ERR usage"


def format_refreshed_at(snapshot: UsageSnapshot) -> str:
    if snapshot.error:
        return _format_error(snapshot.error)
    return format_unix_time_compact(snapshot.refreshed_at)
