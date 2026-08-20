"""Cross-tool coverage for the shared ``config`` subcommand.

All six ai-accounts CLIs (``ai-accounts`` and the five per-provider tools)
delegate ``config`` to :func:`ai_accounts.config_menu.cmd_config` — one shared
implementation, six one-line dispatches. This file proves the wiring, not
the implementation (that's ``tests/test_config_menu.py``'s job): each tool's
``main()`` must route ``config`` / ``config get`` / ``config set`` to the
shared entry point, and ``ai-accounts`` must never let ``config`` fall
through its fan-out dispatcher.

Fixtures use placeholder data only.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import (
    ai_accounts,
    autoswitch,
    claude_accounts,
    codex_accounts,
    gemini_accounts,
    grok_accounts,
    vibe_accounts,
)
from ai_accounts import _keyreader

# (module, prog label used in panel titles / usage lines)
_MODULES = (
    (ai_accounts, "ai-accounts"),
    (codex_accounts, "codex-accounts"),
    (claude_accounts, "claude-accounts"),
    (gemini_accounts, "agy-accounts"),
    (grok_accounts, "grok-accounts"),
    (vibe_accounts, "vibe-accounts"),
)


class _ConfigFileMixin(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_tmp.cleanup)
        self.config_path = Path(self.config_tmp.name) / "config.json"
        env = mock.patch.dict(
            os.environ, {"AI_ACCOUNTS_CONFIG_JSON": str(self.config_path)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)
        # agy-accounts refuses to run at all off macOS; the `config`
        # subcommand is intercepted before that platform gate in every
        # module's main(), but patch it out anyway so this test is
        # platform-independent rather than accidentally macOS-only.
        platform_patch = mock.patch.object(gemini_accounts.sys, "platform", "darwin")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)


class AllFiveToolsConfigDispatchTest(_ConfigFileMixin):
    """AC2: every tool accepts config / config get / config set."""

    def _main(self, module, argv):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            rc = module.main(argv)
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_config_no_args_prints_numbered_fallback_when_non_tty(self) -> None:
        for module, prog in _MODULES:
            with self.subTest(prog=prog):
                with mock.patch("builtins.input", side_effect=EOFError):
                    with mock.patch.object(
                        _keyreader, "is_interactive_tty", return_value=False
                    ):
                        rc, out, _ = self._main(module, ["config"])
                self.assertEqual(rc, 0)
                self.assertIn(f"{prog} config", out)
                self.assertIn("1)", out)

    def test_config_get_no_key_lists_every_key(self) -> None:
        for module, prog in _MODULES:
            with self.subTest(prog=prog):
                rc, out, _ = self._main(module, ["config", "get"])
                self.assertEqual(rc, 0)
                self.assertIn("switch_when_used_pct", out)
                # Unlike the set/get-single-key tests below, `config get`
                # with no key never writes (it's read-only), so the file may
                # not exist yet on the first module's iteration.
                if self.config_path.exists():
                    self.config_path.unlink()

    def test_config_get_single_key(self) -> None:
        for module, prog in _MODULES:
            with self.subTest(prog=prog):
                autoswitch.save_config({"switch_when_used_pct": 42})
                rc, out, _ = self._main(module, ["config", "get", "switch_when_used_pct"])
                self.assertEqual((rc, out), (0, "switch_when_used_pct = 42\n"))
                self.config_path.unlink()

    def test_config_set_writes_and_echoes_unchanged(self) -> None:
        for module, prog in _MODULES:
            with self.subTest(prog=prog):
                rc, out, _ = self._main(
                    module, ["config", "set", "switch_when_used_pct", "80"]
                )
                self.assertEqual((rc, out), (0, "switch_when_used_pct = 80\n"))
                self.assertEqual(autoswitch.load_config()["switch_when_used_pct"], 80)
                self.config_path.unlink()

    def test_config_set_masked_token_echoes_masked_not_cleartext(self) -> None:
        token = "12345:FAKE-TOKEN-PLACEHOLDER"
        for module, prog in _MODULES:
            with self.subTest(prog=prog):
                rc, out, _ = self._main(
                    module, ["config", "set", "telegram_bot_token", token]
                )
                self.assertEqual(rc, 0)
                self.assertNotIn(token, out)
                self.assertEqual(
                    autoswitch.load_config()["telegram_bot_token"], token
                )
                self.config_path.unlink()

    def test_config_set_unknown_key_rejected(self) -> None:
        for module, _prog in _MODULES:
            with self.subTest(module=module.__name__):
                rc, _out, err = self._main(module, ["config", "set", "bogus", "1"])
                self.assertEqual(rc, 1)
                self.assertIn("Unknown config key", err)
                self.assertFalse(self.config_path.exists())

    def test_config_never_forwards_to_a_provider_subprocess(self) -> None:
        # ai-accounts specifically: `config` must be handled locally, never
        # fanned out to the four provider subprocesses.
        with mock.patch.object(ai_accounts.subprocess, "run") as run:
            rc, _out, _err = self._main(ai_accounts, ["config", "get"])
        self.assertEqual(rc, 0)
        run.assert_not_called()

    def test_agy_config_works_off_macos(self) -> None:
        # The class-level setUp patches sys.platform to "darwin" so every
        # other test above is platform-independent; that patch alone would
        # hide a regression where `config` regresses behind agy-accounts'
        # macOS-only Keychain gate. Un-patch it here and prove `config` still
        # works — it edits the shared cross-platform config file, not
        # anything Keychain-backed.
        with mock.patch.object(gemini_accounts.sys, "platform", "linux"):
            rc, out, _ = self._main(gemini_accounts, ["config", "get", "notify"])
        self.assertEqual((rc, out), (0, "notify = desktop\n"))


class AiAccountsConfigNotInCommandsTest(unittest.TestCase):
    """AC3: `config` must stay out of the fan-out command set — regression
    guard against re-introducing the four-subprocess-menu bug."""

    def test_config_absent_from_commands(self) -> None:
        self.assertNotIn("config", ai_accounts._COMMANDS)


class HelpMentionsConfigTest(unittest.TestCase):
    """AC7: every tool's help text lists the config command."""

    def test_help_lists_config(self) -> None:
        for module, prog in _MODULES:
            with self.subTest(module=module.__name__):
                # `f"{prog} config"`, not a bare "config" substring — every
                # module's HELP already mentions "~/.ai-accounts/config.json",
                # which would make a bare substring check pass vacuously.
                self.assertIn(f"{prog} config", module.HELP)


class NoDuplicateConfigImplementationTest(unittest.TestCase):
    """AC4: exactly one cmd_config implementation exists in the codebase."""

    def test_only_config_menu_defines_cmd_config(self) -> None:
        import ast
        import pathlib

        src_dir = pathlib.Path(__file__).resolve().parent.parent / "src" / "ai-accounts"
        owners = []
        for path in src_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "cmd_config":
                    owners.append(path.name)
        self.assertEqual(sorted(owners), ["config_menu.py"])

    def test_no_legacy_helpers_remain_in_ai_accounts(self) -> None:
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "src"
            / "ai-accounts"
            / "ai_accounts.py"
        ).read_text(encoding="utf-8")
        for name in (
            "_parse_bool",
            "_parse_config_value",
            "cmd_config_get",
            "cmd_config_set",
        ):
            self.assertNotIn(name, src)


if __name__ == "__main__":
    unittest.main()
