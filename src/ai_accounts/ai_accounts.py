"""ai-accounts — drive every AI account tool at once.

Fans a subcommand out to all six per-provider tools (codex-accounts,
claude-accounts, agy-accounts, grok-accounts, vibe-accounts, copilot-accounts)
so one command covers every provider. ``list``
runs the providers in parallel and prints each provider's table as soon as
it finishes fetching (its output embeds ANSI unconditionally, so color
survives the pipe); every other command runs the providers one at a time
with live stdio, so interactive flows (switch pickers, login-switch) and
TTY-gated color keep working.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config_menu as cm
from . import _present, i18n
from ._utils import BOLD, CYAN, RESET, Spinner, log_red
from .providers import PROVIDERS

# (display label, importable module). Each module is `python -m`-runnable and
# understands the same subcommand set as the others. Derived from
# providers.PROVIDERS — the single source of truth for the provider list.
_TOOLS: list[tuple[str, str]] = [(p.label, p.module) for p in PROVIDERS]

# Subcommands every per-provider tool understands (shared surface). Anything
# outside this set is rejected rather than blindly forwarded.
_COMMANDS = frozenset(
    {
        "who",
        "current",
        "usage",
        "save",
        "list",
        "switch",
        "remove",
        "refresh",
        "sync",
        "login-switch",
        "autoswitch",
    }
)

HELP = """ai-accounts — drive every AI account tool at once

USAGE
  ai-accounts                        Show this help (the available commands)
  ai-accounts list [--json]          List all provider profiles (providers run in parallel);
                                      --json merges every provider's own --json output into
                                      one object keyed by provider (e.g. "codex-accounts": [...]);
                                      a provider that fails gets {"error": "..."} instead
  ai-accounts who | current          Show the active account for every provider
  ai-accounts usage [--json]         Show only the active account's usage row per provider;
                                      --json merges every provider's --json output the same way
  ai-accounts refresh [<name>|--all] Refresh tokens across every provider
  ai-accounts sync                   Sync active auth back to its profile, every provider
  ai-accounts save [<name>]          Save the current login in every provider;
                                      no name = each provider derives its own name from
                                      its own active account's email (may differ per
                                      provider if they're logged into different accounts —
                                      intentional forwarding behavior, not a bug)
  ai-accounts switch [<name>]        Switch profile in every provider (interactive, one at a time)
  ai-accounts remove [<name>]        Remove profile in every provider; no name = interactive
                                      picker for each provider in turn
  ai-accounts login-switch <name>    Fresh login + save as <name>, every provider (interactive)
  ai-accounts autoswitch             Run the low-quota auto-switch check for every provider now
  ai-accounts autoswitch setup       One-time install of event hooks and timer fallback
  ai-accounts config                 Interactive config menu (arrow keys; numbered
                                      fallback when stdin isn't a TTY)
  ai-accounts config get [key]       Print the auto-switch config (or just one key); the
                                      telegram bot token is always masked
  ai-accounts config set <key> <val> Set one auto-switch config key (rejects unknown keys)
  ai-accounts install-timer [--interval N]
                                      Schedule the auto-switch check with the OS (default:
                                      every 1800s / 30 minutes)
  ai-accounts uninstall-timer       Remove the scheduled auto-switch check
  ai-accounts timer-status          Report whether the auto-switch check is scheduled
  ai-accounts doctor [--json]        Offline health check per provider: CLI binary on PATH,
                                      credential-store reachability, saved-profile JSON
                                      validity/expiry, and auto-switch timer status;
                                      pass/fail table by default, one JSON document with --json
  ai-accounts -h | --help | help     Show this help

Each command is forwarded to codex-accounts, claude-accounts, agy-accounts, grok-accounts, vibe-accounts, and copilot-accounts.
`list` runs them concurrently and prints each table as soon as it finishes
(fastest provider first), with a spinner tracking how many are still
fetching in between; every other command runs them one provider at a time
with live output, so interactive pickers and login flows work and color is
preserved. Any argument after the command (e.g. a profile name or `--all`) is
passed through to each provider — except after `autoswitch`, which accepts
only the local `setup` action and otherwise rejects extra arguments.
`--json` after `list`/`usage` is the one other exception: it also runs
providers concurrently and captures output (never live stdio), merging each
provider's own `--json` document into one object printed once.
"""


# One accent color per provider, so six stacked `list`/`switch`/etc. blocks
# stay visually distinct instead of six identical cyan rules.
_PROVIDER_COLOR = {p.label: p.cli_color for p in PROVIDERS}


def _header(label: str) -> None:
    accent = _PROVIDER_COLOR.get(label, CYAN)
    print(f"{BOLD}{accent}━━━ {label} ━━━{RESET}")


def _run_list(module: str) -> subprocess.CompletedProcess[str]:
    # ponytail: subprocess (not in-process) so each provider's stdout stays
    # isolated for parallel capture; `-m` avoids depending on PATH entry points.
    # capture_output=True gives the child a pipe instead of a real terminal, so
    # its own width detection would find nothing — forward our detected width
    # via COLUMNS (undetectable stays unset; the child applies its own wide
    # fallback).
    env = {**os.environ}
    width = _present.terminal_width()
    if width is not None:
        env["COLUMNS"] = str(width)
    return subprocess.run(
        [sys.executable, "-m", module, "list"],
        capture_output=True,
        text=True,
        env=env,
    )


def cmd_list() -> int:
    # Print each provider's table the moment it finishes, instead of waiting
    # for the slowest one. A fresh Spinner between prints tracks how many are
    # still outstanding, so the count shrinks live as results land; once the
    # last provider prints, no spinner remains.
    exit_code = 0
    total = len(_TOOLS)
    with ThreadPoolExecutor(max_workers=total) as pool:
        futures = {pool.submit(_run_list, module): label for label, module in _TOOLS}
        pending = as_completed(futures)
        remaining = total
        while remaining > 0:
            message = (
                f"Fetching accounts from {total} providers…"
                if remaining == total
                else f"Fetching remaining {remaining} provider{'' if remaining == 1 else 's'}…"
            )
            with Spinner(message):
                future = next(pending)
            remaining -= 1
            label = futures[future]
            result = future.result()
            _header(label)
            stdout = (result.stdout or "").strip("\n")
            if stdout:
                print(stdout)
            stderr = (result.stderr or "").strip("\n")
            if stderr:
                print(stderr, file=sys.stderr)
            if result.returncode != 0:
                exit_code = result.returncode
            print()
    return exit_code


def _run_json(module: str, command: str) -> subprocess.CompletedProcess[str]:
    # Same capture-not-inherit rationale as _run_list, extended to `usage
    # --json`: the child's stdout is one JSON document to be parsed, not
    # printed live, so it must go through a pipe rather than inherited stdio.
    return subprocess.run(
        [sys.executable, "-m", module, command, "--json"],
        capture_output=True,
        text=True,
        env=os.environ,
    )


def cmd_json(command: str) -> int:
    # `list --json` / `usage --json`: run every provider concurrently (the
    # same capturing pattern as cmd_list, never cmd_forward's inherited
    # stdio), then merge each provider's own JSON array into one object keyed
    # by provider label, in providers.PROVIDERS order. A provider that exits
    # non-zero or prints unparseable JSON gets {"error": "..."} instead of
    # crashing the merge.
    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        futures = {pool.submit(_run_json, p.module, command): p.label for p in PROVIDERS}
        results = {futures[future]: future.result() for future in as_completed(futures)}

    merged: dict[str, object] = {}
    for provider in PROVIDERS:
        result = results[provider.label]
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip() or f"exited {result.returncode}"
            merged[provider.label] = {"error": message}
            continue
        try:
            merged[provider.label] = json.loads(result.stdout)
        except ValueError as exc:
            merged[provider.label] = {"error": f"invalid JSON output: {exc}"}
    print(json.dumps(merged))
    return 0


def cmd_forward(argv: list[str]) -> int:
    # Everything but `list`: run one provider at a time with inherited stdio so
    # interactive prompts work and TTY-gated color is preserved. `argv` (command
    # + any extra args) is passed through verbatim to each provider.
    exit_code = 0
    for label, module in _TOOLS:
        _header(label)
        result = subprocess.run([sys.executable, "-m", module, *argv])
        if result.returncode != 0:
            exit_code = result.returncode
        print()
    return exit_code


# ── timer subcommands (handled locally — never forwarded to a provider) ─────


def cmd_install_timer(rest: list[str]) -> int:
    from . import autoswitch_timer

    interval = autoswitch_timer.DEFAULT_INTERVAL_SEC
    if rest:
        if rest[0] == "--interval" and len(rest) == 2:
            try:
                interval = int(rest[1])
            except ValueError:
                log_red(f"❌ --interval must be an integer number of seconds, got {rest[1]!r}")
                return 1
        else:
            log_red("❌ Usage: ai-accounts install-timer [--interval <seconds>]")
            return 1
    autoswitch_timer.install(interval)
    print(f"Installed the auto-switch timer (every {interval}s).")
    return 0


def cmd_uninstall_timer() -> int:
    from . import autoswitch_timer

    autoswitch_timer.uninstall()
    print("Uninstalled the auto-switch timer.")
    return 0


def cmd_timer_status() -> int:
    from . import autoswitch_timer

    print(autoswitch_timer.status())
    return 0


def cmd_autoswitch_setup() -> int:
    from . import autoswitch_setup

    autoswitch_setup.install()
    print("Installed auto-switch event hooks and timer fallback.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_present.format_help(i18n.t("help.ai_accounts", default=HELP)))
        return 0

    command = argv[0]
    # 🔴 Intercepted BEFORE the _COMMANDS gate below, and deliberately absent
    # from _COMMANDS: falling through to cmd_forward would make all six
    # provider subprocesses each print their own "Unknown command".
    if command == "config":
        return cm.cmd_config(argv[1:], prog="ai-accounts")
    if command == "install-timer":
        return cmd_install_timer(argv[1:])
    if command == "uninstall-timer":
        return cmd_uninstall_timer()
    if command == "timer-status":
        return cmd_timer_status()
    if command == "doctor":
        from . import doctor

        return doctor.run_doctor("--json" in argv[1:])

    # `autoswitch` accepts one local setup action; providers ignore every other
    # trailing arg, so `autoswitch install-timer` would run
    # six quota checks and install nothing — silently, while the user believes
    # a scheduler was registered. Reject it and name the working spelling.
    if command == "autoswitch" and argv[1:] == ["setup"]:
        return cmd_autoswitch_setup()
    if command == "autoswitch" and len(argv) > 1:
        log_red(
            f"❌ `ai-accounts autoswitch` accepts only `setup` (got: {' '.join(argv[1:])}). "
            "To install automatic switching: `ai-accounts autoswitch setup`."
        )
        return 1

    if command not in _COMMANDS:
        log_red(f"❌ Unknown command: {command}")
        print(_present.format_help(i18n.t("help.ai_accounts", default=HELP)))
        return 1

    if command == "list":
        return cmd_json("list") if "--json" in argv[1:] else cmd_list()
    if command == "usage" and "--json" in argv[1:]:
        return cmd_json("usage")
    return cmd_forward(argv)


if __name__ == "__main__":
    raise SystemExit(main())
