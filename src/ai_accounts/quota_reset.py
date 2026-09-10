"""Quota-reset notifications: tell the user when a provider's usage window
rolled over and full quota is available again (see TODO.md's future-work
item).

Three layers, kept separate so the detection rule can be unit-tested with no
subprocess and no clock: :func:`collect` gathers a fresh usage snapshot per
watched provider (codex/claude/copilot — the only entries in
``providers.PROVIDERS`` with a non-empty ``reset_windows``); :func:`detect` is
a pure function comparing that snapshot against the previous tick's stored
state; :func:`run_tick` wires them together and sends the notification.

Import direction (leaf-ward, like the rest of this package): this module
imports :mod:`providers`, :mod:`i18n`, :mod:`usage_format`, and
:mod:`autoswitch` — but only ``autoswitch.load_config``/``autoswitch.notify``,
never ``notify_once`` or the config-path/state-path helpers. ``autoswitch``
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import _utils as u
from . import autoswitch as aw
from . import i18n
from . import usage_format
from .providers import PROVIDERS, Provider

# Same env override autoswitch.config_path() honors — duplicated (not
# imported) so this module's only autoswitch dependency stays load_config/
# notify, per the import-direction contract above.
_CONFIG_ENV = "AI_ACCOUNTS_CONFIG_JSON"

# config_schema's declared default for reset_notify_min_used_pct — used only
# as a fallback if a hand-edited config has a non-numeric value there.
_DEFAULT_MIN_USED_PCT = 90

# ponytail: fixed per-provider ceiling, matching autoswitch_timer's
# _REFRESH_TIMEOUT_SEC precedent and its rationale verbatim — a wedged
# `list --json` subprocess must not silently kill every future reset-check
# tick. Upgrade path: make configurable if a provider routinely needs longer.
_LIST_TIMEOUT_SEC = 300


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    """One provider/profile/window's usage as of this collection."""

    used_pct: int
    reset_time: int


# provider key -> (profile name -> window key -> WindowSnapshot), or None when
# that provider's collect() call failed.
ProfileWindows = dict[str, WindowSnapshot]
ProviderSnapshot = dict[str, ProfileWindows]
Snapshot = dict[str, ProviderSnapshot | None]

# "<provider>/<profile>/<window>" -> {"reset_time": int, "used_pct": int}
State = dict[str, dict[str, int]]


@dataclass(frozen=True, slots=True)
class ResetEvent:
    """One window that was just observed to have reset."""

    provider: str
    profile: str
    window: str
    used_pct: int  # usage right before the reset — what the notification shows
    reset_time: int  # the fresh window's next reset instant


# ── state file ───────────────────────────────────────────────────────────────


def _config_dir() -> Path:
    override = os.environ.get(_CONFIG_ENV)
    return (Path(override) if override else Path.home() / ".ai-accounts" / "config.json").parent


def state_path() -> Path:
    """Path to this feature's own de-duplication state, beside the config file."""
    return _config_dir() / "quota-reset-state.json"


def _read_state() -> State:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: State) -> None:
    try:
        u.atomic_write_json(state_path(), state)
    except OSError as exc:
        u.log_red(f"Could not record quota-reset state: {exc}")


# ── collection ───────────────────────────────────────────────────────────────


def _collect_one(provider: Provider) -> ProviderSnapshot | None:
    """*provider*'s `list --json`, parsed into a :data:`ProviderSnapshot`.

    Every failure (timeout, non-zero exit, bad JSON, an unexpected shape)
    returns None instead of raising — one bad provider must never abort the
    whole :func:`collect` call.
    """
    try:
        result = u.run(
            [sys.executable, "-m", provider.module, "list", "--json"],
            capture_output=True,
            timeout=_LIST_TIMEOUT_SEC,
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
        windows: ProfileWindows = {}
        for window in provider.reset_windows:
            raw = usage.get(window)
            if not isinstance(raw, dict):
                continue
            reset_time, percent = raw.get("reset_time"), raw.get("percent")
            if isinstance(reset_time, int) and isinstance(percent, int):
                windows[window] = WindowSnapshot(used_pct=percent, reset_time=reset_time)
        profiles[name] = windows
    return profiles


def _collect_all() -> Snapshot:
    watched = [p for p in PROVIDERS if p.reset_windows]
    with ThreadPoolExecutor(max_workers=max(1, len(watched))) as pool:
        results = list(pool.map(_collect_one, watched))
    return {provider.key: result for provider, result in zip(watched, results)}


def collect() -> Snapshot:
    """A fresh usage snapshot for every quota-watched provider, in parallel."""
    return _collect_all()


# ── detection (pure) ─────────────────────────────────────────────────────────


def detect(
    state: State, snapshot: Snapshot, now: int, min_used_pct: int
) -> tuple[State, list[ResetEvent]]:
    """Compare *snapshot* against *state*; return the updated state and any
    :class:`ResetEvent`\\ s. No I/O — same inputs always give the same output.

    A window fires once when: the fresh reading confirms a NEW reset actually
    happened (``now >= prev.reset_time`` and ``fresh.reset_time >=
    prev.reset_time + 60``, a 60s jitter tolerance against a provider's clock
    not lining up exactly with wall time) AND the usage just before it reset
    was worth telling the user about (``prev.used_pct >= min_used_pct``).

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
                if (
                    prev is not None
                    and now >= prev["reset_time"]
                    and fresh.reset_time >= prev["reset_time"] + 60
                    and prev["used_pct"] >= min_used_pct
                ):
                    events.append(
                        ResetEvent(provider, profile, window, prev["used_pct"], fresh.reset_time)
                    )
                new_state[key] = {"reset_time": fresh.reset_time, "used_pct": fresh.used_pct}
        prefix = f"{provider}/"
        for key in [k for k in new_state if k.startswith(prefix) and k not in fresh_keys]:
            del new_state[key]
    return new_state, events


# ── notification ─────────────────────────────────────────────────────────────


def report(events: list[ResetEvent]) -> bool:
    """One notification for *events* — a single line for one event, the
    grouped `.many` wording for several — via ``aw.notify()``.

    Never ``notify_once``: a reset is a one-shot fact already de-duplicated by
    :func:`detect`'s state machine (it cannot fire twice for the same reset),
    so a second suppression layer here would only hide a real bug.
    """
    if not events:
        return False
    source = u.source_device()
    if len(events) == 1:
        event = events[0]
        title = i18n.t(
            "notify.reset.title",
            provider=event.provider,
            profile=event.profile,
            window=i18n.t(f"window.{event.window}", default=event.window),
            used=event.used_pct,
        )
        body = i18n.t(
            "notify.reset.body",
            next=usage_format.format_unix_time_compact(event.reset_time),
        )
    else:
        title = i18n.t(
            "notify.reset.many.title",
            count=len(events),
            providers=", ".join(dict.fromkeys(event.provider for event in events)),
        )
        body = "\n".join(
            i18n.t(
                "notify.reset.line",
                provider=event.provider,
                profile=event.profile,
                window=i18n.t(f"window.{event.window}", default=event.window),
                used=event.used_pct,
            )
            for event in events
        )
    return aw.notify(title, f"{body}\n{source}")


# ── one tick ─────────────────────────────────────────────────────────────────


def run_tick(collect: Callable[[], Snapshot] | None = None) -> list[ResetEvent]:
    """Collect, detect, persist state, and notify — one scheduled-job tick.

    *collect* lets a test inject a fake snapshot in place of real provider
    subprocesses, the same shape as ``autoswitch_timer.run_once``'s
    check/refresh injection.
    """
    cfg = aw.load_config()
    if cfg.get("reset_notify") is not True:  # fail closed, same rule as autoswitch.config_flag
        return []
    try:
        min_used_pct = int(cfg.get("reset_notify_min_used_pct", _DEFAULT_MIN_USED_PCT))
    except (TypeError, ValueError):
        min_used_pct = _DEFAULT_MIN_USED_PCT
    state = _read_state()
    snapshot = (collect or _collect_all)()
    now = int(time.time())
    new_state, events = detect(state, snapshot, now, min_used_pct)
    _write_state(new_state)
    if events:
        report(events)
    return events
