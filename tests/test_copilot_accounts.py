"""copilot-accounts profile store, --json shape, and quota parsing.

All credentials/identities here are placeholders (see CLAUDE.md): fake
`ghu_…` strings, `user@example.com`, and a canned quota document — nothing
copied from a real Copilot login. Every network call is patched out, so the
suite never touches api.github.com.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import copilot_accounts as ca
from ai_accounts import copilot_usage as cu
from ai_accounts._present import _ANSI_RE

_TOKEN = "ghu_fake1234567890abcdef"
_IDENTITY = {"login": "testuser", "email": "user@example.com", "id": 4242}

# Shape reported for GET https://api.github.com/copilot_internal/user — the
# unverified assumption copilot_usage is written against.
_QUOTA_JSON = {
    "copilot_plan": "individual",
    "quota_reset_date": "2099-01-01",
    "quota_snapshots": {
        "premium_interactions": {
            "entitlement": 300,
            "remaining": 75,
            "percent_remaining": 25,
            "unlimited": False,
        },
        "chat": {"unlimited": True, "entitlement": 0, "remaining": 0},
        "completions": {"entitlement": 100, "remaining": 40},
    },
}


_HOST = "https://github.com"


def _profile(token: str = _TOKEN) -> dict[str, object]:
    return {"oauth_token": token, "host": _HOST, **_IDENTITY}


class CopilotAccountsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.copilot_home = self.home / ".copilot"
        self.account_dir = self.home / ".ai-accounts" / "copilot" / "accounts"
        environment = mock.patch.dict(
            os.environ,
            {
                "COPILOT_HOME": str(self.copilot_home),
                "COPILOT_ACCOUNT_DIR": str(self.account_dir),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        for var in ca._ENV_VARS:
            os.environ.pop(var, None)
        self._install_fake_keychain()
        self._install_fake_identity()

    def _install_fake_keychain(self, *, writable: bool = True) -> None:
        """Stand in for the login keychain: without this the suite would write
        test tokens into the developer's real keychain item."""
        self.keychain: dict[tuple[str, str], str] = {}

        def fake_read(service: str, account: str) -> str | None:
            return self.keychain.get((service, account))

        def fake_write(service: str, account: str, secret: str) -> bool:
            if not writable:
                return False
            self.keychain[(service, account)] = secret
            return True

        for name, fake in (("keychain_read", fake_read), ("keychain_write", fake_write)):
            patcher = mock.patch.object(ca, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _install_fake_identity(self, identity: dict | None = _IDENTITY) -> None:
        patcher = mock.patch.object(ca, "_fetch_identity", return_value=identity)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sign_in(self, token: str = _TOKEN, *, login: str = "testuser", host: str = _HOST) -> Path:
        """Reproduce a real `/login`: config.json is JSONC naming the account
        (no token in it), and the token sits in the keyring under <host>:<login>."""
        path = self.copilot_home / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        user = {"host": host, "login": login}
        document = {"firstLaunchAt": "2026-01-01T00:00:00.000Z", "lastLoggedInUser": user, "loggedInUsers": [user]}
        path.write_text(
            "// User settings belong in settings.json.\n"
            "// This file is managed automatically.\n" + json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
        )
        self.keychain[(ca._KEYCHAIN_SERVICE, f"{host}:{login}")] = token
        return path

    # ── who / save ──────────────────────────────────────────────────────────

    def test_who_reports_not_logged_in_on_an_empty_store(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(ca.main(["who"]), 1)
        self.assertIn("Not logged in", _ANSI_RE.sub("", out.getvalue()))

    def test_save_reports_when_signed_out(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(ca.main(["save"]), 1)
        self.assertIn("No GitHub Copilot login found", _ANSI_RE.sub("", err.getvalue()))

    def test_save_reads_login_from_jsonc_config_and_token_from_keychain(self) -> None:
        self._sign_in()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ca.cmd_save("personal"), 0)
            self.assertEqual(ca.cmd_who(), 0)
        self.assertEqual(ca._read_json(self.account_dir / "personal.json"), _profile())

    def test_config_json_comment_header_is_tolerated_and_preserved(self) -> None:
        path = self._sign_in()
        self.assertEqual(ca._logged_in_user(), (_HOST, "testuser"))
        header, _ = ca._split_jsonc(path.read_text(encoding="utf-8"))
        self.assertTrue(header.startswith("// User settings"))

    def test_keychain_item_alone_is_not_a_login(self) -> None:
        # Without config.json naming the account there is no way to know which
        # keyring item is the live one — that reads as signed out, not as a guess.
        self.keychain[(ca._KEYCHAIN_SERVICE, f"{_HOST}:testuser")] = _TOKEN
        self.assertIsNone(ca._read_active())

    def test_env_token_is_the_last_resort_and_warns(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_env_token"}, clear=False):
            self.assertEqual(ca._read_active(), "ghp_env_token")
            err = io.StringIO()
            with redirect_stderr(err):
                ca._warn_env_shadow()
            self.assertIn("GH_TOKEN", _ANSI_RE.sub("", err.getvalue()))
            # The keyring login still wins over the exported one.
            self._sign_in()
            self.assertEqual(ca._read_active(), _TOKEN)

    def test_save_no_args_derives_the_name_from_the_github_login(self) -> None:
        self._sign_in()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ca.main(["save"]), 0)
        self.assertTrue((self.account_dir / "testuser.json").is_file())

    def test_save_no_args_falls_back_to_a_per_token_name(self) -> None:
        # No identity anywhere (env token only, /user offline): two accounts
        # must still not collide on one label.
        self._install_fake_identity(None)
        with mock.patch.dict(os.environ, {"GH_TOKEN": _TOKEN}, clear=False):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ca.main(["save"]), 0)
        first = ca._derived_name({"oauth_token": _TOKEN})
        self.assertTrue((self.account_dir / f"{first}.json").is_file())
        self.assertNotEqual(first, ca._derived_name({"oauth_token": "ghu_other"}))

    # ── switch / sync / remove ──────────────────────────────────────────────

    def test_switch_updates_both_the_config_file_and_the_keychain(self) -> None:
        self._sign_in("ghu_old_token", login="olduser")
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ca.cmd_switch("personal"), 0)

        self.assertEqual(ca._read_active(), _TOKEN)
        self.assertEqual(self.keychain[(ca._KEYCHAIN_SERVICE, f"{_HOST}:testuser")], _TOKEN)
        # the old account's item is left alone — the CLI can still switch back to it
        self.assertEqual(self.keychain[(ca._KEYCHAIN_SERVICE, f"{_HOST}:olduser")], "ghu_old_token")
        config = ca._read_json(self.copilot_home / "config.json")
        assert config is not None
        self.assertEqual(config["lastLoggedInUser"], {"host": _HOST, "login": "testuser"})
        self.assertIn({"host": _HOST, "login": "olduser"}, config["loggedInUsers"])
        self.assertIn({"host": _HOST, "login": "testuser"}, config["loggedInUsers"])
        # the CLI's own `//` header survives the rewrite
        raw = (self.copilot_home / "config.json").read_text(encoding="utf-8")
        self.assertTrue(raw.startswith("// User settings"))

    def test_switch_refuses_a_profile_without_a_login(self) -> None:
        # Pre-fix profiles carry only the token; the keyring item is keyed on
        # the login, so there is nothing to install — ask for a re-save, don't guess.
        self.assertTrue(ca._write_json(self.account_dir / "legacy.json", {"oauth_token": _TOKEN}))
        self._sign_in("ghu_old_token", login="olduser")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(ca.cmd_switch("legacy"), 1)
        self.assertIn("no GitHub login", _ANSI_RE.sub("", err.getvalue()))
        self.assertEqual(ca._read_active(), "ghu_old_token")  # live login untouched

    def test_switch_keeps_a_backup_of_the_replaced_token(self) -> None:
        self._sign_in("ghu_old_token")
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ca.cmd_switch("personal"), 0)
        self.assertTrue(list((self.account_dir.parent / "backups").glob("token.backup-*.json")))

    def test_switch_refuses_a_profile_with_an_empty_token(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "broken.json", {"oauth_token": ""}))
        self._sign_in()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(ca.cmd_switch("broken"), 1)
        self.assertIn("no Copilot token", _ANSI_RE.sub("", err.getvalue()))
        self.assertEqual(ca._read_active(), _TOKEN)  # live login untouched

    def test_write_active_never_persists_an_empty_secret(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(ca._write_active("   ", _HOST, "testuser"))
        self.assertIn("empty Copilot token", _ANSI_RE.sub("", err.getvalue()))
        self.assertEqual(self.keychain, {})
        self.assertFalse((self.copilot_home / "config.json").exists())

    def test_sync_copies_the_live_token_back_to_its_profile(self) -> None:
        # A profile saved before the identity lookup existed: sync must write
        # the live token without dropping what is already stored.
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        self._sign_in()
        (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(ca.cmd_sync(), 0)

        stored = ca._read_json(self.account_dir / "personal.json")
        assert stored is not None
        self.assertEqual(stored["oauth_token"], _TOKEN)
        self.assertEqual(stored["login"], "testuser")  # identity survives a sync

    def test_sync_refuses_when_the_live_token_matches_no_profile(self) -> None:
        # The token is the identity here (there is no stable account id in the
        # credential), so a rotated login is not attributable to a profile.
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")
        self._sign_in("ghu_rotated_token")

        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(ca.cmd_sync(), 1)
        self.assertIn("No unambiguous current profile", _ANSI_RE.sub("", err.getvalue()))

    def test_remove_no_args_opens_interactive_picker_and_removes(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        self.assertTrue(ca._write_json(self.account_dir / "work.json", _profile("ghu_work_token")))

        with mock.patch("builtins.input", return_value="2"):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = ca.cmd_remove_interactive()

        text = _ANSI_RE.sub("", out.getvalue())
        self.assertEqual(rc, 0)
        self.assertIn("1) personal", text)
        self.assertIn("2) work", text)
        self.assertFalse((self.account_dir / "work.json").is_file())
        self.assertTrue((self.account_dir / "personal.json").is_file())

    # ── list / usage ────────────────────────────────────────────────────────

    def test_list_reports_when_there_are_no_profiles(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(ca.main(["list"]), 0)
        self.assertIn("No saved Copilot profiles", _ANSI_RE.sub("", err.getvalue()))

    def test_list_never_prints_tokens(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        out = io.StringIO()
        with mock.patch.object(cu, "fetch_usage", return_value=cu.empty_usage()):
            with redirect_stdout(out):
                self.assertEqual(ca.cmd_list(), 0)
        listing = out.getvalue()
        self.assertIn("personal", listing)
        self.assertIn("testuser", listing)
        self.assertNotIn(_TOKEN, listing)
        self.assertNotIn("1234567890abcdef", listing)

    def test_list_without_fetch_usage_queries_nothing(self) -> None:
        # The shared `fetch_usage=False` fast path (same flag codex/claude/agy
        # expose) must render the table without touching the quota endpoint.
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        out = io.StringIO()
        with mock.patch.object(cu, "fetch_usage") as fetch:
            with redirect_stdout(out):
                self.assertEqual(ca.cmd_list(fetch_usage=False), 0)
        fetch.assert_not_called()
        self.assertIn("personal", _ANSI_RE.sub("", out.getvalue()))

    def test_list_identifies_active_profile_without_reading_keychain(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        self._sign_in()
        with mock.patch.object(ca, "keychain_read", side_effect=AssertionError("keychain read")):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(ca.cmd_list(fetch_usage=False), 0)
        self.assertIn("ACTIVE", _ANSI_RE.sub("", out.getvalue()))

    def test_list_uses_marker_to_disambiguate_profiles_for_the_same_login(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        self.assertTrue(ca._write_json(self.account_dir / "personal-old.json", _profile("ghu_old_token")))
        self._sign_in()
        with mock.patch.object(ca, "keychain_read", side_effect=AssertionError("keychain read")):
            self.assertIsNone(ca._listed_active_profile(ca._profiles()))
            (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(ca.cmd_list(fetch_usage=False, json_output=True), 0)
        rows = json.loads(out.getvalue())
        self.assertEqual([row["name"] for row in rows if row["active"]], ["personal"])

    def test_switch_warns_once_about_an_exported_token(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_env_token"}, clear=False):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertEqual(ca.cmd_switch("personal"), 0)
        self.assertEqual(_ANSI_RE.sub("", err.getvalue()).count("$GH_TOKEN is exported"), 1)

    def test_usage_shows_only_the_active_profile(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        self.assertTrue(ca._write_json(self.account_dir / "work.json", _profile("ghu_work_token")))
        self._sign_in()
        (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")

        out = io.StringIO()
        with mock.patch.object(cu, "fetch_usage", return_value=cu.empty_usage()):
            with redirect_stdout(out):
                self.assertEqual(ca.cmd_list(only_active=True), 0)
        text = _ANSI_RE.sub("", out.getvalue())
        self.assertIn("Current Copilot account", text)
        self.assertEqual(text.count("ACTIVE"), 1)
        self.assertIn("personal", text)
        self.assertNotIn("work", text)

    def test_usage_reports_when_no_active_profile(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "saved.json", _profile()))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(ca.cmd_list(only_active=True), 0)
        self.assertNotIn("PROFILE", _ANSI_RE.sub("", out.getvalue()))
        self.assertIn("No active Copilot account", _ANSI_RE.sub("", err.getvalue()))

    def test_list_json_round_trips_a_quota_reading(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        self._sign_in()
        (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")

        snapshot = cu.UsageSnapshot(
            premium=cu.UsageWindow(percentage=75, reset_time=4102416000, window_minutes=43200),
            chat=None,
            completions=None,
            plan="individual",
            refreshed_at=1700000000,
            error=None,
        )
        out = io.StringIO()
        with mock.patch.object(cu, "fetch_usage", return_value=snapshot):
            with redirect_stdout(out):
                self.assertEqual(ca.main(["list", "--json"]), 0)
        text = out.getvalue()
        self.assertNotIn("\033[", text)  # no ANSI in --json output
        self.assertEqual(
            json.loads(text),
            [
                {
                    "name": "personal",
                    "active": True,
                    "account": "testuser <user@example.com>",
                    "login": "testuser",
                    "email": "user@example.com",
                    "id": 4242,
                    "auth": "valid",
                    "usage": {
                        "monthly": {
                            "percent": 75,
                            "remaining_percent": 25,
                            "reset_time": 4102416000,
                            "window_minutes": 43200,
                        },
                        "unit": "requests",
                        "used": None,
                        "entitlement": None,
                        "remaining": None,
                        "unlimited": False,
                        "premium": {
                            "percent": 75,
                            "remaining_percent": 25,
                            "reset_time": 4102416000,
                            "window_minutes": 43200,
                        },
                        "chat": None,
                        "completions": None,
                        "plan": "individual",
                        "refreshed_at": 1700000000,
                        "error": None,
                    },
                    "no_quota_api": False,
                }
            ],
        )

    def test_list_json_degrades_to_no_quota_api_when_the_endpoint_fails(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        failed = cu.UsageSnapshot(None, None, None, None, None, "HTTP 404 from quota endpoint")
        out = io.StringIO()
        with mock.patch.object(cu, "fetch_usage", return_value=failed):
            with redirect_stdout(out):
                self.assertEqual(ca.main(["list", "--json"]), 0)
        self.assertEqual(
            json.loads(out.getvalue()),
            [{"name": "personal", "active": False, "usage": None, "no_quota_api": True,
              "account": "testuser <user@example.com>", "login": "testuser",
              "email": "user@example.com", "id": 4242, "auth": "valid"}],
        )

    def test_list_uses_credit_quota_and_live_identity_without_rewriting_profile(self) -> None:
        path = self.account_dir / "personal.json"
        self.assertTrue(ca._write_json(path, {"oauth_token": _TOKEN, "host": _HOST, "login": "testuser"}))
        original = path.read_bytes()
        self._install_fake_identity({**_IDENTITY, "name": "Test User"})
        with mock.patch.object(cu, "_request", return_value=_CREDIT_JSON):
            for only_active in (False, True):
                self._sign_in()
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(ca.cmd_list(only_active=only_active), 0)
                listing = _ANSI_RE.sub("", out.getvalue())
                for value in ("Test User <user@example.com>", "4242", "12/500 AIC", "487.5 AIC", "MONTH USED", "valid"):
                    self.assertIn(value, listing)
                self.assertNotIn("100%", listing)
                self.assertNotIn(_TOKEN, listing)
        self.assertEqual(path.read_bytes(), original)

    def test_usage_json_empty_array_when_no_active_profile(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "saved.json", _profile()))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ca.main(["usage", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), [])

    # ── misc dispatch ───────────────────────────────────────────────────────

    def test_login_switch_without_a_name_prints_usage(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(ca.main(["login-switch"]), 1)
        self.assertIn("login-switch <profile_name>", _ANSI_RE.sub("", err.getvalue()))

    def test_autoswitch_reports_unsupported_and_exits_zero(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(ca.main(["autoswitch"]), 0)
        self.assertEqual(
            _ANSI_RE.sub("", out.getvalue()).splitlines(),
            ["autoswitch unsupported for copilot: quota API unverified"],
        )
        self.assertEqual(err.getvalue(), "")

    def test_refresh_reports_a_rejected_token(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        self._install_fake_identity(None)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(ca.main(["refresh", "--all"]), 1)
        self.assertIn("re-login required", _ANSI_RE.sub("", err.getvalue()))


_CREDIT_JSON = {
    "copilot_plan": "individual",
    "token_based_billing": True,
    "quota_reset_date_utc": "2099-02-01T00:00:00Z",
    "quota_snapshots": {
        "chat": {"has_quota": True, "token_based_billing": True,
                 "credits_used": 12, "entitlement": 500, "remaining": 487,
                 "quota_remaining": 487.5, "percent_remaining": 97.5},
        "premium_interactions": {"has_quota": False, "entitlement": 0,
                                 "remaining": 0, "percent_remaining": 0},
    },
}


class CopilotUsageTests(unittest.TestCase):
    def _fetch(self, payload: object) -> cu.UsageSnapshot:
        with mock.patch.object(cu, "_request", return_value=payload):
            return cu.fetch_usage(_TOKEN)

    def test_parses_the_documented_quota_snapshot_shape(self) -> None:
        snapshot = self._fetch(_QUOTA_JSON)
        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.plan, "individual")
        assert snapshot.premium is not None
        self.assertEqual(snapshot.premium.percentage, 75)  # 25% remaining
        self.assertEqual(snapshot.premium.window_minutes, 30 * 24 * 60)
        self.assertEqual(snapshot.premium.reset_time, cu._reset_epoch("2099-01-01"))
        assert snapshot.chat is not None
        self.assertEqual(snapshot.chat.percentage, 0)  # unlimited
        assert snapshot.completions is not None
        self.assertEqual(snapshot.completions.percentage, 60)  # from raw counters

    def test_missing_token_is_an_error_not_a_crash(self) -> None:
        snapshot = cu.fetch_usage(None)
        self.assertEqual(snapshot.error, "missing token")
        self.assertIsNone(snapshot.premium)

    def test_credit_billing_uses_chat_and_preserves_fractional_balance(self) -> None:
        snapshot = self._fetch(_CREDIT_JSON)
        self.assertIsNone(snapshot.premium)
        self.assertEqual(snapshot.plan_usage, snapshot.chat)
        self.assertEqual(snapshot.used, 12)
        self.assertEqual(snapshot.entitlement, 500)
        self.assertEqual(snapshot.remaining, 487.5)
        self.assertEqual(snapshot.chat.reset_time, cu._reset_epoch("2099-02-01"))
        self.assertEqual(snapshot.chat.percentage, 2)

    def test_legacy_billing_keeps_premium_requests(self) -> None:
        snapshot = self._fetch(_QUOTA_JSON)
        self.assertEqual(snapshot.plan_usage, snapshot.premium)
        self.assertFalse(snapshot.token_based_billing)
        self.assertEqual(snapshot.used, 225)
        self.assertEqual(snapshot.remaining, 75)

    def test_unavailable_and_malformed_quotas_do_not_report_exhaustion(self) -> None:
        for quota in ({"has_quota": False, "percent_remaining": 0},
                      {"percent_remaining": float("nan")},
                      {"percent_remaining": float("inf")}):
            self.assertIsNone(cu._window(quota, None))
        snapshot = self._fetch({**_CREDIT_JSON, "quota_snapshots": {}})
        self.assertIsNone(snapshot.plan_usage)
        self.assertIsNone(snapshot.remaining)
        self.assertNotIn("0d", ca._usage_cell(cu.UsageWindow(20, None, 43200)))

    def test_primary_email_lookup_is_optional(self) -> None:
        for emails, expected in (([{"email": "user@example.com", "primary": True, "verified": True}], "user@example.com"),
                                 (None, None),
                                 ([{"email": "other@example.com", "primary": True, "verified": False}], None)):
            with mock.patch.object(ca, "_identity_request", side_effect=[{"login": "testuser"}, emails]):
                self.assertEqual(ca._fetch_identity(_TOKEN).get("email"), expected)

    def test_unexpected_shapes_degrade_instead_of_raising(self) -> None:
        for payload, expected in (
            ("HTTP 404 from quota endpoint", "HTTP 404 from quota endpoint"),
            ({"nothing": "useful"}, "unexpected quota shape"),
        ):
            with self.subTest(payload=payload):
                snapshot = self._fetch(payload)
                self.assertEqual(snapshot.error, expected)
                self.assertIsNone(snapshot.premium)

    def test_garbage_snapshot_entries_read_as_no_window(self) -> None:
        snapshot = self._fetch({"quota_snapshots": {"premium_interactions": "nope"}})
        self.assertIsNone(snapshot.error)
        self.assertIsNone(snapshot.premium)

    def test_format_refreshed_at_maps_errors_to_short_cells(self) -> None:
        self.assertEqual(
            cu.format_refreshed_at(cu.UsageSnapshot(None, None, None, None, None, "HTTP 404 x")),
            "ERR 404",
        )
