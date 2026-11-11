# Telegram notification examples

Every notification type `ai-accounts` sends over the Telegram channel, rendered from the real message templates (`src/ai_accounts/i18n.py`) with fake data (`Demo-MacBook` stands in for the device label). The images are Telegram-style renderings, not screenshots of the app. The two "account switched" messages (1, 2) arrive as a photo with the text as its caption.

## 1. Auto-switch: account switched, session restarted

![Auto-switch: account switched, session restarted](01-switched-restarted.png)

## 2. Auto-switch: account switched, restart needed

![Auto-switch: account switched, restart needed](02-switched-restart-needed.png)

## 3. Auto-switch: no account to switch to

![Auto-switch: no account to switch to](03-no-candidate.png)

## 4. Token expired (generic fallback)

![Token expired (generic fallback)](04-relogin-generic.png)

## 5. Token expired (grouped by provider)

![Token expired (grouped by provider)](05-relogin-grouped.png)

## 6. Quota reset (single window)

![Quota reset (single window)](06-quota-reset-single.png)

## 7. Quota reset (several windows)

![Quota reset (several windows)](07-quota-reset-grouped.png)

## 8. Reset voucher expiring (single)

![Reset voucher expiring (single)](08-voucher-expiring-single.png)

## 9. Reset voucher expiring (several)

![Reset voucher expiring (several)](09-voucher-expiring-grouped.png)
