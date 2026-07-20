"""Tests for the ai-accounts all-provider lister.

Verify the aggregation logic — fixed provider order regardless of completion
order, and exit-code propagation — without spawning real subprocesses.
Run with: ``python -m unittest discover tests``.
"""

from __future__ import annotations

import io
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ai_accounts import ai_accounts as aa


def _fake(module: str, stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python", "-m", module, "list"], returncode=returncode, stdout=stdout, stderr=""
    )


class AiAccountsTest(unittest.TestCase):
    def test_list_prints_providers_in_fixed_order(self) -> None:
        outputs = {
            "ai_accounts.codex_accounts": _fake("ai_accounts.codex_accounts", "CODEX-TABLE"),
            "ai_accounts.claude_accounts": _fake("ai_accounts.claude_accounts", "CLAUDE-TABLE"),
            "ai_accounts.gemini_accounts": _fake("ai_accounts.gemini_accounts", "AGY-TABLE"),
        }
        buf = io.StringIO()
        with mock.patch.object(aa, "_run_list", side_effect=lambda m: outputs[m]):
            with redirect_stdout(buf):
                rc = aa.cmd_list()
        text = buf.getvalue()
        self.assertEqual(rc, 0)
        # Fixed order: codex → claude → agy, regardless of thread completion order.
        self.assertLess(text.index("CODEX-TABLE"), text.index("CLAUDE-TABLE"))
        self.assertLess(text.index("CLAUDE-TABLE"), text.index("AGY-TABLE"))
        for label in ("codex-accounts", "claude-accounts", "agy-accounts"):
            self.assertIn(label, text)

    def test_list_propagates_nonzero_exit(self) -> None:
        def run(module: str) -> subprocess.CompletedProcess[str]:
            rc = 3 if module == "ai_accounts.gemini_accounts" else 0
            return _fake(module, "x", returncode=rc)

        with mock.patch.object(aa, "_run_list", side_effect=run):
            with redirect_stdout(io.StringIO()):
                rc = aa.cmd_list()
        self.assertEqual(rc, 3)

    def test_unknown_command_returns_1(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(aa.main(["bogus"]), 1)


if __name__ == "__main__":
    unittest.main()
