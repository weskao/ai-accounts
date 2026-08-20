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
        self._install_fake_keychain()

    def _install_fake_keychain(self, *, writable: bool = True) -> None:
        """Stand in for the login keychain. Without this the suite would write
        test keys into the developer's real `ai.mistral.vibe` item and log them
        out of Vibe. ``writable=False`` models a host with no keychain at all
        (Linux/Windows), where .env is the only store."""
        self.keychain: dict[tuple[str, str], str] = {}

        def fake_read(service: str, account: str) -> str | None:
            return self.keychain.get((service, account))

        def fake_write(service: str, account: str, secret: str) -> bool:
            if not writable:
                return False
            self.keychain[(service, account)] = secret
            return True

        for name, fake in (("keychain_read", fake_read), ("keychain_write", fake_write)):
            patcher = mock.patch.object(va, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _set_keychain_key(self, key: str = "sk-fake-1234567890abcdef") -> None:
        self.keychain[(va._KEYCHAIN_SERVICE, "MISTRAL_API_KEY")] = key

    def test_save_reads_the_key_vibe_left_in_the_keychain(self) -> None:
        # `vibe --setup` stores the key in the keyring and deletes the plaintext
        # .env copy, so there is no .env at all — the state save/who/list used
        # to report as "not logged in".
        self._set_keychain_key()
        self.assertFalse(va._auth_file().exists())

        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_save("personal"), 0)
            self.assertEqual(va.cmd_who(), 0)

        self.assertEqual(
            va._read_json(self.account_dir / "personal.json"),
            {"MISTRAL_API_KEY": "sk-fake-1234567890abcdef"},
        )

    def test_switch_updates_the_keychain_and_drops_the_stale_env_copy(self) -> None:
        self._set_keychain_key("sk-old-key")
        # A leftover plaintext key outranks the keyring in vibe's own lookup, so
        # a switch that ignored it would be silently undone.
        self.assertTrue(va._write_env(va._auth_file(), _auth("sk-old-key")))
        self.assertTrue(va._write_json(self.account_dir / "personal.json", _auth()))

        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_switch("personal"), 0)

        self.assertEqual(
            self.keychain[(va._KEYCHAIN_SERVICE, "MISTRAL_API_KEY")],
            "sk-fake-1234567890abcdef",
        )
        env = va._read_env(va._auth_file())
        self.assertNotIn("MISTRAL_API_KEY", env)
        self.assertEqual(env["SOME_OTHER_VAR"], "value")  # unrelated vars survive
        self.assertEqual(va._read_active(), {"MISTRAL_API_KEY": "sk-fake-1234567890abcdef"})

    def test_switch_falls_back_to_env_file_without_a_keychain(self) -> None:
        self._install_fake_keychain(writable=False)
        self.assertTrue(va._write_json(self.account_dir / "personal.json", _auth()))

        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_switch("personal"), 0)

        self.assertEqual(
            va._read_env(va._auth_file())["MISTRAL_API_KEY"], "sk-fake-1234567890abcdef"
        )

    def test_save_switch_and_sync_manage_real_auth_shape(self) -> None:
        # Create active .env
        self.assertTrue(va._write_env(va._auth_file(), _auth()))

        # Save active .env as "personal" profile
        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_save("personal"), 0)
        self.assertTrue((self.account_dir / "personal.json").is_file())

        # Change the active credential
        self.assertTrue(
            va._write_env(va._auth_file(), _auth("sk-fake-different-key"))
        )

        # Switch back to "personal"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.cmd_switch("personal"), 0)
            self.assertEqual(va.cmd_sync(), 0)

        # Check that the switched/synced key matches the personal profile
        self.assertEqual(
            va._read_active(), {"MISTRAL_API_KEY": "sk-fake-1234567890abcdef"}
        )
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
        self.assertIn("MISTRAL_API_KEY", listing)
        self.assertIn("cdef", listing)  # masked tail only
        self.assertNotIn("1234567890abcdef", listing)
        self.assertNotIn("sk-fake", listing)

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

    def test_save_no_args_derives_a_per_key_name(self) -> None:
        # Vibe exposes no account email, so a keyless save names the profile
        # after a digest of the key — two accounts must not collide on one name.
        self.assertTrue(va._write_env(va._auth_file(), _auth()))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.main(["save"]), 0)
        first = va._derived_name(_auth())
        self.assertTrue((self.account_dir / f"{first}.json").is_file())

        self.assertTrue(va._write_env(va._auth_file(), _auth("sk-fake-second-account")))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(va.main(["save"]), 0)
        second = va._derived_name(_auth("sk-fake-second-account"))
        self.assertNotEqual(first, second)
        self.assertTrue((self.account_dir / f"{second}.json").is_file())

    def test_save_reports_when_signed_out(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(va.main(["save"]), 1)
        self.assertIn("No valid Mistral API key", _ANSI_RE.sub("", err.getvalue()))

    def test_login_switch_without_a_name_prints_usage(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(va.main(["login-switch"]), 1)
        self.assertIn("login-switch <profile_name>", _ANSI_RE.sub("", err.getvalue()))

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
