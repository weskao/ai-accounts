"""Message catalogue and language resolution — the single home for translations.

Canonical here: :data:`LANGUAGES` (which languages exist and how a system
locale maps onto them) and :data:`MESSAGES` (the catalogue itself). Adding a
language means appending one :class:`Language` and one entry per message id;
adding a message means appending one entry to :data:`MESSAGES`. Nothing else
in the package holds user-facing wording for notifications.

The catalogue is keyed **message id first, language second**, so every
translation of one string sits on adjacent lines — a missing translation is
visible by reading down the block rather than by diffing two far-apart
tables. A gap is never fatal: :func:`t` falls back to :data:`FALLBACK`.

Dependency direction (deliberate, do not invert): this module is a **leaf** at
import time — like :mod:`ai_accounts.config_schema` it imports nothing from the
package, so :mod:`ai_accounts.autoswitch` can import it freely. The one config
read it needs (:func:`current_language`) uses a function-local import of
``autoswitch``, which is why that import is *not* at module scope.

Scope note: only notification text lives here today. Terminal output stays in
English on purpose — :data:`ai_accounts.autoswitch_timer._REVOKED_MARKERS`
pattern-matches the providers' own English log lines, so translating those
would silently break revoked-token detection.
"""

from __future__ import annotations

import locale
import os
from dataclasses import dataclass
from functools import lru_cache

FALLBACK = "en"

# The config value meaning "ask the OS" — resolved by :func:`system_language`.
AUTO = "auto"


@dataclass(frozen=True)
class Language:
    """One supported language: its config value, its label, its locale prefixes.

    *aliases* are lowercased, underscore-normalised locale prefixes matched
    against the system locale (``zh_TW.UTF-8`` -> ``zh_tw``). List every
    variant that should resolve here; a locale matching nothing falls back to
    :data:`FALLBACK` rather than guessing a neighbouring language.
    """

    code: str
    label: str
    aliases: tuple[str, ...]


# ── supported languages (append one entry to add a language) ─────────────────
# Deliberately no bare "zh" alias: zh_CN/zh_SG are Simplified and must not
# resolve to Traditional — they fall back to English until a zh-CN entry lands.
LANGUAGES: tuple[Language, ...] = (
    Language("en", "English", ("en",)),
    Language("zh-TW", "繁體中文", ("zh_tw", "zh_hant", "zh_hk", "zh_mo")),
)

LANGUAGE_CODES: tuple[str, ...] = tuple(lang.code for lang in LANGUAGES)

# What the config key accepts. AUTO is never one of these — it is only the
# unset default's internal sentinel (see FIELDS in config_schema.py), never a
# third menu row: with two languages, the menu shows exactly those two.
CHOICES: tuple[str, ...] = LANGUAGE_CODES


def _system_locale() -> str:
    """The OS's locale string, or ``""`` when it cannot be determined.

    POSIX environment variables first (macOS and Linux, in the precedence
    POSIX defines), then :func:`locale.getdefaultlocale` — which is what
    reaches ``GetUserDefaultLocaleName`` on Windows, where none of those
    variables are normally set.
    """
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return value
    try:
        # Deprecated since 3.11 but not removed, and still the only stdlib call
        # that reads the Windows user default without setlocale's global
        # side effects. Any failure just means "unknown locale".
        return locale.getdefaultlocale()[0] or ""
    except Exception:
        return ""


def system_language() -> str:
    """The language the OS asks for, or :data:`FALLBACK` when it asks for none.

    ``C`` / ``POSIX`` / an unsupported locale all land on :data:`FALLBACK` —
    matching nothing is the answer, not the nearest language.
    """
    raw = _system_locale().strip().lower().replace("-", "_")
    for lang in LANGUAGES:
        if any(raw == alias or raw.startswith(alias + "_") for alias in lang.aliases):
            return lang.code
    # A bare prefix ("zh_tw" written as "zh_TW.UTF-8") still has to match.
    stem = raw.split(".", 1)[0].split("@", 1)[0]
    for lang in LANGUAGES:
        if stem in lang.aliases:
            return lang.code
    return FALLBACK


def language_labels(lang: str | None = None) -> dict[str, str]:
    """Config value -> the name to SHOW for it, e.g. ``{"en": "English"}``.

    Every language names itself in its own script (``English``, ``繁體中文``),
    which is the convention language pickers use and means these names need no
    translating. The unset (``auto``) default is not a selectable choice —
    only the two real languages are — but still needs a name to show while
    unset, so it renders as whichever of them it currently resolves to.
    """
    names = {lang.code: lang.label for lang in LANGUAGES}
    return {AUTO: names.get(system_language(), system_language()), **names}


def resolve_language(configured: object) -> str:
    """The language *configured* selects — ``auto``/junk/None means the OS's.

    Never raises: an unknown code from a hand-edited config resolves to the
    system language, so a typo degrades to a sane default instead of an error
    inside a notification path.
    """
    code = str(configured or AUTO).strip()
    if code in LANGUAGE_CODES:
        return code
    return system_language()


@lru_cache(maxsize=1)
def current_language() -> str:
    """:func:`resolve_language` applied to the stored ``language`` setting.

    Cached: the config menu re-renders on every keystroke and each row asks for
    a translation, so an uncached lookup would re-read the config file dozens
    of times per frame. :func:`ai_accounts.autoswitch.save_config` drops the cache
    so switching language takes effect on the next frame.
    """
    # Function-local import on purpose: autoswitch imports this module at
    # module scope, so importing it back at module scope would be a cycle.
    from ai_accounts import autoswitch

    try:
        return resolve_language(autoswitch.load_config().get("language"))
    except Exception:
        return system_language()  # notification text never fails on config I/O


# ── the catalogue (append a message id, then one line per language) ──────────
# Notification wording lives here in every language, because it has no other
# home. Config labels/help are the ASYMMETRIC case: their English text stays
# in ``config_schema.FIELDS`` (so "adding a setting means appending one Field
# and nothing else" stays true) and only the translations live here, looked up
# as ``config.<key>.label`` / ``config.<key>.help`` with the schema's English
# as the fallback.
#
# Style rules the layout depends on:
#   * ``*.title`` is ONE line and carries the whole headline — the notification
#     box puts it on the first row, and a desktop notification uses it as the
#     title. Never wrap it.
#   * ``*.body`` is the follow-up action or status, one line, emoji-led.
#   * No Markdown and no space-alignment inside a string: the Telegram sender
#     frames these itself and posts the frame as monospace.

MESSAGES: dict[str, dict[str, str]] = {
    # ── notifications ───────────────────────────────────────────────────────
    "notify.switched.title": {
        "en": "🔄 {provider}: {from_profile} ({used}%) → {to_profile} ({to_used}%)",
        "zh-TW": "🔄 {provider}：{from_profile}（{used}%）→ {to_profile}（{to_used}%）",
    },
    "notify.switched.restarted": {
        "en": "✅ Session restarted — the new account is live",
        "zh-TW": "✅ 已自動重啟 session，新帳號已生效",
    },
    "notify.switched.restart_needed": {
        "en": "⚠️ Restart your session to use it",
        "zh-TW": "⚠️ 需重啟 session 才會生效",
    },
    "notify.no_candidate.title": {
        "en": "🚫 {provider}: {profile} at {used}%, nothing to switch to",
        "zh-TW": "🚫 {provider}：{profile} 已用 {used}%，無帳號可切",
    },
    "notify.no_candidate.body": {
        # "unreadable" is load-bearing: never claim an account we could not
        # read is over quota — that sends the user to "wait for the reset"
        # when the real fix is a re-login.
        "en": "💡 Others are {threshold}%+ or unreadable — wait for the reset, or add one",
        "zh-TW": "💡 其他帳號都在 {threshold}% 以上或讀不到用量 — 等配額重置，或再新增一個",
    },
    # The generic title/body pair is the FALLBACK, used only when a tick
    # reported a revocation in wording `refresh_report.parse` could not
    # attribute to a profile. The normal path is the grouped trio below: a
    # headline naming the count and the provider types, then one heading per
    # provider over its own profile rows (refresh_report.detail_lines).
    "notify.revoked.title": {
        "en": "🔑 ai-accounts: a token expired, re-login needed",
        "zh-TW": "🔑 ai-accounts：token 已失效，需重新登入",
    },
    # `.one`/`.many` pairs, picked by refresh_report._plural: English needs the
    # plural agreement and "profile(s)" in a headline reads like a form field.
    # Languages without plural agreement give the pair the same text.
    "notify.revoked.grouped.one.title": {
        "en": "🔑 ai-accounts: 1 profile needs re-login — {providers}",
        "zh-TW": "🔑 ai-accounts：{count} 個帳號需重新登入 — {providers}",
    },
    "notify.revoked.grouped.many.title": {
        "en": "🔑 ai-accounts: {count} profiles need re-login — {providers}",
        "zh-TW": "🔑 ai-accounts：{count} 個帳號需重新登入 — {providers}",
    },
    "notify.revoked.group.one": {
        "en": "🔐 {provider} · 1 profile",
        "zh-TW": "🔐 {provider} · {count} 個帳號",
    },
    "notify.revoked.group.many": {
        "en": "🔐 {provider} · {count} profiles",
        "zh-TW": "🔐 {provider} · {count} 個帳號",
    },
    "notify.revoked.reason.missing": {
        # Shown when no inline reason was printed: the profile had no refresh
        # token worth an attempt, so the bulk summary is all there was.
        "en": "refresh token missing or rejected",
        "zh-TW": "refresh token 遺失或已被拒絕",
    },
    "notify.terminal_notifier_hint": {
        "en": (
            "terminal-notifier is installed but did not deliver — enable it "
            "in System Settings -> Notifications -> terminal-notifier "
            "(falling back to a plain notification for now)."
        ),
        "zh-TW": (
            "terminal-notifier 已安裝但通知沒有送達 — 請到"
            "「系統設定 → 通知」開啟 terminal-notifier"
            "（目前先改用一般通知）。"
        ),
    },
    "notify.revoked.body": {
        "en": "ai-accounts refresh --all → <provider>-accounts login-switch <name>",
        "zh-TW": "ai-accounts refresh --all → 再 <provider>-accounts login-switch <name>",
    },
    # Terminal-only (log_yellow), so it keeps the provider prefix a notification
    # gets from its title instead.
    "restart.manual": {
        "en": "⚠️ {provider}: switched {from_profile} → {to_profile} — restart your session to use it.",
        "zh-TW": "⚠️ {provider}：已切換 {from_profile} → {to_profile} — 請重啟 session 才會生效。",
    },
    # ── config menu (translations only; English lives in config_schema) ──────
    "config.enabled.label": {"zh-TW": "啟用自動切換"},
    "config.enabled.help": {
        "zh-TW": "作用中的帳號達到下方門檻時，切換到剩餘配額最多的已存帳號。"
    },
    "config.switch_when_used_pct.label": {"zh-TW": "↳ 切換門檻（%）"},
    "config.switch_when_used_pct.help": {
        "zh-TW": "達到這個用量就開始切換。{value}% 代表作用中的帳號還剩 {remaining}% 配額。"
    },
    "config.switch_window.label": {"zh-TW": "↳ 配額視窗"},
    "config.switch_window.help": {
        "zh-TW": "用哪個配額決定切換：1week，或 5h 短視窗。"
    },
    "config.notify.label": {"zh-TW": "通知方式"},
    "config.notify.help": {
        "zh-TW": "切換時通知到哪裡：desktop（桌面）、telegram 或 none（不通知）。"
    },
    "config.telegram_bot_token.label": {"zh-TW": "Telegram bot token"},
    "config.telegram_bot_token.help": {
        "zh-TW": "notify 設為 telegram 時使用的 Bot API token（顯示時會遮蔽）。"
    },
    "config.telegram_chat_id.label": {"zh-TW": "Telegram chat id"},
    "config.telegram_chat_id.help": {"zh-TW": "接收通知的 Bot API chat id。"},
    "config.agy_blind_switch.label": {"zh-TW": "Antigravity 盲切"},
    "config.agy_blind_switch.help": {
        "zh-TW": "agy 只回報當前 session 的配額：即使無法先確認目標帳號的配額也照切。"
    },
    "config.token_refresh.label": {"zh-TW": "自動更新 token"},
    "config.token_refresh.help": {
        "zh-TW": "依排程更新 OAuth token，與自動切換各自獨立。"
    },
    "config.language.label": {"zh-TW": "語言"},
    "config.language.help": {
        "zh-TW": "通知訊息與本選單的語言。未指定前跟隨系統語系。"
    },
    # Booleans keep their JSON spelling on purpose: `true`/`false` are what
    # `config set` accepts, so showing 開/關 would name a value nobody can type.
    # Group headings, keyed by the English heading itself — Field.group holds
    # that string, so no slug mapping is needed in between.
    "group.Automatic switching": {"zh-TW": "自動切換"},
    "group.Notifications": {"zh-TW": "通知"},
    "group.Provider behavior": {"zh-TW": "各家 CLI 行為"},
    "group.General": {"zh-TW": "一般"},
    # ── config menu chrome (English lives at each call site as the default) ──
    "menu.keys": {"zh-TW": "↑↓ 移動 · ←→ 切換 · Enter 編輯/切換 · r 重設 · Esc 取消 · q/Ctrl-C 離開 · 自動儲存"},
    "menu.unset": {"zh-TW": "（未設定）"},
    "menu.reset_confirm": {"zh-TW": "確定要把所有設定還原為預設值嗎？[y/N]"},
    "menu.prompt": {"zh-TW": "{label} 的新值{hint}："},
    "menu.masked_hint": {"zh-TW": "（留空保持原值）"},
    "menu.select": {"zh-TW": "選擇要修改的項目（留空離開）："},
    "menu.bad_number": {"zh-TW": "請輸入上面列出的項目編號。"},
    "menu.install_prompt": {"zh-TW": "尚未安裝自動切換設定，要現在安裝嗎？[y/N]："},
    "menu.usage": {"zh-TW": "用法：{prog} config get [key] | config set <key> <value>"},
    "menu.unknown_key": {"zh-TW": "未知的設定 {key}。可用的設定："},
    # ── validation errors (shown inline in the menu and by `config set`) ─────
    "error.bool": {"zh-TW": "需要布林值（true/false），得到 {raw}"},
    "error.int": {"zh-TW": "{key} 必須是整數{bounds}，得到 {raw}"},
    "error.choice": {"zh-TW": "{key} 必須是 {choices} 之一，得到 {raw}"},
    "error.notify_channel": {"zh-TW": "無效的通知方式 {channel}：必須是 {channels} 之一"},
}


def t(msgid: str, /, lang: str | None = None, default: str | None = None, **fields: object) -> str:
    """*msgid* in *lang* (default: :func:`current_language`), fields filled in.

    Falls back to *default*, then :data:`FALLBACK`, then the id itself — a
    missing string shows up as English or a visible key, never as a raised
    exception inside a notification path. A ``{placeholder}`` with no matching
    keyword is left literal for the same reason.
    """
    table = MESSAGES.get(msgid) or {}
    text = table.get(lang or current_language()) or table.get(FALLBACK) or default or msgid
    try:
        return text.format(**fields)
    except (KeyError, IndexError):
        return text


def refresh() -> None:
    """Forget the cached language — call after the config file changes."""
    current_language.cache_clear()
