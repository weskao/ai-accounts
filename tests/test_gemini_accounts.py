from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import _utils as u
from ai_accounts import gemini_accounts as ga
from ai_accounts import gemini_usage as gu
from ai_accounts.usage_format import UsageWindow



class _TtyStringIO(io.StringIO):
    """A capture buffer that reports itself as a terminal.

    ``cmd_autoswitch`` reads ``sys.stdout.isatty()`` to decide whether there is
    a session to restart into; a plain StringIO is correctly seen as unattended.
    """

    def isatty(self) -> bool:
        return True

def _jwt(payload: ga.JsonDict) -> str:
    def encode(value: object) -> str:
        raw = json.dumps(value).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig"


def _creds(
    sub: str,
    email: str,
    *,
    refresh_token: str = "rt-old",
    access_token: str = "at-old",
    expires_in_ms: int = 3600 * 1000,
) -> ga.JsonDict:
    exp = int(time.time()) + expires_in_ms // 1000
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": _jwt({"email": email, "sub": sub, "exp": exp}),
        "token_type": "Bearer",
        "expiry_date": int(time.time() * 1000) + expires_in_ms,
    }


def _usage(
    email: str = "a@x.com", *, error: str | None = None
) -> gu.UsageSnapshot:
    return gu.UsageSnapshot(
        UsageWindow(6, 2_000_000_000, 10080),
        UsageWindow(25, 2_000_000_000, 300),
        UsageWindow(0, 2_000_000_000, 10080),
        None,
        email,
        "Pro",
        2_000_000_000,
        error,
    )


class _HomeMixin(unittest.TestCase):
    home: Path = Path()
    active: ga.JsonDict | None = None

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "antigravity"
        (self.home / "accounts").mkdir(parents=True)
        env = mock.patch.dict(
            os.environ, {"ANTIGRAVITY_HOME": str(self.home)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANTIGRAVITY_ACCOUNT_DIR", None)
        os.environ.pop("ANTIGRAVITY_OAUTH_JSON", None)
        self.active = None

        # No test may reach Google. Default every case to "client credentials
        # unresolvable", which is exactly the agy-CLI fallback branch; the direct
        # OAuth tests below opt in explicitly.
        creds = mock.patch.object(ga, "_client_credentials", return_value=None)
        creds.start()
        self.addCleanup(creds.stop)

        read = mock.patch.object(ga, "_read_cli_keyring_secret", side_effect=self._secret)
        write = mock.patch.object(ga, "_write_cli_auth_text", side_effect=self._write)
        delete = mock.patch.object(ga, "_delete_cli_auth", side_effect=self._delete)
        read.start()
        write.start()
        delete.start()
        self.addCleanup(read.stop)
        self.addCleanup(write.stop)
        self.addCleanup(delete.stop)

    def _secret(self) -> str | None:
        if self.active is None:
            return None
        return ga._keyring_secret_from_auth(self.active)

    def _write(self, text: str) -> bool:
        value = json.loads(text)
        if ga._keyring_secret_from_auth(value) is None:
            return False
        self.active = value
        mirror = self.home / "oauth_creds.json"
        mirror.write_text(json.dumps(value), encoding="utf-8")
        return True

    def _delete(self) -> bool:
        self.active = None
        return True

    def set_active(self, payload: ga.JsonDict) -> None:
        self.active = payload.copy()

    def write_profile(self, name: str, payload: ga.JsonDict) -> Path:
        path = self.home / "accounts" / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def mark_current(self, name: str) -> None:
        (self.home / "accounts" / ".current-profile").write_text(
            name, encoding="utf-8"
        )

    def quiet(self, function, *args) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return function(*args)

    def capture(self, function, *args) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = function(*args)
        return result, output.getvalue(), error.getvalue()


class StoragePathTests(unittest.TestCase):
    def test_default_storage_is_ai-accounts_owned(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(ga.Path, "home", return_value=Path("/tmp/home")),
        ):
            self.assertEqual(
                ga._antigravity_dir(), Path("/tmp/home/.ai-accounts/antigravity")
            )
            self.assertEqual(
                ga._account_dir(), Path("/tmp/home/.ai-accounts/antigravity/accounts")
            )
            self.assertEqual(
                ga._auth_file(), Path("/tmp/home/.ai-accounts/antigravity/oauth_creds.json")
            )

    def test_legacy_codexbar_store_migrates_with_profiles_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".codexbar" / "antigravity"
            accounts = legacy / "accounts"
            accounts.mkdir(parents=True)
            (legacy / "oauth_creds.json").write_text("{}", encoding="utf-8")
            (accounts / "work.json").write_text("{}", encoding="utf-8")
            (accounts / ".current-profile").write_text("work", encoding="utf-8")

            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(ga.Path, "home", return_value=home),
                redirect_stderr(io.StringIO()),
            ):
                moved = ga._antigravity_dir()

            self.assertEqual(moved, home / ".ai-accounts" / "antigravity")
            self.assertTrue((moved / "oauth_creds.json").is_file())
            self.assertTrue((moved / "accounts" / "work.json").is_file())
            self.assertEqual(
                (moved / "accounts" / ".current-profile").read_text(encoding="utf-8"),
                "work",
            )
            self.assertFalse(legacy.exists())


class ClaimsTests(unittest.TestCase):
    def test_claims_decode_identity_and_millisecond_expiry(self) -> None:
        claims = ga._claims_from_auth(
            _creds("sub-123", "a@x.com", expires_in_ms=7_200_000)
        )
        self.assertEqual(claims["email"], "a@x.com")
        self.assertEqual(claims["account_id"], "sub-123")
        expiry = claims["expires_epoch"]
        self.assertIsInstance(expiry, int)
        self.assertAlmostEqual(int(expiry or 0), int(time.time()) + 7200, delta=5)

    def test_refresh_token_marks_session_refreshable(self) -> None:
        claims = ga._claims_from_auth(_creds("sub", "a@x.com"))
        text = ga._ANSI_RE.sub("", "\n".join(ga._claims_lines(claims)))
        self.assertIn("Refreshable by agy", text)
        self.assertNotIn("soon", text)

    def test_a_misfiled_foreign_credential_is_flagged_malformed_not_expired(self) -> None:
        # Given: a genuine credential shape (has access_token/refresh_token/expiry_date)
        self.assertFalse(ga._claims_from_auth(_creds("sub", "a@x.com"))["malformed"])
        # Given: plan.md §2.4 — an x.ai payload dropped into the antigravity dir by
        # mistake. It parses as JSON but carries none of the expected OAuth keys.
        foreign = {
            "https://auth.x.ai::client": {
                "auth_mode": "oidc",
                "email": "a@x.com",
                "refresh_token": "secret-refresh-token",
                "key": "secret-access-token",
            }
        }
        claims = ga._claims_from_auth(foreign)
        # Then: reported as malformed, not silently read as expired/blank
        self.assertTrue(claims["malformed"])
        self.assertIsNone(claims["expires_epoch"])


class KeyringTests(unittest.TestCase):
    def test_keyring_round_trip_preserves_tokens_and_expiry(self) -> None:
        original = _creds("sub", "a@x.com")
        secret = ga._keyring_secret_from_auth(original)
        self.assertIsNotNone(secret)
        restored = ga._auth_from_keyring_secret(secret or "")
        self.assertIsNotNone(restored)
        if restored is None:
            self.fail("expected decoded keyring credentials")
        self.assertEqual(restored["access_token"], original["access_token"])
        self.assertEqual(restored["refresh_token"], original["refresh_token"])
        restored_expiry = restored["expiry_date"]
        original_expiry = original["expiry_date"]
        if not isinstance(restored_expiry, int | float):
            self.fail("expected numeric restored expiry")
        if not isinstance(original_expiry, int | float):
            self.fail("expected numeric original expiry")
        self.assertAlmostEqual(restored_expiry, original_expiry, delta=1000)

    def test_keyring_rejects_credentials_without_refresh_token(self) -> None:
        auth = _creds("sub", "a@x.com")
        del auth["refresh_token"]
        self.assertIsNone(ga._keyring_secret_from_auth(auth))

    def test_keyring_write_hands_over_the_plain_token_payload(self) -> None:
        # Per-platform encoding (the darwin base64 marker) lives in
        # _utils.go_keyring_write — this layer deals in plaintext JSON only.
        auth = _creds("sub", "a@x.com")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"ANTIGRAVITY_HOME": tmp}
        ), mock.patch.object(ga, "_store_keychain_secret", return_value=True) as store:
            self.assertTrue(ga._write_cli_auth_text(json.dumps(auth)))
        secret = store.call_args.args[0]
        self.assertFalse(secret.startswith("go-keyring-base64:"))
        self.assertEqual(
            json.loads(secret)["token"]["access_token"], auth["access_token"]
        )

    def test_keyring_secret_round_trip_goes_through_the_os_slot(self) -> None:
        auth = _creds("sub", "a@x.com")
        secret = ga._keyring_secret_from_auth(auth)
        self.assertIsNotNone(secret)
        with mock.patch.object(ga, "go_keyring_read", return_value=secret) as read:
            restored = ga._auth_from_keyring_secret(ga._read_cli_keyring_secret() or "")
        read.assert_called_once_with("gemini", "antigravity")
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored["access_token"], auth["access_token"])


class UsageTests(unittest.TestCase):
    def test_ports_parse_lsof_listener_rows(self) -> None:
        output = (
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            "agy 123 user 10u IPv4 0x0 0t0 TCP 127.0.0.1:63833 (LISTEN)\n"
            "agy 123 user 11u IPv4 0x0 0t0 TCP 127.0.0.1:63834 (LISTEN)\n"
        )
        with mock.patch.object(
            gu.subprocess,
            "run",
            return_value=mock.Mock(stdout=output),
        ):
            self.assertEqual(gu._ports(123), [63833, 63834])

    @unittest.skipIf(gu.pty is None, "requires POSIX pseudo-terminal support")
    def test_background_pty_has_a_terminal_size(self) -> None:
        with mock.patch.object(gu.pty, "openpty", return_value=(10, 11)), mock.patch.object(
            gu.fcntl, "ioctl"
        ) as ioctl:
            self.assertEqual(gu._open_pty(), (10, 11))
        ioctl.assert_called_once_with(
            11, gu.termios.TIOCSWINSZ, gu.struct.pack("HHHH", 50, 160, 0, 0)
        )

    def test_parse_official_quota_groups(self) -> None:
        payload: gu.JsonDict = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "weekly", "remainingFraction": 0.936},
                        {"bucketId": "five-hour", "remainingFraction": 0.75},
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "weekly", "remainingFraction": 1.0}
                    ],
                },
            ]
        }
        gemini_week, gemini_session, other_week, other_session = gu._parse_summary(payload)
        self.assertIsNotNone(gemini_week)
        self.assertIsNotNone(gemini_session)
        self.assertIsNotNone(other_week)
        if gemini_week is None or gemini_session is None or other_week is None:
            self.fail("expected parsed quota windows")
        self.assertEqual(gemini_week.percentage, 6)
        self.assertEqual(gemini_session.percentage, 25)
        self.assertEqual(other_week.percentage, 0)
        self.assertIsNone(other_session)

    def test_identity_labels_free_tier_not_pro(self) -> None:
        # Antigravity's free preview reports planName "Pro" for everyone;
        # userTier is the real subscription and must win.
        payload: gu.JsonDict = {
            "userStatus": {
                "email": "a@x.com",
                "userTier": {"id": "free-tier", "name": "Antigravity Starter Quota"},
                "planStatus": {"planInfo": {"planName": "Pro"}},
            }
        }
        self.assertEqual(gu._identity(payload), ("a@x.com", "Free"))

    def test_identity_uses_user_tier_name_for_paid(self) -> None:
        payload: gu.JsonDict = {
            "userStatus": {
                "email": "a@x.com",
                "userTier": {"id": "pro-tier", "name": "Google AI Pro"},
                "planStatus": {"planInfo": {"planName": "Pro"}},
            }
        }
        self.assertEqual(gu._identity(payload), ("a@x.com", "Google AI Pro"))

    def test_identity_falls_back_to_plan_name_without_user_tier(self) -> None:
        payload: gu.JsonDict = {
            "userStatus": {
                "email": "a@x.com",
                "planStatus": {"planInfo": {"planName": "Pro"}},
            }
        }
        self.assertEqual(gu._identity(payload), ("a@x.com", "Pro"))

    @unittest.skipIf(os.name == "nt", "Windows returns the platform error first")
    def test_fetch_usage_without_agy_reports_error(self) -> None:
        with mock.patch.object(gu.shutil, "which", return_value=None):
            self.assertEqual(gu.fetch_usage().error, "agy not found")

    def test_relogin_error_has_an_actionable_label(self) -> None:
        snapshot = _usage(error="re-login required")
        self.assertEqual(gu.format_refreshed_at(snapshot), "RELOGIN")


class ProfileCommandTests(_HomeMixin):
    def test_save_persists_active_keyring_session(self) -> None:
        self.set_active(_creds("sub-w", "w@x.com", refresh_token="rt-live"))
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_usage(email="w@x.com")
        ):
            self.assertEqual(self.quiet(ga.cmd_save, "work"), 0)
        saved = json.loads((self.home / "accounts" / "work.json").read_text())
        self.assertEqual(saved["refresh_token"], "rt-live")
        # agy's keyring token carries no identity — email is backfilled from usage.
        self.assertEqual(saved["email"], "w@x.com")

    def test_save_without_session_errors(self) -> None:
        self.assertEqual(self.quiet(ga.cmd_save, "work"), 1)

    def test_switch_activates_profile_in_keyring(self) -> None:
        self.write_profile("work", _creds("sub-w", "w@x.com", refresh_token="rt-work"))
        self.assertEqual(self.quiet(ga.cmd_switch, "work"), 0)
        self.assertIsNotNone(self.active)
        if self.active is None:
            self.fail("expected active session")
        self.assertEqual(self.active["refresh_token"], "rt-work")
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )

    def test_switch_folds_rotated_outgoing_token_into_profile(self) -> None:
        self.write_profile("old", _creds("sub-o", "o@x.com", refresh_token="rt-stale"))
        self.write_profile("new", _creds("sub-n", "n@x.com", refresh_token="rt-new"))
        self.set_active(_creds("sub-o", "o@x.com", refresh_token="rt-rotated"))
        self.mark_current("old")
        self.quiet(ga.cmd_switch, "new")
        old = json.loads((self.home / "accounts" / "old.json").read_text())
        self.assertEqual(old["refresh_token"], "rt-rotated")
        self.assertEqual(old["id_token"], _creds("sub-o", "o@x.com")["id_token"])

    def test_list_matches_codex_claim_columns_and_shows_quota_columns(self) -> None:
        auth = _creds("sub-a", "a@x.com", refresh_token="rt-a")
        self.write_profile("active", auth)
        self.set_active(auth)
        self.mark_current("active")
        with mock.patch.object(ga.gemini_usage, "fetch_usage", return_value=_usage()):
            result, output, _ = self.capture(ga.cmd_list)
        text = ga._ANSI_RE.sub("", output)
        self.assertEqual(result, 0)
        self.assertIn("ID", text)
        self.assertIn("SESSION", text)
        self.assertNotIn("AUTH", text)
        self.assertIn("sub-a", text)
        self.assertIn("GEMINI 5H USED", text)
        self.assertIn("GEMINI 1W USED", text)
        self.assertIn("CLAUDE/GPT 1W USED", text)
        self.assertIn("refreshable", text)
        self.assertNotIn("soon", text)
        self.assertEqual(text.count("ACTIVE"), 1)

    def test_list_hides_columns_unavailable_from_agy(self) -> None:
        auth = _creds("sub-a", "a@x.com", refresh_token="rt-a")
        auth.pop("id_token")
        auth["email"] = "a@x.com"
        self.write_profile("active", auth)
        self.set_active(auth)
        self.mark_current("active")
        weekly_usage = gu.UsageSnapshot(
            UsageWindow(6, 2_000_000_000, 10080),
            None,
            UsageWindow(0, 2_000_000_000, 10080),
            None,
            "a@x.com",
            "Pro",
            2_000_000_000,
            None,
        )
        with mock.patch.object(ga.gemini_usage, "fetch_usage", return_value=weekly_usage):
            _, output, _ = self.capture(ga.cmd_list)
        text = ga._ANSI_RE.sub("", output)
        self.assertNotIn("│ ID │", text)
        self.assertNotIn("GEMINI 5H USED", text)
        self.assertNotIn("CLAUDE/GPT 5H USED", text)
        self.assertIn("GEMINI 1W USED", text)

    def test_list_restores_original_keyring_session(self) -> None:
        original = _creds("sub-a", "a@x.com", refresh_token="rt-a")
        self.write_profile("a", original)
        self.write_profile("b", _creds("sub-b", "b@x.com", refresh_token="rt-b"))
        self.set_active(original)
        self.mark_current("a")
        with mock.patch.object(ga.gemini_usage, "fetch_usage", return_value=_usage()):
            self.quiet(ga.cmd_list)
        self.assertIsNotNone(self.active)
        if self.active is None:
            self.fail("expected restored session")
        self.assertEqual(self.active["refresh_token"], "rt-a")

    def test_usage_shows_only_active_profile(self) -> None:
        active = _creds("sub-a", "a@x.com", refresh_token="rt-a")
        self.write_profile("active", active)
        self.write_profile("other", _creds("sub-b", "b@x.com", refresh_token="rt-b"))
        self.set_active(active)
        self.mark_current("active")
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_usage()
        ) as fetch:
            result, output, _ = self.capture(lambda: ga.cmd_list(only_active=True))
        text = ga._ANSI_RE.sub("", output)
        self.assertEqual(result, 0)
        self.assertEqual(fetch.call_count, 1)  # only the active profile is activated
        self.assertIn("Current Antigravity account", text)
        self.assertEqual(text.count("ACTIVE"), 1)
        self.assertNotIn("other", text)

    def test_usage_reports_when_no_active_profile(self) -> None:
        self.write_profile("saved", _creds("sub-a", "a@x.com", refresh_token="rt-a"))
        result, output, err = self.capture(lambda: ga.cmd_list(only_active=True))
        text = ga._ANSI_RE.sub("", output + err)
        self.assertEqual(result, 0)
        self.assertNotIn("PROFILE", text)  # no table rendered
        self.assertIn("No active Antigravity account", text)

    def test_list_rejects_quota_from_a_different_account(self) -> None:
        original = _creds("sub-a", "a@x.com", refresh_token="rt-a")
        self.write_profile("a", original)
        self.set_active(original)
        self.mark_current("a")

        def wrong_account(*, timeout: float) -> gu.UsageSnapshot:
            self.set_active(_creds("sub-b", "b@x.com", refresh_token="rt-b"))
            return _usage("b@x.com")

        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", side_effect=wrong_account
        ):
            _, output, _ = self.capture(ga.cmd_list)
        self.assertIn("RELOGIN", ga._ANSI_RE.sub("", output))
        saved = json.loads((self.home / "accounts" / "a.json").read_text())
        self.assertEqual(saved["refresh_token"], "rt-a")

    def test_sync_merges_rotated_tokens_without_losing_identity(self) -> None:
        saved = _creds("sub-w", "w@x.com", refresh_token="rt-old")
        profile = self.write_profile("work", saved)
        active = saved.copy()
        active.pop("id_token")
        active["refresh_token"] = "rt-live"
        self.set_active(active)
        self.mark_current("work")
        self.assertEqual(self.quiet(ga.cmd_sync), 0)
        merged = json.loads(profile.read_text())
        self.assertEqual(merged["refresh_token"], "rt-live")
        self.assertEqual(merged["id_token"], saved["id_token"])

    def test_save_no_args_derives_name_from_email_with_one_fetch(self) -> None:
        self.set_active(_creds("sub-w", "person@example.com", refresh_token="rt-live"))
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_usage(email="person@example.com")
        ) as fetch:
            self.assertEqual(self.quiet(ga.cmd_save), 0)
        # The name lookup and the email backfill share the single RPC.
        self.assertEqual(fetch.call_count, 1)
        saved = json.loads((self.home / "accounts" / "person.json").read_text())
        self.assertEqual(saved["refresh_token"], "rt-live")
        self.assertEqual(saved["email"], "person@example.com")

    def test_save_no_args_without_email_creates_no_profile(self) -> None:
        self.set_active(_creds("sub-w", "person@example.com", refresh_token="rt-live"))
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_usage(email="")
        ):
            result, _, err = self.capture(ga.cmd_save)
        self.assertEqual(result, 1)
        self.assertIn("Could not derive a name", ga._ANSI_RE.sub("", err))
        self.assertEqual(list((self.home / "accounts").glob("*.json")), [])

    def test_save_with_name_still_fetches_exactly_once(self) -> None:
        self.set_active(_creds("sub-w", "w@x.com", refresh_token="rt-live"))
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_usage(email="w@x.com")
        ) as fetch:
            self.assertEqual(self.quiet(ga.cmd_save, "work"), 0)
        # No pre-fetch for the named case: only the post-write backfill runs.
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue((self.home / "accounts" / "work.json").is_file())

    def test_remove_no_args_opens_picker_over_all_profiles(self) -> None:
        self.write_profile("personal", _creds("sub-p", "personal@example.com"))
        self.write_profile("work", _creds("sub-w", "work@example.com"))
        with mock.patch("builtins.input", return_value="2"):
            result, output, _ = self.capture(ga.cmd_remove_interactive)
        text = ga._ANSI_RE.sub("", output)
        self.assertEqual(result, 0)
        self.assertIn("1) personal", text)
        self.assertIn("2) work", text)
        self.assertFalse((self.home / "accounts" / "work.json").exists())
        self.assertTrue((self.home / "accounts" / "personal.json").exists())

    def test_remove_no_args_cancel_keeps_every_profile(self) -> None:
        self.write_profile("personal", _creds("sub-p", "personal@example.com"))
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.quiet(ga.cmd_remove_interactive), 1)
        self.assertTrue((self.home / "accounts" / "personal.json").exists())

    def test_remove_no_args_reports_when_no_saved_profiles(self) -> None:
        result, _, err = self.capture(ga.cmd_remove_interactive)
        self.assertEqual(result, 1)
        self.assertIn("No saved Antigravity profiles", ga._ANSI_RE.sub("", err))

    def test_help_alias_prints_help(self) -> None:
        result, output, _ = self.capture(lambda: ga.main(["help"]))
        self.assertEqual(result, 0)
        self.assertIn("agy-accounts save [<name>]", output)
        self.assertIn("agy-accounts remove [<name>]", output)
        self.assertIn("-h | --help | help", output)

    def test_remove_current_profile_clears_marker(self) -> None:
        self.write_profile("work", _creds("sub", "a@x.com"))
        self.mark_current("work")
        self.assertEqual(self.quiet(ga.cmd_remove, "work"), 0)
        self.assertFalse((self.home / "accounts" / ".current-profile").exists())


class LoginAndRefreshTests(_HomeMixin):
    def test_login_switch_saves_session_when_agy_writes_new_credentials(self) -> None:
        fresh = _creds("sub-new", "new@x.com", refresh_token="rt-new")
        process = mock.Mock(pid=123)
        process.poll.return_value = None

        def launch(*args, **kwargs):
            self.set_active(fresh)
            return process

        with mock.patch.object(ga, "ensure_tool", return_value=True), mock.patch.object(
            ga.subprocess, "Popen", side_effect=launch
        ) as popen, mock.patch.object(
            ga.subprocess,
            "run",
            side_effect=AssertionError("login-switch must not wait for agy to exit"),
        ), mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_usage(email="new@x.com")
        ):
            self.assertEqual(self.quiet(ga.cmd_login_switch, "new"), 0)

        self.assertEqual(popen.call_args.args[0], ["agy"])
        process.terminate.assert_called_once_with()
        saved = json.loads((self.home / "accounts" / "new.json").read_text())
        self.assertEqual(saved["refresh_token"], "rt-new")
        # Identity is backfilled from usage, so the saved profile isn't "(unknown)".
        self.assertEqual(saved["email"], "new@x.com")

    def test_cancelled_login_restores_previous_session(self) -> None:
        old = _creds("sub-old", "old@x.com", refresh_token="rt-old")
        self.set_active(old)
        process = mock.Mock(returncode=130)
        process.poll.return_value = 130
        with mock.patch.object(ga, "ensure_tool", return_value=True), mock.patch.object(
            ga.subprocess, "Popen", return_value=process
        ):
            self.assertEqual(self.quiet(ga.cmd_login_switch, "new"), 130)
        self.assertIsNotNone(self.active)
        if self.active is None:
            self.fail("expected restored session")
        self.assertEqual(self.active["refresh_token"], "rt-old")

    def test_login_switch_refuses_when_only_the_old_session_reappears(self) -> None:
        # A running Antigravity IDE restores the deleted keyring session, so the
        # keyring only ever yields the outgoing credential. login-switch must not
        # save it under the new name — the account is unchanged.
        old = _creds("sub-old", "old@x.com", refresh_token="rt-old")
        self.set_active(old)
        process = mock.Mock(pid=123)
        process.poll.return_value = None

        def launch(*args, **kwargs):
            self.set_active(old)  # IDE re-writes the same session on delete
            return process

        with mock.patch.object(ga, "ensure_tool", return_value=True), mock.patch.object(
            ga.subprocess, "Popen", side_effect=launch
        ), mock.patch.object(ga.time, "sleep"), mock.patch.object(
            ga, "_LOGIN_TIMEOUT_SECONDS", 0.05
        ):
            self.assertEqual(self.quiet(ga.cmd_login_switch, "new"), 1)

        self.assertFalse((self.home / "accounts" / "new.json").exists())
        self.assertIsNotNone(self.active)
        if self.active is None:
            self.fail("expected restored session")
        self.assertEqual(self.active["refresh_token"], "rt-old")
        process.terminate.assert_called_once_with()

    def test_refresh_profile_uses_agy_and_saves_rotated_tokens(self) -> None:
        profile = self.write_profile("work", _creds("sub", "a@x.com", access_token="old"))

        def refresh() -> gu.UsageSnapshot:
            if self.active is None:
                self.fail("expected activated profile")
            self.active["access_token"] = "rotated"
            return _usage()

        with mock.patch.object(ga.gemini_usage, "fetch_usage", side_effect=refresh):
            self.assertEqual(self.quiet(ga.cmd_refresh, "work"), 0)
        self.assertEqual(json.loads(profile.read_text())["access_token"], "rotated")

    def test_refresh_failure_restores_original_session(self) -> None:
        old = _creds("sub-old", "old@x.com", refresh_token="rt-old")
        self.set_active(old)
        self.write_profile("work", _creds("sub", "a@x.com", refresh_token="rt-work"))
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_usage(error="agy unavailable")
        ):
            self.assertEqual(self.quiet(ga.cmd_refresh, "work"), 1)
        self.assertIsNotNone(self.active)
        if self.active is None:
            self.fail("expected restored session")
        self.assertEqual(self.active["refresh_token"], "rt-old")


def _quota(used: int, *, error: str | None = None) -> gu.UsageSnapshot:
    """Placeholder agy snapshot whose every live window sits at *used* percent."""
    return gu.UsageSnapshot(
        UsageWindow(used, 2_000_000_000, 10080),
        UsageWindow(used, 2_000_000_000, 300),
        None,
        None,
        "user@example.com",
        "Free",
        2_000_000_000,
        error,
    )


class _AutoswitchMixin(_HomeMixin):
    """_HomeMixin plus a throwaway ai-accounts config, so no real one is read."""

    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(
            os.environ,
            {"AI_ACCOUNTS_CONFIG_JSON": str(self.home / "config.json")},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        self.write_config(enabled=True)

    def write_config(self, **values: object) -> None:
        # notify="none" keeps the engine from firing a real desktop notification.
        settings: dict[str, object] = {"notify": "none"}
        settings.update(values)
        (self.home / "config.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )

    def given_active(self, name: str, *others: str) -> None:
        """*name* is the live session; *others* are saved but never activated."""
        self.write_profile(name, _creds(f"sub-{name}", f"{name}@example.com"))
        self.set_active(_creds(f"sub-{name}", f"{name}@example.com"))
        self.mark_current(name)
        for other in others:
            self.write_profile(other, _creds(f"sub-{other}", f"{other}@example.com"))


class AgyAutoswitchProbeTests(_AutoswitchMixin):
    def test_quota_check_queries_only_the_active_account(self) -> None:
        # Given: an exhausted active account and two untouched alternatives
        self.given_active("work", "spare", "backup")
        calls: list[float] = []

        def fetch(timeout: float = 15) -> gu.UsageSnapshot:
            calls.append(timeout)
            return _quota(96)

        # When: autoswitch runs
        with mock.patch.object(ga.gemini_usage, "fetch_usage", side_effect=fetch):
            rc = self.quiet(ga.cmd_autoswitch)

        # Then: the agy RPC was consulted exactly once — for the live session.
        # Probing a candidate would mean activating it first, which is the one
        # thing this command must never do.
        self.assertEqual(len(calls), 1)
        self.assertEqual(rc, 0)


class AgyBlindSwitchGateTests(_AutoswitchMixin):
    """A candidate's quota cannot be checked in advance, so switching to one is
    a leap of faith — off unless the user opted in via ``agy_blind_switch``."""

    def test_switch_is_skipped_when_blind_switching_is_not_opted_into(self) -> None:
        # Given: default config (agy_blind_switch unset) and an exhausted active
        self.write_config(enabled=True)
        self.given_active("work", "spare", "backup")

        # When: autoswitch runs
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_quota(96)
        ):
            rc, out, err = self.capture(ga.cmd_autoswitch)

        # Then: it says why, and nothing moved
        self.assertEqual(rc, 0)
        self.assertIn("cannot verify candidate quota, skipping", out + err)
        self.assertIsNotNone(self.active)
        self.assertEqual((self.active or {})["refresh_token"], "rt-old")
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )

    def test_explicit_false_also_skips(self) -> None:
        # Given: the opt-in explicitly declined
        self.write_config(enabled=True, agy_blind_switch=False)
        self.given_active("work", "spare")

        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_quota(96)
        ):
            rc, out, err = self.capture(ga.cmd_autoswitch)

        self.assertEqual(rc, 0)
        self.assertIn("cannot verify candidate quota, skipping", out + err)
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )

    def test_a_hand_edited_string_true_does_not_opt_in(self) -> None:
        # Given: `agy_blind_switch` hand-edited to the STRING "true" — valid
        # JSON and truthy to bool(). Opting into an unverifiable switch must
        # take a real JSON `true`, nothing looser (the `config set` CLI can
        # only ever write a real bool; a text editor can write this).
        self.write_config(enabled=True, agy_blind_switch="true")
        self.given_active("work", "spare", "backup")

        # When: autoswitch runs against an exhausted active account
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_quota(96)
        ):
            rc, out, err = self.capture(ga.cmd_autoswitch)

        # Then: the gate fails CLOSED — nothing moved
        self.assertEqual(rc, 0)
        self.assertIn("cannot verify candidate quota, skipping", out + err)
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )

    def test_opted_in_switch_happens_and_says_the_target_was_unverified(self) -> None:
        # Given: the user opted into blind switching
        self.write_config(enabled=True, agy_blind_switch=True)
        self.given_active("work", "spare", "backup")

        # When: autoswitch runs against an exhausted active account
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_quota(96)
        ):
            rc, out, err = self.capture(ga.cmd_autoswitch)

        # Then: it moves to the first candidate and is explicit that the
        # target's own quota was never checked.
        self.assertEqual(rc, 0)
        report = ga._ANSI_RE.sub("", out + err)
        self.assertIn("backup", report)
        self.assertIn("NOT verified", report)
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "backup"
        )
        self.assertEqual((self.active or {})["refresh_token"], "rt-old")

    def test_below_threshold_leaves_the_active_account_alone(self) -> None:
        # Given: plenty of quota left, and blind switching allowed
        self.write_config(enabled=True, agy_blind_switch=True)
        self.given_active("work", "spare")

        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_quota(12)
        ):
            rc = self.quiet(ga.cmd_autoswitch)

        self.assertEqual(rc, 0)
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )

    def test_windows_usage_limitation_is_reported_without_crashing(self) -> None:
        # Given: the real fetch_usage on a platform it cannot inspect
        self.write_config(enabled=True, agy_blind_switch=True)
        self.given_active("work", "spare")

        # When: autoswitch is handed what the real fetch_usage returns there.
        # The os.name patch is scoped to that call alone — it is the process
        # -wide os.name, and pathlib reads it too.
        with mock.patch.object(gu.os, "name", "nt"):
            on_windows = gu.fetch_usage()
        self.assertEqual(
            on_windows.error, "agy usage inspection requires macOS or Linux"
        )
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=on_windows
        ):
            rc, out, err = self.capture(ga.cmd_autoswitch)

        # Then: the limitation is stated, and nothing was switched on a guess
        self.assertEqual(rc, 0)
        self.assertIn("agy usage inspection requires macOS or Linux", out + err)
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )

    def test_main_routes_the_autoswitch_subcommand(self) -> None:
        # Given: `agy-accounts autoswitch` with the credential store reachable
        # (main gates on go_keyring_available, absent on bare Linux CI)
        with (
            mock.patch.object(ga, "go_keyring_available", return_value=(True, "")),
            mock.patch.object(ga, "cmd_autoswitch", return_value=0) as command,
        ):
            rc = self.quiet(ga.main, ["autoswitch"])

        # Then: it reaches cmd_autoswitch rather than the unknown-command path
        self.assertEqual(rc, 0)
        command.assert_called_once_with()

    def test_a_quoted_string_does_not_count_as_opting_in(self) -> None:
        # Given: a hand-edited config where the opt-in was written as a JSON
        # *string*. bool("false") is True in Python, so a plain truthiness
        # check would silently enable the risky path it is meant to gate.
        self.write_config(enabled=True, agy_blind_switch="false")
        self.given_active("work", "spare", "backup")

        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_quota(96)
        ):
            rc, out, err = self.capture(ga.cmd_autoswitch)

        # Then: it stays closed — only a real JSON `true` opts in
        self.assertEqual(rc, 0)
        self.assertIn("cannot verify candidate quota, skipping", out + err)
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )

    def test_target_deleted_mid_run_never_falls_back_to_the_listing_scan(self) -> None:
        # Given: blind switching allowed, and the chosen target profile is
        # deleted during the (up to 8s) live quota fetch — i.e. after the
        # profile list was globbed but before the switch is attempted.
        self.write_config(enabled=True, agy_blind_switch=True)
        self.given_active("work", "spare", "backup")

        def fetch(timeout: float = 15) -> gu.UsageSnapshot:
            (self.home / "accounts" / "backup.json").unlink()
            return _quota(96)

        # cmd_list activates EVERY saved profile in turn to read its quota —
        # the exact keychain hijack this command exists to avoid. cmd_switch
        # falls through to it when the profile file is missing, so autoswitch
        # must never reach that branch.
        with (
            mock.patch.object(ga.gemini_usage, "fetch_usage", side_effect=fetch),
            mock.patch.object(ga, "cmd_list") as listing,
        ):
            rc = self.quiet(ga.cmd_autoswitch)

        listing.assert_not_called()
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "work"
        )
        self.assertEqual(rc, 1)


class AgyAutoswitchKeychainSafetyTests(unittest.TestCase):
    """The safety-critical one.

    ``cmd_list`` reads every profile's quota by writing each candidate's auth
    into the single shared keychain slot the live agy process reads, restoring
    afterwards. An unattended autoswitch doing that could corrupt an in-flight
    request on the user's real session — so candidate selection must never
    reach the keychain at all.

    Deliberately does NOT use ``_HomeMixin``: it leaves the real keychain
    helpers in place and intercepts only ``subprocess``, so a candidate probe
    would show up here as an ``add-generic-password`` rather than being hidden
    behind a stubbed-out helper.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "antigravity"
        (self.home / "accounts").mkdir(parents=True)
        env = mock.patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_HOME": str(self.home),
                "AI_ACCOUNTS_CONFIG_JSON": str(self.home / "config.json"),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANTIGRAVITY_ACCOUNT_DIR", None)
        os.environ.pop("ANTIGRAVITY_OAUTH_JSON", None)
        (self.home / "config.json").write_text(
            json.dumps({"enabled": True, "notify": "none"}), encoding="utf-8"
        )

    def _profile(self, name: str) -> ga.JsonDict:
        payload = _creds(f"sub-{name}", f"{name}@example.com", refresh_token=f"rt-{name}")
        (self.home / "accounts" / f"{name}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return payload

    def test_candidate_selection_never_mutates_the_shared_keychain_slot(self) -> None:
        # Given: an exhausted active account and two saved alternatives, with
        # the real keychain helpers wired to a captured `security` binary
        active = self._profile("work")
        self._profile("spare")
        self._profile("backup")
        (self.home / "accounts" / ".current-profile").write_text(
            "work", encoding="utf-8"
        )
        live_secret = ga._keyring_secret_from_auth(active)
        self.assertIsNotNone(live_secret)
        calls: list[list[str]] = []

        def run(cmd, *args, **kwargs):
            calls.append([str(part) for part in cmd])
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=live_secret, stderr=""
            )

        fetches: list[float] = []

        def fetch(timeout: float = 15) -> gu.UsageSnapshot:
            fetches.append(timeout)
            return _quota(97)

        # When: autoswitch runs with blind switching NOT opted into.
        # IS_MACOS is pinned so every platform takes the `security` go-keyring
        # branch this test captures (the ctypes/secret-tool branches would
        # bypass the subprocess mock and read the host credential store).
        with (
            mock.patch.object(u, "IS_MACOS", True),
            mock.patch.object(ga.subprocess, "run", side_effect=run),
            mock.patch.object(ga.gemini_usage, "fetch_usage", side_effect=fetch),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            rc = ga.cmd_autoswitch()
        self.assertEqual(rc, 0)

        # Then: `security` was never asked to write or delete anything.
        mutations = [
            call
            for call in calls
            if call[:1] == ["security"] and call[1] != "find-generic-password"
        ]
        self.assertEqual(mutations, [])

        # And no candidate's credential was ever handed to `security` at all.
        arguments = [argument for call in calls for argument in call]
        for candidate in ("spare", "backup"):
            secret = ga._keyring_secret_from_auth(
                json.loads(
                    (self.home / "accounts" / f"{candidate}.json").read_text(
                        encoding="utf-8"
                    )
                )
            )
            self.assertNotIn(secret, arguments)

        # And the quota RPC ran once — for the live session only.
        self.assertEqual(len(fetches), 1)


class AgyLiveUsedPctTests(unittest.TestCase):
    """The worst window decides — an account is throttled the moment any one
    of its quota windows runs out."""

    def test_the_fullest_window_wins_even_when_the_others_are_idle(self) -> None:
        snapshot = gu.UsageSnapshot(
            UsageWindow(20, None, 10080),   # weekly: plenty left
            UsageWindow(96, None, 300),     # 5h session: exhausted
            UsageWindow(3, None, 10080),
            None,
            "user@example.com",
            "Free",
            2_000_000_000,
            None,
        )
        self.assertEqual(ga._live_used_pct(snapshot), 96)

    def test_no_readable_window_is_unknown_rather_than_zero(self) -> None:
        # None must not collapse to 0% — "unknown" and "plenty left" are
        # different answers, and only one of them is safe to act on.
        empty = gu.UsageSnapshot(
            None, None, None, None, None, None, None, "agy unavailable"
        )
        self.assertIsNone(ga._live_used_pct(empty))



class AgyAutoswitchRestartTests(_AutoswitchMixin):
    """After a blind switch, the session is handed over via the restart ladder.

    The Antigravity IDE re-writes the keyring instantly and has no programmatic
    resume, so its presence forces the manual-restart rung
    (docs/autoswitch-hot-reload-spike.md, agy section, footnote 2).
    """

    def _autoswitch_on_a_tty(self, fake_run) -> tuple[int, str]:
        """cmd_autoswitch with a stdout that is a terminal, capturing spawns."""
        out, err = _TtyStringIO(), io.StringIO()
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_quota(96)
        ), mock.patch.object(ga.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(out), redirect_stderr(err):
            rc = ga.cmd_autoswitch()
        return rc, ga._ANSI_RE.sub("", out.getvalue() + err.getvalue())

    def test_an_interactive_switch_resumes_the_session_in_a_new_agy_process(self):
        # Given: blind switching opted into, an exhausted active account, a
        # terminal to restart into, and no Antigravity IDE in the way
        self.write_config(enabled=True, agy_blind_switch=True)
        self.given_active("work", "spare")
        spawned: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            spawned.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        # When: autoswitch runs
        with mock.patch.object(ga, "_antigravity_ide_running", return_value=False), \
                mock.patch.object(ga, "have", return_value=True):
            rc, report = self._autoswitch_on_a_tty(fake_run)

        # Then: exactly one NEW agy session was started to continue the last
        # conversation — nothing was signalled, killed or restarted
        self.assertEqual(rc, 0)
        self.assertEqual(spawned, [["agy", "--continue"]])
        self.assertEqual(
            (self.home / "accounts" / ".current-profile").read_text(), "spare"
        )

    def test_a_switch_with_the_antigravity_ide_running_never_spawns_a_resume(self):
        # Given: the same switch, but an Antigravity IDE process is up — it
        # would re-write the keyring underneath any resumed CLI session
        self.write_config(enabled=True, agy_blind_switch=True)
        self.given_active("work", "spare")
        spawned: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            spawned.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        # When: autoswitch runs on a terminal
        with mock.patch.object(ga, "_antigravity_ide_running", return_value=True), \
                mock.patch.object(ga, "have", return_value=True):
            rc, report = self._autoswitch_on_a_tty(fake_run)

        # Then: the ladder degrades to manual-restart — nothing spawned, the
        # switch still succeeds, and the user is told to restart by hand
        self.assertEqual(rc, 0)
        self.assertEqual(spawned, [])
        self.assertIn("restart your session", report)

    def test_an_unattended_switch_spawns_nothing_and_never_scans_processes(self):
        # Given: the same switch with nothing attached to a terminal
        self.write_config(enabled=True, agy_blind_switch=True)
        self.given_active("work", "spare")

        # When: autoswitch runs (capture's plain StringIO is not a tty)
        with mock.patch.object(
            ga.gemini_usage, "fetch_usage", return_value=_quota(96)
        ), mock.patch.object(ga, "_antigravity_ide_running") as ide, \
                mock.patch.object(ga.subprocess, "run") as spawn:
            rc, out, err = self.capture(ga.cmd_autoswitch)

        # Then: a background poll spawns NOTHING — not even the process scan,
        # which cannot change an already-manual outcome — and says so in words
        self.assertEqual(rc, 0)
        spawn.assert_not_called()
        ide.assert_not_called()
        self.assertIn("restart your session", ga._ANSI_RE.sub("", out + err))

    def test_the_ide_check_only_reads_the_process_table_and_fails_closed(self):
        # Given: a process query that reports a match, then no match
        seen: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            seen.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=len(seen) - 1)

        # When/Then: a match means the IDE is up, no match means it is not
        with mock.patch.object(ga, "have", return_value=True), \
                mock.patch.object(ga.subprocess, "run", side_effect=fake_run):
            self.assertTrue(ga._antigravity_ide_running())
            self.assertFalse(ga._antigravity_ide_running())

        # And: the query only ever READS the process table — pgrep, never
        # pkill/kill, and no signal flag
        self.assertTrue(seen)
        for cmd in seen:
            self.assertEqual(cmd[0], "pgrep")
            self.assertNotIn("-9", cmd)

        # And: with no way to look, it assumes the IDE is up (fail closed)
        with mock.patch.object(ga, "have", return_value=False), \
                mock.patch.object(ga.subprocess, "run") as spawn:
            self.assertTrue(ga._antigravity_ide_running())
        spawn.assert_not_called()


class AgyClientCredentialTests(unittest.TestCase):
    """Resolution order: env vars → Antigravity.app bundle → None (CLI fallback)."""

    def setUp(self) -> None:
        ga._client_credentials.cache_clear()
        self.addCleanup(ga._client_credentials.cache_clear)

    def test_env_vars_win_and_skip_the_bundle_scan(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_OAUTH_CLIENT_ID": "1-aaaaaaaaaaaaaaaaaaaaaa.apps.googleusercontent.com",
                "ANTIGRAVITY_OAUTH_CLIENT_SECRET": "GOCSPX-envplaceholdersecret0001",
            },
            clear=False,
        ), mock.patch.object(ga, "_antigravity_app_paths") as paths:
            self.assertEqual(
                ga._client_credentials(),
                (
                    "1-aaaaaaaaaaaaaaaaaaaaaa.apps.googleusercontent.com",
                    "GOCSPX-envplaceholdersecret0001",
                ),
            )
        paths.assert_not_called()

    def test_bundle_regex_finds_both_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "Antigravity.app"
            main_js = app / ga._APP_MAIN_JS
            main_js.parent.mkdir(parents=True)
            main_js.write_text(
                'const a="2-bbbbbbbbbbbbbbbbbbbbbb.apps.googleusercontent.com",'
                'b="GOCSPX-bundleplaceholdersecret02";',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                ga, "_antigravity_app_paths", return_value=(app,)
            ):
                self.assertEqual(
                    ga._client_credentials(),
                    (
                        "2-bbbbbbbbbbbbbbbbbbbbbb.apps.googleusercontent.com",
                        "GOCSPX-bundleplaceholdersecret02",
                    ),
                )

    def test_missing_app_resolves_to_none_instead_of_crashing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            ga, "_antigravity_app_paths", return_value=(Path("/nope/Antigravity.app"),)
        ):
            self.assertIsNone(ga._client_credentials())


class AgyErrorClassificationTests(unittest.TestCase):
    def test_invalid_grant_is_revoked_whatever_the_status(self) -> None:
        message = ga._http_error_message(400, '{"error":"invalid_grant"}')
        self.assertTrue(ga._is_revoked_error(message))

    def test_other_400s_are_transient(self) -> None:
        self.assertFalse(
            ga._is_revoked_error(ga._http_error_message(400, '{"error":"invalid_client"}'))
        )
        self.assertFalse(ga._is_revoked_error(ga._http_error_message(500, "boom")))
        self.assertFalse(ga._is_revoked_error("network error: timed out"))

    def test_expiry_skew_is_five_minutes_not_a_day(self) -> None:
        self.assertEqual(ga._REFRESH_SKEW_SECONDS, 300)
        now = int(time.time())
        self.assertFalse(ga._token_expired_or_soon({"expires_epoch": now + 3600}))
        self.assertTrue(ga._token_expired_or_soon({"expires_epoch": now + 60}))
        self.assertTrue(ga._token_expired_or_soon({"expires_epoch": now - 60}))
        self.assertFalse(ga._token_expired_or_soon({"expires_epoch": None}))


class AgyDirectRefreshTests(_HomeMixin):
    """The direct Google path, with the token endpoint mocked — never a real POST."""

    def enable(self, response: object) -> mock.MagicMock:
        creds = mock.patch.object(
            ga,
            "_client_credentials",
            return_value=(
                "3-cccccccccccccccccccccc.apps.googleusercontent.com",
                "GOCSPX-testplaceholdersecret003",
            ),
        )
        creds.start()
        self.addCleanup(creds.stop)
        post = mock.patch.object(ga, "oauth_token_refresh", return_value=response)
        handle = post.start()
        self.addCleanup(post.stop)
        return handle

    def test_refresh_writes_new_token_and_recomputed_ms_expiry(self) -> None:
        profile = self.write_profile(
            "work", _creds("sub-w", "user@example.com", access_token="at-stale")
        )
        post = self.enable(({"access_token": "at-fresh", "expires_in": 3600}, None))
        with mock.patch.object(ga.gemini_usage, "fetch_usage") as spawn:
            self.assertEqual(self.quiet(ga.cmd_refresh, "work"), 0)
        spawn.assert_not_called()  # no agy subprocess on the fast path

        saved = json.loads(profile.read_text())
        self.assertEqual(saved["access_token"], "at-fresh")
        self.assertEqual(saved["refresh_token"], "rt-old")  # Google never rotates it
        self.assertAlmostEqual(
            saved["expiry_date"] / 1000, time.time() + 3600, delta=5
        )
        # The POST is form-encoded and carries the four required fields.
        self.assertTrue(post.call_args.kwargs["form_encoded"])
        self.assertEqual(
            set(post.call_args.args[1]),
            {"client_id", "client_secret", "refresh_token", "grant_type"},
        )

    def test_revoked_token_reports_relogin_and_never_spawns_agy(self) -> None:
        self.write_profile("work", _creds("sub-w", "user@example.com"))
        self.enable((None, "revoked: refresh token rejected (invalid_grant)"))
        with mock.patch.object(ga.gemini_usage, "fetch_usage") as spawn:
            code, _, error = self.capture(ga.cmd_refresh, "work")
        self.assertEqual(code, 1)
        self.assertIn("login-switch work", error)
        spawn.assert_not_called()

    def test_transient_failure_falls_back_to_the_agy_path(self) -> None:
        profile = self.write_profile(
            "work", _creds("sub-w", "user@example.com", access_token="at-stale")
        )
        self.enable((None, "network error: timed out"))

        def refresh(*_args, **_kwargs) -> gu.UsageSnapshot:
            if self.active is None:
                self.fail("expected activated profile")
            self.active["access_token"] = "at-via-agy"
            return _usage(email="user@example.com")

        with mock.patch.object(ga.gemini_usage, "fetch_usage", side_effect=refresh):
            self.assertEqual(self.quiet(ga.cmd_refresh, "work"), 0)
        self.assertEqual(json.loads(profile.read_text())["access_token"], "at-via-agy")

    def test_active_profile_refresh_updates_the_keyring_blob_too(self) -> None:
        auth = _creds("sub-w", "user@example.com", access_token="at-stale")
        profile = self.write_profile("work", auth)
        self.set_active(auth)
        self.mark_current("work")
        self.enable(({"access_token": "at-fresh", "expires_in": 3600}, None))
        self.assertEqual(self.quiet(ga.cmd_refresh, "work"), 0)

        self.assertEqual(json.loads(profile.read_text())["access_token"], "at-fresh")
        if self.active is None:
            self.fail("expected a live session")
        self.assertEqual(self.active["access_token"], "at-fresh")
        # Round-tripping through the keyring encoding keeps the ms expiry intact.
        secret = ga._keyring_secret_from_auth(self.active)
        self.assertIsNotNone(secret)
        decoded = ga._auth_from_keyring_secret(secret or "")
        self.assertIsNotNone(decoded)
        self.assertAlmostEqual(
            (decoded or {})["expiry_date"] / 1000, time.time() + 3600, delta=5
        )

    def test_active_auth_refresh_syncs_back_without_agy(self) -> None:
        auth = _creds("sub-w", "user@example.com", access_token="at-stale")
        profile = self.write_profile("work", auth)
        self.set_active(auth)
        self.mark_current("work")
        self.enable(({"access_token": "at-fresh", "expires_in": 3600}, None))
        with mock.patch.object(ga.gemini_usage, "fetch_usage") as spawn:
            self.assertEqual(self.quiet(ga.cmd_refresh, None), 0)
        spawn.assert_not_called()
        self.assertEqual(json.loads(profile.read_text())["access_token"], "at-fresh")

    def test_switch_self_heals_an_expired_snapshot(self) -> None:
        profile = self.write_profile(
            "work",
            _creds(
                "sub-w", "user@example.com", access_token="at-dead", expires_in_ms=-60_000
            ),
        )
        self.enable(({"access_token": "at-fresh", "expires_in": 3600}, None))
        self.assertEqual(self.quiet(ga.cmd_switch, "work"), 0)
        self.assertEqual(json.loads(profile.read_text())["access_token"], "at-fresh")
        if self.active is None:
            self.fail("expected a live session")
        self.assertEqual(self.active["access_token"], "at-fresh")

    def test_switch_leaves_a_healthy_token_untouched(self) -> None:
        self.write_profile("work", _creds("sub-w", "user@example.com"))
        post = self.enable(({"access_token": "at-fresh", "expires_in": 3600}, None))
        self.assertEqual(self.quiet(ga.cmd_switch, "work"), 0)
        post.assert_not_called()

    def test_switch_onto_a_revoked_profile_still_switches_and_hints(self) -> None:
        self.write_profile(
            "work",
            _creds(
                "sub-w", "user@example.com", access_token="at-dead", expires_in_ms=-60_000
            ),
        )
        self.enable((None, "revoked: refresh token rejected (invalid_grant)"))
        code, output, error = self.capture(ga.cmd_switch, "work")
        self.assertEqual(code, 0)
        self.assertIn("login-switch work", ga._ANSI_RE.sub("", output + error))
        self.assertIsNotNone(self.active)


if __name__ == "__main__":
    unittest.main()
