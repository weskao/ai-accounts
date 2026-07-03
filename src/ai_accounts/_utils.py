"""Shared helpers for ai-accounts CLI tools.

Cross-platform: macOS, Windows, and Linux. Anything OS-specific (clipboard,
package installation, ANSI colors) is funnelled through this module so the
individual tools stay platform-agnostic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Sequence

YELLOW = "\033[1;33m"
GREEN = "\033[1;32m"
RED = "\033[1;31m"
DIM = "\033[2m"
RESET = "\033[0m"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ── ANSI / color support ─────────────────────────────────────────────────────

def _enable_windows_ansi() -> bool:
    """Turn on virtual-terminal processing so ANSI escapes render on Windows.

    No-op (returns True) on non-Windows. On modern Windows 10+ consoles this
    flips ENABLE_VIRTUAL_TERMINAL_PROCESSING for both stdout and stderr.
    """
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ENABLE_VT = 0x0004
        ok = False
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            if kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT):
                ok = True
        return ok
    except Exception:
        return False


# Enable VT once at import time so even the unconditional ANSI output (e.g.
# imgmin's summary table printed to stdout) renders on Windows terminals.
_WIN_ANSI_OK = _enable_windows_ansi()


def _color_supported() -> bool:
    try:
        if not sys.stderr.isatty():
            return False
    except Exception:
        return False
    if IS_WINDOWS:
        return _WIN_ANSI_OK or bool(os.environ.get("WT_SESSION"))
    return True


def log_yellow(msg: str) -> None:
    if _color_supported():
        print(f"{YELLOW}{msg}{RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def log_green(msg: str) -> None:
    if _color_supported():
        print(f"{GREEN}{msg}{RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def log_red(msg: str) -> None:
    if _color_supported():
        print(f"{RED}{msg}{RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ── dependency management ────────────────────────────────────────────────────

# Per-platform install instructions for the external binaries ai-accounts shells
# out to. Keyed by the package name passed to ``ensure_tool``. The macOS column
# is also used to drive Homebrew auto-install (preserving the original behavior).
_INSTALL_HINTS: dict[str, dict[str, str]] = {
    "pngquant": {
        "darwin": "brew install pngquant",
        "linux": "sudo apt install pngquant   (or: sudo dnf install pngquant / sudo pacman -S pngquant)",
        "win32": "scoop install pngquant   (or: choco install pngquant)",
    },
    "oxipng": {
        "darwin": "brew install oxipng",
        "linux": "cargo install oxipng   (or your distro package, if available)",
        "win32": "scoop install oxipng   (or: cargo install oxipng)",
    },
    "jpegoptim": {
        "darwin": "brew install jpegoptim",
        "linux": "sudo apt install jpegoptim   (or: sudo dnf install jpegoptim / sudo pacman -S jpegoptim)",
        "win32": "scoop install jpegoptim",
    },
    "webp": {  # provides cwebp
        "darwin": "brew install webp",
        "linux": "sudo apt install webp   (or: sudo dnf install libwebp-tools / sudo pacman -S libwebp)",
        "win32": "scoop install libwebp   (or: choco install webp)",
    },
    "svgo": {
        "darwin": "npm install -g svgo",
        "linux": "npm install -g svgo",
        "win32": "npm install -g svgo",
    },
    "gifsicle": {
        "darwin": "brew install gifsicle",
        "linux": "sudo apt install gifsicle   (or: sudo dnf install gifsicle / sudo pacman -S gifsicle)",
        "win32": "scoop install gifsicle   (or: choco install gifsicle)",
    },
    "pandoc": {
        "darwin": "brew install pandoc",
        "linux": "sudo apt install pandoc   (or: sudo dnf install pandoc / sudo pacman -S pandoc)",
        "win32": "winget install --id JohnMacFarlane.Pandoc   (or: choco install pandoc)",
    },
    "imagemagick": {  # provides magick
        "darwin": "brew install imagemagick",
        "linux": "sudo apt install imagemagick   (or: sudo dnf install ImageMagick / sudo pacman -S imagemagick)",
        "win32": "winget install --id ImageMagick.ImageMagick   (or: choco install imagemagick)",
    },
    "codex": {
        "darwin": "npm install -g @openai/codex",
        "linux": "npm install -g @openai/codex",
        "win32": "npm install -g @openai/codex",
    },
}


def _install_hint(pkg: str) -> str:
    by_os = _INSTALL_HINTS.get(pkg, {})
    key = "darwin" if IS_MACOS else "win32" if IS_WINDOWS else "linux"
    return by_os.get(key) or f"install '{pkg}' using your platform's package manager"


def ensure_python_package(import_name: str, pip_name: str | None = None) -> bool:
    """Ensure a Python package is importable, auto-installing via pip if needed.

    Args:
        import_name: The name used in ``import <import_name>`` statements.
        pip_name: The PyPI package name (defaults to ``import_name`` if omitted).

    Returns True when the package is available, False if installation failed.
    """
    try:
        __import__(import_name)
        return True
    except ImportError:
        pass

    install_name = pip_name or import_name
    log_yellow(f"⚙️  {import_name} not installed, installing automatically...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", install_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        __import__(import_name)
        log_green(f"✅ {install_name} installed successfully")
        return True
    except Exception as exc:
        log_red(f"❌ Failed to auto-install {install_name}: {exc}")
        return False


def ensure_tool(pkg: str, cmd: str | None = None) -> bool:
    """Ensure ``cmd`` (defaults to ``pkg``) is available on PATH.

    On macOS a missing tool is auto-installed via Homebrew (matching the
    original zsh behavior). On Windows/Linux — where system package managers
    typically need ``sudo`` or interactive prompts that don't belong inside a
    CLI run — a clear, platform-specific install hint is printed and ``False``
    is returned so the caller can exit gracefully.
    """
    bin_name = cmd or pkg
    if have(bin_name):
        return True

    if IS_MACOS:
        log_yellow(f"⚠️  {bin_name} not detected, attempting to install {pkg} via Homebrew...")
        if have("brew"):
            res = subprocess.run(["brew", "install", pkg])
            if res.returncode == 0 and have(bin_name):
                return True
            log_red(f"❌ brew install {pkg} failed")
        else:
            log_red("❌ Homebrew not found, please install first: https://brew.sh")

    log_red(f"❌ Required tool '{bin_name}' not found.")
    log_yellow(f"   Install it with:  {_install_hint(pkg)}")
    return False


# ── clipboard ────────────────────────────────────────────────────────────────

def _pipe_to(cmd: Sequence[str], data: bytes) -> bool:
    """Feed ``data`` to ``cmd`` over stdin. Returns True on a clean exit."""
    try:
        proc = subprocess.run(list(cmd), input=data, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _windows_set_clipboard(text: str) -> bool:
    """Set the Windows clipboard via the Win32 API (Unicode-safe, no deps).

    Used in preference to ``clip``/``Set-Clipboard`` because those mangle
    non-ASCII text — and gtrans' primary use case is CJK output.
    """
    try:
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)  # null-terminated UTF-16
            size = ctypes.sizeof(buf)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            locked = kernel32.GlobalLock(handle)
            if not locked:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(locked, buf, size)
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def copy_to_clipboard(text: str) -> bool:
    """Copy ``text`` to the OS clipboard (best-effort, cross-platform).

    - macOS  → ``pbcopy``
    - Windows→ Win32 clipboard API (falls back to ``clip``)
    - Linux  → ``wl-copy`` (Wayland), else ``xclip``, else ``xsel``

    Returns True on success, False if no clipboard mechanism is available.
    """
    data = text.encode("utf-8")

    if IS_MACOS:
        return _pipe_to(["pbcopy"], data)

    if IS_WINDOWS:
        if _windows_set_clipboard(text):
            return True
        # Last-resort fallback (ASCII-safe only) for unusual environments.
        return _pipe_to(["clip"], text.encode("utf-16-le"))

    # Linux / *BSD: prefer Wayland when present, then X11 utilities.
    if os.environ.get("WAYLAND_DISPLAY") and have("wl-copy"):
        if _pipe_to(["wl-copy"], data):
            return True
    if have("xclip"):
        if _pipe_to(["xclip", "-selection", "clipboard"], data):
            return True
    if have("xsel"):
        if _pipe_to(["xsel", "--clipboard", "--input"], data):
            return True
    if have("wl-copy"):
        return _pipe_to(["wl-copy"], data)
    return False


def output_and_copy(text: str) -> None:
    """Print to stdout, copy to clipboard, and announce on stderr."""
    print(text)
    if copy_to_clipboard(text):
        print(f"\n✅ Copied to clipboard", file=sys.stderr)


def run(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run that uses text=True by default."""
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


# ── git helpers ──────────────────────────────────────────────────────────────

def is_git_repo(path: "Path") -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    )
    return result.returncode == 0


def _resolve_union_conflict(file_path: "Path") -> bool:
    """Resolve conflict markers in an append-only word-list file via union merge.

    Keeps all non-duplicate lines from both sides. Returns True if no conflict
    markers remain after resolution.
    """
    from pathlib import Path as _Path
    content = file_path.read_text(encoding="utf-8")  # type: ignore[arg-type]
    if "<<<<<<< " not in content:
        return True

    seen: set[str] = set()
    out: list[str] = []
    state = "normal"  # normal | ours | theirs

    for line in content.splitlines():
        if line.startswith("<<<<<<< "):
            state = "ours"
            continue
        if line == "=======":
            state = "theirs"
            continue
        if line.startswith(">>>>>>> "):
            state = "normal"
            continue
        # Deduplicate by the first token (the word itself); keep comment/blank lines as-is.
        if line and not line.startswith("#") and not line.startswith(" "):
            key = line.split(maxsplit=1)[0]
            if key in seen:
                continue
            seen.add(key)
        out.append(line)

    resolved = "\n".join(out)
    if not resolved.endswith("\n"):
        resolved += "\n"
    file_path.write_text(resolved, encoding="utf-8")  # type: ignore[arg-type]
    return "<<<<<<< " not in resolved


def git_sync(repo_dir: "Path", file_path: "Path", commit_msg: str) -> list[str]:
    """Commit file_path, pull --rebase (auto-resolving union conflicts), then push.

    Order: add → commit → pull --rebase → push.

    On conflict: if only file_path conflicts, resolves via union merge and
    continues the rebase. If other files conflict or resolution fails, aborts
    the rebase and prints instructions for manual recovery.

    Returns a list of completed step descriptions, stopping at the first failure.
    """
    import os as _os

    def _run(args: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {**_os.environ, **(extra_env or {})}
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    done: list[str] = []

    _run(["add", str(file_path)])
    commit = _run(["commit", "-m", commit_msg])
    if commit.returncode != 0:
        print(f"❌ git commit failed: {commit.stderr.strip()}")
        return done
    done.append("git commit")

    # Attempt rebase pull, resolving union conflicts on file_path if needed.
    for attempt in range(10):  # guard against infinite rebase loops
        if attempt == 0:
            result = _run(["pull", "--rebase"])
        else:
            result = _run(["rebase", "--continue"], extra_env={"GIT_EDITOR": "true"})

        if result.returncode == 0:
            done.append("git pull --rebase")
            break

        # Check which files are conflicted.
        unmerged = _run(["diff", "--name-only", "--diff-filter=U"])
        conflicted = [f.strip() for f in unmerged.stdout.splitlines() if f.strip()]

        try:
            rel_file = str(file_path.relative_to(repo_dir))  # type: ignore[attr-defined]
        except ValueError:
            rel_file = str(file_path)

        if conflicted != [rel_file]:
            _run(["rebase", "--abort"])
            others = [f for f in conflicted if f != rel_file]
            print(
                f"❌ Conflict in unexpected file(s): {others or conflicted}. "
                "Rebase aborted — please resolve manually and push."
            )
            return done

        if not _resolve_union_conflict(file_path):  # type: ignore[arg-type]
            _run(["rebase", "--abort"])
            print(
                f"❌ Union merge could not fully resolve conflicts in {rel_file}. "
                "Rebase aborted — please resolve manually and push."
            )
            return done

        _run(["add", str(file_path)])
        done.append("🔀 conflict auto-resolved")
    else:
        _run(["rebase", "--abort"])
        print("❌ Rebase loop exceeded limit. Aborted — please pull and push manually.")
        return done

    push = _run(["push"])
    if push.returncode != 0:
        print(f"❌ git push failed: {push.stderr.strip()}")
        return done
    done.append("git push")

    return done
