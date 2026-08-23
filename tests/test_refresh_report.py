"""The re-login report: what a refresh tick's raw output turns into."""

from __future__ import annotations

import unittest

from ai_accounts import refresh_report as rr
from ai_accounts._utils import CYAN, RESET

# One tick's combined output, in the shape autoswitch_timer captures it:
# a plain provider header per section, then that provider's own wording.
TICK_OUTPUT = """━━━ codex-accounts ━━━
❌ Refresh token revoked/dead for work: HTTP 400 from token endpoint (refresh token may be expired or revoked)
   Re-login with: codex-accounts login-switch work
❌ Revoked (re-login required): work, spare
━━━ claude-accounts ━━━
✅ All 2 profile(s) refreshed.
━━━ agy-accounts ━━━
❌ Refresh token revoked/dead for personal: revoked: refresh token rejected (invalid_grant)
   Re-login with: agy-accounts login-switch personal
━━━ grok-accounts ━━━
⚠️  Direct refresh unavailable for spare (timeout) — using the Grok CLI.
"""


class ParseTests(unittest.TestCase):
    def test_profiles_are_attributed_to_their_provider_section(self) -> None:
        records = rr.parse(TICK_OUTPUT)

        self.assertEqual(
            [(r.provider, r.profile) for r in records],
            [("codex", "work"), ("codex", "spare"), ("agy", "personal")],
        )

    def test_the_inline_reason_wins_over_the_bulk_summary(self) -> None:
        # Given: codex names `work` twice — once with a reason, once in bulk
        work = next(r for r in rr.parse(TICK_OUTPUT) if r.profile == "work")

        # Then: one record, carrying the real reason and the printed command
        self.assertIn("HTTP 400", work.reason)
        self.assertEqual(work.command, "codex-accounts login-switch work")

    def test_a_bulk_only_profile_still_gets_a_reason_and_a_command(self) -> None:
        spare = next(r for r in rr.parse(TICK_OUTPUT) if r.profile == "spare")

        self.assertEqual(spare.reason, "refresh token missing or rejected")
        self.assertEqual(rr.command_for(spare), "codex-accounts login-switch spare")

    def test_transient_failures_are_not_reported_as_revoked(self) -> None:
        # Given: grok's transient wording, and codex's "may be expired or
        # revoked" phrasing on a 5xx — neither is a revocation
        output = (
            "━━━ grok-accounts ━━━\n"
            "⚠️  Direct refresh unavailable for spare (timeout) — using the Grok CLI.\n"
            "❌ Refresh failed for spare: HTTP 503 from token endpoint "
            "(refresh token may be expired or revoked)\n"
        )

        self.assertEqual(rr.parse(output), [])

    def test_grok_wording_without_dead_is_recognized(self) -> None:
        output = (
            "━━━ grok-accounts ━━━\n"
            "❌ Refresh token revoked for spare: revoked: invalid_grant\n"
            "   Re-login with: grok-accounts login-switch spare\n"
        )

        (record,) = rr.parse(output)
        self.assertEqual((record.provider, record.profile), ("grok", "spare"))

    def test_a_provider_run_alone_is_attributed_from_its_hint(self) -> None:
        # Given: output with no section header at all (one provider's own
        # `refresh --all`, or a caller that captured only the failure)
        output = (
            "❌ Refresh token revoked/dead for work: revoked: invalid_grant\n"
            "   Re-login with: claude-accounts login-switch work\n"
        )

        (record,) = rr.parse(output)
        self.assertEqual(record.provider, "claude")

    def test_colored_provider_output_parses(self) -> None:
        # Given: the same failure with ANSI still attached (a provider whose
        # stderr was a terminal)
        output = f"{CYAN}❌ Refresh token revoked/dead for work: dead{RESET}\n"

        (record,) = rr.parse(output)
        self.assertEqual((record.profile, record.reason), ("work", "dead"))

    def test_an_unattributable_revocation_keeps_a_usable_command(self) -> None:
        # Given: a revocation with neither header nor hint
        (record,) = rr.parse("❌ Revoked (re-login required): work\n")

        # Then: the provider is unknown, so the generic instruction stands in
        self.assertEqual(record.provider, rr.UNKNOWN_PROVIDER)
        self.assertIn("login-switch", rr.command_for(record))


class RenderTests(unittest.TestCase):
    def test_the_headline_names_the_count_and_the_provider_types(self) -> None:
        title = rr.title(rr.parse(TICK_OUTPUT))

        self.assertIn("3", title)
        self.assertIn("codex, agy", title)
        self.assertNotIn("\n", title)  # a notification title is one row

    def test_the_body_groups_by_provider_and_lists_name_reason_command(self) -> None:
        body = rr.message(rr.parse(TICK_OUTPUT))

        self.assertEqual(
            body.splitlines(),
            [
                "🔐 codex · 2 profiles",
                "  • work — HTTP 400 from token endpoint (refresh token may be expired or revoked)",
                "    ↳ codex-accounts login-switch work",
                "  • spare — refresh token missing or rejected",
                "    ↳ codex-accounts login-switch spare",
                "🔐 agy · 1 profile",
                "  • personal — revoked: refresh token rejected (invalid_grant)",
                "    ↳ agy-accounts login-switch personal",
            ],
        )

    def test_the_notification_body_carries_no_ansi(self) -> None:
        # Notifiers (Telegram, osascript, notify-send, toast) render none
        self.assertNotIn("\033", rr.message(rr.parse(TICK_OUTPUT)))

    def test_the_terminal_body_paints_each_provider_its_own_color(self) -> None:
        lines = rr.detail_lines(rr.parse(TICK_OUTPUT), color=True)
        codex_row = next(line for line in lines if "work" in line)
        agy_row = next(line for line in lines if "personal" in line)

        self.assertIn(rr.PROVIDER_COLORS["codex"], codex_row)
        self.assertIn(rr.PROVIDER_COLORS["agy"], agy_row)
        self.assertNotEqual(rr.PROVIDER_COLORS["codex"], rr.PROVIDER_COLORS["agy"])


if __name__ == "__main__":
    unittest.main()
