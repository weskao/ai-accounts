"""Stdlib-only raw-mode single-keystroke reader for interactive menus.

Canonical here: decoding one keypress (arrows, Enter, Escape, Backspace,
Ctrl-C, printable chars) into a stable ``KeyEvent`` that callers can match on
without ever seeing raw escape bytes; putting the terminal into raw/cbreak
mode via ``raw_mode()`` with a *guaranteed* restore (including on
``KeyboardInterrupt`` and any other exception); and ``is_interactive_tty()``,
the single funnel a caller uses to decide whether an interactive menu is even
possible.

Delegated elsewhere: colors/log helpers and the ``Spinner`` animation live in
``_utils`` — a "saving…" indicator is just ``Spinner("Saving…")``, reused
as-is (it already degrades to nothing when stderr isn't a TTY), so this
module does not wrap or re-export it. Presentation (panels, tables, pickers)
lives in ``_present``.

Note on Ctrl-C: ``raw_mode()`` uses cbreak (not raw) mode, which leaves
``ISIG`` enabled, so a real terminal delivers Ctrl-C as ``SIGINT`` /
``KeyboardInterrupt`` rather than as a decodable ``\x03`` byte. ``Key.CTRL_C``
exists for completeness (and for fake byte sources in tests) — callers still
need to catch ``KeyboardInterrupt`` around ``read_key()``/``raw_mode()``.

Platform handling mirrors the project convention set by ``_utils`` (see
``IS_WINDOWS``/``IS_MACOS``/``IS_LINUX`` there): every OS-specific byte-read
is funnelled through the single ``_read_byte()`` function below, and
``termios``/``tty`` are imported lazily (never at module top level) so this
module is importable on Windows, and ``msvcrt`` is only touched when
``IS_WINDOWS`` is true — so it is importable on POSIX too.

Callers must never do their own ``sys.stdin.read()``/escape-sequence parsing;
route everything through ``read_key()`` so the ambiguity between a bare
Escape and the start of an arrow sequence, and the POSIX-vs-Windows arrow
encodings, are handled in exactly one place.
"""

from __future__ import annotations

import enum
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

from ._utils import IS_WINDOWS

# How long to wait for the rest of an escape sequence before concluding the
# user pressed a bare Escape key. Real terminals deliver a multi-byte arrow
# sequence effectively atomically; a human never types ESC, waits, then [.
_ESCAPE_SEQUENCE_TIMEOUT = 0.05


class Key(enum.Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    ENTER = "enter"
    ESCAPE = "escape"
    BACKSPACE = "backspace"
    CTRL_C = "ctrl_c"
    CHAR = "char"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class KeyEvent:
    """One decoded keypress. Trivially constructible without a terminal —
    downstream consumers (e.g. a menu state machine) feed these from a fake
    key source in their own tests."""

    key: Key
    char: str | None = None


# ── platform byte-read funnel ────────────────────────────────────────────────


def _read_byte_posix(timeout: float | None) -> bytes | None:
    import select

    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError):
        # stdin has no real fd (e.g. redirected/captured, as under pytest) —
        # nothing to read from this funnel.
        return None
    if timeout is not None:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
    return os.read(fd, 1)


def _read_byte_windows(timeout: float | None) -> bytes | None:
    import msvcrt

    if timeout is not None:
        deadline = time.monotonic() + timeout
        while not msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.001)
    return msvcrt.getch()


def _read_byte(timeout: float | None = None) -> bytes | None:
    """The single OS-specific funnel: read one raw byte from the keyboard.

    ``timeout=None`` blocks until a byte is available. ``timeout=<seconds>``
    returns ``None`` if nothing arrives in time — used only to disambiguate a
    bare Escape from the start of a multi-byte escape sequence. Tests replace
    this whole function with a fake byte source; nothing above this line
    needs a real terminal.
    """
    if IS_WINDOWS:
        return _read_byte_windows(timeout)
    return _read_byte_posix(timeout)


_POSIX_ARROW_MAP = {b"A": Key.UP, b"B": Key.DOWN, b"C": Key.RIGHT, b"D": Key.LEFT}
_WINDOWS_ARROW_MAP = {b"H": Key.UP, b"P": Key.DOWN, b"K": Key.LEFT, b"M": Key.RIGHT}


def read_key() -> KeyEvent:
    """Block for and decode exactly one keypress into a ``KeyEvent``."""
    first = _read_byte()
    if first is None:
        return KeyEvent(Key.UNKNOWN)

    if first == b"\x03":
        return KeyEvent(Key.CTRL_C)
    if first in (b"\r", b"\n"):
        return KeyEvent(Key.ENTER)
    if first in (b"\x7f", b"\x08"):
        return KeyEvent(Key.BACKSPACE)

    if IS_WINDOWS and first in (b"\x00", b"\xe0"):
        second = _read_byte()
        return KeyEvent(_WINDOWS_ARROW_MAP.get(second, Key.UNKNOWN))

    if first == b"\x1b":
        second = _read_byte(timeout=_ESCAPE_SEQUENCE_TIMEOUT)
        if second is None:
            return KeyEvent(Key.ESCAPE)
        if second == b"[":
            # CSI sequence: zero or more parameter/intermediate bytes
            # (0x20-0x3F, e.g. the "5" in PgUp's ESC[5~) followed by exactly
            # one final byte (0x40-0x7E). Consume the whole thing so an
            # unrecognized sequence (Home/End/PgUp/PgDn/modified arrows,
            # not decoded here) can't leak its tail bytes into the next
            # read_key() call as phantom keystrokes.
            final = _read_byte(timeout=_ESCAPE_SEQUENCE_TIMEOUT)
            while final is not None and 0x20 <= final[0] <= 0x3F:
                final = _read_byte(timeout=_ESCAPE_SEQUENCE_TIMEOUT)
            return KeyEvent(_POSIX_ARROW_MAP.get(final, Key.UNKNOWN))
        return KeyEvent(Key.UNKNOWN)

    try:
        char = first.decode("utf-8")
    except UnicodeDecodeError:
        return KeyEvent(Key.UNKNOWN)
    return KeyEvent(Key.CHAR, char)


# ── raw/cbreak mode ───────────────────────────────────────────────────────────


def _posix_modules():
    """Lazy import so this module stays importable where termios/tty don't
    exist (Windows). Split out as its own function purely so tests can
    substitute a fake termios/tty pair without a real terminal."""
    import termios
    import tty

    return termios, tty


@contextmanager
def raw_mode():
    """Put the terminal into cbreak mode for the duration of the ``with``
    block, guaranteeing the original settings are restored afterward —
    including when a ``KeyboardInterrupt`` or any other exception is raised
    inside the block. Mirrors the ``_suppress_interrupt_echo`` idiom in
    ``codex_accounts.py`` (raw termios, try/finally, no curses).

    No-op on Windows (``msvcrt`` reads keys without needing a mode switch)
    and when stdin isn't a real TTY (piped input, tests, CI).
    """
    if IS_WINDOWS:
        yield
        return

    try:
        termios, tty = _posix_modules()
    except ImportError:
        yield
        return

    try:
        fd = sys.stdin.fileno()
        is_tty = os.isatty(fd)
    except (OSError, ValueError):
        # stdin has no real fd (redirected/captured stdin, e.g. pytest's
        # default capture) — nothing to put into raw mode.
        yield
        return
    if not is_tty:
        yield
        return

    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


# ── interactivity funnel ──────────────────────────────────────────────────────


def is_interactive_tty() -> bool:
    """The single funnel for "can we run an interactive keyboard menu?".

    Requires BOTH stdin and stdout to be real TTYs: stdin must be a TTY to
    read keys via ``read_key()``/``raw_mode()``, and stdout must be a TTY to
    redraw the menu in place (cursor moves, line clears). Either stream being
    redirected (piped output, `| cat`, CI logs, non-interactive tests) means
    a keyboard-driven menu can't work, so callers should fall back to a
    plain prompt instead.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False
