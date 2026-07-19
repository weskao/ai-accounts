from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Final, TypeAlias
import urllib.error
import urllib.request

from .codex_usage import UsageWindow, format_unix_time_compact

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonDict: TypeAlias = dict[str, JsonValue]

_LOAD_CODE_ASSIST_URL: Final = (
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
)
_QUOTA_URL: Final = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    pro: UsageWindow | None
    flash: UsageWindow | None
    flash_lite: UsageWindow | None
    refreshed_at: int | None
    error: str | None


def _load_auth(path: Path) -> JsonDict | None:
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return auth if isinstance(auth, dict) else None


def _post(access_token: str, url: str, payload: JsonDict) -> JsonDict | str:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError):
        return "network error"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid response"
    return result if isinstance(result, dict) else "invalid response"


def _project_id(access_token: str) -> str | None:
    result = _post(
        access_token,
        _LOAD_CODE_ASSIST_URL,
        {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
    )
    if not isinstance(result, dict):
        return None
    project = result.get("cloudaicompanionProject")
    if isinstance(project, str):
        return project.strip() or None
    if isinstance(project, dict):
        for key in ("id", "projectId"):
            value = project.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _reset_time(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _tier(model_id: str) -> str | None:
    model_id = model_id.lower()
    if "flash-lite" in model_id:
        return "flash_lite"
    if "flash" in model_id:
        return "flash"
    if "pro" in model_id:
        return "pro"
    return None


def _parse_quota(payload: JsonDict) -> dict[str, UsageWindow]:
    windows: dict[str, UsageWindow] = {}
    buckets = payload.get("buckets")
    if not isinstance(buckets, list):
        return windows
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        model_id = bucket.get("modelId")
        remaining = bucket.get("remainingFraction")
        if (
            not isinstance(model_id, str)
            or isinstance(remaining, bool)
            or not isinstance(remaining, int | float)
        ):
            continue
        tier = _tier(model_id)
        if tier is None:
            continue
        used = round((1 - max(0.0, min(1.0, float(remaining)))) * 100)
        window = UsageWindow(
            percentage=used,
            reset_time=_reset_time(bucket.get("resetTime")),
            window_minutes=1440,
        )
        if tier not in windows or used > windows[tier].percentage:
            windows[tier] = window
    return windows


def fetch_usage(auth_path: Path) -> UsageSnapshot:
    auth = _load_auth(auth_path)
    if auth is None:
        return UsageSnapshot(None, None, None, None, "unreadable auth")
    access_token = auth.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return UsageSnapshot(None, None, None, None, "missing access token")

    project = _project_id(access_token)
    result = _post(access_token, _QUOTA_URL, {"project": project} if project else {})
    if isinstance(result, str):
        return UsageSnapshot(None, None, None, None, result)
    windows = _parse_quota(result)
    if not windows:
        return UsageSnapshot(None, None, None, None, "no quota buckets")
    return UsageSnapshot(
        windows.get("pro"),
        windows.get("flash"),
        windows.get("flash_lite"),
        int(time.time()),
        None,
    )


def format_refreshed_at(snapshot: UsageSnapshot) -> str:
    if snapshot.error:
        if snapshot.error.startswith("HTTP "):
            return "ERR " + snapshot.error.split()[1]
        if snapshot.error in ("unreadable auth", "missing access token"):
            return "ERR auth"
        if snapshot.error == "network error":
            return "ERR network"
        return "ERR usage"
    return format_unix_time_compact(snapshot.refreshed_at)
