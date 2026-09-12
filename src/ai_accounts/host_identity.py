"""Shared machine labels for automation notifications."""

import functools
import hmac
from pathlib import Path
import re
import socket
import subprocess
import sys
import uuid


@functools.cache
def host_name() -> str:
    try:
        result = subprocess.run(
            ["/usr/sbin/scutil", "--get", "ComputerName"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        stdout = (result.stdout or "").strip()
        if result.returncode == 0 and stdout:
            return stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    # scutil is macOS-only and unavailable in headless launchd runs (e.g. at
    # the lock screen); gethostname() is the cross-platform fallback, but on
    # any OS it can carry an mDNS ".local" suffix that isn't a display name.
    return (socket.gethostname() or "unknown-host").removesuffix(".local")


def host_emoji(name: str) -> str:
    return "🖥️" if "mini" in name.lower() else "💻"


def device_code() -> str:
    """macOS serial, or an app-specific OS identity digest; empty if unavailable."""
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                text=True, capture_output=True, timeout=5, check=False,
            )
            match = re.search(r'"IOPlatformSerialNumber"\s*=\s*"([A-Za-z0-9]{5,})"', result.stdout or "")
            return match.group(1) if result.returncode == 0 and match else ""
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return _identity_digest(value)
        if sys.platform.startswith("linux"):
            for filename in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    code = _identity_digest(Path(filename).read_text(encoding="ascii").strip())
                    if code:
                        return code
                except (OSError, UnicodeError):
                    continue
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ImportError):
        pass
    return ""


def _identity_digest(value: str) -> str:
    """Never expose the raw OS installation ID in notification labels."""
    try:
        identity = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return ""
    if identity.int == 0:
        return ""
    return hmac.new(identity.bytes, b"automation-notification-host", "sha256").hexdigest()[:12]


def masked_device_code() -> str:
    """Show only four characters; unavailable identities are explicitly unknown."""
    code = device_code()
    return f"{code[:4]}{'*' * (len(code) - 4)}" if code else "unknown"


def device_label() -> str:
    """Full display label for notifications, e.g. '🖥️ Mac mini · a1b2****'."""
    name = host_name()
    return f"{host_emoji(name)} {name} · {masked_device_code()}"


if __name__ == "__main__":
    # CLI entry point for the shell scripts (see notification_host_label in
    # lib/notification-utils.sh) so they never re-derive the name/emoji/id.
    print(host_name() if "--name" in sys.argv[1:] else device_label())
