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
from datetime import date, datetime
from typing import Final, TypeAlias

from ._utils import clamp_percent, finite_number
from .usage_format import ResetVouchers, UsageWindow, format_elapsed, format_unix_time_compact
from .usage_format import parse_iso8601 as _parse_iso8601

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonDict: TypeAlias = dict[str, JsonValue]

_PLAIN_USAGE_URL: Final = "https://api.anthropic.com/api/oauth/usage"
# `?cedar_ember=1` is what makes the response carry the reset-voucher block (else it is null).
USAGE_URL: Final = f"{_PLAIN_USAGE_URL}?cedar_ember=1"
# If the server ever rejects the `cedar_ember` param, fall back to the plain URL so the
# 5h/7d windows (and autoswitch) never depend on voucher support.
_PARAM_REJECTED: Final = ("HTTP 400", "HTTP 404")
# The OAuth usage endpoint rejects the request without this beta opt-in header.
_BETA_HEADER: Final = "oauth-2025-04-20"
# The server reports the vouchers as ineligible ("surface") unless the User-Agent looks like
# the CLI. A pinned version (not `claude --version`, which would cost a subprocess per call);
# bump it if `ineligible_reason` ever turns "cli_version". CLAUDE_USAGE_USER_AGENT overrides.
_CLI_VERSION: Final = "2.1.296"
_USER_AGENT: Final = os.environ.get(
    "CLAUDE_USAGE_USER_AGENT", f"claude-cli/{_CLI_VERSION} (external, cli)"
)

_FIVE_HOUR_MINUTES: Final = 5 * 60
_SEVEN_DAY_MINUTES: Final = 7 * 24 * 60


@dataclass(frozen=True, slots=True)
class ExtraUsage:
    """Paid extra usage ("credits") this month, from the ``extra_usage`` block. Amounts
    are in minor units (cents) as the server sends them; ``decimals`` says how many."""

    used_minor: int
    limit_minor: int | None
    decimals: int
    currency: str
    percent: int | None


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    five_hour: UsageWindow | None
    seven_day: UsageWindow | None
    plan: str | None
    refreshed_at: int | None
    error: str | None
    reset_vouchers: ResetVouchers = ResetVouchers()
    credits: ExtraUsage | None = None


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


# `ineligible_reason` values that describe the ACCOUNT (so "0 vouchers" is true). Any other
# reason — "surface" / "cli_version" (our client looks wrong) or one we have never seen — says
# nothing about the account, so it stays unknown rather than a misleading 0.
_ACCOUNT_LEVEL_REASONS: Final = frozenset(
    {"tier", "tenure", "no_weekly_limit", "weekly_limit", "not_at_wall", "extra_usage",
     "other_experiment", "mobile"}
)


def _vouchers(cedar_ember: JsonValue, now: float | None = None) -> ResetVouchers:
    """Reset vouchers from the ``cedar_ember`` block: the SUM of ``resets_left`` over all
    still-usable grants, and one ``ends_at`` per voucher (a grant's vouchers share it). ``eligible: false`` with an
    account-level reason means none (count 0); with a client-level or unknown reason it is
    unknown. A missing/odd block is unknown (``count=None``) so a schema change can never break
    the usage windows parsed alongside it. Grants with ``usable_now: false`` or already ended
    are skipped."""
    if not isinstance(cedar_ember, dict):
        return ResetVouchers()
    if cedar_ember.get("eligible") is False:
        known = cedar_ember.get("ineligible_reason") in _ACCOUNT_LEVEL_REASONS
        return ResetVouchers(count=0 if known else None)
    grants = cedar_ember.get("grants")
    if not isinstance(grants, list):
        return ResetVouchers()
    now = time.time() if now is None else now
    count = 0
    expiries: list[int] = []
    for grant in grants:
        if not isinstance(grant, dict) or grant.get("usable_now") is False:
            continue
        left = grant.get("resets_left")
        if isinstance(left, bool) or not isinstance(left, int) or left <= 0:
            continue
        ends_at = _parse_iso8601(grant.get("ends_at"))
        if ends_at is not None and ends_at <= now:
            continue
        count += left
        if ends_at is not None:
            expiries.extend([ends_at] * left)
    return ResetVouchers(count=count, expires_at=tuple(sorted(expiries)))


def _credits(block: JsonValue) -> ExtraUsage | None:
    """:class:`ExtraUsage` from the ``extra_usage`` block; ``None`` when it is off,
    missing or odd, so a schema change never breaks the windows parsed alongside it."""
    if not isinstance(block, dict) or block.get("is_enabled") is not True:
        return None
    used = finite_number(block.get("used_credits"))
    decimals = block.get("decimal_places", 2)
    if used is None or isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 4:
        return None
    limit = finite_number(block.get("monthly_limit"))
    currency = block.get("currency")
    return ExtraUsage(
        used_minor=round(used),
        limit_minor=None if limit is None else round(limit),
        decimals=decimals,
        currency=currency if isinstance(currency, str) and currency else "USD",
        percent=clamp_percent(block.get("utilization")),
    )


def _credits_reset_on(today: date | None) -> date:
    # shortcut: the reply has no reset date; assumes calendar-month billing (Claude Code
    # shows "resets Nov 1"). Read a server date instead if one ever appears.
    today = today or date.today()
    return date(today.year + today.month // 12, today.month % 12 + 1, 1)


def _money(minor: int, credits: ExtraUsage) -> str:
    amount = f"{minor / 10 ** credits.decimals:.{credits.decimals}f}"
    return f"${amount}" if credits.currency == "USD" else f"{credits.currency} {amount}"


def format_credits(credits: ExtraUsage | None, *, now: float | None = None) -> str:
    """Two-line table cell ``66% · $66.26 / $100.00`` + ``resets 20d 7h`` (time left,
    like VOUCHER EXPIRY); ``—`` when unknown."""
    if credits is None:
        return "—"
    now = time.time() if now is None else now
    spent = _money(credits.used_minor, credits)
    if credits.limit_minor is not None:
        spent += f" / {_money(credits.limit_minor, credits)}"
    reset_at = datetime.combine(_credits_reset_on(date.fromtimestamp(now)), datetime.min.time()).timestamp()
    parts = [f"{credits.percent}%"] if credits.percent is not None else []
    return " · ".join([*parts, spent]) + f"\nresets {format_elapsed(int(reset_at - now))}"


def credits_json(credits: ExtraUsage | None, *, today: date | None = None) -> dict[str, object] | None:
    """``--json`` form of :class:`ExtraUsage` in major units (``None`` in, ``None`` out)."""
    if credits is None:
        return None
    scale = 10 ** credits.decimals
    return {
        "used": credits.used_minor / scale,
        "limit": None if credits.limit_minor is None else credits.limit_minor / scale,
        "currency": credits.currency,
        "percent": credits.percent,
        "resets_on": _credits_reset_on(today).isoformat(),
    }


def _request_usage(access_token: str, *, timeout: float, url: str = USAGE_URL) -> JsonDict | str:
    request = urllib.request.Request(
        url,
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
    if isinstance(result, str) and result.startswith(_PARAM_REJECTED):
        result = _request_usage(access_token, timeout=timeout, url=_PLAIN_USAGE_URL)
    if isinstance(result, str):
        return UsageSnapshot(None, None, plan, None, result)
    return UsageSnapshot(
        five_hour=_window(result.get("five_hour"), _FIVE_HOUR_MINUTES),
        seven_day=_window(result.get("seven_day"), _SEVEN_DAY_MINUTES),
        plan=plan,
        refreshed_at=int(time.time()),
        error=None,
        reset_vouchers=_vouchers(result.get("cedar_ember")),
        credits=_credits(result.get("extra_usage")),
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
