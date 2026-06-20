"""Cross-platform dispatch tests for ai_accounts._utils.

These verify that clipboard and dependency-install logic pick the right
mechanism for macOS, Windows, and Linux — without touching the real OS
clipboard or package managers. Run with: ``python -m unittest discover tests``.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ai_accounts import _utils as u


class _PlatformMixin:
    """Force a given platform by patching the module-level OS flags."""

    def force_platform(self, *, macos=False, windows=False, linux=False):
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


if __name__ == "__main__":
    unittest.main()
