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
        # Always say which direction the number runs ("used", not a bare "at
        # {used}%"): a percentage next to a quota reads as "left" just as
        # easily as "spent", and the two lead to opposite actions.
        "en": "🚫 {provider}: {profile} has used {used}% of its quota, nothing to switch to",
        "zh-TW": "🚫 {provider}：{profile} 已用掉 {used}% 配額，沒有其他帳號可切",
    },
    "notify.no_candidate.body": {
        # "unreadable" is load-bearing: never claim an account we could not
        # read is over quota — that sends the user to "wait for the reset"
        # when the real fix is a re-login.
        "en": "💡 Every other account has used ≥{threshold}% too, or its usage is unreadable — wait for the quota reset, or add an account",
        "zh-TW": "💡 其他帳號的已用配額也都 ≥{threshold}%，或讀不到用量 — 等配額重置，或新增帳號",
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
    # Quota-reset notifications (see TODO.md's future-work item): a provider's
    # window rolled over and usage is back at 0%. `.title` is the single-event
    # headline; `.many.title` groups several resets the way
    # `notify.revoked.grouped.many.title` groups re-logins; `.line` is one
    # per-provider/profile row inside that grouped body; `.body` is the
    # follow-up line shown either way.
    "notify.reset.title": {
        "en": "🔋 {provider}: {profile} {window} quota is available again (was {used}% used)",
        "zh-TW": "🔋 {provider}：{profile} 的 {window} 配額已恢復可用（重置前已用 {used}%）",
    },
    "notify.reset.many.title": {
        "en": "🔋 ai-accounts: {count} quota windows reset — {providers}",
        "zh-TW": "🔋 ai-accounts：{count} 個配額視窗已重置 — {providers}",
    },
    "notify.reset.line": {
        "en": "• {provider} · {profile} · {window} (was {used}%)",
        "zh-TW": "• {provider} · {profile} · {window}（重置前已用 {used}%）",
    },
    "notify.reset.body": {
        "en": "Next reset: {next}",
        "zh-TW": "下次重置：{next}",
    },
    # Display names for a provider's quota-window keys (`providers.Provider`'s
    # `reset_windows`), looked up as `i18n.t(f"window.{key}", default=key)` —
    # keyed directly off the JSON window key so there is no second
    # key->display-name dict to drift out of sync with `reset_windows`.
    "window.hourly": {"en": "hourly", "zh-TW": "每小時"},
    "window.weekly": {"en": "weekly", "zh-TW": "每週"},
    "window.monthly": {"en": "monthly", "zh-TW": "每月"},
    # agy's four windows, named as its own list table heads them (GEMINI 5H /
    # GEMINI 1W / CLAUDE-GPT 5H / CLAUDE-GPT 1W).
    "window.gemini_session": {"en": "Gemini 5h", "zh-TW": "Gemini 5 小時"},
    "window.gemini_weekly": {"en": "Gemini weekly", "zh-TW": "Gemini 每週"},
    "window.other_session": {"en": "Claude/GPT 5h", "zh-TW": "Claude/GPT 5 小時"},
    "window.other_weekly": {"en": "Claude/GPT weekly", "zh-TW": "Claude/GPT 每週"},
    # Shown instead of the next-reset time when the reading came from a
    # provider's local cache: the reset is certain, the next deadline is not.
    "notify.reset.cached": {
        "en": "From the last cached reading — run `agy-accounts list` for a live check.",
        "zh-TW": "來自上次快取的讀數 — 執行 `agy-accounts list` 可即時確認。",
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
    "config.reset_notify.label": {"zh-TW": "配額重置時通知"},
    "config.reset_notify.help": {
        "zh-TW": "當某提供者的配額視窗重置、恢復可用時發出通知。"
    },
    "config.reset_notify_min_used_pct.label": {"zh-TW": "↳ 僅在重置前已用 ≥（%）時通知"},
    "config.reset_notify_min_used_pct.help": {
        "zh-TW": "只有重置前已用量達到至少 {value}% 才通知。"
    },
    "config.agy_blind_switch.label": {"zh-TW": "Antigravity 盲切"},
    "config.agy_blind_switch.help": {
        "zh-TW": "agy 只回報當前 session 的配額：即使無法先確認目標帳號的配額也照切。"
    },
    "config.agy_list_cached_usage.label": {"zh-TW": "Antigravity 非當前帳號使用快取"},
    "config.agy_list_cached_usage.help": {
        "zh-TW": "開啟：當前帳號即時查詢，其他帳號用上次紀錄（可能過期，agy-accounts list --refresh 更新全部）；關閉：全部即時查詢，較慢。"
    },
    "config.token_refresh.label": {"zh-TW": "自動更新 token"},
    "config.token_refresh.help": {
        "zh-TW": "依排程更新 OAuth token，與自動切換各自獨立。"
    },
    "config.language.label": {"zh-TW": "語言"},
    "config.language.help": {
        "zh-TW": "通知訊息與本選單的語言。未指定前跟隨系統語系。"
    },
    "config.layout.label": {"zh-TW": "輸出版面"},
    "config.layout.help": {
        "zh-TW": "指令輸出的排版方式：auto 依終端機寬度自動判斷、wide 為完整桌面表格、narrow 為適合手機寬度終端機的堆疊版面。"
    },
    "config.table_style.label": {"zh-TW": "表格樣式"},
    "config.table_style.help": {
        "zh-TW": "表格與面板的繪製方式：modern 為圓角淡化外框、標題列加底色、每隔一列加斑馬紋；classic 為原本的全亮格線（淺色終端機建議選這個）。"
    },
    # Booleans read as a toggle state in the menu, where they are cycled with
    # ←→/Enter and never typed. `Field.format` (what `config get` prints and
    # `config set` accepts) still says true/false, and the fallback menu — the
    # one path that asks for a bool to be typed — spells that out in its hint.
    "value.on": {"zh-TW": "開啟"},
    "value.off": {"zh-TW": "關閉"},
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
    "menu.usage": {
        "zh-TW": "用法：{prog} config get [key] | config set <key> <value>"
        " | config export [檔案] | config import <檔案>"
    },
    "menu.export_stdout": {"zh-TW": "標準輸出"},
    "menu.export_done": {"zh-TW": "已匯出 {count} 項設定到 {target}"},
    "menu.export_secrets": {"zh-TW": "機密資料一律不會匯出，請在另一台機器重新設定：{keys}"},
    "menu.import_done": {"zh-TW": "已從 {path} 匯入 {count} 項設定"},
    "menu.import_skipped": {"zh-TW": "已略過（機密資料一律不匯入，未知的設定保持原樣）：{keys}"},
    "menu.import_not_object": {"zh-TW": "{path} 不是設定的 JSON 物件"},
    "menu.unknown_key": {"zh-TW": "未知的設定 {key}。可用的設定："},
    # ── agy-accounts `list` footer notes ──────────────────────────────────────
    "list.agy.live_queried": {
        "en": "ℹ️ Only the account in use was queried live; any error shows in UPDATED.",
        "zh-TW": "ℹ️ 只有目前使用中的這個帳號是即時查詢；若有錯誤會顯示在「UPDATED」欄",
    },
    "list.agy.cached_missing.one": {
        "en": " {missing} more has no saved reading yet.",
        "zh-TW": " 另外 {missing} 個尚無任何紀錄。",
    },
    "list.agy.cached_missing.many": {
        "en": " {missing} more have no saved reading yet.",
        "zh-TW": " 另外 {missing} 個尚無任何紀錄。",
    },
    "list.agy.cached_usage": {
        "en": "ℹ️ {count} inactive account(s) show the reading saved at the time "
        "in UPDATED, not their usage right now.{missing_note} "
        "Refresh all: {cmd}",
        "zh-TW": "ℹ️ {count} 個非作用中帳號顯示的是 UPDATED 欄那個時間點存下的數值，"
        "不是現在的用量。{missing_note} "
        "全部重新查詢：{cmd}",
    },
    "list.agy.cached_empty": {
        "en": "ℹ️ No saved usage yet for inactive profiles. Fetch it once: {cmd}",
        "zh-TW": "ℹ️ 非作用中帳號尚無用量紀錄。更新一次：{cmd}",
    },
    "list.agy.cached_toggle_off": {
        "en": "ℹ️ To query every account live on each list instead (slower, one agy "
        "launch per account): {cmd}",
        "zh-TW": "ℹ️ 想改成每次 list 都即時查詢所有帳號（較慢，每個帳號各啟動一次 agy）："
        "{cmd}",
    },
    "list.agy.live_usage_hint": {
        "en": "ℹ️ Live mode: every account is checked one by one via agy, which is "
        "slow. Turn this on to make list much faster - inactive accounts show "
        "their last-known usage instead of being checked live: {cmd}",
        "zh-TW": "ℹ️ 目前是即時模式：每個帳號都要逐一啟動 agy 查詢，較耗時。"
        "開啟快取後 list 會快很多——非作用中帳號改顯示上次查到的用量，不必逐一即時查詢："
        "{cmd}",
    },
    # ── validation errors (shown inline in the menu and by `config set`) ─────
    "error.bool": {"zh-TW": "需要布林值（true/false），得到 {raw}"},
    "error.int": {"zh-TW": "{key} 必須是整數{bounds}，得到 {raw}"},
    "error.choice": {"zh-TW": "{key} 必須是 {choices} 之一，得到 {raw}"},
    "error.notify_channel": {"zh-TW": "無效的通知方式 {channel}：必須是 {channels} 之一"},
    # ── CLI help text (translations only; English lives in each module's
    # HELP constant — same asymmetric convention as the config labels above:
    # `t(msgid, default=HELP)` falls back to that English text untouched) ────
    "help.ai_accounts": {
        "zh-TW": """ai-accounts — 一次驅動所有 AI 帳號工具

用法
  ai-accounts                        顯示這份說明（可用的指令）
  ai-accounts list [--json]          列出所有提供者的帳號（各提供者並行執行）；
                                      --json 會把每個提供者自己的 --json 輸出合併成
                                      一個以提供者為鍵的物件（例如 "codex-accounts": [...]）；
                                      失敗的提供者改回傳 {"error": "..."}
  ai-accounts who | current          顯示每個提供者目前作用中的帳號
  ai-accounts usage [--json]         只顯示每個提供者作用中帳號的用量列；
                                      --json 以相同方式合併每個提供者的 --json 輸出
  ai-accounts refresh [<name>|--all] 重新整理每個提供者的 token
  ai-accounts sync                   把作用中的登入同步回其帳號檔案，每個提供者都做
  ai-accounts save [<name>]          在每個提供者儲存目前的登入；
                                      不給名稱 = 各提供者依自己作用中帳號的
                                      email 決定名稱（若各提供者登入的帳號不同，
                                      名稱可能不一樣 — 這是刻意的轉發行為，不是錯誤）
  ai-accounts switch [<name>]        切換每個提供者的帳號（互動式，一次一個）
  ai-accounts remove [<name>]        移除每個提供者的帳號；不給名稱 = 每個提供者
                                      依序出現互動式選擇器
  ai-accounts login-switch <name>    重新登入並存成 <name>，每個提供者都做（互動式）
  ai-accounts autoswitch             立即對每個提供者執行低配額自動切換檢查
  ai-accounts autoswitch setup       一次性安裝事件掛鉤與計時器備援
  ai-accounts config                 互動式設定選單（方向鍵操作；若 stdin
                                      不是 TTY 則改用編號選單）
  ai-accounts config get [key]       印出自動切換設定（或指定單一項目）；
                                      telegram bot token 一律遮蔽
  ai-accounts config set <key> <val> 設定單一自動切換設定項目（拒絕未知項目）
  ai-accounts install-timer [--interval N]
                                      向作業系統排程自動切換檢查（預設：
                                      每 1800 秒／30 分鐘一次）
  ai-accounts uninstall-timer       移除已排程的自動切換檢查
  ai-accounts timer-status          回報自動切換檢查是否已排程
  ai-accounts doctor [--json]        離線健康檢查：各提供者的 CLI 執行檔是否在
                                      PATH 上、憑證儲存區是否可連線、已存帳號
                                      JSON 是否正確且未過期、自動切換計時器狀態；
                                      預設印出成功/失敗表格，--json 則印出單一
                                      JSON 文件
  ai-accounts -h | --help | help     顯示這份說明

每個指令都會轉發給 codex-accounts、claude-accounts、agy-accounts、grok-accounts、
vibe-accounts 與 copilot-accounts。
`list` 會同時執行並在每個表格完成時立即印出（最快的提供者最先顯示），
中間以進度指示器顯示還有幾個提供者在抓取；其他每個指令都會一次對一個
提供者執行、保留即時輸出，讓互動式選擇器與登入流程正常運作、顏色也會保留。
指令後的任何參數（例如帳號名稱或 `--all`）都會原樣轉發給每個提供者 —
但 `autoswitch` 之後只接受本機的 `setup` 動作，其餘多餘參數一律拒絕。
`list`／`usage` 之後的 `--json` 是另一個例外：一樣並行執行每個提供者，
但改為擷取輸出（不使用即時 stdio），把每個提供者自己的 `--json` 文件
合併成一個物件後印出一次。
""",
    },
    "help.codex": {
        "zh-TW": """codex-accounts — 管理多組 Codex CLI 登入帳號

用法
  codex-accounts who                   顯示目前登入的 Codex 帳號
  codex-accounts current               `who` 的別名
  codex-accounts save [<name>]         儲存目前的登入為可重複使用的帳號；
                                       不給名稱 = 依作用中帳號的 email 決定
  codex-accounts list [--json]         列出帳號與用量（不會重新整理 token）；
                                       --json 改印一份 JSON 陣列，取代表格
  codex-accounts usage [--json]        只顯示作用中帳號的用量列；
                                       --json 改印一份 JSON 陣列，取代表格
  codex-accounts switch [<name>]       依名稱切換；不給名稱 = 互動式選擇器
  codex-accounts autoswitch            若作用中帳號配額偏低就切換
                                       （見 ~/.ai-accounts/config.json）
  codex-accounts remove [<name>]       刪除已存帳號；不給名稱 = 互動式選擇器
  codex-accounts refresh [<name>]      透過 OAuth 重新整理 token（不開瀏覽器、不登出）；
                                       不給名稱 = 重新整理作用中登入並同步回去
  codex-accounts refresh --all         重新整理所有已存帳號
  codex-accounts sync                  把作用中的登入複製回對應的帳號檔案
  codex-accounts login-switch <name>   獨立的 codex 登入並存成 <name>
  codex-accounts config                所有 ai-accounts CLI 共用的互動式設定選單
  codex-accounts config get [key]      印出共用的自動切換設定（或指定單一項目）
  codex-accounts config set <k> <v>    設定單一共用設定項目（拒絕未知項目）
  codex-accounts -h | --help | help    顯示這份說明

範例
  codex-accounts login-switch personal
  codex-accounts login-switch work
  codex-accounts list
  codex-accounts switch
  codex-accounts switch personal
  codex-accounts refresh --all
  codex-accounts who

帳號存放於 ~/.ai-accounts/codex/accounts/<name>.json（可用
$CODEX_ACCOUNT_DIR 覆寫）；舊的 ~/.codex/accounts 位置會自動搬移過來。
請把這個目錄當成機密資料 — 已存帳號內含 Codex 的驗證 token。
""",
    },
    "help.claude": {
        "zh-TW": """claude-accounts — 管理多組 Claude Code 登入帳號

用法
  claude-accounts who                   顯示目前登入的 Claude 帳號
  claude-accounts current               `who` 的別名
  claude-accounts save [<name>]         儲存目前的登入為可重複使用的帳號；
                                        不給名稱 = 依作用中帳號的 email 決定
  claude-accounts list [--json]         列出帳號與用量（不會重新整理 token）；
                                        --json 改印一份 JSON 陣列，取代表格
  claude-accounts usage [--json]        只顯示作用中帳號的用量列；
                                        --json 改印一份 JSON 陣列，取代表格
  claude-accounts switch [<name>]       依名稱切換；不給名稱 = 互動式選擇器
  claude-accounts autoswitch            若作用中帳號配額偏低就切換
                                        （見 ~/.ai-accounts/config.json）
  claude-accounts remove [<name>]       依名稱刪除；不給名稱 = 互動式選擇器
  claude-accounts refresh [<name>]      透過 OAuth 重新整理 token（不開瀏覽器、不登出）；
                                        不給名稱 = 重新整理作用中登入並同步回去
  claude-accounts refresh --all         重新整理所有已存帳號
  claude-accounts sync                  把作用中的登入複製回對應的帳號檔案
  claude-accounts login-switch <name>   執行 `claude auth login` 並存成 <name>
  claude-accounts config                所有 ai-accounts CLI 共用的互動式設定選單
  claude-accounts config get [key]      印出共用的自動切換設定（或指定單一項目）
  claude-accounts config set <k> <v>    設定單一共用設定項目（拒絕未知項目）
  claude-accounts -h | --help | help    顯示這份說明

範例
  claude-accounts login-switch personal
  claude-accounts login-switch work
  claude-accounts list
  claude-accounts switch
  claude-accounts switch personal
  claude-accounts refresh --all
  claude-accounts who

帳號存放於 ~/.ai-accounts/claude/accounts/<name>.json（可用
$CLAUDE_ACCOUNT_DIR 覆寫）；舊的 ~/.claude/accounts 位置會自動搬移過來。
請把這個目錄當成機密資料 — 已存帳號內含 Claude 的 OAuth token。
""",
    },
    "help.agy": {
        "zh-TW": """agy-accounts — 管理多組 Antigravity OAuth 帳號

平台
  macOS / Windows / Linux — 官方 agy session 存放在作業系統的憑證儲存區
  （Keychain／Credential Manager／Secret Service）。Linux 另外需要
  libsecret 提供的 `secret-tool`。

用法
  agy-accounts who                   顯示目前選用的 Antigravity 帳號
  agy-accounts current               `who` 的別名
  agy-accounts save [<name>]         儲存目前的登入為可重複使用的帳號；
                                     不給名稱 = 依作用中帳號的 email 決定
                                     （需要查一次配額）
  agy-accounts list [--refresh] [--json]
                                     列出已存帳號（表格檢視）；--refresh
                                     會在快取模式下強制抓取即時配額；
                                     --json 改印一份 JSON 陣列，取代表格
  agy-accounts usage [--json]        只顯示作用中帳號的配額列；
                                     --json 改印一份 JSON 陣列，取代表格
  agy-accounts switch [<name>]       依名稱切換；不給名稱 = 互動式選擇器
  agy-accounts remove [<name>]       依名稱刪除；不給名稱 = 互動式選擇器
  agy-accounts refresh [<name>]      透過 Google OAuth 更新授權更新 token
                                     （不開瀏覽器、不啟動 agy；失敗則改用 agy）；
                                     不給名稱 = 重新整理作用中 session 並同步回去
  agy-accounts refresh --all         重新整理所有已存帳號
  agy-accounts sync                  把作用中的登入複製回對應的帳號檔案
  agy-accounts autoswitch            配額用盡時離開作用中帳號
                                     （沒有 agy 或 Antigravity IDE 執行時會
                                     實際量測候選帳號；否則依上次讀到的配額排序，
                                     完全沒有讀數的候選需要 "agy_blind_switch": true）
  agy-accounts login-switch <name>   Antigravity Google 登入並存成 <name>
  agy-accounts config                所有 ai-accounts CLI 共用的互動式設定選單
                                     （即使憑證儲存區無法使用也能運作）
  agy-accounts config get [key]      印出共用的自動切換設定（或指定單一項目）
  agy-accounts config set <k> <v>    設定單一共用設定項目（拒絕未知項目）
  agy-accounts -h | --help | help    顯示這份說明

範例
  agy-accounts login-switch personal
  agy-accounts login-switch work
  agy-accounts list
  agy-accounts switch
  agy-accounts switch personal
  agy-accounts save
  agy-accounts remove
  agy-accounts refresh --all
  agy-accounts who

帳號存放於 ~/.ai-accounts/antigravity/accounts/<name>.json。
請把這個目錄當成機密資料 — 已存帳號內含 Google OAuth token。
""",
    },
    "help.grok": {
        "zh-TW": """grok-accounts — 管理多組 Grok Build CLI 登入帳號

用法
  grok-accounts who                   顯示目前登入的 Grok 帳號
  grok-accounts current               `who` 的別名
  grok-accounts save [<name>]         儲存目前的登入；不給名稱 = 依 email 決定
  grok-accounts list [--json]         列出已存帳號；--json 改印一份 JSON 陣列，取代表格
                                       （沒有配額 API：usage 一律為 null，
                                       no_quota_api: true）
  grok-accounts usage [--json]        只顯示作用中帳號（session 與到期時間）；
                                       --json 改印一份 JSON 陣列，取代表格
  grok-accounts switch [<name>]       依名稱切換；不給名稱 = 互動式選擇器
  grok-accounts remove [<name>]       刪除已存帳號；不給名稱 = 互動式選擇器
  grok-accounts refresh [<name>]      更新作用中／指定帳號的 session token
  grok-accounts refresh --all         更新所有已存帳號的 token
  grok-accounts sync                  把作用中的登入複製回對應的帳號檔案
  grok-accounts autoswitch            回報 grok 沒有配額 API 可供切換
  grok-accounts login-switch <name>   全新 Grok OAuth 登入並存成 <name>
  grok-accounts config                所有 ai-accounts CLI 共用的互動式設定選單
  grok-accounts config get [key]      印出共用的自動切換設定（或指定單一項目）
  grok-accounts config set <k> <v>    設定單一共用設定項目（拒絕未知項目）
  grok-accounts -h | --help | help    顯示這份說明

範例
  grok-accounts login-switch personal
  grok-accounts login-switch work
  grok-accounts list
  grok-accounts switch
  grok-accounts switch personal
  grok-accounts refresh --all
  grok-accounts who

模型
  grok-4.5（旗艦版，50 萬 token 上下文）— 具備 agentic 工具呼叫、幻覺極低、
  可調整推理強度；xAI 用於程式與其他任務的首選。
  API：每 100 萬輸入 token $2.00，每 100 萬輸出 token $6.00。
  消費方案：Free（每月 $0）、SuperGrok（每月 $30，解鎖 Grok 4.5 與更高限額）。
  Grok Build CLI 文件：docs.x.ai/build/

帳號存放於 ~/.ai-accounts/grok/accounts/<name>.json（可用
$GROK_ACCOUNT_DIR 覆寫）。請把這個目錄當成機密資料 — 帳號內含 OAuth token。
`refresh` 會對憑證自身 issuer 探索到的 token 端點執行標準 OIDC 更新授權 —
沒有任何內容是寫死的。當該授權需要 ai-accounts 沒有的 client secret 時，
會改為執行 `grok models`（讓官方 CLI 自行輪替憑證）。
`switch` 在還原的 token 已過期或即將於 5 分鐘內過期時會就地重新整理。
""",
    },
    "help.vibe": {
        "zh-TW": """vibe-accounts — 管理多組 Mistral Vibe CLI 登入帳號

用法
  vibe-accounts who                   顯示目前登入的 Vibe 帳號
  vibe-accounts current               `who` 的別名
  vibe-accounts save [<name>]         儲存目前的登入；不給名稱 = 依金鑰決定
  vibe-accounts list [--json]         列出已存帳號；--json 改印一份 JSON 陣列，取代表格
                                       （沒有配額 API：usage 一律為 null，
                                       no_quota_api: true）
  vibe-accounts usage [--json]        只顯示作用中帳號；
                                       --json 改印一份 JSON 陣列，取代表格
  vibe-accounts switch [<name>]       依名稱切換；不給名稱 = 互動式選擇器
  vibe-accounts remove [<name>]       刪除已存帳號；不給名稱 = 互動式選擇器
  vibe-accounts refresh [<name>]      驗證／更新作用中或指定帳號的 session
                                       （靜態金鑰不需要）
  vibe-accounts refresh --all         重新整理所有已存帳號（靜態金鑰不需要）
  vibe-accounts sync                  把作用中的登入複製回對應的帳號檔案
  vibe-accounts autoswitch            回報 vibe 沒有配額 API 可供切換
  vibe-accounts login-switch <name>   全新 Vibe 設定／登入並存成 <name>
  vibe-accounts config                所有 ai-accounts CLI 共用的互動式設定選單
  vibe-accounts config get [key]      印出共用的自動切換設定（或指定單一項目）
  vibe-accounts config set <k> <v>    設定單一共用設定項目（拒絕未知項目）
  vibe-accounts -h | --help | help    顯示這份說明

範例
  vibe-accounts login-switch personal
  vibe-accounts login-switch work
  vibe-accounts list
  vibe-accounts switch
  vibe-accounts switch personal
  vibe-accounts who

帳號存放於 ~/.ai-accounts/vibe/accounts/<name>.json（可用
$VIBE_ACCOUNT_DIR 覆寫）。請把這個目錄當成機密資料 — 帳號內含 API 金鑰。

Vibe 會把即時金鑰存在作業系統鑰匙圈（macOS：login keychain，
service 名稱 "ai.mistral.vibe"），若無則改用 $VIBE_HOME/.env；
這些指令會讀寫 vibe 自己實際使用的那個儲存區。
""",
    },
    "help.copilot": {
        "zh-TW": """copilot-accounts — 管理多組 GitHub Copilot CLI 登入帳號

用法
  copilot-accounts who                   顯示目前登入的 Copilot 帳號
  copilot-accounts current               `who` 的別名
  copilot-accounts save [<name>]         儲存目前的登入；不給名稱 = 依 GitHub
                                          帳號決定
  copilot-accounts list [--json]         列出已存帳號與 premium 配額；
                                          --json 改印一份 JSON 陣列，取代表格
  copilot-accounts usage [--json]        只顯示作用中帳號；
                                          --json 改印一份 JSON 陣列，取代表格
  copilot-accounts switch [<name>]       依名稱切換；不給名稱 = 互動式選擇器
  copilot-accounts remove [<name>]       刪除已存帳號；不給名稱 = 互動式選擇器
  copilot-accounts refresh [<name>]      驗證該帳號的 token 是否仍然有效
  copilot-accounts refresh --all         驗證所有已存帳號的 token
  copilot-accounts sync                  把作用中的登入複製回對應的帳號檔案
  copilot-accounts autoswitch            回報 Copilot 目前的自動切換支援狀況
  copilot-accounts login-switch <name>   全新 Copilot CLI 登入並存成 <name>
  copilot-accounts config                所有 ai-accounts CLI 共用的互動式設定選單
  copilot-accounts config get [key]      印出共用的自動切換設定（或指定單一項目）
  copilot-accounts config set <k> <v>    設定單一共用設定項目（拒絕未知項目）
  copilot-accounts -h | --help | help    顯示這份說明

範例
  copilot-accounts login-switch personal
  copilot-accounts login-switch work
  copilot-accounts list
  copilot-accounts switch
  copilot-accounts switch personal
  copilot-accounts who

帳號存放於 ~/.ai-accounts/copilot/accounts/<name>.json（可用
$COPILOT_ACCOUNT_DIR 覆寫）。請把這個目錄當成機密資料 — 已存帳號內含
GitHub token。

即時 token 依序查詢 Copilot CLI 設定目錄（~/.copilot，遵循
$XDG_CONFIG_HOME）、作業系統鑰匙圈，最後是
$COPILOT_GITHUB_TOKEN／$GH_TOKEN／$GITHUB_TOKEN。已匯出的環境變數
token 會蓋過這些指令寫入的任何值，因此 `switch` 在偵測到時會提出警告。

Copilot 的配額端點（premium/chat/completions 用量）尚未對照真實登入
驗證過；查詢失敗時會像 grok／vibe 一樣退回「沒有配額 API」，`list`／
`usage` 不會因此失敗。`autoswitch` 目前會回報配額 API 尚未驗證、暫不
支援自動切換。
""",
    },
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
