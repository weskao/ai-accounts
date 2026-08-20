"""Tests for the ai-accounts all-provider lister.

Verify the aggregation logic — providers print in completion order (not
declaration order) with a live shrinking-remaining-count spinner, and
exit-code propagation — without spawning real subprocesses.
Run with: ``python -m unittest discover tests``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import ai_accounts as aa
from ai_accounts import autoswitch


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
        # Codex is released only after all other futures have been yielded, so a
        # correct implementation must print the other providers before codex even
        # though codex is declared first in _TOOLS.
        codex_may_finish = threading.Event()
        completed_yields = 0

        class ReleaseCodexAfterFourYields:
            def __init__(self, message: str) -> None:
                pass

            def __enter__(self) -> "ReleaseCodexAfterFourYields":
                return self

            def __exit__(self, *exc_info: object) -> None:
                nonlocal completed_yields
                completed_yields += 1
                if completed_yields == 4:
                    codex_may_finish.set()

        def run(module: str) -> subprocess.CompletedProcess[str]:
            if module == "ai_accounts.codex_accounts":
                codex_may_finish.wait(timeout=5)
                return _fake(module, "CODEX-TABLE")
            table = {
                "ai_accounts.claude_accounts": "CLAUDE-TABLE",
                "ai_accounts.gemini_accounts": "AGY-TABLE",
                "ai_accounts.grok_accounts": "GROK-TABLE",
                "ai_accounts.vibe_accounts": "VIBE-TABLE",
            }[module]
            return _fake(module, table)

        buf = io.StringIO()
        with mock.patch.object(aa, "Spinner", ReleaseCodexAfterFourYields):
            with mock.patch.object(aa, "_run_list", side_effect=run):
                with redirect_stdout(buf):
                    rc = aa.cmd_list()
        text = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertLess(text.index("CLAUDE-TABLE"), text.index("CODEX-TABLE"))
        self.assertLess(text.index("AGY-TABLE"), text.index("CODEX-TABLE"))
        self.assertLess(text.index("GROK-TABLE"), text.index("CODEX-TABLE"))
        self.assertLess(text.index("VIBE-TABLE"), text.index("CODEX-TABLE"))
        for label in (
            "codex-accounts",
            "claude-accounts",
            "agy-accounts",
            "grok-accounts",
            "vibe-accounts",
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
            "ai_accounts.vibe_accounts": _fake("ai_accounts.vibe_accounts", "VIBE-TABLE"),
        }
        with mock.patch.object(aa, "Spinner", RecordingSpinner):
            with mock.patch.object(aa, "_run_list", side_effect=lambda m: outputs[m]):
                with redirect_stdout(io.StringIO()):
                    aa.cmd_list()
        self.assertEqual(
            messages,
            [
                "Fetching accounts from 5 providers…",
                "Fetching remaining 4 providers…",
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
                "ai_accounts.vibe_accounts",
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
            ["autoswitch"],
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

    def test_autoswitch_with_a_trailing_subcommand_is_rejected_not_forwarded(self) -> None:
        # Given: the plausible-but-wrong spelling of the timer install. Every
        # provider ignores a trailing arg after `autoswitch`, so forwarding it
        # would run four quota checks and install NOTHING — silently, while the
        # user believes a scheduler was registered.
        calls: list[list[str]] = []

        def run(cmd, *a, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        # When: `ai-accounts autoswitch install-timer` runs
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(aa.subprocess, "run", side_effect=run), \
                mock.patch.object(aa.autoswitch_timer, "install") as install:
            with redirect_stdout(out), redirect_stderr(err):
                rc = aa.main(["autoswitch", "install-timer"])

        # Then: it fails loudly, names the working spelling, installs nothing
        # and never spawns a provider
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [])
        install.assert_not_called()
        self.assertIn("ai-accounts install-timer", out.getvalue() + err.getvalue())

    def test_autoswitch_rejects_any_trailing_argument(self) -> None:
        # Given: `autoswitch` takes no arguments at all — a profile name or a
        # typo would be swallowed by all four providers just as silently.
        for argv in (["autoswitch", "--interval", "600"], ["autoswitch", "work"]):
            with self.subTest(argv=argv):
                with mock.patch.object(aa.subprocess, "run") as run:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        rc = aa.main(argv)
                self.assertEqual(rc, 1)
                run.assert_not_called()


class _ConfigMixin:
    """Point the autoswitch config store at a throwaway temp file."""

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


class AiAccountsConfigTest(_ConfigMixin, unittest.TestCase):
    def test_config_get_no_key_masks_telegram_token_without_forwarding(self) -> None:
        # Given: a stored telegram token
        autoswitch.save_config({"telegram_bot_token": "sk-fake-1234567890abcd"})

        # When: `ai-accounts config get` runs with no key
        buf = io.StringIO()
        with mock.patch.object(aa.subprocess, "run") as run:
            with redirect_stdout(buf):
                rc = aa.main(["config", "get"])

        # Then: it's handled locally (zero subprocess calls) and the token is masked
        self.assertEqual(rc, 0)
        run.assert_not_called()
        output = buf.getvalue()
        self.assertNotIn("sk-fake-1234567890abcd", output)
        self.assertIn("abcd", output)

    def test_config_get_single_key(self) -> None:
        autoswitch.save_config({"switch_when_used_pct": 75})
        buf = io.StringIO()
        with mock.patch.object(aa.subprocess, "run") as run:
            with redirect_stdout(buf):
                rc = aa.main(["config", "get", "switch_when_used_pct"])
        self.assertEqual(rc, 0)
        run.assert_not_called()
        self.assertIn("75", buf.getvalue())

    def test_a_new_boolean_default_is_parsed_strictly_without_a_second_list(self) -> None:
        # Given: a future boolean key added to autoswitch.DEFAULTS and nowhere
        # else. `set` must learn which keys are booleans from DEFAULTS itself —
        # a hand-maintained second list silently stores "false" as the truthy
        # STRING "false", the exact shape every read now has to fail closed on.
        defaults = {**autoswitch.DEFAULTS, "future_flag": False}

        # When: the new key is set to false
        with mock.patch.object(autoswitch, "DEFAULTS", defaults):
            with redirect_stdout(io.StringIO()):
                rc = aa.main(["config", "set", "future_flag", "false"])

        # Then: a real JSON false landed on disk, not a string
        self.assertEqual(rc, 0)
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIs(stored["future_flag"], False)

    def test_config_set_unknown_key_is_rejected_and_does_not_write(self) -> None:
        buf = io.StringIO()
        with mock.patch.object(aa.subprocess, "run") as run:
            with redirect_stdout(io.StringIO()):
                with redirect_stderr(buf):
                    rc = aa.main(["config", "set", "bogus_key", "1"])
        self.assertEqual(rc, 1)
        run.assert_not_called()
        self.assertIn("Unknown config key", buf.getvalue())
        self.assertFalse(self.config_path.exists())

    def test_config_set_rejects_invalid_notify_channel(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = aa.main(["config", "set", "notify", "carrier-pigeon"])
        self.assertEqual(rc, 1)
        self.assertFalse(self.config_path.exists())

    def test_config_set_rejects_out_of_range_pct(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = aa.main(["config", "set", "switch_when_used_pct", "150"])
        self.assertEqual(rc, 1)
        self.assertFalse(self.config_path.exists())

    def test_config_set_parses_bool_strictly(self) -> None:
        # Given/When: "false" is set for a boolean key
        with redirect_stdout(io.StringIO()):
            rc = aa.main(["config", "set", "enabled", "false"])
        # Then: it's stored as the real bool False, not Python's bool("false") == True
        self.assertEqual(rc, 0)
        self.assertIs(autoswitch.load_config()["enabled"], False)

    def test_config_set_writes_a_valid_key(self) -> None:
        with redirect_stdout(io.StringIO()):
            rc = aa.main(["config", "set", "switch_when_used_pct", "80"])
        self.assertEqual(rc, 0)
        self.assertEqual(autoswitch.load_config()["switch_when_used_pct"], 80)


class AiAccountsTimerTest(_ConfigMixin, unittest.TestCase):
    def test_install_timer_does_not_forward_and_installs_default_interval(self) -> None:
        buf = io.StringIO()
        with mock.patch.object(aa.subprocess, "run") as run:
            with mock.patch.object(aa.autoswitch_timer, "install") as install:
                with redirect_stdout(buf):
                    rc = aa.main(["install-timer"])
        self.assertEqual(rc, 0)
        run.assert_not_called()
        install.assert_called_once_with(aa.autoswitch_timer.DEFAULT_INTERVAL_SEC)

    def test_install_timer_parses_interval_flag(self) -> None:
        with mock.patch.object(aa.subprocess, "run") as run:
            with mock.patch.object(aa.autoswitch_timer, "install") as install:
                with redirect_stdout(io.StringIO()):
                    rc = aa.main(["install-timer", "--interval", "60"])
        self.assertEqual(rc, 0)
        run.assert_not_called()
        install.assert_called_once_with(60)

    def test_uninstall_timer_does_not_forward(self) -> None:
        with mock.patch.object(aa.subprocess, "run") as run:
            with mock.patch.object(aa.autoswitch_timer, "uninstall") as uninstall:
                with redirect_stdout(io.StringIO()):
                    rc = aa.main(["uninstall-timer"])
        self.assertEqual(rc, 0)
        run.assert_not_called()
        uninstall.assert_called_once_with()

    def test_timer_status_does_not_forward_and_prints_status(self) -> None:
        buf = io.StringIO()
        with mock.patch.object(aa.subprocess, "run") as run:
            with mock.patch.object(
                aa.autoswitch_timer, "status", return_value="installed"
            ):
                with redirect_stdout(buf):
                    rc = aa.main(["timer-status"])
        self.assertEqual(rc, 0)
        run.assert_not_called()
        self.assertIn("installed", buf.getvalue())


class AiAccountsHelpTest(unittest.TestCase):
    def test_help_lists_the_new_commands(self) -> None:
        for snippet in (
            "autoswitch",
            "config get",
            "config set",
            "install-timer",
            "uninstall-timer",
            "timer-status",
        ):
            self.assertIn(snippet, aa.HELP)


if __name__ == "__main__":
    unittest.main()
