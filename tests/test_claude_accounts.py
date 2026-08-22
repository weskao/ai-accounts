from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import claude_accounts as ca
from ai_accounts import claude_usage as cu



# Env vars a running Claude Code session exports into every command it spawns.
# The suite itself may well be running inside one — which is exactly the case
# the resume exclusion must refuse.
_CLAUDE_SESSION_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")


class _TtyStringIO(io.StringIO):
    """A capture buffer that reports itself as a terminal.

    ``cmd_autoswitch`` reads ``sys.stdout.isatty()`` to decide whether there is
    a session to restart into; a plain StringIO is correctly seen as unattended.
    """

    def isatty(self) -> bool:
        return True

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
        # A Team seat rate-limited at max_5x reads as "Team · 5x" so it is
        # distinguishable from, say, a Max 20x seat at a glance.
        claims = ca._claims_from_oauth(_oauth(sub="team", tier="default_claude_max_5x"))
        self.assertEqual(ca._plan_cell(claims), "Team · 5x")
        big = ca._claims_from_oauth(_oauth(sub="Max", tier="default_claude_max_20x"))
        self.assertEqual(ca._plan_cell(big), "Max · 20x")

    def test_plan_cell_without_multiplier_shows_plan_only(self) -> None:
        claims = ca._claims_from_oauth(_oauth(sub="pro", tier="default_claude_pro"))
        self.assertEqual(ca._plan_cell(claims), "Pro")

    def test_claims_lines_surface_refresh_token_expiry_with_color_by_recency(self) -> None:
        # Given: a refresh token that expires in the future
        future_ms = int(time.time() * 1000) + 3600_000
        oauth = _oauth()
        oauth["refreshTokenExpiresAt"] = future_ms
        lines = ca._claims_lines(ca._claims_from_oauth(oauth))
        future_line = next(line for line in lines if "Refreshable until" in line)
        # Then: the line is present and not colored red (not yet expired)
        self.assertNotIn(ca.RED, future_line)

        # Given: a refresh token that already expired
        past_ms = int(time.time() * 1000) - 3600_000
        oauth["refreshTokenExpiresAt"] = past_ms
        lines = ca._claims_lines(ca._claims_from_oauth(oauth))
        past_line = next(line for line in lines if "Refreshable until" in line)
        # Then: flagged red — this is the date the account actually dies on
        self.assertIn(ca.RED, past_line)

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
    def test_default_storage_is_central_ai_accounts(self) -> None:
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
        with (
            mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/x/cfg"}, clear=True),
            mock.patch.object(ca.Path, "home", side_effect=AssertionError("home queried")),
        ):
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

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
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
        # Must carry a real User-Agent — the default Python-urllib UA is blocked
        # by Cloudflare (error 1010) before the request reaches the endpoint.
        ua = req.headers.get("User-agent", "")
        self.assertTrue(ua and "python-urllib" not in ua.lower(), ua)

    def test_oauth_refresh_bare_403_is_edge_block_not_revoked(self) -> None:
        # A 403 with no OAuth error body is a Cloudflare/WAF block before the
        # endpoint — transient, NOT a revoked token (which would be relogin).
        err = urllib.error.HTTPError(
            ca._OAUTH_TOKEN_URL, 403, "Forbidden", {},
            io.BytesIO(b'{"error_name":"browser_signature_banned"}'),
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            _, msg = ca._oauth_refresh("rt-xyz")
        self.assertFalse(ca._is_revoked_error(msg), msg)

    def test_oauth_refresh_403_invalid_grant_is_revoked(self) -> None:
        err = urllib.error.HTTPError(
            ca._OAUTH_TOKEN_URL, 403, "Forbidden", {},
            io.BytesIO(b'{"error":"invalid_grant"}'),
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            _, msg = ca._oauth_refresh("rt-xyz")
        self.assertTrue(ca._is_revoked_error(msg), msg)

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
        # Far-future expiry so the switch's in-place refresh doesn't fire — this
        # test is about mcp-token preservation, not token refresh.
        self.write_profile("work", _oauth(access="at-w", refresh="rt-w", expires_in_ms=30 * 24 * 3600 * 1000))
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
        self.write_profile(
            "new",
            _oauth(
                access="at-new",
                refresh="rt-new",
                expires_in_ms=30 * 24 * 3600 * 1000,
            ),
        )
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
                    "aiAccountsAccount": {"email": "wes@example.com", "name": "Wes"},
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

    def test_usage_shows_only_active_profile(self) -> None:
        active = _oauth(access="at-a", refresh="rt-a")
        self.write_profile("active", active)
        self.write_profile("other", _oauth(access="at-b", refresh="rt-b"))
        self.set_active(active)
        self.mark_current("active")
        snap = cu.UsageSnapshot(
            cu.UsageWindow(45, 2_000_000_000, 300),
            cu.UsageWindow(12, 2_000_000_000, 10080),
            "Max",
            2_000_000_000,
            None,
        )
        with mock.patch.object(ca.claude_usage, "fetch_usage", return_value=snap) as fetch:
            result, output, _ = self.capture(lambda: ca.cmd_list(only_active=True))
        text = ca._ANSI_RE.sub("", output)
        self.assertEqual(result, 0)
        self.assertEqual(fetch.call_count, 1)  # only the active account is queried
        self.assertIn("Current Claude account", text)
        self.assertEqual(text.count("ACTIVE"), 1)
        self.assertNotIn("other", text)

    def test_usage_reports_when_no_active_profile(self) -> None:
        self.write_profile("saved", _oauth(access="at-a", refresh="rt-a"))
        result, output, err = self.capture(lambda: ca.cmd_list(only_active=True))
        text = ca._ANSI_RE.sub("", output + err)
        self.assertEqual(result, 0)
        self.assertNotIn("PROFILE", text)  # no table rendered
        self.assertIn("No active Claude account", text)

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

    def test_save_no_args_derives_name_from_active_email(self) -> None:
        self.set_active(_oauth(access="live", refresh="rt-live"))
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "user@example.com", "displayName": "User"}}),
            encoding="utf-8",
        )
        self.assertEqual(self.quiet(ca.cmd_save), 0)
        self.assertTrue((self.home / "accounts" / "user.json").is_file())
        self.assertEqual((self.home / "accounts" / ".current-profile").read_text(), "user")

    def test_save_no_args_without_email_fails_with_no_profile_created(self) -> None:
        self.set_active(_oauth(access="live", refresh="rt-live"))
        # No ~/.claude.json identity at all — no email to derive a name from.
        result, _out, err = self.capture(ca.cmd_save)
        self.assertEqual(result, 1)
        self.assertIn("Could not derive a name", err)
        self.assertIn("pass a name explicitly", err)
        self.assertEqual(list((self.home / "accounts").glob("*.json")), [])

    def test_remove_interactive_picker_removes_selected_profile(self) -> None:
        self.write_profile("work", _oauth())
        self.write_profile("personal", _oauth())
        # Profiles are sorted alphabetically ("personal", "work"), so "1" is personal.
        with mock.patch("builtins.input", return_value="1"):
            result, output, _err = self.capture(ca.cmd_remove_interactive)
        text = ca._ANSI_RE.sub("", output)
        self.assertEqual(result, 0)
        self.assertIn("work", text)
        self.assertIn("personal", text)
        self.assertFalse((self.home / "accounts" / "personal.json").exists())
        self.assertTrue((self.home / "accounts" / "work.json").exists())

    def test_remove_interactive_with_no_saved_profiles(self) -> None:
        result, _out, err = self.capture(ca.cmd_remove_interactive)
        self.assertEqual(result, 1)
        self.assertIn("No saved Claude profiles", err)

    def test_help_alias_prints_help(self) -> None:
        result, output, _err = self.capture(lambda: ca.main(["help"]))
        self.assertEqual(result, 0)
        self.assertIn("USAGE", output)


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


class MainDispatchTests(_HomeMixin):
    def test_main_routes_autoswitch(self):
        # Given: cmd_autoswitch is the handler for the "autoswitch" subcommand
        # When: main() dispatches "autoswitch"
        with mock.patch.object(ca, "cmd_autoswitch", return_value=0) as auto:
            rc = ca.main(["autoswitch"])
        # Then: it delegates to cmd_autoswitch (not the unknown-command fallback)
        auto.assert_called_once_with()
        self.assertEqual(rc, 0)


class AutoswitchCommandTests(_HomeMixin):
    """cmd_autoswitch supplies claude-specific data to the shared engine."""

    def setUp(self):
        super().setUp()
        self.aw_config = self.home / "config.json"
        env = mock.patch.dict(
            os.environ, {"AI_ACCOUNTS_CONFIG_JSON": str(self.aw_config)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)

    def _enable_autoswitch(self, **overrides) -> None:
        from ai_accounts import autoswitch as aw

        cfg = {"enabled": True, "notify": "none", "switch_when_used_pct": 90}
        cfg.update(overrides)
        aw.save_config(cfg)

    @staticmethod
    def _snapshot(used_pct=None, error=None) -> cu.UsageSnapshot:
        """A snapshot reporting *used_pct* in BOTH quota windows.

        Which window the trigger reads is a config choice (``switch_window``),
        so a fixture that filled only one would make every test here pass or
        fail on that setting instead of on the behaviour under test.
        """
        def window(minutes: int) -> cu.UsageWindow | None:
            if used_pct is None:
                return None
            return cu.UsageWindow(
                percentage=used_pct, reset_time=None, window_minutes=minutes
            )

        return cu.UsageSnapshot(
            five_hour=window(300),
            seven_day=window(7 * 24 * 60),
            plan="max",
            refreshed_at=1,
            error=error,
        )

    def test_the_weekly_window_decides_when_the_5h_window_is_unavailable(self):
        # Given: no 5-hour figure (the usage table renders it "—") while the
        # weekly quota is 93% spent, and a spare account is fresh. Default
        # config: no `switch_window` written.
        self._enable_autoswitch()
        self.set_active(_oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("alpha", _oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("beta", _oauth(access="at-beta", refresh="rt-beta"))
        self.mark_current("alpha")

        def fake_fetch(access_token, *, plan=None, timeout=20):
            pct = 93 if access_token == "at-alpha" else 5
            return cu.UsageSnapshot(
                five_hour=None,
                seven_day=cu.UsageWindow(
                    percentage=pct, reset_time=None, window_minutes=7 * 24 * 60
                ),
                plan="max",
                refreshed_at=1,
                error=None,
            )

        # When: cmd_autoswitch runs
        with mock.patch.object(ca.claude_usage, "fetch_usage", side_effect=fake_fetch), \
                mock.patch.object(ca, "cmd_switch", return_value=0) as switch, \
                mock.patch.object(ca, "_autoswitch_resume", return_value=False):
            rc, out, err = self.capture(ca.cmd_autoswitch)

        # Then: it switches on the weekly figure instead of reporting "could
        # not determine usage" because only the 5-hour window was missing
        self.assertEqual(rc, 0)
        switch.assert_called_once_with("beta")
        self.assertNotIn("Could not determine usage", out + err)

    def test_candidates_are_probed_via_their_own_token_never_the_live_creds(self):
        # Given: two profiles, both at/above the switch threshold (no qualifying candidate)
        self._enable_autoswitch()
        self.set_active(_oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("alpha", _oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("beta", _oauth(access="at-beta", refresh="rt-beta"))
        self.mark_current("alpha")

        probed: list[str] = []

        def fake_fetch(access_token, *, plan=None, timeout=20):
            probed.append(access_token)
            return self._snapshot(95 if access_token == "at-alpha" else 92)

        with mock.patch.object(ca.claude_usage, "fetch_usage", side_effect=fake_fetch), \
                mock.patch.object(ca, "cmd_switch") as switch, \
                mock.patch.object(ca, "_write_active_oauth") as write_active:
            rc = self.quiet(ca.cmd_autoswitch)

        # Then: only the profiles' OWN access tokens were probed — never the live
        # credentials file/keychain was written during probing (no switch triggered)
        self.assertEqual(rc, 0)
        self.assertEqual(set(probed), {"at-alpha", "at-beta"})
        switch.assert_not_called()
        write_active.assert_not_called()

    def test_a_candidate_probe_error_is_skipped_without_crashing(self):
        # Given: active is over threshold; beta errors (401) on probe, gamma qualifies
        self._enable_autoswitch()
        self.set_active(_oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("alpha", _oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("beta", _oauth(access="at-beta", refresh="rt-beta"))
        self.write_profile("gamma", _oauth(access="at-gamma", refresh="rt-gamma"))
        self.mark_current("alpha")

        def fake_fetch(access_token, *, plan=None, timeout=20):
            if access_token == "at-alpha":
                return self._snapshot(95)
            if access_token == "at-beta":
                return self._snapshot(error="HTTP 401 from usage endpoint")
            return self._snapshot(10)  # gamma: genuinely below threshold

        # When: cmd_autoswitch runs
        with mock.patch.object(ca.claude_usage, "fetch_usage", side_effect=fake_fetch), \
                mock.patch.object(ca, "cmd_switch", return_value=0) as switch:
            rc = self.quiet(ca.cmd_autoswitch)

        # Then: it does not crash, skips the errored candidate, and still finds gamma
        # (this also proves the triggered switch delegates to cmd_switch by name)
        self.assertEqual(rc, 0)
        switch.assert_called_once_with("gamma")

    def test_disabled_config_is_a_clear_no_op_with_zero_probes(self):
        # Given: auto-switch is NOT enabled (default config — nothing written)
        self.set_active(_oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("alpha", _oauth(access="at-alpha", refresh="rt-alpha"))
        self.mark_current("alpha")

        # When: cmd_autoswitch runs
        with mock.patch.object(ca.claude_usage, "fetch_usage") as fetch:
            rc, out, err = self.capture(ca.cmd_autoswitch)

        # Then: no usage probe ever happens, and the user sees a clear reason why
        self.assertEqual(rc, 0)
        fetch.assert_not_called()
        self.assertIn("Auto-switch is disabled", out + err)

    def test_a_switch_is_rendered_readably_naming_both_profiles(self):
        # Given: active is over threshold and a clear lower-usage candidate exists
        self._enable_autoswitch()
        self.set_active(_oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("alpha", _oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("beta", _oauth(access="at-beta", refresh="rt-beta"))
        self.mark_current("alpha")

        def fake_fetch(access_token, *, plan=None, timeout=20):
            return self._snapshot(95 if access_token == "at-alpha" else 10)

        # When: cmd_autoswitch runs
        with mock.patch.object(ca.claude_usage, "fetch_usage", side_effect=fake_fetch), \
                mock.patch.object(ca, "cmd_switch", return_value=0):
            rc, out, err = self.capture(ca.cmd_autoswitch)

        # Then: the user sees both profile names, readably, not just a bare reason code
        self.assertEqual(rc, 0)
        combined = out + err
        self.assertIn("alpha", combined)
        self.assertIn("beta", combined)
        self.assertNotEqual(combined.strip(), "switched")

    def test_no_candidate_is_rendered_readably_naming_the_active_profile(self):
        # Given: active is over threshold and every other profile is too
        self._enable_autoswitch()
        self.set_active(_oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("alpha", _oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("beta", _oauth(access="at-beta", refresh="rt-beta"))
        self.mark_current("alpha")

        # When: cmd_autoswitch runs
        with mock.patch.object(ca.claude_usage, "fetch_usage", return_value=self._snapshot(95)):
            rc, out, err = self.capture(ca.cmd_autoswitch)

        # Then: the user sees a readable "no account to switch to" explanation
        self.assertEqual(rc, 0)
        combined = out + err
        self.assertIn("alpha", combined)
        self.assertNotEqual(combined.strip(), "no_candidate")


    # ── restart ladder ───────────────────────────────────────────────────────

    def _given_a_switch_is_due(self, *, inside_claude: str | None = None) -> None:
        """alpha active and exhausted, beta a clear lower-usage candidate.

        *inside_claude* names the Claude Code env marker to leave set; by
        default every marker is cleared, so the run counts as "not launched
        from inside a claude session".
        """
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for marker in _CLAUDE_SESSION_MARKERS:
            os.environ.pop(marker, None)
        if inside_claude is not None:
            os.environ[inside_claude] = "1"
        self._enable_autoswitch()
        self.set_active(_oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("alpha", _oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("beta", _oauth(access="at-beta", refresh="rt-beta"))
        self.mark_current("alpha")

    def _fetch_alpha_exhausted(self, access_token, *, plan=None, timeout=20):
        return self._snapshot(95 if access_token == "at-alpha" else 10)

    def _autoswitch_on_a_tty(self, fake_run) -> tuple[int, str]:
        """cmd_autoswitch with a stdout that is a terminal and *fake_run* for spawns."""
        out, err = _TtyStringIO(), io.StringIO()
        with mock.patch.object(ca.claude_usage, "fetch_usage", side_effect=self._fetch_alpha_exhausted), \
                mock.patch.object(ca, "cmd_switch", return_value=0), \
                mock.patch.object(ca, "have", return_value=True), \
                mock.patch.object(ca.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(out), redirect_stderr(err):
            rc = ca.cmd_autoswitch()
        return rc, out.getvalue() + err.getvalue()

    def test_an_interactive_switch_resumes_the_conversation_in_a_new_process(self):
        # Given: a switch is due, on a terminal, not from inside a claude session
        self._given_a_switch_is_due()
        spawned: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            spawned.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        # When: cmd_autoswitch runs
        rc, _ = self._autoswitch_on_a_tty(fake_run)

        # Then: exactly one NEW claude session was started to continue the most
        # recent conversation — nothing was signalled, killed or restarted
        self.assertEqual(rc, 0)
        self.assertEqual(spawned, [["claude", "--continue"]])

    def test_a_switch_driven_from_inside_a_claude_session_never_resumes_itself(self):
        # Given: the same switch, but this command was launched from inside a
        # running Claude Code session — `claude --continue` would target THAT
        # session (docs/autoswitch-hot-reload-spike.md, claude section)
        for marker in _CLAUDE_SESSION_MARKERS:
            with self.subTest(marker=marker):
                self.setUp()
                self._given_a_switch_is_due(inside_claude=marker)
                spawned: list[list[str]] = []

                def fake_run(cmd, **kwargs):
                    spawned.append(list(cmd))
                    return subprocess.CompletedProcess(args=cmd, returncode=0)

                # When: cmd_autoswitch runs on a terminal
                rc, report = self._autoswitch_on_a_tty(fake_run)

                # Then: nothing is spawned at all, the switch still succeeds,
                # and the user is told to restart by hand
                self.assertEqual(rc, 0)
                self.assertEqual(spawned, [])
                self.assertIn("restart your session", report)

    def test_an_unattended_switch_spawns_nothing_and_says_to_restart_by_hand(self):
        # Given: a switch is due, but nothing is attached to a terminal
        self._given_a_switch_is_due()

        # When: cmd_autoswitch runs (capture's plain StringIO is not a tty)
        with mock.patch.object(ca.claude_usage, "fetch_usage", side_effect=self._fetch_alpha_exhausted), \
                mock.patch.object(ca, "cmd_switch", return_value=0), \
                mock.patch.object(ca, "have", return_value=True), \
                mock.patch.object(ca.subprocess, "run") as spawn:
            rc, out, err = self.capture(ca.cmd_autoswitch)

        # Then: a background poll spawns NOTHING, and says so in words
        self.assertEqual(rc, 0)
        spawn.assert_not_called()
        self.assertIn("restart your session", out + err)

    def test_a_failed_resume_does_not_turn_a_successful_switch_into_a_failure(self):
        # Given: a switch is due on a terminal, but the resume command fails
        self._given_a_switch_is_due()

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=1)

        # When: cmd_autoswitch runs
        rc, report = self._autoswitch_on_a_tty(fake_run)

        # Then: the switch already happened, so it still reports success — with
        # a fallback telling the user to restart manually
        self.assertEqual(rc, 0)
        self.assertIn("alpha", report)
        self.assertIn("beta", report)
        self.assertIn("restart your session", report)

    def test_a_candidate_reporting_an_error_is_skipped_even_when_it_has_a_window(self):
        # Given: beta's snapshot carries BOTH a probe error and a usage window
        # low enough to make it the emptiest candidate; gamma is honestly free
        self._enable_autoswitch()
        self.set_active(_oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("alpha", _oauth(access="at-alpha", refresh="rt-alpha"))
        self.write_profile("beta", _oauth(access="at-beta", refresh="rt-beta"))
        self.write_profile("gamma", _oauth(access="at-gamma", refresh="rt-gamma"))
        self.mark_current("alpha")

        def fake_fetch(access_token, *, plan=None, timeout=20):
            if access_token == "at-alpha":
                return self._snapshot(95)
            if access_token == "at-beta":
                return self._snapshot(10, error="HTTP 500 from usage endpoint")
            return self._snapshot(20)

        # When: cmd_autoswitch runs
        with mock.patch.object(ca.claude_usage, "fetch_usage", side_effect=fake_fetch), \
                mock.patch.object(ca, "cmd_switch", return_value=0) as switch:
            rc = self.quiet(ca.cmd_autoswitch)

        # Then: the errored candidate is skipped on the strength of its error
        # alone — a window that came back with an error is not trustworthy data
        self.assertEqual(rc, 0)
        switch.assert_called_once_with("gamma")


if __name__ == "__main__":
    unittest.main()
