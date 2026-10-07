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

When a newer GitHub release exists, any command run in a real terminal (both
stdin and stdout are a TTY) ends by asking — `❯ Update now` / `Skip` / `Skip
until next version` — instead of just hinting. Choosing *Update now* runs the
`uv tool install --force --from git+…@vX.Y.Z ai-accounts` install for you;
*Skip until next version* is remembered (in the same cache file) so the
prompt stays quiet until a release past that one shows up. A command whose
output is piped, or run from the scheduled timer or a vendor-CLI hook, falls
back to the old two-line stderr hint instead — never an interactive prompt
with no keyboard behind it. The prompt box also shows a "Release notes:" link
to the new version's GitHub release page. The GitHub request runs in the background while
the command works, at most once every 10 minutes (cached in
`~/.ai-accounts/update-check.json`, 0.8 s timeout); offline stays silent, and
the exit code never changes. Turn it off with
`ai-accounts config set update_check false`.

## Commands

| Command | Purpose |
| --- | --- |
| `ai-accounts` | Run one operation across every provider |
| `ai-accounts --version` / `ai-accounts version` | Print the current ai-accounts version |
| `codex-accounts` | Manage Codex CLI profiles and ChatGPT OAuth usage |
| `claude-accounts` | Manage Claude Code profiles and quota usage |
| `agy-accounts` | Manage Antigravity profiles and quota usage |
| `grok-accounts` | Manage Grok Build OAuth profiles |
| `vibe-accounts` | Manage Mistral Vibe API-key profiles |
| `copilot-accounts` | Manage GitHub Copilot CLI profiles, identity and monthly credit balance |

Every command also accepts a `--` prefix: `ai-accounts --config` is the same
as `ai-accounts config`, `codex-accounts --list` is the same as
`codex-accounts list`, and so on for every provider tool.

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
ai-accounts --version                         # Print the current ai-accounts version
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

The other provider tools use the same commands — swap `codex-accounts` for the
tool you need. For example:

```sh
claude-accounts login-switch work             # Log in to Claude Code and save it as "work"
agy-accounts switch                           # Pick a saved Antigravity profile from a menu
copilot-accounts usage                        # Show the active Copilot account's credit balance
```

`switch` and `remove` without a name open an interactive picker, and `save`
without a name picks one from the active account's email or login. What
differs per tool:

| Command | Worth knowing |
| --- | --- |
| `claude-accounts` | Same workflow as Codex; `login-switch` runs `claude auth login` |
| `agy-accounts` | `list` is slow (one account at a time) — see [Fast Antigravity lists](#fast-antigravity-lists); `usage` checks only the active account; `api-schema` prints agy's reply fields for [change watching](#watching-agys-api-for-changes) |
| `grok-accounts` | `list` shows SuperGrok plan and weekly usage; no auto-switch |
| `vibe-accounts` | API-key profiles with no quota data; `refresh` is not needed for static keys; no auto-switch |
| `copilot-accounts` | `list` shows the monthly AI-credit balance; `refresh` only checks the token is still valid; no auto-switch yet |

Run `<tool> help` for the full command list of any tool.

Every per-provider tool's `list`/`usage` also accepts `--json`, printing one
JSON array of `{"name", "active", "usage", "no_quota_api"}` objects instead of
the table (`usage` is `null` and `no_quota_api` is `true` for Vibe, which has
no quota API). Grok and Copilot attempt a real quota lookup and only degrade
to the same `no_quota_api: true` shape when that lookup fails — see Platform
notes below.

### Antigravity profile names

`agy-accounts save [<profile_name>]` uses the active email when available. If
Antigravity cannot report an email, it still saves the account under a stable
`agy-<token-digest>` label; pass a name to choose your own label.

`who` also answers to `current`. `login-switch <name>` runs a fresh provider
login and saves the result as `<name>` — it is what the re-login report below
tells you to run.

### Grok plan and usage

`grok-accounts list` shows `PROFILE`, `ACCOUNT`, `PLAN`, `ID`, `1W USED`,
`BUILD USED`, `UPDATED`, `EXPIRES`, and `STATE`. `PLAN` is the consumer
subscription (`SuperGrok`, `SuperGrok Plus`, `SuperGrok Heavy`, or `Free`)
from the Grok Build CLI billing proxy — not the OIDC `principal_type`, which
is `User` for every personal login. A successful `/user` reply with a null
`subscriptionTier` is `Free`. `1W USED` is the weekly SuperGrok credit
pool; `BUILD USED` is the Grok Build slice of that same week. Both come from
`GET https://cli-chat-proxy.grok.com/v1/billing?format=credits` plus
`GET …/v1/user?include=subscription`, authenticated with the profile's OAuth
access token. A failed lookup degrades to the no-quota JSON shape rather than
failing `list`.

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
matching the plan allowance used by Copilot `/usage`. A quota marked
`has_quota: false` with an entitlement of `0` — such as the obsolete premium
bucket on a credit plan — is unavailable, not 100% used; the same flag on an
entitled bucket means the quota is *spent*, so an exhausted account reads
`100% · 200/200 AIC` with `0 AIC` remaining instead of `—`. Legacy billing
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
  *access* token backed by a live refresh token is normal, not a failure) —
  this reads a migrated profile's `_secrets.has_refresh_token` marker the same
  way it reads an unmigrated profile's inline refresh token, never the
  credential store itself
- the auto-switch timer's install state
- where saved-profile secrets currently live (see "Where your secrets live"
  below) — the OS credential store's label, or a plaintext-fallback reason —
  printed once for the whole run, not per provider

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
Token storage: PASS macOS Keychain
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
shorter), the top-level `account_tool_note` key mirrors the optional
"different checkout" note (`null` when it doesn't apply), and the top-level
`token_storage` key mirrors the "Token storage" line (`{"ok": true, "detail":
"macOS Keychain"}`, or `{"ok": true, "detail": "plaintext fallback (<reason>)"}`
with no OS credential store):

```sh
ai-accounts doctor --json | python3 -m json.tool
```

### List performance

`ai-accounts list` runs providers concurrently and displays each provider's
table as it finishes. Codex, Claude, Grok, and Copilot also fetch usage
concurrently across profiles. Vibe lists local profile data without quota
network requests. Copilot additionally fetches GitHub identity per profile
(plus the primary email when permitted). Failed quota requests retain the
same no-quota result shape as Vibe.

Antigravity queries different credentials **sequentially**: each query switches
the shared OS keyring session, launches `agy`, waits for authentication and
quota data, then closes it. The original session is restored after listing.
Within each launch, quota and account-status RPCs run concurrently. `agy` is
launched with a freshly generated `--csrf_token` and every RPC carries it as
`x-codeium-csrf-token`; the language server answers `401 missing CSRF token`
without it, which would surface as `ERR agy` in the `UPDATED` column. Its
replies are proto3 JSON, which omits default values, so a fully spent bucket
carries no `remainingFraction` at all — an identifiable bucket (one with a
`bucketId`, `window`, or `resetTime`) that is missing the fraction reads as
100% used, while a bucket with none of those markers stays blank rather than
claiming exhaustion from a malformed payload. Profiles
with identical credential file contents reuse a successful result within that
invocation; different credentials and failed lookups are not reused.

Measured on **2026-09-07 (Asia/Taipei)**, on a local macOS machine:

| Provider command | Saved profiles | Total list time | Time per profile (total ÷ count) | Work performed |
| --- | ---: | ---: | ---: | --- |
| `codex-accounts list` | 5 | 0.944 s | 0.189 s | Concurrent usage requests |
| `claude-accounts list` | 1 | 0.739 s | 0.739 s | Concurrent usage requests when multiple profiles exist |
| `agy-accounts list` | 6 | 24.484 s | 4.081 s | Sequential credential sessions; concurrent RPCs within each session |
| `grok-accounts list` | 7 | 0.003 s | <0.001 s | Concurrent SuperGrok billing requests (was local-only before quota lookup) |
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

Ctrl-C stops `list` for any provider, including `ai-accounts list`. The
command exits with status 130 and does not print a traceback.

To query only the selected Antigravity account, use:

```sh
agy-accounts usage
```

### Watching agy's API for changes

agy's local API has no public spec. `api-schema` prints the closest thing: the
field names and JSON types of the two replies quota reading depends on
(`RetrieveUserQuotaSummary` and `GetUserStatus`), as one JSON object. It never
prints a value, so no email, name, or quota number appears.

```sh
agy-accounts api-schema
```

The object holds:

| Key | Meaning |
| --- | --- |
| `agy_version` | Output of `agy --version` |
| `methods` | Per reply, every field path mapped to its JSON types. `[]` marks list items. A map keyed by non-identifiers, such as `supportedMimeTypes`, collapses to `{*}` |
| `watched` | The field paths the quota and plan parsers read |
| `parsed` | Whether each parsed value (weekly and 5-hour windows, email, plan) was found |
| `buckets` | Each quota bucket as `group · bucketId · window`, without numbers |
| `error` | `null`, or why agy could not be read (the exit status is then 1) |

It reads only the live session, so no saved profile is ever made live for it.
agy omits fields that hold a default value, so a spent bucket's
`remainingFraction` can be missing without any API change.
Quota periods use the bucket's `window` field, falling back to its ID/name
when that field is missing or blank; unknown explicit periods remain unavailable.
If agy returns only weekly buckets,
the 5-hour readings remain unavailable; they are not inferred from weekly
quota. Optional tier UI fields such as `upgradeButtonText` do not affect
email or PLAN parsing.

agy can answer `GetUserStatus` before its keyring session has loaded. That
early reply carries only an error message and no email or tier. Quota reading
ignores it and keeps polling, so the PLAN column no longer goes blank for a
healthy account.

### Fast Antigravity lists

Antigravity can only report quota for the live credential session, so a live
`agy-accounts list` must check profiles one at a time. Cached-list mode
(`agy_list_cached_usage`, **on by default**) queries the current account live
and uses saved readings for other accounts instead:

```sh
agy-accounts list --refresh  # populate or update the saved readings
agy-accounts list            # current account live; other accounts from cache
```

A live probe makes each profile the keyring session for a moment and, when agy
rotated its access token, folds the fresh token back into the profile. That
fold-back only happens while the keyring still holds the profile's own refresh
token: a running `agy` session or the Antigravity IDE can rewrite the shared
slot in between, and saving that blob would hand the profile another account's
tokens (the row then shows `RELOGIN` until fixed). `refresh`, `switch`, `sync`
and `login-switch` apply the same check.

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
├── dpapi/                    # Windows only: DPAPI-encrypted secret files
├── codex/accounts/
├── codex/backups/
├── claude/accounts/
├── antigravity/accounts/
├── antigravity/usage-cache.json
├── grok/accounts/
├── vibe/accounts/
└── copilot/accounts/
```

Newly saved `telegram_bot_token` values go to the OS
credential store instead — macOS Keychain, the Secret Service (`secret-tool`)
on Linux, Credential Manager on Windows — under the service name
`ai-accounts`. On a machine with no credential store, saving a token is
**refused** rather than written as plaintext or scrambled with something
reversible; export `AI_ACCOUNTS_TELEGRAM_BOT_TOKEN` there instead. That
variable also overrides the stored value whenever it is set, so the read order
is environment → credential store → config file.

That last rung exists only for upgrades: a token already sitting in an older
`config.json` keeps working, and the next save moves it into the credential
store and drops it from the file. If the store is unavailable, the legacy
plaintext value remains to avoid losing the only copy; existing backups are
not scrubbed by migration.

| Platform | Bot token location | Chat ID location / protection |
| --- | --- | --- |
| macOS | Keychain generic password: service `ai-accounts`, account `telegram_bot_token` | `~/.ai-accounts/config.json`, plaintext, owner-only mode `0600` on writes |
| Linux | Secret Service default collection via `secret-tool`: `service=ai-accounts`, `username=telegram_bot_token` | `~/.ai-accounts/config.json`, plaintext, owner-only mode `0600` on writes |
| Windows | Credential Manager generic credential: `ai-accounts:telegram_bot_token` | `%USERPROFILE%\.ai-accounts\config.json`, plaintext; access depends on inherited Windows ACLs, not POSIX mode `0600` |

`AI_ACCOUNTS_CONFIG_JSON` overrides the JSON path on every platform. The
credential-store slot belongs to the OS user and is shared across config paths.
Linux needs both `secret-tool` and a running, unlocked Secret Service; a
headless session may not have one. Credential stores protect storage, but do
not guarantee protection from malware running as the same user or an administrator.

The bot token authenticates the bot; keep it private. The chat ID only selects
the recipient, but can identify a user/group: it remains visible in `--config`
and `config export`, so redact it before sharing exports. Token entry in
`--config` is hidden, including the numbered fallback (which refuses input if
echo cannot be disabled). Prefer that over `config set telegram_bot_token …`,
whose argument can appear in shell history and process listings. Environment
overrides are also plaintext process data; do not commit them to shell files.
If a token was exposed, revoke/regenerate it with BotFather and save the replacement.

Saved-profile secrets follow the same pattern. When an OS credential store is
available, a saved profile's `<name>.json` holds metadata only — expiry
fields, account identity, and a small `_secrets` marker block (`store`,
`key`, `has_refresh_token`, a non-secret fingerprint) — while the actual
access/refresh/id tokens live under the credential store's service name
`ai-accounts`, keyed `<tool>--<profile>` (e.g. `codex--work`). See "Where your
secrets live" below for what this protects against, per OS, in plain terms.
Switching an account keeps one latest pre-switch backup per tool (not one per
switch): also in the credential store when available, and printed as "backed
up to <store label>"; otherwise a single plaintext `<tool>/backups/latest`
file, printed as "backed up to backups/latest".

An existing profile written by an older version of this tool still has its
tokens inline; the next time it's read, they're moved into the credential
store and the file is rewritten without them — a one-time, automatic
migration with a short printed line (`→ Moved secrets of <file> into the
<store label>`). A store write that fails, or a machine with no credential
store at all (e.g. headless Linux without `secret-tool`), falls back to the
full plaintext file exactly as before, plus one yellow warning per process —
this is a fallback, not a refusal, so existing headless setups keep working.

`antigravity/usage-cache.json` holds the last quota reading seen for each agy
profile — quota windows, plan and timestamp, but no credentials (see below for
why it is kept). A plaintext-fallback profile JSON file does contain live
credentials: do not commit, publish, or share this directory. Writes use
owner-only permissions and atomic replacement where the provider format
allows it. Windows has no POSIX permission bits, so there the protection
comes from the parent directory's inherited ACL: the default location under
your user profile is already owner-only, but an override pointing outside it
inherits whatever that directory allows.

Provider-native legacy stores such as `~/.codex/accounts` and
`~/.claude/accounts` are moved into the central directory on first use. Override
paths with `CODEX_ACCOUNT_DIR`, `CLAUDE_ACCOUNT_DIR`,
`ANTIGRAVITY_ACCOUNT_DIR`, `GROK_ACCOUNT_DIR`, `VIBE_ACCOUNT_DIR`, or
`COPILOT_ACCOUNT_DIR`. Override the shared config with
`AI_ACCOUNTS_CONFIG_JSON`.

## Where your secrets live

No setup needed — this happens automatically the first time each tool saves
or reads a profile. On macOS, the very first access may show a one-time
Keychain "Allow" prompt for the process asking; approving it is normal and
only needs to happen once per binary.

| OS | Where tokens go | Fallback if unavailable |
| --- | --- | --- |
| macOS | Keychain (`security`), service `ai-accounts` | plaintext profile file |
| Windows | DPAPI-encrypted files under `~/.ai-accounts/dpapi` | plaintext profile file |
| Linux | Secret Service via `secret-tool` (GNOME Keyring, KWallet, ...) | plaintext profile file |

What this protects against: a file-system backup, a cloud-sync folder, or a
Time Machine copy landing on an unencrypted disk no longer hands over a live
token with it; an accidental `cat ~/.ai-accounts/**/*.json`, screenshot, or
`git add -A` no longer leaks a token, only account metadata; and another
account on the same machine reading your files directly (without also being
able to unlock your keychain/session) gets nothing usable.

What it does **not** protect against: malware or a script running as *your
own user* can still ask the credential store for the secret, the same way
this tool does — an OS credential store stops a file being read, not your own
account being compromised. It also doesn't touch each vendor CLI's own files
(`~/.codex/auth.json`, `~/.claude/.credentials.json`, and similar) — those stay
exactly as that vendor's own CLI writes them; this tool's credential-store
protection covers its own saved profile copies, not the live session file the
vendor CLI itself manages.

## Auto-switch

Auto-switch can refresh quota data, select another saved profile when the active
profile crosses a configured threshold, notify you, and restart supported
interactive sessions. Every notification ai-accounts sends — a switch, a dead
end with no account left to switch to, the re-login report, a quota reset —
ends with the source device (for example, `💻 MacBook Pro · d011********` or
`🖥️ Mac mini · d011********`), over desktop and Telegram alike.

The device code uses the hardware serial on macOS (`ioreg`), and an
application-specific HMAC-SHA256 digest of Windows `MachineGuid` or Linux
`/etc/machine-id` (falling back to `/var/lib/dbus/machine-id`). Only the first
four characters are shown; the rest are masked. These IDs do not depend on
network interfaces. Windows/Linux IDs identify the OS installation and may
change after reinstalling or be duplicated by cloning an image. If no valid ID
is readable, the label shows `unknown` and notifications still work.

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
behavior and notifications. Arrow keys select and change values, `d` resets
the highlighted field to its default after a `y` confirmation naming that
field, `D` resets every setting to its default after a `y` confirmation, and
each change is saved as you make it — there is no separate save step. When stdin is not a
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
OS timer fallback. Re-run it after reinstalling the package if hook commands or
the installed Python path changed.
To restart or re-register the timer on any supported OS, re-run
`ai-accounts install-timer --interval 1800` (use your existing interval if it
differs). This unloads and loads the LaunchAgent on macOS, reloads and restarts
the systemd user timer on Linux (or replaces the cron entry), and replaces the
Task Scheduler task on Windows. `ai-accounts config` changes are read on the
next timer tick; they do not require a restart.
Telegram notifications from timer runs include the scheduler and job label:
`launchd: com.ai_accounts.autoswitch` on macOS,
`systemd: com.ai_accounts.autoswitch.timer` or `cron: ai-accounts-autoswitch`
on Linux, and `Task Scheduler: com.ai_accounts.autoswitch` on Windows.
The timer has no dedicated log file.
`copilot-accounts autoswitch` switches off the active profile once its monthly
quota reaches `switch_when_used_pct`; Copilot has no Stop hook, so the timer is
its only trigger, and the new account applies on Copilot CLI's next launch.

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
next tick and never reported here. Copilot has no refresh token: its `refresh`
checks the saved token against GitHub, and only a `401` lands in this report.
Like every notification, it ends with the
source device (see [Auto-switch](#auto-switch)).

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

Every notification also says **how long ago** each window reset, e.g.
`• codex · work · weekly (reset 2d 9h ago, was 100%)` — the units follow the
notification language (zh-TW: `2 天 9 小時前已重置`). A reset can be noticed
late — the timer was not running, a provider's `list --json` kept failing, or
agy's cache was stale — and it is still reported then, but the age makes it
read as old news instead of quota that just came back. A scheduled reset is
dated by the deadline it passed; an off-schedule one only by the previous
scan, so it reads `reset within the last 30m`. The age is left out when
unknown (a state entry from an older version) and when under a minute, so a
reset that fresh keeps the plain wording (`was 95%`).

Detection runs across every saved profile for a covered provider, not just
the currently active one, so an account benched by auto-switch still gets
its "usable again" notification. Each timer tick sends at most one grouped
notification over the configured `notify` channel, covering every window
that reset since the previous tick. The title counts the windows per
provider (`codex ×1, claude ×2, agy ×1`).

`docs/quota-reset-cases.md` is the full case table — every scenario the
detection rule was verified against, with the number of notifications each
one produces, including the ones that deliberately produce none.

Provider coverage:

- `codex` / `claude` — hourly and weekly windows
- `copilot` — the monthly window
- `grok` — weekly SuperGrok credits and the Grok Build slice (`1W USED` /
  `BUILD USED` in `grok-accounts list`)
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
- `vibe` — not supported: no quota API to watch.

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
| Grok Build | `$GROK_HOME/auth.json` | SuperGrok plan and weekly/Grok Build usage come from the CLI billing proxy. Autoswitch is still skipped (no ranking/switch path yet) |
| Mistral Vibe | macOS Keychain or `$VIBE_HOME/.env` | On Windows and Linux, `$VIBE_HOME/.env` is used; `vibe` is required for login flows |
| GitHub Copilot | OS credential store item `copilot-cli` / `<host>:<login>` (macOS Keychain; Linux Secret Service via `secret-tool`; Windows Credential Manager target `<host>:<login>.copilot-cli`, UTF-16), with the signed-in login read from `~/.copilot/config.json` (JSONC); that file's plaintext `authTokens` when the CLI has no store (or `storeTokenPlaintext` is set); `$COPILOT_GITHUB_TOKEN`/`$GH_TOKEN`/`$GITHUB_TOKEN` as fallback | `copilot` is required for login flows. Store layout and monthly AI-credit quota verified on macOS; plaintext layout verified with `copilot login --with-token`. Linux/Windows naming follows the keyring-core store crates the CLI ships but is not yet observed on a live login; Linux needs `secret-tool` (libsecret). `switch` writes the plaintext entry only when the CLI already uses it. |

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
