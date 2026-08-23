"""Per-provider re-login report for a scheduled token-refresh tick.

``ai-accounts timer run`` refreshes every provider on one tick and used to
alert with a single generic line — "a token expired, re-login needed" —
leaving the user to grep the tick's log for WHICH profile of WHICH provider
actually died. This module reads that same combined output back and answers
it: profiles grouped by provider, each with the reason its refresh failed and
the exact re-login command, painted per provider for the terminal and plain
for a notification (Telegram and the OS notifiers render no ANSI).

Text parsing rather than structured returns because each provider's ``refresh
--all`` runs as its own subprocess — :func:`ai_accounts.autoswitch_timer.
_run_token_refresh_everywhere`'s captured output is the only input that exists.
The phrases matched here are the ones that function's ``_REVOKED_MARKERS``
gates on, so a provider that reworded its failure lines must update both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from . import _present
from . import i18n
from ._utils import BLUE, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW

# The provider section header `ai_accounts._header` prints, and the two
# revoked-token wordings every provider's refresh path uses: the per-account
# line (codex/claude/gemini say "revoked/dead", grok says "revoked") and
# codex/claude's end-of-run bulk summary for profiles with no refresh_token to
# even attempt. The hint line that follows a per-account failure carries the
# ready-to-paste command.
_HEADER_RE = re.compile(r"━━━\s+(\S+?)-accounts\s+━━━")
_INLINE_RE = re.compile(r"❌\s*Refresh token revoked(?:/dead)? for (.+?): (.+)$")
_BULK_RE = re.compile(r"❌\s*Revoked \(re-login required\): (.+)$")
_HINT_RE = re.compile(r"Re-login with:\s*(.+)$")
_COMMAND_PROVIDER_RE = re.compile(r"(\S+?)-accounts\b")

UNKNOWN_PROVIDER = "unknown"

# One color per provider so a profile name carries its provider even when the
# eye skips the group heading. Unlisted providers fall back to dim-less plain.
PROVIDER_COLORS = {
    "codex": CYAN,
    "claude": MAGENTA,
    "agy": BLUE,
    "grok": YELLOW,
    "vibe": GREEN,
}


@dataclass(frozen=True, slots=True)
class Revoked:
    """One saved profile whose refresh token can only be fixed by a re-login.

    ``command`` is empty when the provider printed no hint line; use
    :func:`command_for` rather than reading it directly.
    """

    provider: str
    profile: str
    reason: str
    command: str = ""


def parse(output: str) -> list[Revoked]:
    """Every revoked profile named in one refresh tick's *output*.

    Deduplicated per (provider, profile): codex and claude list an
    already-reported profile again in their bulk summary, and the per-account
    line — the one with a real reason — must win. Order of first appearance is
    kept, which is provider order in :data:`ai_accounts.ai_accounts._TOOLS`.
    """
    records: dict[tuple[str, str], Revoked] = {}
    provider = UNKNOWN_PROVIDER
    pending: tuple[str, str] | None = None  # last per-account failure, awaiting its hint

    for raw in output.splitlines():
        line = _present.strip_ansi(raw).strip()

        header = _HEADER_RE.search(line)
        if header:
            provider, pending = header.group(1), None
            continue

        inline = _INLINE_RE.search(line)
        if inline:
            profile, reason = inline.group(1).strip(), inline.group(2).strip()
            pending = (provider, profile)
            records[pending] = Revoked(provider, profile, reason)
            continue

        bulk = _BULK_RE.search(line)
        if bulk:
            for profile in (name.strip() for name in bulk.group(1).split(",")):
                key = (provider, profile)
                if profile and key not in records:
                    records[key] = Revoked(
                        provider,
                        profile,
                        i18n.t(
                            "notify.revoked.reason.missing",
                            default="refresh token missing or rejected",
                        ),
                    )
            pending = None
            continue

        hint = _HINT_RE.search(line)
        if hint and pending is not None:
            records[pending] = replace(records[pending], command=hint.group(1).strip())
            pending = None

    # A caller that captured no section headers (a provider run on its own)
    # can still be attributed: the re-login command names its own tool.
    return [_attributed(record) for record in records.values()]


def _attributed(record: Revoked) -> Revoked:
    if record.provider != UNKNOWN_PROVIDER or not record.command:
        return record
    named = _COMMAND_PROVIDER_RE.match(record.command)
    return replace(record, provider=named.group(1)) if named else record


def command_for(record: Revoked) -> str:
    """The re-login command to run — the provider's own hint when it printed
    one, else the ``login-switch`` form every provider tool accepts."""
    if record.command:
        return record.command
    if record.provider == UNKNOWN_PROVIDER:
        return i18n.t("notify.revoked.body")
    return f"{record.provider}-accounts login-switch {record.profile}"


def providers(records: list[Revoked]) -> list[str]:
    """The provider names in *records*, first-seen order, no repeats."""
    return list(dict.fromkeys(record.provider for record in records))


def _plural(one: str, many: str, count: int) -> str:
    """The message id matching *count* — English needs the agreement, and
    "profile(s)" in a headline reads like an unfilled form field."""
    return one if count == 1 else many


def title(records: list[Revoked]) -> str:
    """One-line headline: how many profiles, and of which providers."""
    return i18n.t(
        _plural(
            "notify.revoked.grouped.one.title",
            "notify.revoked.grouped.many.title",
            len(records),
        ),
        count=len(records),
        providers=", ".join(providers(records)),
    )


def detail_lines(records: list[Revoked], *, color: bool) -> list[str]:
    """Grouped body: one heading per provider, then a row per profile.

    Two lines per profile — ``• <name> — <reason>`` and the indented command —
    so the reason never gets squeezed off the end by a long command, and the
    command stays on a line of its own to copy. Rows are indented under their
    provider heading; a notification is read at a glance and the indent is the
    only thing carrying the grouping once the colors are gone.
    """
    lines: list[str] = []
    for provider in providers(records):
        group = [record for record in records if record.provider == provider]
        accent = PROVIDER_COLORS.get(provider, "") if color else ""
        close = RESET if accent else ""
        dim, dim_close = (DIM, RESET) if color else ("", "")
        lines.append(
            i18n.t(
                _plural("notify.revoked.group.one", "notify.revoked.group.many", len(group)),
                provider=f"{accent}{provider}{close}",
                count=len(group),
            )
        )
        for record in group:
            lines.append(f"  • {accent}{record.profile}{close} — {record.reason}")
            lines.append(f"    ↳ {dim}{command_for(record)}{dim_close}")
    return lines


def message(records: list[Revoked]) -> str:
    """The notification body — plain text, since no notifier renders ANSI."""
    return "\n".join(detail_lines(records, color=False))


def print_report(records: list[Revoked]) -> None:
    """Show the report on the terminal, in the house panel style (red border:
    this is a failure report, not a status one)."""
    _present.panel(title(records), detail_lines(records, color=True), accent=RED)
