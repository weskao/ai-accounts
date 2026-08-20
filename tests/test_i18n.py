"""Language resolution, the message catalogue, and notification framing."""

from __future__ import annotations

import os
import tempfile
import urllib.parse
import unittest
from pathlib import Path
from unittest import mock

from ai_accounts import autoswitch as aw
from ai_accounts import config_schema as cs
from ai_accounts import i18n


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
            if msgid.split(".", 1)[0] in ("config", "group", "menu", "error", "value"):
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

    def test_unset_language_defaults_to_the_os_locale_at_install_time(self) -> None:
        # Given: no "language" key ever written — a fresh install
        aw.save_config({"enabled": True})
        # Then: the baked-in default is a real language, not the "auto"
        # sentinel (a field's default must be one of its own choices), and it
        # is the language this process's OS locale resolved to at import
        self.assertIn(aw.DEFAULTS["language"], i18n.LANGUAGE_CODES)
        self.assertEqual(aw.DEFAULTS["language"], i18n.system_language())
        self.assertEqual(i18n.current_language(), aw.DEFAULTS["language"])


class ConfigLabelTests(_ConfigMixin):
    def test_labels_and_help_follow_the_language(self) -> None:
        # Given: a field whose label is translated
        field = cs._require("enabled")
        aw.save_config({"language": "zh-TW"})
        # Then: the display forms are translated, the schema literals are not
        self.assertEqual(field.display_label(), "啟用自動切換")
        self.assertNotEqual(field.display_label(), field.label)
        self.assertIn("配額", field.display_help())

    def test_english_falls_back_to_the_schema_text(self) -> None:
        # Given: English, which the catalogue deliberately does not duplicate
        aw.save_config({"language": "en"})
        for field in cs.FIELDS:
            with self.subTest(key=field.key):
                # Then: the schema's own literal is what shows
                self.assertEqual(field.display_label(), field.label)
                self.assertEqual(field.display_help(), field.help)

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
        self.assertEqual(field.choices, i18n.LANGUAGE_CODES)
        # Only the two real languages cycle — "auto" is the unset default's
        # internal sentinel, never a third row to land on.
        self.assertNotIn(i18n.AUTO, field.choices or ())
        self.assertEqual(field.parse("zh-TW"), "zh-TW")
        with self.assertRaises(ValueError):
            field.parse("klingon")
        with self.assertRaises(ValueError):
            field.parse(i18n.AUTO)


class LanguageChoiceDisplayTests(_ConfigMixin):
    """The row shows a language's own name, never its code."""

    def test_each_language_is_named_in_its_own_script(self) -> None:
        labels = i18n.language_labels()
        self.assertEqual(labels["en"], "English")
        self.assertEqual(labels["zh-TW"], "繁體中文")

    def test_auto_reports_the_language_it_resolves_to(self) -> None:
        # Given: the OS asking for Traditional Chinese
        # Then: the unset default's row shows that plain language name — no
        # separate "system" wording, since it is not an offered choice
        with mock.patch.object(i18n, "system_language", return_value="zh-TW"):
            self.assertEqual(i18n.language_labels()[i18n.AUTO], "繁體中文")
        with mock.patch.object(i18n, "system_language", return_value="en"):
            self.assertEqual(i18n.language_labels()[i18n.AUTO], "English")

    def test_the_menu_shows_the_name_and_config_get_shows_the_code(self) -> None:
        # Given: the language field
        field = cs._require("language")
        # Then: the menu form is a name...
        self.assertEqual(field.display_value("zh-TW"), "繁體中文")
        # ...while the CLI form stays the code, so `config get` output can be
        # fed straight back to `config set`
        self.assertEqual(field.format("zh-TW"), "zh-TW")
        self.assertEqual(field.parse(field.format("zh-TW")), "zh-TW")

    def test_display_value_is_the_plain_value_for_a_field_without_labels(self) -> None:
        field = cs._require("switch_when_used_pct")
        self.assertEqual(field.display_value(90), "90")

    def test_a_masked_field_is_never_expanded_into_a_label(self) -> None:
        field = cs._require("telegram_bot_token")
        shown = field.display_value("0000000000:secret-tail")
        self.assertNotIn("secret", shown)
        self.assertTrue(shown.startswith("*"))

    def test_language_stands_alone_in_its_own_group(self) -> None:
        # Given: the schema. Then: `language` is not filed under a feature group
        field = cs._require("language")
        self.assertEqual(field.group, "General")
        self.assertEqual([f.key for f in cs.FIELDS if f.group == "General"], ["language"])


class MenuPreviewTests(_ConfigMixin):
    """Cycling the language repaints the menu before anything is saved."""

    def _labels(self, values: dict) -> str:
        from ai_accounts import config_menu as cm

        return "\n".join(cm.render("t", cs.FIELDS, values, cursor=0))

    def test_an_unsaved_language_change_is_previewed_immediately(self) -> None:
        # Given: English on disk
        aw.save_config({"language": "en"})
        # When: the menu holds an unsaved zh-TW in its edit buffer
        frame = self._labels({"language": "zh-TW"})
        # Then: labels, help and the key hints are already Traditional Chinese
        self.assertIn("啟用自動切換", frame)
        self.assertIn("語言", frame)
        self.assertIn("移動", frame)  # footer hint
        # And: the stored setting is untouched until `s` is pressed
        self.assertEqual(aw.load_config()["language"], "en")

    def test_english_renders_the_schema_text(self) -> None:
        frame = self._labels({"language": "en"})
        self.assertIn("Enable automatic switching", frame)
        self.assertIn("select", frame)

    def test_rows_stay_aligned_when_labels_change_width(self) -> None:
        # Given: CJK labels, which are twice as wide per character
        from ai_accounts._present import visible_len

        for lang in ("en", "zh-TW"):
            with self.subTest(lang=lang):
                lines = __import__(
                    "ai_accounts.config_menu", fromlist=["render"]
                ).render("t", cs.FIELDS, {"language": lang}, cursor=0)
                widths = {visible_len(line) for line in lines}
                # Then: every rendered line still measures the same
                self.assertEqual(len(widths), 1, sorted(widths))


class TelegramPayloadTests(_ConfigMixin):
    """The Bot API payload — plain text, no frame, no parse mode."""

    def _capture(self) -> list[object]:
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

        self.urlopen = mock.patch.object(aw.urllib.request, "urlopen", fake_urlopen)
        self.urlopen.start()
        self.addCleanup(self.urlopen.stop)
        return opened

    def test_the_message_is_posted_as_plain_text(self) -> None:
        # Given: telegram configured
        opened = self._capture()
        # When: a notification goes out
        self.assertTrue(aw.notify("🔄 codex: work → personal", "⚠️ restart needed"))
        # Then: title and body, one newline between, and nothing else — no box
        # frame and no parse_mode, so Telegram shows the text as written
        body = opened[0].data.decode("utf-8")
        self.assertNotIn("parse_mode", body)
        self.assertNotIn("%E2%94%8C", body)  # ┌ — the old frame
        self.assertIn("codex", urllib.parse.unquote_plus(body))
        self.assertIn(
            "🔄 codex: work → personal\n⚠️ restart needed",
            urllib.parse.unquote_plus(body),
        )

    def test_an_empty_body_sends_the_title_alone(self) -> None:
        # Given: a title with no follow-up line
        opened = self._capture()
        self.assertTrue(aw.notify("only a title", ""))
        # Then: no trailing newline left dangling
        text = urllib.parse.parse_qs(opened[0].data.decode("utf-8"))["text"][0]
        self.assertEqual(text, "only a title")


if __name__ == "__main__":
    unittest.main()
