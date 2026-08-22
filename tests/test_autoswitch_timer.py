"""Tests for the OS timer trigger of the auto-switch feature.

Every subprocess invocation is mocked and every filesystem write is redirected
into a temp ``$HOME`` — this suite must NEVER touch the real
``~/Library/LaunchAgents``, ``~/.config/systemd/user``, the real crontab, or
issue a real ``schtasks`` call. Run with: ``uv run pytest``.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_accounts import _utils as u
from ai_accounts import autoswitch as aw
from ai_accounts import autoswitch_hooks
from ai_accounts import autoswitch_timer as at


class _PlatformMixin:
    """Force a given platform by patching the module-level OS flags."""

    def force_platform(self, *, macos=False, linux=False, windows=False):
        for name, value in (
            ("IS_MACOS", macos),
            ("IS_LINUX", linux),
            ("IS_WINDOWS", windows),
        ):
            p = mock.patch.object(u, name, value)
            p.start()
            self.addCleanup(p.stop)


class _HomeMixin:
    """Redirect HOME-relative writes (plist/systemd unit dirs) into a temp dir."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        # Path.home() reads HOME on POSIX and USERPROFILE on Windows — set both,
        # or the writes escape into the real profile and leak across tests.
        env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home)},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)


class _ConfigMixin:
    """Point the autoswitch config store at a throwaway temp file."""

    def setUp(self):
        super().setUp()
        self.config_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_tmp.cleanup)
        self.config_path = Path(self.config_tmp.name) / "config.json"
        env = mock.patch.dict(
            os.environ, {"AI_ACCOUNTS_CONFIG_JSON": str(self.config_path)}, clear=False
        )
        env.start()
        self.addCleanup(env.stop)


class _SubprocessMixin:
    """Capture every ``u.run`` call instead of touching a real scheduler."""

    def capture_subprocess(self, responses=None):
        """Patch ``u.run``; returns the list of captured ``cmd`` lists.

        *responses* maps a command's first two tokens (joined by a space) to
        a ``CompletedProcess`` to return instead of the zero-exit default —
        e.g. ``{"crontab -l": subprocess.CompletedProcess([], 0, stdout="")}``.
        """
        calls = []
        responses = responses or {}

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": list(cmd), "kwargs": kwargs})
            key = " ".join(cmd[:2])
            if key in responses:
                return responses[key]
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="")

        p = mock.patch.object(u, "run", side_effect=fake_run)
        p.start()
        self.addCleanup(p.stop)
        return calls


class MacOSInstallTests(_PlatformMixin, _HomeMixin, _SubprocessMixin, unittest.TestCase):
    def test_install_writes_launchd_plist_with_label_and_interval(self) -> None:
        # Given: a forced macOS platform, a temp HOME, and a mocked launchctl
        self.force_platform(macos=True)
        calls = self.capture_subprocess()

        # When: install() is called
        at.install()

        # Then: a launchd plist landed under the temp ~/Library/LaunchAgents
        plist_path = self.home / "Library" / "LaunchAgents" / f"{at.LABEL}.plist"
        self.assertTrue(plist_path.exists())

        # And: it contains the expected label and default interval
        import plistlib

        plist = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(plist["Label"], at.LABEL)
        self.assertEqual(plist["StartInterval"], at.DEFAULT_INTERVAL_SEC)
        self.assertIs(plist["RunAtLoad"], True)

        # And: launchctl was invoked to load it
        launchctl_calls = [c for c in calls if c["cmd"][0] == "launchctl"]
        self.assertEqual(len(launchctl_calls), 1)
        self.assertIn(str(plist_path), launchctl_calls[0]["cmd"])


class LinuxInstallTests(_PlatformMixin, _HomeMixin, _SubprocessMixin, unittest.TestCase):
    def test_install_with_systemctl_writes_systemd_user_timer(self) -> None:
        # Given: a forced Linux platform where `systemctl` is on PATH
        self.force_platform(linux=True)
        calls = self.capture_subprocess()
        have = mock.patch.object(u, "have", return_value=True)
        have.start()
        self.addCleanup(have.stop)

        # When: install() is called
        at.install()

        # Then: a systemd user timer + service unit landed under temp HOME
        unit_dir = self.home / ".config" / "systemd" / "user"
        timer_path = unit_dir / f"{at.LABEL}.timer"
        service_path = unit_dir / f"{at.LABEL}.service"
        self.assertTrue(timer_path.exists())
        self.assertTrue(service_path.exists())

        # And: the timer unit carries the default interval
        timer_text = timer_path.read_text(encoding="utf-8")
        self.assertIn(f"OnUnitActiveSec={at.DEFAULT_INTERVAL_SEC}", timer_text)

        # And: the service unit runs this module as the scheduled command
        service_text = service_path.read_text(encoding="utf-8")
        self.assertIn("ai_accounts.autoswitch_timer run", service_text)

        # And: systemd re-read the units, THEN enabled the timer — no crontab
        # touched. daemon-reload must precede enable, otherwise a re-install
        # with a new interval is ignored (systemd caches unit contents).
        systemctl_verbs = [
            c["cmd"][2] for c in calls if c["cmd"][0] == "systemctl"
        ]
        self.assertEqual(systemctl_verbs, ["daemon-reload", "enable"])
        self.assertFalse(any(c["cmd"][0] == "crontab" for c in calls))

    def test_install_without_systemctl_falls_back_to_cron(self) -> None:
        # Given: a forced Linux platform where `systemctl` is absent
        self.force_platform(linux=True)
        responses = {
            "crontab -l": subprocess.CompletedProcess(
                args=["crontab", "-l"], returncode=0, stdout=""
            )
        }
        calls = self.capture_subprocess(responses)
        have = mock.patch.object(u, "have", return_value=False)
        have.start()
        self.addCleanup(have.stop)

        # When: install() is called
        at.install()

        # Then: no systemd unit files were written
        unit_dir = self.home / ".config" / "systemd" / "user"
        self.assertFalse((unit_dir / f"{at.LABEL}.timer").exists())

        # And: a crontab entry running this module was installed instead
        set_calls = [c for c in calls if c["cmd"] == ["crontab", "-"]]
        self.assertEqual(len(set_calls), 1)
        new_crontab = set_calls[0]["kwargs"]["input"]
        self.assertIn("ai_accounts.autoswitch_timer run", new_crontab)
        self.assertIn(at.CRON_TAG, new_crontab)


class WindowsInstallTests(_PlatformMixin, _HomeMixin, _SubprocessMixin, unittest.TestCase):
    def test_install_composes_schtasks_create_command(self) -> None:
        # Given: a forced Windows platform and a mocked schtasks
        self.force_platform(windows=True)
        calls = self.capture_subprocess()

        # When: install() is called
        at.install()

        # Then: exactly one schtasks /Create call was issued
        create_calls = [c["cmd"] for c in calls if c["cmd"][:2] == ["schtasks", "/Create"]]
        self.assertEqual(len(create_calls), 1)
        cmd = create_calls[0]

        # And: it names the task, runs this module, and sets the interval
        self.assertIn(at.LABEL, cmd)
        self.assertTrue(any("ai_accounts.autoswitch_timer run" in part for part in cmd))
        self.assertIn("MINUTE", cmd)
        self.assertIn(str(at.DEFAULT_INTERVAL_SEC // 60), cmd)


class UninstallTests(_PlatformMixin, _HomeMixin, _SubprocessMixin, unittest.TestCase):
    def test_macos_uninstall_unloads_and_removes_the_plist(self) -> None:
        # Given: an installed macOS launchd plist
        self.force_platform(macos=True)
        self.capture_subprocess()
        at.install()
        plist_path = self.home / "Library" / "LaunchAgents" / f"{at.LABEL}.plist"
        self.assertTrue(plist_path.exists())
        calls = self.capture_subprocess()

        # When: uninstall() is called
        at.uninstall()

        # Then: launchctl was told to unload it, and the plist is gone
        unload_calls = [c["cmd"] for c in calls if c["cmd"][:2] == ["launchctl", "unload"]]
        self.assertEqual(len(unload_calls), 1)
        self.assertFalse(plist_path.exists())

    def test_linux_uninstall_with_systemd_disables_and_removes_units(self) -> None:
        # Given: an installed systemd user timer
        self.force_platform(linux=True)
        have = mock.patch.object(u, "have", return_value=True)
        have.start()
        self.addCleanup(have.stop)
        self.capture_subprocess()
        at.install()
        timer_path = self.home / ".config" / "systemd" / "user" / f"{at.LABEL}.timer"
        self.assertTrue(timer_path.exists())
        calls = self.capture_subprocess()

        # When: uninstall() is called
        at.uninstall()

        # Then: systemctl disabled it, and the unit files are gone
        disable_calls = [c["cmd"] for c in calls if c["cmd"][:2] == ["systemctl", "--user"] and "disable" in c["cmd"]]
        self.assertEqual(len(disable_calls), 1)
        self.assertFalse(timer_path.exists())

    def test_linux_uninstall_with_cron_removes_the_tagged_line(self) -> None:
        # Given: an installed cron fallback entry
        self.force_platform(linux=True)
        have = mock.patch.object(u, "have", return_value=False)
        have.start()
        self.addCleanup(have.stop)
        install_calls = self.capture_subprocess(
            {"crontab -l": subprocess.CompletedProcess(["crontab", "-l"], 0, stdout="")}
        )
        at.install()
        installed_crontab = [c for c in install_calls if c["cmd"] == ["crontab", "-"]][0][
            "kwargs"
        ]["input"]
        self.assertIn(at.CRON_TAG, installed_crontab)

        # When: uninstall() is called, with that crontab now "current"
        uninstall_calls = self.capture_subprocess(
            {
                "crontab -l": subprocess.CompletedProcess(
                    ["crontab", "-l"], 0, stdout=installed_crontab
                )
            }
        )
        at.uninstall()

        # Then: the tagged line is gone from the crontab that gets written back
        set_calls = [c for c in uninstall_calls if c["cmd"] == ["crontab", "-"]]
        self.assertEqual(len(set_calls), 1)
        self.assertNotIn(at.CRON_TAG, set_calls[0]["kwargs"]["input"])

    def test_windows_uninstall_issues_schtasks_delete(self) -> None:
        # Given: a forced Windows platform
        self.force_platform(windows=True)
        calls = self.capture_subprocess()

        # When: uninstall() is called
        at.uninstall()

        # Then: schtasks /Delete was issued for our task name
        delete_calls = [c["cmd"] for c in calls if c["cmd"][:2] == ["schtasks", "/Delete"]]
        self.assertEqual(len(delete_calls), 1)
        self.assertIn(at.LABEL, delete_calls[0])


class StatusTests(_PlatformMixin, _HomeMixin, _SubprocessMixin, unittest.TestCase):
    def test_macos_status_not_installed_then_installed(self) -> None:
        # Given: a forced macOS platform with nothing installed yet
        self.force_platform(macos=True)
        self.capture_subprocess()

        # Then: status reports "not installed"
        self.assertEqual(at.status(), "not installed")

        # When: install() runs
        at.install()

        # Then: status flips to "installed"
        self.assertEqual(at.status(), "installed")

    def test_linux_status_with_systemd_not_installed_then_installed(self) -> None:
        # Given: a forced Linux platform with systemctl present, nothing installed
        self.force_platform(linux=True)
        have = mock.patch.object(u, "have", return_value=True)
        have.start()
        self.addCleanup(have.stop)
        self.capture_subprocess()

        # Then: status reports "not installed"
        self.assertEqual(at.status(), "not installed")

        # When: install() runs
        at.install()

        # Then: status flips to "installed"
        self.assertEqual(at.status(), "installed")

    def test_windows_status_reflects_schtasks_query_result(self) -> None:
        # Given: a forced Windows platform where the query fails (not found)
        self.force_platform(windows=True)
        self.capture_subprocess(
            {"schtasks /Query": subprocess.CompletedProcess([], returncode=1)}
        )
        # Then: status reports "not installed"
        self.assertEqual(at.status(), "not installed")

        # Given: the query now succeeds (task found)
        self.capture_subprocess(
            {"schtasks /Query": subprocess.CompletedProcess([], returncode=0)}
        )
        # Then: status reports "installed"
        self.assertEqual(at.status(), "installed")


class TimerEntryPointTests(_ConfigMixin, unittest.TestCase):
    """`refresh` is stubbed with a clean no-op in every test that isn't
    specifically exercising the token-refresh gate — `token_refresh` defaults
    to True, so leaving it unstubbed would spawn four real provider
    subprocesses against the real ``~/.ai-accounts`` store."""

    _clean_refresh = staticmethod(lambda: "")

    def test_run_once_does_not_probe_when_disabled(self) -> None:
        # Given: the master switch is off (the config default)
        aw.save_config({"enabled": False})
        probe = mock.Mock()

        # When: the scheduled job's entry point fires
        result = at.run_once(check=probe, refresh=self._clean_refresh)

        # Then: it exits cleanly and never touches the probe
        self.assertEqual(result, 0)
        probe.assert_not_called()

    def test_run_once_treats_a_hand_edited_string_false_as_off(self) -> None:
        # Given: `enabled` hand-edited to the STRING "false" — valid JSON, and
        # truthy to bool(), so a bare truthiness check would let the scheduled
        # job run while the user believes the feature is off.
        self.config_path.write_text(
            json.dumps({"enabled": "false"}), encoding="utf-8"
        )
        probe = mock.Mock()

        # When: the scheduled job's entry point fires
        result = at.run_once(check=probe, refresh=self._clean_refresh)

        # Then: the master switch fails CLOSED — a silent no-op, as documented
        self.assertEqual(result, 0)
        probe.assert_not_called()

    def test_run_once_runs_the_check_when_enabled(self) -> None:
        # Given: the master switch is on
        aw.save_config({"enabled": True})
        probe = mock.Mock()

        # When: the scheduled job's entry point fires
        result = at.run_once(check=probe, refresh=self._clean_refresh)

        # Then: the check runs and the entry point exits cleanly
        self.assertEqual(result, 0)
        probe.assert_called_once_with()

    def test_run_once_default_check_drives_autoswitch_across_all_providers(self) -> None:
        # Given: the master switch is on (refresh disabled — this test is only
        # about the autoswitch half), and no explicit check is supplied
        aw.save_config({"enabled": True, "token_refresh": False})

        # When: the scheduled job's entry point fires with its default check
        with mock.patch.object(at, "_run_autoswitch_everywhere") as run:
            result = at.run_once()

        # Then: it reuses the umbrella's quota-provider fan-out, in parallel.
        self.assertEqual(result, 0)
        run.assert_called_once_with()

    def test_run_once_default_check_is_not_invoked_when_disabled(self) -> None:
        # Given: the master switch is off (refresh disabled too — not this
        # test's concern)
        aw.save_config({"enabled": False, "token_refresh": False})

        # When: the scheduled job's entry point fires with its default check
        with mock.patch.object(at, "_run_autoswitch_everywhere") as run:
            result = at.run_once()

        # Then: no fan-out at all — the existing disabled-noop test's guarantee
        # still holds even when nothing is explicitly passed for `check`
        self.assertEqual(result, 0)
        run.assert_not_called()

    def test_background_check_runs_only_quota_providers(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(at, "_run_provider", return_value=result) as run:
            at._run_autoswitch_everywhere()

        self.assertCountEqual(
            [call.args[0] for call in run.call_args_list],
            [autoswitch_hooks.module(provider) for provider in autoswitch_hooks.providers()],
        )

    def test_background_check_skips_agy_without_a_credential_store(self) -> None:
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(
            u, "go_keyring_available", return_value=(False, "no secret-tool")
        ), mock.patch.object(at, "_run_provider", return_value=result) as run:
            at._run_autoswitch_everywhere()

        self.assertCountEqual(
            [call.args[0] for call in run.call_args_list],
            ["ai_accounts.codex_accounts", "ai_accounts.claude_accounts"],
        )


class TokenRefreshGateTests(_ConfigMixin, unittest.TestCase):
    """`token_refresh` is an INDEPENDENT gate from `enabled` (plan.md §4 phase
    4): a user with auto-switch off must still get scheduled token refresh."""

    def test_token_refresh_runs_even_when_autoswitch_is_disabled(self) -> None:
        # Given: auto-switch off, token_refresh on — the exact bug being fixed
        aw.save_config({"enabled": False, "token_refresh": True})
        check = mock.Mock()
        refresh = mock.Mock(return_value="")

        # When: the scheduled job's entry point fires
        result = at.run_once(check=check, refresh=refresh)

        # Then: refresh runs despite auto-switch being off, and autoswitch does not
        self.assertEqual(result, 0)
        refresh.assert_called_once_with()
        check.assert_not_called()

    def test_token_refresh_does_not_run_when_its_own_flag_is_off(self) -> None:
        # Given: auto-switch on, token_refresh explicitly off
        aw.save_config({"enabled": True, "token_refresh": False})
        check = mock.Mock()
        refresh = mock.Mock(return_value="")

        # When: the scheduled job's entry point fires
        result = at.run_once(check=check, refresh=refresh)

        # Then: only autoswitch runs
        self.assertEqual(result, 0)
        check.assert_called_once_with()
        refresh.assert_not_called()

    def test_both_gates_run_independently_when_both_enabled(self) -> None:
        # Given: both flags on
        aw.save_config({"enabled": True, "token_refresh": True})
        check = mock.Mock()
        refresh = mock.Mock(return_value="")

        # When: the scheduled job's entry point fires
        result = at.run_once(check=check, refresh=refresh)

        # Then: both ran on this one tick
        self.assertEqual(result, 0)
        check.assert_called_once_with()
        refresh.assert_called_once_with()

    def test_neither_gate_runs_when_both_are_off(self) -> None:
        # Given: both flags off
        aw.save_config({"enabled": False, "token_refresh": False})
        check = mock.Mock()
        refresh = mock.Mock(return_value="")

        # When: the scheduled job's entry point fires
        result = at.run_once(check=check, refresh=refresh)

        # Then: neither ran
        self.assertEqual(result, 0)
        check.assert_not_called()
        refresh.assert_not_called()

    def test_token_refresh_defaults_to_on(self) -> None:
        # Given: no explicit token_refresh setting (schema default is True)
        aw.save_config({"enabled": False})
        refresh = mock.Mock(return_value="")

        # When: the scheduled job's entry point fires
        result = at.run_once(refresh=refresh)

        # Then: refresh still runs — "on by default" per config_schema
        self.assertEqual(result, 0)
        refresh.assert_called_once_with()

    def test_clean_refresh_never_notifies(self) -> None:
        # Given: token_refresh on, and a refresh call whose output describes a
        # routine, successful rotation (no revoked-token language anywhere)
        aw.save_config({"enabled": False, "token_refresh": True})
        refresh = mock.Mock(
            return_value="All 3 profile(s) refreshed.\n(token was expired — refreshed in place)"
        )

        # When: the scheduled job's entry point fires
        with mock.patch.object(aw, "notify_once") as notify_once:
            result = at.run_once(refresh=refresh)

        # Then: no alert for routine, non-revoked rotation
        self.assertEqual(result, 0)
        notify_once.assert_not_called()

    def test_codex_transient_http_error_does_not_notify(self) -> None:
        # Given: codex's OWN transient-failure wording, which mentions the bare
        # word "revoked" without matching either real revoked-marker phrase —
        # codex_accounts.py:560 `_refresh_reason` returns this for EVERY HTTP
        # status, so a 5xx (server hiccup) prints the same "...may be expired
        # or revoked..." text a 4xx would if it weren't already reclassified.
        # This must never be mistaken for an actual revocation.
        aw.save_config({"enabled": False, "token_refresh": True})
        refresh = mock.Mock(
            return_value=(
                "❌ Refresh failed for wes: HTTP 503 from token endpoint "
                "(refresh token may be expired or revoked)\n"
                "   Token endpoint unreachable — retry later."
            )
        )

        # When: the scheduled job's entry point fires
        with mock.patch.object(aw, "notify_once") as notify_once:
            result = at.run_once(refresh=refresh)

        # Then: no alert — this is a transient 5xx, not a revocation
        self.assertEqual(result, 0)
        notify_once.assert_not_called()

    def test_revoked_refresh_token_triggers_exactly_one_notification(self) -> None:
        # Given: token_refresh on, and a refresh call reporting a revoked token
        # (the exact wording each provider's _is_revoked_error path logs)
        aw.save_config({"enabled": False, "token_refresh": True})
        refresh = mock.Mock(
            return_value=(
                "❌ Refresh token revoked/dead for wes: revoked: refresh token "
                "rejected (invalid_grant)\n   Re-login with: "
                "claude-accounts login-switch wes"
            )
        )

        # When: the scheduled job's entry point fires
        with mock.patch.object(aw, "notify_once") as notify_once:
            result = at.run_once(refresh=refresh)

        # Then: exactly one alert, keyed so repeated ticks de-duplicate
        self.assertEqual(result, 0)
        notify_once.assert_called_once()
        args, _ = notify_once.call_args
        self.assertEqual(args[0], "token-refresh:revoked")

    def test_bulk_revoked_summary_alone_still_triggers_notification(self) -> None:
        # Given: codex's bulk-only wording (a profile with no refresh_token to
        # even attempt never prints "Refresh token revoked" inline — only the
        # end-of-run "❌ Revoked (re-login required): <names>" summary)
        aw.save_config({"enabled": False, "token_refresh": True})
        refresh = mock.Mock(return_value="❌ Revoked (re-login required): gkm85664")

        # When: the scheduled job's entry point fires
        with mock.patch.object(aw, "notify_once") as notify_once:
            result = at.run_once(refresh=refresh)

        # Then: the bulk phrase alone is enough to trigger the alert
        self.assertEqual(result, 0)
        notify_once.assert_called_once()

    def test_transient_refresh_failure_does_not_notify(self) -> None:
        # Given: a non-zero exit that is NOT a revocation (e.g. network error) —
        # the marker-scan must not treat every failure as revocation
        aw.save_config({"enabled": False, "token_refresh": True})
        refresh = mock.Mock(
            return_value="⚠️  Direct refresh unavailable (timeout) — using the Grok CLI."
        )

        # When: the scheduled job's entry point fires
        with mock.patch.object(aw, "notify_once") as notify_once:
            result = at.run_once(refresh=refresh)

        # Then: no revocation language, no alert
        self.assertEqual(result, 0)
        notify_once.assert_not_called()

    def test_default_refresh_drives_all_four_providers_refresh_all(self) -> None:
        # Given: the default refresh callable (no explicit `refresh` injected)
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        # When: _run_token_refresh_everywhere runs with u.run mocked
        with mock.patch.object(at.u, "run", return_value=completed) as run:
            output = at._run_token_refresh_everywhere()

        # Then: one `<module> refresh --all` call per provider in ai_accounts._TOOLS
        self.assertEqual(output, "")
        called_modules = [c.args[0][2] for c in run.call_args_list]
        expected_modules = [module for _, module in at.ai_accounts._TOOLS]
        self.assertEqual(called_modules, expected_modules)
        for c in run.call_args_list:
            self.assertEqual(c.args[0][-2:], ["refresh", "--all"])
            self.assertEqual(c.kwargs.get("timeout"), at._REFRESH_TIMEOUT_SEC)

    def test_a_hung_provider_is_cut_off_rather_than_blocking_the_tick(self) -> None:
        # Given: one provider's refresh subprocess never returns in time
        timeout = subprocess.TimeoutExpired(
            cmd=["python", "-m", "ai_accounts.grok_accounts", "refresh", "--all"],
            timeout=at._REFRESH_TIMEOUT_SEC,
            output="partial output before the hang",
            stderr="",
        )

        # When: _run_token_refresh_everywhere runs with u.run always timing out
        with mock.patch.object(at.u, "run", side_effect=timeout):
            output = at._run_token_refresh_everywhere()

        # Then: it returns instead of hanging, carrying whatever was captured
        self.assertIn("partial output before the hang", output)


class MainRunInstallDispatchTests(
    _PlatformMixin, _HomeMixin, _SubprocessMixin, _ConfigMixin, unittest.TestCase
):
    def test_main_run_invokes_run_once(self) -> None:
        # Given: the "run" subcommand, as the scheduler would invoke it
        with mock.patch.object(at, "run_once", return_value=0) as run_once:
            # When: main(["run"]) is called
            rc = at.main(["run"])
        # Then: run_once() fired and its return code is propagated
        run_once.assert_called_once_with()
        self.assertEqual(rc, 0)

    def test_main_install_invokes_install(self) -> None:
        # Given: the "install" subcommand
        with mock.patch.object(at, "install") as install:
            # When: main(["install"]) is called
            rc = at.main(["install"])
        # Then: install() fired
        install.assert_called_once_with()
        self.assertEqual(rc, 0)


class RunCommandQuotingTests(unittest.TestCase):
    """An interpreter path with a space must survive the shell.

    uv/pyenv installs live under paths like `~/Library/Application Support/...`;
    an unquoted ExecStart or crontab line silently splits at the space and the
    scheduled job never runs.
    """

    def test_a_posix_run_command_round_trips_through_shell_splitting(self) -> None:
        # Given: an interpreter whose path contains a space
        spaced = "/tmp/py 3/bin/python"
        # When: composing the command scheduled by cron / systemd
        with mock.patch.object(at.sys, "executable", spaced):
            command = at._run_command()
        # Then: a shell parses the interpreter back out as ONE argument
        import shlex

        self.assertEqual(shlex.split(command)[0], spaced)

    def test_the_windows_run_command_double_quotes_a_spaced_interpreter(self) -> None:
        # Given: the same spaced interpreter, on the schtasks path
        spaced = r"C:\Program Files\Python\python.exe"
        # When
        with mock.patch.object(at.sys, "executable", spaced):
            command = at._run_command_windows()
        # Then: cmd.exe-style double quoting, not POSIX single quotes
        self.assertTrue(command.startswith(f'"{spaced}"'), command)


class CronIntervalClampTests(unittest.TestCase):
    """`*/N` in cron's minute field is only valid for N in 0-59."""

    def test_an_interval_longer_than_an_hour_is_clamped_to_a_valid_minute_field(self) -> None:
        # Given: a 2-hour interval, which would naively render as */120
        line = at._cron_line(2 * 60 * 60)
        # When: reading back the minute field
        minute_field = line.split()[0]
        step = int(minute_field.removeprefix("*/"))
        # Then: it stays inside cron's 0-59 minute range
        self.assertLessEqual(step, 59, line)
        self.assertGreaterEqual(step, 1, line)


class ReinstallIdempotencyTests(_PlatformMixin, _HomeMixin, _SubprocessMixin, unittest.TestCase):
    """Re-installing with a new interval must actually take effect.

    Rewriting the plist/unit alone leaves the already-loaded job running on the
    OLD interval, so a "change the interval" flow silently does nothing.
    """

    def test_reinstalling_on_macos_unloads_the_stale_job_before_loading(self) -> None:
        # Given: a timer already installed on macOS
        self.force_platform(macos=True)
        calls = self.capture_subprocess()
        at.install(1800)
        calls.clear()
        # When: re-installing with a different interval
        at.install(600)
        # Then: the stale job is unloaded before the new one is loaded
        verbs = [c["cmd"][1] for c in calls if c["cmd"][0] == "launchctl"]
        self.assertIn("unload", verbs, calls)
        self.assertLess(verbs.index("unload"), verbs.index("load"), verbs)

    def test_reinstalling_on_linux_systemd_reloads_the_daemon(self) -> None:
        # Given: systemd available on Linux
        self.force_platform(linux=True)
        with mock.patch.object(u, "have", return_value=True):
            calls = self.capture_subprocess()
            # When
            at.install(600)
        # Then: systemd is told to re-read the rewritten unit files
        joined = [" ".join(c["cmd"]) for c in calls]
        self.assertTrue(
            any("daemon-reload" in c for c in joined),
            joined,
        )


class MainDispatchTests(_PlatformMixin, _HomeMixin, _ConfigMixin, _SubprocessMixin, unittest.TestCase):
    """`python -m ai_accounts.autoswitch_timer <cmd>` — the scheduled entry point."""

    def test_main_routes_uninstall(self) -> None:
        with mock.patch.object(at, "uninstall") as uninstall:
            rc = at.main(["uninstall"])
        self.assertEqual(rc, 0)
        uninstall.assert_called_once_with()

    def test_main_routes_status_and_prints_it(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with mock.patch.object(at, "status", return_value="not installed"), redirect_stdout(buf):
            rc = at.main(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("not installed", buf.getvalue())

    def test_main_rejects_an_unknown_command_with_exit_2(self) -> None:
        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = at.main(["bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("bogus", buf.getvalue())
