from __future__ import annotations

import io
import os
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ai_accounts import usage_format as uf


class TestPrintNoActiveAccount(unittest.TestCase):
    def test_includes_provider_name_and_command_hints(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            uf.print_no_active_account("Codex", "codex-accounts")
        output = stderr.getvalue()
        self.assertIn("No active Codex account detected.", output)
        self.assertIn("codex-accounts save <name>", output)
        self.assertIn("codex-accounts switch <name>", output)


class TestAlignUsageCells(unittest.TestCase):
    def test_right_aligns_percent_and_entitlement_fraction(self) -> None:
        rows = [
            {"usage_premium": "24% · 18d 15h 11m · 47/200 AIC"},
            {"usage_premium": "5% · 18d 15h 11m · 9/200 AIC"},
        ]
        uf.align_usage_cells(rows, "usage_premium")
        self.assertEqual(rows[0]["usage_premium"], "24% · 18d 15h 11m · 47/200 AIC")
        self.assertEqual(rows[1]["usage_premium"], " 5% · 18d 15h 11m ·  9/200 AIC")

    def test_leaves_cells_without_fraction_untouched_by_the_fraction_logic(self) -> None:
        rows = [
            {"usage_premium": "9% · 3h 1m"},
            {"usage_premium": "10% · 3h 1m"},
        ]
        uf.align_usage_cells(rows, "usage_premium")
        self.assertEqual(rows[0]["usage_premium"], " 9% · 3h 1m")
        self.assertEqual(rows[1]["usage_premium"], "10% · 3h 1m")


class TestAlignNumericCells(unittest.TestCase):
    def test_right_aligns_numbers_to_the_widest(self) -> None:
        rows = [
            {"remaining": "0 AIC"},
            {"remaining": "49 AIC"},
            {"remaining": "152.4 AIC"},
        ]
        uf.align_numeric_cells(rows, "remaining")
        self.assertEqual(rows[0]["remaining"], "    0 AIC")
        self.assertEqual(rows[1]["remaining"], "   49 AIC")
        self.assertEqual(rows[2]["remaining"], "152.4 AIC")

    def test_leaves_non_numeric_cells_alone(self) -> None:
        rows = [
            {"remaining": "unlimited"},
            {"remaining": "—"},
            {"remaining": "7 AIC"},
        ]
        uf.align_numeric_cells(rows, "remaining")
        self.assertEqual(rows[0]["remaining"], "unlimited")
        self.assertEqual(rows[1]["remaining"], "—")
        self.assertEqual(rows[2]["remaining"], "7 AIC")


_NOW = 1_800_000_000
_DAY = 86_400


class TestResetVoucherRendering(unittest.TestCase):
    def test_count_unknown_is_dash_but_zero_stays_zero(self) -> None:
        self.assertEqual(uf.format_reset_count(None), "—")
        self.assertEqual(uf.format_reset_count(uf.ResetVouchers()), "—")
        self.assertEqual(uf.format_reset_count(uf.ResetVouchers(0)), "0")
        self.assertEqual(uf.format_reset_count(uf.ResetVouchers(3)), "3")

    def test_expiry_lists_every_voucher_soonest_first(self) -> None:
        soon, late = _NOW + 11 * _DAY + 4 * 3600 + 120, _NOW + 30 * _DAY
        self.assertEqual(
            uf.format_reset_expiry(uf.ResetVouchers(2, (late, soon)), now=_NOW), "11d 4h, 30d 0h"
        )

    def test_vouchers_expiring_at_the_same_time_are_grouped_with_a_count(self) -> None:
        soon, late = _NOW + 2 * _DAY, _NOW + 9 * _DAY + 3600
        vouchers = uf.ResetVouchers(3, (late, soon, soon + 60))
        self.assertEqual(uf.format_reset_expiry(vouchers, now=_NOW), "2d 0h ×2, 9d 1h")

    def test_expiry_is_dash_when_unknown_or_none(self) -> None:
        self.assertEqual(uf.format_reset_expiry(None, now=_NOW), "—")
        self.assertEqual(uf.format_reset_expiry(uf.ResetVouchers(2), now=_NOW), "—")

    def test_past_expiries_are_ignored_for_soonest_and_json(self) -> None:
        past, future = _NOW - _DAY, _NOW + 3 * _DAY
        vouchers = uf.ResetVouchers(2, (past, future))
        self.assertEqual(uf.format_reset_expiry(vouchers, now=_NOW), "3d 0h")
        self.assertEqual(uf.format_reset_expiry(uf.ResetVouchers(1, (past,)), now=_NOW), "—")
        self.assertEqual([v["expires_at_unix"] for v in uf.reset_vouchers_json(vouchers, now=_NOW)["vouchers"]], [future])

    @unittest.skipIf(os.name == "nt", "time.tzset is POSIX-only")
    def test_naive_iso_stamp_is_read_as_utc_whatever_the_local_zone(self) -> None:
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Taipei"  # UTC+8: parsing as local time would shift the result
        time.tzset()
        try:
            self.assertEqual(uf.parse_iso8601("2026-01-01T00:00:00"), 1_767_225_600)
        finally:
            os.environ.pop("TZ") if saved is None else os.environ.__setitem__("TZ", saved)
            time.tzset()

    def test_json_shape(self) -> None:
        soon = _NOW + 2 * _DAY
        result = uf.reset_vouchers_json(uf.ResetVouchers(2, (soon, soon + _DAY)), now=_NOW)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["expires_soonest"], "2027-01-17T08:00:00Z")
        self.assertEqual(
            result["vouchers"][0],
            {"expires_at": "2027-01-17T08:00:00Z", "expires_at_unix": soon, "expires_in_sec": 2 * _DAY},
        )
        self.assertEqual(len(result["vouchers"]), 2)

    def test_json_unknown_and_none(self) -> None:
        self.assertEqual(
            uf.reset_vouchers_json(uf.ResetVouchers(), now=_NOW),
            {"count": None, "expires_soonest": None, "vouchers": []},
        )
        self.assertIsNone(uf.reset_vouchers_json(None))


def _codex_payload(**credits: object) -> dict:
    return {"rate_limit": {}, "rate_limit_reset_credits": credits} if credits else {"rate_limit": {}}


class TestCodexVoucherCount(unittest.TestCase):
    def test_available_count_is_read(self) -> None:
        snap = uf._snapshot(_codex_payload(available_count=3, applicable_available_count=0))
        self.assertEqual(snap.reset_vouchers.count, 3)

    def test_zero_stays_zero(self) -> None:
        self.assertEqual(uf._snapshot(_codex_payload(available_count=0)).reset_vouchers.count, 0)

    def test_negative_count_clamps_to_zero(self) -> None:
        self.assertEqual(uf._snapshot(_codex_payload(available_count=-2)).reset_vouchers.count, 0)

    def test_absent_or_null_is_unknown(self) -> None:
        self.assertIsNone(uf._snapshot(_codex_payload()).reset_vouchers.count)
        self.assertIsNone(uf._snapshot(_codex_payload(available_count=None)).reset_vouchers.count)
        self.assertIsNone(uf._snapshot({"rate_limit_reset_credits": None}).reset_vouchers.count)

    def test_positional_construction_defaults_vouchers_to_unknown(self) -> None:
        self.assertEqual(uf.UsageSnapshot(None, None, None, "x").reset_vouchers, uf.ResetVouchers())


_AUTH = {"tokens": {"access_token": "token-placeholder", "account_id": "acct-placeholder"}}


def _credits_response(body: bytes) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


class TestFetchResetCreditExpiries(unittest.TestCase):
    def _fetch(self, body: dict | None) -> tuple[int, ...] | None:
        with mock.patch.object(uf, "_request_json", return_value=body if body is not None else "network error"):
            return uf.fetch_reset_credit_expiries(_AUTH)

    def test_only_available_credits_count_sorted_soonest_first(self) -> None:
        body = {
            "credits": [
                {"status": "available", "expires_at": "2099-10-22T19:43:18.787885Z"},
                {"status": "redeemed", "expires_at": "2099-10-01T00:00:00Z"},
                {"status": "available", "expires_at": "2099-10-10T00:00:00Z"},
                {"status": "available", "expires_at": None},
            ]
        }
        self.assertEqual(
            self._fetch(body),
            (uf.parse_iso8601("2099-10-10T00:00:00Z"), uf.parse_iso8601("2099-10-22T19:43:18Z")),
        )

    def test_past_expiries_are_dropped(self) -> None:
        body = {"credits": [{"status": "available", "expires_at": "2020-01-01T00:00:00Z"}]}
        self.assertEqual(self._fetch(body), ())

    def test_failure_or_odd_shape_is_unknown(self) -> None:
        self.assertIsNone(self._fetch(None))
        self.assertIsNone(self._fetch({"credits": "x"}))
        self.assertIsNone(self._fetch({}))

    def test_missing_token_is_unknown_without_a_request(self) -> None:
        with mock.patch.object(uf, "_request_json") as request:
            self.assertIsNone(uf.fetch_reset_credit_expiries({}))
            self.assertIsNone(uf.fetch_reset_credit_expiries(None))
        request.assert_not_called()

    def test_request_is_a_get_to_the_listing_url_not_consume(self) -> None:
        body = b'{"credits": []}'
        with mock.patch.object(uf.urllib.request.OpenerDirector, "open", return_value=_credits_response(body)) as opened:
            uf.fetch_reset_credit_expiries(_AUTH)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, uf.RESET_CREDITS_URL)
        self.assertNotIn("consume", request.full_url)
        self.assertEqual(request.get_header("Chatgpt-account-id"), "acct-placeholder")
        self.assertEqual(request.get_header("Authorization"), "Bearer token-placeholder")
        self.assertLessEqual(opened.call_args.kwargs["timeout"], 10)

    def test_http_errors_name_the_endpoint_that_failed(self) -> None:
        error = uf.urllib.error.HTTPError("u", 503, "x", {}, None)  # type: ignore[arg-type]
        with mock.patch.object(uf.urllib.request.OpenerDirector, "open", side_effect=error):
            self.assertEqual(uf._request_usage("t", None), "HTTP 503 from usage endpoint")
            self.assertEqual(
                uf._request_json(uf.RESET_CREDITS_URL, "t", None, 1, label="reset-credits endpoint"),
                "HTTP 503 from reset-credits endpoint",
            )

    def test_fetch_usage_never_asks_for_expiries(self) -> None:
        with mock.patch.object(uf, "_request_usage", return_value=_codex_payload(available_count=2)), \
                mock.patch.object(uf, "fetch_reset_credit_expiries") as expiries:
            uf.fetch_usage(_AUTH)
        expiries.assert_not_called()


class TestWithResetExpiries(unittest.TestCase):
    def _snap(self, count: int | None) -> uf.UsageSnapshot:
        return uf.UsageSnapshot(None, None, 1, None, uf.ResetVouchers(count))

    def test_skips_the_fetch_when_count_is_zero_or_unknown(self) -> None:
        for count in (0, None):
            snap = self._snap(count)
            with mock.patch.object(uf, "fetch_reset_credit_expiries") as fetch:
                self.assertIs(uf.with_reset_expiries(snap, _AUTH), snap)
            fetch.assert_not_called()

    def test_fills_expiries_when_count_positive(self) -> None:
        with mock.patch.object(uf, "fetch_reset_credit_expiries", return_value=(5, 9)):
            result = uf.with_reset_expiries(self._snap(2), _AUTH)
        self.assertEqual(result.reset_vouchers, uf.ResetVouchers(2, (5, 9)))
        self.assertEqual(result.refreshed_at, 1)

    def test_opt_out_env_keeps_the_count_and_skips_the_fetch(self) -> None:
        snap = self._snap(2)
        with mock.patch.dict(os.environ, {uf.SKIP_RESET_EXPIRY_ENV: "1"}), \
                mock.patch.object(uf, "fetch_reset_credit_expiries") as fetch:
            self.assertIs(uf.with_reset_expiries(snap, _AUTH), snap)
        fetch.assert_not_called()

    def test_fetch_failure_keeps_the_count_with_unknown_expiry(self) -> None:
        snap = self._snap(2)
        with mock.patch.object(uf, "fetch_reset_credit_expiries", return_value=None):
            self.assertIs(uf.with_reset_expiries(snap, _AUTH), snap)


if __name__ == "__main__":
    unittest.main()
