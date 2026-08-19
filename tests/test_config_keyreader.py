"""Tests for the raw-mode single-keystroke reader (``_keyreader.py``).

All key decoding is driven by a faked low-level byte source
(``_keyreader._read_byte``) — no test here requires a real TTY. Platform
faking follows the project convention: ``mock.patch.object(kr, "IS_WINDOWS",
...)`` (see tests/test_autoswitch.py / tests/test_cross_platform.py).

Run with: ``uv run pytest tests/test_config_keyreader.py``.
"""

from __future__ import annotations

import unittest
from unittest import mock

from ai_accounts import _keyreader as kr


def _byte_source(*chunks: bytes):
    """Return a stand-in for ``_read_byte`` that yields ``chunks`` in order.

    Extra calls (beyond the given chunks) return ``None`` — mimicking "no
    more input available within timeout", which is exactly what the escape-
    sequence peek needs to distinguish a bare Escape from a sequence start.
    """
    queue = list(chunks)

    def fake(timeout=None):  # noqa: ANN001 - test double signature mirrors real one
        if not queue:
            return None
        return queue.pop(0)

    return fake


class KeyEventDecodingTests(unittest.TestCase):
    """AC: arrow/enter/escape/backspace/ctrl-c/printable decoding."""

    def test_arrow_up_posix_sequence(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x1b", b"[", b"A")):
            self.assertEqual(kr.read_key().key, kr.Key.UP)

    def test_arrow_down_posix_sequence(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x1b", b"[", b"B")):
            self.assertEqual(kr.read_key().key, kr.Key.DOWN)

    def test_arrow_right_posix_sequence(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x1b", b"[", b"C")):
            self.assertEqual(kr.read_key().key, kr.Key.RIGHT)

    def test_arrow_left_posix_sequence(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x1b", b"[", b"D")):
            self.assertEqual(kr.read_key().key, kr.Key.LEFT)

    def test_bare_escape_no_followup_bytes(self):
        # ESC with nothing following (peek times out) must NOT be mistaken
        # for the start of an arrow sequence.
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x1b")):
            self.assertEqual(kr.read_key().key, kr.Key.ESCAPE)

    def test_enter_cr(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\r")):
            self.assertEqual(kr.read_key().key, kr.Key.ENTER)

    def test_enter_lf(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\n")):
            self.assertEqual(kr.read_key().key, kr.Key.ENTER)

    def test_backspace_del(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x7f")):
            self.assertEqual(kr.read_key().key, kr.Key.BACKSPACE)

    def test_backspace_bs(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x08")):
            self.assertEqual(kr.read_key().key, kr.Key.BACKSPACE)

    def test_ctrl_c(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x03")):
            self.assertEqual(kr.read_key().key, kr.Key.CTRL_C)

    def test_printable_char(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"q")):
            event = kr.read_key()
            self.assertEqual(event.key, kr.Key.CHAR)
            self.assertEqual(event.char, "q")

    def test_unrecognized_escape_sequence_is_unknown(self):
        with mock.patch.object(kr, "_read_byte", _byte_source(b"\x1b", b"[", b"Z")):
            self.assertEqual(kr.read_key().key, kr.Key.UNKNOWN)

    def test_multi_byte_csi_sequence_does_not_leak_into_next_read(self):
        # PageUp (ESC [ 5 ~) must be fully consumed as one UNKNOWN keypress,
        # not leave "~" behind to be misread as a printable char next call.
        source = _byte_source(b"\x1b", b"[", b"5", b"~", b"x")
        with mock.patch.object(kr, "_read_byte", source):
            first = kr.read_key()
            second = kr.read_key()
        self.assertEqual(first.key, kr.Key.UNKNOWN)
        self.assertEqual(second.key, kr.Key.CHAR)
        self.assertEqual(second.char, "x")

    def test_windows_prefixed_arrow_up(self):
        with mock.patch.object(kr, "IS_WINDOWS", True):
            with mock.patch.object(kr, "_read_byte", _byte_source(b"\xe0", b"H")):
                self.assertEqual(kr.read_key().key, kr.Key.UP)

    def test_windows_prefixed_arrow_down(self):
        with mock.patch.object(kr, "IS_WINDOWS", True):
            with mock.patch.object(kr, "_read_byte", _byte_source(b"\x00", b"P")):
                self.assertEqual(kr.read_key().key, kr.Key.DOWN)

    def test_key_event_trivially_constructible_without_a_terminal(self):
        # Downstream consumers (a menu state machine) feed KeyEvent values
        # from a fake source in their own tests — must not need real I/O.
        event = kr.KeyEvent(kr.Key.CHAR, "x")
        self.assertEqual(event.key, kr.Key.CHAR)
        self.assertEqual(event.char, "x")


class RawModeRestorationTests(unittest.TestCase):
    """AC: terminal settings are always restored, including on exceptions."""

    def _fake_termios(self):
        fake_termios = mock.MagicMock()
        fake_termios.tcgetattr.return_value = ["ORIGINAL_ATTRS"]
        fake_tty = mock.MagicMock()
        return fake_termios, fake_tty

    def test_restores_settings_on_normal_exit(self):
        fake_termios, fake_tty = self._fake_termios()
        with mock.patch.object(kr, "IS_WINDOWS", False), mock.patch.object(
            kr, "_posix_modules", return_value=(fake_termios, fake_tty)
        ), mock.patch("os.isatty", return_value=True), mock.patch("sys.stdin.fileno", return_value=0):
            with kr.raw_mode():
                pass
        fake_termios.tcsetattr.assert_called_once_with(
            mock.ANY, fake_termios.TCSADRAIN, ["ORIGINAL_ATTRS"]
        )

    def test_restores_settings_on_keyboard_interrupt(self):
        fake_termios, fake_tty = self._fake_termios()
        with mock.patch.object(kr, "IS_WINDOWS", False), mock.patch.object(
            kr, "_posix_modules", return_value=(fake_termios, fake_tty)
        ), mock.patch("os.isatty", return_value=True), mock.patch("sys.stdin.fileno", return_value=0):
            with self.assertRaises(KeyboardInterrupt):
                with kr.raw_mode():
                    raise KeyboardInterrupt
        fake_termios.tcsetattr.assert_called_once_with(
            mock.ANY, fake_termios.TCSADRAIN, ["ORIGINAL_ATTRS"]
        )

    def test_restores_settings_on_arbitrary_exception(self):
        fake_termios, fake_tty = self._fake_termios()
        with mock.patch.object(kr, "IS_WINDOWS", False), mock.patch.object(
            kr, "_posix_modules", return_value=(fake_termios, fake_tty)
        ), mock.patch("os.isatty", return_value=True), mock.patch("sys.stdin.fileno", return_value=0):
            with self.assertRaises(RuntimeError):
                with kr.raw_mode():
                    raise RuntimeError("boom")
        fake_termios.tcsetattr.assert_called_once_with(
            mock.ANY, fake_termios.TCSADRAIN, ["ORIGINAL_ATTRS"]
        )

    def test_raw_mode_noop_when_not_a_tty(self):
        fake_termios, fake_tty = self._fake_termios()
        with mock.patch.object(kr, "IS_WINDOWS", False), mock.patch.object(
            kr, "_posix_modules", return_value=(fake_termios, fake_tty)
        ), mock.patch("os.isatty", return_value=False), mock.patch("sys.stdin.fileno", return_value=0):
            with kr.raw_mode():
                pass
        fake_termios.tcgetattr.assert_not_called()
        fake_termios.tcsetattr.assert_not_called()

    def test_raw_mode_noop_on_windows(self):
        with mock.patch.object(kr, "IS_WINDOWS", True):
            # Must not attempt to import termios/tty on Windows.
            with kr.raw_mode():
                pass

    def test_raw_mode_noop_when_stdin_has_no_fileno(self):
        # Redirected/captured stdin (e.g. pytest's default capture, or a
        # pipe) raises on fileno()/isatty() rather than returning False —
        # raw_mode() must treat that the same as "not a TTY", not crash.
        fake_termios, fake_tty = self._fake_termios()
        with mock.patch.object(kr, "IS_WINDOWS", False), mock.patch.object(
            kr, "_posix_modules", return_value=(fake_termios, fake_tty)
        ), mock.patch("sys.stdin.fileno", side_effect=OSError("no fileno")):
            with kr.raw_mode():
                pass
        fake_termios.tcgetattr.assert_not_called()
        fake_termios.tcsetattr.assert_not_called()

    def test_read_byte_posix_returns_none_when_stdin_has_no_fileno(self):
        with mock.patch.object(kr, "IS_WINDOWS", False), mock.patch(
            "sys.stdin.fileno", side_effect=OSError("no fileno")
        ):
            self.assertIsNone(kr._read_byte())


class ModuleImportabilityTests(unittest.TestCase):
    """AC: the module must import cleanly on every platform."""

    def test_module_usable_with_windows_flag_true(self):
        with mock.patch.object(kr, "IS_WINDOWS", True):
            self.assertTrue(callable(kr.read_key))

    def test_module_usable_with_windows_flag_false(self):
        with mock.patch.object(kr, "IS_WINDOWS", False):
            self.assertTrue(callable(kr.read_key))


class IsInteractiveTtyTests(unittest.TestCase):
    """AC: is_interactive_tty() is the single funnel, tested TTY and non-TTY."""

    def test_true_when_both_stdin_and_stdout_are_ttys(self):
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "sys.stdout.isatty", return_value=True
        ):
            self.assertTrue(kr.is_interactive_tty())

    def test_false_when_stdout_is_not_a_tty(self):
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch(
            "sys.stdout.isatty", return_value=False
        ):
            self.assertFalse(kr.is_interactive_tty())

    def test_false_when_stdin_is_not_a_tty(self):
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch(
            "sys.stdout.isatty", return_value=True
        ):
            self.assertFalse(kr.is_interactive_tty())

    def test_false_when_isatty_raises(self):
        with mock.patch("sys.stdin.isatty", side_effect=OSError):
            self.assertFalse(kr.is_interactive_tty())


class SpinnerReuseTests(unittest.TestCase):
    """AC: spinner/animation support reuses the existing _utils.Spinner as-is
    (a "saving…" indicator is just ``Spinner("Saving…")``) rather than
    _keyreader reimplementing or wrapping it."""

    def test_utils_spinner_is_the_saving_indicator(self):
        from ai_accounts._utils import Spinner

        indicator = Spinner("Saving…")
        self.assertIsInstance(indicator, Spinner)
        # Degrades to no-op animation when stderr isn't a TTY (the test
        # runner's captured stderr) — no crash, no output assumptions.
        with indicator:
            pass


if __name__ == "__main__":
    unittest.main()
