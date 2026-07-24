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


if __name__ == "__main__":
    unittest.main()
