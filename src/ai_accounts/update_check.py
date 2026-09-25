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
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import _keyreader as kr
from . import _present

REPO = "weskao/ai-accounts"
#: Unauthenticated GitHub API allows 60 requests/hour per IP; 6/hour is polite.
TTL_SECONDS = 600
TIMEOUT_SECONDS = 0.8
_check: threading.Thread | None = None
_latest: list[str | None] = []
#: Set by the outermost entry point so the provider tools that
#: ``ai-accounts <provider> …`` forwards to (sharing its TTY) stay quiet.
_CLAIMED_ENV = "AI_ACCOUNTS_UPDATE_CHECK_CLAIMED"

# ── the interactive prompt's answers ─────────────────────────────────────────
UPDATE_NOW = "update-now"
SKIP = "skip"
SKIP_VERSION = "skip-version"
# This project's own cursor, matching config_menu.py's ``_CURSOR_MARK`` — kept
# as a local copy rather than an import: two characters, not worth coupling
# this module to config_menu's settings-editor domain for.
_CURSOR_MARK = "❯ "  # ❯
_NO_CURSOR_MARK = "  "


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


def _write_cache(cache_path: Path, fields: dict[str, object]) -> None:
    """Merge *fields* into the cache file rather than overwriting it — a
    refetch of ``latest``/``checked_at`` must never clobber a ``skipped``
    version a previous run recorded."""
    from . import _utils as u
    from . import autoswitch as aw

    try:
        u.atomic_write_json(cache_path, {**aw._read_json(cache_path), **fields}, mode=0o644)
    except OSError:
        pass


def skip_version(cache_path: Path, version: str) -> None:
    """Remember *version* as skipped: :func:`newer_release` stays silent for
    it (and anything no newer) until a later release comes along."""
    _write_cache(cache_path, {"skipped": version})


def newer_release(current: str, *, now: float | None = None, fetch=fetch_latest_tag) -> str | None:
    """The latest release tag when it is newer than *current* and not
    already skipped, else None."""
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
        _write_cache(cache_path, {"checked_at": stamp, "latest": latest})
        cached = aw._read_json(cache_path)
    latest_parts = version_tuple(latest) if latest else None
    if latest_parts is None or latest_parts <= current_parts:
        return None
    skipped = cached.get("skipped")
    skipped_parts = version_tuple(skipped) if isinstance(skipped, str) else None
    if skipped_parts is not None and latest_parts <= skipped_parts:
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


def _upgrade_cmd(latest: str) -> list[str]:
    return [
        "uv", "tool", "install", "--force", "--from",
        f"git+https://github.com/{REPO}.git@{latest}", "ai-accounts",
    ]


def _run_upgrade(latest: str) -> None:
    """Run the install command; report on stderr (never raise) if it fails."""
    from . import _utils as u
    from . import i18n

    cmd = _upgrade_cmd(latest)
    try:
        done = subprocess.run(cmd, check=False)
        ok = getattr(done, "returncode", 1) == 0
    except OSError:
        ok = False
    if not ok:
        u.log_red(i18n.t("update.failed"))
        print(f"  {' '.join(cmd)}", file=sys.stderr)


def _update_lines(lang: str, current: str, latest: str, selected: int) -> list[str]:
    """The update prompt: this project's own box style (see
    :func:`ai_accounts._present.panel` — rounded corners, open right side),
    with the text layout aicp and codex-reset-watch use: one combined title
    line, column-aligned choices, no rule between them, a footer hint."""
    from . import _utils as u
    from . import i18n

    version = latest.lstrip("vV")
    title = i18n.t("update.available", lang, latest=version, current=current)
    choices = (
        (i18n.t("update.now", lang), " ".join(_upgrade_cmd(latest))),
        (i18n.t("update.skip", lang), i18n.t("update.skip_detail", lang)),
        (i18n.t("update.skip_version", lang), i18n.t("update.skip_version_detail", lang)),
    )
    # No index number — config_menu.py's own arrow-driven rows (_field_row)
    # mark the selected one with a bare cursor glyph and never number it.
    texts = [label for label, _ in choices]
    label_w = max(_present.visible_len(t) for t in texts)
    box_w = max(
        _present.visible_len(title) + 8,
        label_w + max(_present.visible_len(d) for _, d in choices) + 6,
    )
    dashes = box_w - _present.visible_len(title) - 4
    lines = [f"{u.CYAN}╭─ {u.BOLD}{title}{u.RESET}{u.CYAN} {'─' * dashes}╮{u.RESET}"]
    for i, (_, detail) in enumerate(choices, start=1):
        text = texts[i - 1]
        pad = " " * (label_w - _present.visible_len(text))
        if selected == i:
            row = f"{u.CYAN}{_CURSOR_MARK}{u.RESET}{u.CYAN}{u.BOLD}{text}{u.RESET}{pad}  {u.DIM}{detail}{u.RESET}"
        else:
            row = f"{_NO_CURSOR_MARK}{text}{pad}  {u.DIM}{detail}{u.RESET}"
        lines.append(f"{u.CYAN}│{u.RESET}  {row}")
    lines.append(f"{u.CYAN}│{u.RESET}")
    hint = " · ".join([
        f"{u.CYAN}↑↓{u.RESET} {i18n.t('update.select', lang)}",
        f"{u.CYAN}⏎{u.RESET} {i18n.t('update.confirm', lang)}",
        f"{u.CYAN}q{u.RESET} {i18n.t('update.skip_key', lang)}",
    ])
    lines.append(f"{u.CYAN}│{u.RESET}  {hint}")
    lines.append(f"{u.CYAN}╰{'─' * (box_w - 1)}╯{u.RESET}")
    return lines


def update_prompt(current: str, latest: str, *, read=kr.read_key, out=None) -> str:
    """Ask what to do about *latest*: returns UPDATE_NOW / SKIP / SKIP_VERSION.
    An exhausted key source, Ctrl-C, ``q`` or Escape all back out as SKIP —
    same contract as aicp's and codex-reset-watch's prompts."""
    from . import i18n

    out = out or sys.stdout
    lang = i18n.current_language()
    answers = (UPDATE_NOW, SKIP, SKIP_VERSION)
    selected = 1
    painted = 0
    with kr.raw_mode():
        while True:
            lines = _update_lines(lang, current, latest, selected)
            prefix = f"\033[{painted}A" if painted else ""
            out.write(prefix + "".join(f"{line}\033[K\n" for line in lines) + "\033[J")
            out.flush()
            painted = len(lines)
            try:
                event = read()
            except (KeyboardInterrupt, StopIteration):
                return SKIP
            if event.key in (kr.Key.CTRL_C, kr.Key.ESCAPE):
                return SKIP
            if event.key == kr.Key.UP:
                selected = selected - 1 if selected > 1 else len(answers)
            elif event.key == kr.Key.DOWN:
                selected = selected + 1 if selected < len(answers) else 1
            elif event.key == kr.Key.ENTER:
                return answers[selected - 1]
            elif event.key == kr.Key.CHAR and (event.char or "").lower() == "q":
                return SKIP


def maybe_hint() -> None:
    """Act on the started check's result. A real keyboard-and-screen TTY
    (:func:`ai_accounts._keyreader.is_interactive_tty`) gets the interactive
    ask; anything else (piped output, a background job) gets the flat stderr
    hint, unchanged. Never raises."""
    try:
        if _check is None:
            return
        # Usually already done: it ran alongside the command.
        _check.join(TIMEOUT_SECONDS)
        latest = _latest[0] if _latest else None
        if latest is None:
            return
        from . import _utils as u
        from . import autoswitch as aw
        from . import i18n

        current = u.package_version()
        if kr.is_interactive_tty():
            answer = update_prompt(current, latest)
            cache_path = aw.config_path().parent / "update-check.json"
            if answer == SKIP_VERSION:
                skip_version(cache_path, latest)
            elif answer == UPDATE_NOW:
                _run_upgrade(latest)
            return
        u.log_yellow(
            i18n.t(
                "update.available",
                latest=latest.lstrip("vV"),
                current=current,
            )
        )
        print(f"  {' '.join(_upgrade_cmd(latest))}", file=sys.stderr)
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - a hint must never fail the command
        return
