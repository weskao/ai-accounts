"""Tests for the pure state-to-lines config-menu renderer (``config_menu.py``).

Only the render layer is exercised here — no keypress loop, no TTY, no I/O.
Every test calls ``config_menu.render(...)`` directly with plain data and
asserts on the returned ``list[str]``. Two concerns:

  1. **Schema-driven.** The renderer must iterate whatever ``Field`` tuple it
     is given — never a hardcoded list of the six known keys. A field added
     to the tuple must appear in the rendered rows with zero renderer changes
     (see ``test_extra_field_renders_without_renderer_changes``).
  2. **No secret leakage.** A masked field's raw value must never appear in
     any rendered line, in any mode (normal, cursor row, edit buffer).

Fixture values are placeholders only — never real tokens/emails/ids.

Run with ``uv run pytest tests/test_config_menu_render.py -q``.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from ai_accounts import _present
from ai_accounts import autoswitch
from ai_accounts import config_menu
from ai_accounts import config_schema as cs
from ai_accounts._present import _ANSI_RE, visible_len

FAKE_TOKEN = "12345:FAKE-TOKEN-PLACEHOLDER"


def _clean(lines: list[str]) -> list[str]:
    return [_ANSI_RE.sub("", line) for line in lines]


def _default_values() -> dict[str, object]:
    values = cs.defaults()
    values["telegram_bot_token"] = FAKE_TOKEN
    return values


class BoxIntegrityTests(unittest.TestCase):
    def test_all_lines_share_the_same_visible_width(self) -> None:
        lines = config_menu.render("ai-accounts config", cs.FIELDS, _default_values(), cursor=0)
        widths = {visible_len(line) for line in lines}
        self.assertEqual(len(widths), 1, f"inconsistent widths: {widths}")

    def test_top_and_bottom_borders_present(self) -> None:
        lines = _clean(config_menu.render("ai-accounts config", cs.FIELDS, _default_values(), cursor=0))
        self.assertTrue(lines[0].startswith("┌"))
        self.assertTrue(lines[-1].startswith("└"))
        self.assertIn("ai-accounts config", lines[0])

    def test_box_integrity_holds_with_cjk_value(self) -> None:
        fields = (
            cs.Field(key="label_cjk", type=str, default="", label="標籤", help="cjk test field"),
        )
        values = {"label_cjk": "測試值"}
        lines = config_menu.render("ai-accounts config", fields, values, cursor=0)
        widths = {visible_len(line) for line in lines}
        self.assertEqual(len(widths), 1, f"inconsistent widths under CJK: {widths}")

    def test_selecting_another_field_does_not_resize_the_box(self) -> None:
        widths = {
            visible_len(config_menu.render("t", cs.FIELDS, _default_values(), cursor=i)[0])
            for i in range(len(cs.FIELDS))
        }
        self.assertEqual(len(widths), 1, f"cursor-dependent widths: {widths}")


class SchemaDrivenTests(unittest.TestCase):
    def test_every_field_gets_a_row(self) -> None:
        lines = _clean(config_menu.render("ai-accounts config", cs.FIELDS, _default_values(), cursor=0))
        joined = "\n".join(lines)
        for field in cs.FIELDS:
            self.assertIn(field.label, joined, f"missing row for {field.key}")

    def test_extra_field_renders_without_renderer_changes(self) -> None:
        """Proves render() dispatches off the passed-in fields tuple, not a
        hardcoded list of the six known keys."""
        extra = cs.Field(
            key="seventh_key",
            type=str,
            default="",
            label="Seventh Setting",
            help="a field the renderer has never seen before",
        )
        fields = cs.FIELDS + (extra,)
        values = _default_values()
        values["seventh_key"] = "some-value"
        lines = _clean(config_menu.render("ai-accounts config", fields, values, cursor=0))
        joined = "\n".join(lines)
        self.assertIn("Seventh Setting", joined)
        self.assertIn("some-value", joined)


class CursorTests(unittest.TestCase):
    def test_cursor_marks_the_current_row_and_only_that_row(self) -> None:
        for cursor in range(len(cs.FIELDS)):
            with self.subTest(cursor=cursor):
                lines = _clean(config_menu.render("t", cs.FIELDS, _default_values(), cursor=cursor))
                marked = [line for line in lines if "❯" in line]  # ❯
                self.assertEqual(len(marked), 1)
                self.assertIn(cs.FIELDS[cursor].label, marked[0])

    def test_non_cursor_rows_reserve_cursor_marker_space(self) -> None:
        lines = _clean(config_menu.render("t", cs.FIELDS, _default_values(), cursor=0))
        selected = next(line for line in lines if f"❯ {cs.FIELDS[0].label}" in line)
        unselected = [
            line
            for line in lines
            if line.startswith("│    ") and any(field.label in line for field in cs.FIELDS[1:])
        ]
        self.assertTrue(selected.startswith("│  ❯ "))
        self.assertEqual(len(unselected), len(cs.FIELDS) - 1)


class UnsetValueTests(unittest.TestCase):
    def test_empty_string_value_renders_as_unset_not_blank(self) -> None:
        values = _default_values()
        values["telegram_chat_id"] = ""
        lines = _clean(config_menu.render("t", cs.FIELDS, values, cursor=0))
        chat_row = next(line for line in lines if "Telegram chat id" in line)
        self.assertIn("(unset)", chat_row)


class MaskingTests(unittest.TestCase):
    def test_raw_masked_value_never_appears_when_not_editing_it(self) -> None:
        """The previously-saved raw secret must never leak, regardless of
        cursor position — including while the masked row itself is being
        edited, since the buffer holds only the newly typed text (the stored
        secret is never seeded into it; see MaskedFieldEditTest)."""
        for cursor in range(len(cs.FIELDS)):
            for editing in (False, True):
                lines = config_menu.render(
                    "t",
                    cs.FIELDS,
                    _default_values(),
                    cursor=cursor,
                    editing=editing,
                    edit_buffer="unrelated-typed-text" if editing else "",
                )
                for line in lines:
                    self.assertNotIn(FAKE_TOKEN, line)

    def test_masked_field_shows_masked_form_via_schema(self) -> None:
        values = _default_values()
        lines = _clean(config_menu.render("t", cs.FIELDS, values, cursor=0))
        token_row = next(line for line in lines if "Telegram bot token" in line)
        expected = cs.format_value("telegram_bot_token", values["telegram_bot_token"])
        self.assertIn(expected, token_row)


class EditModeTests(unittest.TestCase):
    def test_edit_buffer_shown_on_cursor_row_when_editing(self) -> None:
        lines = _clean(
            config_menu.render(
                "t", cs.FIELDS, _default_values(), cursor=1, editing=True, edit_buffer="55"
            )
        )
        row = next(line for line in lines if cs.FIELDS[1].label in line)
        self.assertIn("55", row)

    def test_an_emptied_numeric_buffer_shows_its_floor_not_a_blank_cell(self) -> None:
        # Calculator display: backspacing the threshold down to nothing reads
        # as 0 on the row itself, matching what the help line previews and
        # what Enter would commit — a blank cell reads as "no value".
        cursor = [f.key for f in cs.FIELDS].index("switch_when_used_pct")
        lines = _clean(
            config_menu.render(
                "t", cs.FIELDS, _default_values(), cursor=cursor,
                editing=True, edit_buffer="",
            )
        )
        row = next(line for line in lines if cs.FIELDS[cursor].label in line)
        self.assertIn("0", row.split(cs.FIELDS[cursor].label)[1])

    def test_an_empty_buffer_on_a_text_field_stays_blank(self) -> None:
        # The floor-on-empty display is scoped to clamped numeric fields; a
        # free-text row must not sprout a "0" while being typed into.
        cursor = [f.key for f in cs.FIELDS].index("telegram_chat_id")
        lines = _clean(
            config_menu.render(
                "t", cs.FIELDS, _default_values(), cursor=cursor,
                editing=True, edit_buffer="",
            )
        )
        row = next(line for line in lines if cs.FIELDS[cursor].label in line)
        self.assertNotIn("0", row.split(cs.FIELDS[cursor].label)[1])

    def test_edit_buffer_on_masked_field_is_not_masked_cleartext_by_design(self) -> None:
        # Deliberate decision: while typing a new secret, the buffer echoes
        # cleartext (it's the user's own terminal and their own new value —
        # not the previously-saved secret, which stays masked everywhere
        # else). This test locks in that choice.
        # Located by key, not by index: a field appended to the schema ahead of
        # it must not silently retarget this assertion at another row.
        cursor = [f.key for f in cs.FIELDS].index("telegram_bot_token")
        lines = _clean(
            config_menu.render(
                "t",
                cs.FIELDS,
                _default_values(),
                cursor=cursor,
                editing=True,
                edit_buffer="new-typed-secret",
            )
        )
        row = next(line for line in lines if cs.FIELDS[cursor].label in line)
        self.assertIn("new-typed-secret", row)


class LiveHelpPreviewTests(unittest.TestCase):
    """While editing the threshold, the help line previews the typed value —
    not just the last-committed one — so the "10% left" math updates as you
    type, before Enter commits anything."""

    KEY = "switch_when_used_pct"

    def setUp(self) -> None:
        self.cursor = [f.key for f in cs.FIELDS].index(self.KEY)

    def test_help_line_previews_the_in_progress_edit_buffer(self) -> None:
        lines = _clean(
            config_menu.render(
                "t", cs.FIELDS, _default_values(), cursor=self.cursor,
                editing=True, edit_buffer="60",
            )
        )
        joined = "\n".join(lines)
        self.assertIn("60%", joined)
        self.assertIn("40%", joined)
        self.assertNotIn("90%", joined)
        self.assertNotIn("10%", joined)

    def test_help_line_previews_the_clamped_value_for_an_over_range_buffer(self) -> None:
        lines = _clean(
            config_menu.render(
                "t", cs.FIELDS, _default_values(), cursor=self.cursor,
                editing=True, edit_buffer="999",
            )
        )
        joined = "\n".join(lines)
        self.assertIn("100%", joined)
        self.assertIn("0% quota left", joined)

    def test_help_line_previews_the_minimum_once_backspaced_to_empty(self) -> None:
        # Calculator-style: backspacing the buffer down to nothing previews
        # the field's floor (0), not the last-committed value and not a crash.
        lines = _clean(
            config_menu.render(
                "t", cs.FIELDS, _default_values(), cursor=self.cursor,
                editing=True, edit_buffer="",
            )
        )
        joined = "\n".join(lines)
        self.assertIn("0% means", joined)
        self.assertIn("100% quota left", joined)
        self.assertNotIn("90%", joined)

    def test_help_line_falls_back_to_the_stored_value_for_a_not_yet_numeric_buffer(self) -> None:
        # Given: the buffer mid-typing a negative number — not parseable yet
        lines = _clean(
            config_menu.render(
                "t", cs.FIELDS, _default_values(), cursor=self.cursor,
                editing=True, edit_buffer="-",
            )
        )
        joined = "\n".join(lines)
        # Then: the help line shows the last-committed (default) value rather
        # than crashing or showing nothing
        self.assertIn("90%", joined)
        self.assertIn("10%", joined)

    def test_help_line_falls_back_to_the_stored_value_when_the_buffer_is_non_numeric(self) -> None:
        lines = _clean(
            config_menu.render(
                "t", cs.FIELDS, _default_values(), cursor=self.cursor,
                editing=True, edit_buffer="abc",
            )
        )
        joined = "\n".join(lines)
        self.assertIn("90%", joined)
        self.assertIn("10%", joined)

    def test_help_line_matches_the_committed_value_when_not_editing(self) -> None:
        values = {**_default_values(), self.KEY: 60}
        lines = _clean(
            config_menu.render("t", cs.FIELDS, values, cursor=self.cursor)
        )
        joined = "\n".join(lines)
        self.assertIn("60%", joined)
        self.assertIn("40%", joined)


class ValidationErrorTests(unittest.TestCase):
    def test_error_message_rendered_as_its_own_line(self) -> None:
        lines = _clean(
            config_menu.render(
                "t",
                cs.FIELDS,
                _default_values(),
                cursor=1,
                editing=True,
                edit_buffer="abc",
                error="switch_when_used_pct must be an integer 0-100, got 'abc'",
            )
        )
        joined = "\n".join(lines)
        self.assertIn("must be an integer 0-100", joined)


class NarrowEditCursorBoundaryTests(unittest.TestCase):
    """Regression for a real off-by-one ``code-reviewer`` caught: in narrow
    mode, ``editing=True`` used to append the edit cursor's trailing ``_``
    AFTER wrapping the buffer to the row width, instead of reserving its
    column first. At an edit-buffer length that is an exact multiple of the
    row's wrap width, the last wrapped chunk already filled the budget, so
    appending ``_`` ran that one row a column past every other row's border.

    A 720-frame sweep of this same code path passed while the bug was live
    because it only ever exercised 0- or 1-character buffers — never long
    enough to reach a wrap boundary. This sweeps buffer lengths through
    several such boundaries (and their neighbors) instead, on a free-text
    field with no length limit of its own, so nothing but the renderer's own
    wrapping can produce the mismatch.
    """

    KEY = "telegram_chat_id"
    WIDTH = 40  # render()'s fixed narrow box width for this test

    def test_all_returned_lines_share_one_visible_width_across_a_wrap_sweep(self) -> None:
        cursor = [f.key for f in cs.FIELDS].index(self.KEY)
        values = _default_values()
        # 0..100 sweeps well past several wrap-boundary multiples for a
        # 40-column narrow box, catching the bug regardless of the exact
        # internal margin arithmetic (label width, marker column, etc.).
        for length in range(0, 101):
            edit_buffer = "x" * length
            with self.subTest(edit_buffer_length=length):
                lines = config_menu.render(
                    "t",
                    cs.FIELDS,
                    values,
                    cursor=cursor,
                    editing=True,
                    edit_buffer=edit_buffer,
                    mode="narrow",
                    width=self.WIDTH,
                )
                widths = {visible_len(line) for line in lines}
                self.assertEqual(
                    len(widths), 1, f"inconsistent widths at buffer length {length}: {widths}"
                )


class FooterTests(unittest.TestCase):
    def test_footer_lists_supported_keys(self) -> None:
        lines = _clean(config_menu.render("t", cs.FIELDS, _default_values(), cursor=0))
        joined = "\n".join(lines)
        for hint in ("select", "change", "edit/toggle", "save", "cancel", "quit"):
            self.assertIn(hint, joined)


class GroupingTests(unittest.TestCase):
    def test_switching_settings_are_grouped_and_explained(self) -> None:
        lines = _clean(config_menu.render("t", cs.FIELDS, _default_values(), cursor=0))
        joined = "\n".join(lines)
        self.assertIn("Automatic switching", joined)
        self.assertIn("↳ Switch at usage (%)", joined)
        self.assertIn("switch to the saved account with the most quota left", joined)

    def test_groups_selection_and_values_have_distinct_colors(self) -> None:
        lines = config_menu.render("t", cs.FIELDS, _default_values(), cursor=0)
        group = next(line for line in lines if "Automatic switching" in line)
        selected = next(line for line in lines if cs.FIELDS[0].label in line)
        value = next(line for line in lines if cs.FIELDS[1].label in line)
        self.assertIn(config_menu.MAGENTA, group)
        self.assertIn(config_menu.CYAN, selected)
        self.assertIn(config_menu.CYAN, value)


class TitleParamTests(unittest.TestCase):
    def test_title_is_a_parameter_not_hardcoded(self) -> None:
        for title in ("ai-accounts config", "codex-accounts config", "claude-accounts config"):
            with self.subTest(title=title):
                lines = _clean(config_menu.render(title, cs.FIELDS, _default_values(), cursor=0))
                self.assertIn(title, lines[0])
                self.assertNotIn("ai-accounts config" if title != "ai-accounts config" else "\0", lines[0])

    def test_default_ai_accounts_title_is_not_baked_into_lower_layer(self) -> None:
        lines = _clean(config_menu.render("codex-accounts config", cs.FIELDS, _default_values(), cursor=0))
        self.assertNotIn("ai-accounts config", "\n".join(lines))


class LivePreviewLayoutTests(unittest.TestCase):
    """The ``layout`` row live-previews like ``language`` does: cycling it in
    the (uncommitted) ``values`` dict reflows the box immediately, and an
    explicit ``mode=`` argument (what tests and a forced override use) always
    wins over whatever ``values["layout"]`` says."""

    def test_uncommitted_layout_value_narrow_vs_wide_produce_different_widths(self) -> None:
        narrow_values = {**_default_values(), "layout": "narrow"}
        wide_values = {**_default_values(), "layout": "wide"}
        narrow_width = visible_len(config_menu.render("t", cs.FIELDS, narrow_values, cursor=0)[0])
        wide_width = visible_len(config_menu.render("t", cs.FIELDS, wide_values, cursor=0)[0])
        self.assertNotEqual(narrow_width, wide_width)
        self.assertLess(narrow_width, wide_width)

    def test_explicit_mode_argument_overrides_the_uncommitted_layout_value(self) -> None:
        # values says narrow, but an explicit mode="wide" wins
        values = {**_default_values(), "layout": "narrow"}
        forced_wide = config_menu.render("t", cs.FIELDS, values, cursor=0, mode="wide")
        plain_wide = config_menu.render(
            "t", cs.FIELDS, {**_default_values(), "layout": "wide"}, cursor=0
        )
        self.assertEqual(
            visible_len(forced_wide[0]), visible_len(plain_wide[0])
        )

        # values says wide, but an explicit mode="narrow" wins
        values = {**_default_values(), "layout": "wide"}
        forced_narrow = config_menu.render("t", cs.FIELDS, values, cursor=0, mode="narrow")
        plain_narrow = config_menu.render(
            "t", cs.FIELDS, {**_default_values(), "layout": "narrow"}, cursor=0
        )
        self.assertEqual(
            visible_len(forced_narrow[0]), visible_len(plain_narrow[0])
        )


class LivePreviewAutoLayoutTests(unittest.TestCase):
    """Cycling the layout row to ``auto`` must live-preview the width-derived
    answer, in both directions. It did not: ``layout_mode`` used to cache the
    RESOLVED mode and only honour ``wide``/``narrow`` as an override, so
    ``auto`` fell through to whatever the first call had frozen — on-disk
    ``narrow`` + a 200-column terminal still previewed a 40-column box, and
    on-disk ``wide`` + a 40-column terminal still previewed 155."""

    def _box_width(self, layout: str, columns: str) -> int:
        with mock.patch.dict(os.environ, {"COLUMNS": columns}):
            _present.reset_layout_cache()
            values = {**_default_values(), "layout": layout}
            return visible_len(config_menu.render("t", cs.FIELDS, values, cursor=0)[0])

    def test_auto_follows_the_terminal_width_in_both_directions(self) -> None:
        wide_reference = self._box_width("wide", columns="40")
        narrow_reference = self._box_width("narrow", columns="200")
        self.assertLess(narrow_reference, wide_reference)

        self.assertEqual(self._box_width("auto", columns="200"), wide_reference)
        self.assertEqual(self._box_width("auto", columns="40"), narrow_reference)

    def test_auto_ignores_the_committed_on_disk_value(self) -> None:
        """``values`` is what the user is editing; disk is stale by definition
        mid-cycle. ``auto`` in ``values`` must beat either on-disk answer."""
        for on_disk in ("narrow", "wide"):
            with self.subTest(on_disk=on_disk), mock.patch.object(
                autoswitch, "load_config", return_value={"layout": on_disk}
            ):
                self.assertEqual(
                    self._box_width("auto", columns="200"), self._box_width("wide", columns="200")
                )
                self.assertEqual(
                    self._box_width("auto", columns="40"), self._box_width("narrow", columns="40")
                )

    def test_a_resize_between_two_renders_changes_the_box(self) -> None:
        """``run_menu`` calls ``render`` once per keystroke for the life of the
        session — two renders at two widths, one process, no cache reset."""
        with mock.patch.object(autoswitch, "load_config", return_value={"layout": "auto"}):
            values = _default_values()
            with mock.patch.dict(os.environ, {"COLUMNS": "200"}):
                _present.reset_layout_cache()
                first = visible_len(config_menu.render("t", cs.FIELDS, values, cursor=0)[0])
            with mock.patch.dict(os.environ, {"COLUMNS": "30"}):
                second = visible_len(config_menu.render("t", cs.FIELDS, values, cursor=0)[0])
        self.assertGreater(first, second)


class PurityTests(unittest.TestCase):
    def test_render_is_pure_no_stdout_no_tty_checks(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = config_menu.render("t", cs.FIELDS, _default_values(), cursor=0)
        self.assertEqual(buf.getvalue(), "")
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(line, str) for line in result))


if __name__ == "__main__":
    unittest.main()
