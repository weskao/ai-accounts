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


def _profile(token: str = _TOKEN) -> dict[str, object]:
    return {"oauth_token": token, **_IDENTITY}


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

    def _write_config_token(self, token: str = _TOKEN) -> Path:
        path = self.copilot_home / "config.json"
        # Nested per-host shape — the token must be found even when it is not
        # a top-level key.
        self.assertTrue(ca._write_json(path, {"github.com": {"oauth_token": token}}))
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

    def test_save_reads_the_token_from_the_config_file(self) -> None:
        self._write_config_token()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ca.cmd_save("personal"), 0)
            self.assertEqual(ca.cmd_who(), 0)
        self.assertEqual(ca._read_json(self.account_dir / "personal.json"), _profile())

    def test_save_reads_the_token_from_the_keychain(self) -> None:
        self.keychain[(ca._KEYCHAIN_SERVICE, ca._KEYCHAIN_ACCOUNT)] = _TOKEN
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ca.cmd_save("personal"), 0)
        self.assertEqual(ca._read_active(), _TOKEN)

    def test_env_token_is_the_last_resort_and_warns(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_env_token"}, clear=False):
            self.assertEqual(ca._read_active(), "ghp_env_token")
            err = io.StringIO()
            with redirect_stderr(err):
                ca._warn_env_shadow()
            self.assertIn("GH_TOKEN", _ANSI_RE.sub("", err.getvalue()))
            # A config-file token still wins over the exported one.
            self._write_config_token()
            self.assertEqual(ca._read_active(), _TOKEN)

    def test_save_no_args_derives_the_name_from_the_github_login(self) -> None:
        self._write_config_token()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ca.main(["save"]), 0)
        self.assertTrue((self.account_dir / "testuser.json").is_file())

    def test_save_no_args_falls_back_to_a_per_token_name(self) -> None:
        # No identity available (offline / revoked token): two accounts must
        # still not collide on one label.
        self._install_fake_identity(None)
        self._write_config_token()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ca.main(["save"]), 0)
        first = ca._derived_name({"oauth_token": _TOKEN})
        self.assertTrue((self.account_dir / f"{first}.json").is_file())
        self.assertNotEqual(first, ca._derived_name({"oauth_token": "ghu_other"}))

    # ── switch / sync / remove ──────────────────────────────────────────────

    def test_switch_updates_both_the_config_file_and_the_keychain(self) -> None:
        self._write_config_token("ghu_old_token")
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ca.cmd_switch("personal"), 0)

        self.assertEqual(ca._read_active(), _TOKEN)
        self.assertEqual(self.keychain[(ca._KEYCHAIN_SERVICE, ca._KEYCHAIN_ACCOUNT)], _TOKEN)
        # rewritten in place, nested shape preserved
        self.assertEqual(
            ca._read_json(self.copilot_home / "config.json"),
            {"github.com": {"oauth_token": _TOKEN}},
        )

    def test_switch_keeps_a_backup_of_the_replaced_token(self) -> None:
        self._write_config_token("ghu_old_token")
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ca.cmd_switch("personal"), 0)
        self.assertTrue(list((self.account_dir.parent / "backups").glob("token.backup-*.json")))

    def test_switch_refuses_a_profile_with_an_empty_token(self) -> None:
        self.assertTrue(ca._write_json(self.account_dir / "broken.json", {"oauth_token": ""}))
        self._write_config_token()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(ca.cmd_switch("broken"), 1)
        self.assertIn("no Copilot token", _ANSI_RE.sub("", err.getvalue()))
        self.assertEqual(ca._read_active(), _TOKEN)  # live login untouched

    def test_write_active_never_persists_an_empty_secret(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(ca._write_active("   "))
        self.assertIn("empty Copilot token", _ANSI_RE.sub("", err.getvalue()))
        self.assertEqual(self.keychain, {})
        self.assertFalse((self.copilot_home / "config.json").exists())

    def test_sync_copies_the_live_token_back_to_its_profile(self) -> None:
        # A profile saved before the identity lookup existed: sync must write
        # the live token without dropping what is already stored.
        self.assertTrue(ca._write_json(self.account_dir / "personal.json", _profile()))
        self._write_config_token()
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
        self._write_config_token("ghu_rotated_token")

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
        self._write_config_token()
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
        self._write_config_token()
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
                    "usage": {
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
            [{"name": "personal", "active": False, "usage": None, "no_quota_api": True}],
        )

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
