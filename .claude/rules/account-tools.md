---
paths:
  - src/ai_accounts/*_accounts.py
  - src/ai_accounts/*_usage.py
  - src/ai_accounts/usage_format.py
---
# Shared account-tool helpers

- Formatting lives in `usage_format.py` (`UsageWindow`, `format_usage_window`,
  `align_usage_cells`, `format_unix_time_compact`) for every account tool. `*_usage.py` holds
  only the provider's `fetch_usage` API logic, never formatting.
- `list` STATE column: `usage_format.state_cells(rows, active)` builds `ACTIVE` / `SAME ACCT` /
  `—`. A new `<tool>_accounts.py` passes `(profile, account id or email, or None)` per row and
  zips the result in. `tests/test_usage_format.py::TestEveryListUsesStateCells` fails any
  `*_accounts.py` that hand-rolls it.
- Profile store: `<name>.json` files plus a `.current-profile` marker under
  `~/.ai-accounts/<app>/accounts/` (env override such as `CODEX_ACCOUNT_DIR`), resolved through
  `_utils.resolve_account_dir`. Never inside the app's own dotdir: dotfiles repos would swallow
  token snapshots.
