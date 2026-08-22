from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import autoswitch as aw
from ai_accounts import grok_accounts as ga
from ai_accounts._present import _ANSI_RE


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

    def test_switch_keeps_backup_in_ai_accounts_store(self) -> None:
        self.assertTrue(ga._write_json(ga._auth_file(), _auth("old@example.test", "old")))
        self.assertTrue(ga._write_json(self.account_dir / "personal.json", _auth()))

        with redirect_stdout(io.StringIO()):
            self.assertEqual(ga.cmd_switch("personal"), 0)

        self.assertTrue(list((self.account_dir.parent / "backups").glob("auth.backup-*.json")))
        self.assertFalse(list(self.grok_home.glob("auth.backup-*.json")))

    def test_usage_shows_only_active_profile(self) -> None:
        self.assertTrue(ga._write_json(ga._auth_file(), _auth()))
        self.assertTrue(ga._write_json(self.account_dir / "personal.json", _auth()))
        self.assertTrue(
            ga._write_json(
                self.account_dir / "work.json",
                _auth("work@example.test", "principal-2"),
            )
        )
        (self.account_dir / ".current-profile").write_text("personal", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(ga.cmd_list(only_active=True), 0)
        text = _ANSI_RE.sub("", output.getvalue())
        self.assertIn("Current Grok account", text)
        self.assertEqual(text.count("ACTIVE"), 1)
        self.assertIn("personal", text)
        self.assertNotIn("work", text)

    def test_usage_reports_when_no_active_profile(self) -> None:
        self.assertTrue(ga._write_json(self.account_dir / "saved.json", _auth()))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(ga.cmd_list(only_active=True), 0)
        self.assertNotIn("PROFILE", _ANSI_RE.sub("", out.getvalue()))  # no table
        self.assertIn("No active Grok account", _ANSI_RE.sub("", err.getvalue()))

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

    def test_refresh_profile_uses_shared_success_panel(self) -> None:
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _auth()))

        with mock.patch.object(ga, "_run_grok_refresh", return_value=0):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(ga.cmd_refresh("personal"), 0)

        text = _ANSI_RE.sub("", output.getvalue())
        self.assertIn("✅ Refreshed Grok profile: personal", text)
        self.assertIn("Profile: personal", text)
        self.assertNotIn("secret-access-token", text)
        self.assertNotIn("secret-refresh-token", text)

    def test_refresh_all_prints_panels_table_and_summary(self) -> None:
        self.assertTrue(ga._write_json(self.account_dir / "a.json", _auth(principal="a")))
        self.assertTrue(ga._write_json(self.account_dir / "b.json", _auth(principal="b")))

        with mock.patch.object(ga, "_run_grok_refresh", return_value=0):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(ga.cmd_refresh("--all"), 0)

        text = _ANSI_RE.sub("", output.getvalue())
        self.assertIn("✅ Refreshed Grok profile: a", text)
        self.assertIn("✅ Refreshed Grok profile: b", text)
        self.assertIn("Saved Grok profiles", text)
        self.assertIn("✅ All 2 profile(s) refreshed.", text)

    def test_refresh_active_prints_current_claims_panel(self) -> None:
        self.assertTrue(ga._write_json(ga._auth_file(), _auth()))
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _auth()))
        ga._set_marker(profile)

        with mock.patch.object(ga, "_run_grok_refresh", return_value=0):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(ga.cmd_refresh(None), 0)

        text = _ANSI_RE.sub("", output.getvalue())
        self.assertIn("✅ Refreshed active Grok auth.", text)
        self.assertIn("(synced back to profile: personal)", text)
        self.assertIn("Current Auth Claims", text)

    def test_remove_no_args_opens_interactive_picker_and_removes(self) -> None:
        self.assertTrue(ga._write_json(self.account_dir / "personal.json", _auth()))
        self.assertTrue(
            ga._write_json(
                self.account_dir / "work.json",
                _auth("work@example.test", "principal-2"),
            )
        )

        with mock.patch("builtins.input", return_value="2"):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = ga.cmd_remove_interactive()

        text = _ANSI_RE.sub("", out.getvalue())
        self.assertEqual(rc, 0)
        self.assertIn("1) personal", text)
        self.assertIn("2) work", text)
        self.assertFalse((self.account_dir / "work.json").is_file())
        self.assertTrue((self.account_dir / "personal.json").is_file())

    def test_remove_no_args_cancel_leaves_all_profiles_on_disk(self) -> None:
        self.assertTrue(ga._write_json(self.account_dir / "personal.json", _auth()))
        self.assertTrue(
            ga._write_json(
                self.account_dir / "work.json",
                _auth("work@example.test", "principal-2"),
            )
        )

        with mock.patch("builtins.input", side_effect=EOFError):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = ga.cmd_remove_interactive()

        self.assertEqual(rc, 1)
        self.assertTrue((self.account_dir / "personal.json").is_file())
        self.assertTrue((self.account_dir / "work.json").is_file())

    def test_remove_no_args_reports_when_no_saved_profiles(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ga.cmd_remove_interactive()

        self.assertEqual(rc, 1)
        self.assertIn("No saved Grok profiles.", _ANSI_RE.sub("", err.getvalue()))

    def test_main_dispatches_remove_no_args_to_interactive_picker(self) -> None:
        with mock.patch.object(ga, "cmd_remove_interactive", return_value=0) as interactive:
            rc = ga.main(["remove"])

        self.assertEqual(rc, 0)
        interactive.assert_called_once_with()

    def test_save_no_args_derives_name_from_email(self) -> None:
        self.assertTrue(ga._write_json(ga._auth_file(), _auth()))
        with redirect_stdout(io.StringIO()):
            rc = ga.main(["save"])

        self.assertEqual(rc, 0)
        self.assertTrue((self.account_dir / "person.json").is_file())

    def test_save_no_args_fails_on_missing_email_sentinel(self) -> None:
        payload = _auth()
        del payload["https://auth.x.ai::client"]["email"]
        self.assertTrue(ga._write_json(ga._auth_file(), payload))
        self.assertEqual(ga._claims(payload)["email"], "—")

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ga.cmd_save(None)

        self.assertEqual(rc, 1)
        self.assertIn("No valid Grok OAuth login found", _ANSI_RE.sub("", err.getvalue()))
        self.assertFalse(list(self.account_dir.glob("*.json")))
        self.assertFalse((self.account_dir / "_.json").is_file())

    def test_a_misfiled_foreign_credential_is_flagged_malformed(self) -> None:
        # Given: a genuine Grok OAuth record (auth_mode/refresh_token/email present)
        self.assertFalse(ga._claims(_auth())["malformed"])
        # Given: plan.md §2.4's failure mode mirrored the other way — an
        # Antigravity/Google credential dropped into the grok accounts dir by
        # mistake. It parses as JSON but has no key that looks like a Grok record.
        foreign = {"authenticated_user_account": {"access_token": "x", "expiry_date": 1}}
        claims = ga._claims(foreign)
        # Then: reported as malformed, not silently read as a blank/expired account
        self.assertTrue(claims["malformed"])

    def test_help_alias_prints_help(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ga.main(["help"])

        self.assertEqual(rc, 0)
        self.assertIn("grok-accounts save [<name>]", out.getvalue())
        self.assertIn("-h | --help | help", out.getvalue())

    def test_unknown_command_still_rejected_after_reshape(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ga.main(["frobnicate"])

        self.assertEqual(rc, 1)
        self.assertIn(
            "Unknown or incomplete command: frobnicate", _ANSI_RE.sub("", err.getvalue())
        )


class GrokAutoswitchTests(unittest.TestCase):
    """grok exposes no quota API at all, so autoswitch can only say so."""

    def test_autoswitch_reports_unsupported_and_exits_zero(self) -> None:
        # Given: grok-accounts, which has no quota endpoint to consult
        out, err = io.StringIO(), io.StringIO()

        # When: the autoswitch subcommand runs
        with (
            mock.patch.object(aw, "run_autoswitch") as engine,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = ga.main(["autoswitch"])

        # Then: exit 0 (the ai-accounts fan-out must not fail over grok),
        # exactly one explicit line, and the engine is never consulted.
        self.assertEqual(rc, 0)
        self.assertEqual(
            _ANSI_RE.sub("", out.getvalue()).splitlines(),
            ["autoswitch unsupported for grok: no quota API"],
        )
        self.assertEqual(err.getvalue(), "")
        engine.assert_not_called()


def _oidc_auth(expires_at: str = "2030-01-02T03:04:05Z") -> ga.JsonDict:
    """A credential that carries its own issuer/client id, so the direct OIDC
    refresh path can run (the plain `_auth()` fixture deliberately does not)."""
    payload = _auth()
    record = payload["https://auth.x.ai::client"]
    record["oidc_issuer"] = "https://auth.example.test"
    record["oidc_client_id"] = "client-id-placeholder"
    record["expires_at"] = expires_at
    return payload


class GrokDirectRefreshTests(unittest.TestCase):
    """The direct OIDC refresh grant: discovery, secretless probe, and the
    revoked / client-auth / transient split that decides the fallback."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.account_dir = home / ".ai-accounts" / "grok" / "accounts"
        environment = mock.patch.dict(
            os.environ,
            {
                "GROK_HOME": str(home / ".grok"),
                "GROK_ACCOUNT_DIR": str(self.account_dir),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        ga._TOKEN_ENDPOINTS.clear()
        self.addCleanup(ga._TOKEN_ENDPOINTS.clear)

    # ── discovery ────────────────────────────────────────────────────────

    def test_token_endpoint_read_from_well_known_and_cached(self) -> None:
        document = mock.MagicMock()
        document.__enter__.return_value.read.return_value = (
            b'{"token_endpoint": "https://auth.example.test/oauth2/token"}'
        )

        with mock.patch("urllib.request.urlopen", return_value=document) as opened:
            first = ga._token_endpoint("https://auth.example.test/")
            second = ga._token_endpoint("https://auth.example.test/")

        self.assertEqual(first, "https://auth.example.test/oauth2/token")
        self.assertEqual(second, first)
        opened.assert_called_once()
        self.assertEqual(
            opened.call_args.args[0],
            "https://auth.example.test/.well-known/openid-configuration",
        )

    def test_token_endpoint_returns_none_when_discovery_fails(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(ga._token_endpoint("https://auth.example.test"))

    # ── classification ───────────────────────────────────────────────────

    def test_error_classification_splits_revoked_client_auth_and_transient(self) -> None:
        revoked = ga._http_error_message(400, '{"error": "invalid_grant"}')
        secret = ga._http_error_message(401, '{"error": "invalid_client"}')
        bare401 = ga._http_error_message(401, "")
        transient = ga._http_error_message(503, "upstream unavailable")

        self.assertTrue(ga._is_revoked_error(revoked))
        self.assertFalse(ga._needs_client_secret(revoked))
        self.assertTrue(ga._needs_client_secret(secret))
        self.assertTrue(ga._needs_client_secret(bare401))
        self.assertFalse(ga._is_revoked_error(secret))
        self.assertFalse(ga._is_revoked_error(transient))
        self.assertFalse(ga._needs_client_secret(transient))
        self.assertIn("503", transient)

    # ── the grant itself ─────────────────────────────────────────────────

    def test_refresh_grant_is_attempted_without_a_client_secret(self) -> None:
        with (
            mock.patch.object(ga, "_token_endpoint", return_value="https://auth.example.test/t"),
            mock.patch.object(
                ga, "oauth_token_refresh", return_value=({"access_token": "new"}, None)
            ) as posted,
        ):
            ga._oidc_refresh(ga._record(_oidc_auth()))

        url, body = posted.call_args.args
        self.assertEqual(url, "https://auth.example.test/t")
        self.assertEqual(body["grant_type"], "refresh_token")
        self.assertEqual(body["client_id"], "client-id-placeholder")
        self.assertNotIn("client_secret", body)
        self.assertTrue(posted.call_args.kwargs["form_encoded"])

    def test_credential_without_issuer_never_touches_the_network(self) -> None:
        with mock.patch.object(ga, "_token_endpoint") as discovery:
            _, error = ga._direct_refresh(_auth())

        discovery.assert_not_called()
        self.assertTrue(error.startswith("unsupported"))

    def test_response_rotates_refresh_token_and_recomputes_iso_expiry(self) -> None:
        response = {
            "access_token": "fresh-access-token",
            "refresh_token": "fresh-refresh-token",
            "expires_in": 3600,
        }
        updated = ga._apply_refreshed(_oidc_auth(), response)

        record = ga._record(updated)
        self.assertEqual(record["key"], "fresh-access-token")
        self.assertEqual(record["refresh_token"], "fresh-refresh-token")
        expires = ga._claims(updated)["expires_at"]
        self.assertTrue(expires.endswith("Z"), expires)
        seconds = (
            ga.datetime.fromisoformat(expires.replace("Z", "+00:00"))
            - ga.datetime.now(ga.timezone.utc)
        ).total_seconds()
        self.assertTrue(3400 < seconds <= 3600, seconds)

    def test_absent_refresh_token_in_response_keeps_the_stored_one(self) -> None:
        updated = ga._apply_refreshed(
            _oidc_auth(), {"access_token": "fresh-access-token", "expires_in": 60}
        )
        self.assertEqual(ga._record(updated)["refresh_token"], "secret-refresh-token")

    def test_unknown_credential_shape_is_not_given_an_invented_field(self) -> None:
        payload = _oidc_auth()
        del payload["https://auth.x.ai::client"]["key"]
        self.assertIsNone(ga._apply_refreshed(payload, {"access_token": "fresh"}))

    # ── wiring: refresh ──────────────────────────────────────────────────

    def test_profile_refresh_uses_the_direct_grant_and_skips_the_cli(self) -> None:
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _oidc_auth()))

        with (
            mock.patch.object(ga, "_token_endpoint", return_value="https://auth.example.test/t"),
            mock.patch.object(
                ga,
                "oauth_token_refresh",
                return_value=({"access_token": "fresh-access-token", "expires_in": 3600}, None),
            ),
            mock.patch.object(ga, "_run_grok_refresh") as cli,
        ):
            self.assertEqual(ga._refresh_profile(profile), 0)

        cli.assert_not_called()
        self.assertEqual(ga._record(ga._read_json(profile))["key"], "fresh-access-token")

    def test_client_secret_requirement_falls_back_to_the_grok_cli(self) -> None:
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _oidc_auth()))
        rejection = (None, "client-auth: token endpoint requires client authentication")

        with (
            mock.patch.object(ga, "_token_endpoint", return_value="https://auth.example.test/t"),
            mock.patch.object(ga, "oauth_token_refresh", return_value=rejection),
            mock.patch.object(ga, "_run_grok_refresh", return_value=0) as cli,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(ga._refresh_profile(profile), 0)

        cli.assert_called_once()

    def test_transient_failure_falls_back_to_the_grok_cli(self) -> None:
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _oidc_auth()))

        with (
            mock.patch.object(ga, "_token_endpoint", return_value="https://auth.example.test/t"),
            mock.patch.object(
                ga, "oauth_token_refresh", return_value=(None, "network error: timed out")
            ),
            mock.patch.object(ga, "_run_grok_refresh", return_value=0) as cli,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(ga._refresh_profile(profile), 0)

        cli.assert_called_once()

    def test_revoked_refresh_token_fails_without_spawning_the_cli(self) -> None:
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _oidc_auth()))
        rejection = (None, "revoked: refresh token rejected (invalid_grant)")

        err = io.StringIO()
        with (
            mock.patch.object(ga, "_token_endpoint", return_value="https://auth.example.test/t"),
            mock.patch.object(ga, "oauth_token_refresh", return_value=rejection),
            mock.patch.object(ga, "_run_grok_refresh") as cli,
            redirect_stderr(err),
        ):
            self.assertEqual(ga._refresh_profile(profile), 1)

        cli.assert_not_called()
        self.assertIn("login-switch personal", _ANSI_RE.sub("", err.getvalue()))

    def test_refresh_output_never_leaks_a_token(self) -> None:
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _oidc_auth()))

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(ga, "_token_endpoint", return_value="https://auth.example.test/t"),
            mock.patch.object(
                ga,
                "oauth_token_refresh",
                return_value=(
                    {
                        "access_token": "fresh-access-token",
                        "refresh_token": "fresh-refresh-token",
                        "expires_in": 3600,
                    },
                    None,
                ),
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            self.assertEqual(ga.cmd_refresh("personal"), 0)

        text = out.getvalue() + err.getvalue()
        for secret in (
            "fresh-access-token",
            "fresh-refresh-token",
            "secret-refresh-token",
            "secret-access-token",
        ):
            self.assertNotIn(secret, text)

    # ── wiring: switch ───────────────────────────────────────────────────

    def test_expired_token_is_refreshed_on_switch(self) -> None:
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _oidc_auth("2020-01-01T00:00:00Z")))

        with (
            mock.patch.object(ga, "_token_endpoint", return_value="https://auth.example.test/t"),
            mock.patch.object(
                ga,
                "oauth_token_refresh",
                return_value=({"access_token": "fresh-access-token", "expires_in": 3600}, None),
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(ga.cmd_switch("personal"), 0)

        for path in (ga._auth_file(), profile):
            self.assertEqual(ga._record(ga._read_json(path))["key"], "fresh-access-token")

    def test_switch_leaves_a_fresh_token_alone(self) -> None:
        profile = self.account_dir / "personal.json"
        self.assertTrue(ga._write_json(profile, _oidc_auth()))

        with (
            mock.patch.object(ga, "oauth_token_refresh") as posted,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(ga.cmd_switch("personal"), 0)

        posted.assert_not_called()

    def test_skew_window_is_five_minutes(self) -> None:
        self.assertEqual(ga._REFRESH_SKEW_SECONDS, 300)
        now = ga.datetime.now(ga.timezone.utc)

        def at(seconds: int) -> ga.JsonDict:
            when = now + ga.timedelta(seconds=seconds)
            return {"expires_at": when.isoformat().replace("+00:00", "Z")}

        self.assertTrue(ga._token_expired_or_soon(at(-1)))
        self.assertTrue(ga._token_expired_or_soon(at(120)))
        self.assertFalse(ga._token_expired_or_soon(at(1800)))
        self.assertFalse(ga._token_expired_or_soon({}))
        self.assertFalse(ga._token_expired_or_soon({"expires_at": "not-a-date"}))


if __name__ == "__main__":
    unittest.main()
