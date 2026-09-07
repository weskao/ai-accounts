# TODO — add GitHub Copilot as the sixth managed provider

Goal: `copilot-accounts` with the same subcommand surface as the other five tools
(`who`, `save`, `list`, `usage`, `switch`, `remove`, `refresh`, `sync`, `autoswitch`,
`login-switch`, `config`), forwarded by `ai-accounts`.

Template: `vibe-accounts` was the last provider added. Its three commits are the
checklist for every touch-point — `bc2e56f` (module + entry point + umbrella),
`8a107a1` (README, autoswitch, config_menu, tests), `8d6ee96` (credential store the
CLI actually uses). Follow the same three-step shape.

## 0. Research first (nothing below is verified yet)

- [ ] Where Copilot CLI stores its login. Candidates: `~/.copilot/` (honors
      `XDG_CONFIG_HOME`), OS keyring, or the `gh` CLI token. Check what `copilot` writes
      after `/login` and what it deletes on success — vibe deleted the plaintext copy on a
      successful keyring write and that broke `who`/`switch` until `8d6ee96`.
- [ ] Env-var precedence the CLI honors (`COPILOT_GITHUB_TOKEN`, `GH_TOKEN`,
      `GITHUB_TOKEN`). `switch` must not be shadowed by an exported token; warn like the
      other tools do.
- [ ] Identity: `GET https://api.github.com/user` (login, email) for the default profile
      name. Decide login-vs-email when email is private.
- [ ] Quota: `GET https://api.github.com/copilot_internal/user` — reported to return
      `quota_snapshots.{premium_interactions,chat,completions}` with `entitlement`,
      `remaining`, `percent_remaining`, `quota_reset_date`, plus `copilot_plan`. Confirm
      the endpoint, auth header form, and that a CLI OAuth token is accepted. If it
      works, Copilot gets real autoswitch (unlike grok/vibe).
- [ ] Token lifetime and refresh path. GitHub OAuth app tokens for Copilot may be
      long-lived with no refresh token — if so `refresh` becomes "verify still valid"
      like vibe, and the timer's re-login report needs a `copilot` color/line.
- [ ] Windows/Linux credential store — decide whether `_utils.keychain_*` suffices or
      a `secret-tool`/Credential Manager branch is needed (agy already has one).

## 1. Provider module + entry point

- [ ] `src/ai_accounts/copilot_accounts.py` — copy `vibe_accounts.py` structure.
      Store under `~/.ai-accounts/copilot/accounts/` via `resolve_account_dir(
      "COPILOT_ACCOUNT_DIR", ...)`. Reuse `_utils` (keychain, run, ensure_tool,
      colors), `_present` (tables, pickers, panels), `config_menu`, `usage_format`.
- [ ] `src/ai_accounts/copilot_usage.py` — `fetch_usage` only, returning
      `usage_format.UsageWindow`s (premium requests = the window that matters; monthly
      reset from `quota_reset_date`). No formatting here.
- [ ] `login-switch <name>` → `ensure_tool("copilot")`, run the CLI's login, then save.
- [ ] `pyproject.toml` `[project.scripts]` → `copilot-accounts = "ai_accounts.copilot_accounts:main"`.
- [ ] Never persist an empty secret (see `0.3.0` keychain fix); mask via
      `config_schema.mask_secret`.

## 2. Umbrella and shared wiring (grep `vibe` to find every site)

- [ ] `ai_accounts.py`: `_TOOLS`, `_PROVIDER_STYLE` (icon + color; e.g. `🐙`, BLUE is
      taken by agy — pick an unused one), module docstring, `help` text.
- [ ] `i18n.py`: `help.copilot` zh-TW block; the umbrella help line listing providers.
- [ ] `refresh_report.py`: provider color map; revoked-line phrasing the parser expects
      (see the comment at the top of that file and `autoswitch_timer.py` ~L269).
- [ ] `autoswitch.py` ~L446 restart-strategy map: add `copilot` with the truthful value
      (`auto-restart` only if the CLI can be restarted the way codex/claude are).
- [ ] `autoswitch_timer.py`: include `copilot_accounts refresh --all` in the timer run.
- [ ] `config_menu.py` / `config_schema.py`: only if a Copilot-specific key is needed
      (probably none — auto-switch settings are shared). One `Field` line, nothing else.

## 3. Tests (placeholder data only — see CLAUDE.md)

- [ ] `tests/test_copilot_accounts.py` — mirror `test_vibe_accounts.py`: who/save/list/
      switch/remove/sync against a temp account dir and a faked credential store;
      `fetch_usage` against a canned `copilot_internal/user` JSON fixture in
      `tests/fixtures/`.
- [ ] Add the new module to the parametrized lists in `test_ai_accounts.py`,
      `test_config_cli.py`, `test_help_i18n.py`, `test_autoswitch_timer.py`.
- [ ] `uv run pytest` and `uv run ruff check .` clean.

## 4. Docs (same change, not a follow-up)

- [ ] `README.md`: intro provider list, Commands table, per-tool section, Profile
      storage tree, List performance table (measure it), Platform notes row
      (credential source + required binary), Supported OS caveats if any.
- [ ] `CLAUDE.md`: mention `copilot_usage.py` next to the other `*_usage.py` modules.
- [ ] `CHANGELOG.md` via git-cliff on release (`feat(copilot): ...` commits).

## Open questions

- Does Copilot need `--json` output first so its quota can feed the statusline? Out
  of scope for the first cut; track separately.
- If `copilot_internal` is undocumented and changes, `usage` should degrade to the
  grok/vibe "no quota API" path instead of failing `list`.
