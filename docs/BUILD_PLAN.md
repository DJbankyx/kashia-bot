# Kashia Bot — Build Plan

_Last updated: 2026-09-03_

This is the working execution plan for getting Kashia to a soft launch and
beyond. It consolidates the roadmap, the code-review findings ("lapses"), and
the tech-debt backlog into one prioritized list with clear acceptance criteria.

Source of truth for high-level status stays in `.kiro/steering/roadmap.md`.
This document is the detailed, task-level plan Kiro works from.

---

## Guiding Priorities

1. **Stabilize** what testers touch first (Sprint 1).
2. **Unblock launch** (real WhatsApp number, templates, Paystack, legal).
3. **Harden** security and reliability (tech debt from code review).
4. **Grow** with post-launch features.

Correctness and reliability beat new features until launch is unblocked.

---

## Multi-Platform: Telegram (LIVE as of 2026-09-03)

**Why:** WhatsApp Business per-message charges begin **1 Oct 2026** (service
messages become billable; ~₦14 each in Nigeria for utility/service messages).
Kashia is chatty, so that cost scales with engagement. Telegram is free (no
per-message fee, no template approval, no Meta business verification), so we
added it as a second, independent bot on the **same engine**.

### Architecture
- One shared engine (router, features, industries, services). Handlers return
  neutral response dicts; each platform renders them its own way.
- `services/messaging_client.py` — `MessagingClient` ABC (`send_text`,
  `send_buttons`, `send_list`, `send_document`) + `resolve_client()` /
  `platform_for_user()` / `bare_recipient_id()` helpers.
- `services/whatsapp_client.py` and `services/telegram_client.py` implement it.
- `handlers/webhook.py` (WhatsApp) and `handlers/telegram_webhook.py` (Telegram)
  are thin, independent inbound adapters → both call the same
  `bot.handle_message(user_id, text, type, platform=...)`.
- **Identity:** WhatsApp users keep bare phone-number keys; Telegram users are
  namespaced `tg:<chat_id>`. The `TelegramClient` strips `tg:` before hitting
  the API. Optional account-linking is a future add.
- Platform-aware everywhere: replies, documents/PDFs (`deliver_file`),
  scheduled reports, Paystack notices, onboarding copy, and profile display.

### Deploy & setup (Telegram)
1. **Create the bot:** message `@BotFather` in Telegram → `/newbot` → get token.
   (Current bot: `@KashiaFinance_Bot`.)
2. **Store secrets in SSM (eu-west-1):**
   ```bash
   aws ssm put-parameter --region eu-west-1 --name /kashia/telegram-bot-token \
     --type SecureString --value "<BOTFATHER_TOKEN>"
   # optional but recommended (fail-closed in prod); generate: openssl rand -hex 32
   aws ssm put-parameter --region eu-west-1 --name /kashia/telegram-webhook-secret \
     --type SecureString --value "<RANDOM_STRING>"
   ```
3. **Deploy:** `./deploy.sh dev` (generic; picks up the Telegram Lambda/route).
4. **Register webhook:** `./set_telegram_webhook.sh dev`
   (pulls token+secret from SSM, reads `TelegramWebhookUrl` from the CFN stack,
   calls Telegram `setWebhook`, prints `getWebhookInfo`).
5. **Test:** open `t.me/<bot_username>`, tap START (sends `/start`).

### Infra added to template.yaml
- `TelegramWebhookFunction` (handler `handlers/telegram_webhook.lambda_handler`,
  `POST /telegram`, same DynamoDB/S3/SSM perms, `STAGE` env for the secret check).
- `TelegramWebhookErrorsAlarm`, `TelegramWebhookThrottlesAlarm`, and the
  `TelegramWebhookUrl` output.

### Debugging (how we fixed the first go-live bug)
- `curl .../getWebhookInfo` shows what Telegram thinks (URL + last error).
- CloudWatch log group `/aws/lambda/kashia-telegram-webhook-dev` shows the
  inbound → engine → send flow. The "chat not found" 400 was the namespaced
  id reaching the API un-stripped; fixed by normalizing `chat_id` in
  `TelegramClient._call()`.

### Known follow-ups
- Paystack `reference`/email sanitize `tg:` → `tg_` (done); metadata keeps the
  full id for routing.
- Telegram-native richness (inline keyboard grids, message editing, `/`-commands,
  Mini App) — see the "Telegram UX / Feature Upgrades" backlog below.

---

## Telegram UX / Feature Upgrades

Telegram lets us build a richer, tap-first UI than WhatsApp's 3-button / 10-row
limits allow. Phases 1–6 shipped 2026-09-03 (all verified, WhatsApp untouched):

- [x] **Persistent command menu** (2026-09-03) — `/menu /sale /report /debts /help
      /start` via `setMyCommands` (`set_telegram_commands.sh`). Feature commands
      route through the button dispatcher. Source of truth: `COMMAND_MENU` in
      `handlers/telegram_webhook.py`.
- [x] **Typing indicator** (2026-09-03) — `sendChatAction: typing` on every
      inbound message/tap (`TelegramClient.send_chat_action` + `_show_typing`).
- [x] **Richer inline keyboards** (2026-09-03) — `_buttons_to_keyboard` grid-packs
      by label length (3/2/1 per row); `send_list` grids pure pickers, keeps rich
      menus 1-per-row with folded descriptions.
- [x] **Message editing + paginated pickers** (2026-09-03) — `edit_message_text`,
      `send_and_get_id`, `page_keyboard` (◀ Prev / Next ▶ via reserved
      `__tgpg__` callbacks). `main._maybe_send_paginated_list` renders long
      description-less pickers as pages; webhook `_handle_page_nav` edits in
      place without touching the engine. Options stashed in session (`__tg_page`).
- [x] **Reminders that actually send — roadmap Q3** (2026-09-03) — 
      `build_recurring_reminder` in `scheduled_reports.py`; recurring due-soon/
      overdue nudges now delivered per-platform (no template wall on Telegram).
      `get_active_users` widened to include recurring-only users.
- [x] **Voice-note transcription** (2026-09-03) — `_handle_voice_note`: getFile →
      download OGG → OpenAI Whisper (`whisper-1`) → dispatched as normal text
      into the transaction parser. Graceful fallback to "please type it".

**Activation:** `./deploy.sh dev` ships phases 1–6; `./set_telegram_commands.sh`
(one-time) populates the `/` menu. No new SSM params (voice reuses the OpenAI key).

**Still open / future:**
- [ ] **Every menu edits in place** (beyond pagination) — needs the engine to
      signal navigation-update intent. Deferred (larger change).
- [ ] **Document scanning (receipts / invoices / quotes)** — see next section.
- [ ] **Mini App** (later) — in-chat web view for a full dashboard / charts.

---

## Document Scanning & Smart Auto-Record (proposed — next big feature)

Goal: user snaps/forwards a photo or PDF of a receipt, invoice, or quote; Kashia
reads it (vision OCR), figures out WHAT it is and WHERE it belongs, then either
auto-records it or guides a quick confirm. Builds directly on the image pipeline
(both webhooks already download images to S3) and the existing categorizer.

Proposed phasing:
- [ ] **A. Vision extraction** — send the image/PDF to an OpenAI vision model;
      extract structured fields (vendor, date, line items, total, tax, doc type).
- [ ] **B. Document-type classification** — decide receipt vs invoice vs quote,
      and purchase vs sale, from content + context (who sent it, industry).
- [ ] **C. Smart routing** — map to the right record: a purchase/expense (with
      the right expense_class/category via the categorizer), a sale, or a
      non-transaction (a quote → save as a quote, not a ledger entry).
- [ ] **D. Confirm-or-auto UX** — high-confidence → auto-record with an
      "undo/edit" button; low-confidence → pre-filled confirmation card.
- [ ] **E. Attach the source doc** — keep the scanned file linked to the record
      (S3) for audit/proof.

---

## Sprint 1 — Stabilize

Fix the rough edges testers hit first.

| # | Task | Status | Acceptance Criteria |
|---|------|--------|---------------------|
| 1 | Sale picker for manufacturing — finished products only | ✅ Done (2026-08-27) | Raw materials, overhead, and consumables never appear in the Sell Output picker; only finished/sellable items show. |
| 2 | Unit conversion display | ✅ Done (2026-08-27) | Quantity prompt shows the tracked unit; picker rows show unit; confirmation shows "3 bags = 150 kg". |
| 3 | Edit recipe flow | ✅ Done (2026-08-27) | User can edit qty/cost of an existing recipe material via Edit Quantity / Edit Cost. |
| 4 | Hybrid industry sync | ✅ Done (2026-08-27) | Quotes wired into hybrid menu (deposits + supplies already worked via generic paths). Also fixed dashboard product/service split with a persisted `sale_kind` marker. |
| 5 | Edit recurring services | ✅ Done (2026-08-27) | Edit/Manage entry lists all services; edit client/service/amount/frequency; frequency change recomputes next_due. |
| 6 | Sale picker for trading — "Other/Not Listed" always shows | ✅ Done (2026-08-27) | Picker rows hard-capped at 9 before appending "Other", so it always shows and never exceeds WhatsApp's 10-row limit. |

### Notes on completed items
- **#1 (done):** `catalog.get_product_list_for_recording` now calls
  `ensure_item_types()` for manufacturing/hybrid before filtering and uses an
  allowlist of sellable types (`finished_product`, `product`, `service`, `""`).
  This closes the gap where recipe-only raw materials with an unset `item_type`
  leaked into the sale picker.

---

## Launch Prep — Blocking a Real Launch

| Task | Status | Notes |
|------|--------|-------|
| Real WhatsApp Business number + Meta verification | ⬜ Blocked | Adding +234 901 640 3500 fails with "Unexpected null value for wabaID" — Meta platform issue. On test number for now. Revisit / open Meta support ticket. |
| WhatsApp message templates | ⬜ To do | Meta requires approval for outbound reports/reminders. Draft + submit templates. |
| Paystack end-to-end test | ⬜ To do | user pays → webhook → tier upgrade. Verify full path in a staging run. |
| Monthly counter reset | ✅ Done (2026-08-27) | `MonthlyResetFunction` (handlers/monthly_reset.py) scans all users and calls `reset_monthly_counters` on the 1st at 00:05 UTC; has DLQ. | ✅ Done |
| Privacy policy + Terms of Service | ⬜ To do | Required by Meta for Business API. |
| Landing page | ⬜ To do | One-pager with WhatsApp link. |
| Onboarding final UX pass | ✅ Done (2026-08-27) | Progress markers (Step X of N); shared button-ID/command guard on name + description steps; *skip* option on product steps (empty catalog); cleaner product extraction (strips location phrases like "in Lagos", drops >4-word junk); trading/hybrid now get catalog/price next-steps guidance like mfg/services. (Business-name re-ask was reviewed — no change needed.) |

---

## Security & Reliability Hardening (from code review)

These are the "lapses" surfaced in review. Ordered by risk.

| # | Issue | Risk | Fix | Status |
|---|-------|------|-----|--------|
| S1 | PIN stored as unsalted SHA-256 | High | ✅ Done (2026-08-27) — salted PBKDF2 (200k iters) in `utils/pin_security.py`; legacy SHA-256 verified + auto-upgraded on next successful entry (no lockout). | ✅ Done |
| S2 | No retry/backoff on WhatsApp API sends | Med | ✅ Done (2026-08-27) — `_send` retries 429/5xx/timeouts/connection errors up to 3x with exponential backoff (honors Retry-After); non-transient 4xx not retried. | ✅ Done |
| S3 | Session race condition on rapid concurrent messages | Med | ✅ Partial (2026-08-27) — sessions now carry a `version`; `save_session` supports conditional writes and `SessionManager.update_context` does a conditional read-modify-write with retry. Direct `save()` calls still overwrite unconditionally (see note). | 🟡 Partial |
| S4 | `check_can_generate_pdf` has `TODO: re-enable tier check after beta` | Low | Re-enable the tier gate when beta ends. | ⬜ Parked (beta) |
| S5 | Router is 900+ lines | Low | ✅ Done (2026-08-27) — extracted the ~225-line `_route_button` chain into `core/button_dispatcher.py` (`ButtonDispatcher`); router delegates. Behavior-neutral; verified by constructing the router and dispatching a sample button. | ✅ Done |
| S6 | No CloudWatch alarms / DLQ on webhook Lambda | Med | ✅ Done (2026-08-27) — SQS DLQ on the async ScheduledReports fn (webhook is sync, so DLQ N/A there); CloudWatch alarms for webhook Errors & Throttles + DLQ-not-empty. Alarms have no SNS action yet (visible in console); wire notifications later. | ✅ Done |

---

## Repo Hygiene

| Task | Status | Notes |
|------|--------|-------|
| Remove dead monolith snapshots | ✅ Done (2026-08-27) | Deleted `conversation_engine_current.py`, `conversation_engine_latest.py`, `database_latest.py`. Confirmed unreferenced. |
| Decide fate of root maintenance scripts | ⬜ Pending decision | `check_syntax.py`, `cleanup_contacts.py`, `fix_conversion_match.py`, `fix_qty_display.py`, `quick_reset.py`, `reset_all_testers.py`, `reset_both_users.py`, `reset_user.py`. Suggest moving to a `scripts/` folder rather than deleting, since some are active. |

---

## Industry Quality Findings (from services/hybrid review)

Findings from a critical review of the Services and Hybrid industries. Two were
fixed on 2026-08-27; the rest are tracked here.

| # | Finding | Status | Notes |
|---|---------|--------|-------|
| Q1 | `expense_class` relied on a manual tap; unclassified expenses dumped into overhead, making gross profit misleading | ✅ Done (2026-08-27) | Expenses now get a default `expense_class` derived from category (COGS category → direct, else indirect) written at save time; the classify prompt still overrides. |
| Q2 | Vendor/client blocklist duplicated & inconsistent across 6+ places | ✅ Done (2026-08-27) | Centralized into `utils/parser.py` (`BAD_VENDOR_NAMES`, `is_bad_vendor`, `clean_vendor`); all copies replaced. |
| Q3 | Recurring "due soon" shows in-app but reminders don't actually send | ⬜ To do | Needs the scheduled Lambda + approved WhatsApp templates (see Launch Prep). In-app display works; no outbound notification yet. |
| Q4 | No service duration / job completion tracking | ⬜ To do (post-launch) | A job is a one-shot sale. "In progress vs completed" + time tracking would make services first-class. Product gap, not a bug. |
| Q5 | Hybrid duplicates trading + services logic instead of composing them | ⬜ To do (pre-launch refactor) | Hybrid re-implements dashboards/menus, so it lags the specialized industries and is the buggiest. Recommend a focused pass where hybrid delegates service-side behavior to the services logic. The `sale_kind` fix patched a symptom; the architecture remains duplicative. |

---

## Post-Launch Queue (parked)

- Multi-currency (Phase 1 — manual exchange rate)
- Returns / refunds
- Bulk stock import ("stock: shoes 50, bags 30")
- Smart reports (profit per product, margin ranking)
- Proactive low-stock alerts (scheduled Lambda)
- Customer loyalty insights
- i18n infrastructure (English, Pidgin, French)
- Account deletion flow ("Delete my data" — compliance)
- Voice note transcription (WhatsApp audio → text)

---

## Suggested Order of Attack

1. Finish Sprint 1 (#2 → #6) — quick wins testers feel immediately.
2. Ship S1 (PIN hashing) and S2 (WhatsApp retry) — cheap, high-value hardening.
3. Work Launch Prep in parallel: draft templates + legal pages while chasing
   the Meta `wabaID` blocker.
4. Add S6 (DLQ + alarms) before onboarding real users.
5. Tackle S3/S5 refactors once launch is stable.
