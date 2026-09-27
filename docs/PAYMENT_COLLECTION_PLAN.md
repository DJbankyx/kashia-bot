# Payment Collection — Design & Build Plan

_Drafted 2026-09-26. Status: PLAN (not yet built). This is the "real revenue
layer" flagged in the roadmap: turn an invoice/bill into a Paystack pay-link the
customer can pay, and when they pay, auto-record the money, settle the debt, and
notify the owner._

> Money-critical. Every webhook path here MUST reuse the idempotency guard
> (`db.claim_web_submit` keyed on the Paystack reference) exactly like the
> subscription webhook already does. Treat this doc as the source of truth;
> update it as we build.

---

## 1. Goal & scope

**Goal:** a business owner sends a customer a link; the customer pays with
card/transfer/USSD on Paystack; Kashia automatically records a paid sale, settles
any matching receivable, and tells the owner "₦X received from <customer> for
<invoice>."

**In scope (v1):**
- Create a **payment request** for an amount + description (+ optional customer +
  optional link to an existing debt/invoice number).
- Generate a **Paystack pay-link** for that request (arbitrary amount, not a plan).
- On `charge.success`, **auto-mark it paid**: record a `sale` transaction
  (paid), settle the customer's receivable if one exists, notify the owner.
- Surfaces: **Telegram chat** (a "Request payment" action) and the **Mini App**
  (a "Request payment" / "Get pay-link" button, e.g. on a customer or an invoice).
- Idempotent + safe against Paystack's at-least-once retries.

**Explicitly OUT of scope (v1) — deferred:**
- Markup/fees on collected payments (owner: test willingness-to-pay first; we send
  the exact amount, Paystack takes its own fee from the payer as today).
- Recurring/subscription billing of the owner's OWN customers.
- Partial payments against one pay-link (v1 = pay the full requested amount).
- A hosted "pay page" we build ourselves — we use Paystack's hosted checkout.
- Refund/void of a collected payment via the bot (do it in the Paystack dashboard
  for v1; the recorded sale can be voided with the existing `void_transaction_web`).

**Non-negotiables:**
- WhatsApp must not break (shared engine; new surfaces Telegram/Mini-App-gated).
- Money stays kobo-precise (`utils/money`); Paystack stays integer kobo at its
  own boundary.
- No forked business logic — reuse `save_transaction`, `settle_debt`, the debt
  settle-then-log-a-sale pattern, `resolve_client`.

---

## 2. Why this needs a new persisted record

Today (confirmed in code):
- **Invoices are ephemeral PDFs.** `InvoiceHandler` + `PDFGenerator.generate_invoice`
  build a numbered PDF (`{INITIALS}-{counter:05d}`, counter on the user row) and
  deliver it. Nothing is persisted; there is **no invoice object, no status, no
  invoice→sale/debt link**.
- `PaystackService.initialize_transaction` is **hard-wired to subscription PLANS**
  — it derives the amount from `PLANS`, so it cannot take an arbitrary invoice
  amount as-is.
- The webhook routes on `metadata.phone_number` + `metadata.plan`; a charge with
  no `plan` currently just returns `200 {"status":"missing_data"}` and does
  nothing.

So payment collection cannot "reuse the invoice object" — there isn't one. We
introduce a small **PaymentRequest** record that ties {owner, amount, customer,
optional debt/invoice ref} to a Paystack reference, and the webhook resolves the
paid charge back to it.

---

## 3. Data model — `PaymentRequest`

Store payment requests in the existing **TransactionsTable** (the Paystack webhook
already has CRUD there; no new table, no new IAM for the read/claim path) using a
namespaced sort key so they never collide with real transactions or idempotency
markers:

```
phone_number   = owner id (bare phone or "tg:<chat_id>")   # partition key
transaction_id = "payreq#<uuid>"                            # sort key (namespaced)
type           = "payment_request"                          # excluded from P&L/records/reports
status         = "pending" | "paid" | "cancelled" | "expired"
amount         = Decimal naira (kobo-precise, money_round at write)
description    = free text ("Invoice BFH-00012", "50 bags of rice", ...)
customer_name  = optional contact name (for settle_debt + notify)
invoice_number = optional (link to a generated PDF's number, display only)
debt_link      = optional {"contact": name}  # if set, settle receivable on pay
paystack_ref   = "kashia_pay_<safe_id>_<uuid8>_<ts>"   # unique per request
payment_url    = Paystack authorization_url (cached from init)
created_at, paid_at, retain_until (soft-delete/retention friendly)
paid_tx_id     = the sale transaction id created when paid (back-link)
```

Rules:
- `type="payment_request"` MUST be **excluded** from `period_pnl`, records lists,
  returns pickers, and reports — mirror how `cash_adjustment` is excluded. The
  request itself is not income; the **sale created when it's paid** is the income.
- The `_live`/soft-delete machinery already ignores unknown types in reads that
  filter by concrete type, but audit every reader that does a broad scan
  (e.g. `list_deleted_transactions`, cash/position sums) to confirm
  `payment_request` rows are skipped. Add explicit guards where a broad scan
  could pick them up.

New `database.py` helpers (thin, kobo-safe):
- `create_payment_request(phone, amount, description, customer_name=None, invoice_number=None, debt_contact=None) -> dict`
- `get_payment_request_by_ref(paystack_ref) -> dict|None`  (needs a lookup by ref;
  simplest: store `paystack_ref` and query the owner's rows, OR carry the owner id
  in the ref so we can `get_item` directly — see §5 note)
- `mark_payment_request_paid(phone, payreq_id, paid_tx_id) -> dict`
- `list_payment_requests(phone, status=None) -> [ ... ]`  (for a "pending links" view)

---

## 4. Paystack: an arbitrary-amount pay-link

`initialize_transaction` today only takes a `plan`. Add a sibling that takes an
explicit amount + a purpose discriminator, WITHOUT touching the subscription path:

```python
def initialize_collection(self, owner_id, amount_naira, description,
                          reference, customer_email=None, metadata_extra=None) -> dict:
    # amount_naira -> kobo = int(money_round(amount) * 100) at the Paystack boundary
    # email fallback: f"{_paystack_safe(owner_id)}@kashia.app"
    # callback_url: a friendly "thank you" page (reuse the existing placeholder
    #   https://kashia.app/payment/success for v1)
    # metadata = {
    #     "purpose": "collection",         # <-- discriminator the webhook branches on
    #     "owner_id": owner_id,            # who gets paid (namespaced id)
    #     "payreq_id": "payreq#<uuid>",    # ties back to the PaymentRequest row
    #     "description": description,
    #     "customer_name": customer_name,  # optional
    # }
    # returns {success, payment_url, reference}
```

Reference format: `kashia_pay_<safe_id>_<uuid8>_<ts>` — the `kashia_pay_` prefix
lets us (and any human reading Paystack) distinguish collection charges from
`kashia_<plan>_...` subscription charges at a glance. Keep it underscore-only so
it's URL-safe (but see the Markdown-escape note in §7).

> Do NOT overload the existing `initialize_transaction` — a plan and an invoice
> amount are different enough that a shared function invites the exact category of
> bug (wrong amount / wrong metadata) we can least afford in money code.

---

## 5. Webhook: branch on `purpose`

Extend `paystack_webhook.lambda_handler`. The branch goes **right after metadata
extraction, before the `plan`-based upgrade** (the current `missing_data` return
is exactly the dead spot an unrecognised charge falls into today):

```
... verify signature (unchanged) ...
... json.loads, verified_and_parsed = True ...
if event != "charge.success": return 200 ignored

data     = payload["data"]
metadata = data["metadata"]
reference = data["reference"]
purpose  = metadata.get("purpose", "")

# IDEMPOTENCY FIRST (shared guard, keyed on the Paystack reference) — for BOTH
# purposes. Claim once; a retry/duplicate returns 200 duplicate.
owner_id = metadata.get("owner_id") or metadata.get("phone_number")
claimed, _ = db.claim_web_submit(owner_id, f"paystack#{reference}")
if not claimed: return 200 {"status":"duplicate"}

try:
    if purpose == "collection":
        _handle_collection_paid(db, metadata, data, reference)   # NEW
    else:
        # existing subscription upgrade path (plan/period/upgrade_user/notify)
        ...
except Exception:
    db.release_web_submit(owner_id, f"paystack#{reference}")
    raise   # -> 500 so Paystack retries; release lets the retry re-process
return 200 success
```

`_handle_collection_paid(db, metadata, data, reference)`:
1. `payreq_id = metadata["payreq_id"]`, `owner_id = metadata["owner_id"]`.
2. Load the `PaymentRequest` (`get_payment_request_by_ref` or `get_item` via
   owner_id+payreq_id). If missing → log + return (charge is real but we can't map
   it; do NOT crash the webhook). If already `status=="paid"` → treat as duplicate
   (belt-and-braces on top of the idempotency claim).
3. Record the sale via the shared engine — reuse the debt settle-then-log-a-sale
   pattern from `debt._apply_directed_payment`:
   - `db.save_transaction(owner_id, amount, "sale", description, "Sales & Income",
     vendor=customer_name, payment_method="cash")`  (paid → cash/transfer in).
   - if `debt_link`/customer has a receivable: `db.settle_debt(owner_id,
     customer_name, amount, "owed_to_me")`.
4. `db.mark_payment_request_paid(owner_id, payreq_id, paid_tx_id)`.
5. Notify the owner via `resolve_client(owner_id, whatsapp_fallback=whatsapp)` +
   `send_text` — "✅ ₦X received from <customer> for <description>." Escape any
   underscore-heavy token (ref, link) — see §7.

Amount handling: use `data["amount"]` (kobo, integer, from Paystack — the amount
actually paid) as the source of truth, converted to naira for the sale, and
cross-check it against the PaymentRequest amount; log a `cost_sanity`-style flag
if they differ (shouldn't, since we set a fixed amount).

---

## 6. IAM / routing / infra changes (⚠️ require `sam deploy`)

- **ContactsTable CRUD on `PaystackWebhookFunction`** — the collection-paid path
  calls `settle_debt` + CRM updates, which write the ContactsTable. The webhook
  function does NOT have Contacts CRUD today (only Users + Transactions). Add
  `DynamoDBCrudPolicy: !Ref ContactsTable`. Without it the paid path throws
  `AccessDenied` (same class as the earlier "idempotency silently off" bug).
- **New API routes** (only if we add a Mini App / chat surface that creates the
  link via an HTTP endpoint):
  - `POST /app/api/payment-request` on `MiniAppFunction` (create request + return
    pay-link). MiniAppFunction already has Users/Transactions/Contacts CRUD + SSM,
    but it needs the Paystack secret (SSM `kashia/paystack-secret-key`) — confirm
    its `SSMParameterReadPolicy` covers `kashia/*` (it does) so it can call
    `initialize_collection`.
  - Optionally `GET /app/api/payment-requests` (list pending links).
  - Declaring a route REQUIRES a deploy or it 404s.
- No new SSM params (reuse `kashia/paystack-secret-key`). Same test/live-mode
  discipline as the subscription webhook (test secret ↔ test webhook URL).

---

## 7. Telegram Markdown pay-link gotcha (learned the hard way)

`telegram_client.send_text` always sends `parse_mode=Markdown` and there is NO
central URL/Markdown escaper. Paystack pay-links AND references are
underscore-heavy. An odd count of `_`/`*` = HTTP 400 "can't parse entities" and
the WHOLE message (with the link!) is dropped — the exact bug that hid the upgrade
confirmation (fixed in `c576ad3` by escaping `_`/`*`/`\` at the call site).

Plan:
- When sending the pay-link to the owner (to forward) or in the paid confirmation,
  **escape `_`, `*`, `\` in the URL/ref**, OR keep the link on its own line with no
  surrounding markup and rely on `disable_web_page_preview:True` (already set).
- Consider a small shared helper `tg_ui.md_escape(s)` so we stop re-implementing
  the escape at each call site (low-risk refactor; the webhook's `safe_ref` and
  this feature both use it). Note in the guardrails: never send an unescaped
  underscore-heavy URL/ref via `send_text`.

---

## 8. Build phases (each: write → verify → commit → push → owner deploys)

**Phase 0 — plan + infra prep (this doc).** Add ContactsTable CRUD to the webhook
function in template.yaml as part of the first code phase's deploy.

**Phase 1 — engine (no UI):**
- `PaymentRequest` model + `database.py` helpers (create/get-by-ref/mark-paid/list).
- `PaystackService.initialize_collection`.
- Ensure `type="payment_request"` is excluded from P&L/records/reports/returns
  (audit + guard broad scans).
- Unit-level check via `check_syntax.py`; a small local script asserting a
  round-trip (create request → simulate paid → sale recorded + debt settled +
  request marked paid) with a fake db, like the existing verify scripts.

**Phase 2 — webhook branch: ✅ DONE (commit 5842047, 2026-09-27).**
- `purpose`-dispatch + `_handle_collection` in `src/handlers/paystack_webhook.py`,
  reusing the shared `claim_web_submit(reference)` idempotency claim.
- Records a paid sale; AUTO-SETTLES the customer's receivable when
  `debt_owed_to_me > 0` (else a fresh paid sale); marks the PaymentRequest paid;
  notifies the owner (Markdown-escaped ref).
- Post-claim failure → release claim + 500 (Paystack retries); notify best-effort.
- template.yaml: ContactsTable CRUD added to `PaystackWebhookFunction` (⚠️ deploy).
- Verified: check_syntax OK; webhook PYC_OK; fakes round-trip green (fresh sale /
  auto-settle / duplicate rejected). Real TEST-mode payment still to run post-deploy.

**Phase 3 — Telegram chat surface:**
- A "💳 Request payment" action (e.g. under a customer / after an invoice) →
  ask amount + description (+ optional customer) → create request → send the
  owner the pay-link to forward (escaped per §7). Telegram-gated.

**Phase 4 — Mini App surface: ✅ DONE (commit bfb2e5b, 2026-09-27).**
- `POST /app/api/payment-request` on MiniAppFunction (⚠️ new route → deploy) +
  a "💳 Get pay-link" button on the customer detail card (shown for anyone
  billable, not a pure supplier). Sheet: amount (prefilled with what they owe) +
  description → returns a copyable Paystack link the owner forwards.
- `_payment_request_write` wraps `initialize_collection` + `create_payment_request`
  (fails loudly if the request can't persist).
- Also shipped: `make_paylink.py` (CLI to mint a link for TEST-mode testing
  before the UI existed — kept as a dev tool).
- Verified: check_syntax OK; miniapp PYC_OK; built-page encode OK / 0 surrogates;
  pay-link UI present; esprima JS_PARSE_OK. Real TEST-mode payment still to run
  post-deploy (see §9). "Pending links" view moved to Phase 5.

**Phase 5 — polish:**
- "Pending payment links" view with cancel/expire.
- Receipt PDF auto-generated on paid (reuse `generate_receipt(transaction_id)`).
- Optional: link a generated invoice PDF's number to the request for a clean
  "Invoice BFH-00012 — paid" story.

---

## 9. Test plan (extends docs/PAYSTACK_TEST_PLAN.md)

Use Paystack TEST keys + the test card (4084 0840 8408 4081, any future expiry,
CVV 408, PIN 0000, OTP 123456).

- [ ] Create a payment request for ₦2,500 to "Sandra" → get a pay-link.
- [ ] Pay it (test card). Within seconds: owner gets "✅ ₦2,500 received from
      Sandra…"; a `sale` shows in Records; Sandra's receivable (if any) drops.
- [ ] The subscription upgrade flow STILL works unchanged (regression).
- [ ] Duplicate/replayed collection webhook → `200 duplicate`, no double sale.
- [ ] Post-claim failure (simulate) → `500` + claim released → retry records once.
- [ ] Pay-link/confirmation message renders (no "can't parse entities" 400).
- [ ] `payment_request` rows never appear in P&L, Records, returns picker, or the
      recently-deleted list.
- [ ] WhatsApp path unaffected.

---

## 10. Owner decisions — LOCKED (2026-09-26)

1. **First surface = the Mini App customer card.** "Get pay-link / Request
   payment" lands on the customer detail view (which already shows history), and
   the Mini App is easier to iterate than the chat flow. Chat surface (Phase 3)
   comes after. → build order becomes Phase 1 → 2 → **4 (Mini App)** → 3 (chat).
2. **Auto-settle debt on pay = YES.** If the paying `customer_name` has an open
   receivable (`debt_owed_to_me > 0`), the paid link SETTLES it (mirroring
   `debt._apply_directed_payment`: `settle_debt` + log the settling `sale`),
   rather than creating a second unrelated sale. If there is NO open receivable,
   record a fresh paid `sale`. This is the "free auto-reconciliation" hook the
   roadmap calls the sticky feature. (Owner confirmed; note it mutates a
   customer's balance from an external event — intended.)
3. **Fees/markup = NONE in v1.** Send the exact amount; Paystack takes its own fee
   from the payer as today. Revisit after real-merchant willingness-to-pay tests.
4. **Link delivery = owner forwards it.** v1 returns a copyable pay-link the owner
   sends to the customer. Kashia messaging the customer directly (needs their
   contact + consent) is deferred.

### Build order (revised from these decisions)
Phase 1 (engine) → Phase 2 (webhook + IAM) → **test end-to-end with a
manually-created request BEFORE any UI** → Phase 4 (Mini App customer-card
surface) → Phase 3 (chat surface) → Phase 5 (polish).

---

## 11. Reused code (from the codebase map)

- `PaystackService.initialize_transaction` / `verify_transaction` /
  `verify_webhook_signature` — `src/services/paystack.py` (extend, don't fork).
- `paystack_webhook.lambda_handler` — signature/base64/idempotency/500-retry
  skeleton; branch after metadata extraction — `src/handlers/paystack_webhook.py`.
- `db.claim_web_submit` / `release_web_submit` (marker in TransactionsTable,
  `idem#…`, 24h TTL) — `src/services/database.py:370`.
- `db.save_transaction` (`:259`), `record_debt` (`:1278`), `settle_debt`
  (`:1332`) — the persistence primitives.
- `debt._apply_directed_payment` — the settle-then-log-a-sale pattern to mirror —
  `src/features/debt.py`.
- `resolve_client` / `send_text` — cross-platform owner notify —
  `src/services/messaging_client.py`, `src/services/telegram_client.py`.
- `PDFGenerator.generate_receipt(transaction_id)` — receipt-on-paid (Phase 5) —
  `src/services/pdf_generator.py`.
- template.yaml `PaystackWebhookFunction` route + IAM; `/app/api/*` route pattern
  on `MiniAppFunction`.
