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
import os
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

    def test_missing_account_tool_reports_remediation_command(self) -> None:
        # This is the reported bug's exact shape: a newly declared
        # [project.scripts] entry point (e.g. copilot-accounts) is absent
        # from PATH until the next reinstall, and the raw shell error alone
        # ("command not found") gives the user no path forward. Fake PATH
        # via `have`, never the real environment. The cell (`detail`) stays
        # short -- the command lives in `remediation`, surfaced once in the
        # run-level footer/JSON, not in every failing row's cell.
        with mock.patch.object(doctor, "have", return_value=False):
            check = doctor._check_account_tool(_FAKE_PROVIDER, doctor._repo_root())
        self.assertFalse(check.ok)
        self.assertIn("fake-accounts", check.detail)
        self.assertIn("not found on PATH", check.detail)
        self.assertNotIn("uv tool install", check.detail)
        self.assertIn("uv tool install --editable", check.remediation)
        self.assertIn("--force", check.remediation)

    def test_present_account_tool_is_pass(self) -> None:
        with mock.patch.object(doctor, "have", return_value=True):
            check = doctor._check_account_tool(_FAKE_PROVIDER, doctor._repo_root())
        self.assertTrue(check.ok)
        self.assertIn("fake-accounts", check.detail)
        self.assertIsNone(check.remediation)

    def test_run_checks_flags_stale_editable_checkout_once(self) -> None:
        # When a cheap, best-effort probe can tell the currently-installed
        # tool's editable link points somewhere other than the checkout this
        # doctor run itself is executing from, say so -- "just reinstall"
        # would otherwise mislead a user into reinstalling from the wrong
        # directory (this repo's exact reported scenario: the installed tool
        # tracked the main checkout's src, which had no copilot_accounts.py
        # at all). This is a run-level fact (the probe is the same regardless
        # of which provider failed), so it's computed once, not per provider.
        stale_root = Path("/tmp/some-other-checkout")
        with mock.patch.object(doctor, "have", return_value=False), mock.patch.object(
            doctor, "_installed_editable_root", return_value=stale_root
        ) as mocked_probe:
            rows, _timer, note = doctor._run_checks()
        self.assertTrue(all(not row["account_tool"].ok for row in rows))
        self.assertIsNotNone(note)
        self.assertIn(str(stale_root), note)
        mocked_probe.assert_called_once()

    def test_run_checks_skips_editable_root_probe_when_nothing_failed(self) -> None:
        with mock.patch.object(doctor, "have", return_value=True), mock.patch.object(
            doctor, "_installed_editable_root"
        ) as mocked_probe:
            doctor._run_checks()
        mocked_probe.assert_not_called()

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


class InstalledEditableRootTest(unittest.TestCase):
    """Direct coverage of `_installed_editable_root`'s shebang/.pth parsing --
    the account-tool tests above mock this function away entirely, so its
    actual body (where the CRITICAL non-UTF-8 crash lived) was previously
    untested. Uses a fabricated PATH, never the real environment.
    """

    @staticmethod
    def _write_executable(path: Path, data: bytes) -> None:
        path.write_bytes(data)
        os.chmod(path, 0o755)

    def test_happy_path_resolves_editable_src_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            venv_root = Path(tmp)
            bin_dir = venv_root / "bin"
            bin_dir.mkdir()
            site_packages = venv_root / "lib" / "python3.11" / "site-packages"
            site_packages.mkdir(parents=True)
            src_dir = venv_root / "checkout" / "src"
            package_dir = src_dir / "ai_accounts"
            package_dir.mkdir(parents=True)

            python_bin = bin_dir / "python"
            self._write_executable(
                bin_dir / "ai-accounts",
                f"#!{python_bin}\n# rest of the console-script shim\n".encode(),
            )
            # The .pth line records the *package* dir (`src/ai_accounts`); the
            # helper takes its parent to land on `src` itself.
            (site_packages / "_editable_impl_ai_accounts.pth").write_text(
                f"{package_dir}\n", encoding="utf-8"
            )

            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
                result = doctor._installed_editable_root()
            self.assertEqual(result, src_dir.resolve())

    def test_non_utf8_ai_accounts_script_on_path_returns_none_not_raise(self) -> None:
        # Regression test for the CRITICAL finding: a non-UTF-8 file
        # shadowing "ai-accounts" earlier on PATH (e.g. a Windows console
        # -script .exe launcher) must degrade to "can't tell", never crash
        # the caller (`_check_account_tool`'s caller expects this never to
        # raise, per the module's own "never raises" bar).
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            self._write_executable(bin_dir / "ai-accounts", b"\xff\xfe\x00\x01garbage")
            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
                result = doctor._installed_editable_root()  # must not raise
            self.assertIsNone(result)

    def test_symlink_loop_in_editable_pth_returns_none_not_raise(self) -> None:
        # Regression test for the CRITICAL finding: `Path.resolve()` raises
        # `RuntimeError("Symlink loop from ...")` on a cyclic symlink on
        # Python 3.10-3.12. The old `except (OSError, ValueError, IndexError):`
        # did not catch that RuntimeError -- `_installed_editable_root` must
        # never raise, on any Python version.
        #
        # NOTE on the return value: on Python 3.13+, `Path.resolve()`'s
        # realpath rewrite stopped raising for symlink loops in non-strict
        # mode -- it silently returns the path unresolved instead (verified
        # directly: `Path("<loop>/x").resolve()` under this repo's 3.13/3.14
        # dev venv returns a Path, not an exception). So this function's
        # *return value* for a symlink loop is version-dependent by design of
        # the stdlib, not a bug: `None` on 3.10-3.12 (the RuntimeError is
        # caught here), some non-crashing Path on 3.13+ (resolve() never
        # raised in the first place). The invariant this test guards --  and
        # the only one that holds on every version -- is "never raises";
        # the concrete 3.10 "-> None" behavior is proven separately with a
        # direct `python3.10` repro (see task verification notes).
        with tempfile.TemporaryDirectory() as tmp:
            venv_root = Path(tmp)
            bin_dir = venv_root / "bin"
            bin_dir.mkdir()
            site_packages = venv_root / "lib" / "python3.11" / "site-packages"
            site_packages.mkdir(parents=True)

            # a -> b -> a: a genuine symlink loop.
            loop_a = venv_root / "loop_a"
            loop_b = venv_root / "loop_b"
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)

            python_bin = bin_dir / "python"
            self._write_executable(
                bin_dir / "ai-accounts",
                f"#!{python_bin}\n# rest of the console-script shim\n".encode(),
            )
            # Route the .pth's recorded package dir through the loop so
            # `.resolve()` in `_installed_editable_root` walks into it.
            (site_packages / "_editable_impl_ai_accounts.pth").write_text(
                f"{loop_a / 'ai_accounts'}\n", encoding="utf-8"
            )

            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
                result = doctor._installed_editable_root()  # must not raise
            self.assertTrue(result is None or isinstance(result, Path))

    def test_run_doctor_completes_with_symlink_loop_on_editable_pth(self) -> None:
        # End-to-end version of the symlink-loop regression: `run_doctor`
        # must complete and return 0, not propagate the RuntimeError.
        with tempfile.TemporaryDirectory() as tmp:
            venv_root = Path(tmp)
            bin_dir = venv_root / "bin"
            bin_dir.mkdir()
            site_packages = venv_root / "lib" / "python3.11" / "site-packages"
            site_packages.mkdir(parents=True)

            loop_a = venv_root / "loop_a"
            loop_b = venv_root / "loop_b"
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)

            python_bin = bin_dir / "python"
            self._write_executable(
                bin_dir / "ai-accounts",
                f"#!{python_bin}\n# rest of the console-script shim\n".encode(),
            )
            (site_packages / "_editable_impl_ai_accounts.pth").write_text(
                f"{loop_a / 'ai_accounts'}\n", encoding="utf-8"
            )

            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = doctor.run_doctor(False)  # must not raise
        self.assertEqual(rc, 0)

    def test_empty_ai_accounts_script_on_path_returns_none_not_raise(self) -> None:
        # IndexError guard: `splitlines()[0]` on an empty file.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            self._write_executable(bin_dir / "ai-accounts", b"")
            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
                result = doctor._installed_editable_root()  # must not raise
            self.assertIsNone(result)

    def test_run_doctor_completes_with_hostile_path_on_it(self) -> None:
        # End-to-end version of the CRITICAL regression: the never-raises
        # invariant holds for the whole `run_doctor` call, not just the
        # helper in isolation.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            self._write_executable(bin_dir / "ai-accounts", b"\xff\xfe")
            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = doctor.run_doctor(False)  # must not raise
        self.assertEqual(rc, 0)
        self.assertIn("FAIL", _plain(buf.getvalue()))


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

    def test_table_cell_stays_short_and_remediation_moves_to_footer(self) -> None:
        # Regression test for the table-readability finding: with every
        # provider's account-tool check failing, the table itself must stay
        # narrow (no wrapped remediation command inside a cell) and the full
        # `uv tool install ...` command must appear exactly once, below the
        # table.
        with self._patched(), mock.patch.object(doctor, "have", return_value=False):
            buf = io.StringIO()
            with redirect_stdout(buf):
                doctor.run_doctor(False)
        output = _plain(buf.getvalue())
        lines = output.splitlines()
        table_lines = [ln for ln in lines if ln.strip().startswith(("│", "┌", "├", "└"))]
        self.assertTrue(table_lines)
        self.assertTrue(all(len(ln) < 200 for ln in table_lines))
        self.assertEqual(output.count("uv tool install --editable"), 1)
        self.assertIn("Account tool not on PATH for:", output)
        self.assertNotIn("uv tool install", "\n".join(table_lines))

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

    def test_json_account_tool_carries_remediation_even_though_cell_is_short(self) -> None:
        # Finding #2's JSON requirement: shortening the table cell must not
        # shrink what a scripted consumer gets for a failing `account_tool`.
        with self._patched(), mock.patch.object(doctor, "have", return_value=False):
            buf = io.StringIO()
            with redirect_stdout(buf):
                doctor.run_doctor(True)
        doc = json.loads(buf.getvalue())
        account_tool = doc["providers"]["codex-accounts"]["account_tool"]
        self.assertFalse(account_tool["ok"])
        self.assertIn("not found on PATH", account_tool["detail"])
        self.assertIn("uv tool install --editable", account_tool["remediation"])
        self.assertIn("--force", account_tool["remediation"])
        self.assertIn("account_tool_note", doc)

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
