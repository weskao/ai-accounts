"""opencode-accounts manages opencode's OWN credential store, read-only.

Every fixture is a throwaway opencode.db / auth.json under tmp_path holding
placeholder tokens; the real store is never touched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest import mock

import pytest

from ai_accounts import autoswitch, opencode_accounts as oa, opencode_usage
from ai_accounts._present import _ANSI_RE
from ai_accounts.usage_format import UsageWindow

EXE = "/usr/bin/opencode"


FUTURE_MS = 4_102_444_800_000  # 2100-01-01, opencode stores `expires` as epoch milliseconds


def _oauth(access: str, expires: int | float | None = FUTURE_MS) -> str:
    record = {"type": "oauth", "access": access, "refresh": "refresh-test"}
    if expires is not None:
        record["expires"] = expires
    return json.dumps(record)


# (id, integration_id, label, value, active)
ROWS = [
    ("cred-oa-1", "openai", "personal", _oauth("access-test-001"), 1),
    ("cred-oa-2", "openai", "work", _oauth("access-test-002"), 0),
    ("cred-an-1", "anthropic", "main", _oauth("access-test-003"), 1),
    ("cred-an-2", "anthropic", "", _oauth("access-test-004"), 0),
    ("cred-zen-1", "opencode", "zen", json.dumps({"type": "api", "key": "key-test-zen"}), 1),
]


def _make_db(path: Path, rows=ROWS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con:
        con.execute(
            "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT,"
            " value TEXT, connector_id TEXT, method_id TEXT, active INTEGER,"
            " time_created INTEGER, time_updated INTEGER)"
        )
        con.executemany(
            "INSERT INTO credential VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
            [(*row, n, n) for n, row in enumerate(rows)],
        )
        con.commit()


@pytest.fixture
def data(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "share" / "opencode"
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(directory))
    monkeypatch.setenv("OPENCODE_ACCOUNT_DIR", str(tmp_path / "accounts"))
    autoswitch.save_config({"enabled": True, "notify": "none", "switch_when_used_pct": 90})
    return directory


@pytest.fixture
def db(data) -> Path:
    path = data / "opencode.db"
    _make_db(path)
    return path


@pytest.fixture
def usage(monkeypatch):
    """opencode_usage.fetch_usage keyed by access token; records every call."""
    used_by_token: dict[str, int | None] = {}
    calls: list[tuple[str, dict]] = []

    def fake(integration, credential, **_kw):
        calls.append((integration, credential))
        if opencode_usage.normalize(integration) not in opencode_usage.QUOTA_INTEGRATIONS or credential.get("type") != "oauth":
            return None
        pct = used_by_token.get(credential["access"])
        return None if pct is None else UsageWindow(pct, None, 10_080)

    monkeypatch.setattr(opencode_usage, "fetch_usage", fake)
    return used_by_token, calls


@pytest.fixture
def run(monkeypatch):
    """subprocess.run stand-in: records argv, returncode per argv[1:3] (default 0)."""
    calls: list[list[str]] = []
    codes: dict[tuple[str, ...], int] = {}

    def fake(argv, *args, **kwargs):
        calls.append(list(argv))
        return mock.Mock(returncode=codes.get(tuple(argv[1:3]), 0), stdout="", stderr="opencode said no")

    monkeypatch.setattr(oa.shutil, "which", lambda name: EXE if name == "opencode" else None)
    monkeypatch.setattr(oa.subprocess, "run", fake)
    return calls, codes


def _main(capsys, *argv: str) -> tuple[int, str]:
    rc = oa.main(list(argv))
    out = capsys.readouterr()
    return rc, _ANSI_RE.sub("", out.out + out.err)


def _names(logins) -> list[str]:
    return [login.name for login in logins]


# ── store reading ────────────────────────────────────────────────────────────


def test_logins_come_from_the_db_with_unique_names(db):
    logins, legacy = oa._logins()
    assert not legacy
    assert _names(logins) == [
        "anthropic:main",
        "anthropic:cred-an-2",  # empty label falls back to the credential id
        "openai:personal",
        "openai:work",
        "opencode:zen",
    ]
    assert {login.name for login in logins if login.active} == {"anthropic:main", "openai:personal", "opencode:zen"}
    work = next(login for login in logins if login.name == "openai:work")
    assert (work.integration, work.cred_id, work.record["access"]) == ("openai", "cred-oa-2", "access-test-002")


def test_duplicate_labels_stay_unique(data):
    _make_db(data / "opencode.db", [
        ("cred-a", "openai", "dup", _oauth("access-test-a"), 1),
        ("cred-b", "openai", "dup", _oauth("access-test-b"), 0),
    ])
    assert _names(oa._logins()[0]) == ["openai:dup", "openai:cred-b"]


def test_mcp_server_credentials_are_not_listed(data):
    _make_db(data / "opencode.db", [
        ("cred-a", "openai", "main", _oauth("access-test-a"), 1),
        ("cred-m", "mcp_0000aaaa", "notion", _oauth("access-test-m"), 1),
    ])
    assert _names(oa._logins()[0]) == ["openai:main"]


def test_a_label_equal_to_another_id_never_shares_a_name(data):
    # A shared name would let `remove` log out the wrong credential.
    _make_db(data / "opencode.db", [
        ("cred-a", "openai", "cred-b", _oauth("access-test-a"), 1),
        ("cred-b", "openai", "", _oauth("access-test-b"), 0),
        ("cred-c", "openai", "bad\x1b[31m", _oauth("access-test-c"), 0),
    ])
    names = _names(oa._logins()[0])
    assert names == ["openai:cred-b", "openai:cred-b#2", "openai:cred-c"]
    assert len(set(names)) == len(names)


def test_a_timed_out_cli_exits_1(db, run, monkeypatch, capsys):
    def hang(argv, *args, **kwargs):
        raise oa.subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(oa.subprocess, "run", hang)
    rc, out = _main(capsys, "switch", "openai:work")
    assert rc == 1 and "timed out" in out


def test_xdg_data_home_locates_the_store(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert oa._data_dir() == tmp_path / "xdg" / "opencode"


def test_legacy_auth_json_lists_the_openai_login(data, usage, capsys):
    data.mkdir(parents=True)
    (data / "auth.json").write_text(
        json.dumps({"openai": json.loads(_oauth("access-test-001"))}), encoding="utf-8"
    )
    usage[0]["access-test-001"] = 40
    rc, out = _main(capsys, "list", "--json")
    assert rc == 0
    entries = json.loads(out)
    assert [(e["name"], e["active"], e["no_quota_api"]) for e in entries] == [("openai:default", True, False)]
    assert entries[0]["usage"]["window"]["percent"] == 40


def test_db_without_credential_table_falls_back_to_auth_json(data):
    data.mkdir(parents=True)
    with closing(sqlite3.connect(data / "opencode.db")) as con:
        con.execute("CREATE TABLE other (x)")
    (data / "auth.json").write_text(json.dumps({"openai": json.loads(_oauth("a"))}), encoding="utf-8")
    logins, legacy = oa._logins()
    assert legacy and _names(logins) == ["openai:default"]


def test_account_dir_honours_its_override(data, tmp_path):
    assert oa._account_dir() == tmp_path / "accounts"


# ── list / usage / who ───────────────────────────────────────────────────────


def test_list_json_groups_every_credential_and_reads_only_quota_rows(db, usage, run, capsys):
    before = db.read_bytes()
    used, calls = usage
    used.update({"access-test-001": 30, "access-test-002": 5, "access-test-003": 70})
    rc, out = _main(capsys, "list", "--json")
    assert rc == 0
    entries = {e["name"]: e for e in json.loads(out)}
    assert list(entries) == _names(oa._logins()[0])
    assert entries["openai:personal"]["active"] is True
    assert entries["openai:personal"]["integration"] == "openai"
    assert entries["openai:personal"]["usage"]["window"]["percent"] == 30
    assert entries["openai:work"]["active"] is False
    assert entries["opencode:zen"] == {
        "name": "opencode:zen", "integration": "opencode", "active": True, "usage": None, "no_quota_api": True,
    }
    assert "opencode" not in {integration for integration, _ in calls}  # Zen never probed
    assert db.read_bytes() == before
    assert run[0] == []


def test_api_key_login_on_a_quota_integration_has_no_quota_api(data, usage, capsys):
    _make_db(data / "opencode.db", [("cred-k", "openai", "key", json.dumps({"type": "api", "key": "k-test"}), 1)])
    rc, out = _main(capsys, "list", "--json")
    assert rc == 0
    assert [(e["name"], e["usage"], e["no_quota_api"]) for e in json.loads(out)] == [("openai:key", None, True)]
    assert usage[1] == []


def test_usage_json_lists_only_active_logins(db, usage, capsys):
    rc, out = _main(capsys, "usage", "--json")
    assert rc == 0
    assert [e["name"] for e in json.loads(out)] == ["anthropic:main", "openai:personal", "opencode:zen"]


def test_list_table_marks_active_and_dashes_quota_less_rows(db, usage, capsys):
    usage[0]["access-test-001"] = 42
    rc, out = _main(capsys, "list")
    assert rc == 0
    assert "openai:personal" in out and "ACTIVE" in out and "42%" in out
    zen_line = next(line for line in out.splitlines() if "opencode:zen" in line)
    assert "—" in zen_line


def test_list_marks_other_login_of_the_active_account_same_acct(data, usage, capsys):
    def oauth(access: str, account: str) -> str:
        return json.dumps({"type": "oauth", "access": access, "metadata": {"accountID": account}})

    _make_db(data / "opencode.db", [
        ("cred-1", "openai", "personal", oauth("access-test-001", "acct-test-1"), 1),
        ("cred-2", "openai", "again", oauth("access-test-002", "acct-test-1"), 0),
        ("cred-3", "openai", "work", oauth("access-test-003", "acct-test-2"), 0),
        # Same account id under another integration is not the same login.
        ("cred-4", "anthropic", "main", oauth("access-test-004", "acct-test-1"), 1),
    ])
    rc, out = _main(capsys, "list")
    assert rc == 0
    state = {name: next(line for line in out.splitlines() if name in line)
             for name in ("openai:personal", "openai:again", "openai:work", "anthropic:main")}
    assert "ACTIVE" in state["openai:personal"]
    assert "SAME ACCT" in state["openai:again"]
    assert "SAME ACCT" not in state["openai:work"]
    assert "ACTIVE" in state["anthropic:main"]


def test_list_without_any_store_is_empty(data, capsys):
    rc, out = _main(capsys, "list", "--json")
    assert (rc, json.loads(out)) == (0, [])


def test_who_shows_the_active_login_per_integration(db, usage, capsys):
    before = db.read_bytes()
    rc, out = _main(capsys, "who")
    assert rc == 0
    assert "openai:personal" in out and "anthropic:main" in out
    assert "openai:work" not in out
    assert db.read_bytes() == before


def test_who_without_logins_exits_1(data, capsys):
    rc, out = _main(capsys, "current")
    assert rc == 1
    assert "opencode auth login" in out


# ── switch / remove / save / login-switch ────────────────────────────────────


def test_switch_delegates_to_opencode_auth_switch(db, run, capsys):
    before = db.read_bytes()
    rc, out = _main(capsys, "switch", "openai:work")
    assert rc == 0
    assert run[0] == [[EXE, "auth", "switch", "openai", "cred-oa-2"]]
    assert "openai:work" in out
    assert db.read_bytes() == before


def test_switch_failure_exits_1(db, run, capsys):
    run[1][("auth", "switch")] = 3
    rc, out = _main(capsys, "switch", "openai:work")
    assert rc == 1
    assert "opencode said no" in out


@pytest.mark.parametrize("cred_id", ["-rf", "x&calc", "a b"])
def test_unsafe_ids_never_reach_argv(data, run, cred_id, capsys):
    _make_db(data / "opencode.db", [(cred_id, "openai", "evil", _oauth("access-test-e"), 0)])
    rc, out = _main(capsys, "remove", "openai:evil")
    assert rc == 1 and "unsafe" in out
    assert run[0] == []


def test_switch_unknown_name_never_runs_opencode(db, run, capsys):
    rc, out = _main(capsys, "switch", "openai:nobody")
    assert rc == 1
    assert run[0] == []
    assert "openai:nobody" in out


def test_switch_without_opencode_on_path_exits_1(db, run, monkeypatch, capsys):
    monkeypatch.setattr(oa.shutil, "which", lambda name: None)
    rc, _ = _main(capsys, "switch", "openai:work")
    assert rc == 1
    assert run[0] == []


def test_switch_on_a_legacy_install_is_unsupported(data, run, capsys):
    data.mkdir(parents=True)
    (data / "auth.json").write_text(json.dumps({"openai": json.loads(_oauth("a"))}), encoding="utf-8")
    rc, out = _main(capsys, "switch", "openai:default")
    assert rc == 1
    assert run[0] == []
    assert "auth.json" in out


def test_remove_delegates_to_opencode_auth_logout(db, run, capsys):
    before = db.read_bytes()
    rc, _ = _main(capsys, "remove", "anthropic:cred-an-2")
    assert rc == 0
    assert run[0] == [[EXE, "auth", "logout", "anthropic", "cred-an-2"]]
    assert db.read_bytes() == before


def test_save_explains_opencode_keeps_every_login(db, run, capsys):
    rc, out = _main(capsys, "save", "anything")
    assert rc == 0
    assert "opencode auth login" in out
    assert run[0] == []


def test_login_switch_runs_opencode_auth_login(db, run, capsys):
    rc, _ = _main(capsys, "login-switch", "new")
    assert rc == 0
    assert run[0] == [[EXE, "auth", "login"]]


def test_login_switch_without_opencode_exits_1(db, run, monkeypatch, capsys):
    monkeypatch.setattr(oa.shutil, "which", lambda name: None)
    rc, _ = _main(capsys, "login-switch", "new")
    assert rc == 1
    assert run[0] == []


# ── refresh / sync: HARD RULE, never touch a token ───────────────────────────


@pytest.mark.parametrize("argv", [("refresh", "--all"), ("refresh",), ("refresh", "openai:work"), ("sync",)])
def test_refresh_and_sync_are_harmless_no_ops(db, usage, run, argv, capsys):
    before = db.read_bytes()
    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")) as urlopen:
        rc, out = _main(capsys, *argv)
    assert rc == 0
    assert "refreshes its own tokens" in out
    assert "❌" not in out
    assert usage[1] == [] and run[0] == []
    urlopen.assert_not_called()
    assert db.read_bytes() == before


# ── autoswitch ───────────────────────────────────────────────────────────────


def _tty(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value, raising=False)


def test_autoswitch_switches_only_the_exhausted_integration(db, usage, run, monkeypatch, capsys):
    usage[0].update({"access-test-001": 95, "access-test-002": 10, "access-test-003": 20, "access-test-004": 0})
    before = db.read_bytes()
    _tty(monkeypatch, False)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert run[0] == [[EXE, "auth", "switch", "openai", "cred-oa-2"]]  # no --continue without a TTY
    assert "openai:personal → openai:work" in out
    assert "anthropic" in out and "below the switch threshold" in out
    assert "opencode" not in {integration for integration, _ in usage[1]}  # Zen skipped
    assert db.read_bytes() == before


def test_autoswitch_resumes_once_even_when_two_integrations_switch(db, usage, run, monkeypatch, capsys):
    usage[0].update({"access-test-001": 95, "access-test-002": 10, "access-test-003": 99, "access-test-004": 1})
    _tty(monkeypatch, True)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    # `opencode --continue` blocks until that session ends, so every integration
    # switches first and the single resume runs last.
    assert run[0] == [
        [EXE, "auth", "switch", "openai", "cred-oa-2"],
        [EXE, "auth", "switch", "anthropic", "cred-an-2"],
        [EXE, "--continue"],
    ]
    assert "anthropic:main → anthropic:cred-an-2" in out
    assert autoswitch.manual_restart_message("opencode", "anthropic:main", "anthropic:cred-an-2") not in out


def _switched_bodies(monkeypatch):
    bodies: list[str] = []
    monkeypatch.setattr(autoswitch, "notify", lambda title, body, *a, **kw: bodies.append(body))
    return bodies


def test_autoswitch_resume_success_prints_no_manual_message(db, usage, run, monkeypatch, capsys):
    usage[0].update({"access-test-001": 95, "access-test-002": 10, "access-test-003": 20})
    _tty(monkeypatch, True)
    bodies = _switched_bodies(monkeypatch)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert run[0][-1] == [EXE, "--continue"]
    # The notification goes out before the resume runs, so it never claims "restarted".
    assert bodies == [autoswitch.i18n.t("notify.switched.restart_needed")]
    assert autoswitch.manual_restart_message("opencode", "openai:personal", "openai:work") not in out


def test_autoswitch_resume_failure_reports_restart_needed_and_prints_manual_message(
    db, usage, run, monkeypatch, capsys
):
    usage[0].update({"access-test-001": 95, "access-test-002": 10, "access-test-003": 20})
    run[1][("--continue",)] = 1
    _tty(monkeypatch, True)
    bodies = _switched_bodies(monkeypatch)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0  # the switch itself worked
    assert run[0].count([EXE, "--continue"]) == 1
    assert bodies == [autoswitch.i18n.t("notify.switched.restart_needed")]
    assert autoswitch.manual_restart_message("opencode", "openai:personal", "openai:work") in out


def test_autoswitch_without_a_tty_prints_the_manual_message_and_never_resumes(db, usage, run, monkeypatch, capsys):
    usage[0].update({"access-test-001": 95, "access-test-002": 10, "access-test-003": 20})
    _tty(monkeypatch, False)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert [EXE, "--continue"] not in run[0]
    assert autoswitch.manual_restart_message("opencode", "openai:personal", "openai:work") in out


# ── autoswitch: expired spares, integration aliases ──────────────────────────

PAST_MS = 1_700_000_000_000


@pytest.mark.parametrize("past", [PAST_MS, PAST_MS // 1000])  # epoch milliseconds or seconds
def test_expired_spare_is_never_probed_and_is_named(data, usage, run, monkeypatch, capsys, past):
    _make_db(data / "opencode.db", [
        ("cred-a", "openai", "active", _oauth("access-test-a"), 1),
        ("cred-b", "openai", "stale", _oauth("access-test-b", past), 0),
    ])
    usage[0]["access-test-a"] = 95
    _tty(monkeypatch, True)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert "access-test-b" not in {cred["access"] for _, cred in usage[1]}
    assert run[0] == []
    assert "no other" in out
    assert "openai:stale" in out and "expired token" in out and "opencode-accounts switch" in out


def test_a_fresh_spare_still_wins_over_an_expired_one(data, usage, run, monkeypatch, capsys):
    _make_db(data / "opencode.db", [
        ("cred-a", "openai", "active", _oauth("access-test-a"), 1),
        ("cred-b", "openai", "stale", _oauth("access-test-b", PAST_MS), 0),
        ("cred-c", "openai", "fresh", _oauth("access-test-c"), 0),
    ])
    usage[0].update({"access-test-a": 95, "access-test-c": 5})
    _tty(monkeypatch, False)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert run[0] == [[EXE, "auth", "switch", "openai", "cred-c"]]
    assert "expired token" not in out


def test_expired_active_login_is_not_probed_and_still_names_expired_spares(data, usage, run, capsys):
    _make_db(data / "opencode.db", [
        ("cred-a", "openai", "active", _oauth("access-test-a", PAST_MS), 1),
        ("cred-b", "openai", "stale", _oauth("access-test-b", PAST_MS), 0),
    ])
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert usage[1] == []  # no network call for either expired token
    assert "Could not determine usage" in out and "openai:stale" in out and "expired token" in out


def test_credential_without_expires_is_probed(data, usage, run, capsys):
    _make_db(data / "opencode.db", [("cred-a", "openai", "active", _oauth("access-test-a", None), 1)])
    usage[0]["access-test-a"] = 10
    _main(capsys, "autoswitch")
    assert [cred["access"] for _, cred in usage[1]] == ["access-test-a"]


def test_copilot_alias_gets_usage_and_autoswitch_end_to_end(data, usage, run, monkeypatch, capsys):
    _make_db(data / "opencode.db", [
        ("cred-a", "copilot", "one", _oauth("access-test-a"), 1),
        ("cred-b", "copilot", "two", _oauth("access-test-b"), 0),
    ])
    usage[0].update({"access-test-a": 95, "access-test-b": 10})
    rc, out = _main(capsys, "list", "--json")
    assert [(e["name"], e["no_quota_api"], e["usage"] is not None) for e in json.loads(out)] == [
        ("copilot:one", False, True), ("copilot:two", False, True),
    ]
    _tty(monkeypatch, False)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert run[0] == [[EXE, "auth", "switch", "copilot", "cred-b"]]  # argv keeps opencode's own id


def test_autoswitch_switch_failure_exits_1(db, usage, run, monkeypatch, capsys):
    usage[0].update({"access-test-001": 95, "access-test-002": 10, "access-test-003": 20})
    run[1][("auth", "switch")] = 1
    _tty(monkeypatch, True)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 1
    assert [EXE, "--continue"] not in run[0]
    assert "openai:work" in out


def test_autoswitch_dedupe_keys_name_the_integration(db, usage, run, monkeypatch, capsys):
    usage[0].update({"access-test-001": 95, "access-test-002": 96, "access-test-003": 97, "access-test-004": 98})
    with mock.patch.object(autoswitch, "notify_once", return_value=False) as once:
        rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    keys = {call.args[0] for call in once.call_args_list}
    assert len(keys) == 2 and any("openai" in k for k in keys) and any("anthropic" in k for k in keys)
    assert "no other" in out


def test_autoswitch_without_logins_warns_and_exits_0(data, run, capsys):
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert "No opencode logins found" in out
    assert run[0] == []


def test_autoswitch_disabled_is_reported_once(db, usage, run, capsys):
    autoswitch.save_config({"enabled": False})
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert out.count("disabled") == 1
    assert run[0] == []


# ── hard guarantees: read-only DB, bounded and non-interactive CLI ───────────


def test_the_credential_db_is_opened_read_only(db, monkeypatch):
    real, outcomes = sqlite3.connect, []

    def probe_connect(*args, **kwargs):
        con = real(*args, **kwargs)
        try:
            con.execute("CREATE TABLE intruder (x)")
            outcomes.append("wrote")
        except sqlite3.OperationalError as exc:
            outcomes.append(str(exc))
        return con

    monkeypatch.setattr(oa.sqlite3, "connect", probe_connect)
    assert oa._logins()[0]
    assert outcomes == ["attempt to write a readonly database"]


def test_delegated_cli_call_has_closed_stdin_and_a_timeout(db, monkeypatch):
    seen: dict = {}

    def fake(argv, *args, **kwargs):
        seen.update(kwargs)
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(oa.shutil, "which", lambda name: EXE)
    monkeypatch.setattr(oa.subprocess, "run", fake)
    assert oa.cmd_switch("openai:work") == 0
    assert seen["stdin"] is oa.subprocess.DEVNULL
    assert seen["timeout"] == oa._CLI_TIMEOUT_SEC and oa._CLI_TIMEOUT_SEC > 0


# ── entry point ──────────────────────────────────────────────────────────────


def test_help_exits_0(capsys):
    rc, out = _main(capsys, "--help")
    assert rc == 0 and "opencode-accounts" in out


def test_unknown_command_exits_1(capsys):
    assert _main(capsys, "bogus")[0] == 1


# ── active login = opencode's rule: last of (active ASC NULLS FIRST, time_created, id) ──


def _raw_db(data: Path, rows) -> None:
    """rows: (id, integration_id, label, active, time_created)."""
    data.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(data / "opencode.db")) as con:
        con.execute(
            "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT,"
            " value TEXT, active INTEGER, time_created INTEGER)"
        )
        con.executemany(
            "INSERT INTO credential VALUES (?, ?, ?, ?, ?, ?)",
            [(i, integ, label, _oauth(f"access-{i}"), act, t) for i, integ, label, act, t in rows],
        )
        con.commit()


@pytest.mark.parametrize(
    ("rows", "active"),
    [
        ([("a", "openai", "one", None, 1)], ["openai:one"]),  # legacy-import row: active NULL
        ([("a", "openai", "one", 1, 1), ("b", "openai", "two", 0, 2)], ["openai:one"]),  # the 1 beats a later 0
        ([("a", "openai", "one", 0, 1), ("b", "openai", "two", 1, 2)], ["openai:two"]),
        ([("a", "openai", "one", None, 1), ("b", "openai", "two", 0, 2)], ["openai:two"]),  # NULL sorts before 0
        ([("a", "openai", "one", None, 1), ("b", "openai", "two", None, 2)], ["openai:two"]),  # later time_created
        ([("a", "openai", "one", None, 5), ("b", "openai", "two", None, 5)], ["openai:two"]),  # tie: larger id
        (  # one active login per integration
            [("a", "openai", "one", 0, 1), ("b", "openai", "two", 0, 2), ("c", "anthropic", "x", None, 1)],
            ["anthropic:x", "openai:two"],
        ),
    ],
)
def test_active_login_follows_opencodes_ordering_rule(data, rows, active):
    _raw_db(data, rows)
    assert sorted(login.name for login in oa._logins()[0] if login.active) == active


def test_a_single_null_active_login_is_shown_by_who(data, usage, capsys):
    _raw_db(data, [("a", "openai", "one", None, 1)])
    rc, out = _main(capsys, "who")
    assert rc == 0 and "openai:one" in out


def test_rows_without_an_integration_id_are_skipped(data, run, capsys):
    _raw_db(data, [("a", None, "ghost", 1, 1), ("b", "openai", "real", 1, 2)])
    assert _names(oa._logins()[0]) == ["openai:real"]
    assert oa.cmd_switch("None:ghost") == 1
    assert run[0] == []


# ── expired active login / copilot never-expires ─────────────────────────────


def test_active_login_is_never_listed_as_an_expired_spare():
    group = {
        "openai:a": oa.Login("openai:a", "openai", "a", json.loads(_oauth("x", PAST_MS)), True),
        "openai:b": oa.Login("openai:b", "openai", "b", json.loads(_oauth("y", PAST_MS)), False),
    }
    line = oa._expired_spares_line(group, "openai:a")
    assert "openai:b" in line and "openai:a" not in line
    assert oa._expired_spares_line({"openai:a": group["openai:a"]}, "openai:a") is None


def test_expired_active_login_is_named_as_the_reason_usage_is_unknown(data, usage, run, capsys):
    _make_db(data / "opencode.db", [("cred-a", "openai", "active", _oauth("access-test-a", PAST_MS), 1)])
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert "Could not determine usage" in out
    assert "openai:active" in out and "has an expired token" in out


def test_never_expiring_copilot_login_is_probed_and_switched_from(data, usage, run, monkeypatch, capsys):
    _make_db(data / "opencode.db", [
        ("cred-a", "github-copilot", "one", _oauth("access-test-a", 0), 1),
        ("cred-b", "github-copilot", "two", _oauth("access-test-b", 0), 0),
    ])
    usage[0].update({"access-test-a": 95, "access-test-b": 10})
    _tty(monkeypatch, False)
    rc, out = _main(capsys, "autoswitch")
    assert rc == 0
    assert {cred["access"] for _, cred in usage[1]} == {"access-test-a", "access-test-b"}
    assert run[0] == [[EXE, "auth", "switch", "github-copilot", "cred-b"]]
