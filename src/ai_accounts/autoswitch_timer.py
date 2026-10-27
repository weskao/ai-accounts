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

import json
import os
import plistlib
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import _utils as u
from . import (
    ai_accounts,
    autoswitch_hooks,
    config_schema,
    host_identity,
    i18n,
    quota_reset,
    refresh_report,
)
from . import autoswitch as aw

LABEL = "com.ai_accounts.autoswitch"
# The OS wakes the job every `timer_interval_min`, but a tick is cheap unless something is due:
# the heavy work (provider probes, token refresh, routine reset scan) runs at
# most every FULL_TICK_SEC, and the reset scan also runs early, on a 5-minute
# tick, once a recorded quota deadline has passed — so a reset notification
# lands within ~5 minutes instead of up to 30.
_INTERVAL_FIELD = config_schema.field("timer_interval_min")
DEFAULT_INTERVAL_SEC = _INTERVAL_FIELD.default * 60
FULL_TICK_SEC = 30 * 60  # 30 minutes
# A scheduler fires a little early or late; a tick this close to the cadence
# counts as on time, so 6 x 5 min reliably yields a full tick every 30.
_TICK_SLACK_SEC = 60
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

# shortcut: only the three folders macOS commonly gates; extend if another shows up.
_TCC_FOLDERS = ("Documents", "Desktop", "Downloads")


def _tcc_blocked_source() -> str | None:
    """The macOS-protected folder this package is loaded from, else None.

    launchd jobs get no access to ~/Documents, ~/Desktop or ~/Downloads, so an
    editable install living there makes every tick die on ``import ai_accounts``
    (exit 1, no output) — the scheduled autoswitch and refresh silently never run.
    """
    source = Path(__file__).resolve()
    home = Path.home().resolve()
    return next((name for name in _TCC_FOLDERS if source.is_relative_to(home / name)), None)


def _install_macos(interval_sec: int) -> None:
    if blocked := _tcc_blocked_source():
        u.log_yellow(
            f"⚠️  ai-accounts is installed from ~/{blocked}, which macOS hides from launchd, "
            "so the timer will fail at start-up. Reinstall without --editable: "
            "uv tool install --reinstall <path-to-repo>"
        )
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
    # Reload before restarting: systemd caches unit contents, so a re-install
    # with a new interval is ignored until it re-reads them.
    u.run(["systemctl", "--user", "daemon-reload"], check=True)
    u.run(["systemctl", "--user", "enable", f"{LABEL}.timer"], check=True)
    u.run(["systemctl", "--user", "restart", f"{LABEL}.timer"], check=True)


def _install_linux_cron(interval_sec: int) -> None:
    existing = u.run(["crontab", "-l"], capture_output=True)
    lines = [
        line for line in (existing.stdout or "").splitlines() if CRON_TAG not in line
    ]
    lines.append(_cron_line(interval_sec))
    # One tick at boot too, matching launchd's RunAtLoad and systemd's
    # OnBootSec — otherwise a reset that happened while the machine was off
    # waits for the next */N slot. Same tag, so uninstall drops both lines.
    lines.append(f"@reboot {_run_command()} {CRON_TAG}")
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


def configured_interval_sec() -> int:
    """The `timer_interval_min` setting in seconds; the default if unreadable."""
    raw = aw.load_config().get(_INTERVAL_FIELD.key, _INTERVAL_FIELD.default)
    try:
        return _INTERVAL_FIELD.parse(str(raw)) * 60
    except ValueError:  # a hand-edited non-number
        return DEFAULT_INTERVAL_SEC


def install(interval_sec: int | None = None) -> None:
    """Register the periodic autoswitch check with the OS scheduler.

    *interval_sec* defaults to the ``timer_interval_min`` setting.
    """
    if interval_sec is None:
        interval_sec = configured_interval_sec()
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
    event-triggered checks on the same cross-platform support matrix. copilot,
    grok, devin and opencode have no Stop hook to install, so the timer is their
    only trigger.
    """
    modules = [autoswitch_hooks.module(provider) for provider in autoswitch_hooks.providers()]
    modules += [
        "ai_accounts.copilot_accounts",
        "ai_accounts.grok_accounts",
        "ai_accounts.devin_accounts",
        "ai_accounts.opencode_accounts",
    ]
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
_REVOKED_MARKERS = ("token revoked", "revoked (re-login required)")

# ponytail: fixed per-provider ceiling, not a config knob — the CLI-proxy
# fallback (agy/grok, when a direct OAuth refresh can't run) spawns a vendor
# binary with no bound of its own, and macOS launchd won't start a new tick
# while one is still running, so one wedged subprocess would silently kill
# the timer forever. Upgrade path: make this configurable if a provider's CLI
# proxy routinely needs longer than this.
_REFRESH_TIMEOUT_SEC = 300


def _run_token_refresh_everywhere() -> str:
    """Default *refresh*: direct-OAuth token refresh, across every provider.

    Drives the same provider CLIs :func:`_run_autoswitch_everywhere` does
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
        # The header goes into the captured text too (uncolored — the printed
        # one carries ANSI), so refresh_report can tell which provider a
        # failing profile belongs to. Without it the combined output is one
        # anonymous blob.
        output_parts.append(f"━━━ {label} ━━━")
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
    collect: Callable[[], quota_reset.Snapshot] | None = None,
) -> int:
    """The scheduled job's entry point — invoked on every timer tick.

    Three INDEPENDENT gates, each reading its own config flag, run in this
    fixed order — ``enabled`` (quota auto-switch check), then
    ``token_refresh`` (renewing tokens across every provider), then
    ``reset_notify`` (quota-reset notifications, :func:`quota_reset.run_tick`)
    last. Any subset runs on a given tick: a user who does not want automatic
    account switching still wants live tokens (plan.md §4 phase 4 — this is
    the fix for `enabled=false` silently disabling refresh too), and either
    of those being off must not silently disable reset notifications either.
    The order is fixed, not stylistic: running ``reset_notify`` last means a
    bug in this newer, smaller feature can never delay or block the
    established switch/refresh behavior the other two gates already provide
    on every tick — a wedge or exception in `collect()` still lands after
    `check`/`refresh` have already completed.

    *check* / *refresh* / *collect* are the actual quota-probe/switch,
    token-refresh, and reset-notify-snapshot calls; when omitted *check* and
    *refresh* default to :func:`_run_autoswitch_everywhere` /
    :func:`_run_token_refresh_everywhere`, and *collect* defaults to
    :func:`quota_reset.run_tick`'s own default collector (a test may still
    inject its own to observe the call without driving real provider
    subprocesses).

    ``enabled`` and ``token_refresh`` run only on a full tick (at most every
    :data:`FULL_TICK_SEC`); ``reset_notify`` also runs between full ticks when
    :func:`quota_reset.reset_due` says a recorded deadline just passed.

    A refresh tick alerts via :func:`ai_accounts.autoswitch.notify_once` only
    when the refresh output mentions a revoked refresh token — never for a
    routine, successful rotation.
    """
    now = int(time.time())
    full = _full_tick_due(now)
    if full:
        # Stamped before the work, so a crashing provider cannot turn every
        # 5-minute tick into a full one.
        _write_last_full_tick(now)
    if full and aw.config_flag("enabled"):  # fail closed: only a JSON `true` runs
        (check or _run_autoswitch_everywhere)()
    if full and aw.config_flag("token_refresh"):  # fail closed: same rule, own flag
        output = (refresh or _run_token_refresh_everywhere)()
        if any(marker in output.lower() for marker in _REVOKED_MARKERS):
            _report_revoked(output)
    if (full and aw.config_flag("reset_expiry_notify")) or (  # fail closed: own flags
        aw.config_flag("reset_notify") and (full or quota_reset.reset_due(now, FULL_TICK_SEC))
    ):
        quota_reset.run_tick(collect=collect, full=full)
    return 0


def _tick_state_path() -> Path:
    return aw.config_path().parent / "timer-state.json"


def _full_tick_due(now: int) -> bool:
    """True when the last full tick is FULL_TICK_SEC old, missing, or in the future."""
    try:
        last = json.loads(_tick_state_path().read_text(encoding="utf-8"))["last_full_tick"]
        elapsed = now - int(last)
    except (OSError, ValueError, KeyError, TypeError):
        return True
    # A negative age is a backward clock jump; don't stall until wall time catches up.
    return elapsed < 0 or elapsed >= FULL_TICK_SEC - _TICK_SLACK_SEC


def _write_last_full_tick(now: int) -> None:
    try:
        u.atomic_write_json(_tick_state_path(), {"last_full_tick": now})
    except OSError as exc:
        u.log_red(f"Could not record timer state: {exc}")


def _report_revoked(output: str) -> None:
    """Show and send the re-login report for one refresh tick's *output*.

    The terminal report is printed whether or not the notification goes out —
    a run inside its cooldown still has to say what it found — and the
    de-duplication key names the profiles, so a NEWLY revoked account alerts
    at once instead of hiding behind the previous alert's hour.

    Falls back to the generic one-liner when nothing could be attributed:
    ``_REVOKED_MARKERS`` matched, so an alert is owed even if the wording
    changed under the parser.
    """
    records = refresh_report.parse(output)
    if not records:
        aw.notify_once(
            "token-refresh:revoked",
            i18n.t("notify.revoked.title"),
            i18n.t("notify.revoked.body"),
        )
        return
    refresh_report.print_report(records)
    key = ",".join(sorted(f"{r.provider}/{r.profile}" for r in records))
    aw.notify_once(
        f"token-refresh:revoked:{key}",
        refresh_report.title(records),
        refresh_report.message(records),
    )


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


def _scheduler_footer() -> str:
    if u.IS_MACOS:
        return host_identity.launchd_footer(LABEL)
    if u.IS_WINDOWS:
        return host_identity.scheduler_footer("Task Scheduler", LABEL)
    if u.IS_LINUX and _systemd_timer_path().exists():
        return host_identity.scheduler_footer("systemd", f"{LABEL}.timer")
    if u.IS_LINUX:
        return host_identity.scheduler_footer("cron", CRON_TAG.removeprefix("# "))
    return ""


if __name__ == "__main__":
    if sys.argv[1:] == ["run"]:
        os.environ["AI_ACCOUNTS_SCHEDULER_FOOTER"] = _scheduler_footer()
    raise SystemExit(main())
