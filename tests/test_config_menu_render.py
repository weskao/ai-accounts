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

import unittest

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
