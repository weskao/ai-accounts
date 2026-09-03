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
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ai_accounts import _present as present
from ai_accounts import autoswitch as aw
from ai_accounts import claude_accounts as cla
from ai_accounts import codex_accounts as coa
from ai_accounts import gemini_accounts as gea
from ai_accounts import grok_accounts as gra
from ai_accounts._utils import GREEN

SENTINEL_ACCESS = "SENTINEL_LEAK_ACCESS_xyz"
SENTINEL_REFRESH = "SENTINEL_LEAK_REFRESH_xyz"

# Narrow-mode fixtures. The budget every narrow line must fit, a store path
# long enough to overflow it (72 columns as printed), and a picker list whose
# entries do too — all placeholders, nothing copied from a real terminal.
_NARROW_COLUMNS = present.NARROW_WIDTH
_LONG_STORE_PATH = "/home/example-user/.ai-accounts/codex/accounts/work-laptop.json"
_PICKER_ITEMS = [
    ("work-laptop-secondary", "expires in 5h 12m"),
    ("測試設定檔", "expired 2026-01-01"),
]


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

    def test_remove_picker_cancel_uses_remove_wording_not_switch(self) -> None:
        """cmd_remove_interactive shares choose_profile/choose_and_run with switch —
        it must print its own "Remove cancelled." on Ctrl-C, not switch's wording."""
        for label, module, setup in _PICKER_CASES:
            with self.subTest(tool=label), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {}, clear=False):
                setup(Path(tmp))
                with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
                    rc, text = _run(module.cmd_remove_interactive)
                clean = present._ANSI_RE.sub("", text)
                self.assertIn(f"Choose {label} profile:", clean)
                self.assertIn("Remove cancelled.", clean)
                self.assertNotIn("Switch cancelled.", clean)
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

    def test_success_panel_uses_shared_success_detail_panel_sequence(self) -> None:
        text = _capture(
            present.success_panel,
            "Refreshed Grok profile",
            "work",
            ["Account       : person@example.test"],
            title="Profile: work",
            details=("(same account is active)",),
        )
        clean = present._ANSI_RE.sub("", text)
        self.assertTrue(text.startswith(f"{GREEN}✅ "))
        self.assertIn("(same account is active)", clean)
        self.assertIn("Profile: work", clean)


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


class VisibleLenWideCharTests(unittest.TestCase):
    def test_cjk_cells_keep_table_borders_aligned(self):
        rows = [
            {"account": "測試 <user@example.com>"},
            {"account": "Test <user@example.com>"},
        ]
        out = io.StringIO()
        with redirect_stdout(out):
            present.accounts_table(rows, [("ACCOUNT", "account")])
        widths = {
            present.visible_len(line) for line in out.getvalue().splitlines()
        }
        self.assertEqual(len(widths), 1, out.getvalue())

    def test_combining_marks_add_no_width(self):
        self.assertEqual(present.visible_len("é"), 1)


# ── layout: auto/wide/narrow (new in this change) ───────────────────────────
#
# `tests/conftest.py`'s autouse fixture pins every test's ambient terminal to
# wide and clears the layout cache before/after each test, so nothing below
# needs to defend against ambient COLUMNS itself — only the tests that
# specifically probe width detection or the auto-resolver override that pin
# with their own explicit mocks/env, scoped to just that test.


class TerminalWidthTests(unittest.TestCase):
    """`terminal_width()` must never invent a width — genuinely undetectable
    (no COLUMNS, no real tty on any of the three streams) means ``None``."""

    def test_columns_env_wins_over_device_detection(self) -> None:
        with mock.patch.dict(os.environ, {"COLUMNS": "123"}, clear=False):
            self.assertEqual(present.terminal_width(), 123)

    def test_a_non_numeric_columns_value_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"COLUMNS": "not-a-number"}, clear=False), \
                mock.patch("os.get_terminal_size", side_effect=OSError):
            self.assertIsNone(present.terminal_width())

    def test_a_zero_or_negative_columns_value_is_ignored(self) -> None:
        for raw in ("0", "-40"):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"COLUMNS": raw}, clear=False), \
                        mock.patch("os.get_terminal_size", side_effect=OSError):
                    self.assertIsNone(present.terminal_width())

    def test_falls_back_to_device_size_when_columns_is_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLUMNS", None)
            with mock.patch(
                "os.get_terminal_size",
                return_value=os.terminal_size((77, 24)),
            ):
                self.assertEqual(present.terminal_width(), 77)

    def test_none_when_columns_unset_and_no_stream_is_a_real_terminal(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLUMNS", None)
            with mock.patch("os.get_terminal_size", side_effect=OSError):
                self.assertIsNone(present.terminal_width())


class LayoutModeResolverTests(unittest.TestCase):
    """`layout_mode()`'s full decision table: explicit override > configured
    wide/narrow > width-based auto-resolution > the undetectable-width
    fallback. Each test resets the cache first/after so it never leaks into a
    sibling test or into `tests/conftest.py`'s own wide pin."""

    def setUp(self) -> None:
        present.reset_layout_cache()
        self.addCleanup(present.reset_layout_cache)

    def _with_config(self, layout: str):
        return mock.patch.object(
            aw, "load_config", return_value={**aw.DEFAULTS, "layout": layout}
        )

    def test_width_59_resolves_to_narrow(self) -> None:
        with self._with_config("auto"), mock.patch.object(present, "terminal_width", return_value=59):
            self.assertEqual(present.layout_mode(), "narrow")

    def test_width_60_the_exact_boundary_resolves_to_wide(self) -> None:
        with self._with_config("auto"), mock.patch.object(present, "terminal_width", return_value=60):
            self.assertEqual(present.layout_mode(), "wide")

    def test_configured_wide_or_narrow_beats_width_detection(self) -> None:
        with self._with_config("wide"), mock.patch.object(present, "terminal_width", return_value=10):
            self.assertEqual(present.layout_mode(), "wide")
        present.reset_layout_cache()
        with self._with_config("narrow"), mock.patch.object(present, "terminal_width", return_value=999):
            self.assertEqual(present.layout_mode(), "narrow")

    def test_auto_resolves_via_width_both_directions(self) -> None:
        with self._with_config("auto"), mock.patch.object(present, "terminal_width", return_value=200):
            self.assertEqual(present.layout_mode(), "wide")
        present.reset_layout_cache()
        with self._with_config("auto"), mock.patch.object(present, "terminal_width", return_value=20):
            self.assertEqual(present.layout_mode(), "narrow")

    def test_undetectable_width_resolves_to_wide(self) -> None:
        with self._with_config("auto"), mock.patch.object(present, "terminal_width", return_value=None):
            self.assertEqual(present.layout_mode(), "wide")

    def test_override_argument_short_circuits_everything(self) -> None:
        with self._with_config("narrow"), mock.patch.object(present, "terminal_width", return_value=999):
            self.assertEqual(present.layout_mode(override="wide"), "wide")
        present.reset_layout_cache()
        with self._with_config("wide"), mock.patch.object(present, "terminal_width", return_value=10):
            self.assertEqual(present.layout_mode(override="narrow"), "narrow")

    def test_an_override_call_never_poisons_the_cache_for_other_callers(self) -> None:
        # A caller passing an explicit override must not corrupt what the
        # NEXT, override-less caller in the same process sees.
        with self._with_config("wide"), mock.patch.object(present, "terminal_width", return_value=200):
            # First call with no override at all: caches "wide".
            self.assertEqual(present.layout_mode(), "wide")
            # A second, unrelated caller passing an override gets its own
            # answer straight back, untouched by the cache...
            self.assertEqual(present.layout_mode(override="narrow"), "narrow")
            # ...and a THIRD, override-less caller still sees the original
            # cached "wide", not "narrow" leaking in from the override call.
            self.assertEqual(present.layout_mode(), "wide")

    def test_cache_is_read_once_per_reset(self) -> None:
        with self._with_config("wide"):
            self.assertEqual(present.layout_mode(), "wide")
            # Config changes without a reset: the stale cached answer wins.
            with self._with_config("narrow"):
                self.assertEqual(present.layout_mode(), "wide")
            # Only after an explicit reset is the new config read.
            present.reset_layout_cache()
            self.assertEqual(present.layout_mode(), "wide")  # outer patch restored
        present.reset_layout_cache()
        with self._with_config("narrow"):
            self.assertEqual(present.layout_mode(), "narrow")

    def test_configured_auto_re_resolves_the_width_on_every_call(self) -> None:
        """The regression the cache split exists for. ``config_menu.run_menu``
        renders a frame per keystroke for the life of an interactive session,
        so a terminal resized mid-session has to change the answer — caching
        the RESOLVED mode froze the first frame's answer forever. Two calls,
        two widths, one process, no reset in between."""
        with self._with_config("auto"):
            with mock.patch.object(present, "terminal_width", return_value=200):
                self.assertEqual(present.layout_mode(), "wide")
            with mock.patch.object(present, "terminal_width", return_value=30):
                self.assertEqual(present.layout_mode(), "narrow")
            with mock.patch.object(present, "terminal_width", return_value=200):
                self.assertEqual(present.layout_mode(), "wide")

    def test_auto_reads_the_config_once_but_measures_the_width_every_time(self) -> None:
        """Both halves of the split at once: the expensive disk read stays
        cached (what ``test_cache_is_read_once_per_reset`` guards), the cheap
        width syscall does not."""
        with self._with_config("auto") as loader, mock.patch.object(
            present, "terminal_width", side_effect=[200, 30]
        ) as width:
            self.assertEqual(present.layout_mode(), "wide")
            self.assertEqual(present.layout_mode(), "narrow")
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(width.call_count, 2)

    def test_override_auto_re_resolves_by_width_without_reading_the_config(self) -> None:
        """``config_menu.render`` passes the UNCOMMITTED ``values["layout"]``
        as the override: cycling that row to ``auto`` must live-preview the
        width-derived answer, not whatever is still on disk."""
        with self._with_config("narrow") as loader:
            with mock.patch.object(present, "terminal_width", return_value=200):
                self.assertEqual(present.layout_mode("auto"), "wide")
            with mock.patch.object(present, "terminal_width", return_value=30):
                self.assertEqual(present.layout_mode("auto"), "narrow")
            loader.assert_not_called()

    def test_an_auto_override_call_never_poisons_the_cache_for_other_callers(self) -> None:
        with self._with_config("wide"), mock.patch.object(present, "terminal_width", return_value=30):
            self.assertEqual(present.layout_mode(), "wide")
            self.assertEqual(present.layout_mode("auto"), "narrow")
            self.assertEqual(present.layout_mode(), "wide")


class NarrowPreservesEveryColumnTests(unittest.TestCase):
    """The widest real table (agy/gemini: 11 columns, 5 optional) — wide mode
    drops an optional column outright when every row renders "—" for it;
    narrow must show every column regardless, since a stacked card has the
    room a fixed-width table does not."""

    def _row(self) -> dict[str, str]:
        # Every OPTIONAL column dashed out (the trigger for wide's drop);
        # every other column carries a real, distinguishable value.
        row = {key: "—" for _, key in gea._TABLE_COLUMNS}
        for _, key in gea._TABLE_COLUMNS:
            if key not in gea._OPTIONAL_COLUMNS:
                row[key] = key  # non-optional value distinct per column
        row["profile"] = "work"
        row["status"] = "active"
        return row

    def test_narrow_keeps_every_column_even_when_all_optional_ones_are_all_dash(self) -> None:
        row = self._row()
        ident_key, state_key = gea._TABLE_COLUMNS[0][1], "status"
        out = _capture(
            present.accounts_table,
            [row],
            gea._TABLE_COLUMNS,
            optional_columns=gea._OPTIONAL_COLUMNS,
            align_keys=gea._ALIGN_KEYS,
            mode="narrow",
        )
        # Every header except the identity/state columns prints as its own
        # "LABEL  value" card line (see `_present._accounts_cards`); identity
        # and state print as the card's bare title/trailing text instead, by
        # design, in both modes — so their headers are never literal text and
        # are checked via their values below instead.
        for header, key in gea._TABLE_COLUMNS:
            if key in (ident_key, state_key):
                continue
            with self.subTest(header=header):
                self.assertIn(header, out)
        self.assertIn("work", out)
        self.assertIn("active", out)
        # All 5 optional columns are present with their real ("—") value —
        # nothing got dropped the way wide mode would drop it.
        self.assertEqual(out.count("—"), len(gea._OPTIONAL_COLUMNS))

    def test_wide_mode_drops_the_same_all_dash_optional_columns(self) -> None:
        # Contrast case, same input: wide mode's existing (unchanged)
        # optional-column-hiding behavior removes them entirely.
        row = self._row()
        out = _capture(
            present.accounts_table,
            [row],
            gea._TABLE_COLUMNS,
            optional_columns=gea._OPTIONAL_COLUMNS,
            align_keys=gea._ALIGN_KEYS,
            mode="wide",
        )
        for header, key in gea._TABLE_COLUMNS:
            if key not in gea._OPTIONAL_COLUMNS:
                continue
            with self.subTest(header=header):
                self.assertNotIn(header, out)


def _load_head_present():
    """The pre-this-change ``_present`` module, loaded from git history, so
    wide-mode output can be diffed against it byte-for-byte. Skips (rather
    than fails) when git history/the old file genuinely isn't available —
    this is an extra regression lock, not a substitute for the tests above."""
    import importlib.util
    import subprocess as sp

    try:
        old_source = sp.run(
            ["git", "show", "HEAD:src/ai_accounts/_present.py"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (sp.CalledProcessError, OSError) as exc:
        raise unittest.SkipTest(f"git show HEAD:_present.py unavailable: {exc}")
    if "def layout_mode" in old_source:
        raise unittest.SkipTest("HEAD already carries the layout feature — nothing to diff against")

    # The temp file is only needed for `exec_module` below — once the module
    # is compiled and loaded into memory, the source file backing it is not
    # read again, so the directory is cleaned up before returning rather than
    # leaked for the rest of the test run.
    with tempfile.TemporaryDirectory(prefix="present_head_") as tmp_dir:
        tmp_file = Path(tmp_dir) / "_present_head.py"
        tmp_file.write_text(old_source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("ai_accounts._present_head", tmp_file)
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "ai_accounts"  # so its `from ._utils import ...` resolves
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    return module


class WideModeUnchangedTests(unittest.TestCase):
    """Wide is byte-for-byte the pre-layout-feature output: fixed synthetic
    rows rendered through the CURRENT ``_present`` in ``mode="wide"`` must
    match the SAME call against the module as it existed at HEAD (obtained
    via ``git show``, since HEAD predates ``mode`` existing at all)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.old = _load_head_present()

    def test_accounts_table_wide_output_is_unchanged_no_optional_columns(self) -> None:
        columns = [
            ("PROFILE", "profile"),
            ("ACCOUNT", "account"),
            ("PLAN", "plan"),
            ("ID", "account_id"),
            ("5H USED", "usage_5h"),
            ("1W USED", "usage_1week"),
            ("UPDATED", "usage_updated"),
            ("AUTH", "expires"),
            ("STATE", "status"),
        ]
        rows = [
            {
                "profile": "work",
                "account": "Test <user@example.com>",
                "plan": "pro",
                "account_id": "acct-1",
                "usage_5h": "12%",
                "usage_1week": "34%",
                "usage_updated": "2m ago",
                "expires": "5h",
                "status": "active",
            },
            {
                "profile": "spare",
                "account": "測試 <user@example.com>",
                "plan": "free",
                "account_id": "—",
                "usage_5h": "—",
                "usage_1week": "—",
                "usage_updated": "—",
                "expires": "expired",
                "status": "expired",
            },
        ]
        old_out = _capture(
            self.old.accounts_table, rows, columns, align_keys=("usage_5h", "usage_1week")
        )
        new_out = _capture(
            present.accounts_table,
            rows,
            columns,
            align_keys=("usage_5h", "usage_1week"),
            mode="wide",
        )
        self.assertEqual(old_out, new_out)

    def test_accounts_table_wide_output_is_unchanged_with_optional_columns(self) -> None:
        rows = [
            {"profile": "work", "account": "user@example.com", "account_id": "—", "status": "active"},
            {"profile": "spare", "account": "second@example.com", "account_id": "id-2", "status": "active"},
        ]
        columns = [
            ("PROFILE", "profile"),
            ("ACCOUNT", "account"),
            ("ID", "account_id"),
            ("STATE", "status"),
        ]
        old_out = _capture(
            self.old.accounts_table, rows, columns, optional_columns={"account_id"}
        )
        new_out = _capture(
            present.accounts_table,
            rows,
            columns,
            optional_columns={"account_id"},
            mode="wide",
        )
        self.assertEqual(old_out, new_out)

    def test_panel_wide_output_is_unchanged(self) -> None:
        old_out = _capture(self.old.panel, "Profile: work", ["Account       : user@example.com"])
        new_out = _capture(
            present.panel, "Profile: work", ["Account       : user@example.com"], mode="wide"
        )
        self.assertEqual(old_out, new_out)

    def test_success_panel_wide_output_is_unchanged(self) -> None:
        old_out = _capture(
            self.old.success_panel,
            "Saved profile",
            "work",
            ["Account       : user@example.com"],
            title="Profile: work",
            details=("(same account is active)",),
        )
        new_out = _capture(
            present.success_panel,
            "Saved profile",
            "work",
            ["Account       : user@example.com"],
            title="Profile: work",
            details=("(same account is active)",),
            mode="wide",
        )
        self.assertEqual(old_out, new_out)

    def test_ok_wide_output_is_unchanged(self) -> None:
        for action, name, bold in (
            ("Saved Codex profile", "work", True),
            ("Switched to Claude profile", "測試", False),
            ("All 3 profile(s) refreshed.", None, True),
        ):
            with self.subTest(action=action):
                old_out = _capture(self.old.ok, action, name, bold=bold)
                new_out = _capture(present.ok, action, name, bold=bold, mode="wide")
                self.assertEqual(old_out, new_out)

    def test_success_panel_detail_lines_wide_output_is_unchanged(self) -> None:
        """The long-path detail line specifically — the one narrow mode now
        squeezes — must still print verbatim in wide mode."""
        details = (f"→ {_LONG_STORE_PATH}", "(same account is active)")
        old_out = _capture(
            self.old.success_panel,
            "Saved Codex profile",
            "work",
            ["Account       : user@example.com"],
            title="Profile: work",
            details=details,
        )
        new_out = _capture(
            present.success_panel,
            "Saved Codex profile",
            "work",
            ["Account       : user@example.com"],
            title="Profile: work",
            details=details,
            mode="wide",
        )
        self.assertEqual(old_out, new_out)
        self.assertIn(_LONG_STORE_PATH, present.strip_ansi(new_out))

    def test_choose_profile_wide_output_is_unchanged(self) -> None:
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            old_out = _capture(self.old.choose_profile, "a Codex", _PICKER_ITEMS)
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            new_out = _capture(present.choose_profile, "a Codex", _PICKER_ITEMS)
        self.assertEqual(old_out, new_out)


class NarrowSurfaceBudgetTests(unittest.TestCase):
    """The three surfaces that print OUTSIDE the panel box — the ``ok``
    headline, ``success_panel``'s detail lines, and the ``choose_profile``
    picker. The box wraps its own content, so these three were the whole
    narrow-mode gap: measured at 46-53, 72 and 56 columns respectively against
    a 40-column budget. Widths are measured with ``visible_len`` (ANSI-stripped
    and East-Asian-width aware), never ``len``."""

    def setUp(self) -> None:
        for patcher in (
            mock.patch.dict(os.environ, {"COLUMNS": str(_NARROW_COLUMNS)}),
            # Pinned rather than read from disk: no test may depend on (or go
            # near) the developer's real ~/.ai-accounts/config.json.
            mock.patch.object(aw, "load_config", return_value={**aw.DEFAULTS, "layout": "auto"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        present.reset_layout_cache()
        self.addCleanup(present.reset_layout_cache)

    def assertFitsNarrow(self, text: str) -> None:
        for line in text.splitlines():
            self.assertLessEqual(
                present.visible_len(line), _NARROW_COLUMNS, f"overflowing line: {line!r}"
            )

    def test_narrow_mode_is_actually_active(self) -> None:
        self.assertEqual(present.layout_mode(), "narrow")

    def test_ok_headline_wraps_to_the_narrow_budget(self) -> None:
        for action, name in (
            ("Saved Codex profile", "work-laptop-secondary"),
            ("Switched to Antigravity profile", "spare"),
            ("All 3 profile(s) refreshed.", None),
            ("已切換 Codex 設定檔", "測試設定檔"),
        ):
            with self.subTest(action=action):
                self.assertFitsNarrow(_capture(present.ok, action, name))

    def test_success_panel_details_fit_and_keep_the_filename(self) -> None:
        text = _capture(
            present.success_panel,
            "Saved Codex profile",
            "work",
            ["Account       : user@example.com"],
            title="Profile: work",
            details=(f"→ {_LONG_STORE_PATH}", "(same account is active)"),
        )
        self.assertFitsNarrow(text)
        clean = present.strip_ansi(text)
        # The identifying tail survives the squeeze; the middle is elided.
        self.assertIn("work-laptop.json", clean)
        self.assertNotIn(_LONG_STORE_PATH, clean)
        self.assertIn("(same account is active)", clean)

    def test_choose_profile_header_and_entries_fit(self) -> None:
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            text = _capture(present.choose_profile, "an Antigravity", _PICKER_ITEMS)
        self.assertFitsNarrow(text)
        clean = present.strip_ansi(text)
        self.assertIn("1)", clean)
        self.assertIn("2)", clean)

    def test_wide_mode_leaves_all_three_surfaces_untouched(self) -> None:
        """Same three calls with the layout pinned wide still overflow 40
        columns — proving the narrow assertions above measure the new
        behaviour, not a change that silently applies everywhere."""
        with mock.patch.object(aw, "load_config", return_value={**aw.DEFAULTS, "layout": "wide"}):
            present.reset_layout_cache()
            headline = _capture(present.ok, "Saved Codex profile", "work-laptop-secondary")
            with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
                picker = _capture(present.choose_profile, "an Antigravity", _PICKER_ITEMS)
        for text in (headline, picker):
            widest = max(present.visible_len(line) for line in text.splitlines())
            self.assertGreater(widest, _NARROW_COLUMNS)


class WideGoldenFixtureTests(unittest.TestCase):
    """Wide output matches a checked-in golden, byte for byte.

    ``WideModeUnchangedTests`` above diffs against ``git show HEAD``, which
    stops being meaningful the moment this feature is committed — HEAD then
    carries the feature and it skips itself. That leaves the "wide is
    unchanged" guarantee with nothing enforcing it, so this pins the same
    guarantee to a fixture that survives the merge. Regenerate the file
    deliberately (and review the diff) if wide output is ever meant to move.
    """

    GOLDEN = Path(__file__).resolve().parent / "fixtures" / "wide_golden.txt"

    COLUMNS = [
        ("PROFILE", "profile"), ("ACCOUNT", "account"), ("PLAN", "plan"),
        ("ID", "account_id"), ("5H USED", "usage_5h"), ("1W USED", "usage_1week"),
        ("UPDATED", "usage_updated"), ("AUTH", "expires"), ("STATE", "status"),
    ]
    ROWS = [
        {"profile": "work", "account": "Test <user@example.com>", "plan": "pro",
         "account_id": "acct-1", "usage_5h": "12%", "usage_1week": "34%",
         "usage_updated": "2m ago", "expires": "5h", "status": "active"},
        {"profile": "spare", "account": "測試 <user@example.com>", "plan": "free",
         "account_id": "—", "usage_5h": "—", "usage_1week": "—",
         "usage_updated": "—", "expires": "expired", "status": "expired"},
    ]

    def test_wide_output_matches_the_checked_in_golden(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            present.accounts_table(
                [dict(row) for row in self.ROWS], self.COLUMNS, mode="wide"
            )
            present.panel(
                "Codex Login Status",
                ["Logged in", "Account: user@example.com"],
                mode="wide",
            )
            present.ok("Switched Codex account", "work", mode="wide")
            present.success_panel(
                "Saved Codex profile", "work", ["Account: user@example.com"],
                title="Current Auth Claims",
                details=["→ /tmp/accounts/work.json"], mode="wide",
            )
        self.assertEqual(
            buffer.getvalue(),
            self.GOLDEN.read_text(encoding="utf-8"),
            "wide output drifted from tests/fixtures/wide_golden.txt",
        )
