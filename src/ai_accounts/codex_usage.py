from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, TypeAlias

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonDict: TypeAlias = dict[str, JsonValue]

USAGE_URL: Final = "https://chatgpt.com/backend-api/wham/usage"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class UsageWindow:
    percentage: int
    reset_time: int | None
    window_minutes: int | None


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    hourly: UsageWindow | None
    weekly: UsageWindow | None
    refreshed_at: int | None
    error: str | None


def _load_json(path: Path) -> JsonDict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(raw, dict):
        return raw
    return None


def _dict_field(data: JsonDict, key: str) -> JsonDict | None:
    value = data.get(key)
    if isinstance(value, dict):
        return value
    return None


def _str_field(data: JsonDict, key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _find_str(value: JsonValue, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            found = _str_field(value, key)
            if found is not None:
                return found
        for child in value.values():
            found = _find_str(child, keys)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_str(child, keys)
            if found is not None:
                return found
    return None


def _number_field(data: JsonDict, key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _decode_jwt_payload(token: str | None) -> JsonDict | None:
    if not token or "." not in token:
        return None
    try:
        payload = token.split(".")[1]
        padded = payload.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        raw = json.loads(base64.b64decode(padded).decode("utf-8"))
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(raw, dict):
        return raw
    return None


def _access_token(auth: JsonDict) -> str | None:
    tokens = _dict_field(auth, "tokens") or {}
    return _str_field(tokens, "access_token") or _str_field(auth, "access_token")


def _account_id(auth: JsonDict, access_token: str) -> str | None:
    tokens = _dict_field(auth, "tokens") or {}
    claims = _decode_jwt_payload(access_token) or {}
    return (
        _str_field(tokens, "account_id")
        or _str_field(auth, "account_id")
        or _find_str(claims, ("chatgpt_account_id", "account_id"))
    )


def _request_usage(access_token: str, account_id: str | None) -> JsonDict | str:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    request = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=20) as response:
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


def _clamp_percent(value: int) -> int:
    return max(0, min(100, value))


def format_unix_time_compact(value: int | None) -> str:
    if value is None:
        return "-"
    dt = datetime.fromtimestamp(value)
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%b %d %H:%M")
    return dt.strftime("%Y-%m-%d")


def format_reset_remaining(value: int | None, *, include_days: bool) -> str:
    """Time remaining until `value` (unix timestamp) resets, e.g. "5h 3m" or
    "1d 3h 5m". Every unit down to minutes is always shown, even when zero."""
    remaining = max(0, int(value - time.time())) if value is not None else 0
    minutes_total = remaining // 60
    if include_days:
        days, rest = divmod(minutes_total, 1_440)
        hours, minutes = divmod(rest, 60)
        return f"{days}d {hours}h {minutes}m"
    hours, minutes = divmod(minutes_total, 60)
    return f"{hours}h {minutes}m"


def format_usage_window(window: UsageWindow | None, window_kind: str, percentage_text: str | None = None) -> str:
    if window is None:
        return "-"
    percent = percentage_text or f"{window.percentage}%"
    remaining = format_reset_remaining(window.reset_time, include_days=window_kind != "5h")
    return f"{percent} · {remaining}"


def _format_error(error: str) -> str:
    if error.startswith("HTTP "):
        return "ERR " + error.split()[1]
    if error == "network error":
        return "ERR network"
    if error in ("missing access token", "unreadable auth"):
        return "ERR auth"
    return "ERR usage"


def format_refreshed_at(snapshot: UsageSnapshot) -> str:
    if snapshot.error:
        return _format_error(snapshot.error)
    return format_unix_time_compact(snapshot.refreshed_at)


def _window(data: JsonDict | None) -> UsageWindow | None:
    if data is None:
        return None

    used_percent = _number_field(data, "used_percent")
    if used_percent is None:
        return None

    used = _clamp_percent(used_percent)
    seconds = _number_field(data, "limit_window_seconds")
    reset_at = _number_field(data, "reset_at")
    reset_after = _number_field(data, "reset_after_seconds")
    reset_time = reset_at
    if reset_time is None and reset_after is not None and reset_after >= 0:
        reset_time = int(time.time()) + reset_after

    window_minutes = None
    if seconds is not None and seconds > 0:
        window_minutes = (seconds + 59) // 60

    return UsageWindow(
        percentage=used,
        reset_time=reset_time,
        window_minutes=window_minutes,
    )


_HOURLY_MAX_MINUTES: Final = 24 * 60


def _classify_window(window: UsageWindow | None, *, positional_slot: str) -> str | None:
    """Return "hourly" or "weekly" for `window` based on its parsed duration.
    The usage API does not guarantee primary_window is the hourly one, so
    duration classifies it; only when the duration is unknown do we fall back
    to the API's positional assumption (primary=hourly, secondary=weekly)."""
    if window is None:
        return None
    if window.window_minutes is None:
        return positional_slot
    return "hourly" if window.window_minutes <= _HOURLY_MAX_MINUTES else "weekly"


def _snapshot(payload: JsonDict) -> UsageSnapshot:
    rate_limit = _dict_field(payload, "rate_limit")
    primary = _window(_dict_field(rate_limit, "primary_window") if rate_limit else None)
    secondary = _window(_dict_field(rate_limit, "secondary_window") if rate_limit else None)

    hourly: UsageWindow | None = None
    weekly: UsageWindow | None = None
    for window, positional_slot in ((primary, "hourly"), (secondary, "weekly")):
        slot = _classify_window(window, positional_slot=positional_slot)
        if slot == "hourly":
            hourly = window
        elif slot == "weekly":
            weekly = window

    return UsageSnapshot(
        hourly=hourly,
        weekly=weekly,
        refreshed_at=int(time.time()),
        error=None,
    )


def fetch_usage(auth_path: Path) -> UsageSnapshot:
    auth = _load_json(auth_path)
    if auth is None:
        return UsageSnapshot(hourly=None, weekly=None, refreshed_at=None, error="unreadable auth")

    access_token = _access_token(auth)
    if access_token is None:
        return UsageSnapshot(hourly=None, weekly=None, refreshed_at=None, error="missing access token")

    result = _request_usage(access_token, _account_id(auth, access_token))
    if isinstance(result, str):
        return UsageSnapshot(hourly=None, weekly=None, refreshed_at=None, error=result)
    return _snapshot(result)
