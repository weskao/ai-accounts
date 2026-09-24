"""Ctrl-C during list exits 130 and prints no traceback."""

from __future__ import annotations

import importlib
import io
import signal
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ai_accounts import _utils as u

# (module, extra patches applied before main(["list"]))
_LIST_COMMANDS = (
    ("ai_accounts.gemini_accounts", {"go_keyring_available": (True, "")}),
    ("ai_accounts.claude_accounts", {}),
    ("ai_accounts.codex_accounts", {}),
    ("ai_accounts.grok_accounts", {}),
    ("ai_accounts.vibe_accounts", {}),
    ("ai_accounts.copilot_accounts", {}),
    ("ai_accounts.ai_accounts", {}),
)


class QuietKeyboardInterruptTests(unittest.TestCase):
    def test_ctrl_c_returns_130_without_a_traceback(self) -> None:
        @u.quiet_keyboard_interrupt
        def boom() -> int:
            raise KeyboardInterrupt

        err = io.StringIO()
        with redirect_stderr(err):
            code = boom()

        self.assertEqual(code, 130)
        text = err.getvalue()
        self.assertNotIn("Traceback", text)
        self.assertNotIn("KeyboardInterrupt", text)
        self.assertNotIn("❌", text)

    def test_a_normal_return_passes_through(self) -> None:
        @u.quiet_keyboard_interrupt
        def ok() -> int:
            return 7

        self.assertEqual(ok(), 7)

    def test_every_provider_list_exits_quietly(self) -> None:
        for module_name, extras in _LIST_COMMANDS:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                patches = [
                    mock.patch.object(module, "cmd_list", side_effect=KeyboardInterrupt),
                    *(
                        mock.patch.object(module, name, return_value=value)
                        for name, value in extras.items()
                    ),
                ]
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    for patch in patches:
                        patch.start()
                    try:
                        code = module.main(["list"])
                    finally:
                        for patch in patches:
                            patch.stop()
                text = stdout.getvalue() + stderr.getvalue()
                self.assertEqual(code, 130, text)
                self.assertNotIn("Traceback", text)
                self.assertNotIn("KeyboardInterrupt", text)
                self.assertNotIn("❌", text)

    def test_a_real_sigint_exits_130_without_a_traceback(self) -> None:
        script = (
            "import time\n"
            "from ai_accounts import _utils as u\n"
            "@u.quiet_keyboard_interrupt\n"
            "def main():\n"
            "    time.sleep(30)\n"
            "    return 0\n"
            "raise SystemExit(main())\n"
        )
        popen_kwargs = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_kwargs,
        )
        time.sleep(0.4)
        sig = signal.CTRL_C_EVENT if sys.platform == "win32" else signal.SIGINT
        process.send_signal(sig)
        stdout, stderr = process.communicate(timeout=5)
        text = stdout + stderr
        self.assertEqual(process.returncode, 130, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("KeyboardInterrupt", text)
