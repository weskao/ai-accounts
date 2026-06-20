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
}


def _install_hint(pkg: str) -> str:
    by_os = _INSTALL_HINTS.get(pkg, {})
    key = "darwin" if IS_MACOS else "win32" if IS_WINDOWS else "linux"
    return by_os.get(key) or f"install '{pkg}' using your platform's package manager"


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
        log_yellow(f"⚠️  未偵測到 {bin_name},嘗試透過 Homebrew 安裝 {pkg}...")
        if have("brew"):
            res = subprocess.run(["brew", "install", pkg])
            if res.returncode == 0 and have(bin_name):
                return True
            log_red(f"❌ brew install {pkg} 失敗")
        else:
            log_red("❌ 找不到 Homebrew,請先安裝:https://brew.sh")

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
