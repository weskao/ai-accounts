"""Tests for the auto-switch shared foundation (config store, notifications).

All filesystem access is redirected into a temp dir via AI_ACCOUNTS_CONFIG_JSON;
every subprocess and HTTP call is mocked — no notifications, no network, no
real tokens. Run with: ``uv run pytest``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from ai_accounts import _utils as u
from ai_accounts import autoswitch as aw
from ai_accounts.usage_format import UsageWindow


class _ConfigMixin(unittest.TestCase):
    """Point the config store at a throwaway temp dir for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = self.home / "config.json"
        env = mock.patch.dict(
            os.environ, {"AI_ACCOUNTS_CONFIG_JSON": str(self.config)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)


class ConfigPathTests(_ConfigMixin):
    def test_config_path_honors_the_env_var_override(self) -> None:
        # Given: AI_ACCOUNTS_CONFIG_JSON points at a temp file (see setUp)
        # When: the module resolves its config path
        path = aw.config_path()
        # Then: it is the override, not the real ~/.ai-accounts/config.json
        self.assertEqual(path, self.config)


class ConfigDefaultsTests(_ConfigMixin):
    def test_switch_when_used_pct_defaults_to_90_with_no_file(self) -> None:
        # Given: no config file exists yet
        self.assertFalse(self.config.exists())
        # When: the config is loaded
        cfg = aw.load_config()
        # Then: the documented defaults come back
        self.assertEqual(cfg["switch_when_used_pct"], 90)
        self.assertIs(cfg["enabled"], False)
        self.assertEqual(cfg["notify"], "desktop")
        self.assertEqual(cfg["telegram_bot_token"], "")
        self.assertEqual(cfg["telegram_chat_id"], "")
        self.assertIs(cfg["agy_blind_switch"], False)

    def test_threshold_is_documented_as_a_used_percentage(self) -> None:
        # Given/When: the module docstring is the contract for the direction
        # Then: it pins USED-percent semantics with a worked example
        self.assertIn(
            "switch_when_used_pct = 90 means: switch once 90% of the quota is "
            "USED, i.e. when 10% remains.",
            aw.__doc__ or "",
        )


class ConfigWriteTests(_ConfigMixin):
    def test_first_write_creates_the_file_with_mode_0600(self) -> None:
        # Given: no config file exists yet
        self.assertFalse(self.config.exists())
        # When: a setting is saved
        aw.save_config({"enabled": True})
        # Then: the file exists, owner-only, and holds the value
        self.assertTrue(self.config.exists())
        if os.name == "posix":
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertIs(aw.load_config()["enabled"], True)

    def test_unknown_pre_existing_keys_survive_a_write(self) -> None:
        # Given: a config file already carrying a key this module knows nothing about
        self.config.write_text(
            '{"enabled": false, "future_option": "keep-me"}', encoding="utf-8"
        )
        # When: an unrelated setting is written
        aw.save_config({"switch_when_used_pct": 75})
        # Then: the foreign key is still on disk alongside the new value
        stored = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(stored["future_option"], "keep-me")
        self.assertEqual(stored["switch_when_used_pct"], 75)


class ConfigValidationTests(_ConfigMixin):
    def test_an_invalid_notify_channel_is_rejected_with_a_clear_message(self) -> None:
        # Given: a channel name that is not one of the three supported ones
        # When: it is written
        with self.assertRaises(ValueError) as ctx:
            aw.save_config({"notify": "carrier-pigeon"})
        # Then: the message names the offender and the valid choices
        message = str(ctx.exception)
        self.assertIn("carrier-pigeon", message)
        for channel in ("desktop", "telegram", "none"):
            self.assertIn(channel, message)
        # And: nothing was written
        self.assertFalse(self.config.exists())

    def test_every_supported_notify_channel_is_accepted(self) -> None:
        # Given/When/Then: each documented channel round-trips
        for channel in ("desktop", "telegram", "none"):
            aw.save_config({"notify": channel})
            self.assertEqual(aw.load_config()["notify"], channel)


class ConfigMaskingTests(_ConfigMixin):
    TOKEN = "1234567890:AAplaceholder-not-a-real-token-xyz"

    def test_the_telegram_token_is_masked_when_the_config_is_displayed(self) -> None:
        # Given: a configured bot token
        aw.save_config({"telegram_bot_token": self.TOKEN, "telegram_chat_id": "42"})
        # When: the config is fetched for display
        shown = aw.masked_config()
        # Then: the secret never appears, but the rest of the config does
        self.assertNotIn(self.TOKEN, repr(shown))
        self.assertNotEqual(shown["telegram_bot_token"], self.TOKEN)
        self.assertTrue(shown["telegram_bot_token"].startswith("*"))
        self.assertEqual(shown["telegram_chat_id"], "42")
        self.assertEqual(shown["switch_when_used_pct"], 90)

    def test_an_unset_token_is_displayed_as_empty_not_as_stars(self) -> None:
        # Given: no token configured
        # When: the config is fetched for display
        shown = aw.masked_config()
        # Then: an empty token stays visibly empty
        self.assertEqual(shown["telegram_bot_token"], "")

    def test_a_short_token_is_not_partially_revealed(self) -> None:
        # Given: a token too short to show a suffix safely
        aw.save_config({"telegram_bot_token": "abcd1234"})
        # When: the config is fetched for display
        shown = aw.masked_config()
        # Then: nothing of it leaks
        self.assertNotIn("abcd", shown["telegram_bot_token"])
        self.assertNotIn("1234", shown["telegram_bot_token"])


class _PlatformMixin:
    """Force a given platform by patching the module-level OS flags."""

    def force_platform(self, *, macos=False, windows=False, linux=False):
        for patch in (
            mock.patch.object(u, "IS_MACOS", macos),
            mock.patch.object(u, "IS_WINDOWS", windows),
            mock.patch.object(u, "IS_LINUX", linux),
            # Whether the real dev/CI box happens to have terminal-notifier
            # installed must not change what these tests exercise — tests
            # that want that path patch `have` again afterward, on top.
            mock.patch.object(u, "have", return_value=False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def record_subprocess(self) -> list:
        calls: list = []

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        patch = mock.patch.object(u.subprocess, "run", side_effect=fake_run)
        patch.start()
        self.addCleanup(patch.stop)
        return calls


class DesktopNotifyDispatchTests(_PlatformMixin, unittest.TestCase):
    def test_macos_notifies_through_osascript(self) -> None:
        # Given: a macOS host
        self.force_platform(macos=True)
        calls = self.record_subprocess()
        # When: a desktop notification is sent
        ok = u.desktop_notify("Switched", "Now on Test profile")
        # Then: exactly one osascript call carrying both strings
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "osascript")
        script = calls[0][-1]
        self.assertIn("display notification", script)
        self.assertIn("Now on Test profile", script)
        self.assertIn("Switched", script)

    def test_macos_notification_plays_a_sound_by_name_not_by_file(self) -> None:
        # Given: a macOS host
        self.force_platform(macos=True)
        calls = self.record_subprocess()
        # When: a desktop notification is sent
        u.desktop_notify("Switched", "Now on Test profile")
        # Then: the sound rides along in the SAME osascript call, named rather
        # than pathed — no second process, no dependency on a file location
        script = calls[0][-1]
        self.assertIn('sound name "Glass"', script)
        self.assertNotIn("afplay", script)
        self.assertNotIn("/System/Library", script)
        self.assertEqual(len(calls), 1)

    def test_macos_sound_can_be_turned_off(self) -> None:
        self.force_platform(macos=True)
        calls = self.record_subprocess()
        u.desktop_notify("Switched", "quiet", sound=False)
        self.assertNotIn("sound name", calls[0][-1])

    def _terminal_notifier_delivers(self, calls) -> None:
        """A `run()` double: records argv, `-list` always answers delivered."""

        def fake(cmd, **kwargs):
            calls.append(list(cmd))
            if "-list" in cmd:
                group = cmd[cmd.index("-list") + 1]
                stdout = f"GroupID\tTitle\n{group}\trow\n"
            else:
                stdout = ""
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout)

        patch = mock.patch.object(u, "run", side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_terminal_notifier_is_preferred_when_installed_and_delivered(self) -> None:
        # Given: a macOS host that has terminal-notifier, and it delivers
        self.force_platform(macos=True)
        mock.patch.object(u, "have", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        calls: list[list[str]] = []
        self._terminal_notifier_delivers(calls)
        # When: a desktop notification is sent
        ok = u.desktop_notify("Switched", "Now on Test profile")
        # Then: terminal-notifier carried it — no osascript fallback needed
        self.assertTrue(ok)
        send = calls[0]
        self.assertEqual(send[0], "terminal-notifier")
        self.assertIn("Switched", send)
        self.assertIn("Now on Test profile", send)
        self.assertIn("Glass", send)
        self.assertNotIn("osascript", [c[0] for c in calls])
        # And: no click action of any kind is wired up
        joined = " ".join(send)
        for flag in ("-open", "-execute", "-activate"):
            self.assertNotIn(flag, joined)

    def test_terminal_notifier_silences_on_request(self) -> None:
        self.force_platform(macos=True)
        mock.patch.object(u, "have", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        calls: list[list[str]] = []
        self._terminal_notifier_delivers(calls)
        u.desktop_notify("Switched", "quiet", sound=False)
        self.assertNotIn("-sound", calls[0])

    def test_not_delivered_falls_back_to_osascript_and_warns_once(self) -> None:
        # Given: terminal-notifier installed but its System Settings toggle
        # is off — the send exits 0, but `-list` never shows a delivered row
        self.force_platform(macos=True)
        mock.patch.object(u, "have", return_value=True).start()
        mock.patch.object(u.time, "sleep", return_value=None).start()  # no real wait
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mock.patch.object(u.Path, "home", return_value=Path(tmp.name)).start()
        self.addCleanup(mock.patch.stopall)
        calls: list[list[str]] = []

        def never_delivered(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="header only\n")

        mock.patch.object(u, "run", side_effect=never_delivered).start()
        # When: a notification is sent
        ok = u.desktop_notify("Switched", "Now on Test profile")
        # Then: the user still sees it — osascript picked up where
        # terminal-notifier's unshown notification left off
        self.assertTrue(ok)
        self.assertIn("osascript", [c[0] for c in calls])
        # And: the one-time hint marker was written
        marker = Path(tmp.name) / ".ai-accounts" / "notify-hint-shown"
        self.assertTrue(marker.exists())
        # And: a second dead notification does not crash or re-derive anything
        calls.clear()
        ok2 = u.desktop_notify("Switched", "again")
        self.assertTrue(ok2)

    def test_macos_plain_osascript_is_the_last_resort_fallback(self) -> None:
        # Given: a macOS host with no terminal-notifier (`have` is False by
        # default via force_platform — see its docstring)
        self.force_platform(macos=True)
        calls = self.record_subprocess()
        # When: a desktop notification is sent
        u.desktop_notify("Switched", "Now on Test profile")
        # Then: a single plain osascript call — no terminal-notifier attempt,
        # and no "System Events" wrapper (verified not to change clickability)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "osascript")
        self.assertNotIn("System Events", calls[0][-1])

    def test_linux_notifies_through_notify_send(self) -> None:
        # Given: a Linux host with notify-send installed
        self.force_platform(linux=True)
        have = mock.patch.object(u, "have", return_value=True)
        have.start()
        self.addCleanup(have.stop)
        calls = self.record_subprocess()
        # When: a desktop notification is sent
        ok = u.desktop_notify("Switched", "Now on Test profile")
        # Then: a notify-send call carrying both strings as argv, then the
        # sound as its own best-effort call (notify-send has no sound flag)
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "notify-send")
        self.assertIn("Switched", calls[0])
        self.assertIn("Now on Test profile", calls[0])
        self.assertEqual(calls[1][0], "canberra-gtk-play")
        self.assertNotIn("--action", " ".join(calls[0]))  # click does nothing

    def test_linux_without_notify_send_fails_quietly(self) -> None:
        # Given: a Linux host with no notifier installed
        self.force_platform(linux=True)
        have = mock.patch.object(u, "have", return_value=False)
        have.start()
        self.addCleanup(have.stop)
        calls = self.record_subprocess()
        # When: a desktop notification is sent
        ok = u.desktop_notify("Switched", "Now on Test profile")
        # Then: no subprocess is spawned and the caller learns it failed
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_windows_notifies_through_a_powershell_toast(self) -> None:
        # Given: a Windows host
        self.force_platform(windows=True)
        calls = self.record_subprocess()
        # When: a desktop notification is sent
        ok = u.desktop_notify("Switched", "Now on Test profile")
        # Then: exactly one powershell call whose script raises a toast
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "powershell")
        script = calls[0][-1]
        self.assertIn("Switched", script)
        self.assertIn("Now on Test profile", script)
        self.assertIn("Windows.UI.Notifications", script)

    def test_the_windows_toast_declares_its_sound_and_no_click_action(self) -> None:
        # Given: a Windows host
        self.force_platform(windows=True)
        calls = self.record_subprocess()
        # When: a desktop notification is sent
        u.desktop_notify("Switched", "Now on Test profile")
        script = calls[0][-1]
        # Then: the sound is explicit, not left to the template default...
        self.assertIn("ms-winsoundevent:Notification.Default", script)
        self.assertIn("'silent', 'false'", script)
        # ...and nothing makes the toast clickable: a `launch` attribute or an
        # <action> would send the click to an AppUserModelID we do not own
        self.assertNotIn("launch", script)
        self.assertNotIn("CreateElement('action')", script)

    def test_the_windows_toast_can_be_silenced(self) -> None:
        self.force_platform(windows=True)
        calls = self.record_subprocess()
        u.desktop_notify("Switched", "quiet", sound=False)
        self.assertIn("'silent', 'true'", calls[0][-1])

    def test_an_unknown_platform_reports_failure_without_spawning_anything(self) -> None:
        # Given: none of the three OS flags set (e.g. a BSD)
        self.force_platform()
        calls = self.record_subprocess()
        # When: a desktop notification is sent
        ok = u.desktop_notify("Switched", "Now on Test profile")
        # Then: nothing is spawned and the caller learns it failed
        self.assertFalse(ok)
        self.assertEqual(calls, [])


class DesktopNotifyEscapingTests(_PlatformMixin, unittest.TestCase):
    """Profile names reach this function — quotes must not break the script."""

    HOSTILE = 'Test" & do shell script "touch /tmp/pwned'

    def test_macos_escapes_quotes_in_user_supplied_text(self) -> None:
        # Given: a profile name carrying an AppleScript-breaking quote
        self.force_platform(macos=True)
        calls = self.record_subprocess()
        # When: it is used as the notification message
        u.desktop_notify("Switched", self.HOSTILE)
        # Then: the quote is escaped, so the payload stays one string literal
        script = calls[0][-1]
        self.assertNotIn('Test" &', script)
        self.assertIn('Test\\" &', script)

    def test_windows_escapes_single_quotes_in_user_supplied_text(self) -> None:
        # Given: a profile name carrying a PowerShell-breaking quote
        self.force_platform(windows=True)
        calls = self.record_subprocess()
        # When: it is used as the notification message
        u.desktop_notify("Switched", "Test'; rm -rf /")
        # Then: the quote is doubled, keeping it inside the literal
        script = calls[0][-1]
        self.assertIn("'Test''; rm -rf /'", script)


class NotifyChannelTests(_ConfigMixin, _PlatformMixin):
    """notify() dispatches on the configured channel and never raises."""

    def setUp(self):
        super().setUp()
        self.force_platform(macos=True)
        self.spawned = self.record_subprocess()
        self.requests: list = []

        def fake_urlopen(request, *a, **k):
            self.requests.append(request)
            raise AssertionError("no test should reach the network")

        patch = mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)
        patch.start()
        self.addCleanup(patch.stop)

    def test_channel_none_performs_no_subprocess_and_no_network_call(self) -> None:
        # Given: notifications switched off
        aw.save_config({"notify": "none"})
        # When: something wants to notify
        ok = aw.notify("Switched", "Now on Test profile")
        # Then: genuinely nothing happened
        self.assertFalse(ok)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.requests, [])

    def test_channel_desktop_goes_through_the_utils_funnel(self) -> None:
        # Given: the default desktop channel on a macOS host
        aw.save_config({"notify": "desktop"})
        # When: something wants to notify
        ok = aw.notify("Switched", "Now on Test profile")
        # Then: one osascript call, no network
        self.assertTrue(ok)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][0], "osascript")
        self.assertEqual(self.requests, [])

    def test_channel_telegram_posts_the_bot_api_payload(self) -> None:
        # Given: telegram credentials on file
        aw.save_config(
            {
                "notify": "telegram",
                "telegram_bot_token": "1234:placeholder-token",
                "telegram_chat_id": "99887766",
            }
        )
        sent: list = []

        class _Response:
            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, *a, **k):
            sent.append(request)
            return _Response()

        patch = mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)
        patch.start()
        self.addCleanup(patch.stop)
        # When: something wants to notify
        ok = aw.notify("Switched", "Now on Test profile")
        # Then: one POST to sendMessage carrying chat id and both strings
        self.assertTrue(ok)
        self.assertEqual(len(sent), 1)
        request = sent[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://api.telegram.org/bot1234:placeholder-token/sendMessage",
        )
        body = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], ["99887766"])
        self.assertIn("Switched", body["text"][0])
        self.assertIn("Now on Test profile", body["text"][0])
        # And: no desktop notification was raised
        self.assertEqual(self.spawned, [])

    def test_telegram_without_credentials_fails_without_calling_out(self) -> None:
        # Given: the telegram channel selected but no token/chat id
        aw.save_config({"notify": "telegram"})
        # When: something wants to notify
        err = io.StringIO()
        with redirect_stderr(err):
            ok = aw.notify("Switched", "Now on Test profile")
        # Then: a clean False, no network attempt, and a message naming the gap
        self.assertFalse(ok)
        self.assertEqual(self.requests, [])
        self.assertIn("telegram_bot_token", err.getvalue())
        self.assertIn("telegram_chat_id", err.getvalue())

    def test_a_dead_network_is_reported_not_raised(self) -> None:
        # Given: telegram configured but the API unreachable
        aw.save_config(
            {
                "notify": "telegram",
                "telegram_bot_token": "1234:placeholder-token",
                "telegram_chat_id": "99887766",
            }
        )
        patch = mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("offline")
        )
        patch.start()
        self.addCleanup(patch.stop)
        # When: something wants to notify
        ok = aw.notify("Switched", "Now on Test profile")
        # Then: the caller gets False instead of an exception
        self.assertFalse(ok)

    def test_a_failing_desktop_notifier_is_reported_not_raised(self) -> None:
        # Given: the desktop channel where osascript cannot even be spawned
        aw.save_config({"notify": "desktop"})
        patch = mock.patch.object(u.subprocess, "run", side_effect=OSError("boom"))
        patch.start()
        self.addCleanup(patch.stop)
        # When: something wants to notify
        ok = aw.notify("Switched", "Now on Test profile")
        # Then: the caller gets False instead of an exception
        self.assertFalse(ok)


class NotifyOnceTests(_ConfigMixin, _PlatformMixin):
    """"No account to switch to" must be said once, not on every poll."""

    KEY = "codex:no-candidate"

    def setUp(self):
        super().setUp()
        self.force_platform(macos=True)
        self.spawned = self.record_subprocess()
        aw.save_config({"notify": "desktop"})

    def test_the_same_exhausted_state_notifies_exactly_once(self) -> None:
        # Given: nothing to switch to, reported once
        aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        self.assertEqual(len(self.spawned), 1)
        # When: the very same state is hit again
        sent_again = aw.notify_once(
            self.KEY, "No account", "Every codex account is over quota"
        )
        # Then: the user is not told twice
        self.assertFalse(sent_again)
        self.assertEqual(len(self.spawned), 1)

    def test_a_changed_state_re_arms_the_notification(self) -> None:
        # Given: one state already reported
        aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # When: a different state occurs
        sent = aw.notify_once("claude:no-candidate", "No account", "Claude is out too")
        # Then: the user hears about the new one
        self.assertTrue(sent)
        self.assertEqual(len(self.spawned), 2)

    def test_the_notification_re_arms_after_the_cooldown_expires(self) -> None:
        # Given: a state reported an hour ago
        with mock.patch.object(aw.time, "time", return_value=1_000.0):
            aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # When: the same state is still true well past the cooldown
        with mock.patch.object(aw.time, "time", return_value=1_000.0 + 3_601):
            sent = aw.notify_once(
                self.KEY, "No account", "Every codex account is over quota", cooldown=3600
            )
        # Then: the reminder is allowed through again
        self.assertTrue(sent)
        self.assertEqual(len(self.spawned), 2)

    def test_the_state_file_is_owner_only(self) -> None:
        # Given/When: a de-duplicated notification is recorded
        aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # Then: its state file sits beside the config, owner-only
        state = self.home / "autoswitch-state.json"
        self.assertTrue(state.exists())
        if os.name == "posix":
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_de_duplication_survives_a_fresh_process(self) -> None:
        # Given: a state reported and the in-memory world thrown away
        aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        stored = json.loads((self.home / "autoswitch-state.json").read_text())
        self.assertIn(self.KEY, stored)
        # When: a later run hits the same state
        sent = aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # Then: still silent — the marker is on disk, not in memory
        self.assertFalse(sent)
        self.assertEqual(len(self.spawned), 1)

    def test_a_failed_send_does_not_consume_the_one_shot(self) -> None:
        # Given: the notifier is broken, so the user never saw the first message
        patch = mock.patch.object(u.subprocess, "run", side_effect=OSError("boom"))
        patch.start()
        sent = aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        self.assertFalse(sent)
        patch.stop()
        # When: the notifier works on the next poll
        retried = aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # Then: the message finally gets through instead of being suppressed
        self.assertTrue(retried)
        self.assertEqual(len(self.spawned), 1)

    def test_a_corrupt_state_file_does_not_crash_the_caller(self) -> None:
        # Given: a state file whose timestamp is garbage (half-written, hand-edited)
        (self.home / "autoswitch-state.json").write_text(
            '{"key": "codex:no-candidate", "at": "yesterday"}', encoding="utf-8"
        )
        # When: the same state is hit
        sent = aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # Then: it is treated as never reported instead of exploding
        self.assertTrue(sent)
        self.assertEqual(len(self.spawned), 1)

    def test_two_providers_exhausted_at_once_each_stay_suppressed(self) -> None:
        # Given: two providers report "nothing to switch to" in the same poll cycle
        # (ai-accounts drives codex/claude/agy/grok together, so this is normal)
        self.assertTrue(aw.notify_once("codex:no-candidate", "No account", "codex dry"))
        self.assertTrue(aw.notify_once("claude:no-candidate", "No account", "claude dry"))
        self.assertEqual(len(self.spawned), 2)
        # When: the next poll finds both still exhausted
        again_codex = aw.notify_once("codex:no-candidate", "No account", "codex dry")
        again_claude = aw.notify_once("claude:no-candidate", "No account", "claude dry")
        # Then: neither repeats — one provider's state must not evict another's
        self.assertFalse(again_codex)
        self.assertFalse(again_claude)
        self.assertEqual(len(self.spawned), 2)

    def test_an_unwritable_state_file_does_not_crash_the_caller(self) -> None:
        # Given: the state path cannot be written (here: a directory sits on it —
        # stands in for a read-only home, a full disk or bad permissions)
        (self.home / "autoswitch-state.json").mkdir()
        # When: an unattended poll loop reports an exhausted state
        sent = aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # Then: the user still saw it and the loop survives to poll again
        self.assertTrue(sent)
        self.assertEqual(len(self.spawned), 1)

    def test_a_backward_clock_jump_does_not_suppress_forever(self) -> None:
        # Given: a state recorded, then the clock jumps back (NTP, VM resume)
        with mock.patch.object(aw.time, "time", return_value=5_000.0):
            aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # When: the same state is hit at an earlier wall-clock time
        with mock.patch.object(aw.time, "time", return_value=1_000.0):
            sent = aw.notify_once(self.KEY, "No account", "Every codex account is over quota")
        # Then: a negative age counts as expired, not as "just notified"
        self.assertTrue(sent)
        self.assertEqual(len(self.spawned), 2)


class NotifyRobustnessTests(_ConfigMixin, _PlatformMixin):
    """The notify path is called from an unattended loop — it must never raise."""

    def setUp(self):
        super().setUp()
        self.force_platform(macos=True)

    def test_a_hung_notifier_cannot_block_the_caller_forever(self) -> None:
        # Given: the desktop channel
        aw.save_config({"notify": "desktop"})
        seen: list = []

        def fake_run(cmd, *a, **k):
            seen.append(k)
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        patch = mock.patch.object(u.subprocess, "run", side_effect=fake_run)
        patch.start()
        self.addCleanup(patch.stop)
        # When: a notification is sent
        aw.notify("Switched", "Now on Test profile")
        # Then: the spawn is bounded by a timeout, like the Telegram path
        self.assertIn("timeout", seen[0])
        self.assertGreater(seen[0]["timeout"], 0)

    def test_a_corrupt_telegram_token_does_not_crash_the_caller(self) -> None:
        # Given: a hand-corrupted token carrying control characters
        aw.save_config(
            {
                "notify": "telegram",
                "telegram_bot_token": "1234:placeholder\r\ntoken",
                "telegram_chat_id": "99887766",
            }
        )
        # When: something wants to notify (urllib rejects the URL outright)
        sent = aw.notify("Switched", "Now on Test profile")
        # Then: reported as a failure rather than raised
        self.assertFalse(sent)

    def test_a_hand_edited_invalid_channel_says_so_instead_of_going_silent(self) -> None:
        # Given: a config file hand-edited to an unsupported channel
        self.config.write_text('{"notify": "smoke-signal"}', encoding="utf-8")
        # When: something wants to notify
        err = io.StringIO()
        with redirect_stderr(err):
            sent = aw.notify("Switched", "Now on Test profile")
        # Then: it fails loudly rather than silently doing nothing forever
        self.assertFalse(sent)
        self.assertIn("smoke-signal", err.getvalue())


# ── engine ───────────────────────────────────────────────────────────────────


class _EngineMixin(_ConfigMixin, _PlatformMixin):
    """Auto-switch enabled, config in a temp dir, notifications captured."""

    def setUp(self):
        super().setUp()
        self.force_platform(macos=True)
        self.spawned = self.record_subprocess()
        aw.save_config({"enabled": True, "notify": "desktop", "language": "en"})
        self.probed: list[str] = []
        self.switched: list[str] = []

    def probe_from(self, used: dict[str, int | None]):
        """A real probe callable over a ``{profile: used_pct}`` table.

        ``None`` is a probe that cannot determine usage; a name missing from
        the table raises, standing in for an unreadable auth file.
        """

        def probe(name: str) -> UsageWindow | None:
            self.probed.append(name)
            if name not in used:
                raise RuntimeError(f"no usage for {name}")
            percent = used[name]
            if percent is None:
                return None
            return UsageWindow(percentage=percent, reset_time=None, window_minutes=None)

        return probe

    def record_switch(self, name: str) -> bool:
        self.switched.append(name)
        return True


class EngineTriggerTests(_EngineMixin):
    def test_used_below_the_threshold_does_not_switch(self) -> None:
        # Given: the active profile at 89% used and a nearly-idle alternative
        probe = self.probe_from({"work": 89, "spare": 10})
        # When: the engine runs at the default 90 threshold
        outcome = aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: 89 is below 90, so nothing is switched
        self.assertFalse(outcome.switched)
        self.assertIsNone(outcome.to_profile)
        self.assertEqual(self.switched, [])

    def test_used_equal_to_the_threshold_switches(self) -> None:
        # Given: the active profile sitting exactly on the 90% trigger point
        probe = self.probe_from({"work": 90, "spare": 10})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: the boundary is inclusive — it moves to the idle profile
        self.assertTrue(outcome.switched)
        self.assertEqual(outcome.to_profile, "spare")
        self.assertEqual(self.switched, ["spare"])


class EngineCandidateTests(_EngineMixin):
    def test_the_active_profile_is_never_its_own_switch_target(self) -> None:
        # Given: the active profile over quota, alongside one idle alternative
        probe = self.probe_from({"work": 95, "spare": 10})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: it moved away from the active profile, and never re-probed it as
        # a candidate — one usage call per profile per run, not two
        self.assertEqual(outcome.to_profile, "spare")
        self.assertEqual(self.switched, ["spare"])
        self.assertEqual(self.probed.count("work"), 1)

    def test_a_candidate_whose_probe_raises_is_skipped_not_fatal(self) -> None:
        # Given: one candidate whose usage lookup blows up (expired auth file)
        probe = self.probe_from({"work": 95, "spare": 10})
        # When: the engine runs with that candidate listed first
        outcome = aw.run_autoswitch(
            "codex", ["work", "broken", "spare"], "work", probe, self.record_switch
        )
        # Then: the broken one is skipped and the run still finds a target
        self.assertEqual(outcome.to_profile, "spare")
        self.assertEqual(self.switched, ["spare"])

    def test_a_candidate_with_no_usage_data_is_skipped(self) -> None:
        # Given: a candidate whose probe cannot determine usage (returns None)
        probe = self.probe_from({"work": 95, "unknown": None, "spare": 10})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "unknown", "spare"], "work", probe, self.record_switch
        )
        # Then: "no data" is never treated as "plenty of quota"
        self.assertEqual(outcome.to_profile, "spare")
        self.assertEqual(self.switched, ["spare"])

    def test_the_least_used_candidate_wins(self) -> None:
        # Given: two usable alternatives with very different headroom
        probe = self.probe_from({"work": 95, "half": 60, "fresh": 20})
        # When: the engine runs with the busier one listed first
        outcome = aw.run_autoswitch(
            "codex", ["work", "half", "fresh"], "work", probe, self.record_switch
        )
        # Then: it takes the emptiest account, not the first acceptable one
        self.assertEqual(outcome.to_profile, "fresh")
        self.assertEqual(self.switched, ["fresh"])

    def test_a_tie_is_broken_by_profile_name_not_by_list_order(self) -> None:
        # Given: two candidates with identical usage, listed z-before-a
        probe = self.probe_from({"work": 95, "zeta": 20, "alpha": 20})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "zeta", "alpha"], "work", probe, self.record_switch
        )
        # Then: the tie resolves to the name-sorted first, so repeated polls and
        # a reordered profile list always pick the same account
        self.assertEqual(outcome.to_profile, "alpha")
        self.assertEqual(self.switched, ["alpha"])

    def test_a_candidate_sitting_on_the_threshold_does_not_qualify(self) -> None:
        # Given: the only alternative is exactly at the trigger line itself
        probe = self.probe_from({"work": 95, "also_full": 90})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "also_full"], "work", probe, self.record_switch
        )
        # Then: no switch — moving there would trip the same trigger next poll
        self.assertFalse(outcome.switched)
        self.assertIsNone(outcome.to_profile)
        self.assertEqual(self.switched, [])


class EngineNoCandidateTests(_EngineMixin):
    def test_nothing_to_switch_to_is_reported_once_not_on_every_poll(self) -> None:
        # Given: the active profile out of quota and every alternative just as full
        probe = self.probe_from({"work": 99, "spare": 97})
        args = ("codex", ["work", "spare"], "work", probe, self.record_switch)
        # When: two consecutive polls hit the same dead end
        first = aw.run_autoswitch(*args)
        second = aw.run_autoswitch(*args)
        # Then: nothing was switched, and the user was told exactly once
        self.assertFalse(first.switched)
        self.assertFalse(second.switched)
        self.assertEqual(self.switched, [])
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("codex", self.spawned[0][-1])

    def test_the_dead_end_message_does_not_claim_a_quota_it_never_read(self) -> None:
        # Given: the active profile out of quota and the only alternative unreadable
        probe = self.probe_from({"work": 99, "spare": None})
        # When: the engine reports the dead end
        aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: it does not tell the user an unchecked account is over quota —
        # that would point them at "wait for the reset" instead of "re-login"
        message = self.spawned[0][-1]
        self.assertIn("unreadable", message)


class EngineSwitchNotificationTests(_EngineMixin):
    def test_a_successful_switch_notifies_naming_both_profiles(self) -> None:
        # Given: the active profile out of quota with a fresh alternative
        probe = self.probe_from({"work": 95, "spare": 10})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: one notification says which account was left and which is now live
        self.assertTrue(outcome.switched)
        self.assertEqual(len(self.spawned), 1)
        script = self.spawned[0][-1]
        self.assertIn("work", script)
        self.assertIn("spare", script)
        self.assertIn("codex", script)

    def test_the_notification_names_both_accounts_usage_not_just_the_old_one(
        self,
    ) -> None:
        # Given: the active profile out of quota and a candidate with headroom
        probe = self.probe_from({"work": 93, "spare": 15})
        # When: the engine switches
        aw.run_autoswitch("codex", ["work", "spare"], "work", probe, self.record_switch)
        # Then: the user can see how much room the NEW account has, which is
        # what tells them whether this switch bought an hour or a week
        message = self.spawned[0][-1]
        self.assertIn("93%", message)
        self.assertIn("15%", message)

    def test_the_notification_says_restart_needed_when_nothing_restarted(self) -> None:
        # Given: no restart hook — an unattended poll, or the manual-restart rung
        probe = self.probe_from({"work": 95, "spare": 10})
        # When: the engine switches
        aw.run_autoswitch("codex", ["work", "spare"], "work", probe, self.record_switch)
        # Then: the notification tells the user the switch is not live yet
        self.assertIn("Restart", self.spawned[0][-1])

    def test_the_notification_confirms_a_restart_that_actually_happened(self) -> None:
        # Given: a restart hook that succeeds
        probe = self.probe_from({"work": 95, "spare": 10})
        # When: the engine switches and restarts
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            probe,
            self.record_switch,
            restart=lambda: True,
        )
        # Then: it reports the new account as live, and does NOT ask for a
        # restart that already happened
        self.assertTrue(outcome.restarted)
        message = self.spawned[0][-1]
        self.assertIn("restarted", message.lower())
        self.assertNotIn("Restart your session", message)

    def test_a_restart_that_failed_still_asks_the_user_to_restart(self) -> None:
        # Given: a restart hook that reports failure — the switch already
        # happened, so the account IS changed but the session is stale
        probe = self.probe_from({"work": 95, "spare": 10})
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            probe,
            self.record_switch,
            restart=lambda: False,
        )
        # Then: the notification does not claim a restart it did not get
        self.assertFalse(outcome.restarted)
        self.assertIn("Restart", self.spawned[0][-1])

    def test_a_failing_switch_surfaces_the_error_and_claims_no_success(self) -> None:
        # Given: a switch that reports failure (unwritable auth file, locked keychain)
        probe = self.probe_from({"work": 95, "spare": 10})

        def failing_switch(name: str) -> bool:
            self.switched.append(name)
            return False

        # When: the engine runs
        err = io.StringIO()
        with redirect_stderr(err):
            outcome = aw.run_autoswitch(
                "codex", ["work", "spare"], "work", probe, failing_switch
            )
        # Then: it was attempted, reported as not switched, and never announced
        self.assertEqual(self.switched, ["spare"])
        self.assertFalse(outcome.switched)
        self.assertIsNone(outcome.to_profile)
        self.assertEqual(self.spawned, [])
        self.assertIn("spare", err.getvalue())

    def test_a_raising_switch_does_not_take_down_the_poll_loop(self) -> None:
        # Given: a switch callable that blows up rather than returning False
        probe = self.probe_from({"work": 95, "spare": 10})

        def exploding_switch(name: str) -> bool:
            self.switched.append(name)
            raise RuntimeError("keychain locked")

        # When: the engine runs (unattended — it may not raise at its caller)
        err = io.StringIO()
        with redirect_stderr(err):
            outcome = aw.run_autoswitch(
                "codex", ["work", "spare"], "work", probe, exploding_switch
            )
        # Then: the failure is reported, not raised, and nothing is announced
        self.assertFalse(outcome.switched)
        self.assertIsNone(outcome.to_profile)
        self.assertEqual(self.spawned, [])
        self.assertIn("keychain locked", err.getvalue())


class EngineDisabledTests(_EngineMixin):
    def test_disabled_probes_nothing_switches_nothing_and_says_nothing(self) -> None:
        # Given: auto-switching turned off, with the active profile out of quota
        aw.save_config({"enabled": False})
        probe = self.probe_from({"work": 99, "spare": 10})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: not one usage call, not one switch, not one notification
        self.assertFalse(outcome.switched)
        self.assertEqual(self.probed, [])
        self.assertEqual(self.switched, [])
        self.assertEqual(self.spawned, [])

    def test_a_hand_edited_string_false_is_off_not_truthy(self) -> None:
        # Given: a hand-edited config where `enabled` is the STRING "false" —
        # valid JSON, and truthy to bool(), so a bare `if not cfg["enabled"]`
        # would run the feature while the user believes it is off. The CLI's
        # own `config set` cannot produce this; a text editor can.
        self.config.write_text(
            json.dumps({"enabled": "false", "notify": "desktop"}), encoding="utf-8"
        )
        probe = self.probe_from({"work": 99, "spare": 10})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: the master switch fails CLOSED — nothing probed, switched or said
        self.assertEqual(outcome.reason, "disabled")
        self.assertEqual(self.probed, [])
        self.assertEqual(self.switched, [])
        self.assertEqual(self.spawned, [])

    def test_only_a_real_json_true_enables_the_feature(self) -> None:
        # Given: every non-`true` shape a hand-edited config can hold
        for value in ("true", "1", 1, "yes", "on", [1], {"a": 1}):
            with self.subTest(enabled=value):
                self.config.write_text(
                    json.dumps({"enabled": value, "notify": "desktop"}),
                    encoding="utf-8",
                )
                # When: the engine runs against an exhausted active profile
                outcome = aw.run_autoswitch(
                    "codex",
                    ["work", "spare"],
                    "work",
                    self.probe_from({"work": 99, "spare": 10}),
                    self.record_switch,
                )
                # Then: only a JSON `true` counts — everything else is off
                self.assertEqual(outcome.reason, "disabled")
        self.assertEqual(self.switched, [])


class EngineUnknownActiveTests(_EngineMixin):
    def test_unreadable_active_usage_is_not_a_trigger(self) -> None:
        # Given: the active profile's usage cannot be determined (probe → None)
        probe = self.probe_from({"work": None, "spare": 10})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: unknown is not "out of quota" — nobody is switched or notified
        self.assertFalse(outcome.switched)
        self.assertEqual(self.switched, [])
        self.assertEqual(self.spawned, [])

    def test_a_raising_active_probe_does_not_crash_the_run(self) -> None:
        # Given: the active profile's auth file is unreadable, so the probe raises
        probe = self.probe_from({"spare": 10})
        # When: the engine runs (unattended — it may not raise at its caller)
        outcome = aw.run_autoswitch(
            "codex", ["work", "spare"], "work", probe, self.record_switch
        )
        # Then: the poll is a no-op instead of an exception
        self.assertFalse(outcome.switched)
        self.assertEqual(self.switched, [])
        self.assertEqual(self.spawned, [])

    def test_with_no_active_profile_there_is_nothing_to_switch_away_from(self) -> None:
        # Given: saved profiles but none of them currently active
        probe = self.probe_from({"spare": 10})
        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex", ["spare"], None, probe, self.record_switch
        )
        # Then: it is a no-op, without probing anything
        self.assertFalse(outcome.switched)
        self.assertEqual(self.probed, [])
        self.assertEqual(self.switched, [])
        self.assertEqual(self.spawned, [])


class EngineFailedSwitchTests(_EngineMixin):
    """A candidate that refuses to activate must not end the run.

    Blind-mode agy ranks candidates by name when no quota is readable, so an
    unactivatable profile (a foreign credential blob, a revoked token) can sit
    at the front of the queue and be re-picked on every 30-minute tick —
    stranding the user on an exhausted account forever.
    """

    def _run(self, refuse: set[str]):
        def switch(name: str) -> bool:
            self.switched.append(name)
            return name not in refuse

        with redirect_stderr(io.StringIO()):
            return aw.run_autoswitch(
                "agy",
                ["backup", "spare", "work"],
                "work",
                self.probe_from({"work": 97, "backup": 0, "spare": 0}),
                switch,
            )

    def test_a_refused_candidate_hands_over_to_the_next_one(self) -> None:
        # Given: the first candidate in order cannot be activated
        # When: the engine runs
        outcome = self._run({"backup"})
        # Then: it moves on rather than giving up, and reports the one that took
        self.assertEqual(outcome.reason, "switched")
        self.assertEqual(outcome.to_profile, "spare")
        self.assertEqual(self.switched, ["backup", "spare"])

    def test_only_an_all_refused_queue_is_a_failure(self) -> None:
        # Given: every candidate refuses
        # When: the engine runs
        outcome = self._run({"backup", "spare"})
        # Then: switch_failed — after each one was actually tried
        self.assertEqual(outcome.reason, "switch_failed")
        self.assertIsNone(outcome.to_profile)
        self.assertEqual(self.switched, ["backup", "spare"])

    def test_a_raising_switch_is_not_the_end_of_the_run_either(self) -> None:
        # Given: activating the first candidate raises instead of returning False
        def switch(name: str) -> bool:
            self.switched.append(name)
            if name == "backup":
                raise OSError("keychain locked")
            return True

        with redirect_stderr(io.StringIO()):
            outcome = aw.run_autoswitch(
                "agy",
                ["backup", "spare", "work"],
                "work",
                self.probe_from({"work": 97, "backup": 0, "spare": 0}),
                switch,
            )
        # Then: the exception is contained and the next candidate still runs
        self.assertEqual(outcome.reason, "switched")
        self.assertEqual(outcome.to_profile, "spare")
        self.assertEqual(self.switched, ["backup", "spare"])


class EngineOutcomeTests(_EngineMixin):
    def _run(self, used, *, profiles=("work", "spare"), active="work", switch=None):
        return aw.run_autoswitch(
            "codex",
            list(profiles),
            active,
            self.probe_from(used),
            switch or self.record_switch,
        )

    def test_each_run_reports_why_it_ended_that_way(self) -> None:
        def refuse(name: str) -> bool:
            return False

        # Given/When/Then: every terminal state carries its own machine-readable
        # reason, so a caller can report it without re-deriving the decision
        with self.subTest("below the threshold"):
            self.assertEqual(
                self._run({"work": 89, "spare": 10}).reason, "below_threshold"
            )
        with self.subTest("switched"):
            self.assertEqual(self._run({"work": 95, "spare": 10}).reason, "switched")
        with self.subTest("nothing to switch to"):
            self.assertEqual(self._run({"work": 95, "spare": 99}).reason, "no_candidate")
        with self.subTest("the switch itself failed"):
            with redirect_stderr(io.StringIO()):
                outcome = self._run({"work": 95, "spare": 10}, switch=refuse)
            self.assertEqual(outcome.reason, "switch_failed")
        with self.subTest("active usage unknown"):
            self.assertEqual(self._run({"work": None, "spare": 10}).reason, "unknown")
        with self.subTest("no active profile"):
            self.assertEqual(self._run({"spare": 10}, active=None).reason, "no_active")
        with self.subTest("auto-switching disabled"):
            aw.save_config({"enabled": False})
            self.assertEqual(self._run({"work": 95, "spare": 10}).reason, "disabled")
        # And: that is the whole vocabulary a caller has to handle
        self.assertEqual(
            set(aw.SWITCH_REASONS),
            {
                "disabled",
                "no_active",
                "unknown",
                "below_threshold",
                "no_candidate",
                "switched",
                "switch_failed",
            },
        )

    def test_a_switch_outcome_carries_the_whole_story_of_the_run(self) -> None:
        # Given: the active profile over quota with one fresh alternative
        # When: the engine runs
        outcome = self._run({"work": 95, "spare": 10})
        # Then: a caller can log the run without re-deriving any of it
        self.assertEqual(outcome.provider, "codex")
        self.assertEqual(outcome.from_profile, "work")
        self.assertEqual(outcome.to_profile, "spare")
        self.assertEqual(outcome.used_pct, 95)
        self.assertIsNone(outcome.error)

    def test_a_failed_switch_carries_the_error_text_it_printed(self) -> None:
        # Given: a switch callable that refuses
        def refuse(name: str) -> bool:
            return False

        # When: the engine runs
        err = io.StringIO()
        with redirect_stderr(err):
            outcome = self._run({"work": 95, "spare": 10}, switch=refuse)
        # Then: the outcome names the attempted profile, and nothing is "now active"
        self.assertIsNotNone(outcome.error)
        self.assertIn("spare", outcome.error or "")
        self.assertIn(outcome.error or "", err.getvalue())
        self.assertEqual(outcome.from_profile, "work")
        self.assertIsNone(outcome.to_profile)


class EngineRestartSlotTests(_EngineMixin):
    """The engine only threads the slot — the ladder behind it is the caller's."""

    def test_a_successful_switch_invokes_the_restart_slot(self) -> None:
        # Given: a restart hook supplied by the caller
        calls: list[str] = []

        def restart() -> bool:
            calls.append("restart")
            return True

        # When: a run switches profiles
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            self.probe_from({"work": 95, "spare": 10}),
            self.record_switch,
            restart,
        )
        # Then: the hook ran once, and its result is reported back
        self.assertTrue(outcome.switched)
        self.assertEqual(calls, ["restart"])
        self.assertIs(outcome.restarted, True)

    def test_a_run_that_did_not_switch_never_restarts(self) -> None:
        # Given: a restart hook and an active profile still inside its quota
        calls: list[str] = []

        def restart() -> bool:
            calls.append("restart")
            return True

        # When: the engine runs
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            self.probe_from({"work": 10, "spare": 10}),
            self.record_switch,
            restart,
        )
        # Then: nothing switched, so nothing is restarted under the user
        self.assertFalse(outcome.switched)
        self.assertEqual(calls, [])
        self.assertIsNone(outcome.restarted)

    def test_a_raising_restart_hook_does_not_undo_the_switch(self) -> None:
        # Given: a restart hook that blows up
        def restart() -> bool:
            raise RuntimeError("no tty to restart into")

        # When: a run switches profiles
        err = io.StringIO()
        with redirect_stderr(err):
            outcome = aw.run_autoswitch(
                "codex",
                ["work", "spare"],
                "work",
                self.probe_from({"work": 95, "spare": 10}),
                self.record_switch,
                restart,
            )
        # Then: the switch still stands and the restart is reported as failed
        self.assertTrue(outcome.switched)
        self.assertEqual(outcome.to_profile, "spare")
        self.assertIs(outcome.restarted, False)
        self.assertIn("no tty to restart into", err.getvalue())


class EngineThresholdConfigTests(_EngineMixin):
    def test_a_hand_edited_non_numeric_threshold_falls_back_to_the_default(self) -> None:
        # Given: a config file whose threshold was hand-edited to junk
        self.config.write_text(
            '{"enabled": true, "notify": "desktop", "switch_when_used_pct": "ninety"}',
            encoding="utf-8",
        )
        # When: the engine runs with the active profile over the default 90
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            self.probe_from({"work": 95, "spare": 10}),
            self.record_switch,
        )
        # Then: the documented default still protects the user, no crash
        self.assertTrue(outcome.switched)
        self.assertEqual(outcome.to_profile, "spare")

    def test_a_custom_threshold_from_the_config_is_honored(self) -> None:
        # Given: a user who wants to move at half a quota, not at 90%
        aw.save_config({"switch_when_used_pct": 50})
        # When: the active profile crosses that lower line
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            self.probe_from({"work": 60, "spare": 40}),
            self.record_switch,
        )
        # Then: their setting drives both the trigger and who qualifies
        self.assertTrue(outcome.switched)
        self.assertEqual(outcome.to_profile, "spare")


class PickWindowTests(_ConfigMixin):
    """Which quota window the trigger reads — the ``switch_window`` setting.

    A provider snapshot carries both a short window and a weekly one; only one
    of them decides whether to switch. The choice is config, not per-provider
    code, so codex and claude cannot drift apart.
    """

    HOURLY = UsageWindow(percentage=10, reset_time=None, window_minutes=60)
    WEEKLY = UsageWindow(percentage=93, reset_time=None, window_minutes=7 * 24 * 60)

    def test_the_weekly_window_is_the_default(self) -> None:
        # Given: no config file at all
        self.assertFalse(self.config.exists())
        # When/Then: the weekly window drives the decision
        self.assertIs(aw.pick_window(self.HOURLY, self.WEEKLY), self.WEEKLY)

    def test_the_short_window_is_used_when_configured(self) -> None:
        # Given: a user who opted back into the 5-hour window
        aw.save_config({"switch_window": "5h"})
        # When/Then: that is the window read
        self.assertIs(aw.pick_window(self.HOURLY, self.WEEKLY), self.HOURLY)

    def test_a_hand_edited_junk_window_falls_back_to_the_weekly_default(self) -> None:
        # Given: the key hand-edited to something the schema never allowed
        self.config.write_text('{"switch_window": "nonsense"}', encoding="utf-8")
        # When/Then: the documented default still protects the user, no crash
        self.assertIs(aw.pick_window(self.HOURLY, self.WEEKLY), self.WEEKLY)

    def test_a_missing_selected_window_is_never_substituted(self) -> None:
        # Given: the provider reports no weekly figure but a live 5-hour one
        # Then: None — the engine reports "unknown" rather than silently
        # deciding on a window the user did not choose.
        self.assertIsNone(aw.pick_window(self.HOURLY, None))

    def test_a_caller_supplied_config_is_reused_not_re_read(self) -> None:
        # Given: a config the caller already loaded (one read per autoswitch run)
        aw.save_config({"switch_window": "1week"})
        # When: it says 5h, that wins over what is on disk
        self.assertIs(
            aw.pick_window(self.HOURLY, self.WEEKLY, {"switch_window": "5h"}),
            self.HOURLY,
        )


# ── restart ladder ───────────────────────────────────────────────────────────
# See docs/autoswitch-hot-reload-spike.md — no provider hot-reloads
# credentials, so every provider ships auto-restart (never seamless) subject
# to two safety downgrades: an unattended caller and, for agy, a running
# Antigravity IDE. Real subprocess calls are never made here — build_restart()
# only decides *whether* to invoke the caller's own resume callable.


class RestartLadderVerdictTableTests(unittest.TestCase):
    def test_each_real_provider_ships_the_verdict_the_spike_doc_recommends(self) -> None:
        # Given/When: the plain-data verdict table
        # Then: it matches docs/autoswitch-hot-reload-spike.md's Summary table —
        # all four ship auto-restart, none reached seamless
        self.assertEqual(aw.PROVIDER_VERDICTS["codex"], "auto-restart")
        self.assertEqual(aw.PROVIDER_VERDICTS["claude"], "auto-restart")
        self.assertEqual(aw.PROVIDER_VERDICTS["agy"], "auto-restart")
        self.assertEqual(aw.PROVIDER_VERDICTS["grok"], "auto-restart")

    def test_groks_verdict_is_documented_as_unreachable_for_want_of_a_quota_api(
        self,
    ) -> None:
        # Given: grok's row is aspirational — `grok-accounts autoswitch` reports
        # "no quota API" and exits before the engine, so this rung can never
        # fire. The doc the table is sourced from must say so, or the two drift
        # apart and the table reads as shipped behavior.
        doc = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "autoswitch-hot-reload-spike.md"
        ).read_text(encoding="utf-8")
        # When: the summary row and the grok section are read
        summary_row = next(
            line for line in doc.splitlines() if line.startswith("| grok |")
        )
        grok_section = doc.split("\n## grok\n", 1)[1].split("\n## ", 1)[0]
        # Then: both name the blocker rather than promising a live behavior
        self.assertIn("unreachable", summary_row)
        self.assertIn("no quota API", grok_section)

    def test_the_rung_vocabulary_is_exactly_the_three_named_in_the_requirement(self) -> None:
        # Given/When/Then: seamless -> auto-restart -> manual-restart, no more
        self.assertEqual(aw.RUNGS, ("seamless", "auto-restart", "manual-restart"))


class RestartLadderEffectiveRungDefaultTests(unittest.TestCase):
    def test_a_provider_with_no_verdict_entry_defaults_to_manual_restart(self) -> None:
        # Given: a provider name absent from the verdict table entirely
        # When: the effective rung is resolved
        rung = aw.effective_rung("mystery-provider", interactive=True)
        # Then: the safest rung is assumed, not a crash or a guess at auto-restart
        self.assertEqual(rung, "manual-restart")

    def test_an_inconclusive_verdict_value_defaults_to_manual_restart(self) -> None:
        # Given: a hand-edited/foreign verdict value outside the rung vocabulary
        verdicts = {"codex": "not-a-real-rung"}
        # When: the effective rung is resolved
        rung = aw.effective_rung("codex", interactive=True, verdicts=verdicts)
        # Then: junk data degrades to the safe manual rung, not a KeyError or crash
        self.assertEqual(rung, "manual-restart")


class RestartLadderAgyIdeTests(unittest.TestCase):
    def test_agy_with_the_antigravity_ide_running_downgrades_to_manual_restart(self) -> None:
        # Given: agy's table entry is auto-restart, but the IDE is running —
        # it re-writes the keyring instantly and has no programmatic resume
        # (docs/autoswitch-hot-reload-spike.md, agy section, footnote ²)
        # When: the effective rung is resolved
        rung = aw.effective_rung("agy", interactive=True, agy_ide_running=True)
        # Then: the CLI-only auto-restart is not safe to assume — downgrade
        self.assertEqual(rung, "manual-restart")

    def test_agy_without_the_ide_running_stays_auto_restart(self) -> None:
        # Given: the CLI-only case the spike doc recommends auto-restart for
        # When: the effective rung is resolved
        rung = aw.effective_rung("agy", interactive=True, agy_ide_running=False)
        # Then: the table's baseline verdict applies unchanged
        self.assertEqual(rung, "auto-restart")


class RestartLadderUnattendedRungTests(unittest.TestCase):
    def test_auto_restart_verdict_but_unattended_downgrades_to_manual_restart(self) -> None:
        # Given: codex ships auto-restart, but this poll is unattended (a
        # background timer, no TTY — SAFETY: never auto-restart there)
        # When: the effective rung is resolved
        rung = aw.effective_rung("codex", interactive=False)
        # Then: it degrades to manual-restart rather than assuming it is safe
        self.assertEqual(rung, "manual-restart")

    def test_auto_restart_verdict_interactive_stays_auto_restart(self) -> None:
        # Given: the same provider, but this is an interactive run
        # When: the effective rung is resolved
        rung = aw.effective_rung("codex", interactive=True)
        # Then: the table's baseline verdict applies unchanged
        self.assertEqual(rung, "auto-restart")


class RestartLadderManualMessageTests(unittest.TestCase):
    def test_the_manual_restart_message_explicitly_instructs_the_user_to_restart(self) -> None:
        # Given: a switch that landed on the manual-restart rung
        # When: the message is rendered
        message = aw.manual_restart_message("codex", "work", "spare")
        # Then: it names both profiles and explicitly says to restart
        self.assertIn("restart", message.lower())
        self.assertIn("work", message)
        self.assertIn("spare", message)
        self.assertIn("codex", message)


class RestartLadderBuildRestartTests(_EngineMixin):
    """build_restart() decides *whether* to call the caller's own resume —
    it never spawns a process itself, so these never touch subprocess beyond
    the switch's own desktop notification (captured by self.spawned)."""

    def resume_recorder(self):
        calls: list[str] = []

        def resume() -> bool:
            calls.append("resume")
            return True

        return resume, calls

    def test_seamless_verdict_never_invokes_the_restart_callable(self) -> None:
        # Given: a provider table-entered as seamless (not real today, but the
        # rung must exist and behave correctly the day one qualifies)
        resume, calls = self.resume_recorder()
        verdicts = {"codex": "seamless"}
        restart = aw.build_restart("codex", resume, interactive=True, verdicts=verdicts)
        # Then: nothing is wired to invoke — the engine's own switch
        # notification is the whole story for this rung
        self.assertIsNone(restart)
        # When: a switch actually happens through the engine with that slot
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            self.probe_from({"work": 95, "spare": 10}),
            self.record_switch,
            restart,
        )
        # Then: the switch stands, but the resume callable was never touched
        self.assertTrue(outcome.switched)
        self.assertEqual(calls, [])
        self.assertIsNone(outcome.restarted)

    def test_auto_restart_verdict_interactive_invokes_the_restart_callable_once(self) -> None:
        # Given: codex's real auto-restart verdict, run interactively
        resume, calls = self.resume_recorder()
        restart = aw.build_restart("codex", resume, interactive=True)
        # When: a switch happens through the engine
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            self.probe_from({"work": 95, "spare": 10}),
            self.record_switch,
            restart,
        )
        # Then: the resume callable ran exactly once, and its result is reported
        self.assertEqual(calls, ["resume"])
        self.assertIs(outcome.restarted, True)

    def test_build_restart_defaults_to_non_interactive_so_a_forgotten_flag_never_spawns(
        self,
    ) -> None:
        # Given: a caller that forgets to pass interactive at all — the safe
        # default must be "do nothing", never "restart unattended"
        resume, calls = self.resume_recorder()
        # When: the factory is built with no interactive kwarg
        restart = aw.build_restart("codex", resume)
        # Then: nothing is wired to invoke
        self.assertIsNone(restart)
        self.assertEqual(calls, [])

    def test_auto_restart_verdict_but_unattended_spawns_nothing_and_degrades_to_manual_message(
        self,
    ) -> None:
        # Given: codex's auto-restart verdict, but this poll is unattended
        resume, calls = self.resume_recorder()
        restart = aw.build_restart("codex", resume, interactive=False)
        # Then: build_restart itself already refuses to wire the callable
        self.assertIsNone(restart)
        # When: the switch happens anyway through the engine
        outcome = aw.run_autoswitch(
            "codex",
            ["work", "spare"],
            "work",
            self.probe_from({"work": 95, "spare": 10}),
            self.record_switch,
            restart,
        )
        # Then: nothing was spawned, and the caller is left to fall back to
        # the manual-restart message using the outcome it already has
        self.assertEqual(calls, [])
        self.assertIsNone(outcome.restarted)
        self.assertEqual(aw.effective_rung("codex", interactive=False), "manual-restart")
        message = aw.manual_restart_message(
            "codex", outcome.from_profile, outcome.to_profile
        )
        self.assertIn("restart", message.lower())

    def test_a_raising_resume_hook_does_not_undo_the_successful_switch(self) -> None:
        # Given: a resume callable that blows up (e.g. no tty to spawn into)
        def resume() -> bool:
            raise RuntimeError("no tty to restart into")

        restart = aw.build_restart("codex", resume, interactive=True)
        # When: a switch happens through the engine
        err = io.StringIO()
        with redirect_stderr(err):
            outcome = aw.run_autoswitch(
                "codex",
                ["work", "spare"],
                "work",
                self.probe_from({"work": 95, "spare": 10}),
                self.record_switch,
                restart,
            )
        # Then: the switch still stands — a broken restart never falsifies it
        self.assertTrue(outcome.switched)
        self.assertEqual(outcome.to_profile, "spare")
        self.assertIs(outcome.restarted, False)
        self.assertIn("no tty to restart into", err.getvalue())

    def test_agy_with_the_ide_running_build_restart_returns_none(self) -> None:
        # Given: the Antigravity IDE is running
        resume, calls = self.resume_recorder()
        # When: the factory is built for agy
        restart = aw.build_restart("agy", resume, interactive=True, agy_ide_running=True)
        # Then: nothing is wired — manual-restart is the only honest rung
        self.assertIsNone(restart)
        self.assertEqual(calls, [])

    def test_agy_without_the_ide_running_build_restart_returns_the_resume_callable(
        self,
    ) -> None:
        # Given: the agy CLI only, no IDE running
        resume, calls = self.resume_recorder()
        # When: the factory is built for agy
        restart = aw.build_restart("agy", resume, interactive=True, agy_ide_running=False)
        # Then: the caller's own resume callable is wired through unchanged
        self.assertIs(restart, resume)
