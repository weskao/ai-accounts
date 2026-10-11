"""Devin CLI daily/weekly quota lookup.

``fetch_usage`` only — formatting lives in ``usage_format`` (same split as
``grok_usage``/``claude_usage``).

The endpoint is UNDOCUMENTED and UNVERIFIED: it is the Connect RPC the Devin CLI
calls on startup (``GetUserStatus`` on the Windsurf/Codeium seat-management
service). Neither the request identity nor the response field names have been
confirmed against a live account, so every parse step returns ``None`` rather
than raising — if the route moves or changes shape, callers degrade to a
snapshot with ``error`` set instead of breaking.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

from ._utils import clamp_percent, finite_number, no_redirect_opener, parse_iso_epoch
from .usage_format import UsageWindow

USAGE_URL: Final = (
    "https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus"
)

# ASSUMPTION: the backend reportedly gates on this identity tuple; the exact
# values (and whether any version is accepted) are unverified.
_IDE_NAME: Final = "devin-cli"
_IDE_VERSION: Final = "1.0.0"

_HEADERS: Final = {
    "Content-Type": "application/json",
    "Connect-Protocol-Version": "1",
    "Accept": "application/json",
    "User-Agent": "ai-accounts",
}

# credentials.toml keys carry this prefix and the server wants it kept (a stripped
# key gets HTTP 401); a bare prefix with nothing after it is no key at all.
_KEY_PREFIX: Final = "devin-session-token$"

# Live check 2026-10-11: planStatus carries {daily,weekly}QuotaRemainingPercent and
# {daily,weekly}QuotaResetAtUnix (digit string). The *used* names are unconfirmed guesses
# (camelCase proto3 JSON), kept as a fallback.
_REMAINING_KEYS: Final = ("{p}QuotaRemainingPercent",)
_NESTED_REMAINING: Final = ("remainingPercent",)
_PERCENT_KEYS: Final = ("{p}QuotaUsedPercent", "{p}UsagePercent", "{p}QuotaUsagePercent", "{p}UsedPercent")
_RESET_KEYS: Final = ("{p}QuotaResetAtUnix", "{p}QuotaResetTime", "{p}ResetAt", "{p}ResetTime")
# Nested form: planStatus.<window>.{usedPercent,...}
_NESTED_PERCENT: Final = ("usedPercent", "usagePercent", "percent")
_NESTED_RESET: Final = ("resetAt", "resetTime", "resetsAt", "resetAtUnix")
_MAX_EPOCH: Final = 32503680000  # year 3000
_WINDOWS: Final = (("daily", 24 * 60), ("weekly", 7 * 24 * 60))


_OPENER = no_redirect_opener()


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    daily: UsageWindow | None
    weekly: UsageWindow | None
    plan: str | None
    email: str | None
    refreshed_at: int | None
    error: str | None


_EMPTY: Final = UsageSnapshot(None, None, None, None, None, None)


def _failed(error: str) -> UsageSnapshot:
    return UsageSnapshot(None, None, None, None, None, error)


def _reset(value: Any) -> int | None:
    """Epoch seconds from an epoch number/digit string or an ISO-8601 string."""
    if isinstance(value, str) and value.strip().isdigit():
        try:
            value = int(value)
        except ValueError:  # "\u00b2" is a digit to str but not to int; >4300 digits trips the int limit
            return None
    number = finite_number(value)
    if number is not None:
        # shortcut: >1e11 is assumed to be epoch milliseconds, not far-future seconds.
        epoch = int(number / 1000 if number > 1e11 else number)
        return epoch if 0 <= epoch <= _MAX_EPOCH else None  # keeps the formatter's fromtimestamp in range
    return parse_iso_epoch(value)


def _first(sources: list[dict[str, Any]], keys: tuple[str, ...], prefix: str, parse):  # noqa: ANN001
    """First parseable value for ``keys`` (flat ``{p}`` templates, then nested) in ``sources``."""
    for source in sources:
        nested = source.get(prefix)
        candidates = [source.get(k.format(p=prefix)) for k in keys if "{p}" in k]
        if isinstance(nested, dict):
            candidates += [nested.get(k) for k in keys if "{p}" not in k]
        for candidate in candidates:
            if (value := parse(candidate)) is not None:
                return value
    return None


def _window(sources: list[dict[str, Any]], prefix: str, minutes: int) -> UsageWindow | None:
    keys = _PERCENT_KEYS + _NESTED_PERCENT
    left_keys = _REMAINING_KEYS + _NESTED_REMAINING
    percent = _first(sources, keys, prefix, clamp_percent)
    if percent is None and (left := _first(sources, left_keys, prefix, clamp_percent)) is not None:
        percent = 100 - left
    reset = _first(sources, _RESET_KEYS + _NESTED_RESET, prefix, _reset)
    if percent is None:
        # proto3 JSON omits zero values and the live field is *remaining*: a reset with
        # no percent key at all is an exhausted window (0% left). A present-but-
        # unparseable percent is a malformed window, not exhausted.
        if reset is None or any(_first(sources, k, prefix, lambda v: v) is not None for k in (keys, left_keys)):
            return None
        percent = 100
    return UsageWindow(percentage=percent, reset_time=reset, window_minutes=minutes)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse(raw: Any) -> UsageSnapshot:
    status = raw.get("userStatus") if isinstance(raw, dict) else None
    if not isinstance(status, dict):
        return _failed("unexpected quota shape")
    plan_status = status.get("planStatus")
    plan_status = plan_status if isinstance(plan_status, dict) else {}
    sources = [plan_status, status]
    windows = {p: _window(sources, p, minutes) for p, minutes in _WINDOWS}
    if not any(windows.values()):
        return _failed("unexpected quota shape")
    info = plan_status.get("planInfo")
    plan = _text(info.get("planName")) if isinstance(info, dict) else None
    return UsageSnapshot(
        daily=windows["daily"],
        weekly=windows["weekly"],
        plan=plan or _text(plan_status.get("planName")),
        email=_text(status.get("email")),
        refreshed_at=int(time.time()),
        error=None,
    )


def fetch_usage(token: str | None, *, timeout: float = 20) -> UsageSnapshot:
    """Daily and weekly Devin quota for one ``windsurf_api_key``.

    Never raises: an unreachable endpoint, a rejected key, or an unexpected JSON
    shape come back as a snapshot with ``error`` set and every window ``None``.
    """
    key = (token or "").strip()
    if not key.removeprefix(_KEY_PREFIX):
        return _failed("missing access token")
    body = {
        "metadata": {
            "apiKey": key,
            "ideName": _IDE_NAME,
            "ideVersion": _IDE_VERSION,
            "extensionName": _IDE_NAME,
            "extensionVersion": _IDE_VERSION,
            "locale": "en",
        }
    }
    request = urllib.request.Request(
        USAGE_URL, data=json.dumps(body).encode("utf-8"), headers=_HEADERS, method="POST"
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return _failed(f"HTTP {exc.code} from usage endpoint")
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return _failed("network error")
    except (ValueError, RecursionError):  # JSONDecodeError/UnicodeDecodeError, and the 4300-digit int limit
        return _failed("invalid response")
    try:
        return _parse(raw)
    except (ValueError, OverflowError, TypeError, RecursionError):
        return _failed("unexpected quota shape")


def empty_usage() -> UsageSnapshot:
    """The all-``None`` snapshot used when a profile has no key to query."""
    return _EMPTY
