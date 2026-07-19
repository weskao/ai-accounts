"""Tests for agy-accounts (Gemini CLI profile manager).

All filesystem access is redirected into a temp dir via GEMINI_CLI_HOME (which
agy-accounts resolves to ``<home>/.gemini``, mirroring the real CLI); the OAuth
refresh HTTP call is mocked — no network, no real tokens. Run with:
``uv run pytest tests/test_gemini_accounts.py``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from unittest import mock

from ai_accounts import gemini_accounts as ga
from ai_accounts import gemini_usage as gu


def _jwt(payload: ga.JsonDict) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _creds(
    sub: str,
    email: str,
    *,
    name: str = "Test User",
    refresh_token: str = "rt-old",
    access_token: str = "at-old",
    expires_in_ms: int = 3600 * 1000,
) -> ga.JsonDict:
    """A Gemini oauth_creds.json payload with a decodable id_token."""
    exp = int(time.time()) + expires_in_ms // 1000
    id_token = _jwt({"email": email, "name": name, "sub": sub, "exp": exp})
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "scope": "openid email profile",
        "token_type": "Bearer",
        "expiry_date": int(time.time() * 1000) + expires_in_ms,
    }


class _GeminiHomeMixin(unittest.TestCase):
    def __init__(self, methodName: str = "runTest"):
        super().__init__(methodName)
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".gemini"

    def setUp(self):
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / ".gemini"
        env = mock.patch.dict(
            os.environ, {"GEMINI_CLI_HOME": self.tmp.name}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        for var in ("GEMINI_ACCOUNT_DIR", "GEMINI_OAUTH_JSON"):
            os.environ.pop(var, None)
        (self.home / "accounts").mkdir(parents=True)

    def write_auth(self, payload: ga.JsonDict) -> Path:
        path = self.home / "oauth_creds.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_profile(self, name: str, payload: ga.JsonDict) -> Path:
        path = self.home / "accounts" / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def mark_current(self, name: str) -> Path:
        path = self.home / "accounts" / ".current-profile"
        path.write_text(name, encoding="utf-8")
        return path

    def google_accounts(self) -> ga.JsonDict:
        path = self.home / "google_accounts.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def run_quiet(self, fn, *args) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return fn(*args)

    def run_capture(self, fn, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = fn(*args)
        return rc, out.getvalue(), err.getvalue()


class ClaimsTests(_GeminiHomeMixin):
    def test_claims_decode_email_sub_and_ms_expiry(self):
        payload = _creds("sub-123", "a@x.com", expires_in_ms=7200 * 1000)
        claims = ga._claims_from_auth(payload)
        self.assertEqual(claims["email"], "a@x.com")
        self.assertEqual(claims["account_id"], "sub-123")
        # expiry_date is milliseconds → seconds; must land ~2h in the future.
        expires_epoch = claims["expires_epoch"]
        if not isinstance(expires_epoch, int):
            self.fail("expected integer expiry")
        self.assertAlmostEqual(expires_epoch, int(time.time()) + 7200, delta=5)

    def test_claims_fall_back_to_id_token_exp_without_expiry_date(self):
        payload = _creds("sub-1", "a@x.com")
        del payload["expiry_date"]
        claims = ga._claims_from_auth(payload)
        self.assertIsNotNone(claims["expires_epoch"])


class OauthRefreshRequestTests(unittest.TestCase):
    def test_request_hits_google_token_endpoint_with_client_creds(self):
        captured = {}

        class FakeResponse:
            def read(self):
                return json.dumps(
                    {"access_token": "at-new", "expires_in": 3600}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode()
            captured["content_type"] = request.get_header("Content-type")
            return FakeResponse()

        with (
            mock.patch.object(
                ga,
                "_oauth_client_credentials",
                return_value=("fixture-client-id", "fixture-client-secret"),
            ),
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            refreshed, error = ga._oauth_refresh("rt-123")

        self.assertIsNone(error)
        self.assertEqual(refreshed, {"access_token": "at-new", "expires_in": 3600})
        self.assertEqual(captured["url"], "https://oauth2.googleapis.com/token")
        self.assertEqual(captured["content_type"], "application/x-www-form-urlencoded")
        self.assertIn("grant_type=refresh_token", captured["body"])
        self.assertIn("refresh_token=rt-123", captured["body"])
        self.assertIn("client_id=fixture-client-id", captured["body"])

    def test_http_error_is_reported_not_raised(self):
        err = urllib.error.HTTPError(
            "u", 401, "Unauthorized", Message(), io.BytesIO(b"")
        )
        with (
            mock.patch.object(
                ga, "_oauth_client_credentials", return_value=("id", "secret")
            ),
            mock.patch("urllib.request.urlopen", side_effect=err),
        ):
            refreshed, error = ga._oauth_refresh("rt-bad")
        self.assertIsNone(refreshed)
        self.assertIsNotNone(error)
        self.assertIn("401", error or "")

    def test_client_credentials_are_loaded_from_installed_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "gemini-cli"
            executable = package / "bundle" / "gemini.js"
            executable.parent.mkdir(parents=True)
            executable.write_text(
                "const OAUTH_CLIENT_ID = 'fixture-id';\n"
                "const OAUTH_CLIENT_SECRET = 'fixture-secret';\n",
                encoding="utf-8",
            )
            with mock.patch.object(ga.shutil, "which", return_value=str(executable)):
                self.assertEqual(
                    ga._oauth_client_credentials(), ("fixture-id", "fixture-secret")
                )


class GeminiUsageTests(_GeminiHomeMixin):
    def test_fetch_usage_uses_oauth_and_groups_model_quota(self):
        auth = self.write_auth(_creds("sub-a", "a@x.com", access_token="oauth-access"))
        requests: list[urllib.request.Request] = []

        class FakeResponse:
            def __init__(self, payload: ga.JsonValue):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            if request.full_url.endswith("loadCodeAssist"):
                return FakeResponse({"cloudaicompanionProject": {"id": "project-1"}})
            return FakeResponse(
                {
                    "buckets": [
                        {
                            "modelId": "gemini-2.5-pro",
                            "remainingFraction": 0.75,
                            "resetTime": "2030-01-01T00:00:00Z",
                        },
                        {"modelId": "gemini-2.5-flash", "remainingFraction": 0.4},
                        {"modelId": "gemini-2.5-flash-lite", "remainingFraction": 0.9},
                    ]
                }
            )

        with mock.patch.object(gu.urllib.request, "urlopen", side_effect=fake_urlopen):
            usage = gu.fetch_usage(auth)

        if usage.pro is None or usage.flash is None or usage.flash_lite is None:
            self.fail("expected all Gemini quota tiers")
        self.assertEqual(
            (usage.pro.percentage, usage.flash.percentage, usage.flash_lite.percentage),
            (25, 60, 10),
        )
        request_body = requests[1].data
        if not isinstance(request_body, bytes):
            self.fail("expected byte request body")
        self.assertEqual(json.loads(request_body), {"project": "project-1"})
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer oauth-access")


class SaveCommandTests(_GeminiHomeMixin):
    def test_save_persists_active_auth_and_sets_google_active(self):
        self.write_auth(_creds("sub-w", "w@x.com", refresh_token="rt-live"))
        rc = self.run_quiet(ga.cmd_save, "work")

        self.assertEqual(rc, 0)
        saved = json.loads((self.home / "accounts" / "work.json").read_text())
        self.assertEqual(saved["refresh_token"], "rt-live")
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )
        # Gemini's own account tracker updated to the saved account.
        self.assertEqual(self.google_accounts()["active"], "w@x.com")

    def test_save_without_auth_errors(self):
        rc = self.run_quiet(ga.cmd_save, "nope")
        self.assertEqual(rc, 1)
        self.assertFalse((self.home / "accounts" / "nope.json").exists())

    def test_login_switch_saves_isolated_auth(self):
        fresh = json.dumps(_creds("sub-new", "new@x.com", refresh_token="rt-fresh"))
        with (
            mock.patch.object(ga, "ensure_tool", return_value=True),
            mock.patch.object(ga, "_run_isolated_login", return_value=(fresh, 0)),
        ):
            rc = self.run_quiet(ga.cmd_login_switch, "newacct")

        self.assertEqual(rc, 0)
        saved = json.loads((self.home / "accounts" / "newacct.json").read_text())
        self.assertEqual(saved["refresh_token"], "rt-fresh")

    def test_login_switch_does_not_clobber_current_profile_on_cancel(self):
        current = self.write_profile(
            "current", _creds("sub-old", "old@x.com", refresh_token="rt-current")
        )
        self.write_auth(_creds("sub-old", "old@x.com", refresh_token="rt-current"))
        self.mark_current("current")

        with (
            mock.patch.object(ga, "ensure_tool", return_value=True),
            mock.patch.object(ga.subprocess, "run", side_effect=KeyboardInterrupt),
        ):
            rc, _out, err = self.run_capture(ga.cmd_login_switch, "newacct")

        self.assertEqual(rc, 130)
        self.assertIn("Login cancelled", err)
        self.assertEqual(json.loads(current.read_text())["refresh_token"], "rt-current")
        self.assertFalse((self.home / "accounts" / "newacct.json").exists())


class SwitchCommandTests(_GeminiHomeMixin):
    def test_switch_restores_profile_backs_up_and_sets_active(self):
        self.write_profile("work", _creds("sub-w", "w@x.com", refresh_token="rt-work"))
        self.write_auth(_creds("sub-p", "p@x.com", refresh_token="rt-personal"))

        rc = self.run_quiet(ga.cmd_switch, "work")

        self.assertEqual(rc, 0)
        auth = json.loads((self.home / "oauth_creds.json").read_text())
        self.assertEqual(auth["refresh_token"], "rt-work")
        self.assertTrue((self.home / "oauth_creds.json.backup").is_file())
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )
        self.assertEqual(self.google_accounts()["active"], "w@x.com")

    def test_switch_unknown_profile_errors(self):
        rc = self.run_quiet(ga.cmd_switch, "ghost")
        self.assertEqual(rc, 1)

    def test_switch_folds_outgoing_rotation_back_into_its_profile(self):
        # The active account rotated its token since it was saved; switching
        # away must fold that rotation back so a later switch-back is fresh.
        self.write_profile(
            "personal", _creds("sub-p", "p@x.com", refresh_token="rt-stale")
        )
        self.write_profile("work", _creds("sub-w", "w@x.com", refresh_token="rt-work"))
        self.write_auth(_creds("sub-p", "p@x.com", refresh_token="rt-rotated"))
        self.mark_current("personal")

        self.run_quiet(ga.cmd_switch, "work")

        folded = json.loads((self.home / "accounts" / "personal.json").read_text())
        self.assertEqual(folded["refresh_token"], "rt-rotated")


class ListCommandTests(_GeminiHomeMixin):
    def test_list_marks_active_and_same_account(self):
        self.write_auth(_creds("sub-a", "a@x.com", refresh_token="rt-live"))
        self.write_profile(
            "active", _creds("sub-a", "a@x.com", refresh_token="rt-live")
        )
        self.write_profile("dupe", _creds("sub-a", "a@x.com", refresh_token="rt-dupe"))
        self.write_profile(
            "other", _creds("sub-b", "b@x.com", refresh_token="rt-other")
        )

        rc, out, _err = self.run_capture(lambda: ga.cmd_list(fetch_usage=False))

        self.assertEqual(rc, 0)
        text = ga._ANSI_RE.sub("", out)
        self.assertEqual(text.count("ACTIVE"), 1)
        self.assertEqual(text.count("SAME ACCT"), 1)
        self.assertIn("PRO USED", text)
        self.assertIn("FLASH USED", text)

    def test_list_without_profiles_is_ok(self):
        rc = self.run_quiet(ga.cmd_list)
        self.assertEqual(rc, 0)


class SyncCommandTests(_GeminiHomeMixin):
    def test_sync_copies_active_to_matching_profile(self):
        auth = self.write_auth(_creds("sub-w", "w@x.com", refresh_token="rt-live"))
        profile = self.write_profile(
            "work", _creds("sub-w", "w@x.com", refresh_token="rt-stale")
        )
        self.write_profile("other", _creds("sub-o", "o@x.com"))

        rc = self.run_quiet(ga.cmd_sync)

        self.assertEqual(rc, 0)
        self.assertEqual(profile.read_text(), auth.read_text())
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)

    def test_sync_without_match_errors(self):
        self.write_auth(_creds("sub-x", "x@x.com", refresh_token="rt-x"))
        self.write_profile("work", _creds("sub-w", "w@x.com", refresh_token="rt-w"))
        rc = self.run_quiet(ga.cmd_sync)
        self.assertEqual(rc, 1)

    def test_copy_refuses_cross_account_overwrite(self):
        dest = self.write_profile(
            "other", _creds("sub-other", "other@x.com", refresh_token="rt-other")
        )
        self.write_auth(_creds("sub-active", "active@x.com", refresh_token="rt-active"))
        self.run_quiet(ga._copy_active_auth_to, dest)
        self.assertEqual(json.loads(dest.read_text())["refresh_token"], "rt-other")


class RemoveCommandTests(_GeminiHomeMixin):
    def test_remove_current_profile_clears_marker(self):
        self.write_profile("work", _creds("sub-w", "w@x.com"))
        marker = self.mark_current("work")
        rc = self.run_quiet(ga.cmd_remove, "work")
        self.assertEqual(rc, 0)
        self.assertFalse(marker.exists())

    def test_remove_unknown_errors(self):
        rc = self.run_quiet(ga.cmd_remove, "ghost")
        self.assertEqual(rc, 1)


class RefreshCommandTests(_GeminiHomeMixin):
    def test_refresh_profile_writes_new_tokens_keeps_refresh_and_sets_expiry(self):
        profile = self.write_profile(
            "work", _creds("sub-w", "w@x.com", refresh_token="rt-keep")
        )
        new = {
            "access_token": "at-new",
            "id_token": _jwt({"email": "w@x.com", "sub": "sub-w"}),
            "expires_in": 3600,
        }
        with mock.patch.object(ga, "_oauth_refresh", return_value=(new, None)):
            rc = self.run_quiet(ga.cmd_refresh, "work")

        self.assertEqual(rc, 0)
        data = json.loads(profile.read_text())
        self.assertEqual(data["access_token"], "at-new")
        # Google omits refresh_token on refresh → the existing one is kept.
        self.assertEqual(data["refresh_token"], "rt-keep")
        self.assertAlmostEqual(
            data["expiry_date"], int((time.time() + 3600) * 1000), delta=5000
        )
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)

    def test_refresh_active_profile_also_updates_oauth_creds(self):
        self.write_profile("work", _creds("sub-w", "w@x.com", refresh_token="rt-w"))
        auth = self.write_auth(_creds("sub-w", "w@x.com", refresh_token="rt-w"))
        new = {"access_token": "at-new", "expires_in": 3600}
        with mock.patch.object(ga, "_oauth_refresh", return_value=(new, None)):
            self.run_quiet(ga.cmd_refresh, "work")
        self.assertEqual(json.loads(auth.read_text())["access_token"], "at-new")

    def test_refresh_other_profile_leaves_oauth_creds_alone(self):
        self.write_profile("work", _creds("sub-w", "w@x.com", refresh_token="rt-w"))
        auth = self.write_auth(_creds("sub-p", "p@x.com", refresh_token="rt-p"))
        before = auth.read_text()
        with mock.patch.object(
            ga, "_oauth_refresh", return_value=({"access_token": "x"}, None)
        ):
            self.run_quiet(ga.cmd_refresh, "work")
        self.assertEqual(auth.read_text(), before)

    def test_refresh_no_arg_refreshes_active_and_syncs_back(self):
        auth = self.write_auth(_creds("sub-w", "w@x.com", refresh_token="rt-w"))
        profile = self.write_profile(
            "work", _creds("sub-w", "w@x.com", refresh_token="rt-w")
        )
        new = {"access_token": "at-new", "expires_in": 3600}
        with mock.patch.object(ga, "_oauth_refresh", return_value=(new, None)):
            rc = self.run_quiet(ga.cmd_refresh, None)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(auth.read_text())["access_token"], "at-new")
        self.assertEqual(json.loads(profile.read_text())["access_token"], "at-new")

    def test_refresh_missing_refresh_token_errors(self):
        payload = _creds("sub-w", "w@x.com")
        del payload["refresh_token"]
        self.write_profile("work", payload)
        rc = self.run_quiet(ga.cmd_refresh, "work")
        self.assertEqual(rc, 1)

    def test_refresh_http_4xx_is_revoked_with_login_switch_hint(self):
        self.write_profile("work", _creds("sub-w", "w@x.com", refresh_token="rt-dead"))
        with mock.patch.object(
            ga, "_oauth_refresh", return_value=(None, "HTTP 401 from token endpoint")
        ):
            rc, _out, err = self.run_capture(ga.cmd_refresh, "work")
        self.assertEqual(rc, 1)
        self.assertIn("agy-accounts login-switch work", err)

    def test_refresh_transient_gets_retry_later(self):
        self.write_profile("work", _creds("sub-w", "w@x.com", refresh_token="rt-w"))
        with mock.patch.object(
            ga, "_oauth_refresh", return_value=(None, "network error: timed out")
        ):
            rc, _out, err = self.run_capture(ga.cmd_refresh, "work")
        self.assertEqual(rc, 1)
        self.assertIn("retry later", err.lower())
        self.assertNotIn("login-switch", err)

    def test_refresh_all_distinguishes_revoked_from_transient_and_continues(self):
        self.write_profile("dead", _creds("sub-d", "d@x.com", refresh_token="rt-dead"))
        self.write_profile(
            "flaky", _creds("sub-f", "f@x.com", refresh_token="rt-flaky")
        )
        self.write_profile("good", _creds("sub-g", "g@x.com", refresh_token="rt-good"))
        calls = []

        def fake_refresh(token):
            calls.append(token)
            if token == "rt-dead":
                return None, "HTTP 401 from token endpoint"
            if token == "rt-flaky":
                return None, "network error: timed out"
            return {"access_token": "at-new", "expires_in": 3600}, None

        with mock.patch.object(ga, "_oauth_refresh", side_effect=fake_refresh):
            rc, _out, err = self.run_capture(ga.cmd_refresh, "--all")

        self.assertEqual(rc, 1)
        self.assertEqual(sorted(calls), ["rt-dead", "rt-flaky", "rt-good"])
        self.assertIn("Revoked (re-login required)", err)
        self.assertIn("agy-accounts login-switch dead", err)
        self.assertIn("Transient failure, retry later", err)


if __name__ == "__main__":
    unittest.main()
