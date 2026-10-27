"""Quota-reset notifications: tell the user when a provider's usage window
rolled over and full quota is available again (see TODO.md's future-work
item).

Three layers, kept separate so the detection rule can be unit-tested with no
subprocess and no clock: :func:`collect` gathers a fresh usage snapshot per
watched provider (every entry in ``providers.PROVIDERS`` with a non-empty
``reset_windows`` — codex/claude/copilot/grok/devin through their `list --json`, agy from
its local cache instead, see :func:`_collect_agy_cached`); :func:`detect` is a
pure function comparing that snapshot against the previous tick's stored
state; :func:`run_tick` wires them together and sends the notification.

Import direction (leaf-ward, like the rest of this package): this module
imports :mod:`providers`, :mod:`i18n`, :mod:`usage_format`, and
:mod:`autoswitch` — but only ``autoswitch.load_config``/``autoswitch.notify``,
never ``notify_once`` or the config-path/state-path helpers — plus
:mod:`gemini_accounts` lazily, inside the one collector that needs it, so a
provider module is not pulled in on every import of this one. ``autoswitch``
must never import this module back.

State lives in its own file beside the shared config, NOT
``autoswitch-state.json``: that file's ``notify_once`` pruner treats any
non-numeric value as junk and strips it on the next unrelated write, which
would silently erase this feature's dict-valued entries. Written via
``_utils.atomic_write_json`` (0600), not ``autoswitch._write_private`` — same
permission pattern, different helper, so this module has no dependency on
autoswitch's private write path either.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _utils as u
from . import autoswitch as aw
from . import i18n, usage_format
from .providers import PROVIDERS, Provider

# Same env override autoswitch.config_path() honors — duplicated (not
# imported) so this module's only autoswitch dependency stays load_config/
# notify, per the import-direction contract above.
_CONFIG_ENV = "AI_ACCOUNTS_CONFIG_JSON"

# config_schema's declared default for reset_notify_min_used_pct — used only
# as a fallback if a hand-edited config has a non-numeric value there.
_DEFAULT_MIN_USED_PCT = 90
# Same fallback role for reset_expiry_notify_hours.
_DEFAULT_EXPIRY_HOURS = 24

# ponytail: fixed per-provider ceiling, matching autoswitch_timer's
# _REFRESH_TIMEOUT_SEC precedent and its rationale verbatim — a wedged
# `list --json` subprocess must not silently kill every future reset-check
# tick. Upgrade path: make configurable if a provider routinely needs longer.
_LIST_TIMEOUT_SEC = 300

# Off-schedule resets (a holiday top-up, a goodwill credit) — see
# _reset_early. With the deadline unmoved, a fixed window cannot fall at all
# without having been cleared, so only a noise floor is needed; with the
# deadline moved but not visibly jumped, a fall this deep is required, since a
# sliding window's gradual ageing also moves its deadline.
# ponytail: fixed thresholds, not config keys — promote them only if a
# provider turns out to need different bounds.
_RESET_MIN_FALL_PCT = 10
_RESET_DROP_PCT = 50


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    """One provider/profile/window's usage as of this collection."""

    used_pct: int
    reset_time: int
    # True when reset_time was inferred from a stale cached reading rather
    # than reported by the provider — the reset itself is certain, the next
    # deadline is not (see _collect_agy_cached).
    estimated: bool = False
    # The account's plan as the provider reports it, e.g. "pro", "team · 5x".
    # A change here rescales every percentage without resetting anything, so
    # detect() needs it to tell an upgrade from a cleared counter. None when
    # the provider does not report one (agy's cache carries no plan).
    plan: str | None = None


# provider key -> (profile name -> window key -> WindowSnapshot), or None when
# that provider's collect() call failed.
ProfileWindows = dict[str, WindowSnapshot]
ProviderSnapshot = dict[str, ProfileWindows]
Snapshot = dict[str, ProviderSnapshot | None]

# "<provider>/<profile>/<window>" -> {"reset_time": int, "used_pct": int,
# "seen_at": int, "plan": str | None}. seen_at is when that reading was taken
# and plan the account's tier then; a state file from before either existed
# simply lacks the key, and gains it on the next write.
State = dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ResetEvent:
    """One window that was just observed to have reset."""

    provider: str
    profile: str
    window: str
    used_pct: int  # usage right before the reset — what the notification shows
    reset_time: int  # the fresh window's next reset instant
    estimated: bool = False  # next reset inferred, not reported (see WindowSnapshot)
    # When the reset happened, so a late-noticed one (timer down, collection
    # failing, a stale agy cache) says how old it is instead of reading as
    # news. On schedule it is the old deadline itself; off schedule only the
    # previous reading bounds it, so reset_at_bound marks it "within the last
    # …". None when unknown (a state entry from before seen_at existed).
    reset_at: int | None = None
    reset_at_bound: bool = False


# provider key -> profile name -> unix expiry of each unused reset voucher
# (RESETS EXPIRE), soonest first — from any provider whose `list --json` rows
# carry `reset_vouchers`, so a new provider is covered by emitting that key.
Expiries = dict[str, dict[str, tuple[int, ...]]]


@dataclass(frozen=True, slots=True)
class ExpiryEvent:
    """Reset vouchers on one profile that expire together, inside the reminder window."""

    provider: str
    profile: str
    expires_at: int
    count: int


# ── state file ───────────────────────────────────────────────────────────────


def _config_dir() -> Path:
    override = os.environ.get(_CONFIG_ENV)
    return (Path(override) if override else Path.home() / ".ai-accounts" / "config.json").parent


def state_path() -> Path:
    """Path to this feature's own de-duplication state, beside the config file."""
    return _config_dir() / "quota-reset-state.json"


def expiry_state_path() -> Path:
    """Reset vouchers already reminded about, ``{"<provider>/<profile>/<expiry>": expiry}``.
    Its own file: :func:`detect` drops every key of a collected provider it did not see."""
    return _config_dir() / "reset-expiry-state.json"


def _read_state(path: Path | None = None) -> State:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: State, path: Path | None = None) -> None:
    try:
        u.atomic_write_json(path or state_path(), state)
    except OSError as exc:
        u.log_red(f"Could not record quota-reset state: {exc}")


# ── collection ───────────────────────────────────────────────────────────────


def _next_boundary(reset_time: int, window_minutes: int | None, now: int) -> int | None:
    """The next future end of a window whose recorded deadline already passed.

    Returns None when the window length is unknown, since there is then
    nothing to roll forward by.
    """
    if window_minutes is None or window_minutes <= 0:
        return None
    length = window_minutes * 60
    return reset_time + ((now - reset_time) // length + 1) * length


def _collect_agy_cached() -> ProviderSnapshot | None:
    """agy's windows from its local usage cache — the one provider that cannot
    be read through `list --json`.

    agy's list activates each profile through the shared CLI credential slot to
    query it, so polling it on a timer would perturb whichever account is
    currently live. ``gemini_accounts.cached_usage_windows`` reads only local
    files instead.

    A cached deadline that has passed is treated as a reset that really
    happened — the same one-directional-ageing inference
    ``gemini_accounts._cached_used_pct`` already makes (and autoswitch already
    trusts to pick accounts): nothing refreshed the cache to confirm it, but a
    quota window does not skip its own reset. The synthesised next deadline is
    the window boundary after ``now``, which is stable across ticks, so
    :func:`detect` fires once for that reset and then goes quiet: the reading
    it stores is 0% used, which the ``min_used_pct`` gate then blocks from
    ever re-firing until a real probe refreshes the cache.
    """
    try:
        from . import gemini_accounts
    except ImportError as exc:
        # Unlike a probe failure this is a code defect, and it would repeat on
        # every tick forever — say so once instead of reading as "nothing new".
        u.log_red(f"Could not read agy's usage cache: {exc}")
        return None
    try:
        cached = gemini_accounts.cached_usage_windows()
    except Exception:
        return None
    now = int(time.time())
    profiles: ProviderSnapshot = {}
    for name, windows in cached.items():
        snapshots: ProfileWindows = {}
        for key, window in windows.items():
            if window.reset_time is None:
                continue
            if now < window.reset_time:
                snapshots[key] = WindowSnapshot(window.percentage, window.reset_time)
                continue
            boundary = _next_boundary(window.reset_time, window.window_minutes, now)
            if boundary is not None:
                snapshots[key] = WindowSnapshot(0, boundary, estimated=True)
        profiles[name] = snapshots
    return profiles


def _voucher_expiries(entry: dict[str, Any]) -> tuple[int, ...]:
    """Unix expiries from one `list --json` row's ``reset_vouchers`` (see
    ``usage_format.reset_vouchers_json``); empty when it carries none."""
    vouchers = entry.get("reset_vouchers")
    items = vouchers.get("vouchers") if isinstance(vouchers, dict) else None
    if not isinstance(items, list):
        return ()
    stamps = (item.get("expires_at_unix") for item in items if isinstance(item, dict))
    return tuple(sorted(ts for ts in stamps if isinstance(ts, int)))


def _collect_one(provider: Provider, expiries: Expiries | None = None) -> ProviderSnapshot | None:
    """*provider*'s `list --json`, parsed into a :data:`ProviderSnapshot`.

    Given *expiries*, also records each profile's reset-voucher expiries into
    ``expiries[provider.key]`` from the same call — no second subprocess.

    Every failure (timeout, non-zero exit, bad JSON, an unexpected shape)
    returns None instead of raising — one bad provider must never abort the
    whole :func:`collect` call.
    """
    if provider.reset_windows_cached:
        return _collect_agy_cached()
    env = dict(os.environ)
    if expiries is None:
        # Windows only; skip the Codex voucher-expiry GET.
        env[usage_format.SKIP_RESET_EXPIRY_ENV] = "1"
    try:
        result = u.run(
            [sys.executable, "-m", provider.module, "list", "--json"],
            capture_output=True,
            timeout=_LIST_TIMEOUT_SEC,
            env=env,
        )
        if result.returncode != 0:
            return None
        entries = json.loads(result.stdout)
    except Exception:
        return None
    if not isinstance(entries, list):
        return None
    profiles: ProviderSnapshot = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("no_quota_api"):
            continue
        name = entry.get("name")
        usage = entry.get("usage")
        if not isinstance(name, str) or not isinstance(usage, dict):
            continue
        plan = entry.get("plan")
        if not isinstance(plan, str) or not plan:
            plan = None
        windows: ProfileWindows = {}
        for window in provider.reset_windows:
            raw = usage.get(window)
            if not isinstance(raw, dict):
                continue
            reset_time, percent = raw.get("reset_time"), raw.get("percent")
            if isinstance(reset_time, int) and isinstance(percent, int):
                windows[window] = WindowSnapshot(
                    used_pct=percent, reset_time=reset_time, plan=plan
                )
        profiles[name] = windows
        if expiries is not None and (stamps := _voucher_expiries(entry)):
            # Each worker thread writes only its own provider's key.
            expiries.setdefault(provider.key, {})[name] = stamps
    return profiles


def _collect_all(expiries: Expiries | None = None) -> Snapshot:
    watched = [p for p in PROVIDERS if p.reset_windows]
    with ThreadPoolExecutor(max_workers=max(1, len(watched))) as pool:
        results = list(pool.map(lambda p: _collect_one(p, expiries), watched))
    return {provider.key: result for provider, result in zip(watched, results)}


def collect() -> Snapshot:
    """A fresh usage snapshot for every quota-watched provider, in parallel."""
    return _collect_all()


# ── detection (pure) ─────────────────────────────────────────────────────────


def _reset_early(prev: dict[str, int], fresh: WindowSnapshot, now: int) -> bool:
    """Did this window reset before its recorded deadline arrived?

    Usage falling is the tell; what the deadline did says how much of a fall
    is proof. Providers differ here — codex restarts the weekly window from
    the reset moment (its deadline jumps a week ahead), claude clears both
    windows but keeps the weekly deadline where it was — so all three shapes
    are handled:

    * the deadline **jumped** — moved forward by more than the time that
      passed since the previous reading (plus 60s jitter). A derived, sliding
      ``reset_time`` advances exactly as fast as the clock; a freshly issued
      window lands hours further out. Any fall counts here, however far
      usage has climbed back since — the scan may land well after the reset.
    * the deadline **stayed put** (within jitter) — a fixed window cannot
      fall at all without having been cleared, so any fall beyond the noise
      floor ``_RESET_MIN_FALL_PCT`` is a reset, whatever the reading is now.
    * the deadline **moved but did not jump** — the shape a sliding window's
      gradual ageing takes, so only a deep fall (``_RESET_DROP_PCT``) counts.

    A **plan change** is excluded up front: it rescales every percentage
    without resetting anything, so it mimics a cleared counter and only the
    reported plan distinguishes them.

    ``seen_at`` is absent from a state file written before it existed; the
    jump test is simply skipped for that one tick.
    """
    prev_plan = prev.get("plan")
    if prev_plan is not None and fresh.plan is not None and prev_plan != fresh.plan:
        # An upgrade (1x to 5x, say) leaves the same absolute usage against a
        # bigger allowance, so the percentage falls with the window and its
        # deadline untouched — indistinguishable from a counter cleared in
        # place, except by the plan itself. Re-baseline, do not notify.
        return False
    fall = prev["used_pct"] - fresh.used_pct
    if fall <= 0:
        return False
    seen_at = prev.get("seen_at")
    moved = fresh.reset_time - prev["reset_time"]
    if seen_at is not None and moved > (now - seen_at) + 60:
        return True
    if abs(moved) < 60:
        return fall >= _RESET_MIN_FALL_PCT
    return fall >= _RESET_DROP_PCT


def detect(
    state: State, snapshot: Snapshot, now: int, min_used_pct: int
) -> tuple[State, list[ResetEvent]]:
    """Compare *snapshot* against *state*; return the updated state and any
    :class:`ResetEvent`\\ s. No I/O — same inputs always give the same output.

    A window fires once when the usage just before it reset was worth telling
    the user about (``prev.used_pct >= min_used_pct``) AND the fresh reading
    confirms a NEW reset actually happened, by either of two routes:

    * **on schedule** — ``now >= prev.reset_time`` and ``fresh.reset_time >=
      prev.reset_time + 60`` (a 60s jitter tolerance against a provider's
      clock not lining up exactly with wall time);
    * **off schedule** — the deadline has NOT arrived, but usage fell and
      something corroborates a reset: see :func:`_reset_early`. A provider
      handing out quota early never trips the scheduled route, since its
      recorded deadline is still in the future.

    Collection is a periodic scan, so the fresh reading is whatever the window
    happened to be at when the tick landed — usage may already be climbing
    again. Neither route requires a near-0% reading for that reason: the
    scheduled route ignores ``fresh.used_pct`` entirely, and the off-schedule
    route asks how far usage fell, not how low it landed.

    Both routes share the ``min_used_pct`` gate, so a window sitting at 0%
    stays silent whichever way its ``reset_time`` moves — which is what keeps
    a provider's sliding ``reset_at`` on a freshly reset window from firing.

    A provider whose :func:`collect` failed (``snapshot[provider] is None``)
    has its state left untouched. A profile/window present in *state* but
    absent from the fresh snapshot for a provider that DID collect
    successfully has its key dropped.
    """
    new_state: State = dict(state)
    events: list[ResetEvent] = []
    for provider, profiles in snapshot.items():
        if profiles is None:
            continue  # collect() failed — preserve this provider's state as-is
        fresh_keys: set[str] = set()
        for profile, windows in profiles.items():
            for window, fresh in windows.items():
                key = f"{provider}/{profile}/{window}"
                fresh_keys.add(key)
                prev = state.get(key)
                # (reset_at, reset_at_bound) once a route fires, else None.
                fired: tuple[int | None, bool] | None = None
                if prev is not None and prev["used_pct"] >= min_used_pct:
                    # the recorded deadline arrived and a later one replaced it
                    if now >= prev["reset_time"] and fresh.reset_time >= prev["reset_time"] + 60:
                        fired = (prev["reset_time"], False)
                    # or the provider reset early, off its own schedule
                    elif _reset_early(prev, fresh, now):
                        fired = (prev.get("seen_at"), True)
                if fired is not None:
                    events.append(
                        ResetEvent(
                            provider,
                            profile,
                            window,
                            prev["used_pct"],
                            fresh.reset_time,
                            fresh.estimated,
                            *fired,
                        )
                    )
                new_state[key] = {
                    "reset_time": fresh.reset_time,
                    "used_pct": fresh.used_pct,
                    "seen_at": now,
                    "plan": fresh.plan,
                }
        prefix = f"{provider}/"
        for key in [k for k in new_state if k.startswith(prefix) and k not in fresh_keys]:
            del new_state[key]
    return new_state, events


def expiring_vouchers(
    expiries: Expiries, reminded: dict[str, int], now: int, within_sec: int
) -> tuple[dict[str, int], list[ExpiryEvent]]:
    """Reset vouchers expiring within *within_sec* of *now* not yet in *reminded*.

    Returns ``(new_reminded, events)``: each voucher expiry is reminded about
    once, keyed ``"<provider>/<profile>/<expiry>"``. A reminder is retired only
    once its expiry passes — not when a provider fails to collect — so a
    flaky tick cannot re-arm it.
    """
    new_reminded = {k: v for k, v in reminded.items() if isinstance(v, int) and v > now}
    events: list[ExpiryEvent] = []
    for provider, profiles in expiries.items():
        for profile, stamps in profiles.items():
            for expires_at, count in sorted(Counter(stamps).items()):
                key = f"{provider}/{profile}/{expires_at}"
                if now < expires_at <= now + within_sec and key not in new_reminded:
                    events.append(ExpiryEvent(provider, profile, expires_at, count))
                    new_reminded[key] = expires_at
    return new_reminded, events


# ── notification ─────────────────────────────────────────────────────────────


def _elapsed(seconds: int) -> str:
    """*seconds* as a localised "2d 9h" / "3h 12m"."""
    return usage_format.format_elapsed(
        seconds, unit=lambda n, u: i18n.t(f"elapsed.{u}", n=n)
    )


def _when(event: ResetEvent, now: int) -> str:
    """The "reset 2d 9h ago, " prefix for *event*, or "" when the age is
    unknown or under a minute — a reset that fresh is just news, no label."""
    if event.reset_at is None or now - event.reset_at < 60:
        return ""
    key = "notify.reset.within" if event.reset_at_bound else "notify.reset.ago"
    return i18n.t(key, ago=_elapsed(now - event.reset_at))


def report(events: list[ResetEvent], now: int | None = None) -> bool:
    """One notification for *events* — a single line for one event, the
    grouped `.many` wording for several — via ``aw.notify()``.

    Each event says how long ago it reset (as of *now*, default the current
    time): a reset only noticed days later is still reported, but reads as
    old news rather than as quota that just came back.

    Never ``notify_once``: a reset is a one-shot fact already de-duplicated by
    :func:`detect`'s state machine (it cannot fire twice for the same reset),
    so a second suppression layer here would only hide a real bug.
    """
    if not events:
        return False
    if now is None:
        now = int(time.time())
    if len(events) == 1:
        event = events[0]
        title = i18n.t(
            "notify.reset.title",
            provider=event.provider,
            profile=event.profile,
            window=i18n.t(f"window.{event.window}", default=event.window),
            used=event.used_pct,
            when=_when(event, now),
        )
        body = (
            i18n.t("notify.reset.cached")
            if event.estimated
            else i18n.t(
                "notify.reset.body",
                next=usage_format.format_unix_time_compact(event.reset_time),
            )
        )
    else:
        title = i18n.t(
            "notify.reset.many.title",
            count=len(events),
            providers=", ".join(
                f"{name} ×{n}"
                for name, n in Counter(event.provider for event in events).items()
            ),
        )
        body = "\n".join(
            i18n.t(
                "notify.reset.line",
                provider=event.provider,
                profile=event.profile,
                window=i18n.t(f"window.{event.window}", default=event.window),
                used=event.used_pct,
                when=_when(event, now),
            )
            for event in events
        )
        if any(event.estimated for event in events):
            body = f"{body}\n{i18n.t('notify.reset.cached')}"
    return aw.notify(title, body)


def report_expiring(events: list[ExpiryEvent], now: int) -> bool:
    """One notification for *events* via ``aw.notify()`` — a single line for
    one, the grouped `.many` wording for several. Already de-duplicated by
    :func:`expiring_vouchers`, so never ``notify_once``."""
    if not events:
        return False

    def fields(event: ExpiryEvent) -> dict[str, Any]:
        return {
            "provider": event.provider,
            "profile": event.profile,
            "count": event.count,
            "left": _elapsed(event.expires_at - now),
            "at": usage_format.format_unix_time_compact(event.expires_at),
        }

    if len(events) == 1:
        args = fields(events[0])
        return aw.notify(i18n.t("notify.voucher.title", **args), i18n.t("notify.voucher.body", **args))
    title = i18n.t("notify.voucher.many.title", count=sum(event.count for event in events))
    body = "\n".join(i18n.t("notify.voucher.line", **fields(event)) for event in events)
    return aw.notify(title, body)


# ── one tick ─────────────────────────────────────────────────────────────────


def _int_setting(cfg: dict[str, Any], key: str, default: int) -> int:
    """*cfg[key]* as an int, *default* if a hand-edited config holds junk there."""
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _min_used_pct(cfg: dict[str, Any]) -> int:
    return _int_setting(cfg, "reset_notify_min_used_pct", _DEFAULT_MIN_USED_PCT)


def reset_due(now: int, grace_sec: int) -> bool:
    """True when a window worth notifying about passed its recorded deadline
    within the last *grace_sec* — reads only the state file, no provider call.

    Lets the timer collect right after a scheduled reset instead of waiting
    for its next full tick. Capped by *grace_sec* so a provider that keeps
    failing, or has not rolled its window yet, falls back to the full-tick
    cadence instead of being polled every few minutes forever.
    """
    min_used_pct = _min_used_pct(aw.load_config())
    for entry in _read_state().values():
        try:
            if entry["used_pct"] >= min_used_pct and 0 <= now - entry["reset_time"] < grace_sec:
                return True
        except (KeyError, TypeError):
            continue  # a hand-edited or malformed entry is not a reason to collect
    return False


def run_tick(
    collect: Callable[[], Snapshot] | None = None, *, full: bool = True
) -> list[ResetEvent]:
    """Collect, detect, persist state, and notify — one scheduled-job tick.

    Two features share the one collection: quota-reset notifications
    (``reset_notify``) and reset-voucher expiry reminders
    (``reset_expiry_notify``). The latter runs on *full* ticks only, since it
    costs Codex an extra GET per account holding vouchers.

    *collect* lets a test inject a fake snapshot in place of real provider
    subprocesses, the same shape as ``autoswitch_timer.run_once``'s
    check/refresh injection; it carries no voucher expiries.
    """
    cfg = aw.load_config()
    # fail closed, same rule as autoswitch.config_flag
    resets_on = cfg.get("reset_notify") is True
    expiry_on = full and cfg.get("reset_expiry_notify") is True
    if not (resets_on or expiry_on):
        return []
    expiries: Expiries | None = {} if expiry_on else None
    snapshot = collect() if collect else _collect_all(expiries)
    now = int(time.time())
    events: list[ResetEvent] = []
    if resets_on:
        new_state, events = detect(_read_state(), snapshot, now, _min_used_pct(cfg))
        _write_state(new_state)
        if events:
            report(events, now)
    if expiries is not None:
        hours = _int_setting(cfg, "reset_expiry_notify_hours", _DEFAULT_EXPIRY_HOURS)
        reminded, expiring = expiring_vouchers(
            expiries, _read_state(expiry_state_path()), now, hours * 3600
        )
        # Recorded only once sent: a dead channel retries next full tick.
        if expiring and report_expiring(expiring, now):
            _write_state(reminded, expiry_state_path())
    return events
