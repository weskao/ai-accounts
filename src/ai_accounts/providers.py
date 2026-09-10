"""The one place every per-provider fact (name, module, color, restart
verdict) is declared — leaf module, no imports from ``ai_accounts``,
``autoswitch`` or ``refresh_report`` (those import *this*, not the reverse).

``ai_accounts.py``'s ``_TOOLS``/``_PROVIDER_COLOR``, ``refresh_report.py``'s
``PROVIDER_COLORS`` and ``autoswitch.py``'s ``PROVIDER_VERDICTS`` are all
derived from :data:`PROVIDERS` below instead of repeating the six providers
as four separate hardcoded dicts that could drift out of sync.

Two independent color fields exist because the two consumers genuinely
disagree for one provider today: ``claude`` renders ORANGE in
``ai_accounts.py``'s stacked command blocks but MAGENTA in
``refresh_report.py``'s re-login report — a real, pre-existing difference,
not an oversight, so it is preserved rather than collapsed to one color.

``verdict`` is ``None`` for a provider that must be absent from
``PROVIDER_VERDICTS`` entirely (``vibe`` — no quota API, no restart-ladder
entry at all; ``copilot`` — same shape, its quota endpoint is unverified;
both fall through to ``effective_rung``'s
``verdicts.get(provider, "manual-restart")`` default). ``grok``'s verdict is
present but aspirational — see ``autoswitch.py``'s ``PROVIDER_VERDICTS``
docstring for why it's currently unreachable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._utils import BLUE, CYAN, GREEN, MAGENTA, ORANGE, RED, YELLOW


@dataclass(frozen=True, slots=True)
class Provider:
    key: str  # short id: "codex", "claude", "agy", "grok", "vibe", "copilot"
    label: str  # display label: "codex-accounts"
    module: str  # importable module: "ai_accounts.codex_accounts"
    binary: str  # CLI binary the login flow shells out to (README's "Platform notes" table)
    cli_color: str  # accent used by ai_accounts.py's stacked command blocks
    report_color: str  # accent used by refresh_report.py's re-login report
    verdict: str | None = None  # restart-ladder verdict; None = no entry at all
    # Quota windows this provider's usage reports reset on, as the JSON window
    # key the quota-reset-notification feature keys its i18n lookup off of
    # (`i18n.t(f"window.{key}", default=key)` — see providers.py's module
    # docstring for why no second display-name dict exists). Empty = the
    # provider has no quota API to watch for a reset at all (agy/grok/vibe).
    reset_windows: tuple[str, ...] = ()
    # True when those windows can only be read from the provider's local usage
    # cache, because querying it live would perturb the shared CLI credential
    # slot — declared here rather than as a provider-key branch in the
    # collector (see quota_reset._collect_one).
    reset_windows_cached: bool = False


PROVIDERS: list[Provider] = [
    Provider(
        "codex", "codex-accounts", "ai_accounts.codex_accounts", "codex", CYAN, CYAN,
        "auto-restart", reset_windows=("hourly", "weekly"),
    ),
    Provider(
        "claude", "claude-accounts", "ai_accounts.claude_accounts", "claude", ORANGE, MAGENTA,
        "auto-restart", reset_windows=("hourly", "weekly"),
    ),
    # Collected from agy's local usage cache, not `list --json` — see
    # quota_reset._collect_agy_cached for why it cannot be polled live.
    Provider(
        "agy", "agy-accounts", "ai_accounts.gemini_accounts", "agy", BLUE, BLUE,
        "auto-restart",
        reset_windows=("gemini_session", "gemini_weekly", "other_session", "other_weekly"),
        reset_windows_cached=True,
    ),
    Provider("grok", "grok-accounts", "ai_accounts.grok_accounts", "grok", YELLOW, YELLOW, "auto-restart"),
    Provider("vibe", "vibe-accounts", "ai_accounts.vibe_accounts", "vibe", GREEN, GREEN, None),
    # No quota API confirmed (copilot_usage.py's endpoint is unverified) and no
    # restart-ladder entry at all — same shape as vibe, not a new pattern.
    Provider(
        "copilot", "copilot-accounts", "ai_accounts.copilot_accounts", "copilot", RED, RED,
        None, reset_windows=("monthly",),
    ),
]


if __name__ == "__main__":
    # ponytail: minimal self-check, not a test suite — the derived-dict
    # equality with the old literals is already covered by the existing
    # test files (test_ai_accounts.py, test_refresh_report.py,
    # test_autoswitch.py), which import the real consumer modules.
    assert [p.key for p in PROVIDERS] == ["codex", "claude", "agy", "grok", "vibe", "copilot"]
    assert {p.key: p.verdict for p in PROVIDERS if p.verdict is not None} == {
        "codex": "auto-restart",
        "claude": "auto-restart",
        "agy": "auto-restart",
        "grok": "auto-restart",
    }
    assert {"vibe", "copilot"}.isdisjoint({p.key for p in PROVIDERS if p.verdict is not None})
    assert {p.key: p.reset_windows for p in PROVIDERS} == {
        "codex": ("hourly", "weekly"),
        "claude": ("hourly", "weekly"),
        "agy": ("gemini_session", "gemini_weekly", "other_session", "other_weekly"),
        "grok": (),
        "vibe": (),
        "copilot": ("monthly",),
    }
    assert {p.key for p in PROVIDERS if p.reset_windows_cached} == {"agy"}
    assert {p.key: p.binary for p in PROVIDERS} == {
        "codex": "codex",
        "claude": "claude",
        "agy": "agy",
        "grok": "grok",
        "vibe": "vibe",
        "copilot": "copilot",
    }
    print("providers.py self-check OK")
