"""Cross-platform dispatch tests for account storage and credentials."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from ai_accounts import _utils as u
from ai_accounts import claude_accounts as ca
from ai_accounts import gemini_accounts as ga
from ai_accounts import gemini_usage as gu


class _PlatformMixin:
    """Force a given platform by patching the module-level OS flags."""

    def force_platform(self, *, macos=False, windows=False, linux=False):
        if not isinstance(self, unittest.TestCase):
            raise TypeError("platform mixin requires unittest.TestCase")
        patches = [
            mock.patch.object(u, "IS_MACOS", macos),
            mock.patch.object(u, "IS_WINDOWS", windows),
            mock.patch.object(u, "IS_LINUX", linux),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class EnsureToolTests(_PlatformMixin, unittest.TestCase):
    def test_present_tool_returns_true(self):
        self.force_platform(linux=True)
        with mock.patch.object(u, "have", return_value=True):
            self.assertTrue(u.ensure_tool("codex"))

    def test_linux_missing_tool_prints_hint_and_fails(self):
        self.force_platform(linux=True)
        buf = io.StringIO()
        with mock.patch.object(u, "have", return_value=False), redirect_stderr(buf):
            self.assertFalse(u.ensure_tool("claude"))
        out = buf.getvalue()
        self.assertIn("not found", out)
        self.assertIn("claude.ai/install.sh", out)

    def test_windows_missing_tool_prints_windows_hint(self):
        self.force_platform(windows=True)
        buf = io.StringIO()
        with mock.patch.object(u, "have", return_value=False), redirect_stderr(buf):
            self.assertFalse(u.ensure_tool("codex"))
        out = buf.getvalue()
        self.assertIn("codex", out)
        self.assertIn("npm install", out)

    def test_macos_missing_without_brew_prints_brew_hint(self):
        self.force_platform(macos=True)
        buf = io.StringIO()
        with mock.patch.object(u, "have", side_effect=lambda c: False), redirect_stderr(buf):
            self.assertFalse(u.ensure_tool("codex"))
        out = buf.getvalue()
        self.assertIn("Homebrew", out)

    def test_install_hint_per_platform(self):
        self.force_platform(macos=True)
        self.assertIn("npm install", u._install_hint("codex"))
        self.force_platform(windows=True)
        self.assertIn("npm install", u._install_hint("codex"))
        self.force_platform(linux=True)
        self.assertIn("npm install", u._install_hint("codex"))

    def test_unknown_package_has_generic_hint(self):
        self.force_platform(linux=True)
        self.assertIn("package manager", u._install_hint("totally-unknown-tool"))


class ResolveAccountDirTests(unittest.TestCase):
    """The central profile-store resolver uses only os.environ / pathlib /
    shutil — no POSIX-only calls — so behavior is identical on macOS,
    Windows, and Linux. These tests pin that behavior."""

    def test_env_override_wins_and_skips_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "dot" / "accounts"
            legacy.mkdir(parents=True)
            (legacy / "a.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"X_ACCOUNT_DIR": str(root / "override")}):
                resolved = u.resolve_account_dir("X_ACCOUNT_DIR", root / "central", legacy)
            self.assertEqual(resolved, root / "override")
            self.assertTrue((legacy / "a.json").exists())  # legacy untouched

    def test_default_used_when_env_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {}, clear=True):
                resolved = u.resolve_account_dir("X_ACCOUNT_DIR", root / "central", root / "legacy")
            self.assertEqual(resolved, root / "central")

    def test_legacy_store_moves_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "dot" / "accounts"
            legacy.mkdir(parents=True)
            (legacy / "a.json").write_text("{}", encoding="utf-8")
            (legacy / ".current-profile").write_text("a", encoding="utf-8")
            central = root / "central" / "accounts"
            with mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()):
                resolved = u.resolve_account_dir("X_ACCOUNT_DIR", central, legacy)
            self.assertEqual(resolved, central)
            self.assertTrue((central / "a.json").is_file())
            self.assertEqual((central / ".current-profile").read_text(encoding="utf-8"), "a")
            self.assertFalse(legacy.exists())

    def test_existing_default_never_clobbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "old.json").write_text("{}", encoding="utf-8")
            central = root / "central"
            central.mkdir()
            (central / "new.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                resolved = u.resolve_account_dir("X_ACCOUNT_DIR", central, legacy)
            self.assertEqual(resolved, central)
            self.assertTrue((central / "new.json").exists())
            self.assertTrue((legacy / "old.json").exists())  # never merged/overwritten


class GoKeyringDispatchTests(_PlatformMixin, unittest.TestCase):
    """The go-keyring slot trio must speak each platform's own dialect."""

    SERVICE = "gemini"
    ACCOUNT = "antigravity"
    SECRET = '{"token":{"access_token":"tok"}}'

    def test_macos_wraps_the_secret_in_the_go_keyring_marker(self):
        self.force_platform(macos=True)
        with mock.patch.object(u, "keychain_write", return_value=True) as write:
            self.assertTrue(u.go_keyring_write(self.SERVICE, self.ACCOUNT, self.SECRET))
        stored = write.call_args.args[2]
        self.assertTrue(stored.startswith("go-keyring-base64:"))
        self.assertNotIn("tok", stored)  # the payload really is encoded

        with mock.patch.object(u, "keychain_read", return_value=stored):
            self.assertEqual(u.go_keyring_read(self.SERVICE, self.ACCOUNT), self.SECRET)

    def test_macos_read_keeps_an_unmarked_secret_verbatim(self):
        self.force_platform(macos=True)
        with mock.patch.object(u, "keychain_read", return_value=self.SECRET):
            self.assertEqual(u.go_keyring_read(self.SERVICE, self.ACCOUNT), self.SECRET)

    def test_macos_read_rejects_a_corrupt_marked_secret(self):
        self.force_platform(macos=True)
        with mock.patch.object(u, "keychain_read", return_value="go-keyring-base64:!!"):
            self.assertIsNone(u.go_keyring_read(self.SERVICE, self.ACCOUNT))

    def _fake_secret_tool(self, stdout="", returncode=0):
        calls = []

        def run(cmd, **kwargs):
            calls.append((list(cmd), kwargs.get("input")))
            return u.subprocess.CompletedProcess(cmd, returncode, stdout, "")

        p = mock.patch.object(u.subprocess, "run", side_effect=run)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(u, "have", return_value=True)
        q.start()
        self.addCleanup(q.stop)
        return calls

    def test_linux_stores_the_secret_verbatim_via_secret_tool(self):
        self.force_platform(linux=True)
        calls = self._fake_secret_tool()
        self.assertTrue(u.go_keyring_write(self.SERVICE, self.ACCOUNT, self.SECRET))
        cmd, stdin = calls[0]
        self.assertEqual(cmd[:2], ["secret-tool", "store"])
        self.assertIn("service", cmd)
        self.assertIn(self.SERVICE, cmd)
        self.assertIn("username", cmd)
        self.assertIn(self.ACCOUNT, cmd)
        self.assertEqual(stdin, self.SECRET)  # no marker, no base64

    def test_linux_lookup_and_clear_match_on_both_attributes(self):
        self.force_platform(linux=True)
        calls = self._fake_secret_tool(stdout=self.SECRET + "\n")
        self.assertEqual(u.go_keyring_read(self.SERVICE, self.ACCOUNT), self.SECRET)
        self.assertTrue(u.go_keyring_delete(self.SERVICE, self.ACCOUNT))
        self.assertEqual(
            calls[0][0],
            ["secret-tool", "lookup", "service", self.SERVICE, "username", self.ACCOUNT],
        )
        self.assertEqual(
            calls[1][0],
            ["secret-tool", "clear", "service", self.SERVICE, "username", self.ACCOUNT],
        )

    def test_linux_without_secret_tool_is_unavailable_with_a_hint(self):
        self.force_platform(linux=True)
        with mock.patch.object(u, "have", return_value=False):
            reachable, reason = u.go_keyring_available()
            self.assertIsNone(u.go_keyring_read(self.SERVICE, self.ACCOUNT))
            self.assertFalse(u.go_keyring_write(self.SERVICE, self.ACCOUNT, self.SECRET))
        self.assertFalse(reachable)
        self.assertIn("libsecret", reason)

    def test_windows_uses_the_service_colon_account_target(self):
        self.force_platform(windows=True)
        with mock.patch.object(u, "_wincred_read", return_value=self.SECRET) as read, \
                mock.patch.object(u, "_wincred_write", return_value=True) as write, \
                mock.patch.object(u, "_wincred_delete", return_value=True) as delete:
            self.assertEqual(u.go_keyring_read(self.SERVICE, self.ACCOUNT), self.SECRET)
            self.assertTrue(u.go_keyring_write(self.SERVICE, self.ACCOUNT, self.SECRET))
            self.assertTrue(u.go_keyring_delete(self.SERVICE, self.ACCOUNT))
        self.assertEqual(read.call_args.args[0], "gemini:antigravity")
        self.assertEqual(write.call_args.args, ("gemini:antigravity", self.ACCOUNT, self.SECRET))
        self.assertEqual(delete.call_args.args[0], "gemini:antigravity")
        self.assertEqual(u.go_keyring_available(), (True, ""))

    def test_macos_and_windows_need_no_extra_binary(self):
        self.force_platform(macos=True)
        self.assertEqual(u.go_keyring_available(), (True, ""))

    def test_unknown_platform_degrades_instead_of_raising(self):
        self.force_platform()  # none of the three
        self.assertIsNone(u.go_keyring_read(self.SERVICE, self.ACCOUNT))
        self.assertFalse(u.go_keyring_write(self.SERVICE, self.ACCOUNT, self.SECRET))
        self.assertFalse(u.go_keyring_delete(self.SERVICE, self.ACCOUNT))
        self.assertFalse(u.go_keyring_available()[0])


class AgyLinuxSlotTests(_PlatformMixin, unittest.TestCase):
    """agy-accounts' own shims must reach the Linux slot, not just _utils."""

    SECRET = '{"token":{"access_token":"tok","refresh_token":"ref"},"auth_method":"consumer"}'

    def test_read_and_store_go_through_secret_tool_on_linux(self):
        self.force_platform(linux=True)
        calls = []

        def run(cmd, **kwargs):
            calls.append((list(cmd), kwargs.get("input")))
            return u.subprocess.CompletedProcess(cmd, 0, self.SECRET, "")

        with mock.patch.object(u.subprocess, "run", side_effect=run), \
                mock.patch.object(u, "have", return_value=True):
            self.assertEqual(ga._read_cli_keyring_secret(), self.SECRET)
            self.assertTrue(ga._store_keychain_secret(self.SECRET))
            self.assertTrue(ga._delete_cli_auth())

        verbs = [cmd[1] for cmd, _ in calls]
        self.assertEqual(verbs, ["lookup", "store", "clear"])
        for cmd, _ in calls:
            self.assertEqual(cmd[0], "secret-tool")
            self.assertIn("gemini", cmd)
            self.assertIn("antigravity", cmd)
        self.assertEqual(calls[1][1], self.SECRET)  # stored verbatim, no marker

    def test_a_linux_session_decodes_into_usable_auth(self):
        self.force_platform(linux=True)
        with mock.patch.object(u, "have", return_value=True), mock.patch.object(
            u.subprocess,
            "run",
            return_value=u.subprocess.CompletedProcess([], 0, self.SECRET, ""),
        ):
            secret = ga._read_cli_keyring_secret()
        auth = ga._auth_from_keyring_secret(secret or "")
        self.assertIsNotNone(auth)
        assert auth is not None
        self.assertEqual(auth["access_token"], "tok")
        self.assertEqual(auth["refresh_token"], "ref")


class PlatformLimitedCommandTests(unittest.TestCase):
    def test_agy_accounts_fails_cleanly_without_a_credential_store(self):
        error = io.StringIO()
        with mock.patch.object(
            ga, "go_keyring_available", return_value=(False, "sudo apt install libsecret-tools")
        ), redirect_stderr(error):
            self.assertEqual(ga.main(["who"]), 1)
        self.assertIn("OS credential store", error.getvalue())
        self.assertIn("libsecret-tools", error.getvalue())

    def test_agy_help_remains_available_without_a_credential_store(self):
        with mock.patch.object(ga, "go_keyring_available", return_value=(False, "nope")):
            self.assertEqual(ga.main(["--help"]), 0)

    def test_agy_usage_without_posix_modules_returns_error(self):
        with mock.patch.object(gu.os, "name", "nt"):
            usage = gu.fetch_usage()
        self.assertEqual(usage.error, "agy usage inspection requires macOS or Linux")

    def test_missing_macos_security_command_does_not_raise(self):
        # Pin the macOS go-keyring branch so every platform exercises the
        # mocked `security` path (off macOS the real branch is ctypes /
        # secret-tool and would touch the host credential store).
        with (
            mock.patch.object(u, "IS_MACOS", True),
            mock.patch.object(ga.subprocess, "run", side_effect=FileNotFoundError),
        ):
            self.assertIsNone(ga._read_cli_keyring_secret())
            self.assertFalse(ga._store_keychain_secret("secret"))
            self.assertFalse(ga._delete_cli_auth())

    def test_claude_help_remains_available_everywhere(self):
        with mock.patch.object(ca.sys, "platform", "win32"):
            self.assertEqual(ca.main(["--help"]), 0)

    def test_claude_keychain_disabled_off_macos(self):
        with mock.patch.object(ca.sys, "platform", "linux"):
            self.assertIsNone(ca._keychain_account())

    def test_claude_missing_security_command_does_not_raise(self):
        with (
            mock.patch.object(ca, "_keychain_account", return_value="user"),
            mock.patch.object(ca.subprocess, "run", side_effect=FileNotFoundError),
        ):
            self.assertIsNone(ca._read_keychain_creds())
            self.assertFalse(ca._write_keychain_creds("secret"))



if __name__ == "__main__":
    unittest.main()
