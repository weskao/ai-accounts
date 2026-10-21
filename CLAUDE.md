# CLAUDE.md

## Shared helpers first

- `src/ai_accounts/_utils.py` is the single home for cross-cutting, cross-platform concerns
  (ANSI/logging, tool and package bootstrap, clipboard, subprocess, account-dir resolution,
  keychain/keyring stores, git sync, device label). Read it before writing a helper. Logic two
  modules would share goes there, never copy-pasted. OS-specific behaviour funnels through one
  `_utils` function so tool modules stay platform-agnostic.
- Device label: `autoswitch.notify()` is the only caller of `source_device()` and appends it as
  the last line for `desktop` / `telegram`. Call sites never append it themselves (double label)
  and never re-shell to `scutil` / `gethostname`.
- User-visible change → update `README.md` (command table and per-tool section) in the same
  change.
- Scoped rules in `.claude/rules/` (loaded by path in Claude Code; other agents read the one
  matching the file): `account-tools.md`, `config-schema.md`.

## Commands (uv)

- Test `uv run pytest` · Lint `uv run ruff check .` · Build `uv build`
- Install editable `uv tool install --editable .` · Run uninstalled `uv run <entry-point>`

## Cross-platform

Code must run on macOS, Linux, and Windows (paths, separators, shell commands, line
endings, `$HOME` vs `%USERPROFILE%`, file locking, symlinks, terminal/ANSI). Guard
platform-specific code and say so.

- Reuse the shared platform helpers first: `_utils.py`.
- Text I/O always passes `encoding="utf-8"` (Windows defaults to cp1252).
- Windows is verified by CI's `windows-latest` job, not locally; POSIX-only tests are
  skipped on Windows (`os.name == "nt"`).
