## [2.1.0] - 2026-07-20

### 🚀 Features

- **accounts:** Show spinner while list fetches usage
- **accounts:** Show account name in antigravity usage spinner
- **accounts:** Unify list spinner labels; tighten agy readiness poll
- **accounts:** Stream ai-accounts list results as they land

### 🐛 Bug Fixes

- **utils:** Fall back to ascii spinner on non-braille terminals

### 📚 Documentation

- Record why agy-accounts list can't parallelize usage fetches
- Spec for ai-accounts list per-provider progress rows

### ⚡ Performance

- **accounts:** Fetch usage in parallel across profiles
## [2.0.0] - 2026-07-20

### 🚀 Features

- **claude-accounts:** Add claude profile manager
- **codex-accounts:** Show chatgpt plan tier
- **codex-accounts:** [**breaking**] Central profile store
- **ai-accounts:** Add all-provider account lister
- **claude-accounts:** Show account email in list
- **ai-accounts:** [**breaking**] Forward all subcommands to every provider
- **accounts:** Capitalize plan label first letter in list output
- **accounts:** Color-code PLAN column by tier rank

### 🐛 Bug Fixes

- **agy-accounts:** Show real subscription tier in PLAN
- **claude-accounts:** Treat token-endpoint 401/403 as revoked, not transient

### 🚜 Refactor

- **ai-accounts:** [**breaking**] Rename codex_usage to usage_format

### 📚 Documentation

- **agy-accounts:** Add output section for list, who, and switch commands
- **readme:** Document central ~/.ai-accounts profile store
- **claude.md:** Require README sync on user-visible changes
- **readme:** Document codex-accounts PLAN column
- **claude.md:** Note claude_usage.py in shared-helper map
- Reflect usage_format rename and ai-accounts forwarding
- **readme:** Capitalize plan labels in list examples

### 🧪 Testing

- **cross-platform:** Pin resolve_account_dir behavior

### ⚙️ Miscellaneous Tasks

- Ignore account profile stores
- **release:** Bump version to 2.0.0
## [1.0.0] - 2026-07-19

### 🚀 Features

- **agy-accounts:** [**breaking**] Use ai-accounts profile store

### 🐛 Bug Fixes

- **platform:** Support clean cross-platform setup

### 📚 Documentation

- **changelog:** Release v1.0.0

### ⚙️ Miscellaneous Tasks

- **release:** Prepare v1.0.0
## [0.7.0] - 2026-07-19

### 🚀 Features

- **accounts:** Add gemini profile quota tracking

### 🐛 Bug Fixes

- **agy-accounts:** [**breaking**] Migrate login to antigravity
- **agy-accounts:** [**breaking**] Use official agy sessions
- **agy-accounts:** Stabilize quota process cleanup
- **agy-accounts:** Detect quota listener ports
- **agy-accounts:** [**breaking**] Stabilize login and list
- **agy-accounts:** Mark refreshable auth
- **gemini:** Rename auth column to session

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.7.0
## [0.6.0] - 2026-07-16

### 🚀 Features

- **codex-accounts:** Add interactive switch

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.6.0
## [0.5.1] - 2026-07-16

### 🐛 Bug Fixes

- **codex_accounts:** Stabilize profile switching
- **codex-accounts:** Refresh current usage
- **codex-accounts:** Quiet cancelled login

### 📚 Documentation

- Update codex accounts list example
- **readme:** Fix demo lab duration
- **readme:** Clarify codex-accounts profiles
- **changelog:** Release v0.5.1

### ⚙️ Miscellaneous Tasks

- **dev:** Add lint and type tools
## [0.5.0] - 2026-07-11

### 🚀 Features

- **codex-accounts:** Add multi-profile codex cli account manager
- **utils:** Add ensure_python_package helper and codex install hint
- **codex:** Add refresh and sync commands

### 🐛 Bug Fixes

- **codex_accounts:** Mirror auth writes to macOS keychain on switch
- **codex_accounts:** Correct usage display and token persistence

### 💼 Other

- **deps:** Add pytest to dev dependencies

### 📚 Documentation

- **readme:** Group zsh aliases by category
- **readme:** Document codex-accounts tool
- Add claude.md project guidance

### ⚙️ Miscellaneous Tasks

- Sync uv.lock to version 0.4.0
- Ignore claude installer state
- Ignore claude skills directory
- **release:** Bump version to 0.5.0
## [0.4.0] - 2026-07-01

### 🚀 Features

- **vcadd:** Use conventional commit format for userdata-cht commits

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.4.0
## [0.3.5] - 2026-06-30

### 🚜 Refactor

- **vcadd:** Remove explicit reload; vChewing auto-reloads via FSEvents

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.3.5
## [0.3.4] - 2026-06-30

### 🚀 Features

- **vcadd:** Git sync with auto union-conflict resolution

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.3.4
## [0.3.3] - 2026-06-29

### 🐛 Bug Fixes

- **vcadd:** Retry activation check after switching to vChewing

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.3.3
## [0.3.2] - 2026-06-29

### 🐛 Bug Fixes

- **vcadd:** Auto-switch to vChewing if not active before reload

### 🎨 Styling

- Translate remaining chinese messages to english

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.3.2
## [0.3.1] - 2026-06-27

### 🐛 Bug Fixes

- **vcadd:** Use TextInputMenuAgent, add timeout

### 🎨 Styling

- **vcadd:** Translate error messages from chinese to english

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.3.1
## [0.3.0] - 2026-06-27

### 🚀 Features

- **vcadd:** Add vChewing user dictionary helper command

### 🐛 Bug Fixes

- **readme:** Replace hardcoded v0.1.0 with vX.Y.Z placeholder

### ⚙️ Miscellaneous Tasks

- **release:** Bump version to 0.3.0
## [0.2.0] - 2026-06-20

### 🚀 Features

- **utils:** Cross-platform clipboard, dependency mgmt, and ANSI support

### 🐛 Bug Fixes

- **gtrans:** Treat all post-flag tokens as translation content

### 📚 Documentation

- **readme:** Switch install to tokenless public HTTPS URL
- **readme:** Document cross-platform clipboard and runtime support

### 🧪 Testing

- Add cross-platform clipboard and dependency-check tests

### ⚙️ Miscellaneous Tasks

- Add MIT LICENSE and README License section
- **release:** Bump version to 0.2.0
## [0.1.0] - 2026-05-17

### 📚 Documentation

- **changelog:** Release v0.1.0
