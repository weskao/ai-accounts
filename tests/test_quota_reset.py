"""Tests for quota_reset: the pure reset-detection rule, collect()'s
per-provider failure isolation, grouped notification wording, and the state
file's permissions. All filesystem access is redirected into a temp dir via
AI_ACCOUNTS_CONFIG_JSON; no real subprocess, no real notification is sent.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_accounts import autoswitch as aw
from ai_accounts import quota_reset as qr
from ai_accounts.providers import Provider


def _win(used_pct: int, reset_time: int) -> qr.WindowSnapshot:
    return qr.WindowSnapshot(used_pct=used_pct, reset_time=reset_time)


class DetectTests(unittest.TestCase):
    """detect() is pure — no I/O, same inputs always give the same output."""

    def test_first_sighting_does_not_fire(self) -> None:
        snapshot = {"codex": {"work": {"hourly": _win(95, 1000)}}}

        new_state, events = qr.detect({}, snapshot, now=500, min_used_pct=90)

        self.assertEqual(events, [])
        self.assertEqual(new_state["codex/work/hourly"], {"reset_time": 1000, "used_pct": 95})

    def test_repeated_tick_same_reset_time_does_not_fire(self) -> None:
        state = {"codex/work/hourly": {"reset_time": 1000, "used_pct": 95}}
        snapshot = {"codex": {"work": {"hourly": _win(97, 1000)}}}

        _, events = qr.detect(state, snapshot, now=1500, min_used_pct=90)

        self.assertEqual(events, [])

    def test_real_reset_fires_exactly_once_then_goes_quiet(self) -> None:
        state = {"codex/work/hourly": {"reset_time": 1000, "used_pct": 95}}
        snapshot = {"codex": {"work": {"hourly": _win(5, 2000)}}}

        # Tick right after the reset actually happened: fires once.
        state, events = qr.detect(state, snapshot, now=1100, min_used_pct=90)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event.provider, event.profile, event.window), ("codex", "work", "hourly"))
        self.assertEqual(event.used_pct, 95)  # the usage right before the reset
        self.assertEqual(event.reset_time, 2000)  # the fresh, next reset instant

        # Next tick, nothing new has reset yet: silent.
        _, events = qr.detect(state, snapshot, now=1500, min_used_pct=90)
        self.assertEqual(events, [])

    def test_below_min_used_pct_does_not_fire(self) -> None:
        state = {"codex/work/hourly": {"reset_time": 1000, "used_pct": 50}}
        snapshot = {"codex": {"work": {"hourly": _win(5, 2000)}}}

        _, events = qr.detect(state, snapshot, now=1100, min_used_pct=90)

        self.assertEqual(events, [])

    def test_jitter_under_60s_does_not_fire(self) -> None:
        state = {"codex/work/hourly": {"reset_time": 1000, "used_pct": 95}}
        # Only 30s later than the previous reset_time — within jitter tolerance.
        snapshot = {"codex": {"work": {"hourly": _win(95, 1030)}}}

        _, events = qr.detect(state, snapshot, now=1100, min_used_pct=90)

        self.assertEqual(events, [])

    def test_failed_collect_preserves_prior_state_for_that_provider(self) -> None:
        state = {
            "codex/work/hourly": {"reset_time": 1000, "used_pct": 95},
            "claude/keep/hourly": {"reset_time": 500, "used_pct": 20},
            "claude/gone/weekly": {"reset_time": 500, "used_pct": 20},
        }
        snapshot = {
            "codex": None,  # collect() failed for codex this tick
            "claude": {"keep": {"hourly": _win(21, 500)}},  # "gone" no longer present
        }

        new_state, events = qr.detect(state, snapshot, now=600, min_used_pct=90)

        self.assertEqual(events, [])
        self.assertEqual(new_state["codex/work/hourly"], {"reset_time": 1000, "used_pct": 95})
        self.assertNotIn("claude/gone/weekly", new_state)
        self.assertIn("claude/keep/hourly", new_state)


class ReportTests(unittest.TestCase):
    def test_multiple_windows_reset_in_one_tick_send_one_grouped_notification(self) -> None:
        events = [
            qr.ResetEvent("codex", "work", "hourly", 95, 2000),
            qr.ResetEvent("claude", "personal", "weekly", 91, 3000),
        ]

        with mock.patch.object(qr.aw, "notify", return_value=True) as notify:
            sent = qr.report(events)

        self.assertTrue(sent)
        notify.assert_called_once()
        title, body = notify.call_args[0]
        self.assertIn("2", title)
        self.assertIn("codex", body)
        self.assertIn("claude", body)

    def test_single_event_sends_one_notification(self) -> None:
        events = [qr.ResetEvent("codex", "work", "hourly", 95, 2000)]

        with mock.patch.object(qr.aw, "notify", return_value=True) as notify:
            qr.report(events)

        notify.assert_called_once()


class CollectTests(unittest.TestCase):
    """_collect_one swallows its own failures and returns None — never raises."""

    def test_timeout_returns_none(self) -> None:
        provider = Provider(
            "codex", "codex-accounts", "ai_accounts.codex_accounts", "codex", "", "",
            reset_windows=("hourly", "weekly"),
        )
        with mock.patch.object(
            qr.u, "run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)
        ):
            self.assertIsNone(qr._collect_one(provider))

    def test_non_zero_exit_returns_none(self) -> None:
        provider = Provider(
            "codex", "codex-accounts", "ai_accounts.codex_accounts", "codex", "", "",
            reset_windows=("hourly", "weekly"),
        )
        with mock.patch.object(
            qr.u, "run", return_value=subprocess.CompletedProcess([], returncode=1, stdout="")
        ):
            self.assertIsNone(qr._collect_one(provider))

    def test_parses_windows_and_skips_no_quota_api_profiles(self) -> None:
        provider = Provider(
            "codex", "codex-accounts", "ai_accounts.codex_accounts", "codex", "", "",
            reset_windows=("hourly", "weekly"),
        )
        payload = [
            {
                "name": "work",
                "no_quota_api": False,
                "usage": {
                    "hourly": {"percent": 42, "reset_time": 1000},
                    "weekly": {"percent": 10, "reset_time": 2000},
                },
            },
            {"name": "unverified", "no_quota_api": True, "usage": None},
        ]
        with mock.patch.object(
            qr.u,
            "run",
            return_value=subprocess.CompletedProcess([], returncode=0, stdout=json.dumps(payload)),
        ):
            result = qr._collect_one(provider)

        self.assertEqual(set(result), {"work"})
        self.assertEqual(result["work"]["hourly"], qr.WindowSnapshot(used_pct=42, reset_time=1000))


class StateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(
            os.environ,
            {"AI_ACCOUNTS_CONFIG_JSON": str(Path(self.tmp.name) / "config.json")},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def test_state_file_created_with_0600_permissions(self) -> None:
        qr._write_state({"codex/work/hourly": {"reset_time": 1000, "used_pct": 95}})

        path = qr.state_path()
        self.assertTrue(path.is_file())
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_state_file_lives_beside_config_not_autoswitch_state(self) -> None:
        path = qr.state_path()

        self.assertEqual(path.name, "quota-reset-state.json")
        self.assertEqual(path.parent, Path(self.tmp.name))

    def test_config_dir_stays_in_sync_with_autoswitch_config_path(self) -> None:
        # _config_dir() duplicates autoswitch.config_path()'s env-var lookup on
        # purpose (keeps quota_reset's autoswitch surface load_config/notify-only)
        # — this guards the two from silently drifting apart.
        self.assertEqual(qr.state_path().parent, aw.config_path().parent)


if __name__ == "__main__":
    unittest.main()
