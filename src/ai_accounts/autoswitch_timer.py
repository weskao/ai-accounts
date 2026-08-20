"""OS scheduling for the auto-switch-on-low-quota feature.

This module owns ONLY the periodic-trigger plumbing — registering,
unregistering and reporting on an OS-level scheduled job, plus the entry
point that job invokes on each tick. The actual quota probe / profile switch
logic lives in :mod:`ai_accounts.autoswitch` (``run_autoswitch``) and the
provider wiring around it, both out of scope here.

Platform is selected via the module-level booleans in :mod:`ai_accounts._utils`
(``IS_MACOS`` / ``IS_LINUX`` / ``IS_WINDOWS``) — never ``sys.platform``
directly — so tests can force a platform without touching the real OS:

    macOS   - a launchd plist under ``~/Library/LaunchAgents/``, loaded via
              ``launchctl load``.
    Linux   - a systemd --user timer + service under
              ``~/.config/systemd/user/``, enabled via ``systemctl``; falls
              back to a tagged crontab entry when ``systemctl`` is absent.
    Windows - a scheduled task registered via ``schtasks /Create``.
"""

from __future__ import annotations

import plistlib
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Sequence

from . import _utils as u
from . import ai_accounts
from . import autoswitch_hooks
from . import autoswitch as aw

LABEL = "com.ai_accounts.autoswitch"
DEFAULT_INTERVAL_SEC = 30 * 60  # 30 minutes
CRON_TAG = "# ai-accounts-autoswitch"


def _run_command() -> str:
    """The scheduled command line, quoted for a POSIX shell (cron, systemd).

    uv and pyenv interpreters live under paths like
    ``~/Library/Application Support/...``; unquoted, cron and systemd split the
    ExecStart at the space and the job silently never runs.
    """
    return f"{shlex.quote(sys.executable)} -m ai_accounts.autoswitch_timer run"


def _run_command_windows() -> str:
    """Same command for ``schtasks /TR`` — cmd.exe wants double quotes, and
    POSIX single-quoting would be taken literally."""
    return f'"{sys.executable}" -m ai_accounts.autoswitch_timer run'


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_timer_path() -> Path:
    return _systemd_unit_dir() / f"{LABEL}.timer"


def _systemd_service_path() -> Path:
    return _systemd_unit_dir() / f"{LABEL}.service"


def _cron_line(interval_sec: int) -> str:
    """A crontab line running the check every *interval_sec*.

    cron's minute field only accepts a 0-59 step, so an interval longer than an
    hour is clamped to hourly rather than emitting an invalid ``*/120``.
    """
    minutes = min(59, max(1, interval_sec // 60))
    return f"*/{minutes} * * * * {_run_command()} {CRON_TAG}"


# ── install ───────────────────────────────────────────────────────────────

def _install_macos(interval_sec: int) -> None:
    plist_path = _launchd_plist_path()
    # Re-install must unload the stale job first: launchd keeps running the
    # already-loaded plist on its OLD StartInterval, so rewriting the file alone
    # makes "change the interval" silently do nothing.
    if plist_path.is_file():
        u.run(["launchctl", "unload", str(plist_path)])
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": LABEL,
                "ProgramArguments": [
                    sys.executable,
                    "-m",
                    "ai_accounts.autoswitch_timer",
                    "run",
                ],
                "StartInterval": interval_sec,
                "RunAtLoad": True,
            }
        )
    )
    u.run(["launchctl", "load", str(plist_path)])


def _install_linux_systemd(interval_sec: int) -> None:
    unit_dir = _systemd_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    _systemd_service_path().write_text(
        "[Unit]\nDescription=ai-accounts autoswitch check\n\n"
        f"[Service]\nType=oneshot\nExecStart={_run_command()}\n",
        encoding="utf-8",
    )
    _systemd_timer_path().write_text(
        "[Unit]\nDescription=ai-accounts autoswitch timer\n\n"
        f"[Timer]\nOnUnitActiveSec={interval_sec}\nOnBootSec=60\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    # daemon-reload before enable: systemd caches unit contents, so a re-install
    # with a new interval is ignored until it re-reads them.
    u.run(["systemctl", "--user", "daemon-reload"])
    u.run(["systemctl", "--user", "enable", "--now", f"{LABEL}.timer"])


def _install_linux_cron(interval_sec: int) -> None:
    existing = u.run(["crontab", "-l"], capture_output=True)
    lines = [
        line for line in (existing.stdout or "").splitlines() if CRON_TAG not in line
    ]
    lines.append(_cron_line(interval_sec))
    u.run(["crontab", "-"], input="\n".join(lines) + "\n")


def _install_linux(interval_sec: int) -> None:
    if u.have("systemctl"):
        _install_linux_systemd(interval_sec)
    else:
        _install_linux_cron(interval_sec)


def _install_windows(interval_sec: int) -> None:
    minutes = max(1, interval_sec // 60)
    u.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            LABEL,
            "/TR",
            _run_command_windows(),
            "/SC",
            "MINUTE",
            "/MO",
            str(minutes),
            "/F",
        ]
    )


def install(interval_sec: int = DEFAULT_INTERVAL_SEC) -> None:
    """Register the periodic autoswitch check with the OS scheduler."""
    if u.IS_MACOS:
        _install_macos(interval_sec)
    elif u.IS_LINUX:
        _install_linux(interval_sec)
    elif u.IS_WINDOWS:
        _install_windows(interval_sec)


# ── uninstall ─────────────────────────────────────────────────────────────

def _uninstall_macos() -> None:
    plist_path = _launchd_plist_path()
    if plist_path.exists():
        u.run(["launchctl", "unload", str(plist_path)])
        plist_path.unlink()


def _uninstall_linux_cron() -> None:
    existing = u.run(["crontab", "-l"], capture_output=True)
    lines = [
        line for line in (existing.stdout or "").splitlines() if CRON_TAG not in line
    ]
    u.run(["crontab", "-"], input="\n".join(lines) + ("\n" if lines else ""))


def _uninstall_linux() -> None:
    timer_path = _systemd_timer_path()
    if timer_path.exists():
        u.run(["systemctl", "--user", "disable", "--now", f"{LABEL}.timer"])
        timer_path.unlink()
        _systemd_service_path().unlink(missing_ok=True)
    else:
        _uninstall_linux_cron()


def _uninstall_windows() -> None:
    u.run(["schtasks", "/Delete", "/TN", LABEL, "/F"])


def uninstall() -> None:
    """Remove the periodic autoswitch check from the OS scheduler."""
    if u.IS_MACOS:
        _uninstall_macos()
    elif u.IS_LINUX:
        _uninstall_linux()
    elif u.IS_WINDOWS:
        _uninstall_windows()


# ── status ────────────────────────────────────────────────────────────────

def _status_linux() -> str:
    if _systemd_timer_path().exists():
        return "installed"
    existing = u.run(["crontab", "-l"], capture_output=True)
    if CRON_TAG in (existing.stdout or ""):
        return "installed"
    return "not installed"


def _status_windows() -> str:
    result = u.run(["schtasks", "/Query", "/TN", LABEL], capture_output=True)
    return "installed" if result.returncode == 0 else "not installed"


def status() -> str:
    """Report whether the periodic autoswitch check is installed."""
    if u.IS_MACOS:
        return "installed" if _launchd_plist_path().exists() else "not installed"
    if u.IS_LINUX:
        return _status_linux()
    if u.IS_WINDOWS:
        return _status_windows()
    return "not installed"


# ── the scheduled job's entry point ─────────────────────────────────────────


def _run_provider(module: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, "autoswitch"], capture_output=True, text=True
    )


def _run_autoswitch_everywhere() -> None:
    """Default *check*: quota probe/switch across eligible providers in parallel.

    The provider registry lives with the hook installer, keeping timed and
    event-triggered checks on the same cross-platform support matrix.
    """
    modules = [autoswitch_hooks.module(provider) for provider in autoswitch_hooks.providers()]
    with ThreadPoolExecutor(max_workers=len(modules)) as pool:
        _ = list(pool.map(_run_provider, modules))


# A revoked refresh token is the only case any provider's refresh path logs
# with one of these exact phrases — see _is_revoked_error's call sites in
# codex_accounts.py, claude_accounts.py, gemini_accounts.py and
# grok_accounts.py: each logs "...Refresh token revoked..." / "...revoked/dead
# for..." inline, and codex/claude additionally print the bulk "❌ Revoked
# (re-login required): <names>" summary for a profile with no refresh_token to
# even attempt. A transient failure never matches either phrase — notably
# codex's own transient message reads "...refresh token may be expired OR
# revoked..." (codex_accounts.py:560), which does NOT contain either phrase
# below, so a 5xx/network hiccup can't false-trigger this. Matching full
# phrases instead of the bare word "revoked" also keeps a profile literally
# named e.g. "revoked-backup" from false-triggering off the printed table.
_REVOKED_MARKERS = ("refresh token revoked", "revoked (re-login required)")

# ponytail: fixed per-provider ceiling, not a config knob — the CLI-proxy
# fallback (agy/grok, when a direct OAuth refresh can't run) spawns a vendor
# binary with no bound of its own, and macOS launchd won't start a new tick
# while one is still running, so one wedged subprocess would silently kill
# the timer forever. Upgrade path: make this configurable if a provider's CLI
# proxy routinely needs longer than this.
_REFRESH_TIMEOUT_SEC = 300


def _run_token_refresh_everywhere() -> str:
    """Default *refresh*: direct-OAuth token refresh, across all four providers.

    Drives the same four provider CLIs :func:`_run_autoswitch_everywhere` does
    (via :data:`ai_accounts.ai_accounts._TOOLS`, so the provider list is never
    duplicated), with ``refresh --all`` — already a ``<provider>-accounts
    refresh --all`` command on every one of them — in place of ``autoswitch``.

    Captured rather than run through :func:`ai_accounts.ai_accounts.cmd_forward`
    (which inherits stdio so its interactive commands — switch pickers, login
    flows — keep working): capturing here is what lets the caller tell a
    revoked refresh token apart from a routine, silent rotation. A hung
    provider is cut off after :data:`_REFRESH_TIMEOUT_SEC` rather than
    blocking the tick forever. Returns the combined stdout+stderr text — the
    exit code isn't surfaced, matching :func:`_run_autoswitch_everywhere`,
    whose analogous default ``check`` reports nothing back to ``run_once``
    either.
    """
    output_parts: list[str] = []
    for label, module in ai_accounts._TOOLS:
        ai_accounts._header(label)
        try:
            result = u.run(
                [sys.executable, "-m", module, "refresh", "--all"],
                capture_output=True,
                timeout=_REFRESH_TIMEOUT_SEC,
            )
            stdout, stderr = result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.stdout or "", exc.stderr or ""
            u.log_red(f"❌ {label} refresh timed out after {_REFRESH_TIMEOUT_SEC}s")
        if stdout:
            print(stdout, end="")
            output_parts.append(stdout)
        if stderr:
            print(stderr, end="", file=sys.stderr)
            output_parts.append(stderr)
        print()
    return "\n".join(output_parts)


def run_once(
    check: Callable[[], None] | None = None,
    refresh: Callable[[], str] | None = None,
) -> int:
    """The scheduled job's entry point — invoked on every timer tick.

    Auto-switch and token-refresh are two INDEPENDENT gates, each reading its
    own config flag — ``enabled`` for the quota auto-switch check,
    ``token_refresh`` for renewing tokens across all four providers. Either,
    both, or neither run on a given tick: a user who does not want automatic
    account switching still wants live tokens (plan.md §4 phase 4 — this is
    the fix for `enabled=false` silently disabling refresh too).

    *check* / *refresh* are the actual quota-probe/switch and token-refresh
    calls; when omitted they default to :func:`_run_autoswitch_everywhere` /
    :func:`_run_token_refresh_everywhere` (a test may still inject its own to
    observe the call without driving real provider subprocesses).

    A refresh tick alerts via :func:`ai_accounts.autoswitch.notify_once` only
    when the refresh output mentions a revoked refresh token — never for a
    routine, successful rotation.
    """
    if aw.config_flag("enabled"):  # fail closed: only a JSON `true` runs
        (check or _run_autoswitch_everywhere)()
    if aw.config_flag("token_refresh"):  # fail closed: same rule, own flag
        output = (refresh or _run_token_refresh_everywhere)().lower()
        if any(marker in output for marker in _REVOKED_MARKERS):
            aw.notify_once(
                "token-refresh:revoked",
                "ai-accounts: an account needs a fresh login",
                "A scheduled token refresh found a revoked refresh token for "
                "at least one account. Run `ai-accounts refresh --all` to see "
                "which provider/profile, then `<provider>-accounts "
                "login-switch <name>`.",
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI dispatch — this is the command line the scheduled job runs.

    ``install`` / ``uninstall`` / ``status`` manage the OS scheduling;
    ``run`` is what the scheduler itself invokes on each tick.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    cmd = args[0] if args else "status"
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    elif cmd == "status":
        print(status())
    elif cmd == "run":
        return run_once()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
