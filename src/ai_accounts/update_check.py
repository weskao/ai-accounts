"""Tell an interactive user when a newer ai-accounts release is on GitHub.

ai-accounts is installed from git tags (``uv tool install --from
git+https://github.com/weskao/ai-accounts.git@vX.Y.Z``), not PyPI, so the
source of truth is the repo's latest GitHub release. The request starts in a
background thread when the command starts, so it overlaps the command's own
work; at most one per ``TTL_SECONDS`` (cached next to ``config.json``, short
because several releases can land in one day), a sub-second timeout, and every
failure — offline, rate-limited, junk payload — is silence: a hint must never
slow a command noticeably or change its exit code.

Only a human sees it: stderr has to be a TTY, which rules out the scheduled
timer, the vendor-CLI hooks and ``ai-accounts list``'s captured children.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request

REPO = "weskao/ai-accounts"
#: Unauthenticated GitHub API allows 60 requests/hour per IP; 6/hour is polite.
TTL_SECONDS = 600
TIMEOUT_SECONDS = 0.8
_check: threading.Thread | None = None
_latest: list[str | None] = []
#: Set by the outermost entry point so the provider tools that
#: ``ai-accounts <provider> …`` forwards to (sharing its TTY) stay quiet.
_CLAIMED_ENV = "AI_ACCOUNTS_UPDATE_CHECK_CLAIMED"


def claim() -> bool:
    """True for the first entry point in this process tree, False for children."""
    if os.environ.get(_CLAIMED_ENV):
        return False
    os.environ[_CLAIMED_ENV] = "1"
    return True


def version_tuple(version: str) -> tuple[int, ...] | None:
    """``v0.12.0`` → ``(0, 12, 0)``; leading dotted integers, at least X.Y."""
    parts: list[int] = []
    for chunk in version.strip().lstrip("vV").split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if len(parts) >= 2 else None


def fetch_latest_tag(timeout: float = TIMEOUT_SECONDS) -> str | None:
    """The latest release's tag name from the GitHub API, or None."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ai-accounts-update-check"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    tag = data.get("tag_name") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and tag else None


def newer_release(current: str, *, now: float | None = None, fetch=fetch_latest_tag) -> str | None:
    """The latest release tag when it is newer than *current*, else None."""
    from . import _utils as u
    from . import autoswitch as aw

    current_parts = version_tuple(current)
    if current_parts is None:
        return None
    stamp = time.time() if now is None else now
    cache_path = aw.config_path().parent / "update-check.json"
    cached = aw._read_json(cache_path)
    latest = cached.get("latest") if isinstance(cached.get("latest"), str) else None
    checked_at = cached.get("checked_at")
    # A failed fetch is cached too (as the old answer or None), so being
    # offline costs one timeout per day, not one per command.
    if not isinstance(checked_at, (int, float)) or stamp - checked_at >= TTL_SECONDS:
        try:
            fetched = fetch()
        except (OSError, ValueError):
            fetched = None
        latest = fetched or latest
        try:
            u.atomic_write_json(cache_path, {"checked_at": stamp, "latest": latest}, mode=0o644)
        except OSError:
            pass
    latest_parts = version_tuple(latest) if latest else None
    if latest_parts is None or latest_parts <= current_parts:
        return None
    return latest


def start_check() -> None:
    """Begin the check in a daemon thread so it overlaps the command. Never raises."""
    global _check
    try:
        if not sys.stderr.isatty():
            return
        from . import _utils as u
        from . import autoswitch as aw

        # The file alone: load_config() would also read the keychain.
        if not aw.config_flag("update_check", {**aw.DEFAULTS, **aw._read_json(aw.config_path())}):
            return
        current = u.package_version()

        def run() -> None:
            try:
                _latest.append(newer_release(current))
            except Exception:  # noqa: BLE001 - http.client errors are not all OSError
                pass

        _check = threading.Thread(target=run, name="ai-accounts-update-check", daemon=True)
        _check.start()
    except Exception:  # noqa: BLE001 - a hint must never fail the command
        return


def maybe_hint() -> None:
    """Print the upgrade hint on stderr when the started check found one. Never raises."""
    try:
        if _check is None:
            return
        # Usually already done: it ran alongside the command.
        _check.join(TIMEOUT_SECONDS)
        latest = _latest[0] if _latest else None
        if latest is None:
            return
        from . import _utils as u
        from . import i18n

        current = u.package_version()
        u.log_yellow(
            i18n.t(
                "update.available",
                latest=latest.lstrip("vV"),
                current=current,
            )
        )
        print(
            f"  uv tool install --force --from git+https://github.com/{REPO}.git@{latest} ai-accounts",
            file=sys.stderr,
        )
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - a hint must never fail the command
        return
