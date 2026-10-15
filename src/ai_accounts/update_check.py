"""``<cmd> update`` and the one-line "newer release" hint, both via update-kit.

ai-accounts is installed from git tags (``uv tool install --from
git+https://github.com/weskao/ai-accounts.git@vX.Y.Z``), not PyPI. The install
and the "is there a newer tag" lookup live in the shared ``update_kit``
library; this module only wires it to the nine entry points.

The hint's lookup starts in a background thread when the command starts, so it
overlaps the command's own work (update-kit caches it for a day, failures
included, in ``~/.update-kit/state.json``); every failure is silence — a hint
must never slow a command noticeably or change its exit code.

Only a human sees it: stderr has to be a TTY, which rules out the scheduled
timer, the vendor-CLI hooks and ``ai-accounts list``'s captured children.
"""

from __future__ import annotations

import os
import sys
import threading

import update_kit

REPO = "weskao/ai-accounts"
DIST = "ai-accounts"
#: How long the command waits for the hint. It must exceed the lookup's own timeout:
#: update-kit writes its cache (failures too) only after the lookup returns, so exiting
#: first would re-run the lookup, and re-pay this wait, on every TTY command.
TIMEOUT_SECONDS = 2.0
CHECK_TIMEOUT_SECONDS = 1.5
_check: threading.Thread | None = None
_latest: list[str | None] = []
_cmd = DIST
#: Set by the outermost entry point so the provider tools that
#: ``ai-accounts <provider> …`` forwards to (sharing its TTY) stay quiet.
_CLAIMED_ENV = "AI_ACCOUNTS_UPDATE_CHECK_CLAIMED"


def claim() -> bool:
    """True for the first entry point in this process tree, False for children."""
    if os.environ.get(_CLAIMED_ENV):
        return False
    os.environ[_CLAIMED_ENV] = "1"
    return True


def command_name(module: str) -> str:
    """The console script a ``main`` in *module* is installed as (``ai-accounts`` for the umbrella)."""
    from .providers import PROVIDERS

    return next((p.label for p in PROVIDERS if p.module == module), DIST)


def _language() -> str:
    """The configured language from the config file alone — ``i18n.current_language()``
    would also read the keychain just to pick a message language."""
    from . import autoswitch as aw
    from . import i18n

    try:
        return i18n.resolve_language(aw._read_json(aw.config_path()).get("language"))
    except Exception:  # noqa: BLE001 - message language never fails the command
        return i18n.system_language()


def run_update(cmd: str) -> int:
    """``<cmd> update``: install the newest release tag; update-kit's exit code. No prompts."""
    return update_kit.update(app=cmd, repo=REPO, dist=DIST, lang=_language(), out=sys.stderr)


def refuse_args(cmd: str) -> int:
    """``<cmd> update <extra>``: say so (never install) and return exit status 2."""
    from . import i18n

    print(i18n.t("update.no_args", _language(), cmd=cmd), file=sys.stderr)
    return 2


def start_check(cmd: str = DIST) -> None:
    """Begin the check in a daemon thread so it overlaps the command. Never raises."""
    global _check, _cmd
    try:
        if not update_kit.should_hint(sys.stderr):
            return
        from . import _utils as u
        from . import autoswitch as aw

        # The file alone: load_config() would also read the keychain.
        if not aw.config_flag("update_check", {**aw.DEFAULTS, **aw._read_json(aw.config_path())}):
            return
        current = u.package_version()
        _cmd = cmd

        def run() -> None:
            try:
                _latest.append(update_kit.check(repo=REPO, current=current, timeout=CHECK_TIMEOUT_SECONDS))
            except Exception:  # noqa: BLE001 - check() never raises, but a hint must never fail the command
                pass

        _check = threading.Thread(target=run, name="ai-accounts-update-check", daemon=True)
        _check.start()
    except Exception:  # noqa: BLE001 - a hint must never fail the command
        return


def maybe_hint() -> None:
    """Print the started check's result as one stderr line. Never raises."""
    try:
        if _check is None:
            return
        # Usually already done: it ran alongside the command.
        _check.join(TIMEOUT_SECONDS)
        latest = _latest[0] if _latest else None
        if latest is None:
            return
        from . import _utils as u
        print(update_kit.hint(_cmd, latest.lstrip("vV"), u.package_version(), _language()), file=sys.stderr)
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - a hint must never fail (or traceback) the command
        return
