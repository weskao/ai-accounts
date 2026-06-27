# ai-accounts

Personal CLI toolbox — a collection of productivity tools packaged as a Python
project and managed via [`uv tool`](https://docs.astral.sh/uv/concepts/tools/).

Requires **uv** ([install uv](https://docs.astral.sh/uv/getting-started/installation/))
and Python ≥ 3.10. No GitHub token or SSH key is required to install — the
repo is public.

Runs on **macOS, Windows, and Linux**. OS-specific bits (clipboard, dependency
install, terminal colors) are handled automatically per platform.

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

After install, the following commands are available on `PATH`:

| Command | Purpose |
| --- | --- |
| `gtrans` | Google Translate CLI with clipboard + chunked translation |
| `charcount` | Count characters in text or file, with optional limit |
| `imgmin` | Visually-lossless image compression toolkit |
| `resize-image` | Resize images (JPG/PNG/WebP) via ImageMagick |
| `towebp` | Convert PNG/JPG/JPEG to WebP |
| `html2md` | Convert HTML files to Markdown via pandoc |
| `vcadd` | Add Chinese words with 注音符號（Bopomofo）readings to vChewing user dictionary |

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

### Dependencies (auto-installed via Homebrew/npm on first use)

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
- vChewing is reloaded via AppleScript after any words are added. If the reload fails,
  a reminder to press **Reload User Phrases** manually is shown.

### vcadd Dependencies

| Dependency | Notes |
| --- | --- |
| `pypinyin` | Python package — installed automatically with ai-accounts |
| vChewing | Must be running for the AppleScript reload to succeed |

---

## External binaries required

Each tool checks its own dependencies and reports a clear error if anything is
missing. Most can be auto-installed via Homebrew on first use.

| Tool | External binaries |
| --- | --- |
| `gtrans` | `curl`, `pbcopy` (macOS) |
| `imgmin` | `pngquant`, `oxipng`, `jpegoptim`, `cwebp` (webp), `svgo`, `gifsicle`, `sharp` (npm), `sips` (macOS) |
| `resize-image` | `magick` (imagemagick) |
| `towebp` | `cwebp` |
| `html2md` | `pandoc` |
| `vcadd` | `osascript` (macOS built-in), vChewing input method |

---

## Local development

```sh
cd ai-accounts
uv sync
uv run gtrans "Hello world"
```

To install the local checkout as a global tool:

```sh
uv tool install --editable .
```

## Optional: keep the zsh aliases

If you want the original short aliases to keep working, drop these into `~/.zshrc`:

```zsh
alias ge='gtrans -e'             # quick: auto-detect → English
alias translate='gtrans'         # explicit alias
alias resize='resize-image'      # avoid clobbering shell's builtin `resize`
alias toWebp='towebp'
alias html_to_md='html2md'
alias htmltomd='html2md'
alias h2m='html2md'
alias htom='html2md'
```

---

## Troubleshooting

### `uv: command not found`

`uv` isn't installed or isn't on `PATH`. Install it and open a new shell:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# or
brew install uv
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

### `Permission denied (publickey)` when using an `ssh://` URL

You don't need SSH anymore — the install URLs above use plain HTTPS and no
auth. If you (or an old script) are still calling
`git+ssh://git@github.com/weskao/ai_accounts.git`, switch to the HTTPS form.

## 📄 License

This project is licensed under the terms of the MIT open source license. Please refer to the [LICENSE](./LICENSE) file for the full terms.
