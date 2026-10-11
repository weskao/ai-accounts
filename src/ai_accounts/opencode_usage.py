"""Quota lookup for opencode credentials, by routing to the existing fetchers.

Routing and payload adaptation only: no HTTP and no formatting live here. The
probe is read-only and sends the credential's access token as-is. It NEVER
refreshes a token -- OpenAI refresh tokens are single-use and shared with
codex-accounts, so refreshing here would log the user out elsewhere. (None of
the three fetchers refreshes on 401; a rejected token is just an error.)
"""

from __future__ import annotations

import time
from typing import Any, Final

from . import autoswitch, claude_usage, copilot_usage, usage_format
from .usage_format import UsageWindow

# ASSUMPTION: only "openai" is observed on the dev machine (legacy opencode
# auth.json OAuth record {"type","refresh","access","expires","accountId"}; v2
# stores metadata.accountID instead);
# "anthropic" and "github-copilot" follow opencode's provider ids, and "copilot"
# is a defensive alias (see normalize). Anything else (opencode / Go / Zen, API keys) has no
# quota API here and yields None.
_ALIASES: Final = {"copilot": "github-copilot"}
QUOTA_INTEGRATIONS: Final = ("openai", "anthropic", "github-copilot")


def normalize(integration: str) -> str:
    """The canonical integration id; every QUOTA_INTEGRATIONS check goes through this."""
    return _ALIASES.get(integration, integration)


def is_expired(credential: object) -> bool:
    """Whether the credential's ``expires`` (epoch ms or s) is past; unknown means False.

    An expired access token is rejected by the usage APIs, and opencode only
    refreshes it when opencode itself uses that login — so callers skip the call.
    """
    expires = credential.get("expires") if isinstance(credential, dict) else None
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return False
    if expires <= 0:  # opencode's Copilot device-flow logins store 0: never expires
        return False
    # shortcut: values above 1e11 are read as ms (that is year 5138 in seconds).
    return (expires / 1000 if expires > 1e11 else expires) <= time.time()


def _openai(token: str, cred: dict[str, Any], timeout: float) -> UsageWindow | None:
    tokens = {"access_token": token}
    meta = cred.get("metadata")
    # ASSUMPTION: v2 credential values keep the id in metadata.accountID; the legacy
    # auth.json record (the one observed on the dev machine) uses a top-level accountId.
    account_id = next(
        (v for v in (meta.get("accountID") if isinstance(meta, dict) else None, cred.get("accountId"))
         if isinstance(v, str) and v),
        None,
    )
    if account_id:
        tokens["account_id"] = account_id
    # shortcut: the codex fetcher takes no timeout; it has its own fixed one.
    snap = usage_format.fetch_usage({"tokens": tokens})
    return None if snap.error else autoswitch.pick_window(snap.hourly, snap.weekly)


def _anthropic(token: str, cred: dict[str, Any], timeout: float) -> UsageWindow | None:
    snap = claude_usage.fetch_usage(token, timeout=timeout)
    return None if snap.error else autoswitch.pick_window(snap.five_hour, snap.seven_day)


def _copilot(token: str, cred: dict[str, Any], timeout: float) -> UsageWindow | None:
    snap = copilot_usage.fetch_usage(token, timeout=timeout)
    return None if snap.error else snap.plan_usage


_PROBES: Final = {"openai": _openai, "anthropic": _anthropic, "github-copilot": _copilot}


def fetch_usage(integration: str, credential: dict | None, *, timeout: float = 20) -> UsageWindow | None:
    """The window autoswitch reads for one opencode credential, else None. Never raises."""
    probe = _PROBES.get(normalize(integration))
    if probe is None or not isinstance(credential, dict) or credential.get("type") != "oauth":
        return None
    token = credential.get("access")
    if not (isinstance(token, str) and token):
        return None
    try:
        return probe(token, credential, timeout)
    except Exception:  # noqa: BLE001 - a probe must never break the caller
        return None
