"""Shared foundation for auto-switch-on-low-quota.

Holds the on-disk configuration (``~/.ai-accounts/config.json``) and the
notification funnel used when a switch happens — or can't. Selection and
trigger logic lives elsewhere; this module only stores settings and speaks to
the user.

The key list itself is declared once, in :mod:`ai_accounts.config_schema` (type,
default, allowed values, masked-ness, label); ``DEFAULTS`` and
``NOTIFY_CHANNELS`` below are derived from it, so a new setting is added there
and nothing here changes. Import direction is one-way: ``config_schema`` is a
leaf and must never import this module.

``switch_when_used_pct`` is a **USED** percentage, matching
``usage_format.UsageWindow.percentage`` (0-100 used), and the trigger fires
when ``used_pct >= switch_when_used_pct``:

switch_when_used_pct = 90 means: switch once 90% of the quota is USED, i.e. when 10% remains.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import _utils as u
from . import config_schema
from .usage_format import UsageWindow

CONFIG_ENV = "AI_ACCOUNTS_CONFIG_JSON"

NOTIFY_CHANNELS = config_schema.NOTIFY_CHANNELS

DEFAULTS: dict[str, object] = config_schema.defaults()


def config_path() -> Path:
    """Path to the ai-accounts config file (``$AI_ACCOUNTS_CONFIG_JSON`` wins)."""
    override = os.environ.get(CONFIG_ENV)
    return Path(override) if override else Path.home() / ".ai-accounts" / "config.json"


def _read_json(path: Path) -> dict:
    """*path* parsed as a JSON object — ``{}`` when absent, unreadable or junk."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_private(path: Path, text: str) -> None:
    """Write *text* to *path* owner-only and atomically.

    Created 0600 up front rather than chmod'ed afterwards, so a stored secret
    is never briefly readable by other local users, and swapped in with
    ``os.replace`` so a crash mid-write cannot truncate the previous contents.
    Raises ``OSError`` if the write fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with os.fdopen(
            os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_config() -> dict:
    """Stored settings layered over :data:`DEFAULTS` (unknown keys included)."""
    return {**DEFAULTS, **_read_json(config_path())}


def config_flag(key: str, cfg: dict | None = None) -> bool:
    """One boolean setting, failing CLOSED on anything that isn't JSON ``true``.

    ``bool()`` is wrong for every switch in this config: a hand-edited
    ``"enabled": "false"`` is valid JSON and a truthy string, so a bare
    truthiness check runs the feature while the user believes it is off. Only
    a real ``true`` counts — every other shape (a string, a number, junk)
    reads as off. Pass *cfg* to reuse a config the caller already loaded.
    """
    return (load_config() if cfg is None else cfg).get(key) is True


def save_config(updates: dict) -> dict:
    """Merge *updates* into the stored config and rewrite it owner-only.

    Keys already in the file that this module knows nothing about are kept —
    a newer ai-accounts's settings must survive an older one's write. Raises
    ``ValueError`` (before touching the file) on an unknown notify channel.
    """
    stored = {**_read_json(config_path()), **updates}
    channel = stored.get("notify", DEFAULTS["notify"])
    if channel not in NOTIFY_CHANNELS:
        raise ValueError(
            f"invalid notify channel {channel!r}: expected one of "
            + ", ".join(NOTIFY_CHANNELS)
        )
    _write_private(config_path(), json.dumps(stored, indent=2) + "\n")
    return stored


# Masking lives in config_schema (the schema needs it to render a masked field);
# kept under the historical private name for this module's callers.
_mask = config_schema.mask_secret


def masked_config() -> dict:
    """:func:`load_config` with every secret masked — safe to print or log.

    Which keys are secret is read off the schema, not named here: a masked key
    added to ``config_schema.FIELDS`` must not leak on the way out just
    because this function was never updated.
    """
    cfg = load_config()
    for field in config_schema.FIELDS:
        if field.masked:
            cfg[field.key] = _mask(str(cfg.get(field.key, "")))
    return cfg


_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _telegram_notify(title: str, message: str, cfg: dict) -> bool:
    """POST one sendMessage to the Bot API. False on any failure."""
    token = str(cfg.get("telegram_bot_token", ""))
    chat_id = str(cfg.get("telegram_chat_id", ""))
    if not token or not chat_id:
        u.log_red("Telegram notifications need telegram_bot_token and telegram_chat_id")
        return False
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": f"{title}\n{message}"}
    ).encode("utf-8")
    request = urllib.request.Request(
        _TELEGRAM_API.format(token=token), data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return False  # incl. InvalidURL from a hand-corrupted token
    return True


def notify(title: str, message: str) -> bool:
    """Send *title*/*message* over the configured channel.

    Returns True when the message went out. A missing notifier, bad
    credentials or a dead network is reported as False — notification is
    never allowed to take down its caller.
    """
    cfg = load_config()
    channel = cfg.get("notify")
    if channel == "desktop":
        return u.desktop_notify(title, message)
    if channel == "telegram":
        return _telegram_notify(title, message, cfg)
    if channel != "none":
        u.log_red(f"Unknown notify channel {channel!r} in {config_path()} — nothing sent")
    return False


# ── one-shot notifications ───────────────────────────────────────────────────
# "No account to switch to" is true on every poll until the quota resets, so
# the raw notify() would repeat it every minute. State is a {state_key: sent_at}
# map beside the config: a key inside its cooldown stays silent, an unseen key
# (other provider, candidate reappeared) speaks up immediately. Keyed rather
# than a single slot because ai-accounts drives four providers together — two
# can be out of candidates at once, and one must not evict the other.

def state_path() -> Path:
    """Path to the de-duplication state, beside the config file."""
    return config_path().with_name("autoswitch-state.json")


def notify_once(
    state_key: str, title: str, message: str, *, cooldown: float = 3600
) -> bool:
    """:func:`notify`, unless *state_key* was already reported recently.

    Returns True when the message was actually sent. Re-arms when *state_key*
    changes (a candidate reappeared, another provider ran dry) or once
    *cooldown* seconds have passed.
    """
    now = time.time()
    seen = _read_json(state_path())
    last = seen.get(state_key)
    # A key that was never recorded — or recorded as junk — is not suppressed,
    # and neither is a negative age: a backward clock jump (NTP, VM resume)
    # must not mute the notification until wall-clock catches up.
    if isinstance(last, (int, float)) and 0 <= now - last < cooldown:
        return False
    if not notify(title, message):
        return False  # nothing was shown, so nothing to de-duplicate against
    # Expired entries can never suppress anything again, so drop them (junk
    # values included) and the file stays the size of the live state.
    # ponytail: prunes with the caller's cooldown; fine while callers share one.
    fresh = {
        k: v
        for k, v in seen.items()
        if isinstance(v, (int, float)) and 0 <= now - v < cooldown
    }
    fresh[state_key] = now
    try:
        _write_private(state_path(), json.dumps(fresh))
    except OSError as exc:
        # The user has already been told; losing the marker only risks a repeat.
        u.log_red(f"Could not record notification state: {exc}")
    return True


# ── the engine ───────────────────────────────────────────────────────────────


SWITCH_REASONS = (
    "disabled",         # auto-switching is off in the config
    "no_active",        # the provider has no active profile to switch away from
    "unknown",          # the active profile's usage could not be determined
    "below_threshold",  # still has quota — nothing to do
    "no_candidate",     # out of quota, but every alternative is too
    "switched",         # moved to another profile
    "switch_failed",    # a target was chosen but the switch callable refused
)


@dataclass(frozen=True, slots=True)
class SwitchOutcome:
    """What one auto-switch attempt did.

    ``to_profile`` is set only when ``switched`` is True; on a failed switch the
    profile that was attempted is named in ``error`` instead.
    """

    provider: str
    switched: bool
    reason: str
    from_profile: str | None = None
    to_profile: str | None = None
    used_pct: int | None = None
    error: str | None = None
    restarted: bool | None = None


def _used_pct(probe: Callable[[str], UsageWindow | None], name: str) -> int | None:
    """``probe(name)``'s used percentage, or None when it cannot be determined."""
    try:
        window = probe(name)
    except Exception as exc:
        u.log_red(f"usage lookup for {name} failed: {exc}")
        return None
    return None if window is None else window.percentage


def _restarted(restart: Callable[[], bool]) -> bool:
    """Run the caller's restart hook; a hook that fails is False, never a raise."""
    try:
        return bool(restart())
    except Exception as exc:
        u.log_red(f"restart after switch failed: {exc}")
        return False


def run_autoswitch(
    provider: str,
    profiles: list[str],
    active: str | None,
    probe: Callable[[str], UsageWindow | None],
    switch: Callable[[str], bool],
    restart: Callable[[], bool] | None = None,
) -> SwitchOutcome:
    """Move *provider* off *active* when it is out of quota.

    Provider-agnostic: callers pass plain functions and data. *probe* returns a
    profile's usage window, or None when it cannot be determined; *switch*
    activates a profile and returns whether that worked; *restart* — when given
    — runs once after a successful switch (what it does is the caller's call).

    Fires when the active profile's used percentage reaches
    ``switch_when_used_pct``. The target is the candidate with the lowest used
    percentage *strictly below* that threshold, ties broken by profile name;
    the active profile and any candidate without usage data are skipped. A
    dead end goes through :func:`notify_once`, a switch through :func:`notify`.
    Never raises: probe, switch and restart failures come back inside the
    returned :class:`SwitchOutcome`.
    """
    used: int | None = None

    def outcome(
        reason: str,
        *,
        to_profile: str | None = None,
        error: str | None = None,
        restarted: bool | None = None,
    ) -> SwitchOutcome:
        return SwitchOutcome(
            provider=provider,
            switched=reason == "switched",
            reason=reason,
            from_profile=active,
            to_profile=to_profile,
            used_pct=used,
            error=error,
            restarted=restarted,
        )

    cfg = load_config()
    if not config_flag("enabled", cfg):
        return outcome("disabled")
    if active is None:
        return outcome("no_active")
    try:
        threshold = int(cfg["switch_when_used_pct"])
    except (TypeError, ValueError):
        threshold = int(DEFAULTS["switch_when_used_pct"])
    used = _used_pct(probe, active)
    if used is None:
        return outcome("unknown")
    if used < threshold:
        return outcome("below_threshold")
    qualified: list[tuple[int, str]] = []
    for name in profiles:
        if name == active:
            continue
        candidate = _used_pct(probe, name)
        if candidate is not None and candidate < threshold:
            qualified.append((candidate, name))
    if not qualified:
        notify_once(
            f"{provider}:no-candidate",
            f"{provider}: no account to switch to",
            f"{active} is at {used}% used, and no other {provider} account is "
            f"below {threshold}% — or their usage could not be checked.",
        )
        return outcome("no_candidate")
    target = min(qualified)[1]  # (used%, name): least used, ties by name
    failure = f"{provider}: could not switch to {target}"
    try:
        error = None if switch(target) else failure
    except Exception as exc:
        error = f"{failure}: {exc}"
    if error is not None:
        u.log_red(error)
        return outcome("switch_failed", error=error)
    notify(
        f"{provider}: switched account",
        f"{active} was at {used}% used — switched from {active} to {target}.",
    )
    return outcome(
        "switched",
        to_profile=target,
        restarted=None if restart is None else _restarted(restart),
    )


# ── post-switch restart ladder ───────────────────────────────────────────────
# "Seamless switch, else auto-restart, else prompt the user" (the requirement
# this implements). See docs/autoswitch-hot-reload-spike.md — its probes found
# NO provider hot-reloads credentials today, so "seamless" is unreachable for
# all four; it stays in the vocabulary for the day one gains it.

RUNGS = ("seamless", "auto-restart", "manual-restart")

# Plain data, not a config key — see docs/autoswitch-hot-reload-spike.md
# "Summary" table. All four ship auto-restart today (none reached seamless);
# claude's exclusion of its own session and agy's IDE-running downgrade are
# the two conditions from that table's footnotes ¹ ² — the exclusion is the
# caller's job (it owns the injected ``resume`` callable), the IDE downgrade
# is applied here by effective_rung().
#
# grok's entry is ASPIRATIONAL and currently UNREACHABLE: xAI ships no quota
# API, so `grok-accounts autoswitch` prints "autoswitch unsupported for grok:
# no quota API" and returns before the engine — nothing ever asks for grok's
# rung. It stays as the answer for the day a quota API lands (the spike found
# grok the cleanest of the four to restart); it is not shipped behavior today.
# Kept in sync with the doc's grok section, which names the same blocker.
PROVIDER_VERDICTS: dict[str, str] = {
    "codex": "auto-restart",
    "claude": "auto-restart",
    "agy": "auto-restart",
    "grok": "auto-restart",
}


def effective_rung(
    provider: str,
    *,
    interactive: bool = False,
    agy_ide_running: bool = False,
    verdicts: dict[str, str] = PROVIDER_VERDICTS,
) -> str:
    """The rung actually in play for one switch of *provider*.

    Starts from *verdicts* (default :data:`PROVIDER_VERDICTS`); a missing or
    unrecognized entry defaults to ``"manual-restart"`` — the safe rung when a
    provider's verdict is not known. agy is forced to ``"manual-restart"``
    whenever *agy_ide_running* is True, regardless of its table entry — the
    Antigravity IDE rewrites the keyring instantly and has no programmatic
    resume (docs/autoswitch-hot-reload-spike.md, agy section, footnote ²).
    An auto-restart verdict is also downgraded whenever *interactive* is
    False — SAFETY: a background timer poll has no TTY to restart into, and
    must never spawn or restart anything unattended.
    """
    rung = verdicts.get(provider, "manual-restart")
    if rung not in RUNGS:
        rung = "manual-restart"
    if provider == "agy" and agy_ide_running:
        rung = "manual-restart"
    if rung == "auto-restart" and not interactive:
        rung = "manual-restart"
    return rung


def build_restart(
    provider: str,
    resume: Callable[[], bool],
    *,
    interactive: bool = False,
    agy_ide_running: bool = False,
    verdicts: dict[str, str] = PROVIDER_VERDICTS,
) -> Callable[[], bool] | None:
    """The zero-arg ``restart`` hook to pass into :func:`run_autoswitch`, or None.

    *resume* is the caller's own "spawn the resume command" (e.g. ``codex
    resume --last``, ``claude --continue`` excluding its own session — that
    exclusion is the caller's job, since only it knows how to identify
    itself). This factory only decides *whether* resume ever runs, from
    :func:`effective_rung`: seamless never calls it (nothing to spawn —
    the engine's own switch notification is the whole story), manual-restart
    never calls it (nothing safe to spawn), auto-restart calls it — but only
    when *interactive* is True, so an unattended timer poll never spawns
    anything (SAFETY).
    """
    rung = effective_rung(
        provider,
        interactive=interactive,
        agy_ide_running=agy_ide_running,
        verdicts=verdicts,
    )
    return resume if rung == "auto-restart" else None


def manual_restart_message(provider: str, from_profile: str, to_profile: str) -> str:
    """The manual-restart rung's message — explicitly tells the user to restart."""
    return (
        f"{provider}: switched {from_profile} -> {to_profile}; restart your "
        "session to use it."
    )
