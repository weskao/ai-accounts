"""Shared helpers for ai-accounts CLI tools.

Cross-platform: macOS, Windows, and Linux. Anything OS-specific (clipboard,
package installation, ANSI colors) is funnelled through this module so the
individual tools stay platform-agnostic.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Sequence

YELLOW = "\033[1;33m"
GREEN = "\033[1;32m"
RED = "\033[1;31m"
CYAN = "\033[1;36m"
BLUE = "\033[1;34m"
MAGENTA = "\033[1;35m"
BOLD = "\033[1m"
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


_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_FRAMES_ASCII = ("|", "/", "-", "\\")
_SPINNER_INTERVAL = 0.08


def _stderr_supports_braille() -> bool:
    """Some Windows consoles (legacy cmd.exe codepages) can't encode Braille."""
    encoding = getattr(sys.stderr, "encoding", None) or ""
    try:
        _SPINNER_FRAMES[0].encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


class Spinner:
    """Terminal spinner for a step that takes a moment (e.g. a network fetch).

    Ticks on a background thread and writes to stderr, so stdout stays clean
    for piping. Auto-disables when stderr isn't a TTY (piped/captured output,
    CI logs, tests) — reuses the same gate as ``log_*`` so it never corrupts
    non-interactive output. Update the label mid-run with ``update()``; the
    line is cleared on exit so following output starts at column 0.
    """

    def __init__(self, message: str = "Working…") -> None:
        self._message = message
        self._enabled = _color_supported()
        self._frames = _SPINNER_FRAMES if _stderr_supports_braille() else _SPINNER_FRAMES_ASCII
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def update(self, message: str) -> None:
        with self._lock:
            self._message = message

    def _run(self) -> None:
        frame = 0
        while not self._stop.is_set():
            with self._lock:
                message = self._message
            print(
                f"\r{CYAN}{self._frames[frame % len(self._frames)]}{RESET} {message}\033[K",
                end="",
                file=sys.stderr,
                flush=True,
            )
            frame += 1
            self._stop.wait(_SPINNER_INTERVAL)
        print("\r\033[K", end="", file=sys.stderr, flush=True)

    def __enter__(self) -> "Spinner":
        if self._enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()


def fetch_parallel(
    items: Sequence,
    fn: Callable,
    spinner: "Spinner | None" = None,
    message: str = "",
    labels: Sequence[str] | None = None,
) -> list:
    """Map ``fn`` over ``items`` concurrently, returning results in input order.

    Independent per-item network fetches (one usage call per account) run on a
    small thread pool instead of serially. When ``spinner`` is given, its label
    updates with a ``(done/total)`` counter as each item finishes; ``labels``
    (aligned with ``items``) adds the just-finished item's name to the label,
    matching the sequential tools' spinner format. Exceptions from ``fn``
    propagate — callers keep the sequential contract of returning
    error-carrying results rather than raising.
    """
    total = len(items)
    if total == 0:
        return []
    results: list = [None] * total
    with ThreadPoolExecutor(max_workers=min(total, 8)) as pool:
        futures = {pool.submit(fn, item): index for index, item in enumerate(items)}
        done = 0
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
            done += 1
            if spinner is not None:
                name = f" {MAGENTA}{labels[index]}{RESET}" if labels else ""
                spinner.update(f"{message} {DIM}({done}/{total}){RESET}{name}")
    return results


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def plan_tier_color(label: str, tiers: Sequence[str] = ()) -> str:
    """ANSI color for a paid subscription-plan label, escalating with rank.

    ``tiers`` lists known paid tier names low → high (case-insensitive
    substring match against ``label``); an unrecognized label still gets the
    top color, since a novel plan name is at least as likely to be a new
    top-end tier as a starter one. Pass no ``tiers`` for a provider whose paid
    tier names aren't enumerable — every paid label then gets a single top
    accent rather than a fabricated rank. Callers decide when to skip this
    entirely (e.g. the free tier, which stays uncolored).
    """
    palette = (CYAN, BLUE, MAGENTA)
    lowered = label.lower()
    for i, tier in enumerate(tiers):
        if tier in lowered:
            return palette[min(i, len(palette) - 1)]
    return palette[-1]


# ── account-tool profile stores ──────────────────────────────────────────────

def resolve_data_dir(
    env_var: str, default_dir: Path, legacy_dir: Path, *, label: str = "profile store"
) -> Path:
    """Resolve a ai-accounts-owned data directory, migrating its legacy location.

    Precedence: ``$<env_var>`` override → *default_dir*. The default lives
    under ``~/.ai-accounts/`` — outside the app dotdirs (``~/.claude``,
    ``~/.codex``) — so account snapshots and their state stay out of dotfiles
    repositories. A store still at *legacy_dir* is moved to *default_dir* on
    first use, with a one-line notice.
    """
    override = os.environ.get(env_var)
    if override:
        return Path(override)
    if not default_dir.exists() and legacy_dir.is_dir():
        default_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_dir), str(default_dir))
        log_yellow(f"→ Moved {label}: {legacy_dir} → {default_dir}")
    return default_dir


def resolve_account_dir(env_var: str, default_dir: Path, legacy_dir: Path) -> Path:
    """Resolve an account-profile directory, migrating its legacy location."""
    return resolve_data_dir(env_var, default_dir, legacy_dir)


def email_local_part(email: str) -> str:
    """The part of an email address before ``@`` (used as a display fallback
    when a profile has no name/label)."""
    return email.split("@", 1)[0]


# ── dependency management ────────────────────────────────────────────────────

# Per-platform install instructions for the external binaries ai-accounts shells
# out to. Keyed by the package name passed to ``ensure_tool``. The macOS column
# is also used to drive Homebrew auto-install (preserving the original behavior).
_INSTALL_HINTS: dict[str, dict[str, str]] = {
    "claude": {
        "darwin": "curl -fsSL https://claude.ai/install.sh | bash   (or: npm install -g @anthropic-ai/claude-code)",
        "linux": "curl -fsSL https://claude.ai/install.sh | bash   (or: npm install -g @anthropic-ai/claude-code)",
        "win32": "npm install -g @anthropic-ai/claude-code",
    },
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


# ── desktop notifications ────────────────────────────────────────────────────

def _osa_string(text: str) -> str:
    """*text* as an AppleScript string literal (quotes/backslashes escaped)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ps_string(text: str) -> str:
    """*text* as a PowerShell single-quoted literal (``'`` doubled)."""
    return "'" + text.replace("'", "''") + "'"


# Toast via WinRT, which every Windows 10+ box has — no module to install.
_PS_TOAST = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode({title})) > $null
$n.Item(1).AppendChild($t.CreateTextNode({message})) > $null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ai-accounts').Show(
    [Windows.UI.Notifications.ToastNotification]::new($t))
"""


def desktop_notify(title: str, message: str) -> bool:
    """Show an OS desktop notification (best-effort, cross-platform).

    - macOS   → ``osascript`` display notification
    - Linux   → ``notify-send``
    - Windows → PowerShell toast

    Returns True when the notification was handed to the OS. Never raises:
    a missing notifier or a failing command is just a False.
    """
    if IS_MACOS:
        script = (
            f"display notification {_osa_string(message)} "
            f"with title {_osa_string(title)}"
        )
        return _notify_run(["osascript", "-e", script])
    if IS_LINUX:
        if not have("notify-send"):
            return False
        return _notify_run(["notify-send", "--", title, message])
    if IS_WINDOWS:
        script = _PS_TOAST.format(title=_ps_string(title), message=_ps_string(message))
        return _notify_run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        )
    return False


def _notify_run(cmd: Sequence[str]) -> bool:
    try:
        return run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def output_and_copy(text: str) -> None:
    """Print to stdout, copy to clipboard, and announce on stderr."""
    print(text)
    if copy_to_clipboard(text):
        print("\n✅ Copied to clipboard", file=sys.stderr)


def run(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """Thin wrapper around subprocess.run that uses text=True by default."""
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


# ── macOS keychain ───────────────────────────────────────────────────────────
# Several CLIs keep their live credential in the login keychain rather than (or
# in addition to) a dotfile, so a switch that only rewrites the file is silently
# ignored. codex-, claude- and vibe-accounts all need the same two primitives;
# only the service/account naming differs, so it lives here once.

def keychain_read(service: str, account: str) -> str | None:
    """Read a generic-password secret. None off macOS or if absent/unreadable."""
    if not IS_MACOS:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    secret = (result.stdout or "").strip()
    if not secret:
        return None
    # `security -w` hex-encodes secrets containing bytes it deems "non-clean"
    # (e.g. newlines). Decode that back to the original text.
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", secret):
        try:
            secret = bytes.fromhex(secret).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
    return secret


def keychain_write(service: str, account: str, secret: str) -> bool:
    """Create-or-update a generic-password item. False off macOS or on failure."""
    if not IS_MACOS:
        return False
    try:
        result = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", service, "-a", account, "-w", secret],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


# ── credential files ─────────────────────────────────────────────────────────

def atomic_write_json(path: Path, payload, *, indent: int = 2, mode: int = 0o600) -> None:
    """Write `payload` as JSON to `path` atomically, 0600 by default.

    A same-directory temp file is filled, chmod'd and `replace`d onto the
    target, so a crash mid-write — or a vendor CLI reading the same credential
    concurrently — never sees a truncated file. Raises OSError on failure,
    after removing the temp file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=indent)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.chmod(mode)
        temporary.replace(path)
        path.chmod(mode)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


# ── OAuth token refresh ──────────────────────────────────────────────────────

def oauth_token_refresh(
    url: str,
    payload: dict,
    *,
    form_encoded: bool = False,
    headers: dict | None = None,
    timeout: int = 30,
    http_error: Callable[[int, str], str] | None = None,
) -> tuple[dict | None, str | None]:
    """POST an OAuth refresh_token grant. Returns (response, None) on success,
    (None, error message) on failure — never raises, never logs tokens.

    Only the transport is shared. What an HTTP status *means* is per provider
    (a bare 403 is a revoked token for one and a Cloudflare block for another),
    so the caller passes `http_error`: it receives (status code, response body)
    and returns the message its own `_is_revoked_error` then judges as
    revoked-vs-transient. Network failures are always transient.
    """
    # urllib.request drags in http.client/ssl/email — keep it off the import
    # path of the tools that never refresh a token.
    import urllib.error
    import urllib.parse
    import urllib.request

    if form_encoded:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": content_type, **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:  # subclass of URLError — must come first
        if http_error is None:
            return None, f"HTTP {exc.code} from token endpoint"
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return None, http_error(exc.code, body)
    except urllib.error.URLError as exc:
        return None, f"network error: {exc.reason}"
    except Exception as exc:  # malformed JSON response, etc.
        return None, str(exc)


# ── git helpers ──────────────────────────────────────────────────────────────

def is_git_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    )
    return result.returncode == 0


def _resolve_union_conflict(file_path: Path) -> bool:
    """Resolve conflict markers in an append-only word-list file via union merge.

    Keeps all non-duplicate lines from both sides. Returns True if no conflict
    markers remain after resolution.
    """
    content = file_path.read_text(encoding="utf-8")
    if "<<<<<<< " not in content:
        return True

    seen: set[str] = set()
    out: list[str] = []

    for line in content.splitlines():
        if line.startswith("<<<<<<< "):
            continue
        if line == "=======":
            continue
        if line.startswith(">>>>>>> "):
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
    file_path.write_text(resolved, encoding="utf-8")
    return "<<<<<<< " not in resolved


def git_sync(repo_dir: Path, file_path: Path, commit_msg: str) -> list[str]:
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
