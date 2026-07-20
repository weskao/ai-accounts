# ai-accounts

Personal CLI toolbox — a collection of productivity tools packaged as a Python
project and managed via [`uv tool`](https://docs.astral.sh/uv/concepts/tools/).

Requires **uv** ([install uv](https://docs.astral.sh/uv/getting-started/installation/))
and Python ≥ 3.10. No GitHub token or SSH key is required to install — the
repo is public.

The package installs and every command starts cleanly on **macOS, Windows, and
Linux**. `vcadd` and `agy-accounts` require macOS because their upstream
credential/input-method integrations are macOS-only; they report that limitation
without a traceback on other platforms.

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
| `codex-accounts` | Manage multiple Codex CLI login profiles (save / list / switch / remove) |
| `claude-accounts` | Manage multiple Claude Code login profiles and inspect usage |
| `agy-accounts` | Manage multiple Antigravity OAuth profiles and inspect quota (macOS) |
| `ai-accounts` | Drive every AI account tool at once — forwards any subcommand (`list`, `who`, `refresh`, `sync`, …) to all three `*-accounts` |

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

### towebp Usage

```sh
towebp           # convert all PNG/JPG/JPEG recursively (default)
towebp -c        # current folder only (no sub-folder recursion)
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

All three account managers (`codex-accounts`, `claude-accounts`, `agy-accounts`)
keep their saved profiles in one central, hidden folder under your **home
directory**:

```text
$HOME/
└── .ai-accounts/
    ├── claude/accounts/          # claude-accounts profiles
    ├── codex/accounts/           # codex-accounts profiles
    └── antigravity/accounts/     # agy-accounts profiles
```

The path is resolved from the running user's home directory at runtime
(`~/.ai-accounts` on macOS/Linux, `C:\Users\<name>\.ai-accounts` on Windows), so it
works the same on all three platforms and is completely independent of where
this repository or the installed tools live — moving or reinstalling ai-accounts
never touches the stored profiles.

The location is deliberately **outside** the app dotdirs (`~/.claude`,
`~/.codex`): if you version-control a dotdir as a dotfiles repo, the OAuth
token snapshots that profiles contain can never end up in a commit. A store
found at a legacy in-dotdir location (`~/.claude/accounts`,
`~/.codex/accounts`) is moved to the central location automatically the first
time a command touches it, with a one-line notice.

Treat `~/.ai-accounts` as secrets — every `<name>.json` profile holds live auth
tokens. Each tool's location can be overridden individually via
`CODEX_ACCOUNT_DIR` / `CLAUDE_ACCOUNT_DIR` / `ANTIGRAVITY_ACCOUNT_DIR` (see
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
codex-accounts save <name>           # save the current login as a reusable profile
codex-accounts list                  # list saved profiles (table view)
codex-accounts switch <name>         # switch to a saved profile
codex-accounts remove <name>         # delete a saved profile
codex-accounts refresh [<name>]      # renew tokens via OAuth refresh (no browser, no logout);
                                     # no name = refresh the active auth + sync it back
codex-accounts refresh --all         # renew every saved profile in one run
codex-accounts sync                  # copy the active auth back to its matching profile
codex-accounts login-switch <name>   # codex logout + codex login + save as <name>
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
claude-accounts save <name>           # save the current login as a reusable profile
claude-accounts list                  # list saved profiles with 5h/1w usage (table view)
claude-accounts switch [<name>]       # switch by name; no name = interactive picker
claude-accounts remove <name>         # delete a saved profile
claude-accounts refresh [<name>]      # renew tokens via OAuth refresh (no browser, no logout)
claude-accounts refresh --all         # renew every saved profile in one run
claude-accounts sync                  # copy the active auth back to its matching profile
claude-accounts login-switch <name>   # `claude auth login` + save as <name>
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
  same way, with the state also spelled out in text, not color alone.
- `switch` backs up the previous credentials (timestamped, `chmod 600`) before overwriting.
- `list` shows a spinner (with a live "which profile" label) on a TTY while it fetches usage
  for each profile; it's automatically skipped when output isn't a terminal (piping, `ai-accounts`).

### claude-accounts Environment Overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Base Claude Code config directory |
| `CLAUDE_CREDENTIALS_JSON` | `$CLAUDE_CONFIG_DIR/.credentials.json` | Active Claude credentials file |
| `CLAUDE_ACCOUNT_DIR` | `~/.ai-accounts/claude/accounts` | Where saved profiles are stored |

---

## `agy-accounts` — Antigravity Account Manager

**Platform:** macOS only. The official `agy` session used by this command is
stored in macOS Keychain.

Save, list, and switch between multiple sessions for the official Antigravity CLI (`agy`). It
never requires a `GEMINI_API_KEY`, embeds no OAuth client credentials, and never prints raw
tokens. The active session is stored in the macOS Keychain by `agy`; reusable profile snapshots
live in ai-accounts's own `~/.ai-accounts/antigravity/` store.

`list` temporarily activates each profile, asks `agy` for the same quota data used by `/usage`,
and restores the original session. It shows plan, Gemini weekly/5-hour use, Claude/GPT
weekly/5-hour use, refresh time, and active state.

### agy-accounts Usage

```sh
agy-accounts who                   # show the selected Antigravity account
agy-accounts current               # alias for `who`
agy-accounts save <name>           # save the current login as a reusable profile
agy-accounts list                  # list profiles with agy model-family quota
agy-accounts switch [<name>]       # switch by name; no name = interactive picker
agy-accounts remove <name>         # delete a saved profile
agy-accounts refresh [<name>]      # let agy refresh the session/quota and save rotations
agy-accounts refresh --all         # refresh every saved profile through agy
agy-accounts sync                  # copy the active Keychain session to its profile
agy-accounts login-switch <name>   # official agy browser login + save as <name>
```

### agy-accounts First-time setup

**Prerequisite** — install the official Antigravity CLI and confirm it is available:

```sh
agy --version
```

Then add one or more accounts. `login-switch` launches `agy` itself; finish its browser login,
then exit the CLI with Ctrl+D twice so the new Keychain session can be saved:

```sh
agy-accounts login-switch personal   # log into the first account, save as "personal"
agy-accounts login-switch work       # log into the second account, save as "work"
agy-accounts list                    # verify both profiles are saved
```

After the initial setup, switch at any time with:

```sh
agy-accounts switch personal   # activate the "personal" profile
agy-accounts switch work       # activate the "work" profile
agy-accounts who               # confirm the current account
```

### agy-accounts Session upkeep

The roughly one-hour expiry is for the access token, not the login. Saved sessions include a
refresh token, and `agy` renews the access token automatically without a Gemini API key:

```sh
agy-accounts refresh --all     # refresh every profile and its quota through agy
agy-accounts refresh work      # refresh one profile and save rotated tokens
agy-accounts refresh           # refresh the active session and sync it back
agy-accounts sync              # copy the current Keychain session to its profile
```

If a refresh token is revoked, re-login with `agy-accounts login-switch <name>`.

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

- `who` and `switch` render a bordered "Current Auth Claims" panel with token expiry color-coded
  (green = valid, yellow = expiring within 24 h, red = `EXPIRED`).
- `switch` backs up the previous Keychain session (timestamped, `chmod 600`) before overwriting it.
- `list` shows a spinner (with a live "which profile" label) on a TTY while it queries `agy`
  for each profile; it's automatically skipped when output isn't a terminal (piping, `ai-accounts`).

### agy-accounts Environment Overrides

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTIGRAVITY_HOME` | `~/.ai-accounts/antigravity` | Profile and credential-mirror root |
| `ANTIGRAVITY_OAUTH_JSON` | `$ANTIGRAVITY_HOME/oauth_creds.json` | Active Keychain session mirror |
| `ANTIGRAVITY_ACCOUNT_DIR` | `$ANTIGRAVITY_HOME/accounts` | Saved profiles |
| `ANTIGRAVITY_CLI_PATH` | resolved from `PATH` | Override the `agy` executable used for quota checks |

---

## `ai-accounts` — All-provider Account Front-end

Forwards a subcommand to all three per-provider tools (`codex-accounts`,
`claude-accounts`, `agy-accounts`) at once, so one command drives every
provider. It exposes the same command surface as the per-provider tools.

### ai-accounts Usage

```text
ai-accounts                        Show this help (the available commands)
ai-accounts list                   List all provider profiles (providers run in parallel)
ai-accounts who | current          Show the active account for every provider
ai-accounts refresh [<name>|--all] Refresh tokens across every provider
ai-accounts sync                   Sync active auth back to its profile, every provider
ai-accounts save <name>            Save the current login as <name> in every provider
ai-accounts switch [<name>]        Switch profile in every provider (interactive, one at a time)
ai-accounts remove <name>          Remove profile <name> from every provider
ai-accounts login-switch <name>    Fresh login + save as <name>, every provider (interactive)
ai-accounts -h | --help            Show this help
```

Bare `ai-accounts` (no arguments) prints this help. `list` runs the three
providers **concurrently** and prints each one's table as soon as it
finishes — fastest provider first, not a fixed order — with a spinner in
between tracking how many are still outstanding (`Fetching remaining 2
providers…`, then `1`, …) until the last table lands and the spinner
disappears for good. Every other command runs the providers **one at a time
with live output**, so interactive flows (switch pickers, `login-switch`) and
color work unchanged; any argument after the command (a profile name,
`--all`, …) is passed through to each provider. Per-provider errors are
printed inline without aborting the others, and the exit code is non-zero if
any provider's command failed. `list`'s spinner only shows on a TTY (their
own inner spinners stay off, since their output is captured rather than run
on a live terminal).

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
| `agy-accounts` | macOS only | Official `agy` CLI and macOS Keychain |

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
