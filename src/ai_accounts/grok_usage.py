"""Grok Build SuperGrok quota lookup for grok-accounts.

``fetch_usage`` only — table/JSON formatting lives in ``usage_format``
(same split as ``claude_usage``/``copilot_usage``).

Verified against the Grok Build CLI billing proxy (2026-09-23):

- ``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`` with
  ``Authorization: Bearer <auth.json access token>`` and
  ``X-XAI-Token-Auth: xai-grok-cli`` returns weekly
  ``config.creditUsagePercent`` plus ``productUsage`` rows (``GrokBuild``).
- ``GET https://cli-chat-proxy.grok.com/v1/user?include=subscription`` returns
  ``subscriptionTier`` (``GrokPro`` for SuperGrok).

The routes are undocumented CLI-proxy endpoints, so every parse step returns
``None`` rather than raising — if they move or change shape, ``list``/``usage``
degrade to the previous no-quota path instead of breaking.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from .usage_format import UsageWindow, capitalize_first, format_unix_time_compact

BILLING_URL: Final = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
USER_URL: Final = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"

_HEADERS: Final = {
    "Accept": "application/json",
    "User-Agent": "ai-accounts",
    "X-XAI-Token-Auth": "xai-grok-cli",
}

_WEEK_MINUTES: Final = 7 * 24 * 60

# Consumer names for the CLI-proxy's subscriptionTier values.
# SuperGrok is ``GrokPro``. Plus/Heavy names come from the same field
# as reported by other Grok CLI usage tools (SuperGrokPlus / SuperGrokHeavy).
_PLAN_ALIASES: Final = {
    "grokpro": "SuperGrok",
    "supergrok": "SuperGrok",
    "grokproplus": "SuperGrok Plus",
    "supergrokplus": "SuperGrok Plus",
    "supergrok heavy": "SuperGrok Heavy",
    "supergrokheavy": "SuperGrok Heavy",
    "grokheavy": "SuperGrok Heavy",
    "free": "Free",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    weekly: UsageWindow | None
    build: UsageWindow | None
    plan: str | None
    subscription_tier: str | None
    refreshed_at: int | None
    error: str | None


_EMPTY: Final = UsageSnapshot(None, None, None, None, None, None)


def plan_label(tier: str | None) -> str | None:
    """Consumer plan name for a CLI-proxy ``subscriptionTier`` string."""
    if not isinstance(tier, str) or not tier.strip():
        return None
    mapped = _PLAN_ALIASES.get(tier.strip().lower().replace("_", "").replace(" ", ""))
    if mapped:
        return mapped
    return capitalize_first(tier.strip())


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _parse_iso(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp())


def _percent(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return max(0, min(100, round(number)))


def _period_reset(config: dict[str, Any]) -> tuple[int | None, int | None, bool]:
    """``(reset_epoch, window_minutes, is_weekly)`` from ``currentPeriod``."""
    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        period = {}
    kind = str(period.get("type") or "")
    weekly = kind == "USAGE_PERIOD_TYPE_WEEKLY"
    start = _parse_iso(period.get("start") or config.get("billingPeriodStart"))
    end = _parse_iso(period.get("end") or config.get("billingPeriodEnd"))
    minutes = _WEEK_MINUTES if weekly else None
    if start is not None and end is not None and end > start:
        minutes = max(1, (end - start + 59) // 60)
    return end, minutes, weekly


def _window(percent: int | None, reset: int | None, minutes: int | None) -> UsageWindow | None:
    if percent is None:
        return None
    return UsageWindow(percentage=percent, reset_time=reset, window_minutes=minutes)


def _build_percent(config: dict[str, Any]) -> int | None:
    products = config.get("productUsage")
    if not isinstance(products, list):
        return None
    for row in products:
        if not isinstance(row, dict):
            continue
        if str(row.get("product") or "").lower() != "grokbuild":
            continue
        return _percent(row.get("usagePercent"))
    return None


def _plan_from_user(user: dict[str, Any]) -> tuple[str | None, str | None]:
    tier = user.get("subscriptionTier")
    tier_text = tier if isinstance(tier, str) and tier else None
    plan = plan_label(tier_text)
    if plan is None and isinstance(user.get("userId"), str):
        # SuperGrok is ``GrokPro``. A 200 from ``/user`` with
        # ``subscriptionTier: null`` is the free plan.
        plan = "Free"
    return plan, tier_text


def _parse(billing: dict[str, Any], user: dict[str, Any]) -> UsageSnapshot:
    config = billing.get("config")
    if not isinstance(config, dict):
        return UsageSnapshot(None, None, None, None, None, "unexpected quota shape")
    reset, minutes, weekly = _period_reset(config)
    used = _percent(config.get("creditUsagePercent"))
    if used is None and weekly:
        # Proto3 JSON omits default zeros. A confirmed weekly period with no
        # percent is a fresh 0% window, matching grok.com/billing at reset.
        used = 0
    plan, tier_text = _plan_from_user(user)
    return UsageSnapshot(
        weekly=_window(used, reset, minutes),
        build=_window(_build_percent(config), reset, minutes),
        plan=plan,
        subscription_tier=tier_text,
        refreshed_at=int(time.time()),
        error=None,
    )


def _request(url: str, token: str, *, timeout: float) -> dict[str, Any] | str:
    request = urllib.request.Request(
        url,
        headers={**_HEADERS, "Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
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


def fetch_usage(token: str | None, *, timeout: float = 20) -> UsageSnapshot:
    """Weekly SuperGrok credits and Grok Build slice for one OAuth access token.

    Never raises: an unreachable endpoint, a rejected token, or an unexpected
    JSON shape come back as a snapshot with ``error`` set and every window
    ``None``. The subscription probe is best-effort — a billing success with
    a failed ``/user`` call still returns the weekly window.
    """
    if not token:
        return UsageSnapshot(None, None, None, None, None, "missing access token")
    billing = _request(BILLING_URL, token, timeout=timeout)
    user = _request(USER_URL, token, timeout=timeout)
    user_doc = user if isinstance(user, dict) else {}
    if isinstance(billing, str):
        plan, tier_text = _plan_from_user(user_doc)
        return UsageSnapshot(
            None, None, plan, tier_text, int(time.time()) if plan else None, billing
        )
    return _parse(billing, user_doc)


def empty_usage() -> UsageSnapshot:
    """The all-``None`` snapshot used when a profile has no token to query."""
    return _EMPTY


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
