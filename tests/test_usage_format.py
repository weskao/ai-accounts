from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from ai_accounts import usage_format as uf


class TestPrintNoActiveAccount(unittest.TestCase):
    def test_includes_provider_name_and_command_hints(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            uf.print_no_active_account("Codex", "codex-accounts")
        output = stderr.getvalue()
        self.assertIn("No active Codex account detected.", output)
        self.assertIn("codex-accounts save <name>", output)
        self.assertIn("codex-accounts switch <name>", output)


class TestAlignUsageCells(unittest.TestCase):
    def test_right_aligns_percent_and_entitlement_fraction(self) -> None:
        rows = [
            {"usage_premium": "24% · 18d 15h 11m · 47/200 AIC"},
            {"usage_premium": "5% · 18d 15h 11m · 9/200 AIC"},
        ]
        uf.align_usage_cells(rows, "usage_premium")
        self.assertEqual(rows[0]["usage_premium"], "24% · 18d 15h 11m · 47/200 AIC")
        self.assertEqual(rows[1]["usage_premium"], " 5% · 18d 15h 11m ·  9/200 AIC")

    def test_leaves_cells_without_fraction_untouched_by_the_fraction_logic(self) -> None:
        rows = [
            {"usage_premium": "9% · 3h 1m"},
            {"usage_premium": "10% · 3h 1m"},
        ]
        uf.align_usage_cells(rows, "usage_premium")
        self.assertEqual(rows[0]["usage_premium"], " 9% · 3h 1m")
        self.assertEqual(rows[1]["usage_premium"], "10% · 3h 1m")


class TestAlignNumericCells(unittest.TestCase):
    def test_right_aligns_numbers_to_the_widest(self) -> None:
        rows = [
            {"remaining": "0 AIC"},
            {"remaining": "49 AIC"},
            {"remaining": "152.4 AIC"},
        ]
        uf.align_numeric_cells(rows, "remaining")
        self.assertEqual(rows[0]["remaining"], "    0 AIC")
        self.assertEqual(rows[1]["remaining"], "   49 AIC")
        self.assertEqual(rows[2]["remaining"], "152.4 AIC")

    def test_leaves_non_numeric_cells_alone(self) -> None:
        rows = [
            {"remaining": "unlimited"},
            {"remaining": "—"},
            {"remaining": "7 AIC"},
        ]
        uf.align_numeric_cells(rows, "remaining")
        self.assertEqual(rows[0]["remaining"], "unlimited")
        self.assertEqual(rows[1]["remaining"], "—")
        self.assertEqual(rows[2]["remaining"], "7 AIC")


if __name__ == "__main__":
    unittest.main()
