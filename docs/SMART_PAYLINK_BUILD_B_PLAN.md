# Build B — Smart pay-link (tied to a balance) · plan + log

_Started 2026-10-01_

## Problem (owner, device testing)
Today a pay-link is a flat amount with only a customer NAME attached. Consequences:
- The owner can mint a link for ANY amount for a customer who already has a link
  out — nothing stops spamming duplicate links.
- The link doesn't know WHAT it settles (which debt / open items).
- If the customer part-pays the issued link IN CASH, the link auto-cancels (good),
  but the bot does NOT recognise the remaining balance or offer a link for just
  the balance.

## Goal
A pay-link that:
1. Is created FROM a specific owed balance / open items (knows the exact amount
   + what it settles).
2. Recomputes the remaining balance after ANY payment (cash OR online), reducing
   the specific open-item rows, not just the contact lump.
3. Offers a fresh link for the REMAINING balance.
4. Blocks / warns on a duplicate live link for the same balance.

## Current machinery (confirmed)
- **Contact lump debt** (authoritative, reports read it): `contact.debt_owed_to_me`
  mutated by `db.record_debt` / `db.settle_debt`.
- **Open items** = transaction rows with `balance_owed > 0` (pm credit/deposit).
  No separate table. `features/open_items.py`:
  `list_open_items(phone, name, direction, site=)`, `open_total`,
  `settle_open_items(phone, name, amount, direction, picked_ids=, site=)` — reduces
  each row's `balance_owed` then reconciles the lump via `settle_debt` once.
- **Payment request / pay-link registry**: a row in the Transactions table under
  sort key `payreq#<uuid>`, `type='payment_request'` (excluded from P&L). Shape:
  amount, description, paystack_ref, status (pending/paid/cancelled), customer_name,
  vendor, `debt_link={"contact":...}` (optional), payment_url, created_at.
  Guards: `has_pending_link(phone,name)`, `cancel_pending_requests_for(phone,name)`,
  `list_payment_requests(phone,status=)`, `mark_payment_request_paid`.
- **Webhook** `_handle_collection`: idempotent on `paystack#<ref>`; honest amount
  (stored request amount, not fee-inflated gross); sale-vs-repayment by looking up
  `contact.debt_owed_to_me` and calling `db.settle_debt` (LUMP ONLY — does NOT
  reduce per-row balance_owed, does NOT cancel sibling links).
- **Paystack**: `initialize_collection(owner, amount, desc, payreq_id, customer_name=)`
  ties the charge to `payreq_id` via metadata. NO Paystack change needed.

## Design (additive, reuses the engine)

### Data: extend `debt_link` on the PaymentRequest row
`debt_link = {"contact": <name>, "open_item_ids": [...], "balance_at_create": <naira>}`
- `open_item_ids` = the specific open-item tx ids this link is meant to settle
  (may be empty → settle oldest-first against the contact).
- `balance_at_create` = owed total when minted (for display / staleness).

### 1. Mint from a balance (mini-app)
- `db.create_payment_request(...)` gains `open_item_ids=None, balance_at_create=None`
  and stores them inside `debt_link`.
- `_payment_request_write` accepts body `{amount, description?, customer?,
  open_item_ids?, debt_contact?, force?}` and forwards them.
- Contact card "💳 Get pay-link" (`cdGetPayLink`) prefills the amount from the
  contact's `owes_me` and passes `debt_contact=name` so the link is tied to that
  customer's balance. (Open-item-id selection is a later refinement; v1 ties to
  the contact + current owed total, which is enough to block dupes + recompute.)

### 2. Duplicate block
- New `db.pending_link_for_contact(phone, name)` → the pending payreq row (or None).
- `_payment_request_write`: if a pending link already exists for this contact and
  `force` is not set → return **409** `{error, existing:{amount, reference,
  created_at}}`. The UI shows a two-tap "a link is already out for ₦X — create
  another anyway?" confirm (mirrors the existing needs_confirm pattern).

### 3. Recompute after online payment (webhook)
- `_handle_collection`: when the paid request has `debt_link.contact`, route the
  receivable settlement through `OpenItems.settle_open_items(owner, contact,
  settled, direction="owed_to_me", picked_ids=debt_link.open_item_ids or None)`
  INSTEAD of a bare `db.settle_debt`. settle_open_items already reduces per-row
  balance_owed AND reconciles the lump once — so numbers stay consistent and the
  repayment is tracked at the item level.
- After settling, `db.cancel_pending_requests_for(owner, contact)` to kill sibling
  pending links (parity with manual payment, which already does this via
  `_settle_open_write`). The just-paid link is marked paid (not cancelled).
- Keep the existing honest-amount + sale-portion logic; only the receivable
  settlement path changes from lump→open-items.

### 4. Offer a link for the remaining balance
- After a manual settle (`_settle_open_write`) or a webhook settle, the remaining
  owed is recomputed from the lump. The contact card already re-reads
  `owes_me`; `cdGetPayLink` prefilling from the live owed total means the "next"
  link is automatically for the remaining balance. Add a one-line hint on the
  pay-link sheet: "Owes ₦X now — this link is for that balance." with the amount
  prefilled + editable.

## Chat parity
Pay-link minting is mini-app only today (no chat command mints one). Chat already:
- warns via `has_pending_link` on the debt board,
- settles via `_apply_directed_payment` → `OpenItems.settle_open_items` (site path).
Build B doesn't break chat. Routing the WEBHOOK through open-items also benefits
chat (per-row balances stay correct when an online payment lands). A chat
"send pay-link" command is a later addition (not in this build).

## Verify
py_compile (miniapp, database, paystack_webhook, open_items); check_syntax; built
`_PAGE_HTML.encode("utf-8")` + 0 surrogate scan; esprima parse; emoji in served JS
DOUBLE-backslash. New route? No — reuses existing /app/api/payment-request (now
accepts the extra fields) + the webhook. No template.yaml change.

## Status — SHIPPED (verify all green: py_compile x4 + check_syntax + encode/0-surrogate + esprima)
- [x] db.create_payment_request stores open_item_ids + balance_at_create in debt_link
- [x] db.pending_link_for_contact(phone, name)
- [x] _payment_request_write: accept open_item_ids/debt_contact/force + 409 dup block
- [x] webhook _handle_collection: settle via OpenItems.settle_open_items(picked_ids)
      (falls back to lump settle on error) + cancel sibling pending links
- [x] contact card: prefill amount from owed + pass debt_contact + "owes ₦X now"
      hint + duplicate-confirm (409 → window.confirm → force retry)
- [x] verify + commit + push

NO template.yaml change (reuses /app/api/payment-request + the existing webhook).

### What this build does NOT do yet (future refinement)
- Per-open-item PICKING in the UI (choose which items a link covers). v1 ties the
  link to the contact + current owed total, which is enough to block dupes +
  recompute per-row balances oldest-first. open_item_ids plumbing is in place for
  when a picker is added.
- A CHAT command to mint a pay-link (still mini-app only). Chat already warns on a
  pending link + records manual repayments through the same open-items engine.
