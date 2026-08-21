# ai-accounts

Personal CLI toolbox — a collection of productivity tools packaged as a Python
project and managed via [`uv tool`](https://docs.astral.sh/uv/concepts/tools/).

Requires **uv** ([install uv](https://docs.astral.sh/uv/getting-started/installation/))
and Python ≥ 3.10. No GitHub token or SSH key is required to install — the
repo is public.

The package installs and every command starts cleanly on **macOS, Windows, and
Linux**. `vcadd` requires macOS because the vChewing input method it edits is
macOS-only; it reports that limitation without a traceback elsewhere.
`agy-accounts` needs to reach the OS credential store holding the live `agy`
session — built in on macOS and Windows, but on Linux it needs `secret-tool`
(`libsecret`), and says so if that is missing.

Install `uv` first:

```sh
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Install (all tools in one shot)

Replace `vX.Y.Z` with the latest tag from
[Releases](https://github.com/weskao/ai-accounts/releases):

```sh
uv tool install --from git+https://github.com/weskao/ai_accounts.git@vX.Y.Z ai-accounts
```

Or install the latest `main`:

```sh
uv tool install --from git+https://github.com/weskao/ai_accounts.git ai-accounts
```

### Install from a clone

The same checkout flow works in bash, zsh, and PowerShell:

```sh
git clone https://github.com/weskao/ai_accounts.git
cd ai-accounts
uv sync --locked
uv tool install --editable .
```

`uv sync --locked` installs the development/test environment from `uv.lock`;
`uv tool install --editable .` exposes every command globally from that checkout.

After install, the following commands are available on `PATH`:

| Command | Purpose |
| --- | --- |
| `gtrans` | Google Translate CLI with clipboard + chunked translation |
| `charcount` | Count characters in text or file, with optional limit |
| `imgmin` | Visually-lossless image compression toolkit |
| `resize-image` | Resize images (JPG/PNG/WebP) via ImageMagick |
| `towebp` | Convert PNG/JPG/JPEG to WebP |
| `html2md` | Convert HTML files to Markdown via pandoc |
| `vcadd` | Add Chinese words with 注音符號（Bopomofo）readings to vChewing user dictionary (macOS) |
| `codex-accounts` | Manage multiple Codex CLI login profiles (save / list / switch / remove / config) |
| `claude-accounts` | Manage multiple Claude Code login profiles, inspect usage, and configure auto-switch (config) |
| `agy-accounts` | Manage multiple Antigravity OAuth profiles, inspect quota, and configure auto-switch (macOS, config) |
| `grok-accounts` | Manage multiple Grok Build CLI OAuth profiles (save / list / switch / refresh / config) |
| `vibe-accounts` | Manage multiple Mistral Vibe API-key profiles (save / list / switch / config) |
| `ai-accounts` | Drive every AI account tool at once — forwards any subcommand (`list`, `usage`, `who`, `refresh`, `sync`, `autoswitch`, …) to all five `*-accounts`, plus its own timer commands and the shared `config` for [auto-switch on low quota](#auto-switch-on-low-quota) |

## Update

```sh
uv tool upgrade ai-accounts
```

## Reinstall (force)

```sh
uv tool install --reinstall --from git+https://github.com/weskao/ai_accounts.git@vX.Y.Z ai-accounts
```

## Uninstall

```sh
uv tool uninstall ai-accounts
```

---

## `gtrans` — Google Translate CLI

A lightweight Google Translate CLI with clipboard support and automatic chunking for long texts.

**Default:** English (`en`) → Traditional Chinese (`zh-TW`)

### gtrans Options

| Flag | Description |
| --- | --- |
| `-e` | Translate to English with auto-detection |
| `-s <lang>` | Source language code (e.g. `en`, `zh-TW`, `ja`, `auto`) |
| `-t <lang>` | Target language code (e.g. `en`, `zh-TW`, `ja`) |
| `-f <file>` | Read input from a file |
| `-w` | Write the translation back to the input file (requires `-f`) |

### gtrans Examples

```sh
# Basic translation
gtrans "Hello world"               # EN → ZH-TW (default)
gtrans -t ja "Good morning"        # EN → JA
gtrans -s zh-TW -t en "你好"        # explicit source + target

# Quick English mode
ge "你好"                           # auto-detect → EN  (alias: ge='gtrans -e')
gtrans -e "你好"                    # same, via flag

# File translation
gtrans -f file.txt                  # translate file (EN → ZH-TW)
gtrans -f file.txt -e               # translate file to English
gtrans -f file.txt -t ja            # translate file to Japanese
gtrans -f file.txt -w               # translate + overwrite original file
gtrans -f -w file.txt               # same (filename can follow trailing flags)
ge -f file.txt -w                   # translate file to English + overwrite
ge -f -w file.txt                   # same, alternative ordering
```

> Results are automatically copied to the system clipboard — `pbcopy` on macOS,
> the Win32 clipboard API on Windows, and `wl-copy` / `xclip` / `xsel` on Linux.
> Texts over 4500 characters are split into chunks and translated sequentially.

---

## `charcount` — Character Counter

Count characters in text or a file, with an optional upper limit check.

### charcount Options

| Flag | Description |
| --- | --- |
| `-f <file>` | Read input from a file |
| `-l <limit>` | Set an upper limit; exits non-zero if exceeded |

### charcount Examples

```sh
charcount "Hello world"              # count characters in a string
charcount -f file.txt                # count characters in a file
charcount -l 3000 "some text"        # count with a 3000-char limit
charcount -f file.txt -l 4500        # file + limit check
```

---

## `imgmin` — Image Compression Toolkit

Non-destructive image compression. Originals are **never modified**; output always
goes into a sibling `imgmin-out/` directory.

### imgmin Usage

```sh
imgmin <file> [1|2]           # compress a single file
imgmin <file> --to-png        # convert to PNG then compress
imgmin <dir> [1|2]            # batch compress all images in a directory (top level)
imgmin <dir> -r               # batch + recurse into sub-folders
imgmin <dir> --to-png         # batch + force every output to PNG
imgmin .                      # shortcut: process current directory
imgmin_dir <dir> [options]    # explicit batch alias (same as imgmin <dir>)
imgmin -h                     # show full help
```

### Mode

| Mode | Description |
| --- | --- |
| `1` | Convert **all formats to JPEG** at quality 70 (TinyPNG-style). Typical savings: HEIC ~70%, PNG/WebP ~50–70%, JPEG ~15–30%. SVG is skipped. |
| `2` *(default)* | **Format-aware "visually lossless"** compression — each format uses its own optimiser. |

Omit `1` or `2` to be prompted interactively (single keypress, no Enter needed).

### Supported formats & tool chain

| Format | Tool chain |
| --- | --- |
| `.png` | `pngquant -Q 80-95` → `oxipng -o max` |
| `.jpg` / `.jpeg` | `jpegoptim --max=85 --all-progressive --strip-all` |
| `.webp` | `cwebp -q 82 -m 6 -af -sharp_yuv -pass 10` (falls back to copy if larger) |
| `.svg` | `svgo --multipass` |
| `.gif` | `gifsicle -O3 --lossy=30` |
| `.heic` | `sips formatOptions 70` (macOS native) |
| `.heic --to-png` | `sips -s format png` (no libheif required) |
| `.heif` `.tiff` `.tif` `.bmp` `.avif` `.raw` | `sharp` → PNG → `pngquant` → `oxipng` |

### imgmin Examples

```sh
imgmin logo.png                      # → ./imgmin-out/logo.png
imgmin photo.heic                    # → ./imgmin-out/photo.heic
imgmin scan.tiff                     # → ./imgmin-out/scan.png
imgmin banner.jpg --to-png           # → ./imgmin-out/banner.png
imgmin photo.heic --to-png           # → ./imgmin-out/photo.png  (via sips)
imgmin .                             # batch top-level → ./imgmin-out/
imgmin assets/                       # batch top-level → assets/imgmin-out/
imgmin assets/ -r                    # batch + recurse into sub-folders
imgmin assets/ --to-png              # batch top-level + force PNG output
imgmin assets/ -r --to-png           # batch + recurse + force PNG
imgmin assets/ 1                     # batch mode 1 (all → JPEG)
```

### Output

- **Single file:** `<reduction%>  <before> → <after>   <filename>`
- **Batch mode:** Unicode-bordered table (Saved / Before / After / File) + summary line
- Colors: green = saved, dim = no change, orange = grew

### Dependencies

Install the format-specific binaries you use. Missing dependencies produce a
platform-specific installation hint. For the complete macOS set:

```sh
brew install pngquant oxipng jpegoptim webp svgo gifsicle
npm install -g sharp-cli
# sips is built into macOS — no install needed
```

---

## `resize-image` — Image Resize

Resize images using ImageMagick (`magick`). Output filename gets a size suffix
(e.g. `img1_43x42.jpg`); original extension is preserved.

### resize-image Usage

```sh
resize-image [-r] [-f] <width> <height> [files...]
```

| Flag | Description |
| --- | --- |
| `-r` | Recurse into sub-folders |
| `-f` | Force resize ignoring aspect ratio |

### resize-image Examples

```sh
# 1. Single file
resize-image 43 42 img1.jpg

# 2. Multiple files
resize-image 100 100 img1.jpg img2.png photo.webp

# 3. All images in current folder
resize-image 50 50

# 4. Recursive resize in folder tree
resize-image -r 50 50

# 5. Recursive + specific filenames
resize-image -r 50 50 logo.png banner.jpg

# 6. Force resize ignoring aspect ratio
resize-image -f 100 200 img1.jpg

# 7. Recursive force resize
resize-image -r -f 100 200
```

---

## `towebp` — Convert Images to WebP

Convert PNG / JPG / JPEG files to WebP using `cwebp` (lossy, default `-q 75`).
Original files are **deleted** after a successful conversion.

### towebp Options

| Flag | Description |
| --- | --- |
| `-c` | Process only the current folder (default: recurse into subfolders) |
| `-q <quality>` | Compression quality `0–100` (default: `75`) |

### towebp Usage

```sh
towebp           # convert all PNG/JPG/JPEG recursively (default quality: 75)
towebp -c        # current folder only (no sub-folder recursion)
towebp -q 80     # convert recursively with 80% compression quality
towebp -c -q 90  # current folder only with 90% quality
```

---

## `html2md` — HTML → Markdown Converter

Convert `.html` files to `.md` via `pandoc`.

### html2md Usage

```sh
html2md                        # convert all .html in the current directory
html2md "API Reference.html"   # convert a specific .html file
html2md "API Reference.md"     # accepts .md extension — auto-maps to .html source
```

---

## `vcadd` — vChewing User Dictionary Helper

Append one or more Chinese words (with their auto-generated 注音符號（Bopomofo）readings) to the
vChewing input method's user phrase file (`userdata-cht.txt`), then trigger a live reload
so the new entries take effect immediately — no manual restart required.

**Platform:** macOS only (requires vChewing and `osascript`).

### vcadd Usage

```sh
vcadd <word> [word ...]
```

### vcadd Examples

```sh
vcadd 蛋白質         # add a single word
vcadd 人工智慧 機器學習  # add multiple words at once
```

- Duplicate entries are detected and skipped automatically.
- Each added entry is printed in `word BopomofoReading` format (e.g. `蛋白質 ㄉㄢˋ-ㄅㄞˊ-ㄓˊ`).
- vChewing reloads the user phrase file automatically via FSEvents — no manual reload or restart required.

### vcadd Dependencies

| Dependency | Notes |
| --- | --- |
| `pypinyin` | Python package — installed automatically with ai-accounts |
| vChewing | Must be installed and running |

---

## Account profile storage

All five account managers (`codex-accounts`, `claude-accounts`, `agy-accounts`, `grok-accounts`, `vibe-accounts`)
keep their saved profiles in one central, hidden folder under your **home
directory**:

```text
$HOME/
└── .ai-accounts/
    ├── claude/accounts/          # claude-accounts profiles + .current-profile
    ├── codex/accounts/           # codex-accounts profiles + .current-profile
    ├── antigravity/accounts/     # agy-accounts profiles + .current-profile
    ├── grok/accounts/            # grok-accounts profiles + .current-profile
    └── vibe/accounts/            # vibe-accounts profiles + .current-profile
```

The path is resolved from the running user's home directory at runtime
(`~/.ai-accounts` on macOS/Linux, `C:\Users\<name>\.ai-accounts` on Windows), so it
works the same on all three platforms and is completely independent of where
this repository or the installed tools live — moving or reinstalling ai-accounts
never touches the stored profiles.

The location is deliberately **outside** the app dotdirs (`~/.claude`,
`~/.codex`, `~/.grok`, and the old `~/.codexbar`): if you version-control a
dotdir as a dotfiles repo, the OAuth token snapshots that profiles contain can
never end up in a commit. Legacy stores (`~/.claude/accounts`,
`~/.codex/accounts`, `~/.grok/accounts`, and `~/.codexbar/antigravity`) move
to the central location automatically the first time their command runs, with
a one-line notice. The `accounts/.current-profile` marker moves with them.

Treat `~/.ai-accounts` as secrets — every `<name>.json` profile holds live auth
tokens. Each tool's location can be overridden individually via
`CODEX_ACCOUNT_DIR` / `CLAUDE_ACCOUNT_DIR` / `ANTIGRAVITY_ACCOUNT_DIR` /
`GROK_ACCOUNT_DIR` (see
each tool's Environment Overrides section).

---

## `codex-accounts` — Codex CLI Account Manager

Save, list, and switch between multiple [Codex CLI](https://github.com/openai/codex) login
profiles. Never prints raw tokens — only decoded, non-secret claims (email, name, plan,
account ID, org ID, expiry). Saved profiles under `~/.ai-accounts/codex/accounts/` contain auth
tokens — treat that directory as secrets.

> **Scope**: this tool only manages `~/.codex/auth.json` — it does **not** touch OpenAI API
> keys or any other credential store. When Codex is configured with `auth_mode: chatgpt`
> (the default login flow), the stored tokens are issued via ChatGPT OAuth. In that case,
> `login-switch` (which calls `codex logout`) will revoke the active ChatGPT OAuth session;
> switching profiles with `switch` does **not** hit the network and is safe to use at any time.
> `refresh` uses the OAuth refresh grant (the same call Codex makes internally) — it renews
> tokens **without** logging out, so it never revokes the ChatGPT session and never opens a
> browser.

### codex-accounts Usage

```sh
codex-accounts who                   # show the current logged-in account
codex-accounts current               # alias for `who`
codex-accounts save [<name>]         # save the current login as a reusable profile;
                                     # no name = derive from the active account's email
codex-accounts list                  # list saved profiles (table view)
codex-accounts usage                 # show only the active account's usage row
codex-accounts switch <name>         # switch to a saved profile
codex-accounts remove [<name>]       # delete a saved profile; no name = interactive picker
codex-accounts refresh [<name>]      # renew tokens via OAuth refresh (no browser, no logout);
                                     # no name = refresh the active auth + sync it back
codex-accounts refresh --all         # renew every saved profile in one run
codex-accounts sync                  # copy the active auth back to its matching profile
codex-accounts login-switch <name>   # codex logout + codex login + save as <name>
codex-accounts autoswitch            # switch away from the active profile if it's low on
                                     # quota (see "Auto-switch on low quota" below)
codex-accounts config                # interactive auto-switch config menu (see below)
codex-accounts config get [key]      # print the auto-switch config (or just one key)
codex-accounts config set <key> <val># set one auto-switch config key
codex-accounts -h | --help | help    # show this help
```

### codex-accounts First-time setup

**Prerequisite** — the Codex CLI must be installed:

```sh
which codex || npm install -g @openai/codex
```

**Single account** — log in once, save the profile:

```sh
codex login                       # opens browser for authentication
codex-accounts save personal      # save the active auth as "personal"
codex-accounts who                # confirm which account is active
```

**Multiple accounts** (e.g. personal + work) — use `login-switch`, which runs
`codex logout` → `codex login` → `save` in one step:

```sh
codex-accounts login-switch personal   # log into the first account, save as "personal"
codex-accounts login-switch work       # log into the second account, save as "work"
codex-accounts list                    # verify both profiles are saved
```

After the initial setup, switch at any time with:

```sh
codex-accounts switch personal   # activate the "personal" profile
codex-accounts switch work       # activate the "work" profile
codex-accounts who               # confirm the current account
```

### codex-accounts Token upkeep

Saved profiles are snapshots — Codex keeps refreshing the *active* `auth.json` while you use
it (access tokens last ~10 days), but the copies under `~/.codex/accounts/` go stale. Two
commands keep them fresh, no re-login required:

```sh
codex-accounts refresh --all     # renew every saved profile via OAuth refresh
codex-accounts refresh work      # renew just one profile
codex-accounts refresh           # renew the active auth, then sync it back to its profile
codex-accounts sync              # no network: copy the (already-fresh) active auth back
                                 # to its matching profile
```

When a refreshed profile belongs to the currently active account, `refresh` also updates
`auth.json` — OAuth refresh rotates the refresh token, so this keeps the live login from
being stranded with a dead token. If a refresh fails because the refresh token itself has
expired or been revoked, re-login with `codex-accounts login-switch <name>`.

### codex-accounts Examples

```sh
codex-accounts login-switch personal   # log into a new account, save it as "personal"
codex-accounts login-switch work       # log into another account, save it as "work"
codex-accounts list                    # see all saved profiles, with the active one marked
codex-accounts switch personal         # switch back to "personal"
codex-accounts refresh --all           # renew tokens for every saved profile
codex-accounts who                     # confirm which account is currently active
```

### codex-accounts Output

- `list` renders a bordered table with plan tier, usage, refresh time, auth expiry, and active state:

```text
❯ codex-accounts list
Saved Codex profiles  (2)
┌──────────┬──────────────────────────────────┬──────┬───────────────┬─────────┬────────────────────┬─────────┬──────────────┬────────┐
│ PROFILE  │ ACCOUNT                          │ PLAN │ ID            │ 5H USED │ 1W USED            │ UPDATED │ AUTH         │ STATE  │
├──────────┼──────────────────────────────────┼──────┼───────────────┼─────────┼────────────────────┼─────────┼──────────────┼────────┤
│ personal │ Alex Example <alex@example.test> │ Pro  │ 71b55315…61bc │ —       │  97% ·  2d  5h 27m │ 11:39   │ Jul 26 04:44 │ —      │
│ work     │ Casey Demo <casey@example.test>  │ Plus │ 4847e557…c28d │ —       │  18% ·  6d  6h 59m │ 11:39   │ Jul 26 04:44 │ ACTIVE │
└──────────┴──────────────────────────────────┴──────┴───────────────┴─────────┴────────────────────┴─────────┴──────────────┴────────┘
```

- `who` and `switch` render a bordered "Current Auth Claims" panel; expiry is color-coded
  (green = valid, yellow = expiring within 24h, red = `EXPIRED`) with the state also spelled
  out in text, not color alone.
- `switch` backs up the previous `auth.json` (timestamped, `chmod 600`) before overwriting it.
- `list` shows a spinner (with a live "which profile" label) on a TTY while it fetches usage
  for each profile; it's automatically skipped when output isn't a terminal (piping, `ai-accounts`).

### codex-accounts Config

`codex-accounts config` with no arguments opens an interactive arrow-key menu on a
TTY (↑↓ move, ⏎ edit — boolean/enum fields cycle their allowed values instead of
typing, Esc cancels an edit, `r` resets every setting after a `y` confirmation,
`q` quits; every change saves itself, so there is no save key). Piped/non-TTY input (scripts, CI)
prints a numbered listing instead and exits `0`. `config get [key]` / `config set
<key> <value>` remain the scriptable form; `set telegram_bot_token` echoes the
saved value masked, not in cleartext. This is the same shared config menu as
`ai-accounts`, `claude-accounts`, `agy-accounts`, `grok-accounts`, and `vibe-accounts` — all six
edit the one `~/.ai-accounts/config.json`. See [Auto-switch on low quota →
Config](#config) for the full key reference.

### codex-accounts Environment Overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODEX_HOME` | `~/.codex` | Base Codex config directory |
| `CODEX_AUTH_JSON` | `$CODEX_HOME/auth.json` | Active Codex auth file |
| `CODEX_ACCOUNT_DIR` | `~/.ai-accounts/codex/accounts` | Where saved profiles are stored |

---

## `claude-accounts` — Claude Code Account Manager

Save, list, and switch between multiple [Claude Code](https://claude.com/claude-code) login
profiles. Never prints raw tokens — only non-secret claims (plan tier with rate multiplier,
scopes, expiry). Works on macOS, Windows, and Linux; on macOS the login-keychain mirror that
Claude Code reads is kept in step automatically.

Claude's OAuth token is opaque (no email/name inside), so profiles are told apart by the
plan tier (e.g. `Team · 5x`, `Max · 20x`) and a short token fingerprint instead of an
account identity.

### claude-accounts Usage

```sh
claude-accounts who                   # show the current logged-in account
claude-accounts current               # alias for `who`
claude-accounts save [<name>]         # save the current login as a reusable profile;
                                       # no name = derive one from the active account's email
claude-accounts list                  # list saved profiles with 5h/1w usage (table view)
claude-accounts usage                 # show only the active account's usage row
claude-accounts switch [<name>]       # switch by name; no name = interactive picker
claude-accounts remove [<name>]       # delete by name; no name = interactive picker
claude-accounts refresh [<name>]      # renew tokens via OAuth refresh (no browser, no logout)
claude-accounts refresh --all         # renew every saved profile in one run
claude-accounts sync                  # copy the active auth back to its matching profile
claude-accounts login-switch <name>   # `claude auth login` + save as <name>
claude-accounts autoswitch            # switch away from the active profile if it's low on
                                       # quota (see "Auto-switch on low quota" below)
claude-accounts config                # interactive auto-switch config menu (see below)
claude-accounts config get [key]      # print the auto-switch config (or just one key)
claude-accounts config set <key> <val># set one auto-switch config key
claude-accounts -h | --help | help    # show this help
```

### claude-accounts First-time setup

```sh
claude-accounts login-switch personal   # log into the first account, save as "personal"
claude-accounts login-switch work       # log into the second account, save as "work"
claude-accounts list                    # verify both profiles are saved
claude-accounts switch personal         # jump back to the first account
```

### claude-accounts Output

- `list` renders a bordered table with plan tier (and rate multiplier), 5h/1w usage,
  refresh time, token expiry, and active state:

```text
❯ claude-accounts list
Saved Claude profiles  (2)
┌──────────┬──────────────────────────────────┬───────────┬──────────────┬──────────────────┬─────────┬─────────────┬────────┐
│ PROFILE  │ ACCOUNT                          │ PLAN      │ 5H USED      │ 1W USED          │ UPDATED │ EXPIRES     │ STATE  │
├──────────┼──────────────────────────────────┼───────────┼──────────────┼──────────────────┼─────────┼─────────────┼────────┤
│ personal │ Wes Kao <wes.personal@gmail.com> │ Team · 5x │  4% · 3h 59m │ 22% · 4d 23h 59m │ 12:56   │ refreshable │ —      │
│ work     │ Wes Kao <wes@acme.com>           │ Max · 20x │ 61% · 1h 12m │ 48% · 3d  6h  3m │ 12:56   │ refreshable │ ACTIVE │
└──────────┴──────────────────────────────────┴───────────┴──────────────┴──────────────────┴─────────┴─────────────┴────────┘
```

- The `ACCOUNT` column shows the profile's email/name. Claude's OAuth token carries no
  identity, so it is snapshotted from `~/.claude.json` when you `save` (or `login-switch`)
  that account — the one moment it provably matches. Profiles saved before this existed
  show `—` until their next `save`; the column is hidden entirely when no profile has one.
- The `EXPIRES` column shows `refreshable` (green) when the profile carries a refresh
  token — the short-lived access-token expiry is renewed automatically, so it is not a
  concern. Only a profile without a usable refresh token shows a raw expiry time,
  color-coded (green = valid, yellow = expiring within 24h, red = `EXPIRED`).
- `who` and `switch` render a bordered "Current Auth Claims" panel; expiry is shown the
  same way, with the state also spelled out in text, not color alone. When the stored
  credential carries a `refreshTokenExpiresAt` claim, the panel adds a `Refreshable
  until` line — the date the account actually stops working, distinct from the short-lived
  access token above it; it reads red once that date has passed.
- `switch` backs up the previous credentials (timestamped, `chmod 600`) before overwriting.
- `list` shows a spinner (with a live "which profile" label) on a TTY while it fetches usage
  for each profile; it's automatically skipped when output isn't a terminal (piping, `ai-accounts`).

### claude-accounts Config

`claude-accounts config` with no arguments opens an interactive arrow-key menu on a
TTY (↑↓ move, ⏎ edit — boolean/enum fields cycle their allowed values instead of
typing, Esc cancels an edit, `r` resets every setting after a `y` confirmation,
`q` quits; every change saves itself, so there is no save key). Piped/non-TTY input (scripts, CI)
prints a numbered listing instead and exits `0`. `config get [key]` / `config set
<key> <value>` remain the scriptable form; `set telegram_bot_token` echoes the
saved value masked, not in cleartext. This is the same shared config menu as
`ai-accounts`, `codex-accounts`, `agy-accounts`, `grok-accounts`, and `vibe-accounts` — all six
edit the one `~/.ai-accounts/config.json`. See [Auto-switch on low quota →
Config](#config) for the full key reference.

### claude-accounts Environment Overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Base Claude Code config directory |
| `CLAUDE_CREDENTIALS_JSON` | `$CLAUDE_CONFIG_DIR/.credentials.json` | Active Claude credentials file |
| `CLAUDE_ACCOUNT_DIR` | `~/.ai-accounts/claude/accounts` | Where saved profiles are stored |

---

## `agy-accounts` — Antigravity Account Manager

**Platform:** macOS, Windows, and Linux. The official `agy` session lives in the
OS credential store, which differs per platform — macOS Keychain, Windows
Credential Manager, or the Linux Secret Service (needs `secret-tool` from
`libsecret`; without it every command except `config` reports the missing binary
and exits).

Save, list, and switch between multiple sessions for the official Antigravity CLI (`agy`). It
never requires a `GEMINI_API_KEY`, embeds no OAuth client credentials, and never prints raw
tokens. The active session is stored in the OS credential store by `agy`; reusable profile
snapshots live in ai-accounts's own `~/.ai-accounts/antigravity/` store.

`list` temporarily activates each profile, asks `agy` for the same quota data used by `/usage`,
and restores the original session. It shows plan, Gemini weekly/5-hour use, Claude/GPT
weekly/5-hour use, refresh time, and active state.

Unlike `codex-accounts list` / `claude-accounts list`, this fetch runs strictly
one profile at a time — not a missed optimization but a hard constraint of how
`agy` reads its session from a single shared credential-store entry. See
[`docs/agy-parallel-limitation.md`](docs/agy-parallel-limitation.md) for why.

### agy-accounts Usage

```sh
agy-accounts who                   # show the selected Antigravity account
agy-accounts current               # alias for `who`
agy-accounts save [<name>]         # save the current login as a reusable profile;
                                   # no name = derive from the active account's email
agy-accounts list                  # list profiles with agy model-family quota
agy-accounts usage                 # show only the active account's quota row
agy-accounts switch [<name>]       # switch by name; no name = interactive picker
agy-accounts remove [<name>]       # delete a saved profile; no name = interactive picker
agy-accounts refresh [<name>]      # renew tokens via the Google OAuth refresh grant
                                   #   (no browser, no agy launch); falls back to agy
agy-accounts refresh --all         # renew every saved profile in one run
agy-accounts sync                  # copy the active session to its profile
agy-accounts login-switch <name>   # official agy browser login + save as <name>
agy-accounts autoswitch            # leave the active account when its quota runs out
                                   # (opt-in blind mode — see "Auto-switch on low quota")
agy-accounts config                # interactive auto-switch config menu (see below)
agy-accounts config get [key]      # print the auto-switch config (or just one key)
agy-accounts config set <key> <val># set one auto-switch config key
agy-accounts -h | --help | help    # show this help
```

### agy-accounts First-time setup

**Prerequisite** — install the official Antigravity CLI and confirm it is available:

```sh
agy --version
```

Then add one or more accounts. `login-switch` launches `agy` itself; finish its browser login,
then exit the CLI with Ctrl+D twice so the new session can be saved:

```sh
agy-accounts login-switch personal   # log into the first account, save as "personal"
agy-accounts login-switch work       # log into the second account, save as "work"
agy-accounts list                    # verify both profiles are saved
```

The agy session token carries no account identity, so `save` and `login-switch` fetch the
email from agy's usage service and store it in the profile (a brief "Fetching account identity…"
step). If that fetch is momentarily unavailable, the account shows as `(unknown)` until the next
`list` or `refresh` backfills it.

After the initial setup, switch at any time with:

```sh
agy-accounts switch personal   # activate the "personal" profile
agy-accounts switch work       # activate the "work" profile
agy-accounts who               # confirm the current account
```

### agy-accounts Session upkeep

The roughly one-hour expiry is for the access token, not the login. Saved sessions include a
long-lived refresh token, so an "expired" profile is renewable without a browser:

```sh
agy-accounts refresh --all     # renew every saved profile
agy-accounts refresh work      # renew one profile and save the new token
agy-accounts refresh           # renew the active session and sync it back
agy-accounts sync              # copy the current session to its profile
```

`refresh` posts the OAuth refresh grant straight to Google
(`https://oauth2.googleapis.com/token`) — one HTTPS request, no `agy` process, fast enough
to run from a timer. Google does not rotate the refresh token, so only the access token and
its expiry are rewritten, into both the profile and (for the active account) the credential store
session. The client id/secret come from `ANTIGRAVITY_OAUTH_CLIENT_ID` /
`ANTIGRAVITY_OAUTH_CLIENT_SECRET` if set, otherwise from the installed `Antigravity.app`
bundle; when neither resolves — or when Google is unreachable — `refresh` falls back to the
old path of letting `agy` renew the session and reading the result back.

`switch` self-heals too: activating a profile whose access token is expired (or within five
minutes of it) refreshes it in place first, so `agy` never starts on a dead token.

If a refresh token is genuinely revoked (Google answers `invalid_grant`), re-login with
`agy-accounts login-switch <name>`. Network errors and server hiccups are reported as
transient and never trigger a re-login.

### agy-accounts Examples

```sh
agy-accounts login-switch personal   # log into a new account, save it as "personal"
agy-accounts login-switch work       # log into another account, save it as "work"
agy-accounts list                    # see all saved profiles, with the active one marked
agy-accounts switch personal         # switch back to "personal"
agy-accounts refresh --all           # renew tokens for every saved profile
agy-accounts who                     # confirm which account is currently active
```

### agy-accounts Output

- `list` temporarily activates each profile, queries `agy` for quota, and restores the original session:

```text
❯ agy-accounts list
Saved Antigravity profiles  (5)
┌─────────────┬──────────────────────────┬──────┬───────────────────┬────────────────────┬─────────┬─────────────┬────────┐
│ PROFILE     │ ACCOUNT                  │ PLAN │ GEMINI 1W USED    │ CLAUDE/GPT 1W USED │ UPDATED │ SESSION     │ STATE  │
├─────────────┼──────────────────────────┼──────┼───────────────────┼────────────────────┼─────────┼─────────────┼────────┤
│ personal    │ alex.nova@gmail.com      │ Pro  │   3% · 6d 18h 12m │   0% · 6d 23h 59m  │ 02:14   │ refreshable │ ACTIVE │
│ work        │ casey.demo@gmail.com     │ Pro  │  52% · 4d  2h 37m │  38% · 5d  9h 22m  │ 02:14   │ browser     │ —      │
│ side        │ jordan.test@gmail.com    │ Pro  │ 100% · 1d 14h 55m │  71% · 3d 17h 48m  │ 02:14   │ refreshable │ —      │
│ research    │ morgan.example@gmail.com │ Free │  29% · 5d  7h 19m │   0% · 6d 23h 59m  │ 02:14   │ api-key     │ —      │
│ backup      │ riley.sample@gmail.com   │ Pro  │   0% · 6d 23h 59m │   0% · 6d 23h 59m  │ 02:14   │ expired     │ —      │
└─────────────┴──────────────────────────┴──────┴───────────────────┴────────────────────┴─────────┴─────────────┴────────┘
```

The `PLAN` column shows the real subscription tier from `agy`'s `userTier`
(`Free` for the Antigravity free preview, or the paid tier's name such as
`Google AI Pro`) — not the `Pro` feature label that the free preview reports
for every account.

Session types:
- `refreshable` — valid refresh token; `agy` renews the access token automatically
- `browser` — session was restored from a browser login snapshot (no refresh token stored)
- `api-key` — profile uses a Gemini API key instead of an OAuth session
- `expired` — refresh token has expired; re-login required (`agy-accounts login-switch <name>`)
- `malformed` — credential file parses as JSON but doesn't look like an Antigravity/Google
  OAuth record (e.g. one misfiled from another provider)

- `who` and `switch` render a bordered "Current Auth Claims" panel. Its expiry line is
  refreshable-first, same as the `list` table above: `refreshable` (green) whenever the
  profile carries a refresh token, otherwise the raw access-token expiry color-coded
  (green = valid, yellow = expiring within 24 h, red = `EXPIRED`). A malformed credential
  file replaces the entire panel body with a single yellow "Malformed credential file"
  line instead of the usual Account/Google ID/Workspace/Issuer/Expires rows.
- `switch` backs up the previous session (timestamped, `chmod 600`) before overwriting it.
- `list` shows a spinner (with a live "which profile" label) on a TTY while it queries `agy`
  for each profile; it's automatically skipped when output isn't a terminal (piping, `ai-accounts`).

### agy-accounts Config

`agy-accounts config` with no arguments opens an interactive arrow-key menu on a
TTY (↑↓ move, ⏎ edit — boolean/enum fields cycle their allowed values instead of
typing, Esc cancels an edit, `r` resets every setting after a `y` confirmation,
`q` quits; every change saves itself, so there is no save key). Piped/non-TTY input (scripts, CI)
prints a numbered listing instead and exits `0`. `config get [key]` / `config set
<key> <value>` remain the scriptable form; `set telegram_bot_token` echoes the
saved value masked, not in cleartext. This is the same shared config menu as
`ai-accounts`, `codex-accounts`, `claude-accounts`, `grok-accounts`, and `vibe-accounts` — all six
edit the one `~/.ai-accounts/config.json`, including `agy_blind_switch`, the setting
this tool's own autoswitch reads. See [Auto-switch on low quota →
Config](#config) for the full key reference.

### agy-accounts Environment Overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTIGRAVITY_HOME` | `~/.ai-accounts/antigravity` | Profile and credential-mirror root |
| `ANTIGRAVITY_OAUTH_JSON` | `$ANTIGRAVITY_HOME/oauth_creds.json` | Active credential-store session mirror |
| `ANTIGRAVITY_ACCOUNT_DIR` | `$ANTIGRAVITY_HOME/accounts` | Saved profiles |
| `ANTIGRAVITY_OAUTH_CLIENT_ID` | discovered from `Antigravity.app` | OAuth client id used by `refresh` |
| `ANTIGRAVITY_OAUTH_CLIENT_SECRET` | discovered from `Antigravity.app` | OAuth client secret used by `refresh` |
| `ANTIGRAVITY_CLI_PATH` | resolved from `PATH` | Override the `agy` executable used for quota checks |

---

## `grok-accounts` — Grok Build Account Manager

Save, list, and switch between multiple [Grok Build CLI](https://x.ai/cli)
OAuth profiles. It manages only Grok Build's `~/.grok/auth.json`; saved profile
snapshots live under `~/.ai-accounts/grok/accounts/`, outside the Grok config
directory. Tokens are never printed.

### grok-accounts Usage

```sh
grok-accounts who                   # show the current logged-in Grok account
grok-accounts current               # alias for `who`
grok-accounts save [<name>]         # save the current login as a reusable profile;
                                    # no name = derive from the active account's email
grok-accounts list                  # list saved profiles
grok-accounts usage                 # show only the active account (session & expiry)
grok-accounts switch [<name>]       # switch by name; no name = interactive picker
grok-accounts remove [<name>]       # delete a saved profile; no name = interactive picker
grok-accounts refresh [<name>]      # renew the active/named session's access token
grok-accounts refresh --all         # renew every saved profile's access token
grok-accounts sync                  # copy the active auth back to its matching profile
grok-accounts login-switch <name>   # fresh browser login + save as <name>
grok-accounts autoswitch            # reports that grok has no quota API to switch on
                                    # (always exits 0 — see "Auto-switch on low quota")
grok-accounts config                # interactive auto-switch config menu (see below)
grok-accounts config get [key]      # print the auto-switch config (or just one key)
grok-accounts config set <key> <val># set one auto-switch config key
grok-accounts -h | --help | help    # show this help
```

### grok-accounts First-time setup

Install the official Grok Build CLI, log in, then save the profile:

```sh
curl -fsSL https://x.ai/cli/install.sh | bash
grok login --oauth
grok-accounts save personal
grok-accounts who
```

For another account, use `grok-accounts login-switch work`; then switch at any
time with `grok-accounts switch personal` or `grok-accounts switch work`.

`refresh` performs a standard OIDC refresh grant — one HTTPS POST, no
subprocess. Nothing is hardcoded: the token endpoint is discovered at runtime
from `<oidc_issuer>/.well-known/openid-configuration`, using the issuer and
client id the credential itself carries. The grant is attempted as a public
client (no client secret); if x.ai rejects it because client authentication is
required, ai-accounts falls back to running `grok models` under the selected
session so Grok Build rotates its own credentials — the original active session
is restored afterwards. A rejected *refresh token* (`invalid_grant`) is not
retried through the CLI: only `grok-accounts login-switch <name>` fixes that.

`switch` self-heals: if the restored access token is expired or within 5 minutes
of expiring, it is refreshed in place and the rotation mirrored back into the
profile, so the Grok CLI is never handed a dead token.

### grok-accounts Output

- `grok-accounts -h` displays current Grok model and pricing reference: grok-4.5 (500k context, agentic tool calling), API pricing, and consumer plan tiers (Free, SuperGrok).
- `list` renders a bordered table with account, principal type/ID, team, created timestamp,
  refresh-token health, data-retention setting, session type, and active state:

```text
❯ grok-accounts list
Saved Grok profiles  (2)
┌──────────┬──────────────────────────────────┬──────┬───────────────┬───────────┬──────────────┬─────────────┬──────────┬─────────────────┬────────┐
│ PROFILE  │ ACCOUNT                          │ TYPE │ ID            │ TEAM      │ CREATED      │ EXPIRES     │ DATA     │ SESSION         │ STATE  │
├──────────┼──────────────────────────────────┼──────┼───────────────┼───────────┼──────────────┼─────────────┼──────────┼─────────────────┼────────┤
│ demo-one │ Casey Demo <casey.demo@x.ai>     │ User │ principa…0abc │ team-91d2 │ May 15 18:00 │ refreshable │ standard │ OAUTH · refresh │ ACTIVE │
│ demo-two │ Alex Example <alex.example@x.ai> │ User │ principa…9c21 │ team-4b7e │ Jun 01 11:04 │ refreshable │ opt-out  │ OIDC · refresh  │ —      │
└──────────┴──────────────────────────────────┴──────┴───────────────┴───────────┴──────────────┴─────────────┴──────────┴─────────────────┴────────┘
```

  `EXPIRES` is refreshable-first: `refreshable` (green) whenever the profile carries a
  refresh token — x.ai's 1-hour access token is renewed automatically and isn't worth
  alarming over. Only a profile with no refresh token shows the raw access-token expiry,
  color-coded (green = valid, yellow = expiring within 24 h, red = `EXPIRED`). A credential
  file that parses but doesn't match Grok's OAuth record shape (e.g. one misfiled from
  another provider) shows `malformed` (yellow) instead.

- `who` and `switch` render two bordered cyan panels — a "Grok Login Status" panel and a "Current Auth Claims" panel — matching the layout and accent color of `codex-accounts`, `claude-accounts`, and `agy-accounts`.
- `save` and `sync` also print a bordered green "Profile: <name>" claims panel after the
  ✅ confirmation line, same as the other three account tools; `switch` follows its
  ✅ line with the `who` panels instead.
- `refresh <name>` prints the same green success line and Profile panel as the other
  account tools. `refresh --all` prints each refreshed profile, then the saved-profile
  table and a final `✅ All N profile(s) refreshed.` summary.
- `switch` without a `<name>` argument opens an interactive picker ("Choose a Grok profile:",
  numbered `1)`, `2)`, …) — the same picker grammar as `codex-accounts`, `claude-accounts`,
  and `agy-accounts`.

### grok-accounts Config

`grok-accounts config` with no arguments opens an interactive arrow-key menu on a
TTY (↑↓ move, ⏎ edit — boolean/enum fields cycle their allowed values instead of
typing, Esc cancels an edit, `r` resets every setting after a `y` confirmation,
`q` quits; every change saves itself, so there is no save key). Piped/non-TTY input (scripts, CI)
prints a numbered listing instead and exits `0`. `config get [key]` / `config set
<key> <value>` remain the scriptable form; `set telegram_bot_token` echoes the
saved value masked, not in cleartext. This is the same shared config menu as
`ai-accounts`, `codex-accounts`, `claude-accounts`, `agy-accounts`, and `vibe-accounts` — all six
edit the one `~/.ai-accounts/config.json`. See [Auto-switch on low quota →
Config](#config) for the full key reference.

### grok-accounts Environment Overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `GROK_HOME` | `~/.grok` | Base Grok Build config directory |
| `GROK_AUTH_JSON` | `$GROK_HOME/auth.json` | Active Grok Build OAuth file |
| `GROK_ACCOUNT_DIR` | `~/.ai-accounts/grok/accounts` | Where saved profiles are stored |

**Residual note:** `grok-accounts`' credential writes go through its own local
`_write_json` (tempfile + `os.replace`), not the shared `_utils.atomic_write_json`
the other three tools use. This is a deliberate, already-reviewed choice, not an
oversight — both give the same atomic-write guarantee, they just aren't literally
the same function, which is worth a maintainer knowing before assuming a change to
one affects the other.

---

## `vibe-accounts` — Mistral Vibe Account Manager

Save, list, and switch between multiple [Mistral Vibe](https://mistral.ai/products/vibe/)
profiles. Vibe uses a static API key; profiles are stored under
`~/.ai-accounts/vibe/accounts/` and never print the raw key.

```sh
vibe-accounts save [<name>]       # save the active Vibe key; no name = derive from the key
vibe-accounts list                # list saved profiles
vibe-accounts switch <name>       # activate a saved profile
vibe-accounts who                 # show the active profile and which store holds it
vibe-accounts sync                # update the active profile
vibe-accounts login-switch <name> # run `vibe --setup`, then save it
```

**Where the live key lives.** Vibe stores it in the OS keyring — on macOS the
login keychain, service `ai.mistral.vibe`, account = the provider's env-var
name — and *deletes* the plaintext `$VIBE_HOME/.env` copy once that write
succeeds. It only falls back to `.env` when no keyring is available. These
commands read and write whichever store Vibe itself would use, in Vibe's own
precedence order (`.env` outranks the keyring, because Vibe loads `.env` into
the process environment at startup). A `switch` that lands in the keyring also
clears any stale `.env` key, which would otherwise silently shadow it.

`who` prints the store it found the key in, which is the quickest way to tell a
keyring-backed login from an `.env` one.

`MISTRAL_API_KEY` is preferred; `OPENAI_API_KEY` is also recognized for
OpenAI-compatible Vibe configurations. Vibe's account API reports only a plan,
never an email, so `save` with no name derives one from a digest of the key
(`vibe-<8 hex>`) rather than an address. Vibe has no quota API, so `refresh`
is a successful no-op and `autoswitch` reports that it is unsupported.

### vibe-accounts Environment Overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `VIBE_HOME` | `~/.vibe` | Base Vibe configuration directory |
| `VIBE_ACCOUNT_DIR` | `~/.ai-accounts/vibe/accounts` | Where saved profiles are stored |

---

## `ai-accounts` — All-provider Account Front-end

Forwards a subcommand to all five per-provider tools (`codex-accounts`,
`claude-accounts`, `agy-accounts`, `grok-accounts`, `vibe-accounts`) at once, so one command drives every
provider. It exposes the same command surface as the per-provider tools.
All commands use the same provider headers and shared success, warning, error,
panel, and table presentation as the individual account tools; provider-specific
claims and usage columns remain native to each provider.

### ai-accounts Usage

```text
ai-accounts                        Show this help (the available commands)
ai-accounts list                   List all provider profiles (providers run in parallel)
ai-accounts who | current          Show the active account for every provider
ai-accounts usage                  Show only the active account's usage row per provider
ai-accounts refresh [<name>|--all] Refresh tokens across every provider
ai-accounts sync                   Sync active auth back to its profile, every provider
ai-accounts save [<name>]          Save the current login in every provider;
                                    no name = each provider derives its own name from
                                    its own active account's email (may differ per
                                    provider if they're logged into different accounts —
                                    intentional forwarding behavior, not a bug)
ai-accounts switch [<name>]        Switch profile in every provider (interactive, one at a time)
ai-accounts remove [<name>]        Remove profile in every provider; no name = interactive
                                    picker for each provider in turn
ai-accounts login-switch <name>    Fresh login + save as <name>, every provider (interactive)
ai-accounts autoswitch             Run the low-quota auto-switch check for every provider now
ai-accounts autoswitch setup       One-time install of event hooks and timer fallback
ai-accounts config                 Interactive auto-switch config menu (numbered fallback
                                    when not run on a TTY) — see "Auto-switch on low
                                    quota" below
ai-accounts config get [key]       Print the auto-switch config (or just one key); the
                                    telegram bot token is always masked
ai-accounts config set <key> <val> Set one auto-switch config key (rejects unknown keys)
ai-accounts install-timer [--interval N]
                                    Schedule the auto-switch check and the token-refresh
                                    check with the OS (default: every 1800s / 30 minutes)
ai-accounts uninstall-timer        Remove the scheduled checks
ai-accounts timer-status           Report whether the scheduled checks are installed
ai-accounts -h | --help | help     Show this help
```

`autoswitch`, `config` (with or without `get`/`set`), `install-timer`,
`uninstall-timer` and `timer-status` are handled by `ai-accounts` itself rather
than forwarded to a provider — `config` is not unique to `ai-accounts`, though:
`codex-accounts`, `claude-accounts`, `agy-accounts`, and `grok-accounts` each own
the identical `config` command against the same shared `~/.ai-accounts/config.json`.
See [Auto-switch on low quota](#auto-switch-on-low-quota) below for the full
config reference, the support matrix, and the timer's per-OS mechanism.

Bare `ai-accounts` (no arguments) prints this help. `list` runs the five
providers **concurrently** and prints each one's table as soon as it
finishes — fastest provider first, not a fixed order — with a spinner in
between tracking how many are still outstanding (`Fetching remaining 3
providers…`, then `2`, `1`, …) until the last table lands and the spinner
disappears for good. Every other command runs the providers **one at a time
with live output**, so interactive flows (switch pickers, `login-switch`) and
color work unchanged; any argument after the command (a profile name,
`--all`, …) is passed through to each provider — except after `autoswitch`,
which accepts only `setup` and rejects every other argument (exit `1`) instead
of forwarding an argument all five providers would ignore. `ai-accounts
autoswitch install-timer` is therefore an error naming `ai-accounts autoswitch
setup`, rather than a silent no-op that installs nothing. Per-provider errors are
printed inline without aborting the others, and the exit code is non-zero if
any provider's command failed. `list`'s spinner only shows on a TTY (their
own inner spinners stay off, since their output is captured rather than run
on a live terminal).

---

## Auto-switch on low quota

Quota-backed account tools can watch their active account's quota and switch to a
saved profile with more room — driven entirely by `~/.ai-accounts/config.json`,
no flags. Enable it, then install its event hooks and low-frequency timer once
with `ai-accounts autoswitch setup` (see [Event hooks](#event-hooks) and
[Timer](#timer)); running it by hand
(`codex-accounts autoswitch`, `claude-accounts autoswitch`, `agy-accounts
autoswitch`, `grok-accounts autoswitch`, `vibe-accounts autoswitch`, or `ai-accounts autoswitch` to run
all five at once) stays available for an immediate check.

### Prerequisites

AI Accounts requires Python ≥ 3.10 and `uv`; the Python dependency `pypinyin` is
installed automatically by `uv`. The autoswitch setup itself uses only built-in
OS facilities and Python's standard JSON library — `jq` is not required.

| OS | Timer prerequisite |
| --- | --- |
| macOS | Built-in `launchctl` |
| Linux | `systemctl --user` or `crontab` (either one) |
| Windows | Built-in `schtasks` |

Codex and Claude need valid logins plus saved profiles before their quotas can
be checked. agy requires a macOS Keychain session and is skipped on Linux and
Windows. Codex also requires one manual `/hooks` approval the first time its
user hook is installed.

### Config

```sh
ai-accounts config                  # interactive menu (numbered fallback off a TTY)
ai-accounts config get              # print the whole config (telegram token masked)
ai-accounts config get notify       # print just one key
ai-accounts config set enabled true # turn auto-switching on
ai-accounts autoswitch setup        # install hooks + timer once
ai-accounts config set notify telegram
```

All six tools — `ai-accounts`, `codex-accounts`, `claude-accounts`,
`agy-accounts`, `grok-accounts`, and `vibe-accounts` — own an identical `config` command
against the one shared `~/.ai-accounts/config.json` (override with
`$AI_ACCOUNTS_CONFIG_JSON`); none of them forward it to another provider.

Running `config` with no arguments on a TTY opens an interactive menu:

```text
┌─ ai-accounts config (v3.0.0) ────────────────────────────────────────────────────────────────────────────────────────┐
│                                                                                                                      │
│  Automatic switching                                                                                                 │
│  ❯ Enable automatic switching  false                                                                                 │
│    ↳ Switch at usage (%)       90                                                                                    │
│    ↳ Quota window              1week                                                                                 │
│  Notifications                                                                                                       │
│    Notifications               desktop                                                                               │
│    Telegram bot token          ********WXYZ                                                                          │
│    Telegram chat id            (unset)                                                                               │
│  Provider behavior                                                                                                   │
│    Antigravity blind switch    false                                                                                 │
│    Automatic token refresh     true                                                                                  │
│  General                                                                                                             │
│    Language                    English                                                                               │
│  When the active account reaches the limit below, switch to the saved account with the most quota left.              │
│                                                                                                                      │
│  ↑↓ select · ←→ change · ⏎ edit/toggle · r reset · Esc cancel · q/Ctrl-C quit · saves as you go                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Cycle the `Language` row and the same menu repaints on the spot:

```text
┌─ ai-accounts config (v3.0.0) ───────────────────────────────────────────────────────┐
│                                                                                     │
│  自動切換                                                                           │
│    啟用自動切換        false                                                        │
│    ↳ 切換門檻（%）     90                                                           │
│    ↳ 配額視窗          1week                                                        │
│  通知                                                                               │
│    通知方式            desktop                                                      │
│    Telegram bot token  ********WXYZ                                                 │
│    Telegram chat id    （未設定）                                                   │
│  各家 CLI 行為                                                                      │
│    Antigravity 盲切    false                                                        │
│    自動更新 token      true                                                         │
│  一般                                                                               │
│  ❯ 語言                繁體中文                                                     │
│  通知訊息與本選單的語言。未指定前跟隨系統語系。                                     │
│                                                                                     │
│  ↑↓ 移動 · ←→ 切換 · Enter 編輯/切換 · r 重設 · Esc 取消 · q/Ctrl-C 離開 · 自動儲存 │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Rows are grouped under a bold heading, and the highlighted row's help text
prints under the list. The menu lists each setting by its human-readable
label, not by the key name you would pass to `config get`/`config set`. The
mapping is: `Enable automatic switching` = `enabled`, `↳ Switch at usage (%)`
= `switch_when_used_pct`, `↳ Quota window` = `switch_window`, `Notifications`
= `notify`, `Telegram bot token` = `telegram_bot_token`, `Telegram chat id` =
`telegram_chat_id`, `Antigravity blind switch` = `agy_blind_switch`,
`Automatic token refresh` = `token_refresh`, and `Language` = `language`.
Labels, help text, group headings, key hints and validation errors are all
translated: cycle the `Language` row and the whole menu repaints in that
language as you cycle it, so you can see what you are choosing. Only the
two supported languages are offered — the row cycles between `English` and
`繁體中文`, shown by name, not by code. Left unset, the row still shows a
plain name (whichever the OS resolves to, no extra "system" option or
wording) — pick a value and it stops following the OS. `config get language`
prints that code (`en`/`zh-TW`) so its output can be fed straight back to
`config set`. Masked fields render as
`********` plus the last four characters (all stars when the stored value is
12 characters or shorter).

Keybindings: `↑`/`↓` move the cursor; `⏎` edits the highlighted field — for a
boolean or an enum (`notify`, `switch_window`, `language`) this **cycles** its allowed
values instead of asking you to type one, `←`/`→` cycle the same way without
entering edit mode first; `Esc` cancels an in-progress edit; `r` resets every
setting; `q` quits. An invalid typed value shows the validation error inline
and is not saved.

**There is no save key: every change is saved the moment you make it.** A
toggle, a cycled enum or a committed typed value is written straight to
`~/.ai-accounts/config.json`, so quitting can no longer discard anything — the
old `s` key is gone, and so is the yellow "discarded unsaved changes" warning.
The write happens on a background thread and is flushed before the menu
returns, so a keypress never waits on the disk; if a write fails (a
hand-edited invalid value elsewhere in the file, an unwritable `$HOME`) the
reason is shown on the menu's error line, or printed as `❌ …` on exit when
you quit before it lands.

`r` resets **all** settings to their defaults, but only after a confirmation:
the row list stays untouched and a `⚠ Reset ALL settings to their defaults?
[y/N]` line appears; `y` performs the reset (and saves it immediately), and
**any** other key cancels — including `Esc`, `n` and an arrow key, since a
reset blanks `telegram_bot_token` and must never ride on one stray keypress.
Keys this version does not know about (a newer ai-accounts's settings) are left
in the file untouched, exactly as a normal save leaves them.

Piped or non-interactive input (scripts, CI, `| cat`) skips raw mode entirely:
`config` prints the same numbered listing `get` would and exits `0` rather
than attempting to read arrow keys.

`config get`/`config set` remain the scriptable form, unchanged. `set` rejects
any key outside the table below with a message listing the valid ones, and
validates the value before writing: `notify` must be one of the three
channels, `switch_window` one of the two windows, `language` one of `en` or
`zh-TW`, `switch_when_used_pct` must be an integer 1-100, and the three
booleans are parsed strictly — a hand-typed `"false"` is stored as `False`,
not Python's `bool("false") == True`. One behaviour changed from the legacy
`ai-accounts`-only implementation: `config set telegram_bot_token <value>` now
echoes the saved value **masked**, like every other masked field, instead of
printing the raw token back to the terminal.

Both booleans are also **read** fail-closed: only a real JSON `true` turns one
on. Hand-edit `~/.ai-accounts/config.json` to `"enabled": "false"` (a truthy
string to `bool()`) and the feature stays off, which is the safe way for an odd
value to be wrong — the same rule guards `agy_blind_switch`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Master switch — off means every provider's `autoswitch`, event hooks and timer are silent no-ops. Only a JSON `true` is on; any other value (including the string `"true"`) reads as off. It never installs or removes OS jobs by itself. |
| `switch_when_used_pct` | int (1-100) | `90` | **USED**-percent trigger — see the worked example below |
| `switch_window` | `1week` \| `5h` | `1week` | Which quota window the trigger reads. Defaults to the weekly one: the 5-hour figure is often unavailable (the usage table renders it `—`), and a trigger reading it would sit idle on an account 93% through its week. The choice is strict — when the chosen window has no data the run reports `Could not determine usage` rather than quietly reading the other one |
| `notify` | `desktop` \| `telegram` \| `none` | `desktop` | Where a switch (or a dead end) is reported. `desktop` plays a notification sound and its notification does nothing when clicked |
| `telegram_bot_token` | string | `""` | Bot API token; stored in `~/.ai-accounts/config.json` (mode `0600`) and masked by `config get` |
| `telegram_chat_id` | string | `""` | Bot API chat id to notify |
| `agy_blind_switch` | bool | `false` | Opt-in: let `agy-accounts autoswitch` switch without verifying the candidate's quota first (see the support matrix below) |
| `token_refresh` | bool | `true` | Independent of `enabled` — lets the scheduled timer renew OAuth tokens across all four providers even when auto-switching itself is off. Only a JSON `true` is on |
| `language` | `en` \| `zh-TW` | the OS locale | Language of notifications **and** of the `config` menu itself. Only `en`/`zh-TW` are settable choices — there is no separate "system" option. Until you pick one, it follows the OS locale on every platform — `LC_ALL`/`LC_MESSAGES`/`LANG` on macOS and Linux, the user default locale on Windows — and falls back to English for any locale with no translation (`zh_CN`/`zh_Hans` included: Simplified deliberately does not resolve to Traditional). An unrecognised stored value behaves the same as unset rather than failing a notification |

🔴 **`switch_when_used_pct` is how much quota is USED, not how much remains**,
and the trigger fires at `used >= switch_when_used_pct`. The default, 90,
means: **switch once 90% of the quota is USED, i.e. when 10% remains.** This
inverts the natural reading of "switch when quota drops below N%" — read it
as a used-percent ceiling, not a remaining-percent floor.

### Notification messages

Each situation sends one plain-text message: a headline line, then one line
saying what to do about it. Telegram and desktop notifications get the same
two lines (the desktop one uses them as title and body).

```
🔄 codex: work (93%) → personal (15%)
⚠️ Restart your session to use it

🔄 claude: work (91%) → personal (8%)
✅ Session restarted — the new account is live

🚫 agy: work at 96%, nothing to switch to
💡 Others are 90%+ or unreadable — wait for the reset, or add one

🔑 ai-accounts: a token expired, re-login needed
ai-accounts refresh --all → <provider>-accounts login-switch <name>
```

In Traditional Chinese the same four read:

```
🔄 codex：work（93%）→ personal（15%）
⚠️ 需重啟 session 才會生效

🔄 claude：work（91%）→ personal（8%）
✅ 已自動重啟 session，新帳號已生效

🚫 agy：work 已用 96%，無帳號可切
💡 其他帳號都在 90% 以上或讀不到用量 — 等配額重置，或再新增一個

🔑 ai-accounts：token 已失效，需重新登入
ai-accounts refresh --all → 再 <provider>-accounts login-switch <name>
```

Provider names, profile names and commands stay untranslated — they are
identifiers you type, not prose.

**Desktop notifications play a sound and do nothing when clicked**, on all
three platforms. The sound is named, never a bundled or hard-coded file:
macOS plays `Glass`, Windows declares `ms-winsoundevent:Notification.Default`
on the toast, and Linux asks `canberra-gtk-play -i complete` for whatever the
active XDG sound theme ships — a box without canberra still gets the
notification, silently. The Windows toast carries no `launch` attribute and
no `<action>` children, and `notify-send` is called with no `--action`,
which is what makes their clicks inert.

macOS needed more than that. AppleScript's `display notification` — even
wrapped as `tell application "System Events" to display notification ...`,
which is the commonly cited fix — is attributed to Script Editor's identity
in Notification Center regardless of which app the script is sent to,
because the command is a Standard Additions scripting addition that runs in
`osascript`'s own process; clicking the banner opens Script Editor. There is
no scripting-level fix for that. So when
[`terminal-notifier`](https://github.com/julienXX/terminal-notifier) is
already on `PATH` (e.g. installed for Fastlane), ai-accounts prefers it: a real
app bundle with its own identity, so a banner with no `-open`/`-execute`/
`-activate` genuinely does nothing when clicked. Delivery is verified with
`terminal-notifier -list <group>` — a documented feature of the tool, not a
guess at the private `com.apple.ncprefs.plist` bit layout — because a System
Settings toggle can silently swallow the notification while the process
itself still exits `0`. If it wasn't actually delivered, or terminal-notifier
isn't installed at all, ai-accounts falls back to plain `osascript` (not
click-inert, but always shown) and — the first time only, via a marker at
`~/.ai-accounts/notify-hint-shown` — prints a one-line hint to turn
terminal-notifier on in System Settings → Notifications.

The switch message names the usage of **both** accounts, so it is obvious
whether the switch bought an hour or a week, and its second line reports what
actually happened: `✅ Session restarted` when the post-switch restart ran,
`⚠️ Restart your session to use it` when it did not (an unattended timer poll
never restarts anything — see the restart ladder below). The dead-end and
revoked-token messages are sent at most once an hour each.

All wording lives in one table, `MESSAGES` in
[`src/ai-accounts/i18n.py`](src/ai-accounts/i18n.py), keyed message-id first and
language second, so every translation of a string sits on adjacent lines.
Adding a language means appending one `Language` entry plus its rows; a row a
language is missing falls back to English rather than failing.

Switch to Telegram notifications in one command each:

```sh
ai-accounts config set notify telegram
ai-accounts config set telegram_bot_token 000000:REPLACE-WITH-YOUR-BOT-TOKEN
ai-accounts config set telegram_chat_id 000000000
```

### Event hooks

Run `ai-accounts autoswitch setup` once after enabling the feature. It adds one
`Stop` hook to Codex, Claude and (on macOS) agy. The hook runs the same existing
provider `autoswitch` command when an agent turn completes, so usage is checked
while the CLI is active rather than by a one-minute background poll. Grok and
Vibe are excluded because they do not expose a quota API. Existing user hooks
are preserved. The hook command embeds the interpreter ai-accounts runs from, so
re-running setup after a reinstall (editable checkout → `uv tool`, or a rebuilt
venv) rewrites ai-accounts's own stale entry in place instead of adding a second
one. Changing `enabled` merely enables or disables checks; it does not rewrite
hooks or scheduler entries. Opening the interactive `config` menu detects a
missing setup and offers to install it; `config set` remains fully
non-interactive.

Codex requires a one-time trust review for a newly installed user hook: start
Codex and use `/hooks` to approve `ai-accounts-autoswitch`. A hook switches the
saved credentials for the next session; a running CLI is not safely hot-swapped
and must restart/resume after a switch (see [Restart after a switch](#restart-after-a-switch)).

### Timer

`ai-accounts autoswitch setup` also registers the scheduled check as a
low-frequency fallback for inactive or failed hooks. It is deliberately a
separate one-time action: repeated `config set enabled true` does not wake the
machine, rewrite hook files, or reinstall the timer. The commands below remain
available to choose a non-default interval, or to check/repair the timer:

```sh
ai-accounts install-timer                # schedule the check every 1800s (30 min, the default)
ai-accounts install-timer --interval 600 # or every 600s
ai-accounts timer-status                 # "installed" | "not installed"
ai-accounts uninstall-timer              # remove the scheduled check
```

The scheduled job runs `python -m ai_accounts.autoswitch_timer run`, which drives
**two independent checks**, each gated by its own config key:

- **Auto-switch** — gated by `enabled` (default `false`). When on, it runs the
  same quota check as `ai-accounts autoswitch`, across all four providers.
- **Token refresh** — gated by `token_refresh` (default `true`). When on, it
  runs `<provider>-accounts refresh --all` for every provider, keeping OAuth
  tokens renewed on the timer's own schedule — independent of `enabled`, so
  a user with auto-switching off still gets live tokens. A revoked refresh
  token (one that needs a fresh login, not just a retry) triggers a
  de-duplicated notification via the same `notify` channel; a routine
  rotation or a transient network failure stays silent.

Either, both, or neither check runs on a given tick, depending on which flags
are set. No `jq` dependency is required: hook configuration uses Python's
standard JSON library. Installed per OS as:

| OS | Mechanism |
| --- | --- |
| macOS | a `launchd` agent (`~/Library/LaunchAgents/com.ai_accounts.autoswitch.plist`), `StartInterval` set to the interval |
| Linux | a `systemd --user` timer + service under `~/.config/systemd/user/`; falls back to a tagged `crontab` line when `systemctl` isn't on `PATH` |
| Windows | a scheduled task via `schtasks /Create`, `/SC MINUTE` |

### Per-provider support

| Provider | Support | Notes |
| --- | --- | --- |
| `codex-accounts` | Full | Each saved profile's quota is probed from its own file — no profile is activated just to check it |
| `claude-accounts` | Full | Same approach: each profile's usage is fetched with its own token, without switching |
| `agy-accounts` | opt-in, blind | `agy` reports quota only for the *live* session — reading a candidate's quota would mean activating it, which hijacks the single shared credential-store slot a running `agy` process depends on. So candidates are never probed in advance; switching anyway is opt-in via `agy_blind_switch` (default off, reports and stops), and once switched the notification says the target's quota was **not pre-verified**. Blind mode's target is the alphabetically-first candidate (every candidate is treated as equally, unverifiably, empty). Where the credential store is unreachable (a Linux box without `secret-tool`) its config remains available, but hooks and timed checks skip it. |
| `grok-accounts` | Not supported | xAI ships no quota API for Grok Build. `autoswitch` prints `autoswitch unsupported for grok: no quota API` and exits `0`, so the `ai-accounts autoswitch` fan-out is never reported as a failure over this one provider |
| `vibe-accounts` | Not supported | Vibe uses static API keys and exposes no quota API. `autoswitch` prints `autoswitch unsupported for vibe: no quota API` and exits `0` |

### Restart after a switch

A switch changes which credentials are on disk; whether a provider's already
running CLI session picks that up without a restart was probed
provider-by-provider — see
[`docs/autoswitch-hot-reload-spike.md`](docs/autoswitch-hot-reload-spike.md)
for the full evidence and caveats. Short version: no provider hot-reloads a
rotated credential seamlessly. Each provider's `autoswitch` follows the
spike's rung table — auto-restarting a fresh session only when the check was
run interactively (never from the unattended timer, which has no terminal to
restart into) — or otherwise tells you to restart the session by hand. `grok`
is the exception: it never switches at all (no quota API, above), so it never
reaches the ladder; the verdict recorded for it is what it would ship once xAI
exposes quota.

---

## External binaries required

Each tool checks its own dependencies and reports a clear error if anything is
missing. macOS can install supported Homebrew dependencies on first use;
Windows and Linux print the appropriate `winget`/Scoop/Chocolatey or system
package-manager command.

| Tool | Platforms | External binaries |
| --- | --- | --- |
| `gtrans` | macOS / Windows / Linux | Linux clipboard integration optionally uses `wl-copy`, `xclip`, or `xsel` |
| `imgmin` | macOS / Windows / Linux | `pngquant`, `oxipng`, `jpegoptim`, `cwebp`, `svgo`, `gifsicle`, `sharp`; HEIC-to-HEIC additionally needs macOS `sips` |
| `resize-image` | macOS / Windows / Linux | `magick` (ImageMagick) |
| `towebp` | macOS / Windows / Linux | `cwebp` |
| `html2md` | macOS / Windows / Linux | `pandoc` |
| `vcadd` | macOS only | vChewing input method |
| `codex-accounts` | macOS / Windows / Linux | `codex` CLI for `who` and `login-switch` |
| `agy-accounts` | macOS / Windows / Linux | Official `agy` CLI. The live session is read from / written to the OS credential store: macOS Keychain, Windows Credential Manager, or Linux Secret Service via `secret-tool` (`libsecret`) |
| `grok-accounts` | macOS / Windows / Linux | Official `grok` CLI for refresh and login |
| `vibe-accounts` | macOS / Windows / Linux | Official `vibe` CLI for `login-switch`. On macOS the live key is read from / written to the login keychain (service `ai.mistral.vibe`); elsewhere `$VIBE_HOME/.env` is the store |

---

## Local development

```sh
cd ai-accounts
uv sync --locked
uv run gtrans "Hello world"
```

To install the local checkout as a global tool:

```sh
uv tool install --editable .
```

## Optional: keep the zsh aliases

If you want the original short aliases to keep working, drop these into `~/.zshrc`:

```zsh
# Translation
alias ge='gtrans -e'             # quick: auto-detect → English
alias translate='gtrans'         # explicit alias

# Image
alias resize='resize-image'      # avoid clobbering shell's builtin `resize`
alias toWebp='towebp'

# HTML → Markdown
alias html_to_md='html2md'
alias htmltomd='html2md'
alias h2m='html2md'
alias htom='html2md'

# Codex accounts
alias codexwho='codex-accounts who'
alias codexcurrent='codex-accounts current'
alias codexsave='codex-accounts save'
alias codexlist='codex-accounts list'
alias codexswitch='codex-accounts switch'
alias codexremove='codex-accounts remove'
alias codexrefresh='codex-accounts refresh'
alias codexsync='codex-accounts sync'
alias codexloginswitch='codex-accounts login-switch'

# Antigravity accounts
alias agywho='agy-accounts who'
alias agycurrent='agy-accounts current'
alias agysave='agy-accounts save'
alias agylist='agy-accounts list'
alias agyswitch='agy-accounts switch'
alias agyremove='agy-accounts remove'
alias agyrefresh='agy-accounts refresh'
alias agysync='agy-accounts sync'
alias agyloginswitch='agy-accounts login-switch'
```

---

## Troubleshooting

### `uv: command not found`

`uv` isn't installed or isn't on `PATH`. Install it and open a new shell:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
brew install uv
```

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### `error: Repository not found` over HTTPS

The repo URL is wrong, or visibility changed. The canonical URL is
`https://github.com/weskao/ai_accounts.git` and the repo is public.

### Stale clone after switching install URL

If a previous install is stuck on an old URL or commit, wipe the cache and
reinstall pinned to a tag:

```sh
uv cache clean
uv tool uninstall ai-accounts 2>/dev/null
uv tool install --reinstall --from git+https://github.com/weskao/ai_accounts.git@vX.Y.Z ai-accounts
```

## 📄 License

This project is licensed under the terms of the MIT open source license. Please refer to the [LICENSE](./LICENSE) file for the full terms.
