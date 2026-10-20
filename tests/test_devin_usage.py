"""Devin CLI quota parsing (undocumented Connect RPC; shape is an assumption).

Fixtures use placeholder identities only. urllib is mocked — no network.
"""

from __future__ import annotations

import http.client
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from ai_accounts import devin_usage as du
from ai_accounts.usage_format import UsageWindow


def _status(**plan_status: object) -> dict:
    return {
        "userStatus": {
            "email": "user@example.com",
            "name": "Test",
            "planStatus": {
                "planInfo": {"planName": "Pro"},
                "dailyQuotaUsedPercent": 25.4,
                "dailyQuotaResetAtUnix": 1894060800,
                "weeklyQuotaUsedPercent": 60,
                "weeklyQuotaResetAtUnix": "2030-01-08T00:00:00Z",
                **plan_status,
            },
        }
    }


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _fetch(token: str | None, body: object = None, *, raw: bytes | None = None, side_effect=None):
    payload = raw if raw is not None else json.dumps(body).encode()
    with mock.patch.object(
        du._OPENER, "open", side_effect=side_effect, return_value=_Resp(payload)
    ) as opener:
        snap = du.fetch_usage(token)
    return snap, opener


class DevinUsageTests(unittest.TestCase):
    def test_epoch_milliseconds_reset_converts_to_seconds(self) -> None:
        snap, _ = _fetch("k", _status(dailyQuotaResetAtUnix=1894060800000, weeklyQuotaResetAtUnix="1894060800000"))
        self.assertEqual(snap.daily.reset_time, 1894060800)
        self.assertEqual(snap.weekly.reset_time, 1894060800)

    def test_redirect_is_not_followed_so_the_key_is_never_resent(self) -> None:
        # Real loopback servers: the production opener must refuse a 302, which a
        # default opener follows (POST -> GET) to whatever host the response names.
        hits: list[str] = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                hits.append(self.command)
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *args: object) -> None:
                return None

        target = HTTPServer(("127.0.0.1", 0), Target)

        class Redirector(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return None

        redirector = HTTPServer(("127.0.0.1", 0), Redirector)
        for server in (target, redirector):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        with mock.patch.object(du, "USAGE_URL", f"http://127.0.0.1:{redirector.server_port}/rpc"):
            snap = du.fetch_usage("secret-key-123", timeout=5)
        self.assertEqual(snap.error, "HTTP 302 from usage endpoint")
        self.assertEqual(hits, [])

    def test_success_reads_both_windows_plan_and_email(self) -> None:
        snap, _ = _fetch("key-123", _status())
        self.assertIsNone(snap.error)
        self.assertEqual(snap.plan, "Pro")
        self.assertEqual(snap.email, "user@example.com")
        self.assertEqual(snap.daily, UsageWindow(25, 1894060800, 1440))
        self.assertEqual(snap.weekly, UsageWindow(60, 1894060800, 10080))
        self.assertIsNotNone(snap.refreshed_at)

    def test_request_shape_keeps_session_token_prefix(self) -> None:
        _, opener = _fetch("devin-session-token$key-123", _status())
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, du.USAGE_URL)
        self.assertEqual(request.get_header("Connect-protocol-version"), "1")
        body = json.loads(request.data)
        # Live check 2026-10-11: the stripped key gets HTTP 401, the full one 200.
        self.assertEqual(body["metadata"]["apiKey"], "devin-session-token$key-123")
        self.assertEqual(body["metadata"]["ideName"], "devin-cli")

    def test_missing_token_never_hits_network(self) -> None:
        for token in (None, "", "devin-session-token$"):
            snap, opener = _fetch(token, _status())
            self.assertEqual(snap.error, "missing access token")
            opener.assert_not_called()

    def test_http_error(self) -> None:
        exc = urllib.error.HTTPError(du.USAGE_URL, 401, "Unauthorized", {}, io.BytesIO(b""))
        snap, _ = _fetch("k", side_effect=exc)
        self.assertEqual(snap.error, "HTTP 401 from usage endpoint")
        self.assertIsNone(snap.daily)

    def test_network_error(self) -> None:
        snap, _ = _fetch("k", side_effect=urllib.error.URLError("down"))
        self.assertEqual(snap.error, "network error")

    def test_malformed_json(self) -> None:
        snap, _ = _fetch("k", raw=b"{not json")
        self.assertEqual(snap.error, "invalid response")

    def test_no_quota_fields_is_unexpected_shape(self) -> None:
        for body in ({"userStatus": {"email": "user@example.com"}}, {}, [1]):
            snap, _ = _fetch("k", body)
            self.assertEqual(snap.error, "unexpected quota shape")
            self.assertIsNone(snap.weekly)

    def test_percent_clamped(self) -> None:
        snap, _ = _fetch(
            "k", _status(dailyQuotaUsedPercent=140, weeklyQuotaUsedPercent=-5)
        )
        self.assertEqual(snap.daily.percentage, 100)
        self.assertEqual(snap.weekly.percentage, 0)

    def test_alternate_candidate_keys_and_single_window(self) -> None:
        body = {
            "userStatus": {
                "planStatus": {"weekly": {"usedPercent": 7, "resetTime": "2030-01-08T00:00:00Z"}}
            }
        }
        snap, _ = _fetch("k", body)
        self.assertIsNone(snap.error)
        self.assertIsNone(snap.daily)
        self.assertEqual(snap.weekly, UsageWindow(7, 1894060800, 10080))

    def test_empty_usage_is_all_none(self) -> None:
        snap = du.empty_usage()
        self.assertIsNone(snap.daily)
        self.assertIsNone(snap.weekly)
        self.assertIsNone(snap.error)

    def test_hostile_reset_strings_degrade_to_no_reset(self) -> None:
        for reset in ("²", "9" * 5000):
            snap, _ = _fetch("k", _status(dailyQuotaResetAtUnix=reset))
            self.assertIsNone(snap.error, reset[:5])
            self.assertEqual(snap.daily.percentage, 25)
            self.assertIsNone(snap.daily.reset_time)

    def test_huge_json_int_percent_is_ignored_not_raised(self) -> None:
        raw = b'{"userStatus": {"planStatus": {"dailyQuotaUsedPercent": 1' + b"0" * 400 + b"}}}"
        snap, _ = _fetch("k", raw=raw)
        self.assertEqual(snap.error, "unexpected quota shape")
        snap, _ = _fetch("k", _status(weeklyQuotaUsedPercent=10**400))
        self.assertIsNone(snap.weekly)
        self.assertEqual(snap.daily.percentage, 25)

    def test_http_client_errors_are_network_errors_without_key(self) -> None:
        key = "secret-key-abc"
        for exc in (http.client.IncompleteRead(b"x"), http.client.BadStatusLine(key)):
            snap, _ = _fetch(key, side_effect=exc)
            self.assertEqual(snap.error, "network error")
            self.assertNotIn(key, snap.error)

    def test_incomplete_read_from_response_body(self) -> None:
        resp = _Resp(b"")
        resp.read = mock.Mock(side_effect=http.client.IncompleteRead(b"partial"))
        with mock.patch.object(du._OPENER, "open", return_value=resp):
            snap = du.fetch_usage("k")
        self.assertEqual(snap.error, "network error")

    def test_deeply_nested_json_is_invalid_response(self) -> None:
        snap, _ = _fetch("k", raw=b"[" * 100000)
        self.assertEqual(snap.error, "invalid response")

    def test_parse_errors_become_unexpected_shape(self) -> None:
        with mock.patch.object(du, "_parse", side_effect=ValueError("boom k")):
            snap, _ = _fetch("k", _status())
        self.assertEqual(snap.error, "unexpected quota shape")
        self.assertNotIn("boom", snap.error)

    def test_live_shape_reports_remaining_percent_as_used(self) -> None:
        # Live check 2026-10-11: the server sends *remaining* percent, reset as a digit string.
        plan_status = {
            "planInfo": {"planName": "Pro"},
            "dailyQuotaRemainingPercent": 75,
            "weeklyQuotaRemainingPercent": 40,
            "dailyQuotaResetAtUnix": "1894060800",
            "weeklyQuotaResetAtUnix": "1894060800",
        }
        snap, _ = _fetch("k", {"userStatus": {"planStatus": plan_status}})
        self.assertIsNone(snap.error)
        self.assertEqual(snap.daily, UsageWindow(25, 1894060800, 1440))
        self.assertEqual(snap.weekly, UsageWindow(60, 1894060800, 10080))

    def test_reset_without_percent_means_exhausted(self) -> None:
        # proto3 JSON omits zero values; the live field is *remaining*, so an omitted
        # one is 0% left. Showing 0% used here would stop autoswitch from ever firing.
        body = {"userStatus": {"planStatus": {"dailyQuotaResetAtUnix": 1894060800}}}
        snap, _ = _fetch("k", body)
        self.assertIsNone(snap.error)
        self.assertEqual(snap.daily, UsageWindow(100, 1894060800, 1440))
        self.assertIsNone(snap.weekly)

    def test_remaining_percent_clamped_and_garbage_is_not_exhausted(self) -> None:
        snap, _ = _fetch(
            "k",
            _status(
                dailyQuotaUsedPercent=None, dailyQuotaRemainingPercent=140,
                weeklyQuotaUsedPercent=None, weeklyQuotaRemainingPercent="lots",
            ),
        )
        self.assertEqual(snap.daily.percentage, 0)
        self.assertIsNone(snap.weekly)

    def test_garbage_percent_with_reset_is_not_zero(self) -> None:
        snap, _ = _fetch("k", _status(weeklyQuotaUsedPercent="lots"))
        self.assertIsNone(snap.weekly)

    def test_json_int_over_digit_limit_is_invalid_response(self) -> None:
        raw = b'{"userStatus": {"planStatus": {"dailyQuotaUsedPercent": 1' + b"0" * 5000 + b"}}}"
        snap, _ = _fetch("k", raw=raw)
        self.assertEqual(snap.error, "invalid response")

    def test_out_of_range_reset_is_dropped(self) -> None:
        for reset in (10**27, 1e300, -5, "9" * 30):
            snap, _ = _fetch("k", _status(dailyQuotaResetAtUnix=reset))
            self.assertIsNone(snap.daily.reset_time, reset)
