"""Tests for the ai-accounts all-provider lister.

Verify the aggregation logic — providers print in completion order (not
declaration order) with a live shrinking-remaining-count spinner, and
exit-code propagation — without spawning real subprocesses.
Run with: ``python -m unittest discover tests``.
"""

from __future__ import annotations

import io
import subprocess
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ai_accounts import ai_accounts as aa


def _fake(
    module: str, stdout: str, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python", "-m", module, "list"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


class AiAccountsTest(unittest.TestCase):
    def test_list_prints_providers_in_completion_order(self) -> None:
        # codex is held back until both other providers have finished, so a
        # correct implementation must print claude/agy before codex even
        # though codex is declared first in _TOOLS.
        codex_may_finish = threading.Event()
        finished = []
        lock = threading.Lock()

        def run(module: str) -> subprocess.CompletedProcess[str]:
            if module == "ai_accounts.codex_accounts":
                codex_may_finish.wait(timeout=5)
                return _fake(module, "CODEX-TABLE")
            table = {
                "ai_accounts.claude_accounts": "CLAUDE-TABLE",
                "ai_accounts.gemini_accounts": "AGY-TABLE",
                "ai_accounts.grok_accounts": "GROK-TABLE",
            }[module]
            result = _fake(module, table)
            with lock:
                finished.append(module)
                if len(finished) == 2:
                    codex_may_finish.set()
            return result

        buf = io.StringIO()
        with mock.patch.object(aa, "_run_list", side_effect=run):
            with redirect_stdout(buf):
                rc = aa.cmd_list()
        text = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertLess(text.index("CLAUDE-TABLE"), text.index("CODEX-TABLE"))
        self.assertLess(text.index("AGY-TABLE"), text.index("CODEX-TABLE"))
        self.assertLess(text.index("GROK-TABLE"), text.index("CODEX-TABLE"))
        for label in (
            "codex-accounts",
            "claude-accounts",
            "agy-accounts",
            "grok-accounts",
        ):
            self.assertIn(label, text)

    def test_list_spinner_messages_count_down_as_providers_finish(self) -> None:
        messages = []

        class RecordingSpinner:
            def __init__(self, message: str) -> None:
                messages.append(message)

            def __enter__(self) -> "RecordingSpinner":
                return self

            def __exit__(self, *exc_info: object) -> None:
                return None

        outputs = {
            "ai_accounts.codex_accounts": _fake("ai_accounts.codex_accounts", "CODEX-TABLE"),
            "ai_accounts.claude_accounts": _fake(
                "ai_accounts.claude_accounts", "CLAUDE-TABLE"
            ),
            "ai_accounts.gemini_accounts": _fake("ai_accounts.gemini_accounts", "AGY-TABLE"),
            "ai_accounts.grok_accounts": _fake("ai_accounts.grok_accounts", "GROK-TABLE"),
        }
        with mock.patch.object(aa, "Spinner", RecordingSpinner):
            with mock.patch.object(aa, "_run_list", side_effect=lambda m: outputs[m]):
                with redirect_stdout(io.StringIO()):
                    aa.cmd_list()
        self.assertEqual(
            messages,
            [
                "Fetching accounts from 4 providers…",
                "Fetching remaining 3 providers…",
                "Fetching remaining 2 providers…",
                "Fetching remaining 1 provider…",
            ],
        )

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
            [
                "ai_accounts.codex_accounts",
                "ai_accounts.claude_accounts",
                "ai_accounts.gemini_accounts",
                "ai_accounts.grok_accounts",
            ],
        )
        for c in calls:
            self.assertEqual(c[-2:], ["refresh", "--all"])

    def test_every_forwarded_command_uses_the_same_provider_path(self) -> None:
        cases = (
            ["who"],
            ["current"],
            ["save", "work"],
            ["switch", "work"],
            ["remove", "work"],
            ["refresh"],
            ["sync"],
            ["login-switch", "work"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                calls = []

                def run(cmd, *a, **k):
                    calls.append(cmd)
                    return subprocess.CompletedProcess(args=cmd, returncode=0)

                with mock.patch.object(aa.subprocess, "run", side_effect=run):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(aa.main(argv), 0)

                self.assertEqual(len(calls), len(aa._TOOLS))
                self.assertEqual([call[2] for call in calls], [module for _, module in aa._TOOLS])
                self.assertTrue(all(call[3:] == argv for call in calls))


if __name__ == "__main__":
    unittest.main()
