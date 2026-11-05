---
paths:
  - src/ai_accounts/migrations.py
  - src/ai_accounts/*_accounts.py
  - src/ai_accounts/_utils.py
  - src/ai_accounts/autoswitch_hooks.py
  - src/ai_accounts/autoswitch_timer.py
---
# Changing where or how ai-accounts stores data: ship a migration

- A change to an ai-accounts-owned on-disk layout (store path, file shape) appends one
  `Migration(N + 1, ...)` to `migrations.MIGRATIONS` and bumps `CURRENT_SCHEMA`. Its `plan()`
  only detects and returns ops; it never writes. Never delete or edit a shipped migration:
  users skip releases and replay the whole chain.
- A provider joins store relocation by defining `_store_paths()` (env override, default,
  legacy) in its own module; `migrations.py` reads it. No per-provider code there.
- Migrations never write a credential store, refresh a token or touch vendor CLI files. Those
  stay lazy, on the owning code's read/save path.
- Tests: `conftest.py` sets `AI_ACCOUNTS_NO_AUTO_MIGRATE=1` for the whole suite, so a wrapped
  `main()` never migrates the real HOME. Migration tests delete it inside a throwaway HOME
  and replace the timer/hook installers with recorders.
