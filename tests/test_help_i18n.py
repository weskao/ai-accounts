"""`--help` output follows the configured `language`, same as the config menu.

English wording stays the canonical source in each module's ``HELP`` constant
(the same asymmetric convention ``i18n.MESSAGES`` already uses for config
labels); only the zh-TW translation lives in the catalogue, looked up with
``HELP`` itself as the fallback default. Every module also runs its resolved
text through ``_present.format_help`` before printing, which colors the title,
section headers, and command entries — the wording is untouched (recovered
exactly by ``_present.strip_ansi``), only ANSI escapes are added.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import (
    _present,
    ai_accounts,
    autoswitch as aw,
    claude_accounts,
    codex_accounts,
    gemini_accounts,
    grok_accounts,
    i18n,
    vibe_accounts,
)

_MODULES = (
    ai_accounts,
    codex_accounts,
    claude_accounts,
    gemini_accounts,
    grok_accounts,
    vibe_accounts,
)


class HelpFollowsLanguageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = Path(self.tmp.name) / "config.json"
        env = mock.patch.dict(
            os.environ, {"AI_ACCOUNTS_CONFIG_JSON": str(config)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        i18n.refresh()
        self.addCleanup(i18n.refresh)

    def _help_output(self, module) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = module.main([])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_zh_tw_translates_help(self) -> None:
        aw.save_config({"language": "zh-TW"})
        for module in _MODULES:
            with self.subTest(module=module.__name__):
                output = self._help_output(module)
                self.assertIn("用法", output)
                self.assertNotEqual(_present.strip_ansi(output).strip(), module.HELP.strip())

    def test_en_help_is_unchanged(self) -> None:
        aw.save_config({"language": "en"})
        for module in _MODULES:
            with self.subTest(module=module.__name__):
                output = self._help_output(module)
                self.assertEqual(_present.strip_ansi(output).strip(), module.HELP.strip())
                self.assertEqual(output.strip(), _present.format_help(module.HELP).strip())

    def test_format_help_colors_without_changing_wording(self) -> None:
        for module in _MODULES:
            with self.subTest(module=module.__name__):
                colored = _present.format_help(module.HELP)
                self.assertIn("\033[", colored)
                self.assertEqual(_present.strip_ansi(colored), module.HELP)


if __name__ == "__main__":
    unittest.main()
