"""Every account tool answers bad input the same way: typo hint + usage line."""

import importlib
import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# module -> program name shown in its usage line
TOOLS = {
    "claude_accounts": "claude-accounts",
    "codex_accounts": "codex-accounts",
    "gemini_accounts": "agy-accounts",
    "grok_accounts": "grok-accounts",
    "vibe_accounts": "vibe-accounts",
    "copilot_accounts": "copilot-accounts",
    "devin_accounts": "devin-accounts",
    "opencode_accounts": "opencode-accounts",
}


def _run(module: str, argv: list[str]) -> tuple[int, str]:
    mod = importlib.import_module(f"ai_accounts.{module}")
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        rc = mod.main(argv)
    return rc, _ANSI_RE.sub("", err.getvalue())


class CommandSurfaceTests(unittest.TestCase):
    def test_typo_suggests_closest_command(self) -> None:
        for module in TOOLS:
            with self.subTest(module=module):
                rc, err = _run(module, ["login-swtich", "test"])
                self.assertEqual(rc, 1)
                self.assertIn("Unknown command: login-swtich", err)
                self.assertIn("Did you mean: login-switch?", err)

    def test_nonsense_has_no_suggestion(self) -> None:
        for module in TOOLS:
            with self.subTest(module=module):
                rc, err = _run(module, ["frobnicate"])
                self.assertEqual(rc, 1)
                self.assertIn("Unknown command: frobnicate", err)
                self.assertNotIn("Did you mean", err)

    def test_login_switch_without_name_prints_usage(self) -> None:
        for module, prog in TOOLS.items():
            with self.subTest(module=module):
                rc, err = _run(module, ["login-switch"])
                self.assertEqual(rc, 1)
                self.assertIn(f"Usage: {prog} login-switch <profile_name>", err)

    def test_ai_accounts_typo_suggests(self) -> None:
        rc, err = _run("ai_accounts", ["login-swtich", "test"])
        self.assertEqual(rc, 1)
        self.assertIn("Did you mean: login-switch?", err)


if __name__ == "__main__":
    unittest.main()
