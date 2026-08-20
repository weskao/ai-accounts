"""Shared _utils helpers: atomic JSON writes and the OAuth refresh transport.

Per-provider behaviour lives with each provider (test_codex_accounts.py /
test_claude_accounts.py); this file covers the shared plumbing only.
Placeholder credentials throughout — never real tokens.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from ai_accounts import _utils
from ai_accounts import claude_accounts as ca


class AtomicWriteJsonTests(unittest.TestCase):
    def test_writes_content_mode_and_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "creds.json"
            _utils.atomic_write_json(target, {"token": "placeholder"})
            self.assertEqual(target.read_text(encoding="utf-8"), '{\n  "token": "placeholder"\n}\n')
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["creds.json"])

    def test_replaces_existing_file_wholesale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "creds.json"
            target.write_text("x" * 500, encoding="utf-8")
            _utils.atomic_write_json(target, {"a": 1})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1})

    def test_failure_raises_and_cleans_up_the_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "creds.json"
            with mock.patch.object(Path, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    _utils.atomic_write_json(target, {"a": 1})
            self.assertEqual(list(Path(tmp).iterdir()), [])


def _fake_response(payload: dict):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class OauthTokenRefreshTests(unittest.TestCase):
    def _capture(self, error=None, **kwargs) -> tuple[dict, tuple]:
        captured: dict = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            if error is not None:
                raise error
            return _fake_response({"access_token": "at-placeholder"})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _utils.oauth_token_refresh(
                "https://example.test/token", {"grant_type": "refresh_token", "refresh_token": "rt-x"}, **kwargs
            )
        return captured, result

    def test_json_body_by_default(self) -> None:
        captured, (response, error) = self._capture()
        request = captured["request"]
        self.assertIsNone(error)
        self.assertEqual(response, {"access_token": "at-placeholder"})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data.decode()), {"grant_type": "refresh_token", "refresh_token": "rt-x"})
        self.assertEqual(captured["timeout"], 30)

    def test_form_encoded_body_with_extra_headers(self) -> None:
        captured, (_, error) = self._capture(form_encoded=True, headers={"User-Agent": "ai-accounts-test"})
        request = captured["request"]
        self.assertIsNone(error)
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(request.get_header("User-agent"), "ai-accounts-test")
        self.assertEqual(request.data.decode(), "grant_type=refresh_token&refresh_token=rt-x")

    def test_http_error_is_classified_by_the_callers_callback(self) -> None:
        seen: list[tuple[int, str]] = []

        def classify(code: int, body: str) -> str:
            seen.append((code, body))
            return "revoked: per-provider verdict"

        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
        _, (response, message) = self._capture(error=error, http_error=classify)
        self.assertIsNone(response)
        self.assertEqual(message, "revoked: per-provider verdict")
        self.assertEqual(seen, [(403, '{"error":"invalid_grant"}')])

    def test_network_error_never_reaches_the_classifier(self) -> None:
        classify = mock.Mock(return_value="revoked: should not happen")
        error = urllib.error.URLError("offline")
        _, (response, message) = self._capture(error=error, http_error=classify)
        self.assertIsNone(response)
        self.assertIn("network error", message)
        classify.assert_not_called()


class ClaudeRefreshExpiryClaimTests(unittest.TestCase):
    """The refresh token, not the 8h access token, is what an account dies on."""

    def test_refresh_expiry_is_surfaced_in_milliseconds(self) -> None:
        claims = ca._claims_from_oauth({"refreshToken": "rt-x", "refreshTokenExpiresAt": 1_700_000_000_000})
        self.assertEqual(claims["refresh_expires_epoch"], 1_700_000_000)
        self.assertTrue(claims["refresh_expires_str"])

    def test_missing_refresh_expiry_stays_none(self) -> None:
        claims = ca._claims_from_oauth({"refreshToken": "rt-x"})
        self.assertIsNone(claims["refresh_expires_epoch"])
        self.assertIsNone(claims["refresh_expires_str"])


if __name__ == "__main__":
    unittest.main()
