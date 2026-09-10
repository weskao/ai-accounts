# 配額重置通知（Quota-reset notifications）— 實作規劃

對應 `TODO.md` → Future work 第一項（🔴 High priority）。

## 0. 結論：可以做到

**可行，且不需要新依賴、不需要新排程器、不需要碰任何 provider 的 HTTP 程式碼。** 所有需要的原料現在就有：

| 需要什麼 | 現況 |
|---|---|
| 每個配額視窗的**重置時間** | `usage_format.UsageWindow.reset_time`（epoch 秒）— codex / claude / agy / copilot 四家都已經解析並輸出在 `list --json` / `usage --json` 的 `reset_time` 欄位 |
| 每個視窗的**已用 %** | 同上 `UsageWindow.percentage`（0–100 used） |
| **週期性執行** | `autoswitch_timer.run_once()` 每 tick 一次（預設 30 分），已有 `enabled` / `token_refresh` 兩道獨立 gate，加第三道即可 |
| **通知管道** | `autoswitch.notify()`（desktop / telegram / none）、`u.source_device()` 來源標記 |
| **持久狀態** | `autoswitch._read_json` / `_write_private`（0600、atomic）— 現在寫 `autoswitch-state.json`，同一套寫新檔 |
| **設定介面** | `config_schema.FIELDS` 加一行 = 選單、`config get/set`、`export/import`、fallback menu 全部自動有 |
| **i18n** | `i18n.MESSAGES` 加 key；`config.<key>.label/help` 慣例已有 |
| **provider 能力偵測** | `providers.Provider` 是唯一的 per-provider 事實表；grok / vibe 無 quota API，copilot 遇到 shape drift 會回 `no_quota_api: true` |

唯一真正的「限制」是**通知延遲 ≤ timer 間隔**（預設 30 分）。5H 視窗一天重置 4–5 次，30 分內收到通知合理；要更快 `ai-accounts install-timer --interval 300`。

## 1. 偵測策略：用 `reset_time`，不要用「% 掉回 0」

TODO 原文寫的是「usage returns to 0%」。**建議不要以 % 變化為觸發條件**，改用 API 給的 `reset_time`：

| 方案 | 問題 |
|---|---|
| A. 比較前後兩次 `%`（前次 ≥ X、這次 ≈ 0） | 30 分一次的取樣，重置後用戶可能已經用掉 15%，就漏報；初次讀到 0% 會誤報；agy 快取回傳 0 也會誤報 — 正是 TODO 點名的三種 false positive |
| **B. 記錄每個視窗的 `reset_time`，下次 tick 看到 `now ≥ 記錄值` 且 API 回傳新的（更晚的）`reset_time`** | 每次重置**恰好觸發一次**（觸發後 state 已更新成新 reset_time）；第一次看到的視窗沒有前值 → 不觸發；重複 tick 沒有新 reset_time → 不觸發。三種 false positive 天然消失，不需要 cooldown |

### 1.1 觸發條件（純函式，可單元測試）

對 fresh snapshot 中每個 `(provider, profile, window)`（`reset_time` 為 None 的直接跳過）：

```
prev = state.get(key)
fire = (
    prev is not None
    and prev.reset_time <= now                     # 記錄中的視窗已經結束
    and fresh.reset_time >= prev.reset_time + 60   # API 確認進入新視窗（+60s 吃掉 codex reset_after_seconds 的秒級抖動）
    and prev.used_pct >= min_used_pct              # 前一個視窗用得夠多，才值得講
)
state[key] = fresh                                  # 不論是否觸發，一律覆寫成最新
```

- snapshot 中不再出現的 key（profile 被 `remove`）→ 從 state 刪掉。
- 該 provider 的 `list --json` 失敗（exit ≠ 0 / 非 JSON）→ 該 provider 的 state **原樣保留**，下個 tick 再比；不會因為一次網路失敗就丟掉前值或誤報。
- 時鐘倒退（NTP、VM resume）：`prev.reset_time <= now` 為假 → 不觸發，等時鐘追上。與 `notify_once` 對負 age 的處理一致。

### 1.2 `min_used_pct` 是唯一的降噪旋鈕

一個 5H 視窗用了 12% 就重置，沒人想被通知；用到 100% 被 autoswitch 切走、然後重置，才是使用者想知道的「帳號回來了」。一個 `%` 門檻同時解決「哪個視窗要通知」與「多吵」，不需要另開「監看哪個視窗」的選項（`switch_window` 那種 1week/5h 二選一在這裡不對：兩個視窗都可能是使用者卡住的那個）。

### 1.3 監看範圍：所有已存 profile，不只 active

`list --json` 已經回傳所有 profile 的視窗（codex / claude 每個 profile 讀自己的檔、agy 非 active 用 `usage-cache.json`、copilot 每個 profile 打一次 `copilot_internal/user`）。autoswitch 把用完的帳號切走後，那個帳號已經不是 active — 但它正是使用者最想被通知「已重置」的那一個。所以監看整個 `list --json`。

## 2. 每家 provider 的視窗與能力偵測

在 `providers.Provider` 加一個欄位（單一事實來源，不另開 dict）：

```python
reset_windows: tuple[str, ...] = ()   # list --json 裡 usage.<key> 的 key；空 = 不支援
```

| provider | `reset_windows` | 視窗顯示名 | 來源 |
|---|---|---|---|
| codex | `("hourly", "weekly")` | 5h / 1week | `codex_accounts.py:871-872` |
| claude | `("hourly", "weekly")` | 5h / 1week | `claude_accounts.py:788-789` |
| agy | `("gemini_session", "gemini_weekly", "other_session", "other_weekly")` | gemini 5h / gemini 1week / other 5h / other 1week | `gemini_accounts.py:1071-1074` |
| copilot | `("monthly",)` | monthly | `copilot_accounts.py:610`（`plan_usage`，避免與 premium/chat 重複報） |
| grok | `()` | — | 無 quota API |
| vibe | `()` | — | 無 quota API |

**能力偵測分兩層**：靜態 = `reset_windows` 非空才會被 tick 呼叫；動態 = copilot 端點 drift 時回 `usage: null, no_quota_api: true` → 視窗為 None → 自然跳過，不報錯。agy 在 IDE 執行中時非 active profile 走快取，快取有 `reset_time`（`_cached_used_pct` 已依此判斷）→ 同樣可用；但快取 entry 過了 reset 後 `list --json` 回什麼要實測（見 §7）。

## 3. `config` 整合（重點）

依 `CLAUDE.md`「Adding a config setting: one line in the schema」— **只在 `config_schema.FIELDS` 追加兩個 `Field`**，選單 / `config get` / `config set` / `export` / `import` / 非 TTY fallback 全部自動出現，不寫任何 per-key `if`。

```python
Field(
    key="reset_notify",
    type=bool,
    default=False,
    label="Quota-reset notifications",
    help="Tell me when a quota window resets and the account is usable again. Runs on the scheduled timer (ai-accounts install-timer).",
    group="Notifications",
    programs=("ai-accounts", "codex-accounts", "claude-accounts", "agy-accounts", "copilot-accounts"),
),
Field(
    key="reset_notify_min_used_pct",
    type=int,
    default=80,
    minimum=0,
    maximum=100,
    clamp=True,
    label="↳ Only if it was ≥ (%) used",
    help="Skip resets of windows that never got busy: notify only when the window had reached {value}% used before it reset.",
    group="Notifications",
    programs=(same as above),
),
```

設計取捨：

| 決定 | 理由 |
|---|---|
| 放在 `Notifications` 群組、排在 `notify` 之後 | 它「是一種通知」；管道沿用 `notify`（desktop / telegram / none），不另開第二套管道設定 |
| `default=False` | 新通知不該無聲出現；使用者在選單打開它時，畫面就有 help 說明 |
| `programs=` 排除 `grok-accounts` / `vibe-accounts` | 與 `agy_*` 兩個 key 的既有作法一致：沒有 quota API 的 CLI 選單不顯示，但 `config get <key>` / `config set` 仍全 CLI 共用（README 已這樣描述 agy keys） |
| `↳` 前綴 + `{value}` help | 沿用 `switch_when_used_pct` 的視覺慣例與 `display_help` 的 placeholder 機制 |
| 不做「監看哪個視窗」的 choices | 見 §1.2；未來真的需要再加一個 `Field`，不用改邏輯 |
| `token_refresh` 那樣的 bool `config_flag()` 讀取 | fail-closed：手改成字串 `"true"` 視為關 |

另外兩處要碰 `config_menu.py`（不是 per-key 邏輯，是既有的「開了功能但 timer 沒裝」提示）：

- `cmd_config` 目前只在 `enabled` 為 true 且 `autoswitch_setup.is_installed()` 為假時問「Install it now?」。改成 `enabled or reset_notify` — `reset_notify` 沒有 timer 就完全不會動。`autoswitch_setup.install()` 會連 Stop hooks 一起裝；hooks 只跑 `autoswitch`，對重置通知無害，重用同一個 prompt 最省。
- `doctor.py` 的 `_check_timer` 已回報 timer 狀態；加一句 note：`reset_notify` 為 true 而 timer `not installed` 時標 ⚠️。可選，Phase 3。

## 4. 執行路徑

```
timer tick → autoswitch_timer.run_once()
  ├─ enabled        → _run_autoswitch_everywhere()            (既有)
  ├─ token_refresh  → _run_token_refresh_everywhere()         (既有)
  └─ reset_notify   → quota_reset.run_tick()                   (新)
        ├─ collect():  對 reset_windows 非空的 provider 並行跑 `python -m <module> list --json`
        │              → {provider_key: [ {name, usage:{<window>: {percent, reset_time, ...}}} ]}
        ├─ detect(state, snapshot, now, min_used_pct) → (new_state, events)   ← 純函式
        ├─ save state  (`~/.ai-accounts/quota-reset-state.json`, _write_private)
        └─ events 非空 → 一則通知（同 tick 多個事件合併，依 provider 分組）+ terminal 印出
```

State 檔格式（與 `autoswitch-state.json` 同目錄、同寫法）：

```json
{
  "codex/work/hourly":   {"reset_time": 1757490000, "used_pct": 100},
  "claude/main/weekly":  {"reset_time": 1757800000, "used_pct": 93},
  "copilot/gh/monthly":  {"reset_time": 1759276800, "used_pct": 88}
}
```

通知文案（`i18n.MESSAGES`，沿用 `notify.*` 慣例、標題帶方向詞「used」）：

```
notify.reset.title       en: "🔋 {provider}: {profile} {window} quota is available again (was {used}% used)"
                         zh-TW: "🔋 {provider}：{profile} 的 {window} 配額已重置（原已用 {used}%）"
notify.reset.many.title  en: "🔋 ai-accounts: {count} quota windows reset — {providers}"
                         zh-TW: "🔋 ai-accounts：{count} 個配額視窗已重置 — {providers}"
notify.reset.line        en: "• {provider} · {profile} · {window} (was {used}%)"
                         zh-TW: "• {provider} · {profile} · {window}（原 {used}%）"
notify.reset.body        en: "Next reset: {next}"           zh-TW: "下次重置：{next}"
window.5h / window.1week / window.monthly / window.gemini_5h … (視窗顯示名，zh-TW 可留英文縮寫)
```

單一事件用 `notify.reset.title`；多事件用 `many.title` + 每行 `line`。body 尾端接 `u.source_device()`，與 revoked report 一致。用 `notify()`（不是 `notify_once`）— §1 的 state 已保證每次重置只報一次，不需要 cooldown。

## 5. 檔案清單（inventory，共 11 個）

| # | 檔案 | 動作 | 規模 |
|---|---|---|---|
| 1 | `src/ai_accounts/providers.py` | `Provider.reset_windows` 欄位 + 六家的值 + `__main__` self-check 一行 | ~10 行 |
| 2 | `src/ai_accounts/config_schema.py` | 兩個 `Field` | ~25 行 |
| 3 | `src/ai_accounts/i18n.py` | `config.reset_notify*.label/help` zh-TW、`notify.reset.*`、`window.*` | ~30 行 |
| 4 | **`src/ai_accounts/quota_reset.py`（新）** | `state_path()`、`collect()`、`detect()`、`report()`、`run_tick()` | ~130 行 |
| 5 | `src/ai_accounts/autoswitch_timer.py` | `run_once` 第三道 gate（`collect` 可注入，同 `check`/`refresh` 的測試模式） | ~8 行 |
| 6 | `src/ai_accounts/config_menu.py` | install prompt 條件 `enabled or reset_notify` | 1 行 |
| 7 | `src/ai_accounts/doctor.py`（可選） | timer 未裝 + reset_notify 開 → ⚠️ note | ~10 行 |
| 8 | **`tests/test_quota_reset.py`（新）** | `detect()` 的六個情境（見 §6）+ `report()` 文案 + `run_tick` 用注入 snapshot 跑一次寫 state | ~150 行 |
| 9 | `tests/test_autoswitch_timer.py` | gate 三態：`reset_notify` 關不跑、開才跑、與另兩道獨立 | ~30 行 |
| 10 | `README.md` | Auto-switch 章節下新增「Quota-reset notifications」小節；設定說明；延遲 = timer 間隔 | ~40 行 |
| 11 | `TODO.md` | Future work 該項 → 勾選 / 指向本文件 | 3 行 |

`test_config_schema.py` / `test_config_menu.py` / `test_config_cli.py` 都以 `FIELDS` 動態迭代（`test_config_schema.py:48`、`test_config_menu.py:48`），加 `Field` 不需改測試；唯一硬編的「back-compat pin」只檢查舊 key 不消失，同樣不需改。

不碰的：任何 `*_accounts.py` / `*_usage.py`（資料已在 `--json` 輸出裡）、`autoswitch.py` 引擎、`autoswitch_hooks.py`（hooks 只跑 `autoswitch`；重置偵測是 timer-only，事件驅動沒意義）。

## 6. 測試矩陣（`detect()` 純函式，placeholder 資料）

| 情境 | 期望 |
|---|---|
| 第一次看到某視窗（state 無前值） | 不觸發、寫入 state — 「initial 0%」不誤報 |
| 同一視窗連續兩次 tick，`reset_time` 相同 | 不觸發 — 「repeated timer checks」不誤報 |
| `now ≥ prev.reset_time`、新 `reset_time` 更晚、`prev.used ≥ min` | **觸發一次**；state 更新為新值；再跑一次不再觸發 |
| 同上但 `prev.used < min` | 不觸發 |
| `now ≥ prev.reset_time` 但 API 仍回舊 `reset_time`（抖動 < 60s） | 不觸發、保留 prev 等下一 tick |
| provider 的 `list --json` 失敗 / profile 消失 | 失敗：state 原樣；消失：key 移除 |
| 同 tick codex 5h + claude 5h 同時重置 | 一則通知、`many.title`、兩行 |
| copilot `no_quota_api: true` | 跳過、不寫 state、不報錯 |

加上 `run_once` gate 測試（`reset_notify` 假 → `collect` 不被呼叫；手改字串 `"true"` 視為關）。

## 7. 未定 / 需實測

1. **agy 快取 entry 過了 reset 之後 `list --json` 回什麼** — `_cached_used_pct` 會回 0，但 JSON 輸出的 `reset_time` 是否仍是舊值？若是舊值，agy 非 active profile 會等到下次真的量測（IDE 關閉時 timer 量、或 `list --refresh`）才更新 → 通知會延後，不會誤報。可接受，但要寫進 README。
2. **copilot `quota_reset_date` 的時區** — 目前解析成 epoch；重置日 00:00 UTC vs 使用者本地時間差最多一天，只影響「多久後被通知」，不影響正確性。
3. **效能取捨（必須標明）**：`reset_notify` 開啟後每 tick 多一輪 `list --json`（每個 provider、每個 profile 各一次 HTTP）。`enabled` 為 true 時 autoswitch 子行程其實剛剛也量過同一批，但 `run_autoswitch` 不回傳視窗。第一版接受重複量測（30 分一次，量級可忽略）；升級路徑：讓 `autoswitch` 子命令帶 `--json` 吐出量到的視窗給 tick 重用，砍掉一輪。
4. **預設值**：`reset_notify=False`、`min_used_pct=80`。80 而非 90（`switch_when_used_pct` 預設）：使用者手動換帳號時常在 80–90% 就換了，門檻太高會漏掉這些「自己換走的帳號」。可討論。

## 8. 分階段

- **Phase 1（核心，可獨立出貨）**：#1 #2 #3 #4 #5 #8 #9 #10 #11 — 開關、偵測、通知、測試、文件。
- **Phase 2（UX 補完）**：#6 install prompt、#7 doctor note。
- **Phase 3（效能）**：§7-3 的 `autoswitch --json` 重用，只有實測發現 tick 太重才做。

Phase 1 約 250 行程式 + 180 行測試，一個 PR 可完成。
