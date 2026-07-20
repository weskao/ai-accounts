from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import grok_accounts as ga


def _auth(
    email: str = "person@example.test", principal: str = "principal-1"
) -> ga.JsonDict:
    return {
        "https://auth.x.ai::client": {
            "auth_mode": "oidc",
            "email": email,
            "first_name": "Person",
            "principal_id": principal,
            "principal_type": "User",
            "team_id": "team-1",
            "create_time": "2030-01-01T03:04:05Z",
            "expires_at": "2030-01-02T03:04:05Z",
            "coding_data_retention_opt_out": True,
            "refresh_token": "secret-refresh-token",
            "key": "secret-access-token",
        }
    }


class GrokAccountsTests(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory[str] | None = None
    home: Path = Path()
    grok_home: Path = Path()
    account_dir: Path = Path()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.grok_home = self.home / ".grok"
        self.account_dir = self.home / ".ai-accounts" / "grok" / "accounts"
        environment = mock.patch.dict(
            os.environ,
            {
                "GROK_HOME": str(self.grok_home),
                "GROK_ACCOUNT_DIR": str(self.account_dir),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_save_switch_and_sync_manage_real_auth_shape(self) -> None:
        self.assertTrue(ga._write_json(ga._auth_file(), _auth()))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ga.cmd_save("personal"), 0)
        self.assertTrue((self.account_dir / "personal.json").is_file())

        self.assertTrue(
            ga._write_json(ga._auth_file(), _auth("work@example.test", "principal-2"))
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ga.cmd_switch("personal"), 0)
            self.assertEqual(ga.cmd_sync(), 0)

        self.assertEqual(
            ga._claims(ga._read_json(ga._auth_file()))["email"], "person@example.test"
        )
        self.assertEqual(
            (self.account_dir / ".current-profile").read_text(), "personal"
        )

    def test_list_never_prints_tokens(self) -> None:
        self.assertTrue(ga._write_json(self.account_dir / "personal.json", _auth()))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(ga.cmd_list(), 0)
        listing = output.getvalue()
        self.assertIn("person@example.test", listing)
        self.assertIn("principal-1", listing)
        self.assertIn("team-1", listing)
        self.assertIn("OIDC · refresh", listing)
        self.assertIn("opt-out", listing)
        self.assertNotIn("secret-access-token", listing)
        self.assertNotIn("secret-refresh-token", listing)

    def test_refresh_profile_restores_original_auth_and_saves_rotation(self) -> None:
        original = _auth("active@example.test", "active")
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(ga._auth_file(), original))
        self.assertTrue(ga._write_json(profile, _auth()))

        def refresh() -> int:
            rotated = ga._read_json(ga._auth_file())
            assert rotated is not None
            rotated["https://auth.x.ai::client"]["key"] = "rotated-access-token"
            self.assertTrue(ga._write_json(ga._auth_file(), rotated))
            return 0

        with mock.patch.object(ga, "_run_grok_refresh", side_effect=refresh):
            self.assertEqual(ga._refresh_profile(profile), 0)

        self.assertEqual(
            ga._claims(ga._read_json(ga._auth_file()))["email"], "active@example.test"
        )
        rotated = ga._read_json(profile)
        assert rotated is not None
        self.assertEqual(
            rotated["https://auth.x.ai::client"]["key"], "rotated-access-token"
        )


if __name__ == "__main__":
    unittest.main()
