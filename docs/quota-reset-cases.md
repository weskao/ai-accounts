# Quota-reset detection: the case table

Every scenario the reset-notification feature was built and verified against, with the
observed number of notifications. Rows showing **0** are deliberate silence, not a gap.

The rule itself lives in `quota_reset.detect` and `quota_reset._reset_early`; read those
alongside this.

## How a reset is recognised

Detection is a **periodic scan** — one timer tick, default 1800s — not an event stream.
The reading is therefore whatever the window happened to be at when the tick landed, and
a fresh window is often already being spent by then. Nothing in the rule requires a
near-0% reading.

Every route shares one gate: the window must have been at least
`reset_notify_min_used_pct` (default 90) used *before* it reset. A window that reset while
barely touched is not worth a message.

| Route | Evidence | Condition |
|---|---|---|
| On schedule | The recorded deadline arrived and a later one replaced it | `now >= prev.reset_time` and `fresh.reset_time >= prev.reset_time + 60` |
| Off schedule, deadline **jumped** | The end moved further out than the clock did — a new window was issued | `fresh.reset_time - prev.reset_time > (now - prev.seen_at) + 60` |
| Off schedule, deadline **held** | A fixed window cannot fall unless it was cleared | `abs(moved) < 60` and fall `>= 10` |
| Off schedule, deadline moved but did not jump | The sliding-decay shape, so only a deep fall counts | fall `>= 50` |
| Plan changed | Not a reset at all — excluded before any route runs | a known `plan` differs from the one in state |

Thresholds are module constants (`_RESET_MIN_FALL_PCT` = 10, `_RESET_DROP_PCT` = 50),
deliberately not config keys.

## A. On schedule — the deadline arrived

The fresh percentage is ignored entirely on this route.

| Case | Notifications |
|---|---|
| First sighting, 95% used, deadline ahead | 0 |
| Deadline passed, fresh 0%, later deadline reported | 1 |
| Deadline passed, fresh 5% | 1 |
| Deadline passed, fresh 40% | 1 |
| Deadline passed, then two more scans on the same reading | 1 |
| Was only 50% used before the reset (under the gate) | 0 |
| New deadline only 30s later (within jitter tolerance) | 0 |

## B. Off schedule — the deadline jumped

A provider handing out quota early: the recorded deadline is still in the future, so
route A cannot fire. Any fall counts here, however far usage has climbed back.

| Case | Notifications |
|---|---|
| 95% → 0%, deadline jumped +5h | 1 |
| 95% → 5%, deadline jumped | 1 |
| 95% → 15%, deadline jumped | 1 |
| 95% → 40%, deadline jumped | 1 |
| 95% → 60%, deadline jumped | 1 |
| 95% → 96% (no fall at all), deadline jumped | 0 |
| 95% → 60%, deadline slid by exactly one scan interval | 0 |
| 95% → 60%, slid one scan interval + 50s of jitter | 0 |

The last two are a sliding window ageing out: its derived `reset_time` is
`now + reset_after`, so between scans it advances exactly as fast as the clock. That is
the signature separating decay from a newly issued window.

## C. Off schedule — the deadline held

| Case | Notifications |
|---|---|
| 95% → 0%, deadline unmoved | 1 |
| 95% → 11%, deadline unmoved | 1 |
| 95% → 46%, deadline unmoved | 1 |
| 95% → 55%, deadline unmoved | 1 |
| 95% → 85%, deadline unmoved (fall = 10, at the floor) | 1 |
| 95% → 86%, deadline unmoved (fall = 9, under the floor) | 0 |
| 95% → 92%, deadline unmoved (reporting jitter) | 0 |

The 10-point floor absorbs jitter in a reported percentage. **The one residual:** with the
deadline unmoved, a reset is missed if the fresh window was re-consumed to within 10
points of the old reading inside a single tick. For a weekly window that means burning
more than 80% of a week's quota in 30 minutes; for a 5h window it means a user who is
plainly not waiting to be told the quota came back. The window's own scheduled deadline
still fires later.

## D. What each provider actually does on an off-schedule reset

| Provider | Behaviour | Route that catches it |
|---|---|---|
| codex | Zeroes 5h and weekly, **and restarts the weekly count from the reset moment** | Deadline jumps — a weekly window with 3 days left suddenly ends 7 days out |
| claude | Zeroes 5h and weekly, **weekly deadline left where it was** | Deadline held, so the fall itself is the proof |

| Case | Notifications |
|---|---|
| codex 5h 95% → 0%, end +5h | 1 |
| codex weekly 95% → 0%, end 3d → 7d | 1 |
| codex weekly 95% → 20%, end 3d → 7d | 1 |
| claude weekly 95% → 0%, end unchanged | 1 |
| claude weekly 95% → 15%, end unchanged | 1 |
| claude 5h 95% → 55%, end unchanged | 1 |

## E. Plan upgrade / downgrade — not a reset

Moving from a 1x to a 5x account leaves the same absolute usage against five times the
allowance, so the percentage collapses (95% → 19%) with the window and its deadline
untouched — byte for byte the shape of section C. Only the plan identity separates them,
so `codex-accounts list --json` and `claude-accounts list --json` report each profile's
`plan`; claude's carries the rate multiplier (`Max · 5x`) so a 5x → 20x move on one plan
is visible.

| Case | Notifications |
|---|---|
| 1x 95% → 5x 19%, deadline unmoved (upgrade) | 0 |
| 5x 95% → 20x 24%, same plan name (upgrade) | 0 |
| 20x 19% → 1x 95% (downgrade) | 0 |
| Upgrade, then a real reset on the new plan | 1 |
| Plan unchanged, 95% → 15% | 1 |
| Plan unknown on both sides (agy reports none) | 1 |

A downgrade needs no special case: it raises the percentage, and a rise never read as a
reset. The cost of the exclusion: a plan change coinciding with a genuine reset swallows
that one notification, and detection resumes from the next tick.

## F. A sliding `reset_time` on a 0%-used window

codex reports no absolute `reset_at` for a window with no consumption, so `usage_format`
derives `now + reset_after` — a value that slides forward every scan.

| Case | Per-scan | Total |
|---|---|---|
| 0% window, `reset_time` slides every scan (4 scans) | `[0, 0, 0, 0]` | 0 |
| 95% → 0% (slid) → 95% → 0%, a full cycle | `[0, 1, 0, 1]` | 2 |

Double-gated: the deadline is never reached *and* 0% is under the threshold. The second
row is the one that matters — a sliding tick does not poison the next real reset, because
state self-corrects as soon as real usage restores an absolute deadline.

## G. agy, read from its local usage cache

agy cannot be polled live: `agy-accounts list` activates each profile through the shared
CLI credential slot, which a background timer must not touch. Its windows come from
`gemini_accounts.cached_usage_windows` (local file reads only), and a cached deadline that
has already passed is rolled forward to the next window boundary.

| Case | Per-scan | Total |
|---|---|---|
| Cached deadline ahead, then passes, then 4 more scans | `[0, 1, 0, 0, 0, 0]` | 1 |
| Stale fire, then a real probe confirms the same reset | `[0, 1, 0, 0]` | 1 |
| Stale fire, estimate rolls forward, then a later real deadline | `[0, 1, 0, 0, 0]` | 1 |
| Cached window with no `reset_time` at all | `[0, 0]` | 0 |
| Cached deadline passed but window length unknown | `[0, 0]` | 0 |

A rolled-forward reading is `used_pct=0`, `estimated=True`, and its boundary is always in
the future — stable for every scan inside that window, which is what makes it fire once.
The stored 0% then blocks any later rollforward via the `min_used_pct` gate.

Because the next deadline is computed rather than reported, agy notifications say the
reading came from the cache instead of quoting a next-reset time.

## H. Not watched, or skipped

| Provider | Why |
|---|---|
| grok, vibe | No quota API to watch — `reset_windows` is empty, so nothing is collected |
| copilot | The monthly window is watched, but its endpoint is unverified; a parse or HTTP failure degrades to `no_quota_api` and that profile is skipped |

A provider whose collection failed keeps its previous state untouched, so a transient
failure never reads as a reset.

## Timing

| Question | Answer |
|---|---|
| How soon after a reset? | Up to one tick interval (`install-timer --interval`, default 1800s) |
| At boot / login? | The first tick runs immediately — `RunAtLoad` (launchd), `OnBootSec=60` (systemd), an `@reboot` line (cron). Windows' scheduled task has no logon trigger and lands within one interval instead |
| Several windows resetting in one tick? | One grouped notification, never one per window |
| Repeat notifications for the same reset? | Impossible — `detect` is a state machine, and the reading it stores after firing is under the `min_used_pct` gate |
| Feature inert without a timer? | Yes. With no timer installed, `reset_notify` makes no extra `list --json` calls and sends nothing |

## How these counts were produced

Sections A–F come from driving `quota_reset.run_tick` with injected snapshots and a
mocked clock, one scan every 30 minutes; section G from `quota_reset.detect` with the agy
cache collector. Most rows also exist as tests — `uv run pytest tests/test_quota_reset.py`
— and the tests are the authority: this file is the readable map, not the specification.
