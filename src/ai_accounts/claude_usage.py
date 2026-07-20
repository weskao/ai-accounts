"""Claude Code OAuth usage lookup for claude-accounts.

Reads the subscription usage windows (5-hour session + 7-day) from the OAuth
usage endpoint using a profile's ``accessToken``. Never prints tokens — only
the returned utilization percentages and reset times. Shares the table/format
helpers with ``usage_format`` so every account tool renders usage identically.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Final, TypeAlias

from .usage_format import UsageWindow, format_unix_time_compact

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonDict: TypeAlias = dict[str, JsonValue]

USAGE_URL: Final = "https://api.anthropic.com/api/oauth/usage"
# The OAuth usage endpoint rejects the request without this beta opt-in header.
_BETA_HEADER: Final = "oauth-2025-04-20"
_USER_AGENT: Final = os.environ.get("CLAUDE_USAGE_USER_AGENT", "claude-code/2.1.0")

_FIVE_HOUR_MINUTES: Final = 5 * 60
_SEVEN_DAY_MINUTES: Final = 7 * 24 * 60


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    five_hour: UsageWindow | None
    seven_day: UsageWindow | None
    plan: str | None
    refreshed_at: int | None
    error: str | None


def _parse_iso8601(value: object) -> int | None:
    """Epoch seconds for an ISO-8601 reset stamp (e.g. ``2025-07-20T10:30:00.000Z``)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _window(data: JsonValue, window_minutes: int) -> UsageWindow | None:
    if not isinstance(data, dict):
        return None
    used = data.get("utilization")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    percentage = max(0, min(100, round(used)))
    return UsageWindow(
        percentage=percentage,
        reset_time=_parse_iso8601(data.get("resets_at")),
        window_minutes=window_minutes,
    )


def _request_usage(access_token: str, *, timeout: float) -> JsonDict | str:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "anthropic-beta": _BETA_HEADER,
            "User-Agent": _USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} from usage endpoint"
    except (urllib.error.URLError, OSError):
        return "network error"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid response"
    if isinstance(raw, dict):
        return raw
    return "usage endpoint returned non-object JSON"


def fetch_usage(
    access_token: str | None, *, plan: str | None = None, timeout: float = 20
) -> UsageSnapshot:
    """Fetch the 5-hour and 7-day usage windows for one OAuth access token."""
    if not access_token:
        return UsageSnapshot(None, None, plan, None, "missing access token")
    result = _request_usage(access_token, timeout=timeout)
    if isinstance(result, str):
        return UsageSnapshot(None, None, plan, None, result)
    return UsageSnapshot(
        five_hour=_window(result.get("five_hour"), _FIVE_HOUR_MINUTES),
        seven_day=_window(result.get("seven_day"), _SEVEN_DAY_MINUTES),
        plan=plan,
        refreshed_at=int(time.time()),
        error=None,
    )


def _format_error(error: str) -> str:
    if error.startswith("HTTP "):
        return "ERR " + error.split()[1]
    if error == "network error":
        return "ERR network"
    if error == "missing access token":
        return "ERR auth"
    return "ERR usage"


def format_refreshed_at(snapshot: UsageSnapshot) -> str:
    if snapshot.error:
        return _format_error(snapshot.error)
    return format_unix_time_compact(snapshot.refreshed_at)
