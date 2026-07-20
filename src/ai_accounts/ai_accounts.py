"""ai-accounts — list every AI account profile at once.

Fans out the per-provider ``list`` commands (codex-accounts, claude-accounts,
agy-accounts) in parallel and prints their tables back-to-back, so one command
shows every saved profile + usage without waiting for each provider serially.
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from ._utils import RESET, log_red

BOLD = "\033[1m"
CYAN = "\033[1;36m"

# (display label, importable module). Each module is `python -m`-runnable with a
# `list` command; its table output embeds ANSI unconditionally, so capturing via
# a pipe preserves color — no pty needed, and it works cross-platform.
_TOOLS: list[tuple[str, str]] = [
    ("codex-accounts", "ai_accounts.codex_accounts"),
    ("claude-accounts", "ai_accounts.claude_accounts"),
    ("agy-accounts", "ai_accounts.gemini_accounts"),
]

HELP = """ai-accounts — list every AI account profile at once

USAGE
  ai-accounts             List all provider profiles (providers run in parallel)
  ai-accounts list        Same as above
  ai-accounts -h | --help Show this help

Runs `codex-accounts list`, `claude-accounts list`, and `agy-accounts list`
concurrently, then prints each provider's table in a fixed order.
"""


def _run_list(module: str) -> subprocess.CompletedProcess[str]:
    # ponytail: subprocess (not in-process) so each provider's stdout stays
    # isolated for parallel capture; `-m` avoids depending on PATH entry points.
    return subprocess.run(
        [sys.executable, "-m", module, "list"],
        capture_output=True,
        text=True,
    )


def cmd_list() -> int:
    with ThreadPoolExecutor(max_workers=len(_TOOLS)) as pool:
        results = list(pool.map(lambda tool: _run_list(tool[1]), _TOOLS))

    exit_code = 0
    for (label, _), result in zip(_TOOLS, results):
        print(f"{BOLD}{CYAN}━━━ {label} ━━━{RESET}")
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(HELP)
        return 0
    if not argv or argv[0] == "list":
        return cmd_list()

    log_red(f"❌ Unknown command: {argv[0]}")
    print(HELP)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
