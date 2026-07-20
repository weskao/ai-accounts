from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import threading
import time
from typing import TypeAlias
import urllib.request

if os.name == "nt":
    fcntl = None
    pty = None
    termios = None
else:
    import fcntl
    import pty
    import termios

from .usage_format import UsageWindow, format_unix_time_compact

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonDict: TypeAlias = dict[str, JsonValue]

_SERVICE = "/exa.language_server_pb.LanguageServerService/"


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    gemini_weekly: UsageWindow | None
    gemini_session: UsageWindow | None
    other_weekly: UsageWindow | None
    other_session: UsageWindow | None
    email: str | None
    plan: str | None
    refreshed_at: int | None
    error: str | None


def _reset_time(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _window(bucket: JsonDict) -> UsageWindow | None:
    remaining = bucket.get("remainingFraction")
    if isinstance(remaining, bool) or not isinstance(remaining, int | float):
        nested = bucket.get("remaining")
        remaining = nested.get("remainingFraction") if isinstance(nested, dict) else None
    if isinstance(remaining, bool) or not isinstance(remaining, int | float):
        return None
    used = round((1 - max(0.0, min(1.0, float(remaining)))) * 100)
    bucket_id = str(bucket.get("bucketId", "")).lower()
    display_name = str(bucket.get("displayName", "")).lower()
    window_minutes = 7 * 24 * 60 if "week" in bucket_id + display_name else 5 * 60
    return UsageWindow(
        percentage=used,
        reset_time=_reset_time(bucket.get("resetTime")),
        window_minutes=window_minutes,
    )


def _parse_summary(payload: JsonDict) -> tuple[UsageWindow | None, ...]:
    root = payload.get("response") or payload.get("summary") or payload
    groups = root.get("groups") if isinstance(root, dict) else None
    result: list[UsageWindow | None] = [None, None, None, None]
    if not isinstance(groups, list):
        return tuple(result)
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("displayName", "")).lower()
        family = 0 if "gemini" in name else 2
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            window = _window(bucket)
            if window is None:
                continue
            index = family if (window.window_minutes or 0) >= 7 * 24 * 60 else family + 1
            result[index] = window
    return tuple(result)


def _plan_label(status: JsonDict) -> str | None:
    """Real subscription tier for the PLAN column.

    Antigravity's free preview grants Pro *features* to everyone, so
    ``planInfo.planName``/``teamsTier`` are both "Pro" regardless of billing.
    ``userStatus.userTier`` is the actual subscription (``id="free-tier"``,
    ``name="Antigravity Starter Quota"`` for free; a distinct object for paid).
    """
    tier = status.get("userTier")
    if isinstance(tier, dict):
        if tier.get("id") == "free-tier":
            return "Free"
        # ponytail: show the paid tier's name verbatim (e.g. "Google AI Pro") —
        # no id→label map, since we can't verify paid-account shapes.
        name = tier.get("name")
        if isinstance(name, str) and name:
            return name
        tier_id = tier.get("id")
        if isinstance(tier_id, str) and tier_id:
            return tier_id
    # Fall back to planName only when userTier is absent/malformed.
    plan_status = status.get("planStatus")
    plan_info = plan_status.get("planInfo") if isinstance(plan_status, dict) else None
    plan = plan_info.get("planName") if isinstance(plan_info, dict) else None
    return plan if isinstance(plan, str) else None


def _identity(payload: JsonDict) -> tuple[str | None, str | None]:
    status = payload.get("userStatus")
    if not isinstance(status, dict):
        return None, None
    email = status.get("email")
    return (
        email if isinstance(email, str) else None,
        _plan_label(status),
    )


def _ports(pid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    ports: list[int] = []
    for line in result.stdout.splitlines()[1:]:
        match = re.search(r":(\d+)\s+\(LISTEN\)$", line)
        if match is None:
            continue
        port = int(match.group(1))
        if port not in ports:
            ports.append(port)
    return ports


def _post(port: int, method: str) -> JsonDict | None:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for scheme in ("https", "http"):
        request = urllib.request.Request(
            f"{scheme}://127.0.0.1:{port}{_SERVICE}{method}",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2, context=context) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, ValueError):
            continue
    return None


def _drain(fd: int, output: bytearray) -> None:
    try:
        while chunk := os.read(fd, 65536):
            output.extend(chunk)
            del output[:-8192]
    except OSError:
        pass


def _open_pty() -> tuple[int, int]:
    if pty is None or fcntl is None or termios is None:
        raise RuntimeError("POSIX pseudo-terminal support is unavailable")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
    return master, slave


def fetch_usage_from_pid(pid: int) -> UsageSnapshot | None:
    summary = status = None
    for port in _ports(pid):
        summary = summary or _post(port, "RetrieveUserQuotaSummary")
        status = status or _post(port, "GetUserStatus")
    if summary is None or status is None:
        return None
    gemini_weekly, gemini_session, other_weekly, other_session = _parse_summary(summary)
    email, plan = _identity(status)
    return UsageSnapshot(
        gemini_weekly,
        gemini_session,
        other_weekly,
        other_session,
        email,
        plan,
        int(time.time()),
        None,
    )


def fetch_usage(timeout: float = 15) -> UsageSnapshot:
    if os.name == "nt":
        return UsageSnapshot(
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "agy usage inspection requires macOS or Linux",
        )
    binary = os.environ.get("ANTIGRAVITY_CLI_PATH") or shutil.which("agy")
    if not binary:
        return UsageSnapshot(None, None, None, None, None, None, None, "agy not found")

    master, slave = _open_pty()
    process = subprocess.Popen(
        [binary],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    drain = threading.Thread(target=_drain, args=(master, output), daemon=True)
    drain.start()
    deadline = time.monotonic() + timeout
    usage = None
    try:
        while time.monotonic() < deadline and process.poll() is None:
            usage = fetch_usage_from_pid(process.pid)
            if usage is not None:
                break
            time.sleep(0.25)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        os.close(master)

    if usage is None:
        if b"Select login method:" in output:
            return UsageSnapshot(
                None, None, None, None, None, None, None, "re-login required"
            )
        return UsageSnapshot(None, None, None, None, None, None, None, "agy unavailable")
    return usage


def format_refreshed_at(snapshot: UsageSnapshot) -> str:
    if snapshot.error:
        if snapshot.error == "re-login required":
            return "RELOGIN"
        return "ERR agy"
    return format_unix_time_compact(snapshot.refreshed_at)
