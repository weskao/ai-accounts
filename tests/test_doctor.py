"""Tests for ``ai-accounts doctor``.

Covers: a missing CLI binary (monkeypatched ``have``), well-formed vs.
malformed vs. genuinely-expired saved profiles (temp account dirs, never the
real ``~/.ai-accounts`` store), and that both the pass/fail table and the
``--json`` document reflect these without raising. Placeholder data only —
no real emails, names, or account ids (this repo's CLAUDE.md hard rule).
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import doctor
from ai_accounts._present import _ANSI_RE
from ai_accounts.providers import PROVIDERS, Provider

_FAKE_PROVIDER = Provider(
    "fake", "fake-accounts", "ai_accounts.codex_accounts", "fake-bin", "", ""
)


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


class DoctorScanTest(unittest.TestCase):
    """Unit-level coverage of the offline profile heuristic."""

    def test_well_formed_profile_with_no_expiry_field_is_ok(self) -> None:
        status, _ = doctor._check_profile_file(self._write({"api_key": "sk-placeholder"}))
        self.assertEqual(status, "ok")

    def test_malformed_json_is_reported_not_raised(self) -> None:
        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text("{not valid json", encoding="utf-8")
        status, _ = doctor._check_profile_file(path)
        self.assertEqual(status, "malformed")

    def test_non_object_json_is_malformed(self) -> None:
        status, _ = doctor._check_profile_file(self._write_raw("[1, 2, 3]"))
        self.assertEqual(status, "malformed")

    def test_past_access_token_expiry_with_refresh_token_is_ok(self) -> None:
        # The normal, expected state of a saved snapshot between uses: the
        # provider's own refresh flow renews an access token on demand, so a
        # past access-token expiry alone must never be flagged.
        status, _ = doctor._check_profile_file(
            self._write({"expiresAt": 1700000000, "refreshToken": "placeholder-refresh"})
        )
        self.assertEqual(status, "ok")

    def test_past_access_token_expiry_without_refresh_token_is_expired(self) -> None:
        status, _ = doctor._check_profile_file(self._write({"expiresAt": 1700000000}))
        self.assertEqual(status, "expired")

    def test_past_refresh_expiry_is_expired_even_with_refresh_token(self) -> None:
        status, _ = doctor._check_profile_file(
            self._write(
                {
                    "refreshToken": "placeholder-refresh",
                    "refreshTokenExpiresAt": 1700000000,
                }
            )
        )
        self.assertEqual(status, "expired")

    @staticmethod
    def _write(data: dict) -> Path:
        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    @staticmethod
    def _write_raw(text: str) -> Path:
        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text(text, encoding="utf-8")
        return path


class DoctorChecksTest(unittest.TestCase):
    def test_missing_binary_is_reported_as_fail_not_raised(self) -> None:
        with mock.patch.object(doctor, "have", return_value=False):
            check = doctor._check_binary(_FAKE_PROVIDER)
        self.assertFalse(check.ok)
        self.assertIn("fake-bin", check.detail)

    def test_present_binary_is_pass(self) -> None:
        with mock.patch.object(doctor, "have", return_value=True):
            check = doctor._check_binary(_FAKE_PROVIDER)
        self.assertTrue(check.ok)

    def test_unreachable_store_fails_only_for_a_required_provider(self) -> None:
        with mock.patch.object(doctor, "go_keyring_available", return_value=(False, "no secret-tool")):
            agy = next(p for p in PROVIDERS if p.key == "agy")
            optional = next(p for p in PROVIDERS if p.key == "grok")
            self.assertFalse(doctor._check_credential_store(agy).ok)
            self.assertTrue(doctor._check_credential_store(optional).ok)

    def test_credential_store_probe_exception_is_reported_not_raised(self) -> None:
        with mock.patch.object(doctor, "go_keyring_available", side_effect=RuntimeError("boom")):
            check = doctor._check_credential_store(_FAKE_PROVIDER)
        self.assertFalse(check.ok)
        self.assertIn("boom", check.detail)

    def test_profiles_directory_with_mixed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            account_dir = Path(tmp)
            (account_dir / "good.json").write_text(
                json.dumps({"api_key": "sk-placeholder"}), encoding="utf-8"
            )
            (account_dir / "bad.json").write_text("{not valid", encoding="utf-8")
            (account_dir / "dead.json").write_text(
                json.dumps({"expiresAt": 1700000000}), encoding="utf-8"
            )
            with mock.patch.object(doctor, "_account_dir_for", return_value=account_dir):
                check = doctor._check_profiles(_FAKE_PROVIDER)
        self.assertFalse(check.ok)
        self.assertIn("bad", check.detail)
        self.assertIn("dead", check.detail)

    def test_missing_account_dir_is_ok_no_profiles(self) -> None:
        with mock.patch.object(doctor, "_account_dir_for", return_value=None):
            check = doctor._check_profiles(_FAKE_PROVIDER)
        self.assertTrue(check.ok)

    def test_account_dir_for_resolves_every_real_provider(self) -> None:
        # Guards the private-`_account_dir()` coupling: if a provider module
        # ever renames/removes it, `_account_dir_for`'s try/except would
        # silently degrade that provider to "no saved profiles" instead of
        # failing loudly — this test is what would catch that.
        for provider in PROVIDERS:
            with self.subTest(provider=provider.key):
                self.assertIsInstance(doctor._account_dir_for(provider), Path)

    def test_timer_status_exception_is_reported_not_raised(self) -> None:
        with mock.patch("ai_accounts.autoswitch_timer.status", side_effect=OSError("no crontab")):
            check = doctor._check_timer()
        self.assertFalse(check.ok)
        self.assertIn("no crontab", check.detail)

    def test_timer_status_ok_is_passed_through(self) -> None:
        with mock.patch("ai_accounts.autoswitch_timer.status", return_value="installed"):
            check = doctor._check_timer()
        self.assertTrue(check.ok)
        self.assertEqual(check.detail, "installed")


class DoctorOutputTest(unittest.TestCase):
    """End-to-end: a fresh/empty account setup plus one missing binary,
    exercised through both the table and ``--json`` paths."""

    def _patched(self):
        return mock.patch.object(doctor, "_account_dir_for", return_value=None)

    def test_table_output_never_raises_and_reports_missing_binary(self) -> None:
        with self._patched(), mock.patch.object(
            doctor, "have", side_effect=lambda cmd: cmd != "codex"
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = doctor.run_doctor(False)
        self.assertEqual(rc, 0)
        output = _plain(buf.getvalue())
        self.assertIn("codex", output.lower())
        self.assertIn("FAIL", output)
        self.assertIn("Autoswitch timer", output)

    def test_json_output_is_one_valid_document_keyed_by_provider(self) -> None:
        with self._patched(), mock.patch.object(
            doctor, "have", side_effect=lambda cmd: cmd != "codex"
        ), mock.patch(
            "ai_accounts.autoswitch_timer.status", return_value="not installed"
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = doctor.run_doctor(True)
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(set(doc["providers"]), {p.label for p in PROVIDERS})
        self.assertFalse(doc["providers"]["codex-accounts"]["binary"]["ok"])
        self.assertTrue(doc["providers"]["claude-accounts"]["binary"]["ok"])
        self.assertFalse(doc["providers"]["codex-accounts"]["ok"])
        # "not installed" is a valid, non-error state (the user may simply not
        # have set it up) — the timer check's `ok` means "the probe itself
        # didn't fail", not "is installed".
        self.assertEqual(doc["autoswitch_timer"], {"ok": True, "detail": "not installed"})
        self.assertFalse(doc["ok"])

    def test_json_output_is_a_single_print_call(self) -> None:
        # `--json` must emit exactly one JSON document (T3's merge pattern),
        # never one per provider.
        with self._patched():
            buf = io.StringIO()
            with redirect_stdout(buf):
                doctor.run_doctor(True)
        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        json.loads(lines[0])  # does not raise


if __name__ == "__main__":
    unittest.main()
