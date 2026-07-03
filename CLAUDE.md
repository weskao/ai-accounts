# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Reuse shared helpers before implementing

Before writing any new helper, **first check [`src/ai-accounts/_utils.py`](src/ai-accounts/_utils.py)** for an existing function that already does the job. Reuse it instead of reimplementing.

`_utils.py` is the single home for cross-cutting, cross-platform concerns. As of now it already provides:

- **Colors / logging**: `YELLOW`, `GREEN`, `RED`, `DIM`, `RESET`; `log_yellow()`, `log_green()`, `log_red()` (TTY- and Windows-aware ANSI).
- **Tool availability**: `have(cmd)` — is a binary on `PATH`?
- **External binary bootstrap**: `ensure_tool(pkg, cmd=None)` — verify a CLI tool is present; auto-install via Homebrew on macOS, print a per-OS install hint elsewhere. Add new tools to the `_INSTALL_HINTS` map rather than hardcoding install commands.
- **Python package bootstrap**: `ensure_python_package(import_name, pip_name=None)` — verify an importable package, auto-install via `pip` if missing.
- **Clipboard**: `copy_to_clipboard(text)`, `output_and_copy(text)` (macOS / Windows / Linux).
- **Subprocess**: `run(cmd, **kwargs)` — `subprocess.run` wrapper defaulting to `text=True`.
- **Git**: `is_git_repo(path)`, `git_sync(repo_dir, file_path, commit_msg)` (add → commit → pull --rebase → push, with union-conflict auto-resolution).

### Rules

1. **Search first.** Read `_utils.py` before adding a helper. If a suitable function exists, import and use it.
2. **Extract, don't duplicate.** If two or more tool modules would share logic, add it to `_utils.py` and have both call it — never copy-paste between modules.
3. **Keep tools platform-agnostic.** OS-specific behavior (clipboard, package installation, ANSI) belongs in `_utils.py`, funneled through one function, so individual tool modules stay clean.

## Commands

This project is managed with [`uv`](https://docs.astral.sh/uv/) (see `uv.lock`).

- **Build**: `uv build` (hatchling backend)
- **Test**: `uv run pytest`
- **Install editable (local dev)**: `uv tool install --editable .`
- **Run a tool without installing**: `uv run <entry-point>` (e.g. `uv run codex-accounts who`)

No linter/formatter is configured; match existing style when editing.
