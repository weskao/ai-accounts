from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import autoswitch as aw
from ai_accounts import vibe_accounts as va
from ai_accounts._present import _ANSI_RE


def _auth(key: str = "sk-fake-1234567890abcdef") -> dict[str, str]:
    return {
        "MISTRAL_API_KEY": key,
        "SOME_OTHER_VAR": "value",
    }


class VibeAccountsTests(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory[str] | None = None
    home: Path = Path()
    vibe_home: Path = Path()
    account_dir: Path = Path()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.vibe_home = self.home / ".vibe"
        self.account_dir = self.home / ".ai-accounts" / "vibe" / "accounts"
        environment = mock.patch.dict(
            os.environ,
            {
                "VIBE_HOME": str(self.vibe_home),
                "VIBE_ACCOUNT_DIR": str(self.account_dir),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_save_switch_and_sync_manage_real_auth_shape(self) -> None:
        # Create active .env
        self.assertTrue(va._write_env(va._auth_file(), _auth()))

        # Save active .env as "personal" profile
        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_save("personal"), 0)
        self.assertTrue((self.account_dir / "personal.json").is_file())

        # Change active .env
        self.assertTrue(
            va._write_env(va._auth_file(), _auth("sk-fake-different-key"))
        )

        # Switch back to "personal"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_switch("personal"), 0)
            self.assertEqual(va.cmd_sync(), 0)

        # Check that the switched/synced key matches the personal profile
        active_env = va._read_env(va._auth_file())
        self.assertEqual(active_env["MISTRAL_API_KEY"], "sk-fake-1234567890abcdef")
        self.assertEqual(
            (self.account_dir / ".current-profile").read_text(), "personal"
        )

    def test_switch_keeps_backup_in_ai-accounts_store(self) -> None:
        self.assertTrue(va._write_env(va._auth_file(), _auth("sk-old-key")))
        self.assertTrue(va._write_json(self.account_dir / "personal.json", _auth()))

        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_switch("personal"), 0)

        self.assertTrue(list((self.account_dir.parent / "backups").glob(".env.backup-*.json")))

    def test_usage_shows_only_active_profile(self) -> None:
        self.assertTrue(va._write_env(va._auth_file(), _auth()))
        self.assertTrue(va._write_json(self.account_dir / "personal.json", _auth()))
        self.assertTrue(
            va._write_json(
                self.account_dir / "work.json",
                _auth("sk-work-key"),
            )
        )
        (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(va.cmd_list(only_active=True), 0)
        text = _ANSI_RE.sub("", output.getvalue())
        self.assertIn("Current Vibe account", text)
        self.assertEqual(text.count("ACTIVE"), 1)
        self.assertIn("personal", text)
        self.assertNotIn("work", text)

    def test_usage_reports_when_no_active_profile(self) -> None:
        self.assertTrue(va._write_json(self.account_dir / "saved.json", _auth()))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(va.cmd_list(only_active=True), 0)
        self.assertNotIn("PROFILE", _ANSI_RE.sub("", out.getvalue()))  # no table
        self.assertIn("No active Vibe account", _ANSI_RE.sub("", err.getvalue()))

    def test_list_never_prints_tokens(self) -> None:
        self.assertTrue(va._write_json(self.account_dir / "personal.json", _auth()))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(va.cmd_list(), 0)
        listing = output.getvalue()
        self.assertIn("personal", listing)
        self.assertIn("sk-fake-...", listing)
        self.assertNotIn("1234567890abcdef", listing)

    def test_remove_no_args_opens_interactive_picker_and_removes(self) -> None:
        self.assertTrue(va._write_json(self.account_dir / "personal.json", _auth()))
        self.assertTrue(
            va._write_json(
                self.account_dir / "work.json",
                _auth("sk-work-key"),
            )
        )

        with mock.patch("builtins.input", return_value="2"):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = va.cmd_remove_interactive()

        text = _ANSI_RE.sub("", out.getvalue())
        self.assertEqual(rc, 0)
        self.assertIn("1) personal", text)
        self.assertIn("2) work", text)
        self.assertFalse((self.account_dir / "work.json").is_file())
        self.assertTrue((self.account_dir / "personal.json").is_file())

    def test_main_dispatches_remove_no_args_to_interactive_picker(self) -> None:
        with mock.patch.object(va, "cmd_remove_interactive", return_value=0) as interactive:
            rc = va.main(["remove"])

        self.assertEqual(rc, 0)
        interactive.assert_called_once_with()

    def test_save_no_args_derives_name_from_email_or_fallback(self) -> None:
        self.assertTrue(va._write_env(va._auth_file(), _auth()))
        with mock.patch("subprocess.run") as run_mock:
            # Mock git user.email returning "test@example.com"
            proc = mock.MagicMock()
            proc.returncode = 0
            proc.stdout = "test@example.com\n"
            run_mock.return_value = proc

            with redirect_stdout(io.StringIO()):
                rc = va.main(["save"])

        self.assertEqual(rc, 0)
        self.assertTrue((self.account_dir / "test.json").is_file())

    def test_openai_api_key_uses_same_identity_for_switch_and_sync(self) -> None:
        payload = {"OPENAI_API_KEY": "sk-openai-compatible-key"}
        self.assertTrue(va._write_json(self.account_dir / "personal.json", payload))
        self.assertTrue(va._write_env(va._auth_file(), payload))
        self.assertTrue(va._active_profile(), "the active profile should be detected")

        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_sync(), 0)

        self.assertEqual(va._read_json(self.account_dir / "personal.json"), payload)


class VibeAutoswitchTests(unittest.TestCase):
    def test_autoswitch_reports_unsupported_and_exits_zero(self) -> None:
        out, err = io.StringIO(), io.StringIO()

        with (
            mock.patch.object(aw, "run_autoswitch") as engine,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = va.main(["autoswitch"])

        self.assertEqual(rc, 0)
        self.assertEqual(
            _ANSI_RE.sub("", out.getvalue()).splitlines(),
            ["autoswitch unsupported for vibe: no quota API"],
        )
        self.assertEqual(err.getvalue(), "")
        engine.assert_not_called()
