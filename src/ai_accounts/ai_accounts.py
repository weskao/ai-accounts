"""ai-accounts — drive every AI account tool at once.

Fans a subcommand out to all four per-provider tools (codex-accounts,
claude-accounts, agy-accounts, grok-accounts) so one command covers every provider. ``list``
runs the providers in parallel and prints each provider's table as soon as
it finishes fetching (its output embeds ANSI unconditionally, so color
survives the pipe); every other command runs the providers one at a time
with live stdio, so interactive flows (switch pickers, login-switch) and
TTY-gated color keep working.
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from ._utils import BOLD, CYAN, RESET, Spinner, log_red

# (display label, importable module). Each module is `python -m`-runnable and
# understands the same subcommand set as the others.
_TOOLS: list[tuple[str, str]] = [
    ("codex-accounts", "ai_accounts.codex_accounts"),
    ("claude-accounts", "ai_accounts.claude_accounts"),
    ("agy-accounts", "ai_accounts.gemini_accounts"),
    ("grok-accounts", "ai_accounts.grok_accounts"),
]

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
    }
)

HELP = """ai-accounts — drive every AI account tool at once

USAGE
  ai-accounts                        Show this help (the available commands)
  ai-accounts list                   List all provider profiles (providers run in parallel)
  ai-accounts who | current          Show the active account for every provider
  ai-accounts usage                  Show only the active account's usage row per provider
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
  ai-accounts -h | --help | help     Show this help

Each command is forwarded to codex-accounts, claude-accounts, agy-accounts, and grok-accounts.
`list` runs them concurrently and prints each table as soon as it finishes
(fastest provider first), with a spinner tracking how many are still
fetching in between; every other command runs them one provider at a time
with live output, so interactive pickers and login flows work and color is
preserved. Any argument after the command (e.g. a profile name or `--all`) is
passed through to each provider.
"""


def _header(label: str) -> None:
    print(f"{BOLD}{CYAN}━━━ {label} ━━━{RESET}")


def _run_list(module: str) -> subprocess.CompletedProcess[str]:
    # ponytail: subprocess (not in-process) so each provider's stdout stays
    # isolated for parallel capture; `-m` avoids depending on PATH entry points.
    return subprocess.run(
        [sys.executable, "-m", module, "list"],
        capture_output=True,
        text=True,
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0

    command = argv[0]
    if command not in _COMMANDS:
        log_red(f"❌ Unknown command: {command}")
        print(HELP)
        return 1

    if command == "list":
        return cmd_list()
    return cmd_forward(argv)


if __name__ == "__main__":
    raise SystemExit(main())
