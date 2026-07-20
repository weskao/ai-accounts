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

    def test_no_args_prints_help_without_running_providers(self) -> None:
        buf = io.StringIO()
        with mock.patch.object(aa.subprocess, "run") as run:
            with redirect_stdout(buf):
                rc = aa.main([])
        self.assertEqual(rc, 0)
        self.assertIn("USAGE", buf.getvalue())
        run.assert_not_called()

    def test_forward_passes_command_and_args_to_every_provider(self) -> None:
        calls = []

        def run(cmd, *a, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with mock.patch.object(aa.subprocess, "run", side_effect=run):
            with redirect_stdout(io.StringIO()):
                rc = aa.main(["refresh", "--all"])
        self.assertEqual(rc, 0)
        modules = [c[2] for c in calls]  # cmd = [python, -m, <module>, ...]
        self.assertEqual(
            modules,
            ["ai_accounts.codex_accounts", "ai_accounts.claude_accounts", "ai_accounts.gemini_accounts"],
        )
        for c in calls:
            self.assertEqual(c[-2:], ["refresh", "--all"])


if __name__ == "__main__":
    unittest.main()
