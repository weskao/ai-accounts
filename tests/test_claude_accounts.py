from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import claude_accounts as ca
from ai_accounts import claude_usage as cu


def _oauth(
    *,
    access: str = "at-1",
    refresh: str = "rt-1",
    expires_in_ms: int = 3600 * 1000,
    sub: str = "Max",
    tier: str = "default_claude_max_20x",
) -> dict:
    return {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": int(time.time() * 1000) + expires_in_ms,
        "scopes": ["user:profile", "user:inference"],
        "subscriptionType": sub,
        "rateLimitTier": tier,
    }


class EnvelopeTests(unittest.TestCase):
    def test_extract_reads_account_from_either_shape(self) -> None:
        oauth = _oauth()
        self.assertEqual(ca._extract_oauth({"mcpOAuth": {}, "claudeAiOauth": oauth}), oauth)
        self.assertEqual(ca._extract_oauth(oauth), oauth)  # bare blob
        self.assertIsNone(ca._extract_oauth({"nope": 1}))

    def test_inject_preserves_unrelated_keys(self) -> None:
        env = {"mcpOAuth": {"srv": {"token": "keep-me"}}, "claudeAiOauth": _oauth()}
        new = _oauth(access="at-2", refresh="rt-2")
        merged = ca._inject_oauth(json.dumps(env), new)
        self.assertEqual(merged["claudeAiOauth"], new)
        self.assertEqual(merged["mcpOAuth"], env["mcpOAuth"])

    def test_inject_replaces_a_bare_store_wholesale(self) -> None:
        new = _oauth(access="at-2")
        self.assertEqual(ca._inject_oauth(json.dumps(_oauth()), new), new)


class ClaimsTests(unittest.TestCase):
    def test_claims_expose_plan_and_millisecond_expiry(self) -> None:
        claims = ca._claims_from_oauth(_oauth(sub="Max", expires_in_ms=7_200_000))
        self.assertEqual(claims["plan"], "Max")
        self.assertTrue(claims["refreshable"])
        expiry = claims["expires_epoch"]
        self.assertIsInstance(expiry, int)
        self.assertAlmostEqual(int(expiry or 0), int(time.time()) + 7200, delta=5)

    def test_claims_lines_never_leak_tokens(self) -> None:
        oauth = _oauth(access="SECRET-ACCESS", refresh="SECRET-REFRESH")
        text = ca._ANSI_RE.sub("", "\n".join(ca._claims_lines(ca._claims_from_oauth(oauth))))
        self.assertIn("Max", text)
        self.assertNotIn("SECRET-ACCESS", text)
        self.assertNotIn("SECRET-REFRESH", text)

    def test_plan_cell_appends_rate_multiplier(self) -> None:
        # A Team seat rate-limited at max_5x reads as "team · 5x" so it is
        # distinguishable from, say, a Max 20x seat at a glance.
        claims = ca._claims_from_oauth(_oauth(sub="team", tier="default_claude_max_5x"))
        self.assertEqual(ca._plan_cell(claims), "team · 5x")
        big = ca._claims_from_oauth(_oauth(sub="Max", tier="default_claude_max_20x"))
        self.assertEqual(ca._plan_cell(big), "Max · 20x")

    def test_plan_cell_without_multiplier_shows_plan_only(self) -> None:
        claims = ca._claims_from_oauth(_oauth(sub="pro", tier="default_claude_pro"))
        self.assertEqual(ca._plan_cell(claims), "pro")

    def test_list_expiry_reports_refreshable_over_soon(self) -> None:
        # A near-expiry access token that is refreshable must not read as "soon":
        # Claude Code auto-renews it, so the list column mirrors the who-panel
        # (and agy-accounts) and reports the session as refreshable.
        soon = ca._claims_from_oauth(_oauth(expires_in_ms=60 * 1000))
        self.assertEqual(ca._list_expiry_status(soon), ("refreshable", ca.GREEN))
        # Without a refresh token the imminent access-token expiry still shows.
        text, color = ca._list_expiry_status({**soon, "refreshable": False})
        self.assertIn("soon", text)
        self.assertEqual(color, ca.YELLOW)


class StoragePathTests(unittest.TestCase):
    def test_default_storage_is_central_ai-accounts(self) -> None:
        # Profile store defaults OUTSIDE ~/.claude so a dotfiles repo of the
        # app dotdir can never accidentally commit OAuth token snapshots.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(ca.Path, "home", return_value=home),
            ):
                self.assertEqual(ca._claude_home(), home / ".claude")
                self.assertEqual(ca._account_dir(), home / ".ai-accounts" / "claude" / "accounts")
                self.assertEqual(ca._creds_file(), home / ".claude" / ".credentials.json")

    def test_legacy_store_migrates_to_central_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".claude" / "accounts"
            legacy.mkdir(parents=True)
            (legacy / "work.json").write_text("{}", encoding="utf-8")
            (legacy / ".current-profile").write_text("work", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(ca.Path, "home", return_value=home),
                redirect_stderr(io.StringIO()),
            ):
                moved = ca._account_dir()
            self.assertEqual(moved, home / ".ai-accounts" / "claude" / "accounts")
            self.assertTrue((moved / "work.json").is_file())
            self.assertEqual((moved / ".current-profile").read_text(), "work")
            self.assertFalse(legacy.exists())

    def test_config_dir_override(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/x/cfg"}, clear=True):
            self.assertEqual(ca._creds_file(), Path("/x/cfg/.credentials.json"))


class UsageParsingTests(unittest.TestCase):
    def test_parse_iso8601_with_fractional_seconds(self) -> None:
        self.assertIsInstance(cu._parse_iso8601("2025-07-20T10:30:00.000Z"), int)
        self.assertIsNone(cu._parse_iso8601("not-a-date"))
        self.assertIsNone(cu._parse_iso8601(None))

    def test_window_rounds_and_clamps_utilization(self) -> None:
        window = cu._window({"utilization": 60.0, "resets_at": "2025-07-27T00:00:00Z"}, 300)
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.percentage, 60)
        self.assertEqual(window.window_minutes, 300)
        self.assertEqual(cu._window({"utilization": 150}, 300).percentage, 100)  # type: ignore[union-attr]
        self.assertIsNone(cu._window({"no": "utilization"}, 300))

    def test_fetch_usage_without_token_reports_error(self) -> None:
        self.assertEqual(cu.fetch_usage(None).error, "missing access token")

    def test_fetch_usage_parses_both_windows(self) -> None:
        payload = {
            "five_hour": {"utilization": 45.0, "resets_at": "2025-07-20T10:30:00Z"},
            "seven_day": {"utilization": 12.0, "resets_at": "2025-07-27T00:00:00Z"},
        }
        with mock.patch.object(cu, "_request_usage", return_value=payload):
            snap = cu.fetch_usage("at", plan="Max")
        self.assertIsNone(snap.error)
        self.assertEqual(snap.plan, "Max")
        assert snap.five_hour is not None and snap.seven_day is not None
        self.assertEqual(snap.five_hour.percentage, 45)
        self.assertEqual(snap.seven_day.percentage, 12)

    def test_http_error_maps_to_short_label(self) -> None:
        snap = cu.UsageSnapshot(None, None, None, None, "HTTP 401 from usage endpoint")
        self.assertEqual(cu.format_refreshed_at(snap), "ERR 401")


class WireFormatTests(unittest.TestCase):
    """Guard the exact request shapes the API rejects with a 4xx if wrong — these
    values are mocked out of every other test, so a typo would otherwise ship green."""

    @staticmethod
    def _fake_response(payload: dict):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_oauth_refresh_posts_form_encoded_credentials(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return self._fake_response({"access_token": "new", "expires_in": 3600})

        with mock.patch.object(ca.urllib.request, "urlopen", side_effect=fake_urlopen):
            _, err = ca._oauth_refresh("rt-xyz")
        self.assertIsNone(err)
        req = captured["req"]
        self.assertEqual(req.full_url, ca._OAUTH_TOKEN_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.headers.get("Content-type"), "application/x-www-form-urlencoded")
        body = req.data.decode()
        self.assertIn("grant_type=refresh_token", body)
        self.assertIn("refresh_token=rt-xyz", body)
        self.assertIn(f"client_id={ca._OAUTH_CLIENT_ID}", body)

    def test_usage_request_sends_oauth_beta_header_and_bearer(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return self._fake_response({"five_hour": {"utilization": 1.0}})

        with mock.patch.object(cu.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = cu._request_usage("at-xyz", timeout=5)
        self.assertIsInstance(result, dict)
        req = captured["req"]
        self.assertEqual(req.full_url, cu.USAGE_URL)
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.headers.get("Anthropic-beta"), "oauth-2025-04-20")
        self.assertEqual(req.headers.get("Authorization"), "Bearer at-xyz")


class _HomeMixin(unittest.TestCase):
    home: Path = Path()
    keychain: str | None = None

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "claude"
        (self.home / "accounts").mkdir(parents=True)
        env = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_CONFIG_DIR": str(self.home),
                "CLAUDE_ACCOUNT_DIR": str(self.home / "accounts"),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CREDENTIALS_JSON", None)

        self.keychain = None
        acct = mock.patch.object(ca, "_keychain_account", return_value="tester")
        read = mock.patch.object(ca, "_read_keychain_creds", side_effect=lambda: self.keychain)
        write = mock.patch.object(ca, "_write_keychain_creds", side_effect=self._write_keychain)
        for patch in (acct, read, write):
            patch.start()
            self.addCleanup(patch.stop)

    def _write_keychain(self, content: str) -> bool:
        self.keychain = content
        return True

    def set_active(self, oauth: dict, *, mcp: bool = True) -> None:
        env: dict = {"claudeAiOauth": dict(oauth)}
        if mcp:
            env["mcpOAuth"] = {"srv": {"token": "keep-me"}}
        text = json.dumps(env)
        self.keychain = text
        ca._creds_file().write_text(text, encoding="utf-8")

    def active_oauth(self) -> dict | None:
        return ca._read_active_oauth()

    def write_profile(self, name: str, oauth: dict) -> Path:
        path = self.home / "accounts" / f"{name}.json"
        path.write_text(json.dumps(oauth), encoding="utf-8")
        return path

    def mark_current(self, name: str) -> None:
        (self.home / "accounts" / ".current-profile").write_text(name, encoding="utf-8")

    def quiet(self, function, *args) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return function(*args)

    def capture(self, function, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = function(*args)
        return result, out.getvalue(), err.getvalue()


class ProfileCommandTests(_HomeMixin):
    def test_save_captures_only_the_oauth_blob(self) -> None:
        self.set_active(_oauth(access="live", refresh="rt-live"))
        self.assertEqual(self.quiet(ca.cmd_save, "work"), 0)
        container = json.loads((self.home / "accounts" / "work.json").read_text())
        self.assertEqual(container["claudeAiOauth"]["refreshToken"], "rt-live")
        self.assertNotIn("mcpOAuth", container)  # profile is account-only
        self.assertEqual((self.home / "accounts" / ".current-profile").read_text(), "work")

    def test_save_without_credentials_errors(self) -> None:
        self.assertEqual(self.quiet(ca.cmd_save, "work"), 1)

    def test_switch_activates_profile_and_preserves_mcp_tokens(self) -> None:
        self.set_active(_oauth(access="at-a", refresh="rt-a"))
        self.write_profile("work", _oauth(access="at-w", refresh="rt-w"))
        self.assertEqual(self.quiet(ca.cmd_switch, "work"), 0)
        active = self.active_oauth()
        assert active is not None
        self.assertEqual(active["refreshToken"], "rt-w")
        # mcpOAuth in both stores survives the account swap.
        self.assertEqual(json.loads(self.keychain or "{}")["mcpOAuth"]["srv"]["token"], "keep-me")
        self.assertEqual(json.loads(ca._creds_file().read_text())["mcpOAuth"]["srv"]["token"], "keep-me")
        self.assertEqual((self.home / "accounts" / ".current-profile").read_text(), "work")

    def test_switch_folds_rotated_outgoing_token_into_profile(self) -> None:
        self.write_profile("old", _oauth(access="at-old", refresh="rt-old"))
        self.write_profile("new", _oauth(access="at-new", refresh="rt-new"))
        # Same refresh token (the match key) but a rotated access token.
        self.set_active(_oauth(access="at-rotated", refresh="rt-old"))
        self.mark_current("old")
        self.quiet(ca.cmd_switch, "new")
        old = ca._read_profile_oauth(self.home / "accounts" / "old.json")
        assert old is not None
        self.assertEqual(old["accessToken"], "at-rotated")

    def test_switch_missing_profile_errors(self) -> None:
        self.assertEqual(self.quiet(ca.cmd_switch, "ghost"), 1)

    def test_list_shows_usage_and_active_marker(self) -> None:
        oauth = _oauth(access="at-a", refresh="rt-a")
        self.write_profile("active", oauth)
        self.set_active(oauth)
        self.mark_current("active")
        snap = cu.UsageSnapshot(
            cu.UsageWindow(45, 2_000_000_000, 300),
            cu.UsageWindow(12, 2_000_000_000, 10080),
            "Max",
            2_000_000_000,
            None,
        )
        with mock.patch.object(ca.claude_usage, "fetch_usage", return_value=snap):
            result, output, _ = self.capture(ca.cmd_list)
        text = ca._ANSI_RE.sub("", output)
        self.assertEqual(result, 0)
        self.assertIn("PLAN", text)
        self.assertIn("Max", text)
        self.assertIn("5H USED", text)
        self.assertIn("1W USED", text)
        self.assertEqual(text.count("ACTIVE"), 1)

    def test_list_hides_usage_columns_when_all_empty(self) -> None:
        self.write_profile("a", _oauth(access="at-a", refresh="rt-a"))
        empty = cu.UsageSnapshot(None, None, None, None, "HTTP 401 from usage endpoint")
        with mock.patch.object(ca.claude_usage, "fetch_usage", return_value=empty):
            _, output, _ = self.capture(ca.cmd_list)
        text = ca._ANSI_RE.sub("", output)
        self.assertNotIn("5H USED", text)
        self.assertNotIn("1W USED", text)

    def test_list_shows_account_from_stored_identity(self) -> None:
        path = self.home / "accounts" / "work.json"
        path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": _oauth(access="at-a", refresh="rt-a"),
                    "ai-accountsAccount": {"email": "wes@example.com", "name": "Wes"},
                }
            ),
            encoding="utf-8",
        )
        empty = cu.UsageSnapshot(None, None, None, None, "HTTP 401")
        with mock.patch.object(ca.claude_usage, "fetch_usage", return_value=empty):
            _, output, _ = self.capture(ca.cmd_list)
        text = ca._ANSI_RE.sub("", output)
        self.assertIn("ACCOUNT", text)
        self.assertIn("Wes <wes@example.com>", text)

    def test_save_snapshots_identity_from_config_json(self) -> None:
        self.set_active(_oauth(access="live", refresh="rt-live"))
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "a@b.co", "displayName": "AB"}}),
            encoding="utf-8",
        )
        self.assertEqual(self.quiet(ca.cmd_save, "work"), 0)
        stored = ca._read_profile_identity(self.home / "accounts" / "work.json")
        self.assertEqual(stored, {"email": "a@b.co", "name": "AB"})

    def test_remove_current_profile_clears_marker(self) -> None:
        self.write_profile("work", _oauth())
        self.mark_current("work")
        self.assertEqual(self.quiet(ca.cmd_remove, "work"), 0)
        self.assertFalse((self.home / "accounts" / ".current-profile").exists())

    def test_sync_writes_rotated_active_back_to_profile(self) -> None:
        profile = self.write_profile("work", _oauth(access="at-old", refresh="rt-1"))
        self.set_active(_oauth(access="at-live", refresh="rt-1"))
        self.mark_current("work")
        self.assertEqual(self.quiet(ca.cmd_sync), 0)
        self.assertEqual(ca._read_profile_oauth(profile)["accessToken"], "at-live")


class RefreshTests(_HomeMixin):
    def test_refresh_profile_rotates_and_saves_tokens(self) -> None:
        profile = self.write_profile("work", _oauth(access="old", refresh="rt-1"))
        refreshed = {"access_token": "rotated", "refresh_token": "rt-2", "expires_in": 3600}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(refreshed, None)):
            self.assertEqual(self.quiet(ca.cmd_refresh, "work"), 0)
        saved = ca._read_profile_oauth(profile)
        assert saved is not None
        self.assertEqual(saved["accessToken"], "rotated")
        self.assertEqual(saved["refreshToken"], "rt-2")

    def test_refresh_active_syncs_into_matching_profile(self) -> None:
        oauth = _oauth(access="old", refresh="rt-1")
        profile = self.write_profile("work", oauth)
        self.set_active(oauth)
        self.mark_current("work")
        refreshed = {"access_token": "rotated", "expires_in": 3600}  # endpoint reuses refresh token
        with mock.patch.object(ca, "_oauth_refresh", return_value=(refreshed, None)):
            self.assertEqual(self.quiet(ca.cmd_refresh, None), 0)
        self.assertEqual(ca._read_profile_oauth(profile)["accessToken"], "rotated")
        active = self.active_oauth()
        assert active is not None
        self.assertEqual(active["accessToken"], "rotated")

    def test_refresh_revoked_token_fails(self) -> None:
        self.write_profile("work", _oauth(refresh="rt-dead"))
        with mock.patch.object(ca, "_oauth_refresh", return_value=(None, "revoked: rejected")):
            self.assertEqual(self.quiet(ca.cmd_refresh, "work"), 1)


class LoginSwitchTests(_HomeMixin):
    def test_login_switch_saves_the_new_account(self) -> None:
        fresh = _oauth(access="at-new", refresh="rt-new")

        def run_login(*args, **kwargs):
            self.set_active(fresh)  # simulate `claude auth login` writing new creds
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(ca, "ensure_tool", return_value=True),
            mock.patch.object(ca.subprocess, "run", side_effect=run_login) as run,
        ):
            self.assertEqual(self.quiet(ca.cmd_login_switch, "new"), 0)
        self.assertEqual(run.call_args.args[0], ["claude", "auth", "login"])
        saved = ca._read_profile_oauth(self.home / "accounts" / "new.json")
        assert saved is not None
        self.assertEqual(saved["refreshToken"], "rt-new")

    def _config_account(self, email: str, name: str) -> None:
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": email, "displayName": name}}),
            encoding="utf-8",
        )

    def test_login_switch_stores_identity_when_config_refreshes(self) -> None:
        self._config_account("old@x.co", "Old")  # outgoing account
        fresh = _oauth(access="at-new", refresh="rt-new")

        def run_login(*args, **kwargs):
            self.set_active(fresh)
            self._config_account("new@x.co", "New")  # login refreshed the config
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(ca, "ensure_tool", return_value=True),
            mock.patch.object(ca.subprocess, "run", side_effect=run_login),
        ):
            self.assertEqual(self.quiet(ca.cmd_login_switch, "new"), 0)
        stored = ca._read_profile_identity(self.home / "accounts" / "new.json")
        self.assertEqual(stored, {"email": "new@x.co", "name": "New"})

    def test_login_switch_skips_stale_config_identity(self) -> None:
        self._config_account("old@x.co", "Old")  # config never updates during login
        fresh = _oauth(access="at-new", refresh="rt-new")

        def run_login(*args, **kwargs):
            self.set_active(fresh)  # new token, but oauthAccount stays stale
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(ca, "ensure_tool", return_value=True),
            mock.patch.object(ca.subprocess, "run", side_effect=run_login),
        ):
            self.assertEqual(self.quiet(ca.cmd_login_switch, "new"), 0)
        # The stale outgoing email must NOT be attributed to the new profile.
        self.assertIsNone(ca._read_profile_identity(self.home / "accounts" / "new.json"))

    def test_cancelled_login_restores_previous_session(self) -> None:
        self.set_active(_oauth(access="at-old", refresh="rt-old"))
        with (
            mock.patch.object(ca, "ensure_tool", return_value=True),
            mock.patch.object(ca.subprocess, "run", return_value=mock.Mock(returncode=1)),
        ):
            self.assertEqual(self.quiet(ca.cmd_login_switch, "new"), 1)
        active = self.active_oauth()
        assert active is not None
        self.assertEqual(active["refreshToken"], "rt-old")
        self.assertFalse((self.home / "accounts" / "new.json").exists())


if __name__ == "__main__":
    unittest.main()
