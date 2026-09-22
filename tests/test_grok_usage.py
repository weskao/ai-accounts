"""Grok Build SuperGrok billing/subscription parsing.

Fixtures mirror the live CLI-proxy shape verified on 2026-09-23
(``GET /v1/billing?format=credits`` + ``GET /v1/user?include=subscription``)
with placeholder identities only.
"""

from __future__ import annotations

import unittest
from unittest import mock

from ai_accounts import grok_usage as gu
from ai_accounts.usage_format import UsageWindow


def _billing(
    *,
    percent: float | None = 12.0,
    build_percent: float | None = 12.0,
    period: bool = True,
) -> dict:
    config: dict = {
        "onDemandCap": {"val": 0},
        "onDemandUsed": {"val": 0},
        "isUnifiedBillingUser": True,
        "prepaidBalance": {"val": 0},
        "billingPeriodStart": "2030-01-01T00:00:00+00:00",
        "billingPeriodEnd": "2030-01-08T00:00:00+00:00",
    }
    if period:
        config["currentPeriod"] = {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2030-01-01T00:00:00+00:00",
            "end": "2030-01-08T00:00:00+00:00",
        }
    if percent is not None:
        config["creditUsagePercent"] = percent
    products = []
    if build_percent is not None:
        products.append({"product": "GrokBuild", "usagePercent": build_percent})
    products.append({"product": "GrokChat"})
    config["productUsage"] = products
    return {"config": config}


def _user(tier: str = "GrokPro") -> dict:
    return {
        "userId": "00000000-0000-0000-0000-000000000001",
        "firstName": "Test",
        "principalType": "User",
        "hasGrokCodeAccess": True,
        "subscriptionTier": tier,
    }


class GrokUsageTests(unittest.TestCase):
    def test_maps_grokpro_subscription_to_supergrok_and_reads_weekly_build(self) -> None:
        with mock.patch.object(gu, "_request", side_effect=[_billing(), _user()]):
            snap = gu.fetch_usage("access-token")

        self.assertIsNone(snap.error)
        self.assertEqual(snap.plan, "SuperGrok")
        self.assertEqual(snap.subscription_tier, "GrokPro")
        self.assertEqual(
            snap.weekly,
            UsageWindow(percentage=12, reset_time=1894060800, window_minutes=10080),
        )
        self.assertEqual(
            snap.build,
            UsageWindow(percentage=12, reset_time=1894060800, window_minutes=10080),
        )

    def test_maps_known_paid_tiers_to_consumer_plan_names(self) -> None:
        self.assertEqual(gu.plan_label("GrokPro"), "SuperGrok")
        self.assertEqual(gu.plan_label("SuperGrok"), "SuperGrok")
        self.assertEqual(gu.plan_label("SuperGrokPlus"), "SuperGrok Plus")
        self.assertEqual(gu.plan_label("SuperGrokHeavy"), "SuperGrok Heavy")
        self.assertEqual(gu.plan_label("free"), "Free")
        self.assertEqual(gu.plan_label("Enterprise"), "Enterprise")
        self.assertIsNone(gu.plan_label(None))
        self.assertIsNone(gu.plan_label(""))

    def test_omitted_weekly_percent_is_zero_not_missing(self) -> None:
        # At a fresh weekly reset the proxy omits creditUsagePercent
        # (proto3 JSON drops zero). A confirmed weekly period still means 0%.
        with mock.patch.object(
            gu, "_request", side_effect=[_billing(percent=None, build_percent=None), _user()]
        ):
            snap = gu.fetch_usage("access-token")

        self.assertEqual(snap.weekly.percentage, 0)
        self.assertIsNone(snap.build)
        self.assertEqual(snap.plan, "SuperGrok")

    def test_null_subscription_tier_is_free(self) -> None:
        user = _user()
        user["subscriptionTier"] = None
        with mock.patch.object(gu, "_request", side_effect=[_billing(), user]):
            snap = gu.fetch_usage("access-token")
        self.assertEqual(snap.plan, "Free")
        self.assertIsNone(snap.subscription_tier)

    def test_user_probe_failure_still_returns_weekly_usage(self) -> None:
        with mock.patch.object(
            gu, "_request", side_effect=[_billing(), "HTTP 403 from usage endpoint"]
        ):
            snap = gu.fetch_usage("access-token")

        self.assertIsNone(snap.error)
        self.assertIsNone(snap.plan)
        self.assertEqual(snap.weekly.percentage, 12)

    def test_billing_failure_still_reports_plan_from_user(self) -> None:
        with mock.patch.object(
            gu, "_request", side_effect=["HTTP 503 from usage endpoint", _user()]
        ):
            snap = gu.fetch_usage("access-token")
        self.assertEqual(snap.plan, "SuperGrok")
        self.assertEqual(snap.error, "HTTP 503 from usage endpoint")
        self.assertIsNone(snap.weekly)

    def test_missing_token_and_http_errors_degrade(self) -> None:
        missing = gu.fetch_usage(None)
        self.assertEqual(missing.error, "missing access token")
        self.assertIsNone(missing.weekly)

        with mock.patch.object(gu, "_request", return_value="HTTP 401 from usage endpoint"):
            failed = gu.fetch_usage("expired-token")
        self.assertEqual(failed.error, "HTTP 401 from usage endpoint")
        self.assertIsNone(failed.weekly)
        self.assertIsNone(failed.plan)

    def test_empty_usage_is_all_none(self) -> None:
        snap = gu.empty_usage()
        self.assertIsNone(snap.weekly)
        self.assertIsNone(snap.build)
        self.assertIsNone(snap.plan)
        self.assertIsNone(snap.error)
