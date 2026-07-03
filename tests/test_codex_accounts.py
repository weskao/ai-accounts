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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import codex_accounts as ca


def _jwt(payload: dict) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _auth_payload(
    account_id: str, email: str, *, refresh_token: str = "rt-old", expires_in: int = 10 * 24 * 3600
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
        "last_refresh": "2026-01-01T00:00:00.000000Z",
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

    def run_quiet(self, fn, *args) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return fn(*args)


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
            rc = self.run_quiet(ca.cmd_refresh, "--all")
        self.assertEqual(rc, 1)  # at least one failure

    def test_refresh_all_success(self):
        self.write_profile("a", _auth_payload("acct-a", "a@x.com"))
        new = {"access_token": _jwt({"exp": int(time.time()) + 864000})}
        with mock.patch.object(ca, "_oauth_refresh", return_value=(new, None)):
            rc = self.run_quiet(ca.cmd_refresh, "--all")
        self.assertEqual(rc, 0)

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
