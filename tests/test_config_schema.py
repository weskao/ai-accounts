"""Tests for the declarative config field schema (``config_schema.py``).

The schema is the single source of truth for every ai-accounts config key: type,
default, allowed values, parser, masked-ness and human label. Two concerns:

  1. **One declaration site.** Every check here iterates :data:`FIELDS` instead
     of naming keys, so a seventh key needs one declarative entry and no test
     edit. The only place keys are spelled out is the back-compat pin below,
     which exists precisely to catch an accidental key *removal*.
  2. **Preserved semantics.** Strict true/false parsing, the inclusive 1-100
     range, the three notify channels, masking delegated to the existing
     ``autoswitch._mask``, and the byte-identical error messages the
     ``ai-accounts config set`` CLI already emits.

No real tokens or account data — placeholders only. Run with
``uv run pytest tests/test_config_schema.py -q``.
"""

from __future__ import annotations

import unittest
from unittest import mock

from ai_accounts import autoswitch as aw
from ai_accounts import config_schema as cs

FAKE_TOKEN = "12345:FAKE-TOKEN-PLACEHOLDER"


class SchemaShapeTests(unittest.TestCase):
    def test_every_field_carries_the_full_descriptor(self) -> None:
        # Given/When: the declared fields
        # Then: each one answers everything a menu or a CLI needs to ask
        for field in cs.FIELDS:
            with self.subTest(key=field.key):
                self.assertTrue(field.key)
                self.assertIsInstance(field.type, type)
                self.assertIsInstance(field.default, field.type)
                self.assertIsInstance(field.masked, bool)
                self.assertTrue(field.label)
                self.assertTrue(field.help)
                self.assertTrue(callable(field.parse))
                if field.choices is not None:
                    self.assertIn(field.default, field.choices)

    def test_fields_are_ordered_and_lookup_agrees_with_the_list(self) -> None:
        # Given: the ordered field list
        keys = [f.key for f in cs.FIELDS]
        # Then: no duplicates, and every key resolves back to its own field
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIs(cs.field(key), next(f for f in cs.FIELDS if f.key == key))

    def test_lookup_of_an_unknown_key_returns_none(self) -> None:
        self.assertIsNone(cs.field("no_such_key"))

    def test_defaults_mapping_is_derived_from_the_fields(self) -> None:
        # Given/When: the defaults mapping
        # Then: it is exactly the fields' defaults — no second hand-kept literal
        self.assertEqual(cs.defaults(), {f.key: f.default for f in cs.FIELDS})

    def test_every_default_parses_back_from_its_own_display_form(self) -> None:
        # Given: each field's default rendered the way the CLI prints it
        for field in cs.FIELDS:
            if field.masked:
                continue  # a masked display form is deliberately not round-trippable
            with self.subTest(key=field.key):
                shown = cs.format_value(field.key, field.default)
                self.assertEqual(field.parse(shown), field.default)


class BackCompatTests(unittest.TestCase):
    """The six keys shipped today must never silently disappear."""

    SHIPPED = {
        "enabled": False,
        "switch_when_used_pct": 90,
        "notify": "desktop",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "agy_blind_switch": False,
    }

    def test_autoswitch_defaults_still_holds_every_shipped_key_and_value(self) -> None:
        for key, default in self.SHIPPED.items():
            with self.subTest(key=key):
                self.assertIn(key, aw.DEFAULTS)
                self.assertIs(type(aw.DEFAULTS[key]), type(default))
                self.assertEqual(aw.DEFAULTS[key], default)

    def test_autoswitch_defaults_is_the_schema_defaults(self) -> None:
        self.assertEqual(aw.DEFAULTS, cs.defaults())

    def test_notify_channels_is_shared_not_copied(self) -> None:
        self.assertEqual(aw.NOTIFY_CHANNELS, ("desktop", "telegram", "none"))
        self.assertEqual(cs.field("notify").choices, aw.NOTIFY_CHANNELS)


class BoolParsingTests(unittest.TestCase):
    BOOL_KEYS = [f.key for f in cs.FIELDS if f.type is bool]

    def test_there_is_at_least_one_boolean_field_to_test(self) -> None:
        self.assertTrue(self.BOOL_KEYS)

    def test_false_strings_never_parse_as_true(self) -> None:
        # Given: the shapes a hand-edited config or a shell arg produces
        for key in self.BOOL_KEYS:
            for raw in ("false", "False", "FALSE", " false ", "0", "no", "off"):
                with self.subTest(key=key, raw=raw):
                    self.assertIs(cs.parse_value(key, raw), False)

    def test_true_strings_parse_as_true(self) -> None:
        for key in self.BOOL_KEYS:
            for raw in ("true", "TRUE", " True ", "1", "yes", "on"):
                with self.subTest(key=key, raw=raw):
                    self.assertIs(cs.parse_value(key, raw), True)

    def test_junk_is_rejected_with_the_cli_message(self) -> None:
        for key in self.BOOL_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    cs.parse_value(key, "maybe")
                self.assertEqual(
                    str(ctx.exception),
                    "expected a boolean (true/false), got 'maybe'",
                )


class ThresholdParsingTests(unittest.TestCase):
    KEY = "switch_when_used_pct"

    def test_the_inclusive_boundaries_are_accepted(self) -> None:
        for raw, expected in (("1", 1), ("100", 100), ("90", 90)):
            with self.subTest(raw=raw):
                self.assertEqual(cs.parse_value(self.KEY, raw), expected)

    def test_out_of_range_values_are_rejected_with_the_cli_message(self) -> None:
        for raw, shown in (("0", "0"), ("101", "101"), ("-1", "-1")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    cs.parse_value(self.KEY, raw)
                self.assertEqual(
                    str(ctx.exception),
                    f"{self.KEY} must be an integer 1-100, got {shown}",
                )

    def test_a_non_integer_is_rejected_with_the_cli_message(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cs.parse_value(self.KEY, "ninety")
        self.assertEqual(
            str(ctx.exception),
            f"{self.KEY} must be an integer 1-100, got 'ninety'",
        )


class ChoiceParsingTests(unittest.TestCase):
    def test_every_declared_choice_round_trips(self) -> None:
        for field in cs.FIELDS:
            if field.choices is None:
                continue
            for choice in field.choices:
                with self.subTest(key=field.key, choice=choice):
                    self.assertEqual(cs.parse_value(field.key, choice), choice)

    def test_an_unlisted_choice_is_rejected_with_the_cli_message(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cs.parse_value("notify", "carrier-pigeon")
        self.assertEqual(
            str(ctx.exception),
            "notify must be one of desktop, telegram, none, got 'carrier-pigeon'",
        )


class FreeFormParsingTests(unittest.TestCase):
    def test_free_form_string_fields_take_any_value_verbatim(self) -> None:
        for field in cs.FIELDS:
            if field.type is not str or field.choices is not None:
                continue
            with self.subTest(key=field.key):
                self.assertEqual(cs.parse_value(field.key, FAKE_TOKEN), FAKE_TOKEN)

    def test_parsing_an_unknown_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            cs.parse_value("no_such_key", "x")
        self.assertIn("no_such_key", str(ctx.exception))


class MaskingTests(unittest.TestCase):
    def test_exactly_the_secret_fields_are_masked(self) -> None:
        masked = {f.key for f in cs.FIELDS if f.masked}
        self.assertEqual(masked, {"telegram_bot_token"})

    def test_masked_display_delegates_to_the_existing_autoswitch_mask(self) -> None:
        # Given: every masked field
        for field in cs.FIELDS:
            if not field.masked:
                continue
            with self.subTest(key=field.key):
                # Then: display is exactly what autoswitch._mask returns
                self.assertEqual(
                    cs.format_value(field.key, FAKE_TOKEN), aw._mask(FAKE_TOKEN)
                )
                self.assertNotIn(FAKE_TOKEN, cs.format_value(field.key, FAKE_TOKEN))
                self.assertEqual(cs.format_value(field.key, ""), "")

    def test_masked_config_masks_every_masked_field_without_naming_keys(self) -> None:
        # Given: a config where every masked field holds a placeholder secret
        secrets = {f.key: FAKE_TOKEN for f in cs.FIELDS if f.masked}
        with mock.patch.object(aw, "load_config", lambda: {**cs.defaults(), **secrets}):
            shown = aw.masked_config()
        # Then: none of them survives the trip to stdout
        self.assertTrue(secrets)
        for key in secrets:
            with self.subTest(key=key):
                self.assertEqual(shown[key], aw._mask(FAKE_TOKEN))
        self.assertNotIn(FAKE_TOKEN, repr(shown))

    def test_unmasked_fields_are_shown_verbatim(self) -> None:
        self.assertEqual(cs.format_value("switch_when_used_pct", 75), "75")
        self.assertEqual(cs.format_value("notify", "telegram"), "telegram")
        self.assertEqual(cs.format_value("enabled", False), "false")
        self.assertEqual(cs.format_value("enabled", True), "true")
        self.assertEqual(cs.format_value("telegram_chat_id", "12345"), "12345")


class GrowthTests(unittest.TestCase):
    """Adding a key must be one declarative entry and nothing else."""

    def test_a_seventh_field_needs_only_a_descriptor(self) -> None:
        # Given: a hypothetical new field appended to the schema
        new = cs.Field(
            key="future_flag",
            type=bool,
            default=False,
            label="Future flag",
            help="A hypothetical future switch.",
        )
        fields = (*cs.FIELDS, new)
        with mock.patch.object(cs, "FIELDS", fields):
            # Then: lookup, defaults and parsing all work with no other change
            self.assertIs(cs.field("future_flag"), new)
            self.assertIs(cs.defaults()["future_flag"], False)
            self.assertIs(cs.parse_value("future_flag", "false"), False)
            self.assertEqual(cs.format_value("future_flag", True), "true")


if __name__ == "__main__":
    unittest.main()
