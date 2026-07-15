"""Tests for codex-accounts refresh/sync token-update commands.

All filesystem access is redirected into a temp dir via CODEX_HOME; the OAuth
refresh HTTP call is mocked — no network, no real tokens. Run with:
``python -m unittest discover tests``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import codex_accounts as ca
from ai_accounts import codex_usage


def _jwt(payload: dict) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _auth_payload(
    account_id: str,
    email: str,
    *,
    refresh_token: str = "rt-old",
    expires_in: int = 10 * 24 * 3600,
    last_refresh: str = "2026-01-01T00:00:00.000000Z",
) -> dict:
    exp = int(time.time()) + expires_in
    token = _jwt({"email": email, "exp": exp, "account_id": account_id})
    return {
        "tokens": {
            "id_token": token,
            "access_token": token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": last_refresh,
    }


class _CodexHomeMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(
            os.environ,
            {"CODEX_HOME": str(self.home)},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        for var in ("CODEX_AUTH_JSON", "CODEX_ACCOUNT_DIR"):
            os.environ.pop(var, None)
        (self.home / "accounts").mkdir(parents=True)

    def write_auth(self, payload: dict) -> Path:
        path = self.home / "auth.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_profile(self, name: str, payload: dict) -> Path:
        path = self.home / "accounts" / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def mark_current(self, name: str) -> Path:
        path = self.home / "accounts" / ".current-profile"
        path.write_text(name, encoding="utf-8")
        return path

    def run_quiet(self, fn, *args) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return fn(*args)

    def run_capture(self, fn, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = fn(*args)
        return rc, out.getvalue(), err.getvalue()


class OauthRefreshRequestTests(unittest.TestCase):
    """_oauth_refresh must send the exact request codex-rs sends."""

    def test_request_shape_and_success(self):
        captured = {}

        class FakeResponse:
            def read(self):
                return json.dumps({"access_token": "at-new"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            captured["content_type"] = request.get_header("Content-type")
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            refreshed, error = ca._oauth_refresh("rt-123")

        self.assertIsNone(error)
        self.assertEqual(refreshed, {"access_token": "at-new"})
        self.assertEqual(captured["url"], "https://auth.openai.com/oauth/token")
        self.assertEqual(captured["content_type"], "application/json")
        self.assertEqual(
            captured["body"],
            {
                "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                "grant_type": "refresh_token",
                "refresh_token": "rt-123",
            },
        )

    def test_http_error_is_reported_not_raised(self):
        import urllib.error

        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            refreshed, error = ca._oauth_refresh("rt-bad")
        self.assertIsNone(refreshed)
        self.assertIn("401", error)


class ReadActiveAuthTextTests(_CodexHomeMixin):
    """_read_active_auth_text feeds every persist path with the NEWEST of the
    keychain item and auth.json (compared by last_refresh). Neither source is
    trustworthy alone: codex rotates tokens keychain-only during normal use
    (stale auth.json), but `codex login` writes auth.json without touching
    the keychain item (stale keychain — a blind keychain-first read clobbered
    fresh logins with pre-login tokens, observed live)."""

    def test_prefers_valid_keychain_json_over_auth_json_file(self):
        # Equal last_refresh stamps (fixture default) — tie goes to the keychain.
        self.write_auth(_auth_payload("acct-file", "file@x.com"))
        kc_secret = json.dumps(_auth_payload("acct-kc", "kc@x.com"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=kc_secret):
            text = ca._read_active_auth_text()
        self.assertEqual(text, kc_secret)

    def test_prefers_fresher_auth_json_over_stale_keychain(self):
        # The login-switch regression: `codex login` just wrote fresh tokens to
        # auth.json; the keychain still holds the pre-login mirror. The fresh
        # file must win or the login is silently destroyed.
        auth = self.write_auth(
            _auth_payload(
                "acct-w", "w@x.com", refresh_token="rt-fresh-login",
                last_refresh="2026-07-11T12:00:00.000000Z",
            )
        )
        stale_kc = json.dumps(
            _auth_payload(
                "acct-w", "w@x.com", refresh_token="rt-pre-login",
                last_refresh="2026-07-11T11:19:07.000000Z",
            )
        )
        with mock.patch.object(ca, "_read_keychain_auth", return_value=stale_kc):
            text = ca._read_active_auth_text()
        self.assertEqual(text, auth.read_text())

    def test_prefers_fresher_keychain_over_stale_auth_json(self):
        self.write_auth(
            _auth_payload(
                "acct-w", "w@x.com", refresh_token="rt-stale",
                last_refresh="2026-07-11T11:00:00.000000Z",
            )
        )
        fresh_kc = json.dumps(
            _auth_payload(
                "acct-w", "w@x.com", refresh_token="rt-rotated",
                last_refresh="2026-07-11T12:00:00.000000Z",
            )
        )
        with mock.patch.object(ca, "_read_keychain_auth", return_value=fresh_kc):
            text = ca._read_active_auth_text()
        self.assertEqual(text, fresh_kc)

    def test_unstamped_auth_json_loses_to_stamped_keychain(self):
        payload = _auth_payload("acct-w", "w@x.com", refresh_token="rt-unstamped")
        del payload["last_refresh"]
        self.write_auth(payload)
        kc = json.dumps(_auth_payload("acct-w", "w@x.com", refresh_token="rt-kc"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=kc):
            text = ca._read_active_auth_text()
        self.assertEqual(text, kc)

    def test_falls_back_to_auth_json_when_keychain_absent(self):
        auth = self.write_auth(_auth_payload("acct-file", "file@x.com"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            text = ca._read_active_auth_text()
        self.assertEqual(text, auth.read_text())

    def test_falls_back_to_auth_json_when_keychain_content_is_invalid_json(self):
        auth = self.write_auth(_auth_payload("acct-file", "file@x.com"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value="not json"):
            text = ca._read_active_auth_text()
        self.assertEqual(text, auth.read_text())

    def test_returns_none_when_nothing_available(self):
        with mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            self.assertIsNone(ca._read_active_auth_text())


class SaveCommandTests(_CodexHomeMixin):
    """cmd_save must persist the *active* auth keychain-first — codex may
    rotate tokens (e.g. a fresh browser login) into the keychain only,
    leaving auth.json stale. Saving auth.json bytes would silently discard
    that rotation into the profile."""

    def test_save_persists_keychain_content_over_stale_auth_json(self):
        self.write_auth(_auth_payload("acct-w", "w@x.com", refresh_token="rt-stale"))
        live = json.dumps(_auth_payload("acct-w", "w@x.com", refresh_token="rt-live"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=live):
            rc = self.run_quiet(ca.cmd_save, "work")

        self.assertEqual(rc, 0)
        saved = json.loads((self.home / "accounts" / "work.json").read_text())
        self.assertEqual(saved["tokens"]["refresh_token"], "rt-live")
        # auth.json rewritten from the same content so file and keychain agree
        auth = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(auth["tokens"]["refresh_token"], "rt-live")

    def test_save_works_with_keychain_only_login_no_auth_json_file(self):
        # Pure keychain-backed login: codex never wrote auth.json at all.
        live = json.dumps(_auth_payload("acct-k", "k@x.com", refresh_token="rt-kc"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=live):
            rc = self.run_quiet(ca.cmd_save, "kc-only")

        self.assertEqual(rc, 0)
        saved = json.loads((self.home / "accounts" / "kc-only.json").read_text())
        self.assertEqual(saved["tokens"]["refresh_token"], "rt-kc")

    def test_save_without_auth_or_keychain_errors(self):
        with mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            rc = self.run_quiet(ca.cmd_save, "nope")
        self.assertEqual(rc, 1)
        self.assertFalse((self.home / "accounts" / "nope.json").exists())

    def test_login_switch_saves_isolated_auth(self):
        fresh = json.dumps(_auth_payload("acct-new", "new@x.com", refresh_token="rt-fresh"))
        with mock.patch.object(ca, "ensure_tool", return_value=True), \
                mock.patch.object(ca, "_run_isolated_login", return_value=(fresh, 0)):
            rc = self.run_quiet(ca.cmd_login_switch, "newacct")

        self.assertEqual(rc, 0)
        saved = json.loads((self.home / "accounts" / "newacct.json").read_text())
        self.assertEqual(saved["tokens"]["refresh_token"], "rt-fresh")

    def test_login_switch_fresh_file_login_beats_stale_keychain_and_updates_it(self):
        # The observed live disaster: `codex login` wrote the fresh login to
        # auth.json ONLY; the keychain still held the previous account's
        # (already-revoked) mirror. Save must persist the fresh login — not
        # resurrect the stale mirror — and converge the keychain to match.
        self.write_auth(
            _auth_payload(
                "acct-new", "new@x.com", refresh_token="rt-fresh-login",
                last_refresh="2026-07-11T12:00:00.000000Z",
            )
        )
        stale_kc = json.dumps(
            _auth_payload(
                "acct-old", "old@x.com", refresh_token="rt-dead",
                last_refresh="2026-07-11T11:19:07.000000Z",
            )
        )
        written = []
        with mock.patch.object(ca, "_read_keychain_auth", return_value=stale_kc), \
                mock.patch.object(ca, "_write_keychain_auth", side_effect=lambda c: written.append(c) or True):
            rc = self.run_quiet(ca.cmd_save, "newacct")

        self.assertEqual(rc, 0)
        saved = json.loads((self.home / "accounts" / "newacct.json").read_text())
        self.assertEqual(saved["tokens"]["refresh_token"], "rt-fresh-login")
        auth = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(auth["tokens"]["refresh_token"], "rt-fresh-login")
        # keychain mirror converged to the fresh login too
        self.assertTrue(written, "cmd_save must update the stale keychain mirror")
        self.assertEqual(json.loads(written[-1])["tokens"]["refresh_token"], "rt-fresh-login")

    def test_login_switch_uses_isolated_login_without_logging_out_current_profile(self):
        current = self.write_profile(
            "current", _auth_payload("acct-old", "old@x.com", refresh_token="rt-current")
        )
        self.write_auth(_auth_payload("acct-old", "old@x.com", refresh_token="rt-current"))
        self.mark_current("current")
        fresh = _auth_payload("acct-new", "new@x.com", refresh_token="rt-new")
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:2] == ["codex", "login"]:
                login_home = Path(kwargs.get("env", {}).get("CODEX_HOME", self.home))
                login_home.mkdir(parents=True, exist_ok=True)
                (login_home / "auth.json").write_text(json.dumps(fresh), encoding="utf-8")
            return mock.Mock(returncode=0)

        with mock.patch.object(ca, "ensure_tool", return_value=True), \
                mock.patch.object(ca, "_read_keychain_auth", return_value=None), \
                mock.patch.object(ca.subprocess, "run", side_effect=fake_run):
            rc = self.run_quiet(ca.cmd_login_switch, "newacct")

        self.assertEqual(rc, 0)
        self.assertNotIn(["codex", "logout"], [command for command, _kwargs in calls])
        login_call = next((item for item in calls if item[0][:2] == ["codex", "login"]), None)
        self.assertIsNotNone(login_call)
        self.assertNotEqual(Path(login_call[1]["env"]["CODEX_HOME"]), self.home)
        self.assertIn('cli_auth_credentials_store="file"', login_call[0])
        self.assertEqual(json.loads(current.read_text())["tokens"]["refresh_token"], "rt-current")
        saved = json.loads((self.home / "accounts" / "newacct.json").read_text())
        self.assertEqual(saved["tokens"]["refresh_token"], "rt-new")

    def test_login_switch_updates_exact_token_aliases(self):
        shared = _auth_payload("acct-a", "a@x.com", refresh_token="rt-shared")
        self.write_profile("primary", shared)
        alias = self.write_profile("alias", shared)
        self.write_auth(_auth_payload("acct-current", "current@x.com"))
        fresh = json.dumps(_auth_payload("acct-a", "a@x.com", refresh_token="rt-fresh"))

        with mock.patch.object(ca, "ensure_tool", return_value=True), \
                mock.patch.object(ca, "_read_keychain_auth", return_value=None), \
                mock.patch.object(ca, "_run_isolated_login", return_value=(fresh, 0)):
            rc = self.run_quiet(ca.cmd_login_switch, "primary")

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(alias.read_text())["tokens"]["refresh_token"], "rt-fresh")

    def test_save_keeps_other_same_account_profile_independent(self):
        # Given: a fresh login and another named profile for the same account.
        alias = self.write_profile(
            "alias", _auth_payload("acct-a", "a@x.com", refresh_token="rt-stale")
        )
        self.write_auth(_auth_payload("acct-a", "a@x.com", refresh_token="rt-live"))

        # When: the fresh login is saved as the current profile.
        with mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            rc = self.run_quiet(ca.cmd_save, "primary")

        # Then: only the named profile is written and the sibling stays intact.
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(alias.read_text())["tokens"]["refresh_token"], "rt-stale")
        self.assertEqual((self.home / "accounts" / ".current-profile").read_text(), "primary")


class CopyActiveAuthGuardTests(_CodexHomeMixin):
    """_copy_active_auth_to serves fold-back/sync callers that only ever sync
    the SAME account; writing a different account's tokens into dest destroys
    dest's only copy (observed live when a stale keychain diverged from
    auth.json). The guard refuses cross-account writes."""

    def test_refuses_to_overwrite_profile_of_different_account(self):
        dest = self.home / "accounts" / "other.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(_auth_payload("acct-other", "other@x.com", refresh_token="rt-other")))
        self.write_auth(_auth_payload("acct-active", "active@x.com", refresh_token="rt-active"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            self.run_quiet(ca._copy_active_auth_to, dest)
        kept = json.loads(dest.read_text())
        self.assertEqual(kept["tokens"]["refresh_token"], "rt-other")

    def test_still_syncs_same_account(self):
        dest = self.home / "accounts" / "mine.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(_auth_payload("acct-a", "a@x.com", refresh_token="rt-old")))
        self.write_auth(_auth_payload("acct-a", "a@x.com", refresh_token="rt-rotated"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            self.run_quiet(ca._copy_active_auth_to, dest)
        synced = json.loads(dest.read_text())
        self.assertEqual(synced["tokens"]["refresh_token"], "rt-rotated")


class RefreshCommandTests(_CodexHomeMixin):
    def test_refresh_profile_writes_new_tokens_and_last_refresh(self):
        profile = self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000}),
               "id_token": "idt-new", "refresh_token": "rt-new"}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)):
            rc = self.run_quiet(ca.cmd_refresh, "work")

        self.assertEqual(rc, 0)
        data = json.loads(profile.read_text())
        self.assertEqual(data["tokens"]["access_token"], new["access_token"])
        self.assertEqual(data["tokens"]["id_token"], "idt-new")
        self.assertEqual(data["tokens"]["refresh_token"], "rt-new")
        self.assertNotEqual(data["last_refresh"], "2026-01-01T00:00:00.000000Z")
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)

    def test_refresh_profile_keeps_same_account_profile_independent(self):
        # Given: two named profiles for one account with independent token chains.
        self.write_profile("primary", _auth_payload("acct-a", "a@x.com", refresh_token="rt-primary"))
        alias = self.write_profile(
            "alias", _auth_payload("acct-a", "a@x.com", refresh_token="rt-alias")
        )
        new = {
            "access_token": _jwt({"exp": int(time.time()) + 864000}),
            "refresh_token": "rt-new",
        }

        # When: one profile refreshes successfully.
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)):
            rc = self.run_quiet(ca.cmd_refresh, "primary")

        # Then: the sibling retains its own refresh token.
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(alias.read_text())["tokens"]["refresh_token"], "rt-alias")

    def test_refresh_active_profile_also_updates_auth_json(self):
        self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        auth = self.write_auth(_auth_payload("acct-w", "w@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000}),
               "refresh_token": "rt-new"}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)):
            self.run_quiet(ca.cmd_refresh, "work")

        auth_data = json.loads(auth.read_text())
        self.assertEqual(auth_data["tokens"]["refresh_token"], "rt-new")

    def test_refresh_other_profile_leaves_auth_json_alone(self):
        self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        auth = self.write_auth(_auth_payload("acct-p", "p@x.com"))
        before = auth.read_text()
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000})}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)):
            self.run_quiet(ca.cmd_refresh, "work")
        self.assertEqual(auth.read_text(), before)

    def test_refresh_failure_leaves_profile_unchanged(self):
        profile = self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        before = profile.read_text()
        with mock.patch.object(ca, "_oauth_refresh", return_value=(None, "HTTP 401")):
            rc = self.run_quiet(ca.cmd_refresh, "work")
        self.assertEqual(rc, 1)
        self.assertEqual(profile.read_text(), before)

    def test_refresh_missing_refresh_token_errors(self):
        payload = _auth_payload("acct-w", "w@x.com")
        del payload["tokens"]["refresh_token"]
        self.write_profile("work", payload)
        rc = self.run_quiet(ca.cmd_refresh, "work")
        self.assertEqual(rc, 1)

    def test_refresh_unknown_profile_errors(self):
        rc = self.run_quiet(ca.cmd_refresh, "nope")
        self.assertEqual(rc, 1)

    def test_refresh_all_continues_past_failures(self):
        self.write_profile("good", _auth_payload("acct-g", "g@x.com", refresh_token="rt-g"))
        self.write_profile("bad", _auth_payload("acct-b", "b@x.com", refresh_token="rt-b"))

        def fake_refresh(token):
            if token == "rt-g":
                return {"access_token": _jwt({"exp": int(time.time()) + 864000})}, None
            return None, "HTTP 401"

        with mock.patch.object(ca, "_oauth_refresh", side_effect=fake_refresh):
            rc, out, _err = self.run_capture(ca.cmd_refresh, "--all")
        self.assertEqual(rc, 1)  # at least one failure
        text = ca._ANSI_RE.sub("", out)
        self.assertIn("Refreshed Codex profile: good", text)
        self.assertIn("Saved Codex profiles", text)

    def test_refresh_all_success(self):
        self.write_profile("a", _auth_payload("acct-a", "a@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000})}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)):
            rc = self.run_quiet(ca.cmd_refresh, "--all")
        self.assertEqual(rc, 0)

    def test_refresh_all_prints_profile_panels_then_final_list_table(self):
        self.write_profile("a", _auth_payload("acct-a", "a@x.com", refresh_token="rt-a"))
        self.write_profile("b", _auth_payload("acct-b", "b@x.com", refresh_token="rt-b"))

        def refresh(token):
            account_id = "acct-a" if token == "rt-a" else "acct-b"
            email = "a-new@x.com" if token == "rt-a" else "b-new@x.com"
            return {
                "access_token": _jwt(
                    {"email": email, "exp": int(time.time()) + 864000, "account_id": account_id}
                ),
                "refresh_token": f"{token}-new",
            }, None

        with mock.patch.object(ca, "_oauth_refresh", side_effect=refresh):
            rc, out, _err = self.run_capture(ca.cmd_refresh, "--all")

        text = ca._ANSI_RE.sub("", out)
        self.assertEqual(rc, 0)
        self.assertTrue(text.startswith("✅ Refreshed Codex profile: a"))
        self.assertEqual(text.count("Saved Codex profiles"), 1)
        self.assertIn("Profile: a", text)
        self.assertIn("Profile: b", text)
        self.assertIn("a-new@x.com", text)
        self.assertIn("b-new@x.com", text)

    def test_refresh_all_refreshes_same_account_profiles_independently(self):
        # Given: two named profiles share an account ID but have separate token chains.
        first = self.write_profile(
            "first", _auth_payload("acct-a", "a@x.com", refresh_token="rt-first")
        )
        second = self.write_profile(
            "second", _auth_payload("acct-a", "a@x.com", refresh_token="rt-second")
        )

        def refresh(token):
            return {
                "access_token": _jwt({"exp": int(time.time()) + 864000}),
                "refresh_token": f"{token}-new",
            }, None

        # When: all profiles are refreshed.
        with mock.patch.object(ca, "_oauth_refresh", side_effect=refresh) as oauth_refresh:
            rc = self.run_quiet(ca.cmd_refresh, "--all")

        # Then: each profile refreshes from and persists its own token chain.
        self.assertEqual(rc, 0)
        self.assertEqual(
            [call.args[0] for call in oauth_refresh.call_args_list],
            ["rt-first", "rt-second"],
        )
        self.assertEqual(json.loads(first.read_text())["tokens"]["refresh_token"], "rt-first-new")
        self.assertEqual(json.loads(second.read_text())["tokens"]["refresh_token"], "rt-second-new")

    def test_refresh_all_refreshes_exact_token_aliases_once(self):
        shared = _auth_payload("acct-a", "a@x.com", refresh_token="rt-shared")
        first = self.write_profile("first", shared)
        second = self.write_profile("second", shared)
        new = {
            "access_token": _jwt({"exp": int(time.time()) + 864000}),
            "refresh_token": "rt-fresh",
        }

        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)) as oauth_refresh:
            rc = self.run_quiet(ca.cmd_refresh, "--all")

        self.assertEqual(rc, 0)
        oauth_refresh.assert_called_once_with("rt-shared")
        self.assertEqual(json.loads(first.read_text())["tokens"]["refresh_token"], "rt-fresh")
        self.assertEqual(json.loads(second.read_text())["tokens"]["refresh_token"], "rt-fresh")

    def test_refresh_no_arg_refreshes_auth_and_syncs_profile(self):
        auth = self.write_auth(_auth_payload("acct-w", "w@x.com"))
        profile = self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000}),
               "refresh_token": "rt-new"}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)):
            rc = self.run_quiet(ca.cmd_refresh, None)

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(auth.read_text())["tokens"]["refresh_token"], "rt-new")
        # profile synced from the freshly refreshed auth.json
        self.assertEqual(json.loads(profile.read_text())["tokens"]["refresh_token"], "rt-new")

    def test_refresh_no_arg_without_auth_errors(self):
        rc = self.run_quiet(ca.cmd_refresh, None)
        self.assertEqual(rc, 1)

    def test_refresh_no_arg_syncback_prefers_genuinely_newer_keychain(self):
        # Sync-back to the matching profile uses the newest active source: if
        # the keychain holds a rotation stamped NEWER than the refresh we just
        # wrote to auth.json (codex rotated concurrently), the keychain wins.
        self.write_auth(_auth_payload("acct-w", "w@x.com"))
        profile = self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000}), "refresh_token": "rt-new"}
        live = json.dumps(
            _auth_payload(
                "acct-w", "w@x.com", refresh_token="rt-keychain-live",
                last_refresh="2030-01-01T00:00:00.000000Z",
            )
        )
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)), \
                mock.patch.object(ca, "_read_keychain_auth", return_value=live), \
                mock.patch.object(ca, "_write_keychain_auth", return_value=True):
            rc = self.run_quiet(ca.cmd_refresh, None)

        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(profile.read_text())["tokens"]["refresh_token"], "rt-keychain-live"
        )

    def test_refresh_http_4xx_classified_as_revoked_with_login_switch_message(self):
        self.write_profile("work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-dead"))
        with mock.patch.object(ca, "_oauth_refresh", return_value=(None, "HTTP 401 from token endpoint")):
            rc, _out, err = self.run_capture(ca.cmd_refresh, "work")
        self.assertEqual(rc, 1)
        self.assertIn("codex-accounts login-switch work", err)

    def test_refresh_active_auth_revoked_suggests_codex_login_not_login_switch(self):
        # "the active auth" is not a profile name — the revoked-token guidance
        # must say `codex login`, not `login-switch the active auth`.
        self.write_auth(_auth_payload("acct-w", "w@x.com", refresh_token="rt-dead"))
        with mock.patch.object(ca, "_oauth_refresh", return_value=(None, "HTTP 401 from token endpoint")):
            rc, _out, err = self.run_capture(ca.cmd_refresh, None)
        self.assertEqual(rc, 1)
        self.assertIn("codex login", err)
        self.assertNotIn("login-switch the active auth", err)

    def test_refresh_transient_network_error_gets_distinct_retry_later_message(self):
        self.write_profile("work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-w"))
        with mock.patch.object(ca, "_oauth_refresh", return_value=(None, "network error: timed out")):
            rc, _out, err = self.run_capture(ca.cmd_refresh, "work")
        self.assertEqual(rc, 1)
        self.assertIn("retry later", err.lower())
        self.assertNotIn("login-switch", err)

    def test_refresh_http_5xx_is_transient_not_revoked(self):
        self.write_profile("work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-w"))
        with mock.patch.object(ca, "_oauth_refresh", return_value=(None, "HTTP 503 from token endpoint")):
            rc, _out, err = self.run_capture(ca.cmd_refresh, "work")
        self.assertEqual(rc, 1)
        self.assertIn("retry later", err.lower())
        self.assertNotIn("login-switch", err)

    def test_refresh_all_distinguishes_revoked_from_transient_and_continues(self):
        self.write_profile("dead", _auth_payload("acct-d", "d@x.com", refresh_token="rt-dead"))
        self.write_profile("flaky", _auth_payload("acct-f", "f@x.com", refresh_token="rt-flaky"))
        self.write_profile("good", _auth_payload("acct-g", "g@x.com", refresh_token="rt-good"))

        calls = []

        def fake_refresh(token):
            calls.append(token)
            if token == "rt-dead":
                return None, "HTTP 401 from token endpoint"
            if token == "rt-flaky":
                return None, "network error: timed out"
            return {"access_token": _jwt({"exp": int(time.time()) + 864000})}, None

        with mock.patch.object(ca, "_oauth_refresh", side_effect=fake_refresh):
            rc, _out, err = self.run_capture(ca.cmd_refresh, "--all")

        self.assertEqual(rc, 1)
        # the loop kept going past both failures — every profile was attempted
        self.assertEqual(sorted(calls), ["rt-dead", "rt-flaky", "rt-good"])
        self.assertIn("Revoked (re-login required)", err)
        self.assertIn("codex-accounts login-switch dead", err)
        self.assertIn("Transient failure, retry later", err)
        self.assertIn("flaky", err)


class SyncCommandTests(_CodexHomeMixin):
    def test_sync_copies_auth_to_matching_profile(self):
        auth = self.write_auth(_auth_payload("acct-w", "w@x.com", refresh_token="rt-live"))
        profile = self.write_profile("work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-stale"))
        self.write_profile("other", _auth_payload("acct-o", "o@x.com"))

        rc = self.run_quiet(ca.cmd_sync)
        self.assertEqual(rc, 0)
        self.assertEqual(profile.read_text(), auth.read_text())
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)

    def test_sync_without_matching_profile_errors(self):
        self.write_auth(_auth_payload("acct-x", "x@x.com"))
        self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        rc = self.run_quiet(ca.cmd_sync)
        self.assertEqual(rc, 1)

    def test_sync_without_auth_file_errors(self):
        rc = self.run_quiet(ca.cmd_sync)
        self.assertEqual(rc, 1)

    def test_sync_prefers_keychain_over_stale_auth_json(self):
        # cmd_sync's whole job is "copy the true active auth to its profile" —
        # it must copy the keychain's live tokens, not a stale auth.json.
        self.write_auth(_auth_payload("acct-w", "w@x.com", refresh_token="rt-file-stale"))
        profile = self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-old-profile")
        )
        live = json.dumps(_auth_payload("acct-w", "w@x.com", refresh_token="rt-keychain-live"))

        with mock.patch.object(ca, "_read_keychain_auth", return_value=live):
            rc = self.run_quiet(ca.cmd_sync)

        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(profile.read_text())["tokens"]["refresh_token"], "rt-keychain-live"
        )

    def test_sync_updates_only_marked_profile_when_account_ids_match(self):
        self.write_auth(_auth_payload("acct-a", "a@x.com", refresh_token="rt-live"))
        first = self.write_profile(
            "first", _auth_payload("acct-a", "a@x.com", refresh_token="rt-first")
        )
        second = self.write_profile(
            "second", _auth_payload("acct-a", "a@x.com", refresh_token="rt-second")
        )
        self.mark_current("second")

        with mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            rc = self.run_quiet(ca.cmd_sync)

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(first.read_text())["tokens"]["refresh_token"], "rt-first")
        self.assertEqual(json.loads(second.read_text())["tokens"]["refresh_token"], "rt-live")


class RemoveCommandTests(_CodexHomeMixin):
    def test_remove_current_profile_clears_marker(self):
        self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        marker = self.mark_current("work")

        rc = self.run_quiet(ca.cmd_remove, "work")

        self.assertEqual(rc, 0)
        self.assertFalse(marker.exists())


class UsageRequestTests(_CodexHomeMixin):
    def test_fetch_usage_sends_codex_headers_and_parses_windows(self):
        auth = self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        captured = {}

        class FakeResponse:
            def read(self):
                return json.dumps(
                    {
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 12,
                                "limit_window_seconds": 18_000,
                                "reset_at": 1_800_000_000,
                            },
                            "secondary_window": {
                                "used_percent": 34,
                                "limit_window_seconds": 604_800,
                                "reset_after_seconds": 3600,
                            },
                        }
                    }
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(request, timeout=None):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["authorization"] = request.get_header("Authorization")
            captured["accept"] = request.get_header("Accept")
            captured["account"] = request.get_header("Chatgpt-account-id")
            return FakeResponse()

        opener = mock.Mock()
        opener.open.side_effect = fake_open
        with mock.patch("urllib.request.build_opener", return_value=opener):
            usage = codex_usage.fetch_usage(auth)

        self.assertEqual(captured["url"], "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(captured["timeout"], 20)
        self.assertTrue(captured["authorization"].startswith("Bearer "))
        self.assertEqual(captured["accept"], "application/json")
        self.assertEqual(captured["account"], "acct-w")
        self.assertIsNotNone(usage.hourly)
        self.assertIsNotNone(usage.weekly)
        self.assertEqual(usage.hourly.percentage, 12)
        self.assertEqual(usage.hourly.window_minutes, 300)
        self.assertEqual(usage.hourly.reset_time, 1_800_000_000)
        self.assertEqual(usage.weekly.percentage, 34)
        self.assertEqual(usage.weekly.window_minutes, 10_080)

    def test_snapshot_classifies_weekly_only_primary_window_by_duration(self):
        # Live-account case: the usage API returned only a 7-day window as
        # primary_window with no secondary_window at all. Positional mapping
        # would put it in the 5H slot; duration-based classification must
        # route it to the weekly slot instead.
        payload = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 58,
                    "limit_window_seconds": 604_800,
                    "reset_after_seconds": 3_600,
                },
            }
        }
        snapshot = codex_usage._snapshot(payload)
        self.assertIsNone(snapshot.hourly)
        self.assertIsNotNone(snapshot.weekly)
        self.assertEqual(snapshot.weekly.percentage, 58)
        self.assertEqual(codex_usage.format_usage_window(snapshot.hourly, "5h"), "-")
        self.assertRegex(
            codex_usage.format_usage_window(snapshot.weekly, "1week"),
            r"^\d+% · \d+d \d+h \d+m$",
        )

    def test_snapshot_classifies_hourly_primary_weekly_secondary_by_duration(self):
        # Assumed ordering (primary=hourly, secondary=weekly) still maps
        # correctly when both windows carry a known duration.
        payload = {
            "rate_limit": {
                "primary_window": {"used_percent": 12, "limit_window_seconds": 18_000},
                "secondary_window": {"used_percent": 34, "limit_window_seconds": 604_800},
            }
        }
        snapshot = codex_usage._snapshot(payload)
        self.assertEqual(snapshot.hourly.percentage, 12)
        self.assertEqual(snapshot.weekly.percentage, 34)

    def test_snapshot_falls_back_to_position_when_duration_missing(self):
        # limit_window_seconds absent on both windows -- duration can't
        # classify them, so it falls back to the positional assumption.
        payload = {
            "rate_limit": {
                "primary_window": {"used_percent": 12},
                "secondary_window": {"used_percent": 34},
            }
        }
        snapshot = codex_usage._snapshot(payload)
        self.assertEqual(snapshot.hourly.percentage, 12)
        self.assertEqual(snapshot.weekly.percentage, 34)

    def test_fetch_usage_reports_error_without_raising(self):
        auth = self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        opener = mock.Mock()
        opener.open.side_effect = OSError("offline")
        with mock.patch("urllib.request.build_opener", return_value=opener):
            usage = codex_usage.fetch_usage(auth)
        self.assertEqual(usage.error, "network error")

    def test_fetch_usage_does_not_follow_redirects_with_bearer_token(self):
        auth = self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        err = urllib.error.HTTPError(
            codex_usage.USAGE_URL,
            302,
            "Found",
            {"Location": "https://example.invalid/steal"},
            io.BytesIO(b""),
        )
        opener = mock.Mock()
        opener.open.side_effect = err
        with mock.patch("urllib.request.build_opener", return_value=opener) as build_opener:
            usage = codex_usage.fetch_usage(auth)

        self.assertEqual(usage.error, "HTTP 302 from usage endpoint")
        self.assertIs(build_opener.call_args.args[0], codex_usage._NoRedirectHandler)

    def test_fetch_usage_treats_window_without_used_percent_as_unknown(self):
        auth = self.write_profile("work", _auth_payload("acct-w", "w@x.com"))

        class FakeResponse:
            def read(self):
                return json.dumps(
                    {"rate_limit": {"primary_window": {"limit_window_seconds": 18_000}}}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        opener = mock.Mock()
        opener.open.return_value = FakeResponse()
        with mock.patch("urllib.request.build_opener", return_value=opener):
            usage = codex_usage.fetch_usage(auth)

        self.assertIsNone(usage.hourly)
        self.assertEqual(codex_usage.format_usage_window(usage.hourly, "5h"), "-")

    def test_usage_window_formats_reset_countdown_5h(self):
        window = codex_usage.UsageWindow(
            percentage=50,
            reset_time=1_800_000_000,
            window_minutes=300,
        )
        with mock.patch.object(codex_usage.time, "time", return_value=1_799_982_480):
            self.assertEqual(codex_usage.format_usage_window(window, "5h"), "50% · 4h 52m")

    def test_usage_window_formats_reset_countdown_1week(self):
        window = codex_usage.UsageWindow(
            percentage=58,
            reset_time=1_800_000_000,
            window_minutes=10_080,
        )
        with mock.patch.object(codex_usage.time, "time", return_value=1_799_902_500):
            self.assertEqual(codex_usage.format_usage_window(window, "1week"), "58% · 1d 3h 5m")

    def test_usage_window_shows_zero_units(self):
        window_5h = codex_usage.UsageWindow(percentage=0, reset_time=1_800_000_120, window_minutes=300)
        window_1w = codex_usage.UsageWindow(percentage=0, reset_time=1_800_003_600, window_minutes=10_080)
        with mock.patch.object(codex_usage.time, "time", return_value=1_800_000_000):
            self.assertEqual(codex_usage.format_usage_window(window_5h, "5h"), "0% · 0h 2m")
            self.assertEqual(codex_usage.format_usage_window(window_1w, "1week"), "0% · 0d 1h 0m")

    def test_usage_cell_matches_required_shape_with_ansi_stripped(self):
        window_5h = codex_usage.UsageWindow(percentage=58, reset_time=1_800_000_000, window_minutes=300)
        window_1w = codex_usage.UsageWindow(percentage=58, reset_time=1_800_000_000, window_minutes=10_080)
        with mock.patch.object(codex_usage.time, "time", return_value=1_799_982_480):
            cell_5h = ca._ANSI_RE.sub("", ca._usage_cell(window_5h, "5h"))
            cell_1w = ca._ANSI_RE.sub("", ca._usage_cell(window_1w, "1week"))
        self.assertRegex(cell_5h, r"^\d+% · \d+h \d+m$")
        self.assertRegex(cell_1w, r"^\d+% · \d+d \d+h \d+m$")

    def test_usage_cell_colors_percent_thresholds(self):
        low = codex_usage.UsageWindow(percentage=0, reset_time=None, window_minutes=300)
        mid = codex_usage.UsageWindow(percentage=50, reset_time=None, window_minutes=300)
        high = codex_usage.UsageWindow(percentage=80, reset_time=None, window_minutes=300)

        self.assertIn(ca.GREEN, ca._usage_cell(low, "5h"))
        self.assertIn(ca.YELLOW, ca._usage_cell(mid, "5h"))
        self.assertIn(ca.RED, ca._usage_cell(high, "5h"))

    def test_usage_table_aligns_weekly_units_to_widest_value(self):
        keys = ("profile", "account", "account_id", "usage_5h", "usage_updated", "expires", "status")
        rows = [
            dict.fromkeys(keys, "x") | {"usage_1week": f"{ca.YELLOW}58%{ca.RESET} · 2d 13h 8m"},
            dict.fromkeys(keys, "x") | {"usage_1week": f"{ca.GREEN}9%{ca.RESET} · 6d 14h 40m"},
            dict.fromkeys(keys, "x") | {"usage_1week": f"{ca.YELLOW}50%{ca.RESET} · 23d 10h 50m"},
            dict.fromkeys(keys, "x") | {"usage_1week": f"{ca.RED}{ca.BOLD}100%{ca.RESET} · 2d 21h 43m"},
        ]

        out = io.StringIO()
        with redirect_stdout(out):
            ca._print_accounts_table(rows)

        text = ca._ANSI_RE.sub("", out.getvalue())
        self.assertIn("│  58% ·  2d 13h  8m │", text)
        self.assertIn("│   9% ·  6d 14h 40m │", text)
        self.assertIn("│  50% · 23d 10h 50m │", text)
        self.assertIn("│ 100% ·  2d 21h 43m │", text)

    def test_list_includes_usage_columns(self):
        self.write_auth(_auth_payload("acct-w", "w@x.com"))
        self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        usage = codex_usage.UsageSnapshot(
            hourly=codex_usage.UsageWindow(
                percentage=12,
                reset_time=1_800_000_000,
                window_minutes=300,
            ),
            weekly=codex_usage.UsageWindow(
                percentage=34,
                reset_time=1_800_003_600,
                window_minutes=10_080,
            ),
            refreshed_at=1_700_000_000,
            error=None,
        )

        out = io.StringIO()
        with mock.patch.object(codex_usage, "fetch_usage", return_value=usage) as fetch_usage, \
                mock.patch.object(ca, "_oauth_refresh") as oauth_refresh, \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = ca.cmd_list()

        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("5H USED", text)
        self.assertIn("1W USED", text)
        self.assertIn("UPDATED", text)
        self.assertIn("12%", text)
        self.assertIn("34%", text)
        fetch_usage.assert_called_once()
        oauth_refresh.assert_not_called()

    def test_list_marks_active_and_fetches_each_profile_once(self):
        self.write_auth(_auth_payload("acct-a", "a@x.com", refresh_token="rt-stale"))
        active_profile = self.write_profile("active", _auth_payload("acct-a", "a@x.com", refresh_token="rt-live"))
        duplicate_profile = self.write_profile("duplicate", _auth_payload("acct-a", "a@x.com", refresh_token="rt-dupe"))
        self.write_profile("other", _auth_payload("acct-b", "b@x.com", refresh_token="rt-other"))
        live_text = json.dumps(_auth_payload("acct-a", "a@x.com", refresh_token="rt-live"))
        duplicate_before = duplicate_profile.read_text()

        with mock.patch.object(ca, "_read_keychain_auth", return_value=live_text), \
                mock.patch.object(
                    codex_usage,
                    "fetch_usage",
                    return_value=codex_usage.UsageSnapshot(
                        hourly=None,
                        weekly=None,
                        refreshed_at=None,
                        error=None,
                    ),
                ) as fetch_usage:
            rc, out, _err = self.run_capture(ca.cmd_list)

        self.assertEqual(rc, 0)
        self.assertEqual(fetch_usage.call_count, 3)
        text = ca._ANSI_RE.sub("", out)
        self.assertEqual(text.count("ACTIVE"), 1)
        self.assertEqual(text.count("SAME ACCT"), 1)
        self.assertEqual(active_profile.read_text(), live_text)
        self.assertEqual(duplicate_profile.read_text(), duplicate_before)

    def test_list_does_not_refresh_or_retry_usage(self):
        # Given: a saved profile with a refresh token that would rotate if used.
        profile = self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-old")
        )
        before = profile.read_text()

        # When: list is rendered.
        with mock.patch.object(ca, "_read_keychain_auth", return_value=None), \
                mock.patch.object(ca, "_oauth_refresh") as oauth_refresh, \
                mock.patch.object(
                    codex_usage,
                    "fetch_usage",
                    return_value=codex_usage.UsageSnapshot(
                        hourly=None,
                        weekly=None,
                        refreshed_at=None,
                        error="HTTP 401 from usage endpoint",
                    ),
                ) as fetch_usage:
            rc, out, _err = self.run_capture(ca.cmd_list)

        # Then: usage is attempted once, but token rotation is explicit-only.
        self.assertEqual(rc, 0)
        self.assertIn("work", out)
        fetch_usage.assert_called_once_with(profile)
        oauth_refresh.assert_not_called()
        self.assertEqual(profile.read_text(), before)

    def test_list_does_not_surface_usage_relogin_state(self):
        self.write_profile("work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-dead"))

        with mock.patch.object(ca, "_read_keychain_auth", return_value=None), \
                mock.patch.object(ca, "_oauth_refresh") as oauth_refresh, \
                mock.patch.object(
                    codex_usage,
                    "fetch_usage",
                    return_value=codex_usage.UsageSnapshot(
                        hourly=None,
                        weekly=None,
                        refreshed_at=None,
                        error="HTTP 401 from usage endpoint",
                    ),
                ) as fetch_usage:
            rc, out, _err = self.run_capture(ca.cmd_list)

        text = ca._ANSI_RE.sub("", out)
        self.assertEqual(rc, 0)
        self.assertNotIn("RELOGIN", text)
        self.assertNotIn("ERR 401", text)
        fetch_usage.assert_called_once()
        oauth_refresh.assert_not_called()

    def test_refresh_all_summary_fetches_current_usage(self):
        self.write_profile("a", _auth_payload("acct-a", "a@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000})}
        usage = codex_usage.UsageSnapshot(
            hourly=codex_usage.UsageWindow(
                percentage=12,
                reset_time=1_800_000_000,
                window_minutes=300,
            ),
            weekly=codex_usage.UsageWindow(
                percentage=34,
                reset_time=1_800_003_600,
                window_minutes=10_080,
            ),
            refreshed_at=1_700_000_000,
            error=None,
        )
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)), \
                mock.patch.object(codex_usage, "fetch_usage", return_value=usage) as fetch_usage:
            rc, out, _err = self.run_capture(ca.cmd_refresh, "--all")

        self.assertEqual(rc, 0)
        self.assertIn("12%", out)
        self.assertIn("34%", out)
        fetch_usage.assert_called_once()


class SwitchStalenessTests(_CodexHomeMixin):
    """switch must not restore tokens codex has already rotated away.

    Codex rotates (and revokes) the refresh_token in auth.json during normal
    use. If switch blindly restores an old profile snapshot, it reactivates a
    revoked token — codex then fails MCP startup with HTTP 401 token_revoked.
    The guard: fold the live auth back into its own profile before overwriting.
    """

    def test_switch_syncs_outgoing_rotated_auth_back_to_its_profile(self):
        # Active account "work": codex rotated auth.json to a live refresh_token,
        # but the saved profile still holds the old (now-revoked) one.
        work_profile = self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-revoked")
        )
        self.write_auth(_auth_payload("acct-w", "w@x.com", refresh_token="rt-live"))
        self.write_profile("personal", _auth_payload("acct-p", "p@x.com"))

        with mock.patch.object(ca, "have", return_value=False):
            rc = self.run_quiet(ca.cmd_switch, "personal")

        self.assertEqual(rc, 0)
        # The outgoing profile must now carry codex's live token, not the stale
        # one that would 401 on a later switch back.
        self.assertEqual(
            json.loads(work_profile.read_text())["tokens"]["refresh_token"], "rt-live"
        )

    def test_switch_back_restores_live_token_not_revoked_one(self):
        self.write_profile("personal", _auth_payload("acct-p", "p@x.com", refresh_token="rt-p-old"))
        self.write_profile("work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-w"))
        # personal is active; codex rotated its refresh_token in auth.json.
        self.write_auth(_auth_payload("acct-p", "p@x.com", refresh_token="rt-p-live"))

        with mock.patch.object(ca, "have", return_value=False):
            self.run_quiet(ca.cmd_switch, "work")      # switch away from personal
            self.run_quiet(ca.cmd_switch, "personal")  # switch back to personal

        auth = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(auth["tokens"]["refresh_token"], "rt-p-live")

    def test_switch_still_backs_up_previous_auth(self):
        self.write_auth(_auth_payload("acct-w", "w@x.com"))
        self.write_profile("personal", _auth_payload("acct-p", "p@x.com"))
        with mock.patch.object(ca, "have", return_value=False):
            rc = self.run_quiet(ca.cmd_switch, "personal")
        self.assertEqual(rc, 0)
        self.assertTrue((self.home / "auth.json.backup").exists())

    def test_switch_with_unmanaged_active_auth_does_not_crash(self):
        # Active auth matches no saved profile (raw `codex login`): nothing to
        # sync back, but switch must still succeed.
        self.write_auth(_auth_payload("acct-x", "x@x.com"))
        self.write_profile("personal", _auth_payload("acct-p", "p@x.com"))
        with mock.patch.object(ca, "have", return_value=False):
            rc = self.run_quiet(ca.cmd_switch, "personal")
        self.assertEqual(rc, 0)
        auth = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(auth["tokens"]["account_id"], "acct-p")

    def test_switch_folds_back_keychain_live_tokens_not_stale_auth_json(self):
        # codex rotated the refresh_token in the keychain only; auth.json on
        # disk is stale. The outgoing profile must capture the live keychain
        # tokens, not the stale file bytes.
        work_profile = self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-profile-old")
        )
        self.write_auth(_auth_payload("acct-w", "w@x.com", refresh_token="rt-file-stale"))
        self.write_profile("personal", _auth_payload("acct-p", "p@x.com"))
        live = json.dumps(_auth_payload("acct-w", "w@x.com", refresh_token="rt-keychain-live"))

        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(ca, "_read_keychain_auth", return_value=live), \
                mock.patch.object(ca, "_write_keychain_auth", return_value=True):
            rc = self.run_quiet(ca.cmd_switch, "personal")

        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(work_profile.read_text())["tokens"]["refresh_token"], "rt-keychain-live"
        )

    def test_switch_folds_back_to_keychain_account_when_auth_json_is_another_account(self):
        # Given: Codex is using work from the keychain while auth.json is stale
        # enough to belong to a different saved account.
        work_profile = self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-profile-old")
        )
        stale_profile = self.write_profile(
            "stale", _auth_payload("acct-s", "s@x.com", refresh_token="rt-stale")
        )
        self.write_auth(json.loads(stale_profile.read_text()))
        self.write_profile("personal", _auth_payload("acct-p", "p@x.com"))
        live = json.dumps(
            _auth_payload("acct-w", "w@x.com", refresh_token="rt-keychain-live")
        )

        # When: switching away folds the active credentials back into a profile.
        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(ca, "_read_keychain_auth", return_value=live), \
                mock.patch.object(ca, "_write_keychain_auth", return_value=True):
            rc = self.run_quiet(ca.cmd_switch, "personal")

        # Then: the actual keychain account keeps its rotated token.
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(work_profile.read_text())["tokens"]["refresh_token"],
            "rt-keychain-live",
        )

    def test_switch_keeps_same_account_profiles_separate(self):
        first = self.write_profile(
            "first", _auth_payload("acct-a", "a@x.com", refresh_token="rt-first-stale")
        )
        second = self.write_profile(
            "second", _auth_payload("acct-a", "a@x.com", refresh_token="rt-second")
        )
        self.write_auth(_auth_payload("acct-a", "a@x.com", refresh_token="rt-first-live"))
        marker = self.mark_current("first")

        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            rc = self.run_quiet(ca.cmd_switch, "second")

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(first.read_text())["tokens"]["refresh_token"], "rt-first-live")
        self.assertEqual(json.loads(second.read_text())["tokens"]["refresh_token"], "rt-second")
        self.assertEqual(
            json.loads((self.home / "auth.json").read_text())["tokens"]["refresh_token"],
            "rt-second",
        )
        self.assertEqual(marker.read_text(), "second")

    def test_switch_folds_rotated_auth_into_exact_token_aliases(self):
        shared = _auth_payload("acct-a", "a@x.com", refresh_token="rt-shared-old")
        first = self.write_profile("first", shared)
        alias = self.write_profile("alias", shared)
        self.write_auth(_auth_payload("acct-a", "a@x.com", refresh_token="rt-live"))
        self.mark_current("first")
        self.write_profile("other", _auth_payload("acct-b", "b@x.com"))

        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            rc = self.run_quiet(ca.cmd_switch, "other")

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(first.read_text())["tokens"]["refresh_token"], "rt-live")
        self.assertEqual(json.loads(alias.read_text())["tokens"]["refresh_token"], "rt-live")


class SwitchExpiredFallbackTests(_CodexHomeMixin):
    """switch self-heals an expired restored token: refresh in place, and only
    escalate to an interactive re-login when the refresh_token is itself dead."""

    def test_fresh_token_skips_refresh_entirely(self):
        self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(ca, "_oauth_refresh") as refresh:
            rc = self.run_quiet(ca.cmd_switch, "work")
        self.assertEqual(rc, 0)
        refresh.assert_not_called()  # non-expired → no network

    def test_expired_token_with_live_refresh_token_auto_refreshes(self):
        self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-work", expires_in=-3600)
        )
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000}), "refresh_token": "rt-fresh"}
        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)) as refresh, \
                mock.patch.object(ca, "cmd_login_switch") as relogin:
            rc = self.run_quiet(ca.cmd_switch, "work")

        self.assertEqual(rc, 0)
        refresh.assert_called_once()
        relogin.assert_not_called()  # refresh sufficed, no browser login
        auth = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(auth["tokens"]["refresh_token"], "rt-fresh")
        # rotated token mirrored back to the profile so it stays live
        prof = json.loads((self.home / "accounts" / "work.json").read_text())
        self.assertEqual(prof["tokens"]["refresh_token"], "rt-fresh")

    def test_expired_token_with_revoked_refresh_token_triggers_login_switch(self):
        self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-dead", expires_in=-3600)
        )
        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(
                    ca, "_oauth_refresh", return_value=(None, "HTTP 401 from token endpoint")
                ), \
                mock.patch.object(ca, "cmd_login_switch", return_value=0) as relogin:
            rc = self.run_quiet(ca.cmd_switch, "work")

        relogin.assert_called_once_with("work")
        self.assertEqual(rc, 0)

    def test_expired_token_network_error_does_not_relogin(self):
        self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", expires_in=-3600)
        )
        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(
                    ca, "_oauth_refresh", return_value=(None, "network error: timed out")
                ), \
                mock.patch.object(ca, "cmd_login_switch") as relogin:
            rc = self.run_quiet(ca.cmd_switch, "work")

        self.assertEqual(rc, 0)  # non-fatal
        relogin.assert_not_called()  # transient blip must not pop a browser

    def test_expired_token_server_5xx_does_not_relogin(self):
        self.write_profile(
            "work", _auth_payload("acct-w", "w@x.com", expires_in=-3600)
        )
        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(
                    ca, "_oauth_refresh", return_value=(None, "HTTP 503 from token endpoint")
                ), \
                mock.patch.object(ca, "cmd_login_switch") as relogin:
            rc = self.run_quiet(ca.cmd_switch, "work")

        self.assertEqual(rc, 0)
        relogin.assert_not_called()  # 5xx is transient, not a revoked token


class KeychainMirrorTests(_CodexHomeMixin):
    """On macOS codex reads its OAuth credentials from the login keychain
    ("Codex Auth") in preference to auth.json. Any write to the active auth
    MUST mirror into that keychain item, or codex keeps using the old account.

    We never *create* a keychain item that codex didn't already have: if no
    item exists (Linux, older codex, or a fresh test home) auth.json is the
    source of truth and we leave the keychain alone.
    """

    def test_keychain_account_is_deterministic_home_hash(self):
        acct = ca._keychain_account()
        self.assertIsNotNone(acct)
        self.assertTrue(acct.startswith("cli|"))
        self.assertEqual(acct, ca._keychain_account())  # stable
        self.assertEqual(len(acct), len("cli|") + 16)

    def test_keychain_account_is_none_off_macos(self):
        with mock.patch.object(ca.platform, "system", return_value="Linux"):
            self.assertIsNone(ca._keychain_account())

    def test_switch_mirrors_new_auth_into_existing_keychain_item(self):
        self.write_auth(_auth_payload("acct-w", "w@x.com"))
        self.write_profile("personal", _auth_payload("acct-p", "personal@x.com"))

        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(ca, "_read_keychain_auth", return_value="{}"), \
                mock.patch.object(ca, "_write_keychain_auth", return_value=True) as write_kc:
            rc = self.run_quiet(ca.cmd_switch, "personal")

        self.assertEqual(rc, 0)
        write_kc.assert_called_once()
        written = json.loads(write_kc.call_args.args[0])
        self.assertEqual(written["tokens"]["account_id"], "acct-p")

    def test_switch_does_not_create_keychain_item_when_absent(self):
        self.write_auth(_auth_payload("acct-w", "w@x.com"))
        self.write_profile("personal", _auth_payload("acct-p", "personal@x.com"))

        with mock.patch.object(ca, "have", return_value=False), \
                mock.patch.object(ca, "_read_keychain_auth", return_value=None), \
                mock.patch.object(ca, "_write_keychain_auth", return_value=True) as write_kc:
            rc = self.run_quiet(ca.cmd_switch, "personal")

        self.assertEqual(rc, 0)
        write_kc.assert_not_called()

    def test_active_refresh_mirrors_into_keychain(self):
        self.write_auth(_auth_payload("acct-w", "w@x.com"))
        self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000}), "refresh_token": "rt-new"}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)), \
                mock.patch.object(ca, "_read_keychain_auth", return_value="{}"), \
                mock.patch.object(ca, "_write_keychain_auth", return_value=True) as write_kc:
            rc = self.run_quiet(ca.cmd_refresh, None)
        self.assertEqual(rc, 0)
        self.assertTrue(write_kc.called)
        written = json.loads(write_kc.call_args.args[0])
        self.assertEqual(written["tokens"]["refresh_token"], "rt-new")

    def test_refreshing_inactive_profile_never_touches_keychain(self):
        # auth.json is a different account than the profile being refreshed.
        self.write_auth(_auth_payload("acct-p", "p@x.com"))
        self.write_profile("work", _auth_payload("acct-w", "w@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000})}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)), \
                mock.patch.object(ca, "_read_keychain_auth", return_value="{}"), \
                mock.patch.object(ca, "_write_keychain_auth", return_value=True) as write_kc:
            self.run_quiet(ca.cmd_refresh, "work")
        write_kc.assert_not_called()

    def test_refresh_profile_active_syncback_mirrors_into_keychain(self):
        # Direction here is profile → active: "work" is refreshed from its own
        # saved refresh_token, then folded into the currently-active auth.json
        # (same account). Those rotated tokens must land in the keychain too.
        self.write_auth(_auth_payload("acct-w", "w@x.com"))
        self.write_profile("work", _auth_payload("acct-w", "w@x.com", refresh_token="rt-old"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000}), "refresh_token": "rt-new"}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)), \
                mock.patch.object(ca, "_read_keychain_auth", return_value="{}"), \
                mock.patch.object(ca, "_write_keychain_auth", return_value=True) as write_kc:
            rc = self.run_quiet(ca.cmd_refresh, "work")

        self.assertEqual(rc, 0)
        write_kc.assert_called_once()
        written = json.loads(write_kc.call_args.args[0])
        self.assertEqual(written["tokens"]["refresh_token"], "rt-new")

    def test_read_active_claims_prefers_keychain_over_auth_json(self):
        # auth.json says acct-file, keychain says acct-kc → codex uses keychain.
        self.write_auth(_auth_payload("acct-file", "file@x.com"))
        kc_secret = json.dumps(_auth_payload("acct-kc", "keychain@x.com"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=kc_secret):
            claims = ca._read_active_claims()
        self.assertEqual(claims["email"], "keychain@x.com")

    def test_read_active_claims_falls_back_to_auth_json(self):
        self.write_auth(_auth_payload("acct-file", "file@x.com"))
        with mock.patch.object(ca, "_read_keychain_auth", return_value=None):
            claims = ca._read_active_claims()
        self.assertEqual(claims["email"], "file@x.com")


class MainDispatchTests(_CodexHomeMixin):
    def test_main_routes_refresh_and_sync(self):
        with mock.patch.object(ca, "cmd_refresh", return_value=0) as refresh, \
                mock.patch.object(ca, "cmd_sync", return_value=0) as sync:
            self.assertEqual(ca.main(["refresh", "work"]), 0)
            refresh.assert_called_once_with("work")
            self.assertEqual(ca.main(["refresh"]), 0)
            refresh.assert_called_with(None)
            self.assertEqual(ca.main(["sync"]), 0)
            sync.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
