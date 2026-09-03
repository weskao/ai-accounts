# ai-accounts

Manage, inspect, refresh, and switch saved profiles across AI coding CLIs from
one dependency-free Python package.

`ai-accounts` is the all-provider command. The package also installs focused
commands for Codex, Claude Code, Antigravity, Grok Build, and Mistral Vibe.

## Requirements

- Python 3.10 or newer
- [`uv`](https://docs.astral.sh/uv/)
- The provider CLI for each account manager you use

The Python package has no runtime dependencies.

## Supported operating systems

`ai-accounts` is tested on and supports macOS, Windows, and Linux. Profile
management, the umbrella command, and auto-switch timers work on all three.

| Operating system | Native integration | Caveat |
| --- | --- | --- |
| macOS | Keychain and `launchd` | None |
| Windows | Credential Manager and Task Scheduler | `agy-accounts usage` is unavailable because Antigravity usage inspection requires POSIX pseudo-terminals. |
| Linux | Secret Service (`secret-tool`) and systemd user timers (or cron fallback) | Install `libsecret-tools` before using `agy-accounts`. |

Credential-store integration follows the native platform. The provider CLI for
each account manager must also support your operating system.

## Install

Install the first standalone release:

```sh
uv tool install --from git+https://github.com/weskao/ai-accounts.git@v0.1.0 ai-accounts
```

Or install the latest `main`:

```sh
uv tool install --from git+https://github.com/weskao/ai-accounts.git ai-accounts
```

Upgrade or uninstall:

```sh
uv tool upgrade ai-accounts
uv tool uninstall ai-accounts
```

## Commands

| Command | Purpose |
| --- | --- |
| `ai-accounts` | Run one operation across every provider |
| `codex-accounts` | Manage Codex CLI profiles and ChatGPT OAuth usage |
| `claude-accounts` | Manage Claude Code profiles and quota usage |
| `agy-accounts` | Manage Antigravity profiles and quota usage |
| `grok-accounts` | Manage Grok Build OAuth profiles |
| `vibe-accounts` | Manage Mistral Vibe API-key profiles |

Use the umbrella command to run the same action for all providers:

```sh
ai-accounts list
ai-accounts who
ai-accounts usage
ai-accounts refresh --all
ai-accounts sync
ai-accounts --help
```

`list` fetches providers concurrently. Interactive actions run providers one at
a time so their prompts remain usable.

Every provider command supports the common profile workflow:

```sh
codex-accounts who
codex-accounts save work
codex-accounts list
codex-accounts switch work
codex-accounts refresh --all
codex-accounts remove work
codex-accounts --help
```

`list` gives a compact, provider-by-provider view of every saved profile and
marks the active one.

![Saved profiles from every provider](ai-accounts-list%20demo.png)

## Profile storage

Saved profiles and shared settings live under `~/.ai-accounts`:

```text
~/.ai-accounts/
├── config.json
├── autoswitch-state.json
├── codex/accounts/
├── claude/accounts/
├── antigravity/accounts/
├── antigravity/usage-cache.json
├── grok/accounts/
└── vibe/accounts/
```

`antigravity/usage-cache.json` holds the last quota reading seen for each agy
profile — percentages and reset times, no credentials (see below for why it is
kept). Profile JSON files do contain live credentials: do not commit, publish,
or share this directory. Writes use owner-only permissions and atomic replacement where
the provider format allows it.

Provider-native legacy stores such as `~/.codex/accounts` and
`~/.claude/accounts` are moved into the central directory on first use. Override
paths with `CODEX_ACCOUNT_DIR`, `CLAUDE_ACCOUNT_DIR`,
`ANTIGRAVITY_ACCOUNT_DIR`, `GROK_ACCOUNT_DIR`, or `VIBE_ACCOUNT_DIR`. Override
the shared config with `AI_ACCOUNTS_CONFIG_JSON`.

## Auto-switch

Auto-switch can refresh quota data, select another saved profile when the active
profile crosses a configured threshold, notify you, and restart supported
interactive sessions.

```sh
ai-accounts config
ai-accounts config get
ai-accounts config set enabled true
ai-accounts config set layout narrow
ai-accounts autoswitch
ai-accounts autoswitch setup
ai-accounts timer-status
```

`ai-accounts config` opens the interactive menu for configuring auto-switch
behavior and notifications.

![Interactive ai-accounts configuration](ai-accounts%20config%20demo.gif)

`ai-accounts autoswitch setup` installs provider event hooks plus a low-frequency
OS timer fallback. Re-run it after reinstalling the package so hooks point at
the current Python environment.

Remove only the timer with:

```sh
ai-accounts uninstall-timer
```

The target is the saved profile with the lowest usage still under the threshold,
ties broken by profile name. When activating it fails — a deleted profile, a
credential the CLI refuses — the next candidate in that order is tried, so one
dead profile cannot strand you on an exhausted account.

Antigravity is the exception: `agy` reports quota only for the session that is
*live*, so reading a candidate's quota means writing its credential into the
single slot a running `agy` reads. That is safe only while nothing else holds
that slot, which gives `agy-accounts autoswitch` two modes:

- **Nothing running** (no Antigravity IDE, no `agy` process) — every candidate
  is measured for real, the slot restored after each reading, and the switch
  goes to the account with the lowest verified usage. No opt-in needed: nothing
  here is a guess.
- **Something running** — nothing is swapped. Candidates are ranked by the last
  reading taken for each of them, which is what `antigravity/usage-cache.json`
  is for; a reading holds until its window's reset time passes. A candidate
  nothing at all is known about still needs the `agy_blind_switch` opt-in,
  which switches without any usage data for the target.

Note the Stop hook always takes the second path: the agy session whose exit
fired the hook is still up, and is itself a reader. Measuring is the background
timer's job.

Either way it passes over a profile that could not take over: a malformed or
foreign credential blob, a token that can no longer be refreshed, or a second
profile of the same Google account as the exhausted one.

The interactive config menu documents each setting. CLI config reads mask the
Telegram bot token.

### Scheduled token refresh and the re-login report

`token_refresh` is an independent switch from `enabled`: each timer tick runs
`refresh --all` on every provider even when auto-switching is off. A routine
rotation is silent. When a refresh token can only be fixed by a fresh login,
the tick prints a report and sends the same content over the configured
`notify` channel — grouped by provider, one row per profile with the reason it
failed and the command that fixes it:

```
┌─ 🔑 ai-accounts: 3 profiles need re-login — codex, agy ─┐
│  🔐 codex · 2 profiles
│    • work — HTTP 400 from token endpoint
│      ↳ codex-accounts login-switch work
│    • spare — refresh token missing or rejected
│      ↳ codex-accounts login-switch spare
│  🔐 agy · 1 profile
│    • personal — revoked: refresh token rejected (invalid_grant)
│      ↳ agy-accounts login-switch personal
└─────────────────────────────────────────────────────────┘
```

Each provider gets its own color, on the heading and on every profile name
under it. The alert is de-duplicated by the exact set of profiles it names:
the same set stays quiet for an hour, while a newly revoked profile alerts on
the next tick instead of waiting out the previous alert's cooldown. Transient
failures (a 5xx, a timeout, an unreachable token endpoint) are retried on the
next tick and never reported here.

## Output layout

Every table and panel — `list`, `who`, the save/switch/refresh success
panels, the re-login report, and the interactive `ai-accounts config` menu
itself — adapts to how wide the terminal is. The `layout` setting controls
it:

```sh
ai-accounts config set layout auto     # default: follow terminal width
ai-accounts config set layout wide     # always the desktop table
ai-accounts config set layout narrow   # always the stacked, phone-width layout
```

`auto` renders the `wide` box-drawing table at 60 columns or wider, and
switches to stacked `narrow` cards — one per profile, no column dropped —
below that, which is the layout a phone-width SSH session or a narrow split
pane needs. `COLUMNS` overrides the detected width, so either mode can be
previewed from a full-size terminal:

```sh
COLUMNS=200 codex-accounts list
```

```text
Saved Codex profiles  (2)
┌──────────┬───────────────────┬──────┬────────────────┬──────────────┬─────────────────┬─────────┬───────┬────────┐
│ PROFILE  │ ACCOUNT           │ PLAN │ ID             │ 5H USED      │ 1W USED         │ UPDATED │ AUTH  │ STATE  │
├──────────┼───────────────────┼──────┼────────────────┼──────────────┼─────────────────┼─────────┼───────┼────────┤
│ work     │ user@example.com  │ Plus │ acct_ab12cd345 │ 42% · 3h 12m │ 68% · 2d 4h 15m │ 14:32   │ 18:00 │ ACTIVE │
│ personal │ user2@example.com │ Free │ acct_9f8…5b4a  │ 10% · 4h 50m │ 15% · 6d 2h 45m │ 09:02   │ 20:15 │ —      │
└──────────┴───────────────────┴──────┴────────────────┴──────────────┴─────────────────┴─────────┴───────┴────────┘
```

```sh
COLUMNS=40 codex-accounts list
```

```text
Saved Codex profiles  (2)
work                              ACTIVE
  ACCOUNT  user@example.com
  PLAN     Plus
  ID       acct_ab12cd345
  5H USED  42% · 3h 12m
  1W USED  68% · 2d 4h 15m
  UPDATED  14:32
  AUTH     18:00
────────────────────────────────────────
personal                               —
  ACCOUNT  user2@example.com
  PLAN     Free
  ID       acct_9f8…5b4a
  5H USED  10% · 4h 50m
  1W USED  15% · 6d 2h 45m
  UPDATED  09:02
  AUTH     20:15
```

## Platform notes

| Provider | Credential source | Notes |
| --- | --- | --- |
| Codex | `~/.codex/auth.json` and the native credential store | `codex` is required for login flows |
| Claude Code | `~/.claude/.credentials.json` and macOS Keychain when used | `claude` is required for login flows |
| Antigravity | macOS Keychain, Windows Credential Manager, or Linux Secret Service | Linux needs `secret-tool` from libsecret |
| Grok Build | `$GROK_HOME/auth.json` | Quota switching is skipped when no quota API is available |
| Mistral Vibe | macOS Keychain or `$VIBE_HOME/.env` | On Windows and Linux, `$VIBE_HOME/.env` is used; `vibe` is required for login flows |

Run a provider command with `--help` for its exact files, environment overrides,
and authentication behavior.

## Development

```sh
git clone https://github.com/weskao/ai-accounts.git
cd ai-accounts
uv sync --locked
uv run pytest
uv run ruff check .
uv build
```

Install the checkout globally while developing:

```sh
uv tool install --editable .
```

## License

[MIT](LICENSE)
