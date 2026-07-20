# `ai-accounts list` — per-provider live progress rows

## Goal (acceptance criteria)

- Running `ai-accounts list` interactively shows one live-updating status row
  per still-running provider (codex-accounts / claude-accounts / agy-accounts),
  each displaying that provider's *own* real inner progress message (the same
  text its standalone `codex-accounts list` / `claude-accounts list` /
  `agy-accounts list` spinner would show, e.g.
  `Fetching Antigravity usage… (2/5) gkm85663`), not a generic aggregate count.
- Rows are flush-left, no indentation, one per provider, spinner-frame-first
  (matches the previously approved preview).
- As each provider finishes, its row is cleared and its `━━━ label ━━━` header
  + table print in the same completion-order-first behavior as today; the
  live area just shrinks by one row. No change to final printed output.
- Non-TTY / piped stderr (CI, tests, `| cat`): identical to today — nothing
  extra is written or drawn, `ai-accounts list` behaves exactly as it does
  now.
- No corruption when a row's text (e.g. a long saved email) would exceed the
  terminal width — each row is truncated to fit one physical terminal line so
  the multi-row cursor math never desyncs.
- `README.md`'s `ai-accounts list` description is updated to mention the
  per-provider live rows.

## Problem being solved

`ai_accounts.cmd_list()` (`src/ai-accounts/ai_accounts.py:75`) runs the three
provider `list` subcommands as subprocesses in parallel via
`subprocess.run(capture_output=True)`. Both stdout *and stderr* are captured,
so:

1. Each provider's own `Spinner` (constructed inside `codex_accounts.cmd_list`
   / `claude_accounts.cmd_list` / `gemini_accounts.cmd_list`) never even starts
   its background thread — `Spinner.__init__` gates on `_color_supported()`,
   which checks `sys.stderr.isatty()`, and the child's stderr is a pipe, not a
   TTY.
2. The only visible progress today is the *parent's own* single spinner line
   ("Fetching remaining N providers…"), which knows nothing about what's
   happening inside each provider — not even which provider is closest to
   done, let alone which account it's currently fetching.

This is most visible for `agy-accounts` (Antigravity), which — per
[`docs/agy-parallel-limitation.md`](../../agy-parallel-limitation.md) — must
fetch one profile at a time and already has a carefully-tuned inner spinner
message (`Fetching Antigravity usage… (i/N) <profile>`) that simply isn't
reaching the user when run through `ai-accounts list`.

## Design

### Progress side-channel: env var + temp file

Add to `src/ai-accounts/_utils.py`:

- `_PROGRESS_FILE_ENV = "AI_ACCOUNTS_PROGRESS_FILE"`
- `_write_progress_file(message: str) -> None` — if that env var is set,
  overwrite the file at that path with `message` (best-effort; swallow
  `OSError`). No-op if the env var is unset (the normal case for every
  existing standalone invocation).
- `Spinner` calls `_write_progress_file(self._message)` once in `__enter__`
  (so a row isn't blank before the first `update()`) and again on every
  `update()`. This is the *only* change to `Spinner`, and it's independent of
  `_color_supported()` — it must fire even when the child's own stderr isn't a
  TTY, since that's exactly the situation `ai-accounts list` creates.

Because every provider's per-profile progress already flows through
`Spinner.update()` (directly or via `fetch_parallel`'s `spinner.update(...)`),
**no changes are needed in `codex_accounts.py`, `claude_accounts.py`, or
`gemini_accounts.py`** — they get the new behavior for free.

### `MultiSpinner` (new, in `_utils.py`, next to `Spinner`)

A small terminal-rendering primitive for "N concurrent named things, each with
its own live-updating one-line status," reusing `Spinner`'s TTY/color/ASCII-
fallback gating:

```python
class MultiSpinner:
    def __init__(self, rows: dict[str, Path]) -> None: ...  # label -> progress-file path
    def __enter__(self) -> "MultiSpinner": ...
    def finish(self, label: str) -> None: ...  # clear the live area, drop this row, keep drawing the rest
    def __exit__(self, *exc_info) -> None: ...  # stop the ticker, clear the live area
```

Behavior:

- Ticks on a background thread (reusing `Spinner`'s frame set and interval),
  reading each remaining row's progress file and redrawing all remaining rows
  in place (`\033[{n}A` up, `\033[K` clear + rewrite each line, per tick).
- Each row is `{frame} {label:<width} {message}`, truncated to
  `shutil.get_terminal_size().columns` (measuring **visible** width — i.e.
  ignoring ANSI escape sequences already embedded in `message`, such as the
  magenta profile name — so a long saved email can't wrap the line and desync
  the cursor-up math on the next tick).
- `finish(label)` is called from the main thread right after a provider's
  future resolves and before its header/table print: it takes the render
  lock, clears all currently-live rows, removes `label` from the row set, and
  leaves the cursor at column 0 so the caller's normal `print()` calls append
  as plain scrollback — then the ticker resumes drawing the now-shorter row
  set below that.
- Same `_color_supported()` gate as `Spinner`: if stderr isn't a TTY, `__enter__`
  never starts the thread and `finish()` is a no-op — `ai-accounts list`'s
  output is byte-for-byte what it is today in that case.

### `ai_accounts.py` changes

- `_run_list(module: str, progress_file: Path) -> subprocess.CompletedProcess[str]`
  gains the `progress_file` parameter, passed to the subprocess via
  `env={**os.environ, "AI_ACCOUNTS_PROGRESS_FILE": str(progress_file)}`.
- `cmd_list()`: allocate one temp file per provider under a single
  `tempfile.TemporaryDirectory()` (auto-cleaned on exit, including on
  exception — no manual unlink bookkeeping), seed each with a starting
  message, submit the three `_run_list` futures, and replace the old single
  `Spinner` aggregate-count loop with:
  - `with MultiSpinner({label: path for ...}) as spinner:` wrapping the
    existing `as_completed` loop
  - `spinner.finish(label)` right before printing that provider's header +
    table (same position the old spinner-teardown implicitly happened via the
    `with Spinner(...)` block ending each iteration)
- The old "Fetching accounts from N providers…" / "Fetching remaining N
  providers…" aggregate message is removed — the per-provider rows replace it
  entirely rather than stacking alongside it.

### Tests (`tests/test_ai_accounts.py`)

- Update `_run_list` call sites / the `side_effect` signature in existing
  tests to accept the new `progress_file` parameter.
- Replace `test_list_spinner_messages_count_down_as_providers_finish` (which
  asserts the old aggregate-count message sequence — that mechanism is being
  removed) with a test that mocks `aa.MultiSpinner` the same way the old test
  mocked `aa.Spinner`, asserting: constructed once with all 3 provider labels,
  and `.finish(label)` called once per provider in completion order.
- Keep `test_list_prints_providers_in_completion_order`,
  `test_list_propagates_nonzero_exit`, `test_unknown_command_returns_1`,
  `test_no_args_prints_help_without_running_providers`,
  `test_forward_passes_command_and_args_to_every_provider` — update only the
  `_run_list` signature usage, no behavioral change expected in these.
- New unit test(s) for the added `_utils` pieces: `_write_progress_file`
  writes when the env var is set and no-ops (no exception) when it isn't;
  a `MultiSpinner` visible-width-truncation test with an over-long message
  against a small fake terminal width.

## Out of scope

- `cmd_forward` (every non-`list` subcommand) — untouched, already streams
  real live stdio one provider at a time.
- Any change to what a standalone `codex-accounts list` / `claude-accounts
  list` / `agy-accounts list` prints or how its own spinner behaves when run
  directly (not through `ai-accounts`).
- Fixing the pre-existing single-line `Spinner`'s own wrap-around behavior
  when its message exceeds terminal width (`MultiSpinner` gets width-safe
  truncation because multi-row correctness depends on it; retrofitting it
  onto `Spinner` is a separate, unrelated cleanup).
