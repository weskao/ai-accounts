"""opencode_usage routes opencode credentials to the existing usage fetchers."""

from __future__ import annotations

from unittest import mock

import pytest

from ai_accounts import (
    autoswitch,
    claude_usage,
    copilot_usage,
    opencode_usage,
    usage_format,
)
from ai_accounts.usage_format import UsageWindow

HOURLY = UsageWindow(10, 1_000, 300)
WEEKLY = UsageWindow(55, 2_000, 10_080)
OAUTH = {"type": "oauth", "refresh": "r-placeholder", "access": "a-placeholder", "expires": 1, "accountId": "acct-1"}


@pytest.fixture(autouse=True)
def _default_window(monkeypatch):
    monkeypatch.setattr(autoswitch, "load_config", lambda: {})  # weekly default


def test_quota_integrations():
    assert {"openai", "anthropic", "github-copilot"} <= set(opencode_usage.QUOTA_INTEGRATIONS)
    assert "opencode" not in opencode_usage.QUOTA_INTEGRATIONS


def test_openai_adapts_to_codex_auth_and_picks_weekly():
    snap = usage_format.UsageSnapshot(HOURLY, WEEKLY, 1, None)
    with mock.patch.object(usage_format, "fetch_usage", return_value=snap) as fetch:
        assert opencode_usage.fetch_usage("openai", OAUTH) == WEEKLY
    fetch.assert_called_once_with({"tokens": {"access_token": "a-placeholder", "account_id": "acct-1"}})


def test_openai_without_account_id_omits_it():
    snap = usage_format.UsageSnapshot(None, WEEKLY, 1, None)
    with mock.patch.object(usage_format, "fetch_usage", return_value=snap) as fetch:
        opencode_usage.fetch_usage("openai", {"type": "oauth", "access": "a-placeholder"})
    fetch.assert_called_once_with({"tokens": {"access_token": "a-placeholder"}})


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"metadata": {"accountID": "acct-v2"}}, "acct-v2"),
        ({"metadata": {"accountID": "acct-v2"}, "accountId": "acct-legacy"}, "acct-v2"),
        ({"accountId": "acct-legacy"}, "acct-legacy"),
        ({"metadata": "junk", "accountId": "acct-legacy"}, "acct-legacy"),
    ],
)
def test_openai_reads_v2_metadata_account_id_before_the_legacy_field(extra, expected):
    snap = usage_format.UsageSnapshot(None, WEEKLY, 1, None)
    with mock.patch.object(usage_format, "fetch_usage", return_value=snap) as fetch:
        opencode_usage.fetch_usage("openai", {"type": "oauth", "access": "a-placeholder", **extra})
    fetch.assert_called_once_with({"tokens": {"access_token": "a-placeholder", "account_id": expected}})


def test_anthropic_routes_access_token_and_picks_weekly():
    snap = claude_usage.UsageSnapshot(HOURLY, WEEKLY, None, 1, None)
    with mock.patch.object(claude_usage, "fetch_usage", return_value=snap) as fetch:
        assert opencode_usage.fetch_usage("anthropic", OAUTH, timeout=5) == WEEKLY
    fetch.assert_called_once_with("a-placeholder", timeout=5)


@pytest.mark.parametrize("integration", ["github-copilot", "copilot"])
def test_copilot_routes_token_and_returns_plan_usage(integration):
    snap = copilot_usage.UsageSnapshot(WEEKLY, None, None, None, 1, None)
    with mock.patch.object(copilot_usage, "fetch_usage", return_value=snap) as fetch:
        assert opencode_usage.fetch_usage(integration, OAUTH) == WEEKLY
    fetch.assert_called_once_with("a-placeholder", timeout=20)


def test_fetcher_error_snapshot_is_none():
    snap = usage_format.UsageSnapshot(None, None, None, "HTTP 401 from usage endpoint")
    with mock.patch.object(usage_format, "fetch_usage", return_value=snap):
        assert opencode_usage.fetch_usage("openai", OAUTH) is None


@pytest.mark.parametrize("integration", ["opencode", "opencode-go", "zen", "something-new", ""])
@pytest.mark.parametrize("cred", [OAUTH, None, {"type": "api", "key": "k-placeholder"}])
def test_unknown_integrations_never_call_a_fetcher(integration, cred):
    with (
        mock.patch.object(usage_format, "fetch_usage") as codex,
        mock.patch.object(claude_usage, "fetch_usage") as claude,
        mock.patch.object(copilot_usage, "fetch_usage") as copilot,
    ):
        assert opencode_usage.fetch_usage(integration, cred) is None
    for fetcher in (codex, claude, copilot):
        fetcher.assert_not_called()


@pytest.mark.parametrize("integration", ["openai", "anthropic", "github-copilot"])
@pytest.mark.parametrize(
    "cred",
    [None, {"type": "api", "key": "k"}, {"type": "oauth"}, {"type": "oauth", "access": ""}, {"type": "oauth", "access": 5}, "x"],
)
def test_non_oauth_or_tokenless_credentials_skip_fetchers(integration, cred):
    with (
        mock.patch.object(usage_format, "fetch_usage") as codex,
        mock.patch.object(claude_usage, "fetch_usage") as claude,
        mock.patch.object(copilot_usage, "fetch_usage") as copilot,
    ):
        assert opencode_usage.fetch_usage(integration, cred) is None
    for fetcher in (codex, claude, copilot):
        fetcher.assert_not_called()


@pytest.mark.parametrize(
    ("integration", "module"),
    [("openai", usage_format), ("anthropic", claude_usage), ("github-copilot", copilot_usage)],
)
def test_raising_fetcher_yields_none(integration, module):
    with mock.patch.object(module, "fetch_usage", side_effect=RuntimeError("boom")):
        assert opencode_usage.fetch_usage(integration, OAUTH) is None


@pytest.mark.parametrize(
    ("expires", "expected"),
    [
        (1_700_000_000_000, True),  # epoch milliseconds, past
        (1_700_000_000, True),  # epoch seconds, past
        (4_102_444_800_000, False),  # 2100, ms
        (4_102_444_800, False),  # 2100, s
        (None, False),
        ("soon", False),
        (True, False),
        (0, False),  # opencode's Copilot device-flow logins store 0 = never expires
        (-1, False),
    ],
)
def test_is_expired_handles_epoch_seconds_and_milliseconds(expires, expected):
    assert opencode_usage.is_expired({"type": "oauth", "expires": expires}) is expected


@pytest.mark.parametrize("cred", [None, "x", {}])
def test_is_expired_is_false_without_an_expires_field(cred):
    assert opencode_usage.is_expired(cred) is False


def test_normalize_maps_the_copilot_alias_once():
    assert opencode_usage.normalize("copilot") == "github-copilot"
    assert opencode_usage.normalize("openai") == "openai"
