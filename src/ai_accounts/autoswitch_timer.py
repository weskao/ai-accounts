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
import sys
from pathlib import Path
from typing import Callable, Sequence

from . import _utils as u
from . import ai_accounts
from . import autoswitch as aw

LABEL = "com.ai_accounts.autoswitch"
DEFAULT_INTERVAL_SEC = 30 * 60  # 30 minutes
CRON_TAG = "# ai-accounts-autoswitch"


def _run_command() -> str:
    # ponytail: no shell-quoting of sys.executable — breaks if the interpreter
    # path ever contains a space. Add shlex.quote() if that surfaces.
    return f"{sys.executable} -m ai_accounts.autoswitch_timer run"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_timer_path() -> Path:
    return _systemd_unit_dir() / f"{LABEL}.timer"


def _systemd_service_path() -> Path:
    return _systemd_unit_dir() / f"{LABEL}.service"


def _cron_line(interval_sec: int) -> str:
    # ponytail: not clamped to cron's 0-59 minute field — fine at the shipped
    # 30-min default; clamp to 59 if a caller ever passes a longer interval.
    minutes = max(1, interval_sec // 60)
    return f"*/{minutes} * * * * {_run_command()} {CRON_TAG}"


# ── install ───────────────────────────────────────────────────────────────

def _install_macos(interval_sec: int) -> None:
    # ponytail: re-install (changing the interval) rewrites the plist but
    # doesn't unload the already-loaded job first — launchd won't pick up the
    # new interval until an explicit unload/reload. Fine for a fresh install;
    # revisit if/when a "change interval" flow lands.
    plist_path = _launchd_plist_path()
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
                "RunAtLoad": False,
            }
        )
    )
    u.run(["launchctl", "load", str(plist_path)])


def _install_linux_systemd(interval_sec: int) -> None:
    # ponytail: same re-install caveat as macOS — an already-loaded unit needs
    # `systemctl --user daemon-reload` to notice rewritten unit file contents.
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
            _run_command(),
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


def _run_autoswitch_everywhere() -> None:
    """Default *check*: the real quota-probe/switch, across all four providers.

    Reuses :mod:`ai_accounts.ai_accounts`'s own umbrella fan-out (the same path
    ``ai-accounts autoswitch`` runs through) instead of duplicating the
    provider list here.
    """
    ai_accounts.cmd_forward(["autoswitch"])


def run_once(check: Callable[[], None] | None = None) -> int:
    """The scheduled job's entry point — invoked on every timer tick.

    A silent no-op while the feature's master switch (config ``enabled``) is
    off: no probing, no notification. *check* is the actual quota-probe /
    switch call; when omitted it defaults to :func:`_run_autoswitch_everywhere`
    (a test may still inject its own to observe the call without driving real
    provider subprocesses).
    """
    if not aw.config_flag("enabled"):  # fail closed: only a JSON `true` runs
        return 0
    (check or _run_autoswitch_everywhere)()
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
