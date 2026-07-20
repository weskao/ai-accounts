"""Cross-platform dispatch tests for ai_accounts._utils.

These verify that clipboard and dependency-install logic pick the right
mechanism for macOS, Windows, and Linux — without touching the real OS
clipboard or package managers. Run with: ``python -m unittest discover tests``.
"""

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
from ai_accounts import vcadd


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


class ClipboardDispatchTests(_PlatformMixin, unittest.TestCase):
    def _record_pipe(self):
        calls = []

        def fake_pipe(cmd, data):
            calls.append((list(cmd), data))
            return True

        p = mock.patch.object(u, "_pipe_to", side_effect=fake_pipe)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_macos_uses_pbcopy(self):
        self.force_platform(macos=True)
        calls = self._record_pipe()
        self.assertTrue(u.copy_to_clipboard("héllo 你好"))
        self.assertEqual(calls[0][0], ["pbcopy"])
        self.assertEqual(calls[0][1], "héllo 你好".encode("utf-8"))

    def test_windows_uses_win32_api(self):
        self.force_platform(windows=True)
        with mock.patch.object(u, "_windows_set_clipboard", return_value=True) as win, \
                mock.patch.object(u, "_pipe_to") as pipe:
            self.assertTrue(u.copy_to_clipboard("你好"))
            win.assert_called_once_with("你好")
            pipe.assert_not_called()  # no fallback needed when Win32 path works

    def test_windows_falls_back_to_clip(self):
        self.force_platform(windows=True)
        calls = self._record_pipe()
        with mock.patch.object(u, "_windows_set_clipboard", return_value=False):
            self.assertTrue(u.copy_to_clipboard("ascii"))
        self.assertEqual(calls[0][0], ["clip"])

    def test_linux_prefers_xclip(self):
        self.force_platform(linux=True)
        calls = self._record_pipe()
        with mock.patch.object(u, "have", side_effect=lambda c: c == "xclip"), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(u.copy_to_clipboard("text"))
        self.assertEqual(calls[0][0], ["xclip", "-selection", "clipboard"])

    def test_linux_falls_back_to_xsel(self):
        self.force_platform(linux=True)
        calls = self._record_pipe()
        with mock.patch.object(u, "have", side_effect=lambda c: c == "xsel"), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(u.copy_to_clipboard("text"))
        self.assertEqual(calls[0][0], ["xsel", "--clipboard", "--input"])

    def test_linux_wayland_uses_wl_copy(self):
        self.force_platform(linux=True)
        calls = self._record_pipe()
        with mock.patch.object(u, "have", side_effect=lambda c: c == "wl-copy"), \
                mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True):
            self.assertTrue(u.copy_to_clipboard("text"))
        self.assertEqual(calls[0][0], ["wl-copy"])

    def test_linux_no_clipboard_tool_returns_false(self):
        self.force_platform(linux=True)
        with mock.patch.object(u, "have", return_value=False), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(u.copy_to_clipboard("text"))


class EnsureToolTests(_PlatformMixin, unittest.TestCase):
    def test_present_tool_returns_true(self):
        self.force_platform(linux=True)
        with mock.patch.object(u, "have", return_value=True):
            self.assertTrue(u.ensure_tool("pandoc"))

    def test_linux_missing_tool_prints_hint_and_fails(self):
        self.force_platform(linux=True)
        buf = io.StringIO()
        with mock.patch.object(u, "have", return_value=False), redirect_stderr(buf):
            self.assertFalse(u.ensure_tool("pandoc"))
        out = buf.getvalue()
        self.assertIn("not found", out)
        self.assertIn("apt install pandoc", out)

    def test_windows_missing_tool_prints_windows_hint(self):
        self.force_platform(windows=True)
        buf = io.StringIO()
        with mock.patch.object(u, "have", return_value=False), redirect_stderr(buf):
            self.assertFalse(u.ensure_tool("imagemagick", "magick"))
        out = buf.getvalue()
        self.assertIn("magick", out)
        self.assertIn("winget", out)

    def test_macos_missing_without_brew_prints_brew_hint(self):
        self.force_platform(macos=True)
        buf = io.StringIO()
        with mock.patch.object(u, "have", side_effect=lambda c: False), redirect_stderr(buf):
            self.assertFalse(u.ensure_tool("pngquant"))
        out = buf.getvalue()
        self.assertIn("Homebrew", out)

    def test_install_hint_per_platform(self):
        self.force_platform(macos=True)
        self.assertEqual(u._install_hint("gifsicle"), "brew install gifsicle")
        self.force_platform(windows=True)
        self.assertIn("scoop", u._install_hint("gifsicle"))
        self.force_platform(linux=True)
        self.assertIn("apt install gifsicle", u._install_hint("gifsicle"))

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


class PlatformLimitedCommandTests(unittest.TestCase):
    def test_agy_accounts_fails_cleanly_outside_macos(self):
        error = io.StringIO()
        with mock.patch.object(ga.sys, "platform", "win32"), redirect_stderr(error):
            self.assertEqual(ga.main(["who"]), 1)
        self.assertIn("requires macOS Keychain", error.getvalue())

    def test_agy_help_remains_available_outside_macos(self):
        with mock.patch.object(ga.sys, "platform", "linux"):
            self.assertEqual(ga.main(["--help"]), 0)

    def test_agy_usage_without_posix_modules_returns_error(self):
        with mock.patch.object(gu.os, "name", "nt"):
            usage = gu.fetch_usage()
        self.assertEqual(usage.error, "agy usage inspection requires macOS or Linux")

    def test_missing_macos_security_command_does_not_raise(self):
        with mock.patch.object(ga.subprocess, "run", side_effect=FileNotFoundError):
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

    def test_vcadd_fails_cleanly_outside_macos(self):
        error = io.StringIO()
        with mock.patch.object(vcadd.sys, "platform", "win32"), redirect_stderr(error):
            self.assertEqual(vcadd.main(["測試"]), 1)
        self.assertIn("requires macOS", error.getvalue())

    def test_vcadd_help_remains_available_outside_macos(self):
        with mock.patch.object(vcadd.sys, "platform", "win32"):
            self.assertEqual(vcadd.main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
