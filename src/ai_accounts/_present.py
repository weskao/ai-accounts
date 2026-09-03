"""Shared terminal-presentation helpers for the account tools.

Canonical rendering extracted from ``codex-accounts`` (the first account tool;
its box-drawing panels, tables, usage colors, interactive picker, and
success-message grammar are the reference the sibling tools adopt). Everything
here is pure presentation — no auth — so the per-provider modules stay focused
on their own auth logic. Its I/O is ``print``/``input`` plus, for the layout
resolver, one cached read of the ``layout`` setting (``auto``/``wide``/
``narrow``) and — under ``auto`` — a fresh, uncached measurement of the
terminal width per call, so a resize is followed rather than frozen; ``auto``
renders the stacked narrow variants below 60 columns so every table and panel
fits a phone-sized terminal.

These helpers render whatever they are given and do NOT redact: callers must
pre-filter to non-secret claim fields (never pass tokens, refresh tokens, or
raw auth payloads into panel lines, table cells, picker items, or messages).

Cross-cutting color/log primitives live in ``_utils``; the usage-cell
formatting/alignment (shared with the non-interactive usage modules) lives in
``usage_format``. This module composes those, it does not duplicate them.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from typing import Callable, Sequence

from . import usage_format
from ._utils import BOLD, CYAN, DIM, GREEN, RESET, RED, YELLOW, log_red, log_yellow

# ANSI escape stripper — the single source of truth for measuring the visible
# width of a colored cell. Sibling tools that still keep a local copy migrate
# onto this one.
_ANSI_PATTERN = r"\033\[[0-9;]*m"
_ANSI_RE = re.compile(_ANSI_PATTERN)

# One ANSI escape or one character — the unit both the width measurement and the
# wrapper count in, so an escape never gets split across a wrapped line.
_TOKEN_RE = re.compile(rf"{_ANSI_PATTERN}|.", re.DOTALL)

# Below this many columns ``auto`` renders the narrow (stacked) variants…
NARROW_BELOW = 60
# …and those variants budget every emitted line to this many visible columns.
NARROW_WIDTH = 40

_layout_config: str | None = None


def strip_ansi(s: str) -> str:
    """``s`` without color escapes — the shared stripper (see ``_ANSI_RE``)."""
    return _ANSI_RE.sub("", s)


def visible_len(s: str) -> int:
    """Terminal column width of ``s``, ignoring embedded ANSI color escapes.

    Counts columns, not characters: East-Asian Wide/Fullwidth glyphs (CJK,
    fullwidth punctuation) occupy two columns and combining marks none, so a
    cell holding e.g. a Chinese name pads correctly instead of shifting the
    table borders right.
    """
    return sum(_char_width(char) for char in strip_ansi(s))


def _char_width(token: str) -> int:
    """Columns taken by one ``_TOKEN_RE`` token: none for an ANSI escape or a
    combining mark, two for an East-Asian Wide/Fullwidth glyph, else one."""
    if token.startswith("\033") or unicodedata.combining(token):
        return 0
    return 2 if unicodedata.east_asian_width(token) in "WF" else 1


def wrap(text: str, width: int) -> list[str]:
    """``text`` split into lines of at most ``width`` visible columns.

    ANSI- and wide-glyph aware (``textwrap`` measures neither), so a colored or
    CJK cell wraps where it actually overflows. Breaks after the last space that
    fits, or mid-text when a single run is longer than ``width``.
    """
    if width < 1 or visible_len(text) <= width:
        return [text]
    lines: list[str] = []
    current: list[str] = []
    used = 0
    break_at = 0  # index in ``current`` just past the last space that fits
    for token in _TOKEN_RE.findall(text):
        token_width = _char_width(token)
        if used + token_width > width and current:
            cut = break_at or len(current)
            lines.append("".join(current[:cut]).rstrip())
            current = current[cut:]
            while current and current[0] == " ":
                current.pop(0)
            used = sum(_char_width(t) for t in current)
            break_at = 0
        current.append(token)
        used += token_width
        if token == " ":
            break_at = len(current)
    lines.append("".join(current).rstrip())
    return lines


def elide(text: str, width: int) -> str:
    """``text`` cut to ``width`` visible columns, ending in "…" when cut."""
    if visible_len(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for token in _TOKEN_RE.findall(text):
        token_width = _char_width(token)
        if used + token_width > width - 1:
            break
        kept.append(token)
        used += token_width
    return "".join(kept).rstrip() + "…"


def _pad(text: str, width: int) -> str:
    """``text`` left-aligned in ``width`` visible columns (ANSI-aware ``ljust``)."""
    return text + " " * max(width - visible_len(text), 0)


def terminal_width() -> int | None:
    """Terminal width in columns, or ``None`` when it cannot be measured.

    Unlike ``shutil.get_terminal_size`` this never invents an 80-column answer:
    a redirected or closed stream reports ``None`` so callers can pick their own
    default. ``COLUMNS`` wins over the device — it is how a parent process
    forwards its width to a captured child.
    """
    columns = os.environ.get("COLUMNS", "").strip()
    if columns.isdecimal() and int(columns) > 0:
        return int(columns)
    for stream in (sys.__stdout__, sys.stdout, sys.__stderr__):
        try:
            measured = os.get_terminal_size(stream.fileno()).columns  # pyright: ignore[reportOptionalMemberAccess]
        except (AttributeError, OSError, ValueError):
            continue
        if measured > 0:
            return measured
    return None


def reset_layout_cache() -> None:
    """Forget the cached ``layout`` setting (tests, and anything changing the config)."""
    global _layout_config
    _layout_config = None


def layout_mode(override: str | None = None) -> str:
    """The active layout: ``"wide"`` (box tables, 64-column panels) or
    ``"narrow"`` (stacked cards, ``NARROW_WIDTH``-column panels).

    ``override`` short-circuits the config read, so a caller or test can force a
    mode (``"wide"``/``"narrow"``) or force the width-based resolution
    (``"auto"``). Otherwise the ``layout`` setting decides: ``wide``/``narrow``
    are taken as given, ``auto`` (the default, and anything unrecognized)
    resolves by terminal width — narrow below :data:`NARROW_BELOW` columns, wide
    when at or above it or when the width is unmeasurable.

    **Only the config read is cached** (see :func:`reset_layout_cache`), so a
    multi-panel command still touches the file once — but ``auto`` re-measures
    :func:`terminal_width` on *every* call, because the one long-lived caller,
    :func:`ai_accounts.config_menu.run_menu`, renders a frame per keystroke and
    has to follow a mid-session resize. Caching the resolved mode instead froze
    the first frame's answer for the life of the session. ``autoswitch`` is
    imported lazily to keep this leaf module off the config/i18n import chain.
    """
    global _layout_config
    if override in ("wide", "narrow"):
        return override
    configured = override  # only ``"auto"`` survives the check above
    if configured is None:
        if _layout_config is None:
            try:
                from . import autoswitch

                _layout_config = autoswitch.load_config().get("layout", "auto")
            except Exception:  # noqa: BLE001 — presentation must never fail on config
                _layout_config = "auto"
        configured = _layout_config
    if configured in ("wide", "narrow"):
        return configured
    width = terminal_width()
    return "narrow" if width is not None and width < NARROW_BELOW else "wide"


def narrow_width() -> int:
    """Visible columns the narrow renderers may use — the terminal, capped."""
    return max(min(terminal_width() or NARROW_WIDTH, NARROW_WIDTH), 20)


def panel(
    title: str,
    lines: list[str],
    accent: str = CYAN,
    width: int = 64,
    *,
    mode: str | None = None,
) -> None:
    """Bordered header/footer rule around left-aligned content — legible even
    with embedded ANSI color codes since only the header/footer are measured.

    In narrow mode the border shrinks to the terminal (capped at
    ``NARROW_WIDTH``) and content lines wrap instead of running off the screen;
    ``mode`` defaults to :func:`layout_mode` so every existing call site follows
    the setting without a change.
    """
    if (mode or layout_mode()) == "narrow":
        total = narrow_width()
        title = elide(title, total - 5)
        body = total - 3
        print(f"{accent}┌─ {BOLD}{title}{RESET}{accent} {'─' * (total - visible_len(title) - 5)}┐{RESET}")
        for line in lines or [f"{DIM}(none){RESET}"]:
            for part in wrap(line, body):
                print(f"{accent}│{RESET}  {part}")
        print(f"{accent}└{'─' * (total - 2)}┘{RESET}")
        return

    width = max(width, visible_len(title) + 8)
    top_dashes = width - visible_len(title) - 4
    print(f"{accent}┌─ {BOLD}{title}{RESET}{accent} {'─' * top_dashes}┐{RESET}")
    for line in lines or [f"{DIM}(none){RESET}"]:
        print(f"{accent}│{RESET}  {line}")
    print(f"{accent}└{'─' * (width - 1)}┘{RESET}")


def usage_color(percentage: int) -> str:
    """Threshold color for a usage percentage: ≥80% red+bold, ≥50% yellow, else
    green. Shared so every tool draws the same lines in the same places."""
    if percentage >= 80:
        return RED + BOLD
    if percentage >= 50:
        return YELLOW
    return GREEN


def accounts_table(
    rows: list[dict[str, str]],
    columns: Sequence[tuple[str, str]],
    *,
    optional_columns: frozenset[str] | set[str] = frozenset(),
    align_keys: Sequence[str] = (),
    mode: str | None = None,
) -> None:
    """Render dict-keyed ``rows`` as a box-drawing table.

    ``columns`` is an ordered list of ``(header, row_key)`` pairs. A column whose
    key is in ``optional_columns`` is dropped entirely when every row renders as
    "—" (after stripping ANSI) — used to hide usage/identity columns that carry
    no data yet. ``align_keys`` names usage-cell columns to right-align via
    ``usage_format.align_usage_cells`` before measuring widths (so the percent
    and time units line up). Widths and padding are ANSI-aware.

    In narrow mode (``mode`` defaults to :func:`layout_mode`) the same rows are
    stacked as one labelled card per record instead — no columns are dropped
    there, since a card has room for every field a table cannot fit.
    """
    for key in align_keys:
        usage_format.align_usage_cells(rows, key)

    if (mode or layout_mode()) == "narrow":
        _accounts_cards(rows, columns)
        return

    columns = [
        (header, key)
        for header, key in columns
        if key not in optional_columns
        or any(_ANSI_RE.sub("", row[key]) != "—" for row in rows)
    ]
    if not columns:
        return
    headers, keys = zip(*columns, strict=True)
    widths = [
        max(visible_len(h), max((visible_len(r[k]) for r in rows), default=0))
        for h, k in zip(headers, keys)
    ]

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def row(cells: list[str]) -> str:
        parts = [f" {cell}{' ' * (w - visible_len(cell))} " for cell, w in zip(cells, widths)]
        return "│" + "│".join(parts) + "│"

    print(rule("┌", "┬", "┐"))
    print(row([f"{BOLD}{h}{RESET}" for h in headers]))
    print(rule("├", "┼", "┤"))
    for r in rows:
        print(row([r[k] for k in keys]))
    print(rule("└", "┴", "┘"))


def _accounts_cards(rows: list[dict[str, str]], columns: Sequence[tuple[str, str]]) -> None:
    """The narrow rendering of :func:`accounts_table`: one stacked card per row.

    Each card leads with the first column's value (the record's identity) and,
    trailing on that line when it fits, its ``STATE`` cell; the remaining columns
    follow as ``LABEL  value`` lines using the table headers as labels, wrapped
    to the narrow budget. Unlike the table, nothing is elided away — the wide
    path's ``optional_columns`` filter is deliberately skipped, because a card
    has the room a 11-column table does not.
    """
    if not rows or not columns:
        return
    width = narrow_width()
    ident_key = columns[0][1]
    state_key = next((key for header, key in columns[1:] if header == "STATE"), None)
    fields = [(h, k) for h, k in columns[1:] if k != state_key]
    label_width = min(max((visible_len(h) for h, _ in fields), default=0), 18)
    indent = 2 + label_width + 2

    for index, row in enumerate(rows):
        if index:
            print(f"{DIM}{'─' * width}{RESET}")
        identity = row[ident_key]
        state = row[state_key] if state_key else ""
        gap = width - visible_len(identity) - visible_len(state)
        if state and gap >= 2:
            print(f"{BOLD}{identity}{RESET}{' ' * gap}{state}")
        else:
            for part in wrap(identity, width):
                print(f"{BOLD}{part}{RESET}")
            for part in wrap(state, width - 2) if state else ():
                print(f"  {part}")
        for header, key in fields:
            chunks = wrap(row[key], width - indent)
            label = _pad(elide(header, label_width), label_width)
            print(f"  {DIM}{label}{RESET}  {chunks[0]}")
            for extra in chunks[1:]:
                print(f"{' ' * indent}{extra}")


def choose_profile(
    kind_label: str,
    items: Sequence[tuple[str, str | None]],
    *,
    cancel_message: str = "Switch cancelled.",
) -> str | None:
    """Interactive numbered profile picker over ``items``.

    ``kind_label`` carries its article so the header reads naturally for every
    provider ("a Codex", "a Claude", "an Antigravity"). ``items`` is a
    PRE-FILTERED list of ``(name, sublabel_or_None)`` display entries — candidate
    filtering (e.g. dropping expired profiles) stays with the caller.
    ``cancel_message`` lets non-switch callers (e.g. remove) print an action-
    appropriate line instead of the switch-flavored default.

    Returns the chosen name, or ``None`` on cancel (Ctrl-C / EOF) or invalid
    input — the caller maps ``None`` to exit code 1. In narrow mode the header
    and every numbered entry wrap to the narrow budget (a name plus its
    sublabel measures ~56 columns) instead of running off the screen.
    """
    narrow = layout_mode() == "narrow"
    _emit(f"{BOLD}Choose {kind_label} profile:{RESET}", narrow)
    for index, (name, sublabel) in enumerate(items, start=1):
        suffix = f"  {DIM}{sublabel}{RESET}" if sublabel else ""
        _emit(f"  {index}) {name}{suffix}", narrow)

    try:
        selection = input("Select account number: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        log_yellow(cancel_message)
        return None

    if not selection.isdecimal():
        log_red("❌ Enter one of the account numbers shown above.")
        return None
    index = int(selection) - 1
    if index < 0 or index >= len(items):
        log_red("❌ Enter one of the account numbers shown above.")
        return None
    return items[index][0]


def choose_and_run(
    kind_label: str,
    items: Sequence[tuple[str, str | None]],
    action: Callable[[str], int],
    *,
    cancel_message: str = "Cancelled.",
) -> int:
    """Run ``choose_profile`` then ``action(chosen)``, or return 1 on cancel.

    ``items`` is already the caller's pre-filtered candidate list — candidate
    filtering, sublabel formatting, and "no saved profiles" messaging stay
    with the caller. Defaults to a generic cancel message since this helper is
    shared across actions (switch already has its own call site with its own
    "Switch cancelled." wording); pass ``cancel_message`` to match a specific
    action's grammar (e.g. "Remove cancelled.").
    """
    chosen = choose_profile(kind_label, items, cancel_message=cancel_message)
    if chosen is None:
        return 1
    return action(chosen)


def _emit(line: str, narrow: bool) -> None:
    """Print ``line`` — wrapped to :func:`narrow_width` when ``narrow``, verbatim
    otherwise, so wide output stays byte-for-byte the string it always was."""
    for part in wrap(line, narrow_width()) if narrow else (line,):
        print(part)


def _fit_detail(text: str, width: int) -> str:
    """``text`` squeezed into ``width`` visible columns.

    A slash-separated path loses its middle segments first
    (``→ /home/u/accounts/work.json`` → ``→ /…/work.json``), because the
    identifying part of a store path is the filename at the tail and hard-
    wrapping a spaceless path would split it across lines. Anything else — and
    a path whose two ends alone still overflow — falls back to :func:`elide`.
    """
    if visible_len(text) <= width:
        return text
    parts = text.split("/")
    if len(parts) > 2:
        squeezed = "/".join([parts[0], "…", parts[-1]])
        if visible_len(squeezed) <= width:
            return squeezed
    return elide(text, width)


def ok(action: str, name: str | None = None, *, bold: bool = True, mode: str | None = None) -> None:
    """Print a green "✅ …" success line in the shared save/switch/remove/
    sync/refresh grammar.

    With ``name``: ``✅ <action>:`` then the name (bold by default; ``bold=False``
    for the plain-name variant). Without ``name``: ``✅ <action>`` verbatim, no
    trailing colon (for whole-sentence confirmations like
    "All 3 profile(s) refreshed."). Prints to stdout.

    ``mode`` defaults to :func:`layout_mode`; in narrow mode the headline wraps
    to the narrow budget instead of running off a phone-sized screen (a
    "✅ Saved Codex profile: <name>" line measures 46-53 columns).
    """
    if name is None:
        line = f"{GREEN}✅ {action}{RESET}"
    else:
        rendered = f"{BOLD}{name}{RESET}" if bold else name
        line = f"{GREEN}✅ {action}:{RESET} {rendered}"
    _emit(line, (mode or layout_mode()) == "narrow")


def success_panel(
    action: str,
    name: str | None,
    lines: list[str],
    *,
    title: str,
    details: Sequence[str] = (),
    mode: str | None = None,
) -> None:
    """``ok`` line + optional detail lines + a green panel. ``mode`` is passed
    straight to :func:`panel` (so the narrow border lives in one place) and to
    :func:`ok`; the detail lines — store paths, measured at 72 columns — are
    squeezed to the same narrow budget by :func:`_fit_detail`."""
    narrow = (mode or layout_mode()) == "narrow"
    ok(action, name, mode="narrow" if narrow else "wide")
    for detail in details:
        if narrow:
            detail = _fit_detail(detail, narrow_width() - 3)
        print(f"{DIM}   {detail}{RESET}")
    print()
    panel(title, lines, accent=GREEN, mode=mode)

