"""Event-triggered autoswitch hook configuration and dispatch."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import autoswitch_hooks as hooks
from ai_accounts import _utils as u


class HookConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.home / ".codex"),
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "GEMINI_HOME": str(self.home / ".gemini"),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

        # `agy` is only a provider where the OS credential store is reachable;
        # pin it so these cases assert the same hook set on every runner.
        keyring = mock.patch.object(u, "go_keyring_available", return_value=(True, ""))
        keyring.start()
        self.addCleanup(keyring.stop)

    def read(self, relative: str) -> dict:
        return json.loads((self.home / relative).read_text(encoding="utf-8"))

    def test_install_merges_all_quota_provider_hooks_once(self) -> None:
        claude = self.home / ".claude" / "settings.json"
        claude.parent.mkdir(parents=True)
        claude.write_text(
            json.dumps({"model": "sonnet", "hooks": {"Stop": [{"hooks": [{"command": "keep"}]}]}}),
            encoding="utf-8",
        )
        agy = self.home / ".gemini" / "config" / "hooks.json"
        agy.parent.mkdir(parents=True)
        agy.write_text(json.dumps({"orca-status": {"Stop": []}}), encoding="utf-8")

        hooks.install()
        hooks.install()

        codex = self.read(".codex/hooks.json")
        self.assertEqual(len(codex["hooks"]["Stop"]), 1)
        self.assertEqual(
            codex["hooks"]["Stop"][0]["hooks"][0]["command"], hooks.command("codex")
        )
        claude_data = self.read(".claude/settings.json")
        self.assertEqual(claude_data["model"], "sonnet")
        self.assertEqual(claude_data["hooks"]["Stop"][0]["hooks"][0]["command"], "keep")
        self.assertEqual(len(claude_data["hooks"]["Stop"]), 2)
        self.assertEqual(
            claude_data["hooks"]["Stop"][1]["hooks"][0]["command"], hooks.command("claude")
        )
        agy_data = self.read(".gemini/config/hooks.json")
        self.assertIn("orca-status", agy_data)
        self.assertEqual(
            agy_data[hooks.MANAGED_HOOK]["Stop"][0]["command"], hooks.command("agy")
        )

    def test_uninstall_removes_only_ai_accounts_hooks(self) -> None:
        hooks.install()
        codex = self.home / ".codex" / "hooks.json"
        data = json.loads(codex.read_text(encoding="utf-8"))
        data["hooks"]["Stop"].append({"hooks": [{"command": "keep"}]})
        codex.write_text(json.dumps(data), encoding="utf-8")

        hooks.uninstall()

        self.assertEqual(self.read(".codex/hooks.json")["hooks"]["Stop"], [{"hooks": [{"command": "keep"}]}])
        self.assertNotIn(hooks.MANAGED_HOOK, self.read(".gemini/config/hooks.json"))

    def test_install_rewrites_hooks_left_by_another_interpreter(self) -> None:
        stale_codex = "/old/venv/bin/python3 -m ai_accounts.autoswitch_hooks run codex"
        stale_agy = "/old/venv/bin/python3 -m ai_accounts.autoswitch_hooks run agy"
        codex = self.home / ".codex" / "hooks.json"
        codex.parent.mkdir(parents=True)
        codex.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"command": "keep"}, {"command": stale_codex}]},
                            {"hooks": [{"command": stale_codex}]},
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        agy = self.home / ".gemini" / "config" / "hooks.json"
        agy.parent.mkdir(parents=True)
        agy.write_text(
            json.dumps({hooks.MANAGED_HOOK: {"Stop": [{"command": stale_agy}]}}),
            encoding="utf-8",
        )

        self.assertFalse(hooks.is_installed())
        hooks.install()

        self.assertTrue(hooks.is_installed())
        stop = self.read(".codex/hooks.json")["hooks"]["Stop"]
        self.assertEqual(
            [hook["command"] for group in stop for hook in group["hooks"]],
            ["keep", hooks.command("codex")],
        )
        agy_data = self.read(".gemini/config/hooks.json")
        self.assertEqual(agy_data[hooks.MANAGED_HOOK]["Stop"][0]["command"], hooks.command("agy"))

    def test_install_leaves_a_foreign_managed_key_alone(self) -> None:
        agy = self.home / ".gemini" / "config" / "hooks.json"
        agy.parent.mkdir(parents=True)
        foreign: dict = {hooks.MANAGED_HOOK: {"Stop": [{"command": "someone else"}]}}
        agy.write_text(json.dumps(foreign), encoding="utf-8")

        hooks.install()

        self.assertEqual(self.read(".gemini/config/hooks.json"), foreign)

    def test_an_unreachable_credential_store_skips_the_agy_hook(self) -> None:
        with mock.patch.object(u, "go_keyring_available", return_value=(False, "no secret-tool")):
            hooks.install()
            self.assertTrue(hooks.is_installed())

        self.assertTrue((self.home / ".codex" / "hooks.json").exists())
        self.assertTrue((self.home / ".claude" / "settings.json").exists())
        self.assertFalse((self.home / ".gemini" / "config" / "hooks.json").exists())


class HookRunnerTests(unittest.TestCase):
    def test_runner_reuses_the_provider_autoswitch_command_and_returns_hook_json(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="switched", stderr="")
        out = io.StringIO()
        with mock.patch.object(hooks.subprocess, "run", return_value=result) as run, redirect_stdout(out):
            rc = hooks.main(["run", "claude"])

        self.assertEqual(rc, 0)
        run.assert_called_once_with(
            [sys.executable, "-m", "ai_accounts.claude_accounts", "autoswitch"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(out.getvalue(), "{}\n")
