"""Behavioural half of the interactive config menu.

Covers the pure keypress state machine (``step``), the terminal shell
(``run_menu``), the non-TTY numbered fallback, and the shared
``cmd_config(argv)`` entry point. Every test drives the machine from a FAKE
key source — a plain iterator of ``KeyEvent`` values — so nothing here needs a
real TTY (the isatty/termios faking mirrors ``test_config_keyreader.py``).

Fixtures use placeholder data only — the one secret-shaped fixture is
``"12345:FAKE-TOKEN-PLACEHOLDER"``.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import autoswitch, config_menu as cm, config_schema
from ai_accounts._keyreader import Key, KeyEvent

TOKEN = "12345:FAKE-TOKEN-PLACEHOLDER"


def char(c: str) -> KeyEvent:
    return KeyEvent(Key.CHAR, c)


def keys(*events: KeyEvent):
    """A fake key source: yields *events*, then Ctrl-C forever — the one event
    that quits from ANY state (a bare 'q' is literal text mid-edit), so a test
    with a wrong key sequence fails instead of hanging."""
    it = iter(events)

    def read() -> KeyEvent:
        return next(it, KeyEvent(Key.CTRL_C))

    return read


def index_of(key: str) -> int:
    return [f.key for f in config_schema.FIELDS].index(key)


def state_at(key: str, **kwargs) -> cm.MenuState:
    return cm.MenuState(values=config_schema.defaults(), cursor=index_of(key), **kwargs)


class StateMachineCursorTest(unittest.TestCase):
    def test_down_moves_the_cursor(self) -> None:
        after = cm.step(cm.MenuState(values={}), KeyEvent(Key.DOWN))
        self.assertEqual(after.cursor, 1)

    def test_up_from_the_top_wraps_to_the_bottom(self) -> None:
        after = cm.step(cm.MenuState(values={}), KeyEvent(Key.UP))
        self.assertEqual(after.cursor, len(config_schema.FIELDS) - 1)

    def test_down_from_the_bottom_wraps_to_the_top(self) -> None:
        start = cm.MenuState(values={}, cursor=len(config_schema.FIELDS) - 1)
        self.assertEqual(cm.step(start, KeyEvent(Key.DOWN)).cursor, 0)

    def test_unknown_keys_are_ignored_not_crashed_on(self) -> None:
        # T2 leaves SS3 arrows and Home/End/PgUp/PgDn decoding as UNKNOWN.
        start = cm.MenuState(values={}, cursor=2)
        self.assertEqual(cm.step(start, KeyEvent(Key.UNKNOWN)), start)

    def test_unknown_key_while_editing_keeps_the_buffer(self) -> None:
        start = state_at("telegram_chat_id", editing=True, edit_buffer="12")
        self.assertEqual(cm.step(start, KeyEvent(Key.UNKNOWN)), start)


class StateMachineEditTest(unittest.TestCase):
    def test_enter_on_a_text_field_opens_edit_mode_seeded_with_the_value(self) -> None:
        start = state_at("switch_when_used_pct")
        after = cm.step(start, KeyEvent(Key.ENTER))
        self.assertTrue(after.editing)
        self.assertEqual(after.edit_buffer, "90")

    def test_typing_appends_and_backspace_deletes(self) -> None:
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="")
        for c in "755":
            state = cm.step(state, char(c))
        state = cm.step(state, KeyEvent(Key.BACKSPACE))
        self.assertEqual(state.edit_buffer, "75")

    def test_backspace_on_an_empty_buffer_is_harmless(self) -> None:
        state = state_at("telegram_chat_id", editing=True, edit_buffer="")
        self.assertEqual(cm.step(state, KeyEvent(Key.BACKSPACE)).edit_buffer, "")

    def test_enter_commits_the_parsed_value_and_leaves_edit_mode(self) -> None:
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="75")
        after = cm.step(state, KeyEvent(Key.ENTER))
        self.assertFalse(after.editing)
        self.assertEqual(after.values["switch_when_used_pct"], 75)
        self.assertIs(type(after.values["switch_when_used_pct"]), int)
        self.assertTrue(after.pending_save)
        self.assertIsNone(after.error)

    def test_invalid_input_surfaces_the_schema_message_and_keeps_editing(self) -> None:
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="abc")
        after = cm.step(state, KeyEvent(Key.ENTER))
        self.assertTrue(after.editing)
        self.assertEqual(
            after.error, "switch_when_used_pct must be an integer 0-100, got 'abc'"
        )
        self.assertEqual(after.values["switch_when_used_pct"], 90)
        self.assertFalse(after.pending_save)

    def test_an_over_range_value_commits_clamped_to_the_maximum(self) -> None:
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="150")
        after = cm.step(state, KeyEvent(Key.ENTER))
        self.assertFalse(after.editing)
        self.assertIsNone(after.error)
        self.assertEqual(after.values["switch_when_used_pct"], 100)
        self.assertTrue(after.pending_save)

    def test_backspacing_to_empty_then_enter_commits_the_minimum(self) -> None:
        # Calculator-style: backspacing a numeric field down to nothing is
        # the floor value, not an error — mirrors the live help preview.
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="")
        after = cm.step(state, KeyEvent(Key.ENTER))
        self.assertFalse(after.editing)
        self.assertIsNone(after.error)
        self.assertEqual(after.values["switch_when_used_pct"], 0)
        self.assertTrue(after.pending_save)
        self.assertEqual(after.touched, frozenset({"switch_when_used_pct"}))

    def test_an_empty_buffer_commit_on_a_plain_text_field_still_clears_it(self) -> None:
        # The empty-means-floor rule is scoped to clamped numeric fields —
        # a plain text field's existing empty-means-cleared behavior must
        # not change.
        state = state_at("telegram_chat_id", editing=True, edit_buffer="")
        after = cm.step(state, KeyEvent(Key.ENTER))
        self.assertFalse(after.editing)
        self.assertEqual(after.values["telegram_chat_id"], "")

    def test_escape_cancels_the_edit_and_restores_the_prior_value(self) -> None:
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="7")
        after = cm.step(state, KeyEvent(Key.ESCAPE))
        self.assertFalse(after.editing)
        self.assertEqual(after.edit_buffer, "")
        self.assertEqual(after.values["switch_when_used_pct"], 90)
        self.assertFalse(after.pending_save)

    def test_moving_the_cursor_clears_a_stale_error(self) -> None:
        start = cm.MenuState(values={}, error="boom")
        self.assertIsNone(cm.step(start, KeyEvent(Key.DOWN)).error)


class StateMachineToggleTest(unittest.TestCase):
    def test_enter_toggles_a_bool_instead_of_asking_for_typing(self) -> None:
        state = state_at("enabled")
        after = cm.step(state, KeyEvent(Key.ENTER))
        self.assertFalse(after.editing)
        self.assertIs(after.values["enabled"], True)
        self.assertTrue(after.pending_save)
        self.assertIs(cm.step(after, KeyEvent(Key.ENTER)).values["enabled"], False)

    def test_enter_cycles_an_enum_through_its_choices_and_wraps(self) -> None:
        state = state_at("notify")
        seen = []
        for _ in range(4):
            state = cm.step(state, KeyEvent(Key.ENTER))
            seen.append(state.values["notify"])
        self.assertEqual(seen, ["telegram", "none", "desktop", "telegram"])

    def test_left_cycles_backwards(self) -> None:
        after = cm.step(state_at("notify"), KeyEvent(Key.LEFT))
        self.assertEqual(after.values["notify"], "none")

    def test_right_on_a_free_text_field_does_nothing(self) -> None:
        start = state_at("telegram_chat_id")
        self.assertEqual(cm.step(start, KeyEvent(Key.RIGHT)), start)


class SeventhFieldTest(unittest.TestCase):
    """AC4: a hypothetical 7th schema field needs ZERO state-machine changes."""

    EXTRA = (
        config_schema.Field(
            key="retry_limit",
            type=int,
            default=3,
            minimum=1,
            maximum=9,
            label="Retry limit",
            help="placeholder",
        ),
        config_schema.Field(
            key="theme",
            type=str,
            default="dark",
            choices=("dark", "light"),
            label="Theme",
            help="placeholder",
        ),
    )

    @property
    def fields(self):
        return config_schema.FIELDS + self.EXTRA

    def test_a_new_int_field_edits_and_validates(self) -> None:
        cursor = len(config_schema.FIELDS)
        state = cm.MenuState(values={"retry_limit": 3}, cursor=cursor)
        state = cm.step(state, KeyEvent(Key.ENTER), self.fields)
        self.assertEqual(state.edit_buffer, "3")
        state = cm.step(state, KeyEvent(Key.BACKSPACE), self.fields)
        state = cm.step(state, char("7"), self.fields)
        state = cm.step(state, KeyEvent(Key.ENTER), self.fields)
        self.assertEqual(state.values["retry_limit"], 7)

        bad = cm.MenuState(values={"retry_limit": 3}, cursor=cursor, editing=True,
                           edit_buffer="99")
        self.assertIn("1-9", cm.step(bad, KeyEvent(Key.ENTER), self.fields).error)

    def test_a_new_enum_field_cycles_off_its_own_descriptor(self) -> None:
        cursor = len(config_schema.FIELDS) + 1
        state = cm.MenuState(values={"theme": "dark"}, cursor=cursor)
        state = cm.step(state, KeyEvent(Key.ENTER), self.fields)
        self.assertEqual(state.values["theme"], "light")

    def test_the_cursor_wraps_over_the_extended_tuple(self) -> None:
        state = cm.MenuState(values={}, cursor=len(self.fields) - 1)
        self.assertEqual(cm.step(state, KeyEvent(Key.DOWN), self.fields).cursor, 0)

    def test_the_extended_tuple_renders_without_touching_the_renderer(self) -> None:
        lines = cm.render("t", self.fields, {}, 0)
        self.assertTrue(any("Theme" in line for line in lines))


class MaskedFieldEditTest(unittest.TestCase):
    """Data-loss regression: opening a masked row must never make the MASK the
    new value. The edit buffer seeds EMPTY for a masked field, and a bare Enter
    on that empty buffer cancels instead of blanking the stored secret."""

    def state(self, **kwargs) -> cm.MenuState:
        values = {**config_schema.defaults(), "telegram_bot_token": TOKEN}
        return cm.MenuState(
            values=values, cursor=index_of("telegram_bot_token"), **kwargs
        )

    def test_enter_on_a_masked_row_does_not_seed_the_mask(self) -> None:
        after = cm.step(self.state(), KeyEvent(Key.ENTER))
        self.assertTrue(after.editing)
        self.assertEqual(after.edit_buffer, "")

    def test_accept_unchanged_leaves_the_stored_secret_intact(self) -> None:
        opened = cm.step(self.state(), KeyEvent(Key.ENTER))
        closed = cm.step(opened, KeyEvent(Key.ENTER))
        self.assertEqual(closed.values["telegram_bot_token"], TOKEN)
        self.assertFalse(closed.editing)
        self.assertFalse(closed.pending_save)

    def test_escape_mid_edit_leaves_the_stored_secret_intact(self) -> None:
        state = cm.step(self.state(), KeyEvent(Key.ENTER))
        state = cm.step(state, char("x"))
        state = cm.step(state, KeyEvent(Key.ESCAPE))
        self.assertEqual(state.values["telegram_bot_token"], TOKEN)
        self.assertFalse(state.editing)
        self.assertEqual(state.edit_buffer, "")

    def test_typing_a_new_secret_still_replaces_the_old_one(self) -> None:
        state = cm.step(self.state(), KeyEvent(Key.ENTER))
        for c in "9:NEW":
            state = cm.step(state, char(c))
        state = cm.step(state, KeyEvent(Key.ENTER))
        self.assertEqual(state.values["telegram_bot_token"], "9:NEW")
        self.assertTrue(state.pending_save)

    def test_backspacing_to_empty_then_enter_still_cancels(self) -> None:
        state = cm.step(self.state(), KeyEvent(Key.ENTER))
        state = cm.step(state, char("z"))
        state = cm.step(state, KeyEvent(Key.BACKSPACE))
        state = cm.step(state, KeyEvent(Key.ENTER))
        self.assertEqual(state.values["telegram_bot_token"], TOKEN)

    def test_a_non_masked_text_row_still_seeds_from_its_stored_value(self) -> None:
        values = {**config_schema.defaults(), "telegram_chat_id": "12345"}
        state = cm.MenuState(values=values, cursor=index_of("telegram_chat_id"))
        self.assertEqual(cm.step(state, KeyEvent(Key.ENTER)).edit_buffer, "12345")


class EmptyFieldsTest(unittest.TestCase):
    """An empty field tuple must not raise ZeroDivisionError in ``_step_browsing``."""

    def test_every_row_key_is_a_no_op_with_no_fields(self) -> None:
        state = cm.MenuState(values={})
        for event in (
            KeyEvent(Key.UP),
            KeyEvent(Key.DOWN),
            KeyEvent(Key.LEFT),
            KeyEvent(Key.RIGHT),
            KeyEvent(Key.ENTER),
        ):
            with self.subTest(event=event):
                self.assertEqual(cm.step(state, event, ()), state)

    def test_quit_still_works_with_no_fields(self) -> None:
        self.assertTrue(cm.step(cm.MenuState(values={}), char("q"), ()).quitting)

    def test_a_confirmed_reset_with_no_fields_saves_nothing(self) -> None:
        state = cm.step(cm.MenuState(values={}), char("r"), ())
        self.assertTrue(state.confirm_reset)
        after = cm.step(state, char("y"), ())
        self.assertFalse(after.pending_save)
        self.assertEqual(after.touched, frozenset())


class ResetIntentTest(unittest.TestCase):
    """``r`` asks; only ``y`` acts. Everything else leaves the config alone —
    a reset blanks a stored token, so it must never ride on one stray key."""

    def state(self) -> cm.MenuState:
        values = {
            **config_schema.defaults(),
            "enabled": True,
            "switch_when_used_pct": 42,
            "telegram_bot_token": TOKEN,
        }
        return cm.MenuState(values=values)

    def test_r_only_asks_and_changes_nothing_yet(self) -> None:
        after = cm.step(self.state(), char("r"))
        self.assertTrue(after.confirm_reset)
        self.assertFalse(after.pending_save)
        self.assertEqual(after.touched, frozenset())
        self.assertIs(after.values["enabled"], True)

    def test_y_restores_every_declared_default_and_saves(self) -> None:
        after = cm.step(cm.step(self.state(), char("r")), char("y"))
        self.assertFalse(after.confirm_reset)
        self.assertTrue(after.pending_save)
        self.assertEqual(after.values, config_schema.defaults())
        self.assertEqual(after.touched, frozenset(config_schema.defaults()))

    def test_any_other_key_cancels_the_reset(self) -> None:
        asked = cm.step(self.state(), char("r"))
        for event in (char("n"), char("x"), KeyEvent(Key.ESCAPE), KeyEvent(Key.DOWN)):
            with self.subTest(event=event):
                after = cm.step(asked, event)
                self.assertFalse(after.confirm_reset)
                self.assertFalse(after.pending_save)
                self.assertEqual(after.values, asked.values)

    def test_ctrl_c_while_confirming_quits_without_resetting(self) -> None:
        after = cm.step(cm.step(self.state(), char("r")), KeyEvent(Key.CTRL_C))
        self.assertTrue(after.quitting)
        self.assertFalse(after.confirm_reset)
        self.assertEqual(after.values["switch_when_used_pct"], 42)

    def test_the_prompt_is_rendered_while_confirming(self) -> None:
        lines = cm.render("t", config_schema.FIELDS, {}, 0, confirm_reset=True)
        self.assertTrue(any("[y/N]" in line for line in lines))

    def test_r_is_literal_text_while_editing(self) -> None:
        state = state_at("telegram_chat_id", editing=True, edit_buffer="")
        after = cm.step(state, char("r"))
        self.assertEqual(after.edit_buffer, "r")
        self.assertFalse(after.confirm_reset)


class QuitAndSaveIntentTest(unittest.TestCase):
    def test_q_asks_to_quit(self) -> None:
        self.assertTrue(cm.step(cm.MenuState(values={}), char("q")).quitting)

    def test_ctrl_c_quits_even_mid_edit(self) -> None:
        state = state_at("telegram_chat_id", editing=True, edit_buffer="x")
        after = cm.step(state, KeyEvent(Key.CTRL_C))
        self.assertTrue(after.quitting)
        self.assertFalse(after.editing)

    def test_s_is_no_longer_a_key_because_changes_save_themselves(self) -> None:
        state = cm.MenuState(values={})
        self.assertEqual(cm.step(state, char("s")), state)

    def test_q_and_s_are_literal_text_while_editing(self) -> None:
        state = state_at("telegram_chat_id", editing=True, edit_buffer="")
        state = cm.step(state, char("s"))
        state = cm.step(state, char("q"))
        self.assertEqual(state.edit_buffer, "sq")
        self.assertFalse(state.quitting)
        self.assertFalse(state.pending_save)


class TouchedKeysTest(unittest.TestCase):
    """Data-loss regression: the menu must remember which keys the USER changed,
    so a save merges only those and cannot revert another terminal's write.
    Navigating, cancelling an edit, or committing an unchanged value are not
    changes and must leave the key untouched."""

    def test_a_fresh_state_has_touched_nothing(self) -> None:
        self.assertEqual(cm.MenuState(values={}).touched, frozenset())

    def test_toggling_a_bool_marks_only_that_key(self) -> None:
        after = cm.step(state_at("enabled"), KeyEvent(Key.ENTER))
        self.assertEqual(after.touched, frozenset({"enabled"}))

    def test_cycling_an_enum_marks_only_that_key(self) -> None:
        after = cm.step(state_at("notify"), KeyEvent(Key.RIGHT))
        self.assertEqual(after.touched, frozenset({"notify"}))

    def test_committing_a_typed_value_marks_that_key(self) -> None:
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="75")
        self.assertEqual(
            cm.step(state, KeyEvent(Key.ENTER)).touched,
            frozenset({"switch_when_used_pct"}),
        )

    def test_navigating_over_a_row_touches_nothing(self) -> None:
        state = cm.step(state_at("enabled"), KeyEvent(Key.DOWN))
        state = cm.step(state, KeyEvent(Key.UP))
        self.assertEqual(state.touched, frozenset())

    def test_an_escape_cancelled_edit_touches_nothing(self) -> None:
        state = cm.step(state_at("telegram_chat_id"), KeyEvent(Key.ENTER))
        state = cm.step(state, char("9"))
        state = cm.step(state, KeyEvent(Key.ESCAPE))
        self.assertEqual(state.touched, frozenset())

    def test_committing_an_unchanged_value_touches_nothing(self) -> None:
        state = cm.step(state_at("switch_when_used_pct"), KeyEvent(Key.ENTER))
        self.assertEqual(state.edit_buffer, "90")
        after = cm.step(state, KeyEvent(Key.ENTER))
        self.assertEqual(after.touched, frozenset())
        self.assertFalse(after.pending_save)
        self.assertFalse(after.editing)

    def test_accepting_a_masked_row_unchanged_touches_nothing(self) -> None:
        values = {**config_schema.defaults(), "telegram_bot_token": TOKEN}
        state = cm.MenuState(values=values, cursor=index_of("telegram_bot_token"))
        state = cm.step(state, KeyEvent(Key.ENTER))
        after = cm.step(state, KeyEvent(Key.ENTER))
        self.assertEqual(after.touched, frozenset())
        self.assertEqual(after.values["telegram_bot_token"], TOKEN)

    def test_a_rejected_edit_touches_nothing(self) -> None:
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="abc")
        self.assertEqual(cm.step(state, KeyEvent(Key.ENTER)).touched, frozenset())

    def test_a_clamped_edit_still_touches_the_key(self) -> None:
        state = state_at("switch_when_used_pct", editing=True, edit_buffer="150")
        self.assertEqual(
            cm.step(state, KeyEvent(Key.ENTER)).touched,
            frozenset({"switch_when_used_pct"}),
        )

    def test_save_passes_only_the_touched_keys(self) -> None:
        state = cm.MenuState(
            values={**config_schema.defaults(), "enabled": True},
            touched=frozenset({"enabled"}),
            pending_save=True,
        )
        writes: list[dict] = []
        saver = cm._Saver(save=writes.append)
        after = cm._save(state, saver)
        self.assertIsNone(saver.close())
        self.assertEqual(writes, [{"enabled": True}])
        self.assertEqual(after.touched, frozenset())
        self.assertFalse(after.pending_save)


class _ConfigFileMixin:
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        env = mock.patch.dict(
            os.environ, {"AI_ACCOUNTS_CONFIG_JSON": str(self.config_path)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)

    def stored(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))


class RunMenuTest(_ConfigFileMixin, unittest.TestCase):
    def run_menu(self, read, **kwargs):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cm.run_menu("test config", read=read, out=out, **kwargs)
        return rc, out.getvalue(), err.getvalue()

    def test_a_toggle_persists_without_a_save_key(self) -> None:
        rc, out, _ = self.run_menu(keys(KeyEvent(Key.ENTER), char("q")))
        self.assertEqual(rc, 0)
        self.assertIs(self.stored()["enabled"], True)
        self.assertIn("Enable automatic switching", out)

    def test_quitting_without_changing_anything_writes_nothing(self) -> None:
        rc, _, err = self.run_menu(keys(KeyEvent(Key.DOWN), KeyEvent(Key.UP), char("q")))
        self.assertEqual(rc, 0)
        self.assertFalse(self.config_path.exists())
        self.assertEqual(err, "")

    def test_a_confirmed_reset_restores_the_defaults_on_disk(self) -> None:
        autoswitch.save_config(
            {"enabled": True, "switch_when_used_pct": 42, "telegram_bot_token": TOKEN}
        )
        rc, _, _ = self.run_menu(keys(char("r"), char("y"), char("q")))
        self.assertEqual(rc, 0)
        stored = self.stored()
        self.assertIs(stored["enabled"], False)
        self.assertEqual(stored["switch_when_used_pct"], 90)
        self.assertEqual(stored["telegram_bot_token"], "")

    def test_a_cancelled_reset_leaves_the_file_byte_identical(self) -> None:
        autoswitch.save_config({"switch_when_used_pct": 42})
        before = self.config_path.read_text(encoding="utf-8")
        rc, out, _ = self.run_menu(keys(char("r"), char("n"), char("q")))
        self.assertEqual(rc, 0)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertIn("[y/N]", out)

    def test_a_reset_keeps_a_key_this_version_does_not_declare(self) -> None:
        self.config_path.write_text(
            json.dumps({"enabled": True, "from_a_newer_ai_accounts": "keep me"}),
            encoding="utf-8",
        )
        rc, _, _ = self.run_menu(keys(char("r"), char("y"), char("q")))
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored()["from_a_newer_ai_accounts"], "keep me")
        self.assertIs(self.stored()["enabled"], False)

    def test_an_over_range_edit_clamps_and_reaches_disk(self) -> None:
        autoswitch.save_config({"switch_when_used_pct": 42})
        rc, _, _ = self.run_menu(
            keys(
                KeyEvent(Key.DOWN),
                KeyEvent(Key.ENTER),
                KeyEvent(Key.BACKSPACE),
                KeyEvent(Key.BACKSPACE),
                char("9"),
                char("9"),
                char("9"),
                KeyEvent(Key.ENTER),  # clamped to the maximum, not rejected
                char("q"),
            )
        )
        self.assertEqual(rc, 0)
        # 999 is clamped to 100, and *that* value is what autosave carries
        # to disk — never the raw out-of-range 999.
        self.assertEqual(self.stored()["switch_when_used_pct"], 100)
        self.assertNotIn("999", self.config_path.read_text(encoding="utf-8"))

    def test_a_rejected_edit_never_reaches_disk(self) -> None:
        autoswitch.save_config({"switch_when_used_pct": 42})
        rc, out, _ = self.run_menu(
            keys(
                KeyEvent(Key.DOWN),
                KeyEvent(Key.ENTER),
                KeyEvent(Key.BACKSPACE),
                KeyEvent(Key.BACKSPACE),
                char("x"),
                KeyEvent(Key.ENTER),  # rejected: not a number
                KeyEvent(Key.ESCAPE),
                char("q"),
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn("must be an integer 0-100", out)
        # The rejected "x" never entered `values`, so no autosave could carry
        # it to disk — the stored 42 stands.
        self.assertEqual(self.stored()["switch_when_used_pct"], 42)
        self.assertNotIn("999", self.config_path.read_text(encoding="utf-8"))

    def test_a_rejected_edit_then_quit_leaves_the_file_byte_identical(self) -> None:
        autoswitch.save_config({"switch_when_used_pct": 42})
        before = self.config_path.read_text(encoding="utf-8")
        rc, out, _ = self.run_menu(
            keys(
                KeyEvent(Key.DOWN),
                KeyEvent(Key.ENTER),
                char("x"),
                KeyEvent(Key.ENTER),  # rejected
                KeyEvent(Key.ESCAPE),
                char("q"),
            )
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_a_saved_secret_is_never_echoed_in_cleartext_afterwards(self) -> None:
        autoswitch.save_config({"telegram_bot_token": TOKEN})
        rc, out, _ = self.run_menu(keys(char("q")))
        self.assertEqual(rc, 0)
        self.assertNotIn(TOKEN, out)
        self.assertIn("****", out)

    def test_the_terminal_is_restored_when_the_loop_raises(self) -> None:
        fake_termios = mock.MagicMock()
        fake_termios.tcgetattr.return_value = ["ORIGINAL_ATTRS"]
        fake_tty = mock.MagicMock()

        def boom() -> KeyEvent:
            raise RuntimeError("mid-loop explosion")

        with mock.patch.object(cm.kr, "IS_WINDOWS", False), mock.patch.object(
            cm.kr, "_posix_modules", return_value=(fake_termios, fake_tty)
        ), mock.patch("os.isatty", return_value=True), mock.patch(
            "sys.stdin.fileno", return_value=0
        ):
            with self.assertRaises(RuntimeError):
                self.run_menu(boom)

        fake_termios.tcsetattr.assert_called_once_with(
            mock.ANY, fake_termios.TCSADRAIN, ["ORIGINAL_ATTRS"]
        )

    def test_keyboard_interrupt_mid_loop_exits_cleanly(self) -> None:
        def interrupt() -> KeyEvent:
            raise KeyboardInterrupt

        rc, _, _ = self.run_menu(interrupt)
        self.assertEqual(rc, 0)

    def test_the_redraw_does_not_leave_the_cursor_hidden(self) -> None:
        _, out, _ = self.run_menu(keys(KeyEvent(Key.DOWN), char("q")))
        self.assertNotIn("\033[?25l", out)

    def test_a_preexisting_invalid_notify_on_disk_surfaces_as_an_error_not_a_traceback(
        self,
    ) -> None:
        # A hand-edited config already has an out-of-range "notify" on disk.
        # Toggling an unrelated field ("enabled") and saving must not crash —
        # `save_config` rewrites the whole dict, so the pre-existing bad
        # "notify" poisons every save until it is fixed.
        self.config_path.write_text(json.dumps({"notify": "bogus"}), encoding="utf-8")
        rc, out, err = self.run_menu(keys(KeyEvent(Key.ENTER), char("q")))
        self.assertEqual(rc, 0)
        combined = out + err
        self.assertIn("invalid notify channel", combined)
        self.assertIn("bogus", combined)
        self.assertNotIn("Traceback", combined)
        # The write never happened: the "enabled" toggle did not reach disk.
        self.assertEqual(self.stored(), {"notify": "bogus"})

    def test_a_failed_write_is_reported_and_the_menu_still_exits_clean(self) -> None:
        saver = cm._Saver(save=mock.Mock(side_effect=ValueError("disk said no")))
        rc, out, err = self.run_menu(keys(KeyEvent(Key.ENTER), char("q")), saver=saver)
        self.assertEqual(rc, 0)
        # Reported on the next frame if the worker was that quick, at exit
        # otherwise — either way the user is told, and never by a traceback.
        self.assertIn("disk said no", out + err)
        self.assertNotIn("Traceback", out + err)

    def test_a_concurrent_write_survives_a_menu_save(self) -> None:
        # Terminal A opens the menu; terminal B then writes a new token. Saving
        # from A must merge A's own toggle, never rewind B's key — the menu once
        # posted its whole stale snapshot, so the on-disk token silently lost.
        other = "67890:FAKE-OTHER-TOKEN-PLACEHOLDER"
        self.config_path.write_text(
            json.dumps({"telegram_bot_token": TOKEN}), encoding="utf-8"
        )
        events = iter([KeyEvent(Key.ENTER), char("q")])
        wrote_from_b = False

        def read() -> KeyEvent:
            # Terminal B writes BEFORE A's first change, i.e. after A's snapshot
            # was taken — the ordering that used to lose B's key.
            nonlocal wrote_from_b
            if not wrote_from_b:
                autoswitch.save_config({"telegram_bot_token": other})  # terminal B
                wrote_from_b = True
            return next(events, KeyEvent(Key.CTRL_C))

        rc, _, _ = self.run_menu(read)
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored()["telegram_bot_token"], other)
        self.assertIs(self.stored()["enabled"], True)

    def test_an_untouched_key_is_never_written_to_the_file(self) -> None:
        rc, _, _ = self.run_menu(keys(KeyEvent(Key.ENTER), char("q")))
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored(), {"enabled": True})


class SaverTest(_ConfigFileMixin, unittest.TestCase):
    """Autosave writes off the key loop, so the write side needs its own tests:
    it must land, never block, never lose the newest value, and never raise."""

    def test_a_submitted_update_is_on_disk_once_close_returns(self) -> None:
        saver = cm._Saver()
        saver.submit({"enabled": True})
        self.assertIsNone(saver.close())
        self.assertEqual(self.stored(), {"enabled": True})

    def test_an_empty_update_never_touches_the_file(self) -> None:
        saver = cm._Saver()
        saver.submit({})
        self.assertIsNone(saver.close())
        self.assertFalse(self.config_path.exists())

    def test_the_newest_of_several_rapid_writes_is_the_one_on_disk(self) -> None:
        # Whether the worker coalesces them or writes each in turn, a later
        # value must never be overwritten by an earlier one.
        saver = cm._Saver()
        for value in (True, False, True):
            saver.submit({"enabled": value})
        self.assertIsNone(saver.close())
        self.assertIs(self.stored()["enabled"], True)

    def test_a_failed_write_is_reported_once_and_never_raised(self) -> None:
        saver = cm._Saver(save=mock.Mock(side_effect=ValueError("nope")))
        saver.submit({"enabled": True})
        self.assertEqual(saver.close(), "nope")
        self.assertIsNone(saver.take_error())

    def test_an_unwritable_config_is_an_error_not_a_crash(self) -> None:
        saver = cm._Saver(save=mock.Mock(side_effect=OSError("read-only fs")))
        saver.submit({"enabled": True})
        self.assertIn("read-only fs", saver.close())

    def test_a_submit_after_close_still_starts_a_new_worker(self) -> None:
        saver = cm._Saver()
        saver.submit({"enabled": True})
        saver.close()
        saver.submit({"switch_when_used_pct": 55})
        self.assertIsNone(saver.close())
        self.assertEqual(self.stored()["switch_when_used_pct"], 55)


class FallbackTest(_ConfigFileMixin, unittest.TestCase):
    """AC9: piped-empty stdin must exit 0, never hang, never traceback."""

    def fallback(self, stdin_lines=()):
        it = iter(stdin_lines)
        out, err = io.StringIO(), io.StringIO()
        # Exhausting the iterator raises StopIteration; the implementation must
        # instead hit its own EOFError path, so we raise EOFError explicitly.
        def fake_input(prompt: str = "") -> str:
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None

        with mock.patch("builtins.input", fake_input):
            with redirect_stdout(out), redirect_stderr(err):
                rc = cm.cmd_config([], prog="ai-accounts")
        return rc, out.getvalue(), err.getvalue()

    def setUp(self) -> None:
        super().setUp()
        tty = mock.patch.object(cm.kr, "is_interactive_tty", return_value=False)
        tty.start()
        self.addCleanup(tty.stop)

    def test_empty_stdin_lists_the_config_and_exits_zero(self) -> None:
        autoswitch.save_config({"telegram_bot_token": TOKEN})
        rc, out, _ = self.fallback([])  # input() raises StopIteration -> EOF path
        self.assertEqual(rc, 0)
        self.assertIn("Enable automatic switching", out)
        self.assertIn("1)", out)
        self.assertNotIn(TOKEN, out)

    def test_a_number_then_a_value_saves(self) -> None:
        rc, out, _ = self.fallback([str(index_of("notify") + 1), "telegram"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored()["notify"], "telegram")

    def test_an_invalid_value_is_rejected_without_writing(self) -> None:
        rc, out, err = self.fallback([str(index_of("notify") + 1), "carrier-pigeon"])
        self.assertEqual(rc, 1)
        self.assertFalse(self.config_path.exists())

    def test_an_out_of_range_number_is_rejected(self) -> None:
        rc, _, _ = self.fallback(["99"])
        self.assertEqual(rc, 1)

    def test_a_blank_selection_exits_zero(self) -> None:
        rc, _, _ = self.fallback([""])
        self.assertEqual(rc, 0)

    def test_a_blank_value_for_a_masked_field_keeps_the_stored_secret(self) -> None:
        autoswitch.save_config({"telegram_bot_token": TOKEN})
        token_number = str(index_of("telegram_bot_token") + 1)
        rc, out, _ = self.fallback([token_number, ""])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored()["telegram_bot_token"], TOKEN)
        self.assertNotIn(TOKEN, out)

    def test_a_blank_value_for_a_plain_field_still_clears_it(self) -> None:
        autoswitch.save_config({"telegram_chat_id": "12345"})
        rc, _, _ = self.fallback([str(index_of("telegram_chat_id") + 1), ""])
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored()["telegram_chat_id"], "")

    def test_it_never_enters_raw_mode(self) -> None:
        with mock.patch.object(cm.kr, "raw_mode") as raw:
            self.fallback([])
        raw.assert_not_called()

    def test_a_preexisting_invalid_notify_on_disk_is_a_clean_error_not_a_traceback(
        self,
    ) -> None:
        # A hand-edited config already has an out-of-range "notify" on disk.
        # Editing an unrelated field ("enabled") must not crash with a
        # traceback about "notify" — and the failed save must exit non-zero.
        self.config_path.write_text(json.dumps({"notify": "bogus"}), encoding="utf-8")
        rc, out, err = self.fallback([str(index_of("enabled") + 1), "true"])
        self.assertEqual(rc, 1)
        combined = out + err
        self.assertIn("invalid notify channel", combined)
        self.assertIn("bogus", combined)
        self.assertNotIn("Traceback", combined)


class ConfigSetupTimingTest(_ConfigFileMixin, unittest.TestCase):
    """Editing config never churns OS jobs; setup is explicit or prompted once."""

    def call(self, argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cm.cmd_config(argv, prog="ai-accounts")

    def menu_save(self, enabled: bool):
        state = cm.MenuState(
            values={**config_schema.defaults(), "enabled": enabled},
            touched=frozenset({"enabled"}),
            pending_save=True,
        )
        saver = cm._Saver()
        result = cm._save(state, saver)
        self.assertIsNone(saver.close())
        return result

    def fallback(self, lines):
        it = iter(lines)

        def fake_input(prompt: str = "") -> str:
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None

        with mock.patch.object(cm.kr, "is_interactive_tty", return_value=False), \
                mock.patch("builtins.input", fake_input), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cm.cmd_config([], prog="ai-accounts")

    def test_enabling_from_the_menu_only_changes_the_master_switch(self) -> None:
        self.menu_save(True)
        self.assertTrue(self.stored()["enabled"])

    def test_disabling_from_the_menu_leaves_setup_untouched(self) -> None:
        self.menu_save(False)
        self.assertFalse(self.stored()["enabled"])

    def test_config_set_true_does_not_install_again(self) -> None:
        self.assertEqual(self.call(["set", "enabled", "true"]), 0)
        self.assertTrue(self.stored()["enabled"])

    def test_config_set_off_does_not_uninstall(self) -> None:
        self.assertEqual(self.call(["set", "enabled", "off"]), 0)
        self.assertFalse(self.stored()["enabled"])

    def test_the_numbered_fallback_never_prompts_or_installs(self) -> None:
        self.assertEqual(self.fallback([str(index_of("enabled") + 1), "true"]), 0)
        self.assertTrue(self.stored()["enabled"])

    def test_saving_an_unrelated_key_needs_no_setup(self) -> None:
        self.assertEqual(self.call(["set", "switch_when_used_pct", "80"]), 0)

    def test_a_rejected_save_never_reaches_setup(self) -> None:
        self.config_path.write_text(json.dumps({"notify": "bogus"}), encoding="utf-8")
        self.assertEqual(self.call(["set", "enabled", "true"]), 1)
    def test_interactive_config_offers_one_time_setup_when_enabled(self) -> None:
        autoswitch.save_config({"enabled": True})
        with mock.patch.object(cm.kr, "is_interactive_tty", return_value=True), \
                mock.patch.object(cm, "run_menu", return_value=0), \
                mock.patch.object(cm, "_ask", return_value="y"), \
                mock.patch("ai_accounts.autoswitch_setup.is_installed", return_value=False), \
                mock.patch("ai_accounts.autoswitch_setup.install") as install:
            self.assertEqual(cm.cmd_config([], prog="ai-accounts"), 0)
        install.assert_called_once_with()

    def test_interactive_config_does_not_prompt_when_setup_exists(self) -> None:
        autoswitch.save_config({"enabled": True})
        with mock.patch.object(cm.kr, "is_interactive_tty", return_value=True), \
                mock.patch.object(cm, "run_menu", return_value=0), \
                mock.patch.object(cm, "_ask") as ask, \
                mock.patch("ai_accounts.autoswitch_setup.is_installed", return_value=True):
            self.assertEqual(cm.cmd_config([], prog="ai-accounts"), 0)
        ask.assert_not_called()


class CmdConfigLegacyTest(_ConfigFileMixin, unittest.TestCase):
    """AC8: byte-identical stdout and exit codes vs the legacy ai_accounts block."""

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cm.cmd_config(argv, prog="ai-accounts")
        return rc, out.getvalue(), err.getvalue()

    def test_get_with_no_key_prints_every_key_masked(self) -> None:
        autoswitch.save_config({"telegram_bot_token": TOKEN})
        rc, out, _ = self.call(["get"])
        self.assertEqual(rc, 0)
        self.assertIn("enabled = False\n", out)
        self.assertIn("switch_when_used_pct = 90\n", out)
        self.assertNotIn(TOKEN, out)

    def test_get_single_key(self) -> None:
        autoswitch.save_config({"switch_when_used_pct": 75})
        rc, out, _ = self.call(["get", "switch_when_used_pct"])
        self.assertEqual((rc, out), (0, "switch_when_used_pct = 75\n"))

    def test_get_unknown_key_exits_one_with_the_legacy_message(self) -> None:
        rc, out, err = self.call(["get", "bogus"])
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("Unknown config key: 'bogus'", err)
        self.assertIn(", ".join(sorted(autoswitch.DEFAULTS)), err)

    def test_set_writes_and_echoes_the_stored_value(self) -> None:
        rc, out, _ = self.call(["set", "switch_when_used_pct", "80"])
        self.assertEqual((rc, out), (0, "switch_when_used_pct = 80\n"))
        self.assertEqual(self.stored()["switch_when_used_pct"], 80)

    def test_set_parses_bool_strictly_and_echoes_python_repr(self) -> None:
        rc, out, _ = self.call(["set", "enabled", "false"])
        self.assertEqual((rc, out), (0, "enabled = False\n"))
        self.assertIs(self.stored()["enabled"], False)

    def test_set_unknown_key_is_rejected_and_does_not_write(self) -> None:
        rc, out, err = self.call(["set", "bogus_key", "1"])
        self.assertEqual(rc, 1)
        self.assertIn("Unknown config key", err)
        self.assertFalse(self.config_path.exists())

    def test_set_rejects_an_invalid_notify_channel(self) -> None:
        rc, _, err = self.call(["set", "notify", "carrier-pigeon"])
        self.assertEqual(rc, 1)
        self.assertIn("notify must be one of", err)
        self.assertFalse(self.config_path.exists())

    def test_set_clamps_an_over_range_pct_to_the_maximum(self) -> None:
        rc, out, _ = self.call(["set", "switch_when_used_pct", "150"])
        self.assertEqual((rc, out), (0, "switch_when_used_pct = 100\n"))
        self.assertEqual(self.stored()["switch_when_used_pct"], 100)

    def test_set_clamps_a_negative_pct_to_the_minimum(self) -> None:
        rc, out, _ = self.call(["set", "switch_when_used_pct", "-5"])
        self.assertEqual((rc, out), (0, "switch_when_used_pct = 0\n"))
        self.assertEqual(self.stored()["switch_when_used_pct"], 0)

    def test_set_rejects_a_non_numeric_pct(self) -> None:
        rc, _, err = self.call(["set", "switch_when_used_pct", "ninety"])
        self.assertEqual(rc, 1)
        self.assertIn("must be an integer 0-100, got 'ninety'", err)
        self.assertFalse(self.config_path.exists())

    def test_set_masks_a_secret_in_its_echo(self) -> None:
        rc, out, _ = self.call(["set", "telegram_bot_token", TOKEN])
        self.assertEqual(rc, 0)
        self.assertNotIn(TOKEN, out)
        self.assertEqual(self.stored()["telegram_bot_token"], TOKEN)

    def test_a_future_key_present_only_in_defaults_still_parses_strictly(self) -> None:
        defaults = {**autoswitch.DEFAULTS, "future_flag": False}
        with mock.patch.object(autoswitch, "DEFAULTS", defaults):
            rc, out, _ = self.call(["set", "future_flag", "false"])
        self.assertEqual(rc, 0)
        self.assertIs(self.stored()["future_flag"], False)

    def test_set_on_a_preexisting_invalid_notify_is_a_clean_error_not_a_traceback(
        self,
    ) -> None:
        # Third `save_config` caller, same defect class as the menu and the
        # fallback: a hand-edited "notify" already on disk must surface as a
        # one-line error with a non-zero exit, never a raw traceback.
        self.config_path.write_text(json.dumps({"notify": "bogus"}), encoding="utf-8")
        rc, out, err = self.call(["set", "enabled", "true"])
        self.assertEqual(rc, 1)
        combined = out + err
        self.assertIn("invalid notify channel", combined)
        self.assertIn("bogus", combined)
        self.assertNotIn("Traceback", combined)
        self.assertNotIn("enabled = True", out)

    def test_bad_usage_exits_one_with_the_prog_specific_line(self) -> None:
        rc, _, err = self.call(["frobnicate"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "Usage: ai-accounts config get [key] | config set <key> <value>", err
        )

    def test_the_prog_name_labels_the_panel_title(self) -> None:
        with mock.patch.object(cm, "run_menu", return_value=0) as run:
            with mock.patch.object(cm.kr, "is_interactive_tty", return_value=True):
                with mock.patch.object(cm, "package_version", return_value="9.9.9"):
                    cm.cmd_config([], prog="codex-accounts")
        self.assertEqual(run.call_args[0][0], "codex-accounts config (v9.9.9)")


# NOTE (T4): ByteIdentityAgainstLegacyTest used to compare this module's
# cmd_config against a legacy block in ai_accounts.py that pre-dated the
# shared implementation here. That block was the sole reason for its
# existence — it proved a byte-identical drop-in replacement was possible —
# and per T4's task it has since been deleted from ai_accounts.py (which now
# delegates to this module's cmd_config, like every other ai-accounts CLI).
# Removed alongside that deletion; tests/test_config_cli.py now covers the
# cross-tool wiring this class used to spot-check against ai_accounts alone.


if __name__ == "__main__":
    unittest.main()
