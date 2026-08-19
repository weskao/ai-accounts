"""Declarative schema for every ai-accounts config key — the single source of truth.

Canonical here: the ordered :data:`FIELDS` tuple. One :class:`Field` entry per
config key carries its type, default, allowed values, range, masked-ness, human
label and help text, plus the parser derived from those facts. **Adding a
setting means appending one entry here and nothing else** — there is no
per-key ``if``/``elif`` chain anywhere; parsing and display dispatch on the
descriptor. :data:`NOTIFY_CHANNELS` and :func:`mask_secret` also live here
because the schema needs them to describe itself.

Delegated elsewhere: reading, writing and merging the config file
(:mod:`ai_accounts.autoswitch` — ``load_config``/``save_config``/``config_flag``),
and the CLI surface that prints these values (``ai_accounts``).

Dependency direction (deliberate, do not invert): this module is a **leaf** —
it imports nothing from the package. :mod:`ai_accounts.autoswitch` imports it,
derives ``DEFAULTS``/``NOTIFY_CHANNELS`` from it, and keeps its historical
``_mask`` name as an alias of :func:`mask_secret` for its own callers.
Importing ``autoswitch`` from here would be a cycle.

Callers: consume :func:`parse_value` / :func:`format_value` rather than
re-deriving a key's type from its default — a hand-kept second list is how a
new flag ends up stored as the truthy *string* ``"false"``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

NOTIFY_CHANNELS = ("desktop", "telegram", "none")

# The quota windows every provider reports, longest first — the default leads.
SWITCH_WINDOWS = ("1week", "5h")

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


def mask_secret(secret: str) -> str:
    """Stars, plus at most the last 4 chars — never enough to reuse."""
    if not secret:
        return ""
    return "*" * 8 + secret[-4:] if len(secret) > 12 else "*" * 8


@dataclass(frozen=True)
class Field:
    """One config key, described completely enough to parse and display it."""

    key: str
    type: type
    default: object
    label: str
    help: str
    choices: tuple[str, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    masked: bool = False
    group: str | None = None

    @property
    def parse(self) -> Callable[[str], object]:
        """This field's parser — a callable, so a menu can hold on to it."""
        return self._parse

    def _parse(self, raw: str) -> object:
        """*raw* as this field's type, or ``ValueError`` with a CLI-ready message."""
        if self.type is bool:
            lowered = raw.strip().lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError(f"expected a boolean (true/false), got {raw!r}")
        if self.type is int:
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"{self._range_error} {raw!r}") from None
            if not self._in_range(value):
                raise ValueError(f"{self._range_error} {value}")
            return value
        if self.choices is not None and raw not in self.choices:
            raise ValueError(
                f"{self.key} must be one of {', '.join(self.choices)}, got {raw!r}"
            )
        return raw

    def format(self, value: object) -> str:
        """*value* rendered for display — masked when this field holds a secret."""
        if self.masked:
            return mask_secret(str(value))
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @property
    def _range_error(self) -> str:
        bounds = f" {self.minimum}-{self.maximum}" if self.minimum is not None else ""
        return f"{self.key} must be an integer{bounds}, got"

    def _in_range(self, value: int) -> bool:
        return (self.minimum is None or value >= self.minimum) and (
            self.maximum is None or value <= self.maximum
        )


# ── the schema (append here to add a setting; nothing else to touch) ─────────

FIELDS: tuple[Field, ...] = (
    Field(
        key="enabled",
        type=bool,
        default=False,
        label="Enable automatic switching",
        help="When the active account reaches the limit below, switch to the saved account with the most quota left.",
        group="Automatic switching",
    ),
    Field(
        key="switch_when_used_pct",
        type=int,
        default=90,
        minimum=1,
        maximum=100,
        label="↳ Switch at usage (%)",
        help="Start switching at this usage. 90% means the active account has 10% quota left.",
        group="Automatic switching",
    ),
    Field(
        key="switch_window",
        type=str,
        default="1week",
        choices=SWITCH_WINDOWS,
        label="↳ Quota window",
        help="Which quota decides a switch: 1week, or 5h for the short window.",
        group="Automatic switching",
    ),
    Field(
        key="notify",
        type=str,
        default="desktop",
        choices=NOTIFY_CHANNELS,
        label="Notifications",
        help="Where a switch is announced: desktop, telegram or none.",
        group="Notifications",
    ),
    Field(
        key="telegram_bot_token",
        type=str,
        default="",
        masked=True,
        label="Telegram bot token",
        help="Bot API token used when notify is telegram (masked on display).",
        group="Notifications",
    ),
    Field(
        key="telegram_chat_id",
        type=str,
        default="",
        label="Telegram chat id",
        help="Bot API chat id that receives the notification.",
        group="Notifications",
    ),
    Field(
        key="agy_blind_switch",
        type=bool,
        default=False,
        label="Antigravity blind switch",
        help="Switch antigravity accounts even without usage data.",
        group="Provider behavior",
    ),
    Field(
        key="token_refresh",
        type=bool,
        default=True,
        label="Automatic token refresh",
        help="Refresh OAuth tokens on the scheduled timer, independent of auto-switch.",
    ),
)


def field(key: str) -> Field | None:
    """The descriptor for *key*, or ``None`` if no such setting is declared."""
    return next((f for f in FIELDS if f.key == key), None)


def defaults() -> dict[str, object]:
    """Every key's default — the mapping ``autoswitch.DEFAULTS`` is built from."""
    return {f.key: f.default for f in FIELDS}


def _require(key: str) -> Field:
    found = field(key)
    if found is None:
        raise ValueError(f"unknown config key {key!r}")
    return found


def parse_value(key: str, raw: str) -> object:
    """*raw* parsed as *key*'s type, or ``ValueError`` with a CLI-ready message."""
    return _require(key).parse(raw)


def format_value(key: str, value: object) -> str:
    """*value* rendered for display, honouring *key*'s masked flag."""
    return _require(key).format(value)
