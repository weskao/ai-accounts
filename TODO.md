# TODO — add GitHub Copilot as the sixth managed provider

Goal: `copilot-accounts` with the same subcommand surface as the other five tools
(`who`, `save`, `list`, `usage`, `switch`, `remove`, `refresh`, `sync`, `autoswitch`,
`login-switch`, `config`), forwarded by `ai-accounts`.

Template: `vibe-accounts` was the last provider added. Its three commits are the
checklist for every touch-point — `bc2e56f` (module + entry point + umbrella),
`8a107a1` (README, autoswitch, config_menu, tests), `8d6ee96` (credential store the
CLI actually uses). Follow the same three-step shape.

## 0. Research first (nothing below is verified yet)

- [x] Where Copilot CLI stores its login. **Verified on macOS (2026-09-08) against a
      real `/login`:** `~/.copilot/config.json` is JSONC (two leading `//` comment
      lines, then JSON) holding `lastLoggedInUser: {host, login}` and `loggedInUsers`
      — no token in it. The token is a Keychain generic-password item, service
      `copilot-cli`, account `<host>:<login>` (e.g. `https://github.com:<login>`).
      The first best-effort guess (token inside config.json, keyring service
      `com.github.copilot`) was wrong on both counts and made `save` report "no login";
      fixed in `copilot_accounts.py`. Still open: Linux/Windows store layout.
- [ ] Env-var precedence the CLI honors (`COPILOT_GITHUB_TOKEN`, `GH_TOKEN`,
      `GITHUB_TOKEN`). `switch` must not be shadowed by an exported token; warn like the
      other tools do. Implemented best-effort (the order above, with a `switch`/`who`
      shadow warning), see `# ASSUMPTION:` comment above `_ENV_VARS` in
      `copilot_accounts.py` — exact precedence needs live verification.
- [x] Identity: `GET https://api.github.com/user` (login, email) for the default profile
      name. Decide login-vs-email when email is private. Implemented in
      `_fetch_identity`/`_derived_name` (`copilot_accounts.py`) against GitHub's
      documented, stable `/user` endpoint — login preferred, then email's local part,
      then a token digest; no `# ASSUMPTION:` comment here since this endpoint (unlike
      the quota one below) is public and documented.
- [x] Quota: `GET https://api.github.com/copilot_internal/user` — **verified live
      (2026-09-08)**: the CLI's Keychain OAuth token is accepted, and the response
      carries `copilot_plan` plus `quota_snapshots.{premium_interactions,chat,completions}`
      in the shape `copilot_usage.py` parses (`list --json` returned real premium/chat/
      completions windows with a monthly reset). Parsing still degrades to "no quota
      API" on any shape drift. Follow-up (not done here): with a working quota API,
      Copilot could get real autoswitch instead of the vibe-shaped "unsupported" stub.
- [ ] Token lifetime and refresh path. GitHub OAuth app tokens for Copilot may be
      long-lived with no refresh token — if so `refresh` becomes "verify still valid"
      like vibe, and the timer's re-login report needs a `copilot` color/line.
      Implemented best-effort: `cmd_refresh` re-checks the token against `/user` instead
      of renewing it (see the `# ASSUMPTION:` comment on `cmd_refresh` in
      `copilot_accounts.py`) — needs live verification. The re-login-report color line
      is done (`providers.py`'s `report_color` propagates automatically), but the
      *revoked-line phrasing* `refresh_report.py` parses for is NOT done — see the new
      note under "2. Umbrella and shared wiring" below.
- [ ] Windows/Linux credential store — decide whether `_utils.keychain_*` suffices or
      a `secret-tool`/Credential Manager branch is needed (agy already has one).
      Not resolved: `copilot_accounts.py` calls the plain `keychain_read`/`keychain_write`
      pair, which per `CLAUDE.md` is macOS-only (no-op elsewhere) — unlike `agy`'s
      cross-platform `go_keyring_*` functions. On Windows/Linux the keychain step
      silently no-ops and Copilot falls back to the config file or an env var only.
      Needs a decision + live verification, not just implementation.

## 1. Provider module + entry point

- [x] `src/ai_accounts/copilot_accounts.py` — copy `vibe_accounts.py` structure.
      Store under `~/.ai-accounts/copilot/accounts/` via `resolve_account_dir(
      "COPILOT_ACCOUNT_DIR", ...)`. Reuse `_utils` (keychain, run, ensure_tool,
      colors), `_present` (tables, pickers, panels), `config_menu`, `usage_format`.
- [x] `src/ai_accounts/copilot_usage.py` — `fetch_usage` only, returning
      `usage_format.UsageWindow`s (premium requests = the window that matters; monthly
      reset from `quota_reset_date`). No formatting here.
- [x] `login-switch <name>` → run the CLI's login, then save. Uses `shutil.which("copilot")`
      plus a manual install hint (`npm install -g @github/copilot`) rather than literally
      calling `ensure_tool("copilot")` — `ensure_tool` auto-installs via Homebrew, which is
      the wrong install path for an npm-distributed CLI, so this is a deliberate deviation,
      not a miss.
- [x] `pyproject.toml` `[project.scripts]` → `copilot-accounts = "ai_accounts.copilot_accounts:main"`.
- [x] Never persist an empty secret (see `0.3.0` keychain fix); mask via
      `config_schema.mask_secret`.

## 2. Umbrella and shared wiring (grep `vibe` to find every site)

- [x] `ai_accounts.py`: `_TOOLS`, module docstring, `help` text. (`_PROVIDER_STYLE` no
      longer exists as its own dict — an earlier pass replaced it with
      `providers.PROVIDERS`, a single source of truth `_TOOLS`/`_PROVIDER_COLOR` derive
      from; adding copilot's `Provider` entry there, with `RED` as its unused
      `cli_color`/`report_color`, was the only edit needed. No `icon` field exists on
      `Provider` today, so `🐙` was not added anywhere — nothing currently renders a
      per-provider icon.)
- [x] `i18n.py`: `help.copilot` zh-TW block; the umbrella help line listing providers.
- [x] `refresh_report.py`: provider color map — this is derived from `providers.PROVIDERS`
      (`PROVIDER_COLORS = {p.key: p.report_color for p in PROVIDERS}`), so adding the
      `Provider` entry alone propagated it.
- [ ] `refresh_report.py`: revoked-line phrasing the parser expects. **Not done.**
      `copilot_accounts.py`'s `cmd_refresh` logs `"Token for '<name>' is not accepted by
      GitHub — re-login required"`, which matches neither `refresh_report.py`'s
      `_INLINE_RE` (`"Refresh token revoked(/dead)? for ..."`) nor `_BULK_RE`
      (`"Revoked (re-login required): ..."`). A revoked Copilot token is caught by
      `copilot-accounts refresh`/`doctor` but will NOT surface in the scheduled
      re-login report/notification the way codex/claude/agy/grok do (vibe has the same
      gap, by design, since it has no refresh token to revoke at all — Copilot's OAuth
      token genuinely can be revoked, so this is a real gap, not an intentional parity
      with vibe). Needs a decision: extend the parser's phrasing set, or accept the
      silence.
- [x] `autoswitch.py` ~L446 restart-strategy map: add `copilot` with the truthful value.
      Uses `verdict=None` (same shape as `vibe` — absent from `PROVIDER_VERDICTS`
      entirely, falls through to `manual-restart`), matching `autoswitch`'s existing
      "unsupported for copilot: quota API unverified" message. Not `auto-restart` —
      there is no verified restart path for `copilot` today.
- [x] `autoswitch_timer.py`: include `copilot_accounts refresh --all` in the timer run.
      Not touched directly — `_run_token_refresh_everywhere` drives
      `ai_accounts._TOOLS`, which is derived from `providers.PROVIDERS`, so copilot was
      included automatically once added there (verified via
      `tests/test_autoswitch_timer.py::test_default_refresh_drives_all_four_providers_refresh_all`,
      whose name is stale — it already asserted against all of `_TOOLS`, dynamically).
- [x] `config_menu.py` / `config_schema.py`: only if a Copilot-specific key is needed
      (probably none — auto-switch settings are shared). Confirmed none needed; neither
      file was touched.

## 3. Tests (placeholder data only — see CLAUDE.md)

- [x] `tests/test_copilot_accounts.py` — mirror `test_vibe_accounts.py`: who/save/list/
      switch/remove/sync against a temp account dir and a faked credential store;
      `fetch_usage` against a canned `copilot_internal/user` JSON fixture. Present and
      covers who/save/switch/remove/sync/list/usage/refresh/autoswitch plus
      `copilot_usage.fetch_usage` parsing (inline fixture data, not a separate
      `tests/fixtures/` file, but same placeholder-data coverage).
- [x] Add the new module to the parametrized lists in `test_ai_accounts.py`,
      `test_config_cli.py`, `test_help_i18n.py`, `test_autoswitch_timer.py`. Added to
      the first three (their provider lists are hand-maintained); `test_autoswitch_timer.py`
      needed no edit — its provider-facing assertions already derive from
      `ai_accounts._TOOLS`/`autoswitch_hooks.providers()` dynamically.
- [x] `uv run pytest` and `uv run ruff check .` clean.

## 4. Docs (same change, not a follow-up)

- [ ] `README.md`: intro provider list, Commands table, per-tool section, Profile
      storage tree, List performance table (measure it), Platform notes row
      (credential source + required binary), Supported OS caveats if any.
      Done: intro provider list, Commands table row, Profile storage tree,
      Platform notes row (credential source + `copilot` binary, pulled from
      `copilot_accounts.py`'s actual read order, not this file's speculative
      candidates), plus the `list`/`usage --json` and Doctor/List-performance prose
      updated to mention Copilot's degrade-to-no-quota-API behavior.
      Not done, on purpose: no dedicated "per-tool section" — grep confirms neither
      Grok nor Vibe has one either (README documents all providers together via the
      shared tables/prose above, not per-tool headers), so none was invented for
      Copilot. Also skipped the List performance table row: with zero saved Copilot
      profiles on this machine there is nothing meaningful to time (division by zero
      profiles), unlike Grok/Vibe's rows which come from real saved profiles. No
      Supported-OS caveat was needed (Copilot doesn't have an agy-style platform gap
      yet — its Windows/Linux keychain gap is tracked under section 0 above instead,
      since it's a correctness question, not a documented product caveat).
- [x] `CLAUDE.md`: mention `copilot_usage.py` next to the other `*_usage.py` modules.
- [ ] `CHANGELOG.md` via git-cliff on release (`feat(copilot): ...` commits). Deferred
      to release time, as originally scoped — not part of this change.

## Open questions

- ~~Does Copilot need `--json` output first so its quota can feed the statusline? Out
  of scope for the first cut; track separately.~~ Stale: `--json` shipped in this same
  change (`copilot-accounts list --json` / `usage --json`, plus `ai-accounts list/usage
  --json` merging it in), so this is no longer an open question.
- If `copilot_internal` is undocumented and changes, `usage` should degrade to the
  grok/vibe "no quota API" path instead of failing `list`. Resolved: implemented —
  `copilot_usage.fetch_usage`/`copilot_accounts._has_quota` degrade to
  `no_quota_api: true` on any parse/HTTP failure rather than raising or failing
  `list`/`usage`.

## Future work

- [x] 🔴 **High priority:** Add configurable AI quota-reset notifications: detect when a supported
      provider's quota window rolls over and usage returns to 0%, then notify the
      user that the quota is available again. Support provider-specific quota
      windows, such as 5H / 1W for Codex and Claude, and Monthly AI Credits for
      GitHub Copilot. Prevent false notifications from initial 0% readings or
      repeated timer checks, and integrate the feature with `ai-accounts config`,
      applicable `*-accounts config` commands, `config get/set`, notification
      toggles, persistent state, provider capability detection, and English /
      zh-TW localization.
      Phase 1 landed: see [`docs/spec.md`](docs/spec.md) for the locked design
      decisions and [`src/ai_accounts/quota_reset.py`](src/ai_accounts/quota_reset.py)
      for the detection engine.
