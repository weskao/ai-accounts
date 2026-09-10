# Quota-reset notifications — Phase 1 spec

Source plan: [`docs/quota-reset-notifications-plan.md`](quota-reset-notifications-plan.md)
(left untouched — this is a new, separate document). This spec locks the decisions the
plan left open or suggested differently, and breaks Phase 1 into an executable,
dependency-ordered task table.

## Goal

Detect, per (provider, profile, quota window), the moment a quota window rolls over
(`reset_time` reached and the API confirms a new, later `reset_time`) and send one
desktop/Telegram notification saying that account/window is usable again — reusing the
existing timer (`autoswitch_timer.run_once`), notification channel (`autoswitch.notify`),
and config-schema machinery (`config_schema.FIELDS`). No new dependency, no new scheduler,
no provider HTTP code touched.

## Goal-wide acceptance criteria

1. A quota window's reset fires **exactly one** notification the first time it is
   observed to have rolled over — never on the first sighting of a window (no prior
   state), never on a repeated tick with an unchanged `reset_time`, never on a `list
   --json` failure for that provider (state held, not wiped).
2. Detection only fires for a window whose *previous* recorded `used_pct` was
   `>= reset_notify_min_used_pct` — windows that reset lightly-used stay silent.
3. `reset_notify` and `reset_notify_min_used_pct` are the only two new keys; both are
   declared exactly once, in `config_schema.FIELDS`, and need zero per-key branches in
   `config_menu.py`, `config` CLI, `export`/`import`, or the non-TTY fallback (per this
   repo's `CLAUDE.md` "Adding a config setting" rule).
4. `codex` and `claude` are covered in Phase 1 (`hourly`/`weekly` windows); `copilot` is
   covered via its `monthly` window; `grok` and `vibe` are correctly inert (no quota
   API); `agy` is covered via its four cached windows (originally excluded by Locked
   decision (a) — see "Revision: agy via its usage cache").
5. The feature does nothing at all — no extra `list --json` calls, no state file, no
   notification — unless the scheduled timer is installed (`ai-accounts install-timer`).
   Nothing added in Phase 1 prompts the user to install it (Locked decision (c)).
6. `uv run pytest` passes, covering every row of the plan's §6 test matrix plus the
   `run_once` gate behavior (off → `collect` never called; on → called; independent of
   `enabled`/`token_refresh`).
7. `README.md` documents: how to turn it on, what the two settings mean and their actual
   defaults, that detection latency is bounded by the timer interval, per-provider
   coverage including what agy's cache-derived readings do and don't guarantee, and the
   default-on notification-volume caveat (Locked decision (d)).
8. `TODO.md`'s "Future work" quota-reset item is checked off and points at this spec.

## Locked decisions

These override or make explicit what `docs/quota-reset-notifications-plan.md` left as a
suggestion, a footnote, or an implicit default. Nothing below is up for silent
re-interpretation by an implementing task — a task that finds a reason to deviate stops
and reports it rather than picking a different default.

> **Superseded after sign-off (2026-09-11):** decision (a) below was reversed once a path
> around *both* of its reasons was found — `agy` is now covered, from its local usage cache
> rather than a live probe. See "Revision: agy via its usage cache" at the end of this
> document. The original reasoning is kept intact for the record.

**(a) `agy` is excluded from Phase 1 `reset_windows` entirely.**
The plan's §2 table lists `agy` with four windows (`gemini_session`, `gemini_weekly`,
`other_session`, `other_weekly`). Phase 1 ships `reset_windows = ()` for `agy` instead —
the same empty tuple as `grok`/`vibe` — for two independent, each-sufficient reasons:

- `agy`'s `list --json` is **not read-only for the active profile**: it writes the
  shared credential slot as a side effect of reading it. Polling it every timer tick
  (30 min default) for reset-detection purposes means the tick itself perturbs the
  state it's trying to observe.
- For a **non-active** profile, `agy` usage is served from `usage-cache.json`, and the
  plan's own §7-1 open question already flags that this cache's `reset_time` may never
  advance past the real reset (it only updates when that profile is actually used or
  the IDE quits). If `reset_time` never advances, the `fresh.reset_time >= prev.reset_time
  + 60` condition in `detect()` can never become true — detection can structurally never
  fire for a non-active agy profile, which is exactly the case (an account benched by
  autoswitch) this feature exists to notify about.

Net effect: `Provider.reset_windows` for `agy` is `()` in Phase 1, matching `grok`/`vibe`.
Revisiting this needs a read-only, cache-independent usage source for agy — out of scope
here, not merely deferred to a numbered later phase.

**(b) Defaults: `reset_notify=True`, `reset_notify_min_used_pct=90`.**
The plan's §3/§7-4 draft suggested `default=False` (opt-in) and `min_used_pct=80`. Phase 1
ships default-**on** at a **90%** threshold instead:

- `reset_notify: bool = True` — this is a "tell me my stuff works again" notification,
  not a new intrusive alert category; existing users get it without an upgrade step.
- `reset_notify_min_used_pct: int = 90` (not 80) — raises the bar from the plan's
  `switch_when_used_pct`-parity suggestion so default-on doesn't also mean noisy-by-default:
  only windows that were genuinely nearly exhausted trigger a notification on reset.

**(c) The feature is inert without `ai-accounts install-timer`; no auto-install prompt.**
`reset_notify` only does anything inside `autoswitch_timer.run_once()`, which only runs
under the installed scheduled timer. The plan's §3 also proposed widening `cmd_config`'s
existing "Install it now?" prompt from `enabled` to `enabled or reset_notify` (its file
#6, `config_menu.py`) so turning the setting on would proactively offer to install the
timer. **That tie-in is out of scope for Phase 1** — it stays exactly where the plan's own
§8 phasing already put it, Phase 2, and is not one of the six tasks below. A user who
enables `reset_notify` without a timer installed sees no notifications and no in-product
signal telling them why, until Phase 2 ships.

**(d) Default-on notification volume is bounded and intended, but real.**
With `reset_notify=True` out of the box, an existing user with the timer already
installed (any table `enabled`/`token_refresh` user) starts getting notifications the
next time a covered window resets **without taking any action** — this is a behavior
change on upgrade, not just a new opt-in knob. Concretely, for a single provider/profile
sitting above the 90% threshold: the 5h (`hourly`) window resets up to ~5 times/day, so
up to ~5 desktop notifications/day *per provider* at that window, plus at most one more
for the weekly/monthly window in the same period. This is bounded (one notification per
window per reset, never more, per acceptance criterion 1) and is the intended behavior
per decision (b) — but it must be stated plainly in `README.md`, not left implicit.

## Task table (Phase 1, 6 tasks)

Tasks are numbered in dependency order; a task may start once every task in its
`depends-on` column has landed. Tasks 1–2 have no dependency on each other and can run in
parallel; everything else is a chain.

| # | Task | File(s) | Change | Depends on |
|---|------|---------|--------|-------------|
| 1 | Provider capability | [`src/ai_accounts/providers.py`](../src/ai_accounts/providers.py) | Add `reset_windows: tuple[str, ...] = ()` to `Provider`; set it per Locked decision (a): `codex`/`claude` → `("hourly", "weekly")`, `copilot` → `("monthly",)`, `agy`/`grok`/`vibe` → `()`. Extend the module's `__main__` self-check by one line. | — |
| 2 | Config schema | [`src/ai_accounts/config_schema.py`](../src/ai_accounts/config_schema.py) | Add two `Field`s to `FIELDS`, group `"Notifications"`, `programs=("ai-accounts", "codex-accounts", "claude-accounts", "copilot-accounts")` (no `agy-accounts`, per decision (a); no `grok-accounts`/`vibe-accounts`, no quota API): `reset_notify` (`bool`, `default=True`, per decision (b)) and `reset_notify_min_used_pct` (`int`, `default=90`, `minimum=0`, `maximum=100`, `clamp=True`, per decision (b)). | — |
| 3 | i18n strings | [`src/ai_accounts/i18n.py`](../src/ai_accounts/i18n.py) | Add `config.reset_notify*.label/help` zh-TW pair (English lives in the `Field.label`/`help` from task 2); `notify.reset.title` / `notify.reset.many.title` / `notify.reset.line` / `notify.reset.body` (en + zh-TW); `window.hourly` / `window.weekly` / `window.monthly` display names. | 1, 2 |
| 4 | Detection engine | [`src/ai_accounts/quota_reset.py`](../src/ai_accounts/quota_reset.py) (new) | `state_path()`, `collect()` (parallel `list --json` per provider with non-empty `reset_windows`), `detect(state, snapshot, now, min_used_pct)` (pure function — see plan §1.1 for the exact fire condition, unchanged by this spec), `report()` (single vs. `many` notification text via task 3's i18n keys), `run_tick()` (collect → detect → persist state to `~/.ai-accounts/quota-reset-state.json` via the existing `_write_private` atomic-0600 helper → notify via `autoswitch.notify()`, never `notify_once`). | 1, 2, 3 |
| 5 | Timer wiring | [`src/ai_accounts/autoswitch_timer.py`](../src/ai_accounts/autoswitch_timer.py) | Add `reset_notify` as a third, independent gate in `run_once()` alongside `enabled`/`token_refresh`, calling `quota_reset.run_tick()` (injectable `collect`, matching the existing test seam for the other two gates). | 4 |
| 6 | Tests + docs | [`tests/test_quota_reset.py`](../tests/test_quota_reset.py) (new), [`tests/test_autoswitch_timer.py`](../tests/test_autoswitch_timer.py), [`README.md`](../README.md), [`TODO.md`](../TODO.md) | New test file: every row of plan §6's matrix against `detect()`/`report()`/`run_tick()` with placeholder data (no real account data, per this repo's `CLAUDE.md`). Existing timer test file: gate on/off/independent-of-other-two-gates cases. `README.md`: new "Quota-reset notifications" subsection under Auto-switch — defaults from decision (b), the agy exclusion and why from decision (a), the install-timer prerequisite from decision (c), and the notification-volume caveat from decision (d). `TODO.md`: check off the "Future work" quota-reset item, point it at this spec. | 1, 2, 3, 4, 5 |

Not touched by Phase 1 (unchanged from the plan): any `*_accounts.py`/`*_usage.py`
provider HTTP code, `autoswitch.py`'s engine, `autoswitch_hooks.py` (event-driven hooks
only run `autoswitch`; reset detection is timer-only), and — per Locked decision (c) —
`config_menu.py`'s install prompt and `doctor.py`'s timer-note (both remain Phase 2).

## Sign-off verification: `reset_time` stability against live accounts

Task 4's `detect()` assumes `reset_time` is a stable absolute deadline between ticks (it
only fires once `now >= prev.reset_time`). This was checked empirically at Phase 1
sign-off by calling `codex-accounts list --json` / `claude-accounts list --json` twice,
~65s apart (2026-09-10, against this machine's saved profiles), and diffing `reset_time`
per window:

- **`claude`: not testable.** The one saved `claude-accounts` profile returned
  `"error": "HTTP 429 from usage endpoint"` on both calls (no `usage.weekly`/`usage.hourly`
  data at all). This is a documented gap, not a pass — `claude`'s reset-time stability is
  unverified on live data as of this sign-off.
- **`codex`: mixed — stable for 4/5 profiles, sliding for 1/5.** (Profiles anonymized as
  `profile-A`..`profile-E` below — real names withheld per this repo's placeholder-data
  convention.) `profile-A` (hourly + weekly) and `profile-B`/`profile-C` (weekly) returned
  byte-identical `reset_time` across both calls — a fixed absolute deadline, `detect()`
  works as designed. `profile-D`'s weekly `reset_time` moved by 1s against a 77s call gap
  — noise, not a countdown. But `profile-E`'s weekly `reset_time` (0% used — a window
  that had just reset, with nothing drawn from it yet) advanced by exactly 77s, matching
  the call gap exactly:
  `reset_time - refreshed_at` was `2592000` (= the window's own 43200 minutes) on *both*
  calls. Root cause, confirmed by reading `usage_format.py`'s `_window()` (the codex
  primary/secondary-window parser, ~line 299): when the API omits an absolute `reset_at`
  and only supplies `reset_after_seconds`, that function falls back to
  `reset_time = int(time.time()) + reset_after` — recomputed fresh on every call, not a
  stored deadline. For a window sitting at 0% used, this is exactly what codex's API
  appears to return.
- **Consequence for `detect()` — double-gated, so this costs nothing** (verified by
  `test_sliding_reset_time_on_an_unused_window_never_fires` and
  `test_a_sliding_tick_does_not_poison_the_next_real_reset`): for a window in that
  state `fresh.reset_time` is always `~now + window_length`, so `now >=
  prev.reset_time` never becomes true and `detect()` cannot fire for it. That is the
  correct outcome, not a missed feature — the window is *also* independently blocked
  by acceptance criterion 2 (`prev.used_pct >= reset_notify_min_used_pct`), because a
  window nothing has been drawn from sits at 0% used, and a window that was never
  consumed is by definition not one a user needs a "your quota is back" notification
  for. Critically, a sliding value passing *through* state does **not** poison later
  detection: as soon as real usage exists the provider supplies an absolute `reset_at`
  again, state self-corrects on the next tick, and the following genuine reset fires
  normally.
- **Residual (theoretical, not observed):** if a provider ever reported *high* usage
  while omitting `reset_at`, that window's deadline would sit perpetually in the
  future and its reset would go unannounced. The failure direction is a **missed**
  notification, never a false one, and no provider was observed behaving this way (in
  the probe, every window with usage in it carried an absolute `reset_at`). Left
  unguarded deliberately: a heuristic to second-guess a provider's own deadline would
  add false-positive risk to buy back a case that does not occur.

## Revision: agy via its usage cache (2026-09-11)

Locked decision (a) excluded `agy` for two independently-sufficient reasons. Both were
re-checked against the code and both were real — but each has a way around it, so `agy`
is now covered with `reset_windows = ("gemini_session", "gemini_weekly",
"other_session", "other_weekly")`.

| Original blocker | Confirmed? | Way around it |
|---|---|---|
| `list --json` writes the shared credential slot | Yes — `cmd_list` sets `live_query` whenever an active profile exists, even in cached mode, so the active profile is always activated to be read | Don't call the CLI at all. `gemini_accounts.cached_usage_windows()` reads `usage-cache.json` plus the saved profiles — local files only, no activation, no network |
| A benched profile's cached `reset_time` never advances, so `detect()`'s confirmation can never be satisfied | Yes | Treat a cached deadline that has *passed* as a reset that really happened, and synthesise the next window boundary. This is the same one-directional-ageing inference `gemini_accounts._cached_used_pct` already makes — and that autoswitch already trusts to choose accounts, a higher-stakes decision than a notification |

`detect()` is unchanged: the agy collector hands it a normal-looking snapshot, so the
single confirmed fire rule still governs everything.

**Why this cannot turn into notification spam.** The synthesised deadline is the window
boundary *after* `now`, which is a stable value for every tick inside that window — so
`detect()` fires once, not per tick. The reading stored alongside it is `0%` used, so the
`reset_notify_min_used_pct` gate blocks any later rollforward from firing again until a
real probe refreshes the cache with genuine usage. Pinned by
`test_a_benched_profile_notifies_once_and_then_stays_quiet`.

**The trade, stated plainly.** agy readings are only as fresh as the last real probe
(`agy-accounts list`, `list --refresh`, or an autoswitch probe) left in the cache. A
profile that has never been probed has nothing cached and is silently skipped; a stale
cache means a late notification, never a wrong one. Because the next deadline is
computed rather than reported, agy notifications say so instead of quoting a next-reset
time (`notify.reset.cached`), and the `WindowSnapshot`/`ResetEvent` `estimated` flag is
what carries that distinction through to the notification text. Windows with no cached
`reset_time`, or no known window length to roll forward by, are skipped.
