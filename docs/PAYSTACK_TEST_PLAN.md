# Paystack End-to-End Test Plan

_Created 2026-09-11. Verify the full pay → webhook → tier-upgrade path before
launch. Mostly a LIVE/owner test; code prep done (base64-body safety net +
PaystackWebhookUrl output)._

## The full path (how it works)
1. **Chat:** user taps Upgrade → `settings._handle_upgrade_request` →
   `tier_manager.handle_upgrade_request(phone, plan)` → `PaystackService.
   initialize_transaction` → returns a Paystack payment URL sent to the user.
   The user's id (bare phone for WhatsApp, `tg:<chat_id>` for Telegram) is put in
   the transaction **metadata** (`phone_number`, `plan`) — that's what routes the
   upgrade back.
2. **User pays** on the Paystack page (card / transfer / USSD).
3. **Paystack POSTs** `charge.success` to our `/paystack` webhook
   (`handlers/paystack_webhook.lambda_handler`).
4. **Webhook:** verifies the HMAC signature (`X-Paystack-Signature`), reads
   `metadata.phone_number` + `metadata.plan`, calls
   `tier_manager.upgrade_user(phone, plan)` (sets `tier`, `tier_upgraded_at`,
   resets monthly counters), then notifies the user on their own platform via
   `resolve_client` (Telegram or WhatsApp).

## PRE-TEST setup (once)
- [ ] Deploy: `./deploy.sh dev`
- [ ] Get the webhook URL:
      `aws cloudformation describe-stacks --region eu-west-1 --stack-name kashia-bot --query "Stacks[0].Outputs[?OutputKey=='PaystackWebhookUrl'].OutputValue" --output text`
- [ ] In the **Paystack dashboard → Settings → API Keys & Webhooks**, set the
      **Webhook URL** to that URL. (If unset, payment succeeds but NO upgrade.)
- [ ] Confirm the SSM secret matches the dashboard: `/kashia/paystack-secret-key`
      must be the SAME account's secret key whose webhook you're registering
      (test-mode secret ↔ test-mode webhook; live ↔ live). A mismatch = every
      signature check fails (401).

## THE TEST
1. [ ] In Telegram, open Help & Settings → Upgrade → pick Basic (₦3,000).
2. [ ] Confirm you receive a payment link message.
3. [ ] Open the link, pay with a **Paystack TEST card** (if secret is test-mode):
       card 4084 0840 8408 4081, any future expiry, CVV 408, PIN 0000, OTP 123456.
       (Use a real small payment only if you're on LIVE keys.)
4. [ ] Within seconds, you should get the "🎉 Upgrade Successful!" message in
       Telegram.
5. [ ] Verify the upgrade stuck: Help & Settings → Usage & Limits should show
       the new tier (Basic/Pro) with unlimited transactions.

## WHAT TO CHECK IN LOGS (CloudWatch → kashia-paystack-webhook-dev)
- [ ] "Payment received: <id> → <plan>" appears (webhook fired + parsed).
- [ ] NO "Invalid Paystack webhook signature" (that = 401; see failure modes).
- [ ] "User upgraded: <id> → <plan>".

## LIKELY FAILURE MODES + fixes
- **401 "Invalid signature":** (a) wrong secret in SSM vs dashboard account/mode;
  (b) body arriving base64 — we added a decode safety net, but confirm; (c) the
  raw body was altered. Fix: align SSM secret with the dashboard's mode.
- **"missing_data":** metadata didn't carry phone/plan — check
  initialize_transaction sent metadata (it does). Old links from before a change
  could lack it.
- **No webhook at all:** URL not registered in the dashboard, or registered to a
  different stage/account. Re-register the PaystackWebhookUrl.
- **Upgrade message not received:** upgrade DID happen (check Usage) but the
  notify step failed (resolve_client / send) — non-critical; the tier is set.
- **callback_url** is `https://kashia.app/payment/success` (a placeholder). After
  paying, the browser redirects there; it may 404 if that page doesn't exist yet.
  This does NOT affect the upgrade (the webhook is server-to-server). Cosmetic —
  set a real success page for launch.

## Notes / possible polish (not blocking the test)
- The webhook is idempotent-ish: re-sending the same charge.success just re-sets
  the same tier (harmless). No double-charge risk (Paystack charges once).
- Plan amounts: Basic ₦3,000 (300000 kobo), Pro ₦6,000 (600000 kobo) — in
  services/paystack.py PLANS.

---

## Re-test after pricing redesign — 2026-09-23 (commits `ee36588`, `5d5040b`)

Ran a live TEST-mode payment on `tg:1072412276` (Basic / Monthly). The redesign
WORKS — the webhook fired, charged ₦3,500 (350000 kobo), and upgraded the user
(`ends 2026-10-23`). But the real payment surfaced 3 issues (all fixed):

1. **No success message to the user.** Webhook log:
   `Telegram sendMessage ... can't parse entities ... byte offset 311`.
   The success text ended with `_Ref: kashia_basic_monthly_tg_..._` — the ref is
   full of underscores and Telegram Markdown reads each `_` as an italic toggle,
   so the message was rejected. Upgrade succeeded; only the confirmation failed.
   **Fix (`ee36588`):** ref printed on a plain line, no markup.

2. **Idempotency guard silently disabled.** Webhook log:
   `claim_web_submit error: AccessDeniedException ... dynamodb:PutItem on
   kashia-transactions-dev`. The guard added in `58e9b43` writes to the
   TransactionsTable, but PaystackWebhookFunction only had CRUD on UsersTable →
   the double-charge protection never actually ran. **Fix (`ee36588`):** granted
   TransactionsTable CRUD in template.yaml (→ deploy required).

3. **Amount ₦3,654.83 vs ₦3,500 on the receipt** — NOT a bug. That's Paystack's
   own fee (₦154.83) shown to the payer; we send exactly 350000 kobo.

4. **Usage screen "showed the old way".** After paying, Settings → Usage &
   Limits still showed only bare limits with no subscription info. **Fix
   (`5d5040b`):** paid tiers now lead with period + renewal date + days-left
   (grace/expired variants too), via `subscription_status`. Free unchanged.

### Test C (14-day trial) — now verifiable via `verify_trial.py`
```
python verify_trial.py <user_id>                       # read-only status
python verify_trial.py <user_id> --simulate-days 20    # dry-run post-trial (restores)
python verify_trial.py <user_id> --simulate-days 20 --commit   # persist backdate
```

### Still to verify after deploy
- New payment → success message now arrives in the bot.
- Duplicate webhook (replay) → rejected as `duplicate` (guard now has IAM).
- Usage screen shows the subscription window for the paid account.
