from __future__ import annotations

import unittest
from unittest import mock

from ai_accounts import claude_usage as cu
from ai_accounts.usage_format import ResetVouchers

_WINDOWS = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-01-01T05:00:00Z"},
    "seven_day": {"utilization": 40, "resets_at": "2026-01-08T00:00:00Z"},
}


def _grant(left: int, ends_at: str | None = "2099-02-01T00:00:00+00:00") -> dict:
    return {"resets_left": left, "resets_total": 1, "ends_at": ends_at, "usable_now": True}


class TestVouchers(unittest.TestCase):
    def test_sums_resets_left_across_grants_and_sorts_expiries(self) -> None:
        later, sooner = "2099-02-01T00:00:00+00:00", "2099-01-15T00:00:00+00:00"
        vouchers = cu._vouchers({"eligible": True, "grants": [_grant(2, later), _grant(1, sooner)]})
        self.assertEqual(vouchers.count, 3)
        self.assertEqual(
            vouchers.expires_at,
            (cu._parse_iso8601(sooner), cu._parse_iso8601(later), cu._parse_iso8601(later)),
        )

    def test_grants_without_resets_left_add_no_count_or_expiry(self) -> None:
        vouchers = cu._vouchers({"eligible": True, "grants": [_grant(0), _grant(1, "2099-03-01T00:00:00Z")]})
        self.assertEqual(vouchers.count, 1)
        self.assertEqual(len(vouchers.expires_at), 1)

    def test_eligible_without_grants_is_zero_not_unknown(self) -> None:
        self.assertEqual(cu._vouchers({"eligible": True, "grants": []}), ResetVouchers(0, ()))

    def test_account_level_ineligible_reasons_are_zero(self) -> None:
        for reason in sorted(cu._ACCOUNT_LEVEL_REASONS):
            payload = {"eligible": False, "ineligible_reason": reason, "grants": [_grant(1)]}
            self.assertEqual(cu._vouchers(payload), ResetVouchers(0, ()), reason)

    def test_client_level_or_unknown_ineligible_reasons_are_unknown_not_zero(self) -> None:
        for reason in ("surface", "cli_version", "brand_new_reason", None):
            payload = {"eligible": False, "ineligible_reason": reason}
            self.assertEqual(cu._vouchers(payload), ResetVouchers(), reason)

    def test_unusable_and_ended_grants_are_skipped(self) -> None:
        now = cu._parse_iso8601("2026-01-20T00:00:00Z")
        grants = [
            {**_grant(1, "2026-02-01T00:00:00Z"), "usable_now": False},
            _grant(1, "2026-01-10T00:00:00Z"),
            _grant(2, "2026-03-01T00:00:00Z"),
        ]
        vouchers = cu._vouchers({"eligible": True, "grants": grants}, now=now)
        self.assertEqual(vouchers.count, 2)
        self.assertEqual(vouchers.expires_at, (cu._parse_iso8601("2026-03-01T00:00:00Z"),) * 2)

    def test_missing_or_odd_block_is_unknown(self) -> None:
        for odd in (None, "x", [], {"eligible": True}, {"eligible": True, "grants": "x"}):
            self.assertIsNone(cu._vouchers(odd).count, odd)

    def test_malformed_grant_entries_are_skipped(self) -> None:
        grants = ["x", {"resets_left": True}, {"resets_left": "1"}, _grant(1)]
        self.assertEqual(cu._vouchers({"eligible": True, "grants": grants}).count, 1)


class TestFetchUsage(unittest.TestCase):
    def test_old_shape_payload_without_cedar_ember_still_parses(self) -> None:
        with mock.patch.object(cu, "_request_usage", return_value=dict(_WINDOWS)):
            snap = cu.fetch_usage("token-placeholder")
        self.assertIsNone(snap.error)
        self.assertEqual(snap.five_hour.percentage, 12)
        self.assertEqual(snap.seven_day.percentage, 40)
        self.assertEqual(snap.reset_vouchers, ResetVouchers())

    def test_broken_cedar_ember_leaves_windows_intact(self) -> None:
        payload = {**_WINDOWS, "cedar_ember": {"eligible": True, "grants": 7}}
        with mock.patch.object(cu, "_request_usage", return_value=payload):
            snap = cu.fetch_usage("token-placeholder")
        self.assertEqual(snap.five_hour.percentage, 12)
        self.assertIsNone(snap.reset_vouchers.count)

    def test_vouchers_are_attached_to_the_snapshot(self) -> None:
        payload = {**_WINDOWS, "cedar_ember": {"eligible": True, "grants": [_grant(1)]}}
        with mock.patch.object(cu, "_request_usage", return_value=payload):
            snap = cu.fetch_usage("token-placeholder")
        self.assertEqual(snap.reset_vouchers.count, 1)

    def test_param_rejection_retries_plain_url_and_keeps_windows(self) -> None:
        with mock.patch.object(
            cu, "_request_usage", side_effect=["HTTP 400 from usage endpoint", dict(_WINDOWS)]
        ) as request:
            snap = cu.fetch_usage("token-placeholder")
        self.assertEqual(request.call_args_list[1].kwargs["url"], cu._PLAIN_USAGE_URL)
        self.assertNotIn("cedar_ember", cu._PLAIN_USAGE_URL)
        self.assertIsNone(snap.error)
        self.assertEqual(snap.five_hour.percentage, 12)
        self.assertIsNone(snap.reset_vouchers.count)

    def test_other_http_errors_are_not_retried(self) -> None:
        with mock.patch.object(cu, "_request_usage", return_value="HTTP 401 from usage endpoint") as request:
            snap = cu.fetch_usage("token-placeholder")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(snap.error, "HTTP 401 from usage endpoint")

    def test_positional_construction_defaults_vouchers_to_unknown(self) -> None:
        self.assertEqual(cu.UsageSnapshot(None, None, None, None, None).reset_vouchers, ResetVouchers())

    def test_request_asks_for_cedar_ember_with_cli_user_agent_via_get(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with mock.patch.object(cu.urllib.request, "urlopen", return_value=response) as urlopen:
            cu._request_usage("token-placeholder", timeout=5)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.endswith("/api/oauth/usage?cedar_ember=1"))
        self.assertRegex(request.get_header("User-agent"), r"^claude-cli/\S+ \(external, cli\)$")


if __name__ == "__main__":
    unittest.main()
