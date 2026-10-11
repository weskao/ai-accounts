from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import autoswitch as aw
from ai_accounts import devin_accounts as da
from ai_accounts import devin_usage as du
from ai_accounts import profile_secrets as ps
from ai_accounts._present import _ANSI_RE
from ai_accounts.usage_format import UsageWindow


def _creds(key: str = "key-aaaa-1111-secret") -> dict[str, str]:
    return {
        "windsurf_api_key": key,
        "api_server_url": "https://api.example.test",
        "devin_webapp_host": "app.example.test",
        "devin_api_url": "https://devin.example.test",
    }


def _snapshot(
    daily: int | None = None,
    weekly: int | None = None,
    email: str | None = "person@example.com",
    plan: str | None = "Pro",
) -> du.UsageSnapshot:
    def window(used: int | None, minutes: int) -> UsageWindow | None:
        return None if used is None else UsageWindow(used, None, minutes)

    return du.UsageSnapshot(window(daily, 1440), window(weekly, 10080), plan, email, 1, None)


def _failed(error: str) -> du.UsageSnapshot:
    return du.UsageSnapshot(None, None, None, None, None, error)


class DevinAccountsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.data_dir = self.home / "data" / "devin"
        self.account_dir = self.home / ".ai-accounts" / "devin" / "accounts"
        environment = mock.patch.dict(
            os.environ,
            {"DEVIN_DATA_DIR": str(self.data_dir), "DEVIN_ACCOUNT_DIR": str(self.account_dir)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        # Keep unit tests offline unless a case installs its own snapshot.
        usage = mock.patch.object(du, "fetch_usage", return_value=du.empty_usage())
        usage.start()
        self.addCleanup(usage.stop)

    def _login(self, key: str = "key-aaaa-1111-secret") -> None:
        self.assertTrue(da._write_creds(da._auth_file(), _creds(key)))

    def _save(self, name: str, key: str) -> None:
        self._login(key)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.cmd_save(name), 0)

    # ── credentials.toml ─────────────────────────────────────────────────────

    def test_toml_reader_handles_comments_escapes_and_literal_strings(self) -> None:
        text = (
            "# comment\n"
            'windsurf_api_key = "abc\\"def"  # trailing\n'
            "api_server_url = 'https://x.example.test'\n"
            "[section]\n"
            "count = 3\n"
        )
        self.assertEqual(
            da._parse_toml(text),
            {"windsurf_api_key": 'abc"def', "api_server_url": "https://x.example.test"},
        )

    def test_toml_write_round_trips_and_is_private(self) -> None:
        creds = _creds('quote"key')
        self.assertTrue(da._write_creds(da._auth_file(), creds))
        self.assertEqual(da._read_creds(), creds)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(da._auth_file().stat().st_mode), 0o600)

    def test_write_creds_uses_the_shared_atomic_writer_and_reports_oserror(self) -> None:
        from ai_accounts import _utils

        target = self.data_dir / "plain.toml"
        _utils.atomic_write_text(target, "a = 1\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "a = 1\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        with mock.patch.object(da, "atomic_write_text", side_effect=OSError("disk full")):
            with redirect_stderr(io.StringIO()) as err:
                self.assertFalse(da._write_creds(da._auth_file(), _creds()))
        self.assertIn("disk full", err.getvalue())

    def test_write_creds_lone_surrogate_fails_cleanly_without_temp_leftovers(self) -> None:
        da._auth_file().parent.mkdir(parents=True)
        with redirect_stderr(io.StringIO()):
            self.assertFalse(da._write_creds(da._auth_file(), _creds("k\ud83d")))
        self.assertEqual(list(da._auth_file().parent.iterdir()), [])

    def test_toml_write_round_trips_non_ascii_and_non_bmp(self) -> None:
        creds = _creds("key-\u00e9\u6e2c\U0001f600-secret")
        self.assertTrue(da._write_creds(da._auth_file(), creds))
        text = da._auth_file().read_text(encoding="utf-8")
        self.assertIn("\U0001f600", text)  # raw utf-8, not a \ud83d\ude00 surrogate pair
        self.assertNotIn("\\ud83d", text)
        self.assertEqual(da._read_creds(), creds)

    def test_read_creds_none_without_key(self) -> None:
        self.assertIsNone(da._read_creds())
        da._auth_file().parent.mkdir(parents=True)
        da._auth_file().write_text('api_server_url = "https://x.example.test"\n', encoding="utf-8")
        self.assertIsNone(da._read_creds())

    def test_data_dir_resolution_per_platform(self) -> None:
        with mock.patch.dict(os.environ, {"DEVIN_DATA_DIR": ""}):
            with mock.patch.object(da.sys, "platform", "linux"), mock.patch.dict(
                os.environ, {"XDG_DATA_HOME": "/xdg"}
            ):
                self.assertEqual(da._data_dir(), Path("/xdg") / "devin")
            with mock.patch.object(da.sys, "platform", "win32"), mock.patch.dict(
                os.environ, {"APPDATA": "/appdata"}
            ):
                self.assertEqual(da._data_dir(), Path("/appdata") / "devin")
        self.assertEqual(da._data_dir(), self.data_dir)

    def test_account_dir_honours_override(self) -> None:
        self.assertEqual(da._account_dir(), self.account_dir)

    def test_account_dir_never_migrates_a_folder_out_of_devins_own_data_dir(self) -> None:
        # A stray <data dir>/accounts folder belongs to Devin, not us: the default
        # resolution must leave it (and its contents) exactly where it is.
        theirs = self.data_dir / "accounts"
        theirs.mkdir(parents=True)
        (theirs / "devin-owned.txt").write_text("keep", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "DEVIN_ACCOUNT_DIR"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(Path, "home", return_value=self.home),
        ):
            resolved = da._account_dir()
            self.assertEqual(resolved, self.home / ".ai-accounts" / "devin" / "accounts")
            self._save("personal", "key-aaaa-1111-secret")
        self.assertEqual((theirs / "devin-owned.txt").read_text(encoding="utf-8"), "keep")
        self.assertFalse((theirs / "personal.json").exists())

    # ── save / switch / sync / remove ────────────────────────────────────────

    def test_save_keeps_the_key_out_of_the_profile_file(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        text = (self.account_dir / "personal.json").read_text(encoding="utf-8")
        self.assertNotIn("key-aaaa-1111-secret", text)
        loaded = da._load_profile(self.account_dir / "personal.json")
        assert loaded is not None
        self.assertEqual(loaded["windsurf_api_key"], "key-aaaa-1111-secret")
        self.assertEqual((self.account_dir / ".current-profile").read_text(), "personal")

    def test_save_no_args_names_after_email_else_key_digest(self) -> None:
        self._login("key-aaaa-1111-secret")
        with mock.patch.object(du, "fetch_usage", return_value=_snapshot()):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(da.main(["save"]), 0)
        self.assertTrue((self.account_dir / "person.json").is_file())

        self._login("key-bbbb-2222-secret")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.main(["save"]), 0)
        digest = [p.stem for p in self.account_dir.glob("devin-*.json")]
        self.assertEqual(len(digest), 1)
        self.assertNotIn("key-bbbb", digest[0])

    def test_save_without_login_fails(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(da.cmd_save("x"), 1)
        self.assertIn("No Devin login found", _ANSI_RE.sub("", err.getvalue()))
        self.assertFalse(list(self.account_dir.glob("*.json")))

    def test_profile_name_rejects_control_characters(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(da._profile_file("a\nb"))
        self.assertIn("invalid characters", _ANSI_RE.sub("", err.getvalue()))

    def test_switch_restores_credentials_and_backs_up_live_login(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        self._login("key-bbbb-2222-secret")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.cmd_switch("personal"), 0)
        live = da._read_creds()
        assert live is not None
        self.assertEqual(live["windsurf_api_key"], "key-aaaa-1111-secret")
        self.assertEqual(live["api_server_url"], "https://api.example.test")
        backup = ps.read_backup("devin")
        assert backup is not None
        self.assertIn("key-bbbb-2222-secret", backup)

    def test_switch_writes_top_level_string_keys_and_keeps_the_raw_original_as_backup(self) -> None:
        # Contract: the profile's top-level string keys are written (no allow-list);
        # non-string values and [tables] of the previous file are not carried over,
        # but the raw pre-switch text survives as the one-deep backup.
        self._save("personal", "key-aaaa-1111-secret")
        raw = (
            'windsurf_api_key = "key-bbbb-2222-secret"\n'
            'future_field = "gone-after-switch"\n'
            "count = 3\n"
            "[extra]\n"
            'n = "3"\n'
        )
        da._auth_file().write_text(raw, encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.cmd_switch("personal"), 0)
        self.assertEqual(ps.read_backup("devin"), raw)  # lossless undo
        new_text = da._auth_file().read_text(encoding="utf-8")
        for dropped in ("future_field", "count", "[extra]", "key-bbbb"):
            self.assertNotIn(dropped, new_text)
        self.assertEqual(da._read_creds(), _creds("key-aaaa-1111-secret"))

    def test_switch_writes_a_saved_profiles_extra_string_key(self) -> None:
        creds = {**_creds("key-aaaa-1111-secret"), "future_field": "carried"}
        self.assertTrue(da._save_profile(self.account_dir / "personal.json", creds))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.cmd_switch("personal"), 0)
        live = da._read_creds()
        assert live is not None
        self.assertEqual(live["future_field"], "carried")

    def test_switch_never_writes_profile_metadata_into_credentials_toml(self) -> None:
        self.assertTrue(
            da._save_profile(self.account_dir / "personal.json", _creds("key-aaaa-1111-secret"), "user@example.com")
        )
        meta = da._load_profile_metadata(self.account_dir / "personal.json")
        self.assertEqual(meta["email"], "user@example.com")  # precondition: the profile has both fields
        self.assertIn("key_id", meta)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.cmd_switch("personal"), 0)
        live_text = da._auth_file().read_text(encoding="utf-8")
        self.assertNotIn("email", live_text)
        self.assertNotIn("key_id", live_text)
        self.assertNotIn("user@example.com", live_text)

    def test_toml_reader_stops_at_the_first_table(self) -> None:
        text = 'windsurf_api_key = "top"\n[section]\nshadowed = "inside-table"\n'
        self.assertEqual(da._parse_toml(text), {"windsurf_api_key": "top"})

    def test_toml_reader_drops_non_string_values(self) -> None:
        text = 'windsurf_api_key = "k"\ncount = 3\nflag = true\narr = ["a"]\nname = "kept"\n'
        self.assertEqual(da._parse_toml(text), {"windsurf_api_key": "k", "name": "kept"})

    def test_a_stale_marker_is_not_trusted_without_a_key_id_match(self) -> None:
        self._save("alpha", "key-aaaa-1111-secret")
        self._save("beta", "key-bbbb-2222-secret")
        self._login("key-bbbb-2222-secret")  # live key belongs to beta...
        da._marker_file().write_text("alpha", encoding="utf-8")  # ...but the marker says alpha
        active = da._active_profile()
        assert active is not None
        self.assertEqual(active.stem, "beta")
        # Two profiles sharing the live key: the marker may only pick among key matches.
        self._save("beta-copy", "key-bbbb-2222-secret")
        da._marker_file().write_text("alpha", encoding="utf-8")
        self.assertIsNone(da._active_profile())

    def test_switch_survives_marker_write_failure(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        self._login("key-bbbb-2222-secret")
        da._marker_file().unlink()
        da._marker_file().mkdir()  # a directory in the marker's place: write_text raises OSError
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(da.cmd_switch("personal"), 0)
        self.assertEqual(da._key(da._read_creds()), "key-aaaa-1111-secret")
        self.assertIn("active profile marker", _ANSI_RE.sub("", err.getvalue()))

    def test_switch_missing_profile_fails(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertEqual(da.cmd_switch("nope"), 1)

    def test_sync_copies_rotated_live_login_back_to_the_matching_profile(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        live = _creds("key-aaaa-1111-secret")
        live["api_server_url"] = "https://moved.example.test"
        self.assertTrue(da._write_creds(da._auth_file(), live))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.cmd_sync(), 0)
        saved = da._load_profile(self.account_dir / "personal.json")
        assert saved is not None
        self.assertEqual(saved["api_server_url"], "https://moved.example.test")

    def test_sync_without_matching_profile_fails(self) -> None:
        self._login("key-zzzz-9999-secret")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(da.cmd_sync(), 1)

    def test_remove_deletes_profile_and_marker(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.main(["remove", "personal"]), 0)
        self.assertFalse((self.account_dir / "personal.json").exists())
        self.assertFalse(da._marker_file().exists())

    def test_remove_keeps_marker_naming_another_surviving_profile(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        self._save("work", "key-bbbb-2222-secret")
        self._login("key-unsaved-3333-secret")  # live login matches no profile
        da._marker_file().write_text("personal", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.cmd_remove("work"), 0)
        self.assertEqual(da._marker_file().read_text(encoding="utf-8"), "personal")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.cmd_remove("personal"), 0)
        self.assertFalse(da._marker_file().exists())

    def test_remove_no_args_opens_interactive_picker(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        self._save("work", "key-bbbb-2222-secret")
        with mock.patch("builtins.input", return_value="2"), redirect_stdout(io.StringIO()):
            self.assertEqual(da.main(["remove"]), 0)
        self.assertFalse((self.account_dir / "work.json").exists())
        self.assertTrue((self.account_dir / "personal.json").exists())

    # ── who / list / usage ───────────────────────────────────────────────────

    def test_who_never_prints_the_key(self) -> None:
        self._login()
        out = io.StringIO()
        with mock.patch.object(du, "fetch_usage", return_value=_snapshot()):
            with redirect_stdout(out):
                self.assertEqual(da.main(["who"]), 0)
        text = _ANSI_RE.sub("", out.getvalue())
        self.assertIn("person@example.com", text)
        self.assertNotIn("key-aaaa-1111-secret", text)

    def test_who_without_login_returns_1(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(da.main(["current"]), 1)

    def test_list_table_shows_plan_and_both_windows(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        out = io.StringIO()
        with mock.patch.object(du, "fetch_usage", return_value=_snapshot(40, 70)):
            with redirect_stdout(out):
                self.assertEqual(da.main(["list"]), 0)
        text = _ANSI_RE.sub("", out.getvalue())
        for needle in ("PROFILE", "1D USED", "1W USED", "STATE", "Pro", "40%", "70%", "ACTIVE"):
            self.assertIn(needle, text)
        self.assertNotIn("key-aaaa-1111-secret", text)

    def test_list_marks_other_profile_of_the_active_account_same_acct(self) -> None:
        self._save("work", "key-bbbb-2222-secret")
        self._save("personal", "key-aaaa-1111-secret")
        out = io.StringIO()
        with mock.patch.object(du, "fetch_usage", return_value=_snapshot(40, 70)):
            with redirect_stdout(out):
                self.assertEqual(da.main(["list"]), 0)
        lines = _ANSI_RE.sub("", out.getvalue()).splitlines()
        self.assertIn("ACTIVE", next(line for line in lines if "personal" in line))
        self.assertIn("SAME ACCT", next(line for line in lines if "work" in line))

    def test_list_json_with_quota_and_degraded(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        out = io.StringIO()
        with mock.patch.object(du, "fetch_usage", return_value=_snapshot(40, 70)):
            with redirect_stdout(out):
                self.assertEqual(da.main(["list", "--json"]), 0)
        text = out.getvalue()
        self.assertNotIn("\033[", text)
        (entry,) = json.loads(text)
        self.assertEqual((entry["name"], entry["active"], entry["no_quota_api"]), ("personal", True, False))
        self.assertEqual(entry["usage"]["daily"]["percent"], 40)
        self.assertEqual(entry["usage"]["weekly"]["percent"], 70)
        self.assertEqual(entry["usage"]["plan"], "Pro")
        self.assertEqual(entry["plan"], "Pro")  # top-level, like grok; quota_reset reads it

        out = io.StringIO()
        with mock.patch.object(du, "fetch_usage", return_value=_failed("HTTP 404 from usage endpoint")):
            with redirect_stdout(out):
                self.assertEqual(da.main(["list", "--json"]), 0)
        self.assertEqual(
            json.loads(out.getvalue()),
            [{"name": "personal", "active": True, "usage": None, "no_quota_api": True}],
        )

    def test_usage_shows_only_the_active_profile(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        self._save("work", "key-bbbb-2222-secret")
        self._login("key-aaaa-1111-secret")
        (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(da.main(["usage"]), 0)
        text = _ANSI_RE.sub("", out.getvalue())
        self.assertIn("Current Devin account", text)
        self.assertIn("personal", text)
        self.assertNotIn("work", text)

    def test_usage_json_empty_when_no_active_profile(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        self._login("key-zzzz-9999-secret")
        (self.account_dir / ".current-profile").unlink()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(da.main(["usage", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), [])

    # ── refresh (verify only) ────────────────────────────────────────────────

    def _refresh(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = da.main(["refresh", *args])
        return rc, _ANSI_RE.sub("", out.getvalue()), _ANSI_RE.sub("", err.getvalue())

    def test_refresh_all_reports_valid_and_revoked_without_modifying_anything(self) -> None:
        self._save("personal", "key-aaaa-1111-secret")
        self._save("work", "key-bbbb-2222-secret")
        before = {p.name: p.read_bytes() for p in self.account_dir.glob("*.json")}
        live_before = da._auth_file().read_bytes()

        def fake(token, **_kw):
            if token == "key-bbbb-2222-secret":
                return _failed("HTTP 401 from usage endpoint")
            return _snapshot(10, 10)

        with mock.patch.object(du, "fetch_usage", side_effect=fake):
            rc, out, err = self._refresh("--all")
        self.assertEqual(rc, 1)
        self.assertIn("Token still valid", out)
        self.assertIn("Token revoked for work", err)
        self.assertIn("devin-accounts login-switch work", err)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.account_dir.glob("*.json")})
        self.assertEqual(live_before, da._auth_file().read_bytes())

    def test_refresh_wording_is_caught_by_the_timer_and_report_parsers(self) -> None:
        from ai_accounts import autoswitch_timer as at
        from ai_accounts import refresh_report as rr

        self._save("work", "key-bbbb-2222-secret")
        with mock.patch.object(du, "fetch_usage", return_value=_failed("HTTP 403 from usage endpoint")):
            rc, out, err = self._refresh("work")
        self.assertEqual(rc, 1)
        combined = out + err
        self.assertTrue(any(m in combined.lower() for m in at._REVOKED_MARKERS))
        (record,) = rr.parse(combined)
        self.assertEqual((record.provider, record.profile), ("devin", "work"))

    def test_refresh_network_error_is_not_reported_as_revoked(self) -> None:
        self._save("work", "key-bbbb-2222-secret")
        with mock.patch.object(du, "fetch_usage", return_value=_failed("network error")):
            rc, out, err = self._refresh("work")
        self.assertEqual(rc, 1)
        self.assertNotIn("revoked", (out + err).lower())

    def test_refresh_invalid_response_is_unreachable_not_valid_or_revoked(self) -> None:
        self._save("work", "key-bbbb-2222-secret")
        with mock.patch.object(du, "fetch_usage", return_value=_failed("invalid response")):
            rc, out, err = self._refresh("work")
        self.assertEqual(rc, 1)
        self.assertNotIn("Token still valid", out)
        self.assertNotIn("revoked", (out + err).lower())
        self.assertIn("Could not reach Devin", err)

    def test_refresh_unexpected_quota_shape_still_counts_as_valid(self) -> None:
        self._save("work", "key-bbbb-2222-secret")
        with mock.patch.object(du, "fetch_usage", return_value=_failed("unexpected quota shape")):
            rc, out, _err = self._refresh("work")
        self.assertEqual(rc, 0)
        self.assertIn("Token still valid", out)

    def test_refresh_active_login_and_missing_login(self) -> None:
        rc, _out, err = self._refresh()
        self.assertEqual(rc, 1)
        self.assertIn("No Devin login", err)
        self._login()
        with mock.patch.object(du, "fetch_usage", return_value=_snapshot(1, 1)):
            rc, out, _err = self._refresh()
        self.assertEqual(rc, 0)
        self.assertIn("Token still valid", out)

    # ── login-switch ─────────────────────────────────────────────────────────

    def test_login_switch_runs_logout_then_login_then_saves(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, **_kw):
            calls.append(list(argv))
            if argv[1:] == ["auth", "login"]:
                self._login("key-new-3333-secret")
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(da.shutil, "which", return_value="/usr/bin/devin"),
            mock.patch.object(da.subprocess, "run", side_effect=fake_run),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(da.main(["login-switch", "fresh"]), 0)
        self.assertEqual(
            calls,
            [["/usr/bin/devin", "auth", "logout"], ["/usr/bin/devin", "auth", "login"]],
        )
        loaded = da._load_profile(self.account_dir / "fresh.json")
        assert loaded is not None
        self.assertEqual(loaded["windsurf_api_key"], "key-new-3333-secret")

    def test_login_switch_rejects_invalid_name_before_logout(self) -> None:
        with (
            mock.patch.object(da.shutil, "which", return_value="/usr/bin/devin"),
            mock.patch.object(da.subprocess, "run") as run,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(da.cmd_login_switch("a\nb"), 1)
        run.assert_not_called()

    def test_login_switch_backs_up_raw_live_login_before_logout(self) -> None:
        raw = 'windsurf_api_key = "key-live-4444-secret"\nextra = "x"\n'
        da._auth_file().parent.mkdir(parents=True)
        da._auth_file().write_text(raw, encoding="utf-8")
        with (
            mock.patch.object(da.shutil, "which", return_value="/usr/bin/devin"),
            mock.patch.object(da.subprocess, "run", return_value=mock.Mock(returncode=1)),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(da.cmd_login_switch("fresh"), 1)  # cancelled login
        self.assertEqual(ps.read_backup("devin"), raw)

    def _login_switch_with(self, login_effect) -> int:
        """Run login-switch over a live login; logout deletes it like the real CLI."""
        raw = 'windsurf_api_key = "key-old-9999-secret"\nextra = "x"\n'
        da._auth_file().parent.mkdir(parents=True)
        da._auth_file().write_text(raw, encoding="utf-8")
        self.raw_live = raw

        def fake_run(argv, **_kw):
            if argv[1:] == ["auth", "logout"]:
                da._auth_file().unlink()
                return mock.Mock(returncode=0)
            return login_effect()

        with (
            mock.patch.object(da.shutil, "which", return_value="/usr/bin/devin"),
            mock.patch.object(da.subprocess, "run", side_effect=fake_run),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            return da.cmd_login_switch("fresh")

    def test_login_switch_restores_previous_login_when_login_fails(self) -> None:
        rc = self._login_switch_with(lambda: mock.Mock(returncode=1))
        self.assertEqual(rc, 1)
        self.assertEqual(da._auth_file().read_text(encoding="utf-8"), self.raw_live)

    def test_login_switch_restores_previous_login_on_ctrl_c(self) -> None:
        def interrupt():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self._login_switch_with(interrupt)
        self.assertEqual(da._auth_file().read_text(encoding="utf-8"), self.raw_live)

    def test_login_switch_keeps_new_login_when_it_succeeds(self) -> None:
        def login():
            self._login("key-new-3333-secret")
            return mock.Mock(returncode=0)

        self.assertEqual(self._login_switch_with(login), 0)
        self.assertIn("key-new-3333-secret", da._auth_file().read_text(encoding="utf-8"))

    def test_login_switch_requires_the_cli(self) -> None:
        err = io.StringIO()
        with mock.patch.object(da.shutil, "which", return_value=None), redirect_stderr(err):
            self.assertEqual(da.cmd_login_switch("x"), 1)
        self.assertIn("Devin CLI is required", _ANSI_RE.sub("", err.getvalue()))

    # ── help / dispatch ──────────────────────────────────────────────────────

    def test_help_exits_zero_and_names_every_subcommand(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(da.main(["--help"]), 0)
        text = _ANSI_RE.sub("", out.getvalue())
        for needle in ("devin-accounts save [<name>]", "login-switch", "autoswitch", "DEVIN_DATA_DIR"):
            self.assertIn(needle, text)

    def test_unknown_command_rejected(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(da.main(["frobnicate"]), 1)
        self.assertIn("Unknown command: frobnicate", _ANSI_RE.sub("", err.getvalue()))


class DevinAutoswitchTests(unittest.TestCase):
    """Daily/weekly windows drive the shared autoswitch engine."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.account_dir = home / "accounts"
        env = mock.patch.dict(
            os.environ,
            {
                "DEVIN_DATA_DIR": str(home / "devin"),
                "DEVIN_ACCOUNT_DIR": str(self.account_dir),
                "AI_ACCOUNTS_CONFIG_JSON": str(home / "config.json"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self._config({})

    def _config(self, extra: dict) -> None:
        aw.save_config(
            {"enabled": True, "notify": "none", "switch_when_used_pct": 90, "reset_notify": False, **extra}
        )

    def _setup(self, by_token: dict[str, tuple[int | None, int | None] | None]) -> None:
        """Active `personal` (token-a) plus spare `work` (token-b). The probe may
        only see each profile's OWN key; a None entry is a failed quota read."""
        for name, token in (("personal", "token-a"), ("work", "token-b")):
            creds = _creds(token)
            self.assertTrue(da._save_profile(self.account_dir / f"{name}.json", creds))
            if name == "personal":
                self.assertTrue(da._write_creds(da._auth_file(), creds))
        (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")

        def fake_fetch(token, **_kw):
            used = by_token.get(token or "")
            return _failed("network error") if used is None else _snapshot(*used)

        patcher = mock.patch.object(du, "fetch_usage", side_effect=fake_fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = da.main(["autoswitch"])
        return rc, _ANSI_RE.sub("", out.getvalue())

    def _live_key(self) -> str:
        creds = da._read_creds()
        assert creds is not None
        return creds["windsurf_api_key"]

    def test_moves_off_an_exhausted_active_profile_and_spawns_nothing_unattended(self) -> None:
        self._setup({"token-a": (10, 95), "token-b": (10, 10)})
        with mock.patch.object(da.subprocess, "run") as spawn:
            rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._live_key(), "token-b")
        self.assertIn("personal → work", out)
        spawn.assert_not_called()

    def test_stale_marker_does_not_make_autoswitch_probe_the_wrong_profile(self) -> None:
        # Live key is work's (token-b, exhausted) but the marker still says personal.
        self._setup({"token-a": (10, 10), "token-b": (10, 95)})
        self.assertTrue(da._write_creds(da._auth_file(), _creds("token-b")))
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("work", out)  # the exhausted live profile is the one acted on
        self.assertNotIn("below the switch threshold", out)
        self.assertEqual(self._live_key(), "token-a")  # moved off work onto personal

    def test_stays_put_while_the_active_profile_has_quota(self) -> None:
        self._setup({"token-a": (10, 10), "token-b": (0, 0)})
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._live_key(), "token-a")
        self.assertIn("below the switch threshold", out)

    def test_skips_a_spare_whose_quota_cannot_be_read(self) -> None:
        self._setup({"token-a": (100, 100), "token-b": None})
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(self._live_key(), "token-a")
        self.assertIn("no other", out)

    def test_default_reads_weekly_and_5h_setting_reads_daily(self) -> None:
        # Daily exhausted, weekly fine: the default (weekly) window does not trigger.
        self._setup({"token-a": (99, 10), "token-b": (0, 0)})
        rc, _out = self._run()
        self.assertEqual((rc, self._live_key()), (0, "token-a"))
        # switch_window=5h makes the short (daily) window decide.
        self._config({"switch_window": "5h"})
        rc, _out = self._run()
        self.assertEqual((rc, self._live_key()), (0, "token-b"))

    def test_interactive_autoswitch_resumes_with_devin_continue(self) -> None:
        self._setup({"token-a": (10, 95), "token-b": (10, 10)})
        done = mock.Mock(returncode=0)
        with (
            mock.patch.object(da.sys.stdout, "isatty", return_value=True, create=True),
            mock.patch.object(da.shutil, "which", return_value="/usr/bin/devin"),
            mock.patch.object(da.subprocess, "run", return_value=done) as spawn,
            redirect_stderr(io.StringIO()),
        ):
            rc = da.main(["autoswitch"])
        self.assertEqual(rc, 0)
        spawn.assert_called_once_with(["/usr/bin/devin", "--continue"])


if __name__ == "__main__":
    unittest.main()
