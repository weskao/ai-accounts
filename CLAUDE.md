# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Keep `README.md` in sync (required)

[`README.md`](README.md) is the user-facing reference for every command. Whenever you add a new command/entry point, change a flag, rename an option, or alter any user-visible behavior or output, **update `README.md` in the same change** — add or revise the relevant description and usage examples so the docs never lag the code. This includes the command table near the top and the per-tool section. A code change that touches user-visible behavior without a matching `README.md` edit is incomplete.

## Reuse shared helpers before implementing

Before writing any new helper, **first check [`src/ai-accounts/_utils.py`](src/ai-accounts/_utils.py)** for an existing function that already does the job. Reuse it instead of reimplementing.

`_utils.py` is the single home for cross-cutting, cross-platform concerns. As of now it already provides:

- **Colors / logging**: `YELLOW`, `GREEN`, `RED`, `DIM`, `RESET`; `log_yellow()`, `log_green()`, `log_red()` (TTY- and Windows-aware ANSI).
- **Tool availability**: `have(cmd)` — is a binary on `PATH`?
- **External binary bootstrap**: `ensure_tool(pkg, cmd=None)` — verify a CLI tool is present; auto-install via Homebrew on macOS, print a per-OS install hint elsewhere. Add new tools to the `_INSTALL_HINTS` map rather than hardcoding install commands.
- **Python package bootstrap**: `ensure_python_package(import_name, pip_name=None)` — verify an importable package, auto-install via `pip` if missing.
- **Clipboard**: `copy_to_clipboard(text)`, `output_and_copy(text)` (macOS / Windows / Linux).
- **Subprocess**: `run(cmd, **kwargs)` — `subprocess.run` wrapper defaulting to `text=True`.
- **Account stores**: `resolve_account_dir(env_var, default_dir, legacy_dir)` — env override → central `~/.ai-accounts/` default, auto-migrating a legacy in-dotdir store.
- **Git**: `is_git_repo(path)`, `git_sync(repo_dir, file_path, commit_msg)` (add → commit → pull --rebase → push, with union-conflict auto-resolution).

### Rules

1. **Search first.** Read `_utils.py` before adding a helper. If a suitable function exists, import and use it.
2. **Extract, don't duplicate.** If two or more tool modules would share logic, add it to `_utils.py` and have both call it — never copy-paste between modules.
3. **Keep tools platform-agnostic.** OS-specific behavior (clipboard, package installation, ANSI) belongs in `_utils.py`, funneled through one function, so individual tool modules stay clean.

### Beyond `_utils.py`: shared account-tool helpers

`_utils.py` holds cross-*platform* concerns. Domain logic shared between the account tools (`codex-accounts`, `claude-accounts`, `agy-accounts`) lives elsewhere — check before reimplementing:

- **Usage / table formatting**: [`usage_format.py`](src/ai-accounts/usage_format.py) (`UsageWindow`, `format_usage_window`, `align_usage_cells`, `format_unix_time_compact`) is the shared formatting module for **all three** account tools — imported by `gemini_accounts.py`, `claude_accounts.py`, and `claude_usage.py`. (Formerly `codex_usage.py`: codex-accounts came first and the others built on it; renamed to a provider-neutral name to match its shared role.) `gemini_usage.py`/`claude_usage.py` hold only per-provider `fetch_usage` API logic, not formatting — don't duplicate these helpers there.
- **Profile store**: each tool keeps profiles as `<name>.json` files plus a `.current-profile` marker under the central `~/.ai-accounts/<app>/accounts/` dir (override via `CODEX_ACCOUNT_DIR` / `CLAUDE_ACCOUNT_DIR` / `ANTIGRAVITY_ACCOUNT_DIR`), resolved through `_utils.resolve_account_dir` — kept out of the app dotdirs so dotfiles repos never swallow token snapshots.

## Test fixtures: no real data

Tests must use placeholder values only — never real emails, names, account IDs, or other
data copied from a user's terminal output, screenshots, or live accounts. Use
`user@example.com`, generic names ("Test"/"測試"), and made-up IDs instead.

## Commands

This project is managed with [`uv`](https://docs.astral.sh/uv/) (see `uv.lock`).

- **Build**: `uv build` (hatchling backend)
- **Test**: `uv run pytest`
- **Install editable (local dev)**: `uv tool install --editable .`
- **Run a tool without installing**: `uv run <entry-point>` (e.g. `uv run codex-accounts who`)

No linter/formatter is configured; match existing style when editing.
