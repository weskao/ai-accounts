---
paths:
  - src/ai_accounts/config_schema.py
  - src/ai_accounts/config_menu.py
  - src/ai_accounts/autoswitch.py
---
# Adding a config setting: one `Field` in `config_schema.FIELDS`

`FIELDS` is the single source of truth for every config key (type, default, allowed values,
validator, masked flag, label). `autoswitch.DEFAULTS` and `NOTIFY_CHANNELS` derive from it; the
menu, the non-TTY fallback and the text commands are schema-driven.

1. Add the `Field`, nothing else. If a new key needs an edit anywhere but `FIELDS`, the menu is
   buggy: fix the menu, don't special-case the key.
2. No per-key `if`/`elif` chains. Cyclable rows derive from the descriptor. Per-key semantics in
   business logic (`config_flag("enabled")`, `cfg["switch_when_used_pct"]`) are fine; a second
   list of key names is not.
3. Secrets set `masked=True`. `config_schema.mask_secret` is the only masking implementation
   (`autoswitch._mask` aliases it). A masked key in `DEFAULTS` but not in `FIELDS` leaks through
   `config get`.
4. `save_config` merges `{**on_disk, **updates}`: pass only the touched keys, or a stale
   snapshot reverts another terminal's write.
5. Every `save_config` caller catches `ValueError`: it validates before writing, so an invalid
   value already on disk would otherwise traceback.
