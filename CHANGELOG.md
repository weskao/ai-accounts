## [0.3.1] - 2026-08-26

### ⚙️ Miscellaneous Tasks

- Ignore .omc directory
## [0.3.0] - 2026-08-25

### 🚀 Features

- **timer:** [**breaking**] Report revoked profiles by provider

### 🐛 Bug Fixes

- **keychain:** Stop silently storing empty secrets
- **agy:** Skip redundant keychain writes

### 📚 Documentation

- **changelog:** Release v0.3.0

### 🧪 Testing

- **keychain:** Guard oauth token exposure
## [0.2.1] - 2026-08-23

### 🐛 Bug Fixes

- Secure cross-platform CI and credentials

### 📚 Documentation

- Add list and config demos to readme
- Update config demo gif

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.2.1
## [0.2.0] - 2026-08-23

### 🚀 Features

- **agy:** Measure candidate quota when the slot is free

### 🐛 Bug Fixes

- **autoswitch:** Try the next candidate on a failed switch
- **agy:** Skip candidates a blind switch cannot use

### 📚 Documentation

- Move common workflow example after umbrella command
- Document candidate order and the agy exception
- Document agy's two candidate-measuring modes

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.2.0
## [0.1.0] - 2026-08-22

### 🚀 Features

- **utils:** Cross-platform clipboard, dependency mgmt, and ANSI support
- **vcadd:** Add vChewing user dictionary helper command
- **vcadd:** Git sync with auto union-conflict resolution
- **codex-accounts:** Add multi-profile codex cli account manager
- **utils:** Add ensure_python_package helper and codex install hint
- **codex:** Add refresh and sync commands
- **codex-accounts:** Add interactive switch
- **accounts:** Add gemini profile quota tracking
- **agy-accounts:** [**breaking**] Use ai-accounts profile store
- **claude-accounts:** Add claude profile manager
- **codex-accounts:** Show chatgpt plan tier
- **codex-accounts:** [**breaking**] Central profile store
- **ai-accounts:** Add all-provider account lister
- **claude-accounts:** Show account email in list
- **ai-accounts:** [**breaking**] Forward all subcommands to every provider
- **accounts:** Capitalize plan label first letter in list output
- **accounts:** Color-code PLAN column by tier rank
- **accounts:** Show spinner while list fetches usage
- **accounts:** Show account name in antigravity usage spinner
- **accounts:** Unify list spinner labels; tighten agy readiness poll
- **accounts:** Stream ai-accounts list results as they land
- **accounts:** Add grok-accounts CLI account manager
- **present:** Unify save/refresh/sync success UI
- **towebp:** Add quality flag for conversion
- **ai-accounts:** Add usage command for active account only
- **ai-accounts:** Remove/save parity + help alias
- **autoswitch:** Switch AI accounts automatically on low quota
- **config:** Add stdlib raw-mode key reader
- **config:** Add interactive config menu
- **config:** [**breaking**] Open interactive menu on all five clis
- **config:** Group menu settings and show inline help
- **autoswitch:** Pick weekly quota by default, 5h opt-in
- **config:** Register the OS timer when enabled toggles
- **vibe-accounts:** Add vibe as a fifth managed provider
- **autoswitch:** Add explicit setup lifecycle
- **accounts:** Share oauth-refresh and atomic-write helpers
- **accounts:** Direct oauth/oidc refresh for antigravity and grok
- **config:** Add token_refresh flag and wire the timer to it
- **config:** Show installed version in config panel title
- **agy-accounts:** Support Windows and Linux credential stores
- **i18n:** Add language setting and redesign notifications
- **i18n:** Translate the whole config menu, drop notification frames
- **notify:** Sound on desktop notifications, inert on click
- **notify:** Prefer terminal-notifier on macOS, verify delivery
- **i18n:** [**breaking**] Drop the "system" language menu option
- **config:** [**breaking**] Autosave the config menu, add r reset

### 🐛 Bug Fixes

- **readme:** Replace hardcoded v0.1.0 with vX.Y.Z placeholder
- **vcadd:** Use TextInputMenuAgent, add timeout
- **codex_accounts:** Mirror auth writes to macOS keychain on switch
- **codex_accounts:** Correct usage display and token persistence
- **codex_accounts:** Stabilize profile switching
- **codex-accounts:** Refresh current usage
- **codex-accounts:** Quiet cancelled login
- **agy-accounts:** [**breaking**] Migrate login to antigravity
- **agy-accounts:** [**breaking**] Use official agy sessions
- **agy-accounts:** Stabilize quota process cleanup
- **agy-accounts:** Detect quota listener ports
- **agy-accounts:** [**breaking**] Stabilize login and list
- **agy-accounts:** Mark refreshable auth
- **gemini:** Rename auth column to session
- **platform:** Support clean cross-platform setup
- **agy-accounts:** Show real subscription tier in PLAN
- **claude-accounts:** Treat token-endpoint 401/403 as revoked, not transient
- **utils:** Fall back to ascii spinner on non-braille terminals
- **ci:** Sync lockfile version
- **ci:** Correct cross-platform assumptions
- **ci:** Enable utf-8 on windows
- **claude-accounts:** Send real User-Agent on token refresh
- **present:** Guard empty column set; document no-redaction contract
- **agy:** Save login after keyring update
- **login-switch:** Reject ide-restored session, add timeout
- **ai-accounts:** Centralize profile storage
- **agy-accounts:** Backfill email so save/login-switch show account
- **present:** Count terminal columns for CJK table widths
- **autoswitch-timer:** Harden install against real-world paths
- **vibe-accounts:** Finish ai integration
- **config:** Stabilize and color menu
- **vibe:** Read and write the credential store vibe actually uses
- **autoswitch:** Import ai_accounts in the timer
- **tests:** Make Windows CI portable for autoswitch/oauth tests
- **tests:** Pin agy keyring tests to the mocked security path
- Rewrite stale autoswitch hooks

### 💼 Other

- **deps:** Add pytest to dev dependencies

### 🚜 Refactor

- **vcadd:** Remove explicit reload; vChewing auto-reloads via FSEvents
- **ai-accounts:** [**breaking**] Rename codex_usage to usage_format
- **grok-accounts:** Align output with sibling tools
- **present:** Extract shared presentation module from codex-accounts
- **agy-accounts:** Adopt shared _present presentation module
- **grok-accounts:** [**breaking**] Adopt shared _present module; fix picker + Ctrl-C handling
- **claude-accounts:** Adopt shared _present presentation module
- **ai-accounts:** Dedup no-active-account warning
- **config:** Derive defaults from a field schema
- [**breaking**] Make ai-accounts standalone

### 📚 Documentation

- **changelog:** Release v0.1.0
- **readme:** Switch install to tokenless public HTTPS URL
- **readme:** Document cross-platform clipboard and runtime support
- **readme:** Group zsh aliases by category
- **readme:** Document codex-accounts tool
- Add claude.md project guidance
- Update codex accounts list example
- **readme:** Fix demo lab duration
- **readme:** Clarify codex-accounts profiles
- **changelog:** Release v0.5.1
- **changelog:** Release v1.0.0
- **agy-accounts:** Add output section for list, who, and switch commands
- **readme:** Document central ~/.ai-accounts profile store
- **claude.md:** Require README sync on user-visible changes
- **readme:** Document codex-accounts PLAN column
- **claude.md:** Note claude_usage.py in shared-helper map
- Reflect usage_format rename and ai-accounts forwarding
- **readme:** Capitalize plan labels in list examples
- Record why agy-accounts list can't parallelize usage fetches
- Spec for ai-accounts list per-provider progress rows
- Link agy-parallel-limitation from agy-accounts list
- **grok-accounts:** Fix switch-panel overclaim; neutralize profile names in list example
- Require placeholder data in test fixtures
- Record the credential hot-reload spike findings
- **config:** Document the interactive config menu
- Record how to add a config setting
- **config:** Correct the config menu mockup
- **changelog:** Release v3.0.0
- **config:** Document switch_window and timer auto-registration
- Document token refresh across all four account tools
- **config:** Clarify agy_blind_switch help text
- **readme:** Document the language key and message formats
- **readme:** Unframed notifications and the language row
- **readme:** Document the terminal-notifier notification path
- Add prerequisites to README
- **readme:** Document config menu autosave and r reset
- Document the standalone project

### ⚡ Performance

- **accounts:** Fetch usage in parallel across profiles

### 🎨 Styling

- Translate remaining chinese messages to english

### 🧪 Testing

- Add cross-platform clipboard and dependency-check tests
- **cross-platform:** Pin resolve_account_dir behavior
- **present:** Sentinel-leak and shared-grammar coverage across account tools
- **config:** Cover config menu autosave and reset

### ⚙️ Miscellaneous Tasks

- Add MIT LICENSE and README License section
- **release:** Bump version to 0.2.0
- **release:** Bump version to 0.3.0
- **release:** Bump version to 0.3.1
- **release:** Bump version to 0.3.2
- **release:** Bump version to 0.3.3
- **release:** Bump version to 0.3.4
- **release:** Bump version to 0.3.5
- **release:** Bump version to 0.4.0
- Sync uv.lock to version 0.4.0
- Ignore claude installer state
- Ignore claude skills directory
- **release:** Bump version to 0.5.0
- **dev:** Add lint and type tools
- **release:** Bump version to 0.6.0
- **release:** Bump version to 0.7.0
- **release:** Prepare v1.0.0
- Ignore account profile stores
- **release:** Bump version to 2.0.0
- **release:** Bump version to 2.1.0
- **release:** Bump version to 2.2.0
- Ignore .omo directory
- **release:** Bump version to 2.3.0
- **release:** Bump version to 2.4.0
- **release:** Bump version to 3.0.0
- **release:** Bump version to 4.0.0
- Resync uv.lock with the released version bump
- **release:** Bump version to 5.0.0
