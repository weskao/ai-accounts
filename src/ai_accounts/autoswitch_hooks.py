"""Install lifecycle hooks that dispatch to the shared autoswitch engine."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias, cast

from . import autoswitch
from . import _utils as u
from ._utils import log_yellow

MANAGED_HOOK = "ai-accounts-autoswitch"
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
JsonList: TypeAlias = list[JsonValue]
_MODULES = {
    "codex": "ai_accounts.codex_accounts",
    "claude": "ai_accounts.claude_accounts",
    "agy": "ai_accounts.gemini_accounts",
}


def command(provider: str) -> str:
    """The stable, shell-safe command installed in a provider's Stop hook."""
    return shlex.join([sys.executable, "-m", "ai_accounts.autoswitch_hooks", "run", provider])


def module(provider: str) -> str:
    """Existing provider CLI module for a quota-aware hook provider."""
    return _MODULES[provider]


def _paths() -> dict[str, Path]:
    return {
        "codex": Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json",
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        / "settings.json",
        "agy": Path(os.environ.get("GEMINI_HOME", Path.home() / ".gemini"))
        / "config"
        / "hooks.json",
    }


def providers() -> tuple[str, ...]:
    """Quota-aware CLIs that can receive a Stop hook on this platform."""
    return ("codex", "claude", "agy") if u.IS_MACOS else ("codex", "claude")


def _load(path: Path) -> JsonObject:
    if not path.exists():
        return {}
    try:
        data = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot merge autoswitch hook into {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"cannot merge autoswitch hook into {path}: expected a JSON object")
    return cast(JsonObject, data)


def _save(path: Path, data: JsonObject) -> None:
    autoswitch._write_private(path, json.dumps(data, indent=2) + "\n")  # pyright: ignore[reportPrivateUsage]  # canonical atomic 0600 writer


def _has_command(groups: JsonList, value: str) -> bool:
    for group in groups:
        handlers = _handlers(group)
        if handlers and any(
            isinstance(hook, dict) and hook.get("command") == value for hook in handlers
        ):
            return True
    return False


def _handlers(group: JsonValue) -> JsonList | None:
    if not isinstance(group, dict):
        return None
    value = group.get("hooks")
    return value if isinstance(value, list) else None


def _install_grouped_stop(data: JsonObject, provider: str) -> bool:
    hooks_value = data.get("hooks")
    if hooks_value is None:
        hooks: JsonObject = {}
        data["hooks"] = hooks
    elif not isinstance(hooks_value, dict):
        raise ValueError("hooks must be a JSON object")
    else:
        hooks = hooks_value
    stop_value = hooks.get("Stop")
    if stop_value is None:
        stop: JsonList = []
        hooks["Stop"] = stop
    elif not isinstance(stop_value, list):
        raise ValueError("hooks.Stop must be a JSON array")
    else:
        stop = stop_value
    value = command(provider)
    if _has_command(stop, value):
        return False
    stop.append({"hooks": [{"type": "command", "command": value, "timeout": 30}]})
    return True


def _remove_grouped_stop(data: JsonObject, provider: str) -> bool:
    hooks_value = data.get("hooks")
    if not isinstance(hooks_value, dict):
        return False
    stop_value = hooks_value.get("Stop")
    if not isinstance(stop_value, list):
        return False
    value = command(provider)
    kept: JsonList = []
    changed = False
    for group in stop_value:
        handlers = _handlers(group)
        if handlers is None:
            kept.append(group)
            continue
        remaining = [
            hook
            for hook in handlers
            if not (isinstance(hook, dict) and hook.get("command") == value)
        ]
        changed |= len(remaining) != len(handlers)
        if remaining:
            assert isinstance(group, dict)
            kept.append({**group, "hooks": remaining})
    if changed:
        hooks_value["Stop"] = kept
    return changed


def _install_agy_stop(data: JsonObject) -> bool:
    value: JsonObject = {
        "Stop": [{"type": "command", "command": command("agy"), "timeout": 30}]
    }
    current = data.get(MANAGED_HOOK)
    if current == value:
        return False
    if current is not None:
        raise ValueError(f"{MANAGED_HOOK!r} is reserved for ai-accounts")
    data[MANAGED_HOOK] = value
    return True


def _remove_agy_stop(data: JsonObject) -> bool:
    if MANAGED_HOOK not in data:
        return False
    del data[MANAGED_HOOK]
    return True


def _grouped_update(
    provider: str, change: Callable[[JsonObject, str], bool]
) -> Callable[[JsonObject], bool]:
    def update(data: JsonObject) -> bool:
        return change(data, provider)

    return update


def _grouped_stop_installed(data: JsonObject, provider: str) -> bool:
    hooks = data.get("hooks")
    stop = hooks.get("Stop") if isinstance(hooks, dict) else None
    return isinstance(stop, list) and _has_command(stop, command(provider))


def is_installed() -> bool:
    """Whether every hook relevant to this OS is present, without writing."""
    paths = _paths()
    try:
        for provider in providers():
            data = _load(paths[provider])
            if provider == "agy":
                if MANAGED_HOOK not in data:
                    return False
            elif not _grouped_stop_installed(data, provider):
                return False
    except (OSError, ValueError):
        return False
    return True


def _change(path: Path, update: Callable[[JsonObject], bool]) -> None:
    try:
        data = _load(path)
        if update(data):
            _save(path, data)
    except (OSError, ValueError) as exc:
        # A hook must never make a successful config edit look failed or block
        # a provider's agent loop; the low-frequency timer remains the fallback.
        log_yellow(f"⚠️  Could not update autoswitch hook: {exc}")


def install() -> None:
    """Merge one Stop hook into each CLI with a usable quota API."""
    paths = _paths()
    for provider in providers():
        update = (
            _install_agy_stop
            if provider == "agy"
            else _grouped_update(provider, _install_grouped_stop)
        )
        _change(paths[provider], update)


def uninstall() -> None:
    """Remove only hooks owned by ai-accounts, leaving every user hook intact."""
    paths = _paths()
    for provider in providers():
        update = (
            _remove_agy_stop
            if provider == "agy"
            else _grouped_update(provider, _remove_grouped_stop)
        )
        _change(paths[provider], update)


def main(argv: list[str] | None = None) -> int:
    """Hook command: run one existing provider check and emit hook JSON."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "run" or args[1] not in _MODULES:
        return 2
    try:
        _ = subprocess.run(
            [sys.executable, "-m", _MODULES[args[1]], "autoswitch"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass
    # All three CLI hook protocols require a JSON object; never disrupt an
    # agent turn if an optional quota probe or account switch fails.
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
