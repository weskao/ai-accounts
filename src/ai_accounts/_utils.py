"""Shared helpers for ai-accounts CLI tools.

Cross-platform: macOS, Windows, and Linux. OS-specific credential stores,
notifications, package installation, and ANSI colors are funnelled through this module.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from functools import lru_cache
from importlib import metadata
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


# Enable VT once at import time so ANSI output renders on Windows terminals.
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


def package_version() -> str:
    """Installed ai-accounts version, from package metadata (not source checkout)."""
    return metadata.version("ai-accounts")


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
    """Resolve an ai-accounts-owned data directory, migrating its legacy location.

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
    "codex": {
        "darwin": "npm install -g @openai/codex",
        "linux": "npm install -g @openai/codex",
        "win32": "npm install -g @openai/codex",
    },
    "secret-tool": {  # libsecret CLI — reaches Secret Service credential slots
        "darwin": "brew install libsecret",
        "linux": "sudo apt install libsecret-tools   (or: sudo dnf install libsecret / sudo pacman -S libsecret)",
        "win32": "not needed on Windows — Credential Manager is built in",
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


# ── desktop notifications ────────────────────────────────────────────────────

def _osa_string(text: str) -> str:
    """*text* as an AppleScript string literal (quotes/backslashes escaped)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ps_string(text: str) -> str:
    """*text* as a PowerShell single-quoted literal (``'`` doubled)."""
    return "'" + text.replace("'", "''") + "'"


# Toast via WinRT, which every Windows 10+ box has — no module to install.
#
# The audio element is explicit rather than left to the template default: a
# toast with no <audio> child inherits whatever the system theme decides, and
# on some builds that is silence. `Notification.Default` is the short chime
# that matches macOS's Glass, and `silent="true"` is how the caller mutes it.
#
# No `launch` attribute and no `<action>` children, deliberately: a toast
# without them does nothing when clicked. Adding either would make the click
# activate an AppUserModelID (and Windows would fall back to the Start menu,
# since `ai-accounts` is not a registered app).
_PS_TOAST = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode({title})) > $null
$n.Item(1).AppendChild($t.CreateTextNode({message})) > $null
$a = $t.CreateElement('audio')
$a.SetAttribute('src', 'ms-winsoundevent:Notification.Default')
$a.SetAttribute('silent', {silent})
$t.DocumentElement.AppendChild($a) > $null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ai-accounts').Show(
    [Windows.UI.Notifications.ToastNotification]::new($t))
"""

# macOS: the notification's sound, by system-sound NAME rather than by file
# path — `display notification ... sound name "Glass"` plays
# /System/Library/Sounds/Glass.aiff without this code knowing that path, and
# without spawning a second process the way a separate `afplay` would.
_MACOS_SOUND = "Glass"

# Linux: an XDG sound-theme event id, not a file. `canberra-gtk-play -i` maps
# it to whatever the active theme ships, so no path is baked in here. Absent
# canberra, the notification is silent — most desktops play their own sound
# for notify-send anyway.
_LINUX_SOUND_EVENT = "complete"


def _osascript_notify(title: str, message: str, sound: bool) -> bool:
    """The pure-AppleScript path: no extra dependency, but NOT click-inert.

    ``display notification`` (with or without a ``tell application "System
    Events"`` wrapper — verified empirically, the wrapper does not change
    it) is attributed to Script Editor's identity in Notification Center
    regardless of which app the AppleScript is sent to, since the command is
    a Standard Additions scripting addition that runs in ``osascript``'s own
    process. Clicking the banner therefore opens Script Editor. This is a
    long-standing AppleScript limitation with no scripting-level fix — see
    :func:`_terminal_notifier_notify` for the one that actually is inert.
    """
    body = (
        f"display notification {_osa_string(message)} "
        f"with title {_osa_string(title)}"
    )
    if sound:
        body += f" sound name {_osa_string(_MACOS_SOUND)}"
    return _notify_run(["osascript", "-e", body])


def _terminal_notifier_group() -> str:
    """A group id unique enough that ``-list`` never confuses two calls."""
    return f"ai-accounts-{os.getpid()}-{time.perf_counter_ns()}"


def _terminal_notifier_delivered(group: str) -> bool:
    """Whether Notification Center actually recorded *group* as delivered.

    ``terminal-notifier -list <group>`` prints a header row always, plus one
    data row per delivered notification in that group — so more than one
    line means delivery happened. This reads the OS's own notification
    database (a public, documented feature of the tool), unlike guessing at
    the meaning of the private, undocumented ``com.apple.ncprefs.plist``
    bit flags, which is not something this reaches for.
    """
    result = _run_notifier(["terminal-notifier", "-list", group])
    if result is None:
        return False
    return len((result.stdout or "").splitlines()) > 1


def _run_notifier(cmd: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return run(cmd, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


def _terminal_notifier_notify(title: str, message: str, sound: bool) -> bool:
    """Send through ``terminal-notifier`` and confirm the OS actually showed it.

    Unlike plain ``osascript``, a terminal-notifier banner with no ``-open``/
    ``-execute``/``-activate`` genuinely does nothing when clicked — it is a
    real app bundle with its own identity, not an anonymous scripting
    addition borrowing Script Editor's. Delivery is verified with
    :func:`_terminal_notifier_delivered` because a System Settings toggle can
    silently swallow the notification while the process itself still exits 0;
    the polling window is short (three tries, 150ms apart) since a delivered
    notification showed up with no wait at all in practice, so the cost only
    shows up on the disabled path, which then falls back anyway.
    """
    group = _terminal_notifier_group()
    cmd = ["terminal-notifier", "-title", title, "-message", message, "-group", group]
    if sound:
        cmd += ["-sound", _MACOS_SOUND]
    if _run_notifier(cmd) is None:
        return False
    for _ in range(3):
        if _terminal_notifier_delivered(group):
            _run_notifier(["terminal-notifier", "-remove", group])
            return True
        time.sleep(0.15)
    _warn_terminal_notifier_disabled_once()
    return False


def _notify_hint_marker() -> Path:
    return Path.home() / ".ai-accounts" / "notify-hint-shown"


def _warn_terminal_notifier_disabled_once() -> None:
    """Tell the user once, ever, that terminal-notifier needs enabling.

    Not every call: the toggle lives in System Settings and does not flap
    from one notification to the next, so repeating this on every dead
    notification would just be noise.
    """
    marker = _notify_hint_marker()
    if marker.exists():
        return
    from . import i18n

    log_yellow(i18n.t("notify.terminal_notifier_hint"))
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass  # worst case the hint repeats; never worth failing a notification over


def desktop_notify(title: str, message: str, *, sound: bool = True) -> bool:
    """Show an OS desktop notification, with a sound (best-effort, all OSes).

    - macOS   → ``terminal-notifier`` when installed (genuinely click-inert,
      verified delivered), else plain ``osascript`` (click opens Script
      Editor — an AppleScript limitation, not something this can fix; see
      :func:`_osascript_notify`)
    - Linux   → ``notify-send``, plus ``canberra-gtk-play`` when present
    - Windows → PowerShell toast with an explicit notification sound

    Clicking the notification does nothing on Linux and Windows always, and
    on macOS whenever ``terminal-notifier`` is the one that delivered it.

    Returns True when the notification was handed to the OS. Never raises:
    a missing notifier or a failing command is just a False.
    """
    if IS_MACOS:
        if have("terminal-notifier") and _terminal_notifier_notify(title, message, sound):
            return True
        return _osascript_notify(title, message, sound)
    if IS_LINUX:
        if not have("notify-send"):
            return False
        shown = _notify_run(["notify-send", "--", title, message])
        if shown and sound and have("canberra-gtk-play"):
            # Best-effort: a themeless box still gets the notification.
            _notify_run(["canberra-gtk-play", "-i", _LINUX_SOUND_EVENT])
        return shown
    if IS_WINDOWS:
        script = _PS_TOAST.format(
            title=_ps_string(title),
            message=_ps_string(message),
            silent=_ps_string("false" if sound else "true"),
        )
        return _notify_run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        )
    return False


def _notify_run(cmd: Sequence[str]) -> bool:
    try:
        return run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False



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
             "-s", service, "-a", account, "-w"],
            input=secret + "\n",
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


# ── go-keyring credential slots ──────────────────────────────────────────────
# Go CLIs built on github.com/zalando/go-keyring — the `agy` CLI is one — keep
# their live session in the OS credential store, but each platform gets a
# different backend *and* a different encoding:
#
#   macOS    `security` generic-password (service/account verbatim), secret
#            base64-encoded behind a "go-keyring-base64:" marker.
#   Linux    Secret Service item matched on the attributes {service, username},
#            secret stored verbatim. Reached here through `secret-tool`.
#   Windows  Credential Manager generic credential targeted "<service>:<account>",
#            secret stored verbatim as UTF-8.
#
# The trio below hides that split: callers pass and receive the plaintext
# secret and never see the darwin marker. Deliberately separate from
# keychain_read/keychain_write above, which speak the plain macOS-only
# generic-password protocol that codex- and claude-accounts mirror into — those
# tools fall back to file storage off macOS on purpose, because the vendor CLI
# does too.

_GO_KEYRING_B64_PREFIX = "go-keyring-base64:"

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


@lru_cache(maxsize=1)
def _wincred_api():
    """Bind Credential Manager entry points. None off Windows or if unavailable."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        class _CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", _FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        advapi32.CredDeleteW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        return ctypes, advapi32, _CREDENTIALW
    except (ImportError, OSError, AttributeError):
        return None


def _wincred_target(service: str, account: str) -> str:
    """go-keyring's Windows target-name convention."""
    return f"{service}:{account}"


def _wincred_read(target: str) -> str | None:
    api = _wincred_api()
    if api is None:
        return None
    ctypes, advapi32, credentialw = api
    handle = ctypes.POINTER(credentialw)()
    if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(handle)):
        return None
    try:
        cred = handle.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        advapi32.CredFree(ctypes.cast(handle, ctypes.c_void_p))
    try:
        return blob.decode("utf-8") or None
    except UnicodeDecodeError:
        return None


def _wincred_write(target: str, account: str, secret: str) -> bool:
    api = _wincred_api()
    if api is None:
        return False
    ctypes, advapi32, credentialw = api
    blob = secret.encode("utf-8")
    buffer = ctypes.create_string_buffer(blob, len(blob))
    cred = credentialw()  # ctypes zero-initializes; only the used fields are set
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = account
    return bool(advapi32.CredWriteW(ctypes.byref(cred), 0))


def _wincred_delete(target: str) -> bool:
    api = _wincred_api()
    if api is None:
        return False
    _ctypes, advapi32, _credentialw = api
    return bool(advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0))


def _secret_tool(
    *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run `secret-tool`. None if the binary is absent or could not start."""
    if not have("secret-tool"):
        return None
    try:
        return subprocess.run(
            ["secret-tool", *args], input=stdin, capture_output=True, text=True
        )
    except OSError:
        return None


def go_keyring_available() -> tuple[bool, str]:
    """Can this platform reach a go-keyring slot? Second item is why not."""
    if IS_MACOS or IS_WINDOWS:
        return True, ""
    if IS_LINUX:
        if have("secret-tool"):
            return True, ""
        return False, _install_hint("secret-tool")
    return False, f"unsupported platform: {sys.platform}"


def go_keyring_read(service: str, account: str) -> str | None:
    """Read a go-keyring secret as plaintext. None if absent or unsupported."""
    if IS_MACOS:
        secret = keychain_read(service, account)
        if not secret:
            return None
        if not secret.startswith(_GO_KEYRING_B64_PREFIX):
            # go-keyring only adds the marker when it encodes; take the rest
            # verbatim rather than discarding a session we could still use.
            return secret
        try:
            decoded = base64.b64decode(
                secret.removeprefix(_GO_KEYRING_B64_PREFIX), validate=True
            )
            return decoded.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    if IS_LINUX:
        result = _secret_tool("lookup", "service", service, "username", account)
        if result is None or result.returncode != 0:
            return None
        return result.stdout.strip() or None
    if IS_WINDOWS:
        return _wincred_read(_wincred_target(service, account))
    return None


def go_keyring_write(service: str, account: str, secret: str) -> bool:
    """Create-or-replace a go-keyring secret. False if unsupported or on failure."""
    if IS_MACOS:
        encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        return keychain_write(service, account, _GO_KEYRING_B64_PREFIX + encoded)
    if IS_LINUX:
        # Writes land in the default collection, which on every mainstream
        # desktop is the same "login" keyring go-keyring itself opens. The label
        # is cosmetic — lookups match on the two attributes only.
        result = _secret_tool(
            "store",
            f"--label={service}/{account}",
            "service",
            service,
            "username",
            account,
            stdin=secret,
        )
        return result is not None and result.returncode == 0
    if IS_WINDOWS:
        return _wincred_write(_wincred_target(service, account), account, secret)
    return False


def go_keyring_delete(service: str, account: str) -> bool:
    """Remove a go-keyring secret. False if unsupported, absent, or on failure."""
    if IS_MACOS:
        try:
            result = subprocess.run(
                ["security", "delete-generic-password", "-s", service, "-a", account],
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        return result.returncode == 0
    if IS_LINUX:
        result = _secret_tool("clear", "service", service, "username", account)
        return result is not None and result.returncode == 0
    if IS_WINDOWS:
        return _wincred_delete(_wincred_target(service, account))
    return False


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
