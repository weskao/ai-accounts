"""Shared terminal-presentation helpers for the account tools.

Canonical rendering extracted from ``codex-accounts`` (the first account tool;
its box-drawing panels, tables, usage colors, interactive picker, and
success-message grammar are the reference the sibling tools adopt). Everything
here is pure presentation — no auth, no I/O beyond ``print``/``input`` — so the
per-provider modules stay focused on their own auth logic.

Cross-cutting color/log primitives live in ``_utils``; the usage-cell
formatting/alignment (shared with the non-interactive usage modules) lives in
``usage_format``. This module composes those, it does not duplicate them.
"""

from __future__ import annotations

import re
from typing import Sequence

from . import usage_format
from ._utils import BOLD, CYAN, DIM, GREEN, RESET, RED, YELLOW, log_red, log_yellow

# ANSI escape stripper — the single source of truth for measuring the visible
# width of a colored cell. Sibling tools that still keep a local copy migrate
# onto this one.
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible_len(s: str) -> int:
    """Printable width of ``s``, ignoring embedded ANSI color escapes."""
    return len(_ANSI_RE.sub("", s))


def panel(title: str, lines: list[str], accent: str = CYAN, width: int = 64) -> None:
    """Bordered header/footer rule around left-aligned content — legible even
    with embedded ANSI color codes since only the header/footer are measured."""
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
) -> None:
    """Render dict-keyed ``rows`` as a box-drawing table.

    ``columns`` is an ordered list of ``(header, row_key)`` pairs. A column whose
    key is in ``optional_columns`` is dropped entirely when every row renders as
    "—" (after stripping ANSI) — used to hide usage/identity columns that carry
    no data yet. ``align_keys`` names usage-cell columns to right-align via
    ``usage_format.align_usage_cells`` before measuring widths (so the percent
    and time units line up). Widths and padding are ANSI-aware.
    """
    for key in align_keys:
        usage_format.align_usage_cells(rows, key)

    columns = [
        (header, key)
        for header, key in columns
        if key not in optional_columns
        or any(_ANSI_RE.sub("", row[key]) != "—" for row in rows)
    ]
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


def choose_profile(
    kind_label: str, items: Sequence[tuple[str, str | None]]
) -> str | None:
    """Interactive numbered profile picker over ``items``.

    ``kind_label`` carries its article so the header reads naturally for every
    provider ("a Codex", "a Claude", "an Antigravity"). ``items`` is a
    PRE-FILTERED list of ``(name, sublabel_or_None)`` display entries — candidate
    filtering (e.g. dropping expired profiles) stays with the caller.

    Returns the chosen name, or ``None`` on cancel (Ctrl-C / EOF) or invalid
    input — the caller maps ``None`` to exit code 1.
    """
    print(f"{BOLD}Choose {kind_label} profile:{RESET}")
    for index, (name, sublabel) in enumerate(items, start=1):
        suffix = f"  {DIM}{sublabel}{RESET}" if sublabel else ""
        print(f"  {index}) {name}{suffix}")

    try:
        selection = input("Select account number: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        log_yellow("Switch cancelled.")
        return None

    if not selection.isdecimal():
        log_red("❌ Enter one of the account numbers shown above.")
        return None
    index = int(selection) - 1
    if index < 0 or index >= len(items):
        log_red("❌ Enter one of the account numbers shown above.")
        return None
    return items[index][0]


def ok(action: str, name: str | None = None, *, bold: bool = True) -> None:
    """Print a green "✅ …" success line in the shared save/switch/remove/
    sync/refresh grammar.

    With ``name``: ``✅ <action>:`` then the name (bold by default; ``bold=False``
    for the plain-name variant). Without ``name``: ``✅ <action>`` verbatim, no
    trailing colon (for whole-sentence confirmations like
    "All 3 profile(s) refreshed."). Prints to stdout.
    """
    if name is None:
        print(f"{GREEN}✅ {action}{RESET}")
    else:
        rendered = f"{BOLD}{name}{RESET}" if bold else name
        print(f"{GREEN}✅ {action}:{RESET} {rendered}")
