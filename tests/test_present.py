"""Tests for the shared presentation layer (``_present.py``) adopted by all
four account CLIs (codex/claude/gemini(agy)/grok-accounts).

Two concerns:
  1. Sentinel-leak regression: unique sentinel tokens planted in fixture auth
     JSON must never reach stdout/stderr of who/list/save/switch.
  2. Shared-grammar consistency: the picker header/numbering, the cancel
     message, the "✅ " success prefix, and the table box-drawing/optional-
     column behavior must be identical across all four tools.

All filesystem access is redirected into a temp dir via each tool's env-var
override; keychain/subprocess/network calls are monkeypatched out — no
network, no touching the real ~/.ai_accounts. Run with ``uv run pytest
tests/test_present.py -q``.
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

from ai_accounts import _present as present
from ai_accounts import claude_accounts as cla
from ai_accounts import codex_accounts as coa
from ai_accounts import gemini_accounts as gea
from ai_accounts import grok_accounts as gra
from ai_accounts._utils import GREEN

SENTINEL_ACCESS = "SENTINEL_LEAK_ACCESS_xyz"
SENTINEL_REFRESH = "SENTINEL_LEAK_REFRESH_xyz"


def _future_ms() -> int:
    return int(time.time() * 1000) + 30 * 24 * 3600 * 1000


def _run(fn, *args, **kwargs) -> tuple[int, str]:
    """Call a cmd_* function, returning (exit_code, combined stdout+stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return rc, out.getvalue() + err.getvalue()


def _capture(fn, *args, **kwargs) -> str:
    """Call a print-only helper, returning captured stdout."""
    out = io.StringIO()
    with redirect_stdout(out):
        fn(*args, **kwargs)
    return out.getvalue()


class _NoLeakMixin:
    def assert_no_leak(self, text: str) -> None:
        self.assertNotIn(SENTINEL_ACCESS, text)
        self.assertNotIn(SENTINEL_REFRESH, text)


# ── 1. sentinel-leak tests ───────────────────────────────────────────────────
# who/save/switch/list are run end-to-end (no network, hermetic) and their
# combined output is scanned for the raw sentinel tokens planted in the fixture.

class CodexSentinelLeakTests(_NoLeakMixin, unittest.TestCase):
    def test_no_sentinel_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(home), "CODEX_ACCOUNT_DIR": str(home / "accounts")},
                clear=False,
            ):
                os.environ.pop("CODEX_AUTH_JSON", None)
                (home / "accounts").mkdir(parents=True)
                auth = {
                    "tokens": {
                        "access_token": SENTINEL_ACCESS,
                        "id_token": SENTINEL_ACCESS,
                        "refresh_token": SENTINEL_REFRESH,
                        "account_id": "acct-1",
                    },
                    "last_refresh": "2026-01-01T00:00:00.000000Z",
                }
                (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

                with mock.patch.object(coa, "_read_keychain_auth", return_value=None), \
                        mock.patch.object(coa, "have", return_value=False):
                    _, out1 = _run(coa.cmd_who)
                    _, out2 = _run(coa.cmd_save, "s1")
                    _, out3 = _run(coa.cmd_switch, "s1")
                    _, out4 = _run(coa.cmd_list, fetch_usage=False)
        self.assert_no_leak(out1 + out2 + out3 + out4)


class ClaudeSentinelLeakTests(_NoLeakMixin, unittest.TestCase):
    def test_no_sentinel_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "claude"
            (home / "accounts").mkdir(parents=True)
            with mock.patch.dict(
                os.environ,
                {"CLAUDE_CONFIG_DIR": str(home), "CLAUDE_ACCOUNT_DIR": str(home / "accounts")},
                clear=False,
            ):
                os.environ.pop("CLAUDE_CREDENTIALS_JSON", None)
                oauth = {
                    "accessToken": SENTINEL_ACCESS,
                    "refreshToken": SENTINEL_REFRESH,
                    "expiresAt": _future_ms(),
                    "scopes": ["user:profile"],
                    "subscriptionType": "pro",
                    "rateLimitTier": "default_claude_pro",
                }
                cla._creds_file().write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")

                with mock.patch.object(cla, "_keychain_account", return_value=None), \
                        mock.patch.object(cla, "have", return_value=False):
                    _, out1 = _run(cla.cmd_who)
                    _, out2 = _run(cla.cmd_save, "s1")
                    _, out3 = _run(cla.cmd_switch, "s1")
                    _, out4 = _run(cla.cmd_list, fetch_usage=False)
        self.assert_no_leak(out1 + out2 + out3 + out4)


class GeminiSentinelLeakTests(_NoLeakMixin, unittest.TestCase):
    """agy's active session lives behind a fake macOS-keyring secret — mock
    the keyring read/write/delete only; the real encode/decode round-trip
    (``_keyring_secret_from_auth`` / ``_auth_from_keyring_secret``) still runs."""

    def test_no_sentinel_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "antigravity"
            (home / "accounts").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"ANTIGRAVITY_HOME": str(home)}, clear=False):
                os.environ.pop("ANTIGRAVITY_ACCOUNT_DIR", None)
                os.environ.pop("ANTIGRAVITY_OAUTH_JSON", None)

                state: dict = {
                    "active": {
                        "access_token": SENTINEL_ACCESS,
                        "refresh_token": SENTINEL_REFRESH,
                        "token_type": "Bearer",
                        "expiry_date": _future_ms(),
                    }
                }

                def secret() -> str | None:
                    return gea._keyring_secret_from_auth(state["active"]) if state["active"] else None

                def write(text: str) -> bool:
                    value = json.loads(text)
                    if gea._keyring_secret_from_auth(value) is None:
                        return False
                    state["active"] = value
                    return True

                def delete() -> bool:
                    state["active"] = None
                    return True

                with mock.patch.object(gea, "_read_cli_keyring_secret", side_effect=secret), \
                        mock.patch.object(gea, "_write_cli_auth_text", side_effect=write), \
                        mock.patch.object(gea, "_delete_cli_auth", side_effect=delete):
                    _, out1 = _run(gea.cmd_who)
                    _, out2 = _run(gea.cmd_save, "s1")
                    _, out3 = _run(gea.cmd_switch, "s1")
                    _, out4 = _run(gea.cmd_list, fetch_usage=False)
        self.assert_no_leak(out1 + out2 + out3 + out4)


class GrokSentinelLeakTests(_NoLeakMixin, unittest.TestCase):
    def test_no_sentinel_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.dict(
                os.environ,
                {
                    "GROK_HOME": str(home / ".grok"),
                    "GROK_ACCOUNT_DIR": str(home / "accounts"),
                },
                clear=False,
            ):
                payload = {
                    "https://auth.x.ai::client": {
                        "auth_mode": "oidc",
                        "email": "person@example.test",
                        "first_name": "Person",
                        "principal_id": "principal-1",
                        "principal_type": "User",
                        "team_id": "team-1",
                        "create_time": "2030-01-01T03:04:05Z",
                        "expires_at": "2030-01-02T03:04:05Z",
                        "coding_data_retention_opt_out": True,
                        "refresh_token": SENTINEL_REFRESH,
                        "key": SENTINEL_ACCESS,
                    }
                }
                gra._write_json(gra._auth_file(), payload)

                _, out1 = _run(gra.cmd_who)
                _, out2 = _run(gra.cmd_save, "s1")
                _, out3 = _run(gra.cmd_switch, "s1")
                _, out4 = _run(gra.cmd_list)
        self.assert_no_leak(out1 + out2 + out3 + out4)


# ── 2. shared-grammar consistency tests ─────────────────────────────────────

def _codex_picker_home(home: Path) -> None:
    os.environ["CODEX_HOME"] = str(home / "codex")
    os.environ["CODEX_ACCOUNT_DIR"] = str(home / "codex" / "accounts")
    os.environ.pop("CODEX_AUTH_JSON", None)
    account_dir = home / "codex" / "accounts"
    account_dir.mkdir(parents=True)
    # Needs a real exp claim: codex's interactive picker filters to unexpired
    # profiles only, and an undecodable token has no expires_epoch at all.
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time()) + 86400})}.sig"
    auth = {"tokens": {"access_token": token, "refresh_token": "rt", "account_id": "a"}}
    (account_dir / "p1.json").write_text(json.dumps(auth), encoding="utf-8")


def _claude_picker_home(home: Path) -> None:
    os.environ["CLAUDE_CONFIG_DIR"] = str(home / "claude")
    os.environ["CLAUDE_ACCOUNT_DIR"] = str(home / "claude" / "accounts")
    os.environ.pop("CLAUDE_CREDENTIALS_JSON", None)
    account_dir = home / "claude" / "accounts"
    account_dir.mkdir(parents=True)
    oauth = {"accessToken": "at", "refreshToken": "rt", "expiresAt": _future_ms()}
    (account_dir / "p1.json").write_text(json.dumps(oauth), encoding="utf-8")


def _gemini_picker_home(home: Path) -> None:
    os.environ["ANTIGRAVITY_HOME"] = str(home / "antigravity")
    os.environ.pop("ANTIGRAVITY_ACCOUNT_DIR", None)
    os.environ.pop("ANTIGRAVITY_OAUTH_JSON", None)
    account_dir = home / "antigravity" / "accounts"
    account_dir.mkdir(parents=True)
    auth = {"access_token": "at", "refresh_token": "rt", "expiry_date": _future_ms()}
    (account_dir / "p1.json").write_text(json.dumps(auth), encoding="utf-8")


def _grok_picker_home(home: Path) -> None:
    os.environ["GROK_HOME"] = str(home / "grok")
    os.environ["GROK_ACCOUNT_DIR"] = str(home / "grok-accounts")
    account_dir = home / "grok-accounts"
    account_dir.mkdir(parents=True)
    payload = {
        "https://auth.x.ai::client": {
            "auth_mode": "oidc",
            "email": "a@example.test",
            "first_name": "A",
            "principal_id": "p1",
            "principal_type": "User",
            "team_id": "t1",
            "refresh_token": "rt",
            "key": "at",
        }
    }
    (account_dir / "p1.json").write_text(json.dumps(payload), encoding="utf-8")


_PICKER_CASES = [
    ("a Codex", coa, _codex_picker_home),
    ("a Claude", cla, _claude_picker_home),
    ("an Antigravity", gea, _gemini_picker_home),
    ("a Grok", gra, _grok_picker_home),
]


class PickerGrammarTests(unittest.TestCase):
    """(a) header + numbering and (b) KeyboardInterrupt cancellation must read
    identically across all four tools — grok previously mishandled the
    Ctrl-C case before adopting the shared ``_present.choose_profile``."""

    def test_header_numbering_and_cancel_shared_across_tools(self) -> None:
        for label, module, setup in _PICKER_CASES:
            with self.subTest(tool=label), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {}, clear=False):
                setup(Path(tmp))
                with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
                    rc, text = _run(module.cmd_switch_interactive)
                clean = present._ANSI_RE.sub("", text)
                self.assertIn(f"Choose {label} profile:", clean)
                self.assertIn("  1) ", clean)
                self.assertIn("Switch cancelled.", clean)
                self.assertEqual(rc, 1)


class OkGrammarTests(unittest.TestCase):
    """(c) every success line starts with the shared '✅ ' + GREEN prefix."""

    def test_ok_with_name_uses_shared_success_grammar(self) -> None:
        text = _capture(present.ok, "Saved Codex profile", "work")
        self.assertTrue(text.startswith(f"{GREEN}✅ "))
        self.assertIn("Saved Codex profile:", text)
        self.assertIn("work", text)

    def test_ok_without_name_has_no_trailing_colon(self) -> None:
        text = _capture(present.ok, "All 3 profile(s) refreshed.")
        self.assertTrue(text.startswith(f"{GREEN}✅ "))
        self.assertNotIn(":", text)


class TableGrammarTests(unittest.TestCase):
    """(d) box-drawing frame + optional-column hide/keep, (e) the all-optional
    all-empty guard never raises."""

    def test_renders_box_drawing_frame(self) -> None:
        text = _capture(present.accounts_table, [{"a": "1", "b": "y"}], [("A", "a"), ("B", "b")])
        self.assertIn("┌", text)
        self.assertIn("│", text)
        self.assertIn("└", text)

    def test_optional_column_hides_when_every_row_is_empty(self) -> None:
        rows = [{"a": "1", "b": "—"}, {"a": "2", "b": "—"}]
        text = _capture(present.accounts_table, rows, [("A", "a"), ("B", "b")], optional_columns={"b"})
        self.assertIn("A", text)
        self.assertNotIn("B", text)

    def test_optional_column_stays_when_any_row_has_data(self) -> None:
        rows = [{"a": "1", "b": "—"}, {"a": "2", "b": "val"}]
        text = _capture(present.accounts_table, rows, [("A", "a"), ("B", "b")], optional_columns={"b"})
        self.assertIn("B", text)
        self.assertIn("val", text)

    def test_all_optional_all_empty_returns_without_raising(self) -> None:
        rows = [{"a": "—", "b": "—"}]
        text = _capture(
            present.accounts_table, rows, [("A", "a"), ("B", "b")], optional_columns={"a", "b"}
        )
        self.assertEqual(text, "")


if __name__ == "__main__":
    unittest.main()
