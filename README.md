# ai-accounts

Manage, inspect, refresh, and switch saved profiles across AI coding CLIs from
one dependency-free Python package.

`ai-accounts` is the all-provider command. The package also installs focused
commands for Codex, Claude Code, Antigravity, Grok Build, Mistral Vibe, and
GitHub Copilot.

[Install](#install) · [Commands](#commands) · [Auto-switch](#auto-switch) ·
[Platform notes](#platform-notes)

## See it in action

### View saved profiles across providers

`ai-accounts list` brings saved profiles into one provider-by-provider view,
marks the active profile, and shows usage where the provider supports it.

![ai-accounts list showing saved profiles grouped by provider and active profile markers](ai-accounts-list%20demo.png)

### Configure switching and notifications

`ai-accounts config` opens an interactive settings menu. Adjust auto-switch
behavior, notifications, and display options; changes save as you make them.
See [Auto-switch](#auto-switch) for setup and activation commands.

![Interactive ai-accounts config menu for auto-switch, notifications, and display settings](ai-accounts%20config%20demo.gif)

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

Install the latest release:

```sh
uv tool install --from git+https://github.com/weskao/ai-accounts.git@vX.Y.Z ai-accounts
```

Replace `X.Y.Z` with the release version you want to install.

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
| `copilot-accounts` | Manage GitHub Copilot CLI profiles, identity and monthly credit balance |

Use the umbrella command to run the same action for all providers:

```sh
ai-accounts list                              # List saved profiles for every provider
ai-accounts who                               # Show the active account for every provider
ai-accounts usage                             # Show usage for every active account
ai-accounts refresh --all                     # Refresh every saved profile's tokens
ai-accounts sync                              # Sync active auth back to matching profiles
ai-accounts login-switch <profile_name>       # Log in again and save each login under this name
ai-accounts doctor                            # Run offline health checks for every provider
ai-accounts help                              # Show available commands
```

Replace `<profile_name>` with a name you choose for the saved profile.

`list` fetches providers concurrently. Interactive actions run providers one at
a time so their prompts remain usable.

`list` and `usage` also accept `--json`. In that mode every provider runs
concurrently with its output captured (not printed live), and each
provider's own `--json` document is merged into one object printed once,
keyed by provider (`codex-accounts`, `claude-accounts`, `agy-accounts`,
`grok-accounts`, `vibe-accounts`, `copilot-accounts`). A provider that exits
non-zero or prints unparseable output gets `{"error": "..."}` in its place
instead of crashing the merge:

```sh
ai-accounts list --json | python3 -m json.tool
ai-accounts usage --json
```

Every provider command supports the common profile workflow:

```sh
codex-accounts who                            # Show the current Codex account
codex-accounts save <profile_name>            # Save the current login as a reusable profile
codex-accounts list                           # List saved profiles and their usage
codex-accounts list --json                    # Print saved profiles and usage as JSON
codex-accounts switch <profile_name>          # Switch to a saved profile
codex-accounts refresh --all                  # Refresh tokens for every saved profile
codex-accounts login-switch <profile_name>    # Log in again and save it as a profile
codex-accounts remove <profile_name>          # Delete a saved profile
codex-accounts help                           # Show available commands
```

Every per-provider tool's `list`/`usage` also accepts `--json`, printing one
JSON array of `{"name", "active", "usage", "no_quota_api"}` objects instead of
the table (`usage` is `null` and `no_quota_api` is `true` for Grok and Vibe,
which have no quota API). Copilot attempts a real quota lookup and only
degrades to the same `no_quota_api: true` shape when that lookup fails — see
Platform notes below.

`who` also answers to `current`. `login-switch <name>` runs a fresh provider
login and saves the result as `<name>` — it is what the re-login report below
tells you to run.

### Copilot account details and balance

`copilot-accounts list` shows `PROFILE`, `ACCOUNT`, `PLAN`, `ID`, `MONTH USED`,
`REMAINING`, `UPDATED`, `AUTH`, and `STATE`. `copilot-accounts usage` shows the
same details for the active saved profile. Account details are fetched from
GitHub on each listing, with saved details as a fallback, without rewriting
profiles. Listings identify the active profile from Copilot's `config.json`
without reading the OS credential store on macOS, Windows, or Linux. `ACCOUNT`
displays `Name <email>` when available, otherwise the name,
GitHub login, or email. A private primary email is included only when GitHub
allows the token to read it and reports it as verified.

For AI-credit billing, the monthly balance comes from GitHub's `chat` quota,
matching the plan allowance used by Copilot `/usage`. The obsolete premium
quota marked `has_quota: false` is unavailable, not 100% used. Legacy billing
still shows premium requests. For example, `2% · 22d 4h 35m · 12/500 AIC`
shows percent used, time until reset, and used/total credits; `487.5 AIC` in
`REMAINING` preserves GitHub's fractional balance. GitHub's percentage and
credit counters can have different rounding. Unlimited quotas say `unlimited`.
Copilot does not supply 5-hour/weekly windows or token expiry through these
lookups, so those columns are omitted. `UPDATED` is the successful fetch time;
`AUTH` is `valid`, `rejected` (HTTP 401), `missing`, or `unknown`.

`--json` adds `account`, `login`, `email`, `id`, and `auth` to each profile.
The `usage` object includes `monthly`, `unit` (`AIC` or `requests`), `used`,
`entitlement`, `remaining`, and `unlimited`, alongside the existing
`premium`, `chat`, `completions`, `plan`, `refreshed_at`, and `error` fields.
Unavailable values are null; existing no-quota behavior is retained.

### Doctor

`ai-accounts doctor` runs one offline health check per provider — no live
quota/HTTP calls — and never fails the process just because a provider isn't
set up; it reports the gap instead:

- the provider's CLI binary (`codex`, `claude`, `agy`, `grok`, `vibe`, `copilot`) is on `PATH`
- the provider's own account-tool console script (`codex-accounts`,
  `claude-accounts`, `agy-accounts`, `grok-accounts`, `vibe-accounts`,
  `copilot-accounts`) is on `PATH` — distinct from the binary check above: a
  newly declared `[project.scripts]` entry point stays invisible until the
  next `uv tool install`/`uv sync`, so a stale install can have the vendor
  CLI present while its own account tool is a bare shell "command not found"
  with no other clue. The table cell for a failing row just names the missing
  tool, matching every other cell's length; the fix — a `uv tool install
  --editable <repo> --force` you can run as-is, plus, best-effort, a note
  when the currently installed tool is an editable install pointing at a
  *different* checkout than the one `doctor` is running from — is printed
  once below the table, not repeated per provider
- the OS credential store is reachable (macOS/Windows Keychain/Credential
  Manager, or Linux `secret-tool`)
- every saved profile's JSON is well-formed and, if it carries a recognizable
  expiry field, not already expired with nothing left to refresh it (a stale
  *access* token backed by a live refresh token is normal, not a failure)
- the auto-switch timer's install state

```sh
ai-accounts doctor
```

This is the real output — one wide table plus a short footer — from a fresh
install where none of the vendor CLIs or account tools are on `PATH` yet, and
no profiles have been saved for any provider (every existing user's first run
after upgrading to this version). Captured by pointing each provider's
`*_ACCOUNT_DIR` override (see "Profile storage" below) at an empty directory,
so the table reflects a true empty store rather than hand-edited numbers:

```sh
CODEX_ACCOUNT_DIR=<empty-dir> CLAUDE_ACCOUNT_DIR=<empty-dir> \
ANTIGRAVITY_ACCOUNT_DIR=<empty-dir> GROK_ACCOUNT_DIR=<empty-dir> \
VIBE_ACCOUNT_DIR=<empty-dir> COPILOT_ACCOUNT_DIR=<empty-dir> \
PATH=/usr/bin:/bin ai-accounts doctor
```

```
┌──────────────────┬──────────────────────────────────┬───────────────────────────────────────────┬──────────────────┬────────────────────────┬───────────────────┐
│ Provider         │ Binary                           │ Account tool                              │ Credential store │ Profiles               │ STATE             │
├──────────────────┼──────────────────────────────────┼───────────────────────────────────────────┼──────────────────┼────────────────────────┼───────────────────┤
│ codex-accounts   │ FAIL `codex` not found on PATH   │ FAIL `codex-accounts` not found on PATH   │ PASS reachable   │ PASS no saved profiles │ FAIL issues found │
│ claude-accounts  │ FAIL `claude` not found on PATH  │ FAIL `claude-accounts` not found on PATH  │ PASS reachable   │ PASS no saved profiles │ FAIL issues found │
│ agy-accounts     │ FAIL `agy` not found on PATH     │ FAIL `agy-accounts` not found on PATH     │ PASS reachable   │ PASS no saved profiles │ FAIL issues found │
│ grok-accounts    │ FAIL `grok` not found on PATH    │ FAIL `grok-accounts` not found on PATH    │ PASS reachable   │ PASS no saved profiles │ FAIL issues found │
│ vibe-accounts    │ FAIL `vibe` not found on PATH    │ FAIL `vibe-accounts` not found on PATH    │ PASS reachable   │ PASS no saved profiles │ FAIL issues found │
│ copilot-accounts │ FAIL `copilot` not found on PATH │ FAIL `copilot-accounts` not found on PATH │ PASS reachable   │ PASS no saved profiles │ FAIL issues found │
└──────────────────┴──────────────────────────────────┴───────────────────────────────────────────┴──────────────────┴────────────────────────┴───────────────────┘

Autoswitch timer: PASS installed
Account tool not on PATH for: codex-accounts, claude-accounts, agy-accounts, grok-accounts, vibe-accounts, copilot-accounts
This can mean the tool was never installed, a newly declared entry point needs a reinstall, or its install directory isn't on PATH.
Run uv tool install --editable <repo> --force
```

Once a provider's CLI and account tool are actually on `PATH`, its "Binary"
and "Account tool" cells read `PASS` instead — an "Account tool" cell never
carries the fix, only the fact; the fix is the "Run ..." line below the
table, printed once for however many providers are missing theirs, not
repeated per row. (`STATE`, not "Status" — matches the header every other
`accounts_table` caller in this repo uses.) When `doctor` can also tell that
the currently installed tool is an editable install pointing at a
*different* checkout than the one it's running from, it appends one more
line noting that under the "Run ..." line.

`--json` prints one JSON document (never one per provider — same merge shape
as `list --json`/`usage --json` above), keyed by provider label. A failing
`account_tool` check carries a `remediation` field with the same install
command shown in the footer above (kept in full — only the table cell got
shorter), and the top-level `account_tool_note` key mirrors the optional
"different checkout" note (`null` when it doesn't apply):

```sh
ai-accounts doctor --json | python3 -m json.tool
```

### List performance

`ai-accounts list` runs providers concurrently and displays each provider's
table as it finishes. Codex and Claude also fetch usage concurrently across
profiles; Grok and Vibe list local profile data without quota network requests.
Copilot fetches quota and GitHub identity per profile (plus the primary email
when permitted), with profiles queried concurrently. Failed quota requests
retain the same no-quota result shape as Grok/Vibe.

Antigravity queries different credentials **sequentially**: each query switches
the shared OS keyring session, launches `agy`, waits for authentication and
quota data, then closes it. The original session is restored after listing.
Within each launch, quota and account-status RPCs run concurrently. Profiles
with identical credential file contents reuse a successful result within that
invocation; different credentials and failed lookups are not reused.

Measured on **2026-09-07 (Asia/Taipei)**, on a local macOS machine:

| Provider command | Saved profiles | Total list time | Time per profile (total ÷ count) | Work performed |
| --- | ---: | ---: | ---: | --- |
| `codex-accounts list` | 5 | 0.944 s | 0.189 s | Concurrent usage requests |
| `claude-accounts list` | 1 | 0.739 s | 0.739 s | Concurrent usage requests when multiple profiles exist |
| `agy-accounts list` | 6 | 24.484 s | 4.081 s | Sequential credential sessions; concurrent RPCs within each session |
| `grok-accounts list` | 7 | 0.003 s | <0.001 s | Local profile reads |
| `vibe-accounts list` | 1 | 0.028 s | 0.028 s | Local profile and credential-store reads |

These are single-run measurements of each provider's `cmd_list()`, with the
five providers running concurrently in separate processes and output captured.
They include list rendering and, for Antigravity, `agy` startup and cleanup,
but exclude Python interpreter startup and module imports. Counts refer to
saved profiles, which are not necessarily distinct accounts.

**Time per profile is an amortized value, not individual request latency.**
In particular, dividing a concurrent provider's total time by its profile count
does not measure how long one account would take on its own. Authentication,
network conditions, and machine load affect these results. Parallel RPCs do
not remove Antigravity's per-account startup cost, so it can still dominate
the total time of `ai-accounts list`.

To query only the selected Antigravity account, use:

```sh
agy-accounts usage
```

### Fast Antigravity lists

Antigravity can only report quota for the live credential session, so a live
`agy-accounts list` must check profiles one at a time. Cached-list mode
(`agy_list_cached_usage`, **on by default**) queries the current account live
and uses saved readings for other accounts instead:

```sh
agy-accounts list --refresh  # populate or update the saved readings
agy-accounts list            # current account live; other accounts from cache
```

Cached readings can be stale. The list labels this mode and repeats the refresh
command; turn the setting off when every listing must fetch live quota:

```sh
ai-accounts config set agy_list_cached_usage false
```

The current account is always queried live, so listing still waits for that
one query. If it fails, UPDATED shows the error instead of stale quota.

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
├── vibe/accounts/
└── copilot/accounts/
```

`antigravity/usage-cache.json` holds the last quota reading seen for each agy
profile — quota windows, plan and timestamp, but no credentials (see below for
why it is kept). Profile JSON files do contain live credentials: do not commit,
publish, or share this directory. Writes use owner-only permissions and atomic
replacement where the provider format allows it. Windows has no POSIX
permission bits, so there the protection comes from the parent directory's
inherited ACL: the default location under your user profile is already
owner-only, but an override pointing outside it inherits whatever that
directory allows.

Provider-native legacy stores such as `~/.codex/accounts` and
`~/.claude/accounts` are moved into the central directory on first use. Override
paths with `CODEX_ACCOUNT_DIR`, `CLAUDE_ACCOUNT_DIR`,
`ANTIGRAVITY_ACCOUNT_DIR`, `GROK_ACCOUNT_DIR`, `VIBE_ACCOUNT_DIR`, or
`COPILOT_ACCOUNT_DIR`. Override the shared config with
`AI_ACCOUNTS_CONFIG_JSON`.

## Auto-switch

Auto-switch can refresh quota data, select another saved profile when the active
profile crosses a configured threshold, notify you, and restart supported
interactive sessions. A desktop or Telegram switch notification ends with the
source device, same as the re-login report below (for example, `💻 MacBook Pro`
or `🖥️ Mac mini`).

```sh
ai-accounts config
ai-accounts config get
ai-accounts config set enabled true
ai-accounts config set layout narrow
ai-accounts config export settings.json
ai-accounts config import settings.json
ai-accounts autoswitch
ai-accounts autoswitch setup
ai-accounts install-timer --interval 1800
ai-accounts timer-status
```

`ai-accounts config` opens the interactive menu for configuring auto-switch
behavior and notifications. Arrow keys select and change values, `r` resets
every setting to its default after a `y` confirmation, and each change is
saved as you make it — there is no separate save step. When stdin is not a
TTY the menu falls back to a numbered prompt. On/off settings read as **On**
/ **Off** there (green / dim, translated with the menu); `config get` and
`config set` keep the scriptable `true` / `false` spelling.

`config export [file]` writes the current settings as JSON — to the given
file (owner-only, mode `600`), or to standard output when no file is given so
`config export | …` pipes cleanly. **Secrets are never exported**:
`telegram_bot_token` is left out of the file entirely, and the command says so
every time, because a restored backup with no token is why notifications would
otherwise go quiet. Export is a full backup and is not filtered per CLI — the
Antigravity keys are included even when it runs from `codex-accounts`.

`config import <file>` applies those settings back, all or nothing, and reports
what it did:

- A **secret** in the file is skipped, never written — a hand-written or
  mask-shaped (`********WXYZ`) value would destroy the real token stored on
  this machine.
- An **unknown key** (from a newer ai-accounts) is left alone rather than
  written back unvalidated.
- One **invalid value** aborts the whole import before the first write, so a
  half-applied config is never the outcome. The exit code is 1 and the config
  on disk is untouched.

Both directions work from every CLI (`codex-accounts config export`, …) and
print their notice on stderr.

The Antigravity blind-switch and inactive-account cache options appear only in
`ai-accounts config` and `agy-accounts config` (including their `config get`
listings). Other provider menus omit them, and resetting those menus preserves
the hidden settings. Explicit `config get <key>` and `config set <key> <value>`
remain shared across all CLIs.

`ai-accounts autoswitch setup` installs provider event hooks plus a low-frequency
OS timer fallback. Re-run it after reinstalling the package so hooks point at
the current Python environment.

Status checks are read-only: `enabled` reads the shared config,
`autoswitch_setup.is_installed()` checks both timer registration and relevant
hooks, and `ai-accounts timer-status` checks the platform's timer registration.
macOS checks the LaunchAgent file; Linux checks the systemd timer file or reads
crontab; Windows queries Task Scheduler with `schtasks /Query`. An `installed`
result confirms registration, not successful execution or quota availability.
Windows hook commands quote interpreter paths containing spaces; re-run setup
to update hooks installed by an older version.

When CI fails after a push, GitHub Actions sends a formatted Telegram alert
with the repository, branch, short commit ID, and a clickable workflow URL.
Repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be
configured before the alert can be delivered.

`ai-accounts install-timer` registers only the OS timer, without the provider
hooks, and takes `--interval <seconds>` (default 1800). The first tick runs at
login/boot, not one interval later — `RunAtLoad` on macOS, `OnBootSec=60` on
systemd, an `@reboot` line on cron — so a quota window that reset while the
machine was off is reported as soon as you are back, provided `reset_notify`
is on. (Windows' scheduled task has no logon trigger; its first tick lands
within one interval of boot instead.) Remove it with:

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
next tick and never reported here. The sent notification ends with the source
device (for example, `💻 MacBook Pro` or `🖥️ Mac mini`).

### Quota-reset notifications

`reset_notify` (default `true`) sends a notification when a provider's quota
window resets and full usage is available again. `reset_notify_min_used_pct`
(default `90`) gates it: only a window that was at least that heavily used
just before it reset triggers a notification, so a window that reset while
barely touched stays quiet. Both are `ai-accounts config` keys (group
"Notifications"), also available from `codex-accounts config`,
`claude-accounts config`, and `copilot-accounts config`.

A reset is recognised two ways, so one that happens **off schedule** still
notifies. Normally the window's recorded reset time arrives and the provider
reports a later one. But a provider can also hand out quota early — a holiday
top-up, a goodwill credit — and then that deadline never arrives, so only the
fall in usage reveals it. A fall counts as a reset when the reset time
**jumped** with it — moved further into the future than the time that passed
since the previous scan. A freshly issued window lands hours further out; a
window merely ageing out has a reset time that advances only as fast as the
clock, which is the signature that keeps gradual decay from reading as a
reset. Without such a jump, a fall of at least 50 points still counts if the
reset time **stayed put**, any fall beyond a 10-point noise floor counts — a
fixed window cannot fall at all without having been cleared. Providers differ
here and both shapes are handled: codex zeroes its 5h and weekly windows and
restarts the weekly count from the reset moment (its deadline jumps), while
claude zeroes both but leaves the weekly deadline where it was. A reset time
that moved but did not jump is the sliding-decay shape, and needs a fall of at
least 50 points. All routes share the `reset_notify_min_used_pct` gate.

An **account plan change is not a reset**, and is excluded before either
route runs. Moving from a 1x to a 5x account (or 5x to 20x) leaves the same
absolute usage against a larger allowance, so the reported percentage drops
sharply while the window and its deadline carry on untouched — the exact shape
of a cleared counter. `codex-accounts list --json` and `claude-accounts list
--json` therefore also report each profile's `plan` (claude's includes the
rate multiplier, e.g. `Max · 5x`, so a 5x-to-20x move on one plan is visible),
and a window whose plan changed since the previous tick is re-baselined
silently. A downgrade raises the percentage instead, which never looked like a
reset. Real resets on the new plan notify as usual from the next tick on; agy
reports no plan, so nothing changes there.

Because detection is a periodic scan, **neither route needs the window to
still read 0% when the tick lands** — you may well have started using the
fresh quota already. The scheduled route ignores the fresh percentage
entirely, and the off-schedule route asks what the window's end did, not how
low the reading landed, so an early reset caught at 5%, 15%, 40% or 60% used
still notifies. The one gap left: with the deadline unmoved, a reset is missed
if the fresh window was re-consumed to within 10 points of the old reading
inside a single tick (95% down to 86%, say) — at that point the fall is
indistinguishable from reporting jitter.

Detection runs across every saved profile for a covered provider, not just
the currently active one, so an account benched by auto-switch still gets
its "usable again" notification. Each timer tick sends at most one grouped
notification over the configured `notify` channel, covering every window
that reset since the previous tick.

`docs/quota-reset-cases.md` is the full case table — every scenario the
detection rule was verified against, with the number of notifications each
one produces, including the ones that deliberately produce none.

Provider coverage:

- `codex` / `claude` — hourly and weekly windows
- `copilot` — the monthly window
- `agy` — all four windows (Gemini 5h/weekly, Claude/GPT 5h/weekly), but read
  from its local usage cache rather than probed live: `agy-accounts list`
  activates each profile through the shared credential slot to query it, which
  is not something a background timer should do. So agy readings are only as
  fresh as the last real `agy-accounts list`, `list --refresh`, or auto-switch
  probe. A profile that has never been probed is silently skipped, and a stale
  cache means a *late* notification, never a wrong one. Because the next reset
  time is then computed rather than reported by the provider, agy
  notifications say the reading came from the cache instead of quoting a next
  reset time.
- `grok` / `vibe` — not supported: neither has a quota API to watch.

Detection only happens on a timer tick, so a notification lands up to one
tick interval (`install-timer`'s `--interval`, default 1800 seconds) after
the actual reset — it is not instantaneous or event-driven.

**`ai-accounts install-timer` must already be installed, or this feature is
completely inert:** with no timer running, `reset_notify` makes no extra
`list --json` calls and sends no notifications. Turning `reset_notify` on
does not yet prompt you to install the timer — that tie-in is deferred to a
later phase.

Because `reset_notify` defaults to on, anyone who already has the timer
installed starts getting these notifications the next time a covered window
resets, with no action taken on their part — this is a behavior change on
upgrade, not just a new opt-in setting. Volume is bounded: at most one
notification per window per reset, so a single provider/profile sitting
above the threshold sees at most ~5 notifications a day from its hourly
window plus at most one more from its weekly/monthly window in the same
period.

## Language

Notifications, the interactive config menu, `agy-accounts list`'s
usage-freshness footer notes, and every tool's `help`/`-h`/`--help` output are
localized. English (`en`) and Traditional Chinese (`zh-TW`) are available; the
default follows the OS locale and falls back to English for a locale with no
translation:

```sh
ai-accounts config set language zh-TW
ai-accounts config set language en
```

The setting only affects display text. Config keys, values, and command names
stay in English so scripts keep working across languages.

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

In `wide`, the `config` menu grows its box to fit the longest label, value and
help text, but never past the terminal — long help lines wrap inside the box
instead of pushing it off-screen.

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

(the box-drawing corners above are the `classic` `table_style`; see below — the
default `modern` style rounds them instead.)

## Table style

Every table and panel is also painted by the `table_style` setting, independent
of `layout` above — `layout` decides table vs. stacked cards, `table_style`
decides how either one is drawn:

```sh
ai-accounts config set table_style modern   # default: rounded, dimmed frame,
                                             # banded header, zebra-striped rows
ai-accounts config set table_style classic  # the plain full-brightness grid —
                                             # also the safer pick on a light
                                             # terminal, where a 256-color band
                                             # can wash out
```

```text
Saved Codex profiles  (2)
╭──────────┬───────────────────┬──────┬────────┐
│ PROFILE  │ ACCOUNT           │ PLAN │ STATE  │
├──────────┼───────────────────┼──────┼────────┤
│ work     │ user@example.com  │ Plus │ ACTIVE │
│ personal │ user2@example.com │ Free │ —      │
╰──────────┴───────────────────┴──────┴────────╯
```

In the interactive menu (`ai-accounts config`) the **Table style** row previews
itself: while it is selected, a three-row sample table is drawn in the empty
gutter to the right of the settings list, in whatever style is currently cycled
onto the row — and the menu's own frame re-rounds with it — so the choice is
made by looking at it rather than by saving, quitting and running a listing.
The demo fills space the box already had, so nothing moves as you arrow onto
that row; at phone width, where there is no gutter, it stacks under the rows
instead.

`table_style` also stripes every `--help` output's `USAGE`/`EXAMPLES` command
list under `modern` — every other command entry (its wrapped description
lines included) gets the same zebra-stripe background as an odd table row, so
a long command list stays easy to scan line by line; `classic` leaves it
plain, same as it leaves tables unbanded.

## Platform notes

| Provider | Credential source | Notes |
| --- | --- | --- |
| Codex | `~/.codex/auth.json` and the native credential store | `codex` is required for login flows |
| Claude Code | `~/.claude/.credentials.json` and macOS Keychain when used | `claude` is required for login flows |
| Antigravity | macOS Keychain, Windows Credential Manager, or Linux Secret Service | Linux needs `secret-tool` from libsecret |
| Grok Build | `$GROK_HOME/auth.json` | Quota switching is skipped when no quota API is available |
| Mistral Vibe | macOS Keychain or `$VIBE_HOME/.env` | On Windows and Linux, `$VIBE_HOME/.env` is used; `vibe` is required for login flows |
| GitHub Copilot | macOS Keychain item `copilot-cli` keyed `<host>:<login>`, with the signed-in login read from `~/.copilot/config.json` (JSONC); `$COPILOT_GITHUB_TOKEN`/`$GH_TOKEN`/`$GITHUB_TOKEN` as fallback | `copilot` is required for login flows. Store layout and monthly AI-credit quota verified on macOS; the quota endpoint is undocumented. Linux/Windows stores and env-var precedence remain unverified. |

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
