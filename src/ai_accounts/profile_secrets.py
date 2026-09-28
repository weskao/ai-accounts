"""Saved-profile secrets in the OS credential store, with a plaintext fallback.

Every account tool keeps profiles as ``<root>/<tool>/accounts/<name>.json``.
This module splits such a profile in two: the *secret* fields (access /
refresh / id tokens) go to the OS credential store, and the file keeps only
metadata plus a marker block. Tools name their secret fields as dotted paths,
so nested shapes work (codex ``tokens.refresh_token``, claude
``claudeAiOauth.refreshToken``).

Usage::

    from ai_accounts import profile_secrets as ps

    FIELDS = ("tokens.refresh_token", "tokens.access_token", "tokens.id_token")
    ps.save(path, "codex", auth, FIELDS)       # True = secrets are in the store
    auth = ps.load(path, "codex", FIELDS)      # full dict (migrates inline files)
    meta = ps.load_metadata(path)              # file only, zero store reads
    if meta and meta.get(ps.SECRETS_KEY, {}).get("fingerprint") == ps.fingerprint(live_token):
        ...                                    # active-profile match, no store read
    ps.delete(path, "codex")
    ps.backup("codex", auth_json_text); ps.read_backup("codex")

On-disk marker (written by :func:`save` and by migration)::

    "_secrets": {"store": "ai-accounts", "key": "codex--work",
                 "has_refresh_token": true, "fingerprint": "<16 hex>"}

``fingerprint`` is :func:`fingerprint` of the FIRST non-empty string secret in
*secret_fields* order — list the identity-bearing token (the refresh token)
first. ``has_refresh_token`` is true when any secret whose last path segment
is ``refresh_token``/``refreshToken`` was non-empty. Expiry timestamps are not
secrets and stay in the file, so ``doctor`` can still judge "expired" from
metadata alone. A file without the block is a plaintext profile.

Policy: FALLBACK, not refuse. No store (headless Linux without
``secret-tool``) or a failed store write → the full payload is written as
before (0600) with one yellow warning per process. Refusing would break
existing headless users.

Accepted debt: ``secrets_store.py`` + ``_utils.go_keyring_*`` already hold the
Telegram bot token with a REFUSE-plaintext policy and Windows ``CredWriteW``,
whose ~2.5 KB blob cap is too small for codex profiles (~4 KB). Profiles use
``telegram_kit.CredentialStore`` (macOS ``security -i``, Linux ``secret-tool``,
Windows DPAPI files) instead. Two store stacks until someone unifies them.

macOS caveat: ``security -i`` batch mode drops lines over ~4 KB, i.e. secrets
over ~2 KB (a failed write even leaves a truncated item). Values are therefore
split into ``_KEYCHAIN_CHUNK``-sized parts on the keychain backend only; every
write is read back and compared before the file loses its plaintext.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path

import telegram_kit

from . import _utils as u

SERVICE = "ai-accounts"
SECRETS_KEY = "_secrets"
_REFRESH_NAMES = frozenset({"refresh_token", "refreshToken"})  # mirrors doctor._REFRESH_TOKEN_KEYS
_KEYCHAIN_CHUNK = 1500  # probed: 1900 B still round-trips with a 130-char account name


def _root() -> Path:
    """The central ``~/.ai-accounts`` dir (tests swap this function)."""
    return Path.home() / ".ai-accounts"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


# The ONE store handle; tests swap `_store` and `_available`, never telegram_kit.
_store = telegram_kit.CredentialStore(SERVICE, dpapi_dir=lambda: _private_dir(_root() / "dpapi"))
_available = telegram_kit.available

# Memo of raw store reads, key -> value ("" = absent). Invalidation rule: only
# this process's _put/_drop change an entry; writes by another process during
# this one are not seen — fine for short-lived CLI/timer runs, and it saves a
# ~30-50 ms subprocess per repeated read while autoswitch probes profiles.
_cache: dict[str, str] = {}
_warned = False


def _chunk_size() -> int:
    return _KEYCHAIN_CHUNK if telegram_kit.backend() == "keychain" else 0


# ── keys and fingerprints ────────────────────────────────────────────────────

def store_key(tool: str, stem: str) -> str:
    """``codex--work``: a store account name limited to ``[A-Za-z0-9_-]``.

    Names that needed sanitising (``a.b``, ``測試``) get a short hash suffix, so
    ``a.b`` and ``a_b`` never share a slot.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", stem)
    if safe != stem:
        safe += "-" + hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
    # ponytail: a clean stem that literally ends in "-<8 hex>" could still collide
    # with a sanitised one; profile filenames are already [A-Za-z0-9._-], add a
    # key->path index if that ever happens.
    return f"{tool}--{safe}"


def _backup_key(tool: str) -> str:
    return f"backup--{tool}"  # profile keys start with "<tool>--", never "backup--"


def _part_key(key: str, index: int) -> str:
    return f"part{index}--{key}"


def fingerprint(secret_text: str) -> str:
    """Short sha256 hex of *secret_text* — safe to keep in metadata."""
    return hashlib.sha256(secret_text.encode("utf-8")).hexdigest()[:16]


# ── raw store I/O (memoised, chunked, verified) ──────────────────────────────

def _get(key: str) -> str:
    if key not in _cache:
        _cache[key] = _store.get(key) if _available() else ""
    return _cache[key]


def _read(key: str):
    """Decoded JSON value stored under *key*, or ``None``."""
    head = _get(key)
    count, sep, text = head.partition(":")
    if not sep or not count.isdigit():
        return None
    parts = [text] + [_get(_part_key(key, i)) for i in range(1, int(count))]
    try:
        return json.loads("".join(parts))
    except ValueError:
        return None


def _drop(key: str, keep: int = 0) -> None:
    """Delete *key*'s parts from index *keep* on (``keep=0`` deletes it all)."""
    count, _, _ = _get(key).partition(":")
    total = int(count) if count.isdigit() else 1
    for i in range(max(keep, 1), total):
        _store.delete(_part_key(key, i))
        _cache.pop(_part_key(key, i), None)
    if keep == 0:
        _store.delete(key)
        _cache.pop(key, None)


def _put(key: str, value) -> bool:
    """Store *value* (JSON-able) under *key*; True only if it reads back equal."""
    if not _available():
        return False
    text = json.dumps(value, separators=(",", ":"))  # ASCII-only, newline-free
    size = _chunk_size() or len(text)
    chunks = [text[i:i + size] for i in range(0, len(text), size)]
    stored = [f"{len(chunks)}:{chunks[0]}"] + chunks[1:]
    keys = [key] + [_part_key(key, i) for i in range(1, len(chunks))]
    _drop(key, keep=len(chunks))  # an older, longer value's extra parts
    for k, v in zip(keys, stored):
        _cache.pop(k, None)
        if not (_store.set(k, v) and _store.get(k) == v):
            for written in keys:
                _store.delete(written)  # a failed keychain write can leave a truncated item
                _cache.pop(written, None)
            return False
        _cache[k] = v
    return True


def _warn_plaintext() -> None:
    global _warned
    if not _warned:
        _warned = True
        u.log_yellow(
            "No OS credential store on this machine; tokens saved in plain text "
            "under ~/.ai-accounts (readable only by you)."
        )


# ── dotted-path helpers ──────────────────────────────────────────────────────

def _split(data: dict, secret_fields: Sequence[str]) -> tuple[dict, dict]:
    """``(metadata, secrets)``: *data* minus its non-empty secret paths, and those."""
    meta = copy.deepcopy(data)
    meta.pop(SECRETS_KEY, None)
    secrets = {}
    for field in secret_fields:
        *parents, leaf = field.split(".")
        node = meta
        for part in parents:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(leaf) not in (None, ""):
            secrets[field] = node.pop(leaf)
    return meta, secrets


def _merge(meta: dict, secrets: dict) -> dict:
    data = copy.deepcopy(meta)
    data.pop(SECRETS_KEY, None)
    for field, value in secrets.items():
        *parents, leaf = field.split(".")
        node = data
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return data


def _marker(key: str, secrets: dict, secret_fields: Sequence[str]) -> dict:
    first = next((secrets[f] for f in secret_fields if isinstance(secrets.get(f), str)), "")
    return {
        "store": SERVICE,
        "key": key,
        "has_refresh_token": any(f.rsplit(".", 1)[-1] in _REFRESH_NAMES for f in secrets),
        "fingerprint": fingerprint(first) if first else "",
    }


# ── public API ───────────────────────────────────────────────────────────────

def load_metadata(path: Path) -> dict | None:
    """The profile file as-is (``_secrets`` block included); never reads the store."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load(path: Path, tool: str, secret_fields: Sequence[str]) -> dict | None:
    """The full profile (metadata + secrets), without the ``_secrets`` block.

    A file still holding its secrets inline is migrated once: store write →
    read back → compare → only then the file is rewritten without them. Any
    failure leaves the file untouched and the inline data is returned.
    ``None`` when the file is unreadable, or when it points at a store entry
    that is gone (keychain item deleted, store unavailable on this machine).
    """
    path = Path(path)
    data = load_metadata(path)
    if data is None:
        return None
    meta, inline = _split(data, secret_fields)
    if inline:
        key = store_key(tool, path.stem)
        if _put(key, inline):
            meta[SECRETS_KEY] = _marker(key, inline, secret_fields)
            try:
                _private_dir(path.parent)
                u.atomic_write_json(path, meta)
                u.log_green(f"→ Moved secrets of {path.name} into the {telegram_kit.backend_label()}")
            except OSError:
                pass  # file untouched (atomic write); the store copy is harmless
        else:
            _warn_plaintext()
        return _merge(data, {})
    marker = data.get(SECRETS_KEY)
    if not isinstance(marker, dict):
        return _merge(data, {})  # a profile with no secrets at all
    secrets = _read(str(marker.get("key") or store_key(tool, path.stem)))
    return _merge(meta, secrets) if isinstance(secrets, dict) else None


def save(path: Path, tool: str, payload: dict, secret_fields: Sequence[str]) -> bool:
    """Write *payload*: secrets to the store, metadata to *path* (0600, atomic).

    ``True`` when the secrets are in the store; ``False`` when the store was
    unavailable or failed and the FULL payload was written in plain text
    instead. Raises ``OSError`` if the file itself cannot be written.
    """
    path = Path(path)
    _private_dir(path.parent)
    meta, secrets = _split(payload, secret_fields)
    if not secrets:
        u.atomic_write_json(path, meta)
        return True
    key = store_key(tool, path.stem)
    if _put(key, secrets):
        meta[SECRETS_KEY] = _marker(key, secrets, secret_fields)
        u.atomic_write_json(path, meta)
        return True
    _warn_plaintext()
    u.atomic_write_json(path, _merge(payload, {}))
    return False


def delete(path: Path, tool: str) -> None:
    """Remove the profile file and its store entry."""
    path = Path(path)
    marker = (load_metadata(path) or {}).get(SECRETS_KEY)
    key = marker.get("key") if isinstance(marker, dict) and marker.get("key") else store_key(tool, path.stem)
    path.unlink(missing_ok=True)
    if _available():
        _drop(str(key))


def _backup_dir(tool: str) -> Path:
    override = os.environ.get(f"{tool.upper()}_ACCOUNT_DIR")
    return (Path(override).parent if override else _root() / tool) / "backups"


def backup(tool: str, text: str) -> bool:
    """Keep *text* as the ONE latest pre-switch backup for *tool*.

    Backups are a last-switch safety net (undo the switch that just
    happened), so one copy is enough and older ones are just stale tokens on
    disk. ``True`` = in the store; ``False`` = plaintext ``backups/latest``
    (0600). Either way older plaintext ``*.backup*`` files are pruned.
    """
    directory = _private_dir(_backup_dir(tool))
    in_store = _put(_backup_key(tool), text)
    if in_store:
        (directory / "latest").unlink(missing_ok=True)
    else:
        _warn_plaintext()
        telegram_kit.write_private(directory / "latest", text)
    for old in directory.glob("*.backup*"):
        old.unlink(missing_ok=True)
    return in_store


def read_backup(tool: str) -> str | None:
    """The latest backup for *tool* (store first, then ``backups/latest``)."""
    value = _read(_backup_key(tool))
    if isinstance(value, str):
        return value
    try:
        return (_backup_dir(tool) / "latest").read_text(encoding="utf-8")
    except OSError:
        return None


def status() -> tuple[str, str]:
    """``(label, reason)`` for doctor; *reason* is empty when a store is available."""
    if _available():
        return telegram_kit.backend_label(), ""
    return "none", "no OS credential store found; profile tokens are kept in 0600 plain-text files"
