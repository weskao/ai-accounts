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
from ai_accounts import usage_format
from ai_accounts.providers import Provider
from ai_accounts.usage_format import UsageWindow


def _win(used_pct: int, reset_time: int, plan: str | None = None) -> qr.WindowSnapshot:
    return qr.WindowSnapshot(used_pct=used_pct, reset_time=reset_time, plan=plan)


class DetectTests(unittest.TestCase):
    """detect() is pure — no I/O, same inputs always give the same output."""

    def test_first_sighting_does_not_fire(self) -> None:
        snapshot = {"codex": {"work": {"hourly": _win(95, 1000)}}}

        new_state, events = qr.detect({}, snapshot, now=500, min_used_pct=90)

        self.assertEqual(events, [])
        self.assertEqual(
            new_state["codex/work/hourly"],
            {"reset_time": 1000, "used_pct": 95, "seen_at": 500, "plan": None},
        )

    def test_repeated_tick_same_reset_time_does_not_fire(self) -> None:
        state = {"codex/work/hourly": {"reset_time": 1000, "used_pct": 95}}
        snapshot = {"codex": {"work": {"hourly": _win(97, 1000)}}}

        _, events = qr.detect(state, snapshot, now=1500, min_used_pct=90)

        self.assertEqual(events, [])

    def test_an_early_reset_before_the_deadline_still_fires(self) -> None:
        # A provider can hand out quota off its own schedule — a holiday
        # top-up, a goodwill credit. The recorded deadline never arrives, so
        # only the collapse of usage reveals it.
        state = {"codex/work/hourly": {"reset_time": 9_000, "used_pct": 96}}
        snapshot = {"codex": {"work": {"hourly": _win(0, 20_000)}}}

        # Well before the old deadline: the scheduled route cannot fire here.
        state, events = qr.detect(state, snapshot, now=5_000, min_used_pct=90)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].used_pct, 96)
        self.assertEqual(events[0].reset_time, 20_000)

        # And it stays a one-shot: the stored 0% now fails the min_used_pct gate.
        _, again = qr.detect(state, snapshot, now=6_000, min_used_pct=90)
        self.assertEqual(again, [])

    def test_an_early_reset_already_being_used_again_still_fires(self) -> None:
        # Collection is a periodic scan, so the tick can easily land after the
        # user started spending the fresh window. 15% used is not 0%, but the
        # fall is what identifies the reset — plus the new deadline.
        state = {"codex/work/hourly": {"reset_time": 9_000, "used_pct": 96}}
        snapshot = {"codex": {"work": {"hourly": _win(15, 20_000)}}}

        _, events = qr.detect(state, snapshot, now=5_000, min_used_pct=90)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].used_pct, 96)

    def test_an_early_reset_clearing_the_counter_in_place_still_fires(self) -> None:
        # The other off-schedule shape: usage zeroed but the window's end left
        # where it was, so no new deadline corroborates the fall.
        state = {"codex/work/hourly": {"reset_time": 9_000, "used_pct": 96}}
        snapshot = {"codex": {"work": {"hourly": _win(0, 9_000)}}}

        _, events = qr.detect(state, snapshot, now=5_000, min_used_pct=90)

        self.assertEqual(len(events), 1)

    def test_an_early_reset_scanned_after_heavy_use_still_fires(self) -> None:
        # Scan lands late enough that more than half the fresh window is
        # already spent — too shallow a fall to count on its own. But the
        # deadline jumped 5h further out while only 30 minutes passed: a new
        # window was issued, whatever the reading has climbed back to.
        state = {"codex/work/hourly": {"reset_time": 9_000, "used_pct": 96, "seen_at": 3_200}}
        snapshot = {"codex": {"work": {"hourly": _win(60, 9_000 + 18_000)}}}

        _, events = qr.detect(state, snapshot, now=5_000, min_used_pct=90)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].used_pct, 96)

    def test_a_partial_fall_before_the_deadline_does_not_fire(self) -> None:
        # A sliding window ageing out gradually is not a reset. Its derived
        # reset_time advances exactly as fast as the clock (here: 1800s of
        # both), which is the signature that separates it from a new window.
        state = {"codex/work/hourly": {"reset_time": 9_000, "used_pct": 96, "seen_at": 3_200}}
        snapshot = {"codex": {"work": {"hourly": _win(60, 9_000 + 1_800)}}}

        _, events = qr.detect(state, snapshot, now=5_000, min_used_pct=90)

        self.assertEqual(events, [])

    def test_a_small_fall_under_a_low_threshold_does_not_fire(self) -> None:
        # With min_used_pct lowered, a reading that is already low would pass
        # both the gate and the low-reading bound — the required fall is what
        # stops an 8%-to-4% drift from reading as a reset.
        state = {"codex/work/hourly": {"reset_time": 9_000, "used_pct": 8}}
        snapshot = {"codex": {"work": {"hourly": _win(4, 9_000)}}}

        _, events = qr.detect(state, snapshot, now=5_000, min_used_pct=5)

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

    def test_sliding_reset_time_on_an_unused_window_never_fires(self) -> None:
        # A window with no consumption gets no absolute deadline from codex, so
        # usage_format derives reset_time as now + the full window length —
        # a value that slides forward every tick. Such a window is blocked
        # twice over (the deadline is never reached AND 0% is under the
        # threshold), so the sliding value costs nothing.
        window = 18000
        state: qr.State = {}
        for tick in range(4):
            now = 1_000_000 + tick * 1800
            snapshot = {"codex": {"work": {"hourly": _win(0, now + window)}}}
            state, events = qr.detect(state, snapshot, now=now, min_used_pct=90)
            self.assertEqual(events, [])

    def test_a_sliding_tick_does_not_poison_the_next_real_reset(self) -> None:
        # The tick right after a reset reports 0% with a derived (sliding)
        # reset_time. That value lands in state, but the next real reset must
        # still fire: once usage exists the provider supplies an absolute
        # deadline again, and state self-corrects on the way through.
        window = 18000
        state: qr.State = {}
        fired = []
        ticks = [
            (1_000_000, 95, 1_000_600),  # absolute deadline, nearly exhausted
            (1_001_200, 0, 1_001_200 + window),  # reset happened; derived value
            (1_002_400, 95, 1_000_600 + window),  # usage back, absolute again
            (1_020_000, 0, 1_020_000 + window),  # the next real reset
        ]
        for now, used, reset_time in ticks:
            snapshot = {"codex": {"work": {"hourly": _win(used, reset_time)}}}
            state, events = qr.detect(state, snapshot, now=now, min_used_pct=90)
            fired.append(len(events))

        self.assertEqual(fired, [0, 1, 0, 1])

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

    def test_an_estimated_event_does_not_quote_a_next_reset_time(self) -> None:
        # A cache-derived deadline is a computed boundary, not something the
        # provider said — printing it as "Next reset: …" would dress a guess
        # up as fact.
        events = [qr.ResetEvent("agy", "work", "gemini_session", 95, 9_999_999, True)]

        with mock.patch.object(qr.aw, "notify", return_value=True) as notify:
            qr.report(events)

        _, body = notify.call_args[0]
        self.assertIn("cached", body.lower())
        self.assertNotIn(
            usage_format.format_unix_time_compact(9_999_999), body
        )


class AgyCachedCollectorTests(unittest.TestCase):
    """agy is collected from its local usage cache, never `list --json`."""

    def _cached(self, **windows: UsageWindow) -> dict[str, dict[str, UsageWindow]]:
        return {"work": dict(windows)}

    def _collect_at(
        self, now: int, cached: dict[str, dict[str, UsageWindow]]
    ) -> qr.ProviderSnapshot:
        with mock.patch.object(qr.time, "time", return_value=now):
            with mock.patch(
                "ai_accounts.gemini_accounts.cached_usage_windows", return_value=cached
            ):
                collected = qr._collect_agy_cached()
        assert collected is not None
        return collected

    def test_a_future_deadline_is_passed_through_as_reported(self) -> None:
        now = 1_000_000
        cached = self._cached(gemini_session=UsageWindow(95, now + 3600, 300))

        snapshot = self._collect_at(now, cached)

        window = snapshot["work"]["gemini_session"]
        self.assertEqual((window.used_pct, window.reset_time), (95, now + 3600))
        self.assertFalse(window.estimated)

    def test_a_passed_deadline_becomes_an_estimated_future_boundary(self) -> None:
        # The cache still says 95% used, but its deadline is an hour gone: the
        # window has reset even though nothing refreshed the cache to say so.
        reset_time = 1_000_000
        now = reset_time + 3600
        cached = self._cached(gemini_session=UsageWindow(95, reset_time, 300))

        snapshot = self._collect_at(now, cached)

        window = snapshot["work"]["gemini_session"]
        self.assertEqual(window.used_pct, 0)
        self.assertTrue(window.estimated)
        self.assertGreater(window.reset_time, now)

    def test_the_boundary_holds_still_within_a_window_then_advances(self) -> None:
        # Stability is what makes detect() fire once rather than every tick.
        reset_time = 1_000_000
        length = 300 * 60
        first = qr._next_boundary(reset_time, 300, reset_time + 10)
        middle = qr._next_boundary(reset_time, 300, reset_time + length - 10)
        after = qr._next_boundary(reset_time, 300, reset_time + length + 10)

        self.assertEqual(first, reset_time + length)
        self.assertEqual(middle, reset_time + length)
        self.assertEqual(after, reset_time + 2 * length)

    def test_a_window_of_unknown_length_or_deadline_is_skipped(self) -> None:
        now = 1_000_000
        cached = self._cached(
            gemini_session=UsageWindow(95, now - 10, None),  # no length to roll by
            gemini_weekly=UsageWindow(95, None, 10080),  # provider never said
        )

        snapshot = self._collect_at(now, cached)

        self.assertEqual(snapshot["work"], {})

    def test_a_benched_profile_notifies_once_and_then_stays_quiet(self) -> None:
        # The whole point of the cache path: an account parked at 95% gets one
        # "usable again" notification after its window turns over, and no more
        # — even though every later tick still rolls the boundary forward.
        reset_time = 1_000_000
        length = 300 * 60
        cached = self._cached(gemini_session=UsageWindow(95, reset_time, 300))
        state: qr.State = {}
        fired = []

        for tick in range(6):
            now = reset_time - 600 + tick * length
            snapshot = {"agy": self._collect_at(now, cached)}
            state, events = qr.detect(state, snapshot, now=now, min_used_pct=90)
            fired.append(len(events))

        self.assertEqual(fired, [0, 1, 0, 0, 0, 0])

    def test_a_real_probe_confirming_the_reset_does_not_notify_again(self) -> None:
        # The cache-derived fire already told the user. When a real
        # `agy-accounts list` later refreshes the cache with the genuine new
        # deadline, that must not read as a second reset.
        reset_time = 1_000_000
        length = 300 * 60
        stale = self._cached(gemini_session=UsageWindow(95, reset_time, 300))
        probed = self._cached(gemini_session=UsageWindow(5, reset_time + length, 300))
        state: qr.State = {}
        fired = []

        for now, cached in [
            (reset_time - 600, stale),  # deadline still ahead
            (reset_time + 600, stale),  # deadline gone, cache stale -> fires
            (reset_time + 1200, probed),  # real probe confirms the same reset
            (reset_time + 1800, probed),
        ]:
            snapshot = {"agy": self._collect_at(now, cached)}
            state, events = qr.detect(state, snapshot, now=now, min_used_pct=90)
            fired.append(len(events))

        self.assertEqual(fired, [0, 1, 0, 0])

    def test_a_real_probe_landing_past_the_estimated_boundary_stays_quiet(self) -> None:
        # Same as above but the real probe arrives *after* now has already
        # crossed the synthesised boundary, so the deadline it reports is a
        # later one than the estimate — still one reset, still one notification.
        reset_time = 1_000_000
        length = 300 * 60
        stale = self._cached(gemini_session=UsageWindow(95, reset_time, 300))
        probed = self._cached(gemini_session=UsageWindow(5, reset_time + 2 * length, 300))
        state: qr.State = {}
        fired = []

        for now, cached in [
            (reset_time - 600, stale),  # deadline still ahead
            (reset_time + 600, stale),  # deadline gone, cache stale -> fires
            (reset_time + length + 600, stale),  # estimate rolls forward
            (reset_time + length + 1200, probed),  # real probe, later deadline
            (reset_time + length + 1800, probed),
        ]:
            snapshot = {"agy": self._collect_at(now, cached)}
            state, events = qr.detect(state, snapshot, now=now, min_used_pct=90)
            fired.append(len(events))

        self.assertEqual(fired, [0, 1, 0, 0, 0])


class ProviderResetShapeTests(unittest.TestCase):
    """The two shapes an off-schedule reset actually takes in the wild.

    codex zeroes both windows and restarts the weekly one from the reset
    moment, so its weekly deadline jumps a week ahead. claude zeroes both
    windows too but leaves the weekly deadline where it was. Both must
    notify, and at whatever usage the scan happens to land on — a tick is up
    to 30 minutes wide, so the fresh window is often already being spent.
    """

    HOUR = 3600
    DAY = 24 * HOUR
    WEEK = 7 * DAY

    def _detect(self, prev_used, prev_deadline, fresh_used, fresh_deadline, elapsed=1800):
        now = 1_000_000
        state = {
            "p/work/w": {
                "reset_time": now + prev_deadline,
                "used_pct": prev_used,
                "seen_at": now - elapsed,
            }
        }
        snapshot = {"p": {"work": {"w": _win(fresh_used, now + fresh_deadline)}}}
        _, events = qr.detect(state, snapshot, now=now, min_used_pct=90)
        return events

    # ── codex: both windows zeroed, weekly restarted from the reset moment ──

    def test_codex_session_window_restarted(self) -> None:
        # 5h window zeroed and reissued: its end jumps ~5h out.
        events = self._detect(95, 2 * self.HOUR, 0, 5 * self.HOUR)
        self.assertEqual(len(events), 1)

    def test_codex_weekly_window_restarted_from_the_reset_moment(self) -> None:
        # The weekly window had 3 days left; the reset restarts the 7-day
        # count from now, so the deadline jumps 4 days further out.
        events = self._detect(95, 3 * self.DAY, 0, self.WEEK)
        self.assertEqual(len(events), 1)

    def test_codex_weekly_restarted_and_already_in_use_when_scanned(self) -> None:
        events = self._detect(95, 3 * self.DAY, 20, self.WEEK)
        self.assertEqual(len(events), 1)

    # ── claude: both windows zeroed, weekly deadline left in place ──────────

    def test_claude_weekly_window_zeroed_with_its_deadline_unchanged(self) -> None:
        # No jump to corroborate the fall — the deadline is exactly where it
        # was. A fixed window cannot fall without having been cleared.
        events = self._detect(95, 3 * self.DAY, 0, 3 * self.DAY)
        self.assertEqual(len(events), 1)

    def test_claude_weekly_zeroed_and_already_in_use_when_scanned(self) -> None:
        events = self._detect(95, 3 * self.DAY, 15, 3 * self.DAY)
        self.assertEqual(len(events), 1)

    def test_claude_session_window_zeroed_and_heavily_used_when_scanned(self) -> None:
        # A 5h window can absorb a lot inside one 30-minute tick.
        events = self._detect(95, 2 * self.HOUR, 55, 2 * self.HOUR)
        self.assertEqual(len(events), 1)

    def test_a_deadline_that_stayed_put_still_needs_a_real_fall(self) -> None:
        # Noise floor: a couple of points of jitter in the reported percentage
        # is not a reset.
        events = self._detect(95, 3 * self.DAY, 92, 3 * self.DAY)
        self.assertEqual(events, [])

    def test_the_noise_floor_boundary_with_an_unmoved_deadline(self) -> None:
        # Exactly where the floor sits, so the residual is explicit: with the
        # deadline unmoved, a reset is missed only when the fresh window was
        # re-consumed to within 10 points of the old reading inside one tick.
        self.assertEqual(len(self._detect(95, 3 * self.DAY, 85, 3 * self.DAY)), 1)
        self.assertEqual(self._detect(95, 3 * self.DAY, 86, 3 * self.DAY), [])


class PlanChangeTests(unittest.TestCase):
    """A plan upgrade rescales the percentage without resetting anything.

    Going from a 1x to a 5x account leaves the same absolute usage against a
    five-times-larger allowance, so the reported percentage drops sharply
    while the window itself — and its deadline — carry on unchanged. That is
    the exact shape of a counter cleared in place, so without the plan
    identity to tell them apart it would notify about a reset that never
    happened.
    """

    DAY = 24 * 3600

    def _tick(self, state, used, deadline, plan, now):
        snapshot = {"claude": {"work": {"weekly": _win(used, deadline, plan)}}}
        return qr.detect(state, snapshot, now=now, min_used_pct=90)

    def test_a_plan_upgrade_is_not_a_reset(self) -> None:
        now = 1_000_000
        deadline = now + 3 * self.DAY
        state = {
            "claude/work/weekly": {
                "reset_time": deadline,
                "used_pct": 95,
                "seen_at": now - 1800,
                "plan": "pro",
            }
        }

        # 1x at 95% becomes 5x at 19%: same usage, five times the allowance.
        new_state, events = self._tick(state, 19, deadline, "team · 5x", now)

        self.assertEqual(events, [])
        # And the new plan is what later ticks compare against.
        entry = new_state["claude/work/weekly"]
        self.assertEqual(entry["used_pct"], 19)
        self.assertEqual(entry["plan"], "team · 5x")

    def test_a_real_reset_after_a_plan_upgrade_still_fires(self) -> None:
        now = 1_000_000
        deadline = now + 3 * self.DAY
        state = {
            "claude/work/weekly": {
                "reset_time": deadline,
                "used_pct": 95,
                "seen_at": now - 1800,
                "plan": "pro",
            }
        }
        state, events = self._tick(state, 19, deadline, "team · 5x", now)
        self.assertEqual(events, [])

        # The bigger allowance gets used up too, and then really resets.
        state, events = self._tick(state, 93, deadline, "team · 5x", now + 3600)
        self.assertEqual(events, [])
        _, events = self._tick(state, 0, deadline, "team · 5x", now + 7200)
        self.assertEqual(len(events), 1)

    def test_the_same_plan_still_reads_a_cleared_window_as_a_reset(self) -> None:
        # Guard: the suppression must key off the plan actually changing, not
        # merely being present.
        now = 1_000_000
        deadline = now + 3 * self.DAY
        state = {
            "claude/work/weekly": {
                "reset_time": deadline,
                "used_pct": 95,
                "seen_at": now - 1800,
                "plan": "pro",
            }
        }

        _, events = self._tick(state, 15, deadline, "pro", now)

        self.assertEqual(len(events), 1)

    def test_an_unknown_plan_does_not_suppress_detection(self) -> None:
        # agy is read from a cache that carries no plan, and a state entry
        # written before this existed has no plan either. Neither may block a
        # real reset from being reported.
        now = 1_000_000
        deadline = now + 3 * self.DAY
        state = {
            "claude/work/weekly": {
                "reset_time": deadline,
                "used_pct": 95,
                "seen_at": now - 1800,
            }
        }

        _, events = self._tick(state, 15, deadline, None, now)

        self.assertEqual(len(events), 1)


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

    def test_carries_the_reported_plan_onto_every_window(self) -> None:
        # detect() needs the plan to tell an upgrade's rescaled percentage
        # from a real reset, so collection has to carry it through.
        provider = Provider(
            "codex", "codex-accounts", "ai_accounts.codex_accounts", "codex", "", "",
            reset_windows=("hourly", "weekly"),
        )
        payload = [
            {
                "name": "work",
                "no_quota_api": False,
                "plan": "team · 5x",
                "usage": {
                    "hourly": {"percent": 42, "reset_time": 1000},
                    "weekly": {"percent": 10, "reset_time": 2000},
                },
            },
            {
                "name": "no-plan",
                "no_quota_api": False,
                "usage": {"hourly": {"percent": 7, "reset_time": 1000}},
            },
        ]
        with mock.patch.object(
            qr.u,
            "run",
            return_value=subprocess.CompletedProcess([], returncode=0, stdout=json.dumps(payload)),
        ):
            result = qr._collect_one(provider)

        self.assertEqual(result["work"]["hourly"].plan, "team · 5x")
        self.assertEqual(result["work"]["weekly"].plan, "team · 5x")
        # A provider that reports no plan leaves it None rather than guessing.
        self.assertIsNone(result["no-plan"]["hourly"].plan)


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
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

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
