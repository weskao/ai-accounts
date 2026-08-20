"""One-time setup is shared by the command and interactive config prompt."""

from __future__ import annotations

import unittest
from unittest import mock

from ai_accounts import autoswitch_setup as setup


class AutoswitchSetupTest(unittest.TestCase):
    def test_install_sets_up_both_the_event_hook_and_fallback_timer(self) -> None:
        with mock.patch("ai_accounts.autoswitch_timer.install") as install_timer, mock.patch(
            "ai_accounts.autoswitch_hooks.install"
        ) as install_hooks:
            setup.install()

        install_timer.assert_called_once_with()
        install_hooks.assert_called_once_with()

    def test_status_requires_both_parts(self) -> None:
        with mock.patch("ai_accounts.autoswitch_timer.status", return_value="installed"), mock.patch(
            "ai_accounts.autoswitch_hooks.is_installed", return_value=True
        ):
            self.assertTrue(setup.is_installed())
        with mock.patch("ai_accounts.autoswitch_timer.status", return_value="not installed"), mock.patch(
            "ai_accounts.autoswitch_hooks.is_installed", return_value=True
        ):
            self.assertFalse(setup.is_installed())
