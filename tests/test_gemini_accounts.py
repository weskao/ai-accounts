from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import gemini_accounts as ga
from ai_accounts import gemini_usage as gu
from ai_accounts.codex_usage import UsageWindow


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

    def test_keyring_write_uses_encoded_keyring_secret(self) -> None:
        auth = _creds("sub", "a@x.com")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"ANTIGRAVITY_HOME": tmp}
        ), mock.patch.object(ga, "_store_keychain_secret", return_value=True) as store:
            self.assertTrue(ga._write_cli_auth_text(json.dumps(auth)))
        secret = store.call_args.args[0]
        self.assertTrue(secret.startswith("go-keyring-base64:"))
        self.assertNotIn(auth["access_token"], secret)


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

    def test_fetch_usage_without_agy_reports_error(self) -> None:
        with mock.patch.object(gu.shutil, "which", return_value=None):
            self.assertEqual(gu.fetch_usage().error, "agy not found")

    def test_relogin_error_has_an_actionable_label(self) -> None:
        snapshot = _usage(error="re-login required")
        self.assertEqual(gu.format_refreshed_at(snapshot), "RELOGIN")


class ProfileCommandTests(_HomeMixin):
    def test_save_persists_active_keyring_session(self) -> None:
        self.set_active(_creds("sub-w", "w@x.com", refresh_token="rt-live"))
        self.assertEqual(self.quiet(ga.cmd_save, "work"), 0)
        saved = json.loads((self.home / "accounts" / "work.json").read_text())
        self.assertEqual(saved["refresh_token"], "rt-live")

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

    def test_remove_current_profile_clears_marker(self) -> None:
        self.write_profile("work", _creds("sub", "a@x.com"))
        self.mark_current("work")
        self.assertEqual(self.quiet(ga.cmd_remove, "work"), 0)
        self.assertFalse((self.home / "accounts" / ".current-profile").exists())


class LoginAndRefreshTests(_HomeMixin):
    def test_login_switch_saves_session_when_agy_becomes_authenticated(self) -> None:
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
            ga.gemini_usage, "fetch_usage_from_pid",
            return_value=_usage("new@x.com"),
        ):
            self.assertEqual(self.quiet(ga.cmd_login_switch, "new"), 0)

        self.assertEqual(popen.call_args.args[0], ["agy"])
        process.terminate.assert_called_once_with()
        saved = json.loads((self.home / "accounts" / "new.json").read_text())
        self.assertEqual(saved["refresh_token"], "rt-new")

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


if __name__ == "__main__":
    unittest.main()
