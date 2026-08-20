"""Language resolution, the message catalogue, and notification framing."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_accounts import autoswitch as aw
from ai_accounts import config_schema as cs
from ai_accounts import i18n
from ai_accounts._present import notify_width, plain_box


class _ConfigMixin(unittest.TestCase):
    """Point the config store at a throwaway temp dir for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "config.json"
        env = mock.patch.dict(
            os.environ, {"AI_ACCOUNTS_CONFIG_JSON": str(self.config)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        i18n.refresh()
        self.addCleanup(i18n.refresh)


class SystemLanguageTests(unittest.TestCase):
    """The OS locale — every platform's spelling of it — maps to one language."""

    def _with_locale(self, value: str | None, windows: str | None = None) -> str:
        # No POSIX vars set is the Windows case: getdefaultlocale answers there.
        env = {var: value for var in ("LC_ALL", "LC_MESSAGES", "LANG") if value}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(
                i18n.locale, "getdefaultlocale", return_value=(windows, "UTF-8")
            ):
                return i18n.system_language()

    def test_posix_locales_resolve_to_their_language(self) -> None:
        # Given/When/Then: the spellings macOS and Linux actually export
        for value, expected in [
            ("zh_TW.UTF-8", "zh-TW"),
            ("zh_TW", "zh-TW"),
            ("zh_Hant", "zh-TW"),
            ("zh_HK.UTF-8", "zh-TW"),
            ("en_US.UTF-8", "en"),
            ("en_GB", "en"),
        ]:
            with self.subTest(locale=value):
                self.assertEqual(self._with_locale(value), expected)

    def test_windows_locale_is_read_when_no_posix_vars_exist(self) -> None:
        # Given: a Windows box — no LANG/LC_*, GetUserDefaultLocaleName answers
        # When/Then: the language still resolves from that value
        self.assertEqual(self._with_locale(None, windows="zh_TW"), "zh-TW")
        self.assertEqual(self._with_locale(None, windows="en_US"), "en")

    def test_unknown_or_absent_locale_falls_back_to_english(self) -> None:
        # Given: locales that name no supported language
        # Then: English — never a guess at a neighbouring language
        for value in ("C", "POSIX", "C.UTF-8", "de_DE.UTF-8", "ja_JP.UTF-8"):
            with self.subTest(locale=value):
                self.assertEqual(self._with_locale(value), "en")
        self.assertEqual(self._with_locale(None, windows=None), "en")

    def test_simplified_chinese_does_not_resolve_to_traditional(self) -> None:
        # Given: a Simplified locale, which has no entry of its own yet
        # Then: English, because Traditional text would be the wrong script
        for value in ("zh_CN.UTF-8", "zh_SG", "zh_Hans"):
            with self.subTest(locale=value):
                self.assertEqual(self._with_locale(value), "en")

    def test_lc_all_wins_over_lang_as_posix_requires(self) -> None:
        # Given: LC_ALL and LANG disagreeing
        with mock.patch.dict(
            os.environ, {"LC_ALL": "zh_TW.UTF-8", "LANG": "en_US.UTF-8"}, clear=True
        ):
            # Then: LC_ALL decides
            self.assertEqual(i18n.system_language(), "zh-TW")


class ResolveLanguageTests(unittest.TestCase):
    def test_an_explicit_code_is_used_verbatim(self) -> None:
        self.assertEqual(i18n.resolve_language("zh-TW"), "zh-TW")
        self.assertEqual(i18n.resolve_language("en"), "en")

    def test_auto_junk_and_none_all_defer_to_the_system(self) -> None:
        # Given: the system asking for Traditional Chinese
        with mock.patch.object(i18n, "system_language", return_value="zh-TW"):
            # Then: every non-code value means "ask the OS" — a hand-edited
            # typo degrades to the default instead of raising in a notifier
            for value in ("auto", "", None, "zh_TW", "klingon", 7):
                with self.subTest(value=value):
                    self.assertEqual(i18n.resolve_language(value), "zh-TW")


class CatalogueTests(unittest.TestCase):
    def test_every_message_has_the_fallback_language(self) -> None:
        # Given: the catalogue. Then: nothing can fall through to a bare msgid
        for msgid, table in i18n.MESSAGES.items():
            if msgid.split(".", 1)[0] in ("config", "group", "menu"):
                continue  # English for these lives in config_schema.FIELDS
            with self.subTest(msgid=msgid):
                self.assertIn(i18n.FALLBACK, table)

    def test_declared_languages_are_the_only_ones_used(self) -> None:
        # Given: the catalogue. Then: no stray language key nobody can select
        for msgid, table in i18n.MESSAGES.items():
            with self.subTest(msgid=msgid):
                self.assertLessEqual(set(table), set(i18n.LANGUAGE_CODES))

    def test_titles_are_single_line_because_the_box_puts_them_on_one_row(self) -> None:
        for msgid, table in i18n.MESSAGES.items():
            if not msgid.endswith(".title"):
                continue
            for lang, text in table.items():
                with self.subTest(msgid=msgid, lang=lang):
                    self.assertNotIn("\n", text)

    def test_an_untranslated_message_falls_back_to_english(self) -> None:
        # Given: a language with no translation of this id
        text = i18n.t("notify.revoked.body", lang="zh-TW")
        zh = i18n.MESSAGES["notify.revoked.body"]["zh-TW"]
        self.assertEqual(text, zh)
        # And: an id present only in English answers in English either way
        with mock.patch.dict(
            i18n.MESSAGES, {"probe.only-en": {"en": "english only"}}, clear=False
        ):
            self.assertEqual(i18n.t("probe.only-en", lang="zh-TW"), "english only")

    def test_a_missing_id_or_field_never_raises(self) -> None:
        # Given: an id nobody declared. Then: the id itself, not a KeyError
        self.assertEqual(i18n.t("nope.not.here", lang="en"), "nope.not.here")
        # And: the caller's default wins over the bare id when supplied
        self.assertEqual(i18n.t("nope.not.here", lang="en", default="hi"), "hi")
        # And: a placeholder with no matching keyword stays literal
        self.assertIn("{provider}", i18n.t("notify.switched.title", lang="en"))


class CachedLanguageTests(_ConfigMixin):
    def test_the_configured_language_is_used_for_notifications(self) -> None:
        # Given: the config asking for Traditional Chinese
        aw.save_config({"language": "zh-TW"})
        # Then: the catalogue answers in it
        self.assertEqual(i18n.current_language(), "zh-TW")
        self.assertIn("已用", i18n.t("notify.no_candidate.title", provider="agy",
                                     profile="work", used=96))

    def test_changing_the_language_takes_effect_without_a_restart(self) -> None:
        # Given: the language read once and cached
        aw.save_config({"language": "en"})
        self.assertEqual(i18n.current_language(), "en")
        # When: the setting changes through save_config (what the menu calls)
        aw.save_config({"language": "zh-TW"})
        # Then: the next lookup sees it — the cache was dropped on write
        self.assertEqual(i18n.current_language(), "zh-TW")

    def test_auto_follows_the_system_locale(self) -> None:
        # Given: language left at its "auto" default
        aw.save_config({"enabled": True})
        # When/Then: the OS decides
        with mock.patch.object(i18n, "system_language", return_value="zh-TW"):
            i18n.refresh()
            self.assertEqual(i18n.current_language(), "zh-TW")

    def test_language_defaults_to_auto_so_a_fresh_install_follows_the_os(self) -> None:
        self.assertEqual(aw.DEFAULTS["language"], i18n.AUTO)


class ConfigLabelTests(_ConfigMixin):
    def test_labels_and_help_follow_the_language(self) -> None:
        # Given: a field whose label is translated
        field = cs._require("enabled")
        aw.save_config({"language": "zh-TW"})
        # Then: the display forms are translated, the schema literals are not
        self.assertEqual(field.display_label, "啟用自動切換")
        self.assertNotEqual(field.display_label, field.label)
        self.assertIn("配額", field.display_help)

    def test_english_falls_back_to_the_schema_text(self) -> None:
        # Given: English, which the catalogue deliberately does not duplicate
        aw.save_config({"language": "en"})
        for field in cs.FIELDS:
            with self.subTest(key=field.key):
                # Then: the schema's own literal is what shows
                self.assertEqual(field.display_label, field.label)
                self.assertEqual(field.display_help, field.help)

    def test_every_field_is_translated_in_every_declared_language(self) -> None:
        # Given: each language other than the English source
        for lang in i18n.LANGUAGE_CODES:
            if lang == i18n.FALLBACK:
                continue
            for field in cs.FIELDS:
                with self.subTest(lang=lang, key=field.key):
                    # Then: no config row silently shows English in a zh menu
                    self.assertIn(lang, i18n.MESSAGES[f"config.{field.key}.label"])
                    self.assertIn(lang, i18n.MESSAGES[f"config.{field.key}.help"])

    def test_group_headings_are_translated_in_every_declared_language(self) -> None:
        groups = {f.group for f in cs.FIELDS if f.group is not None}
        for lang in i18n.LANGUAGE_CODES:
            if lang == i18n.FALLBACK:
                continue
            for group in groups:
                with self.subTest(lang=lang, group=group):
                    # Then: no heading stays English in an otherwise-zh menu
                    self.assertIn(lang, i18n.MESSAGES[f"group.{group}"])

    def test_language_is_a_cyclable_choice_the_menu_can_offer(self) -> None:
        field = cs._require("language")
        self.assertEqual(field.choices, i18n.CHOICES)
        self.assertIn(i18n.AUTO, field.choices or ())
        self.assertEqual(field.parse("zh-TW"), "zh-TW")
        with self.assertRaises(ValueError):
            field.parse("klingon")


class NotifyWidthTests(unittest.TestCase):
    def test_wide_glyphs_and_emoji_count_two_columns(self) -> None:
        self.assertEqual(notify_width("ab"), 2)
        self.assertEqual(notify_width("中文"), 4)
        self.assertEqual(notify_width("🔄"), 2)
        # ⚠️ is a narrow codepoint that renders wide with its variation selector
        self.assertEqual(notify_width("⚠️"), 2)
        # → is Ambiguous: one column in the monospace font a notification uses
        self.assertEqual(notify_width("→"), 1)


class PlainBoxTests(unittest.TestCase):
    def test_every_row_is_padded_to_one_common_width(self) -> None:
        # Given: lines mixing CJK, emoji and ASCII — the alignment hard case
        lines = ["🔄 codex: work (93%) → personal (15%)", "⚠️ 需重啟 session"]
        box = plain_box(lines)
        rendered = box.splitlines()
        # Then: the frame and every content row measure the same
        widths = {notify_width(line) for line in rendered}
        self.assertEqual(len(widths), 1, rendered)
        # And: the frame closes
        self.assertTrue(rendered[0].startswith("┌"))
        self.assertTrue(rendered[-1].startswith("└"))
        self.assertEqual(len(rendered), len(lines) + 2)

    def test_content_survives_framing_verbatim(self) -> None:
        box = plain_box(["🚫 agy：work 已用 96%，無帳號可切"])
        self.assertIn("無帳號可切", box)


class TelegramPayloadTests(_ConfigMixin):
    def test_the_message_is_posted_as_a_monospace_block(self) -> None:
        # Given: a title and a body
        text = aw.telegram_text("🔄 codex: work → personal", "⚠️ restart needed")
        # Then: <pre>, so Telegram renders the frame monospace and it lines up
        self.assertTrue(text.startswith("<pre>"))
        self.assertTrue(text.endswith("</pre>"))
        self.assertIn("┌", text)
        self.assertIn("└", text)

    def test_angle_brackets_in_a_message_are_not_eaten_as_html(self) -> None:
        # Given: a body containing a literal <provider> placeholder
        text = aw.telegram_text("title", "<provider>-accounts login-switch <name>")
        # Then: it is escaped, so Telegram shows it instead of dropping a "tag"
        self.assertIn("&lt;provider&gt;", text)
        self.assertNotIn("<provider>", text)

    def test_an_empty_body_does_not_leave_a_blank_row(self) -> None:
        text = aw.telegram_text("only a title", "")
        self.assertEqual(len(text.splitlines()), 3)  # top, content, bottom

    def test_the_api_call_declares_html_parse_mode(self) -> None:
        # Given: telegram configured
        aw.save_config(
            {"notify": "telegram", "telegram_bot_token": "t", "telegram_chat_id": "1"}
        )
        opened: list[object] = []

        class _Response:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            opened.append(request)
            return _Response()

        # When: a notification goes out
        with mock.patch.object(aw.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(aw.notify("t", "m"))
        # Then: parse_mode travels with it — without it the <pre> is literal
        body = opened[0].data.decode("utf-8")
        self.assertIn("parse_mode=HTML", body)


if __name__ == "__main__":
    unittest.main()
