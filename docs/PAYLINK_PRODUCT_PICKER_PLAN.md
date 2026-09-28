# Pay-link Product Picker + Per-item Allocation — PLAN (not yet built)

_Drafted 2026-09-27. Owner surfaced this during payment-collection testing._

## Problem / motivation
Today's pay-link (Mini App customer card → "💳 Get pay-link") collects a **free-text
amount + free-text description**. It has no product picker, and the webhook cannot
attribute a payment to specific products. When a customer owes for **several sales /
several products** and pays **part** of the total, Kashia just reduces their single
`debt_owed_to_me` balance — it can't record "₦2,000 toward the rice, ₦1,000 toward
the oil."

The owner also noted the chat bot's document builder (`features/tg_invoice.py`) DOES
let you add/edit/remove line items — but the **Mini App has no equivalent**. So this
plan is Mini-App-first (the chat side already has a line-item document flow).

This is deliberately its own feature: the roadmap has **multi-item basket DEFERRED**
and debt is stored as **one balance per contact**, not per-product or per-invoice.
Adding a real picker + allocation touches the money-critical path, so it must not be
bolted onto the existing simple pay-link as a rushed patch.

## Why it's non-trivial (the honest constraints)
1. **Debt is a single number per contact.** There is no per-invoice or per-product
   receivable ledger. "Pay ₦1k toward product A, ₦2k toward product B" has nowhere
   to land today without a data-model change.
2. **No per-transaction basket.** A Kashia sale is ONE line item. Selling 3 products
   in one go is 3 transactions, not one basket. A picker that lets you bill "3
   products on one link" implies either 3 sales on payment or a new basket concept.
3. **The webhook is money-critical and idempotent.** Any allocation logic runs on
   payment confirmation — it must be safe under Paystack's at-least-once retries and
   must never corrupt the books if allocation fails.

## Scope options (pick before building)
### Option A — Picker for DESCRIPTION only (small, safe)
- Mini App pay-link sheet gains a **product dropdown** (from the catalog) + a
  "something else" free-text fallback. Picking a product prefills the description
  and (optionally) the amount from its selling price × qty.
- Payment still records ONE sale against the customer and settles their single
  balance, exactly as today. No allocation, no data-model change.
- Delivers the "dropdown of my products" the owner asked for, with near-zero risk.
- Does NOT solve per-item allocation of a part-payment.

### Option B — Multi-line bill + proportional allocation (medium)
- Pay-link can carry **multiple line items** (product + qty + price), summed to the
  charged amount (mirror the chat `tg_invoice` line editor in the Mini App).
- On payment, if the customer pays the FULL amount → record each line as its own
  paid sale. If they pay PART → allocate the payment **proportionally** across the
  lines (or FIFO oldest-first) and record partial sales / settlements accordingly.
- Requires storing the line items on the PaymentRequest and an allocation routine in
  the webhook. Still no per-invoice ledger, but "which products this payment covered"
  becomes answerable.

### Option C — Per-invoice receivable ledger (large, deferred)
- Introduce invoice-level receivables so a customer's debt is a set of open invoices,
  each with its own balance; a payment is allocated to specific invoices the owner
  (or customer) chooses. This is the "real" accounting answer and the biggest change
  (data model + every debt surface + reports). Park unless demanded.

## Recommendation
Ship **Option A first** (product dropdown on the Mini App pay-link sheet) — it
answers the owner's immediate "where's my product list?" with almost no risk and no
money-path change. Treat **Option B** as the next planned step once real merchants
actually bill multi-product links, and keep **Option C** parked behind demand.

## Touch points (when built)
- `src/handlers/miniapp.py` — pay-link sheet UI (product `<select>` + free-text
  fallback); `_payment_request_write` accepts a chosen product / line items.
- `src/services/database.py` — `create_payment_request` already stores amount +
  description; Option B adds a `line_items` list on the PaymentRequest row.
- `src/handlers/paystack_webhook.py` `_handle_collection` — Option B adds the
  allocation routine (proportional / FIFO), still idempotent + release-on-failure.
- `src/features/catalog.py` — product list source for the dropdown (reuse existing
  catalog read; do NOT fork).
- Reuse `features/tg_invoice.py` line-item concepts for Option B rather than
  reinventing (chat already has add/edit/remove lines).

## Out of scope for this plan
- Kashia messaging the customer directly (needs contact + consent) — separate.
- Per-invoice ledger (Option C) unless explicitly prioritized.

## Status
NOT STARTED — plan only. Immediate honest-amount + part-payment-receipt fixes shipped
separately (commit c8e436c); see docs/PAYMENT_COLLECTION_PLAN.md §12–13.
