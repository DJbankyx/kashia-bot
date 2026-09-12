# Multi-Item Basket at Sale Time (Option B) — Plan Doc

_Drafted: 2026-09-11. Status: FOR BUILD (owner already said MUST-BUILD)._

Sell several items to one customer in ONE tidy-box session, save them as a linked
group, and generate a single invoice/receipt. Telegram tidy-box only; WhatsApp
and the single-item flow stay exactly as they are.

---

## ⏸️ STATUS: DEFERRED (owner decision, 2026-09-11) — plan kept, NOT built
On review the owner chose to skip B for now. Rationale (agreed):
- The original DOCUMENT pain is ALREADY solved two other ways: the multi-item
  CHAT parse ("sold 2 shoes and 3 bags to Sandra 36k" → separate lines) and
  Bill-a-Customer (features/billdoc.py — pick several existing sales → ONE
  invoice/receipt). So combining items into one document is covered TODAY.
- B is a data-ENTRY convenience (basket at sale time), not a missing capability.
  For a car dealership, multi-item single-basket sales are rare.
- B is the highest-RISK item on the backlog (it's the one change that most
  directly touches the money/save path and adds state to the most-used tidy box)
  for the least incremental value right now.
This doc is kept intact and BUILD-READY. Revisit only if real users regularly
ring up multi-item baskets at sale time (e.g. selling parts/accessories
alongside vehicles). If revived, follow B1→B2→B3 with heavy verification and
resolve the 3 decisions below (esp. part-payment across a basket).

_Everything below is the original build plan, unchanged._

---

## What exists today (grounded in tg_fastentry.py)
- The tidy-box is a step machine on an `fx` session dict (state `TG_FASTENTRY`):
  item pick → (variant drill) → qty → price/total → payment → who → confirm →
  save. Steps live in `fx["step"]`; persisted via `_save_fx`.
- On confirm-Save, `_do_save` builds ONE `tx_data` (`_build_tx_data`) and calls
  `self.tx._save_transaction(phone, tx_data)` — the SHARED engine save that does
  stock, cost stamp, CRM totals, debt, AND the interactive post-sale landing-cost
  prompt, returning response cards.
- Payment (cash/transfer/credit/part), the boxed "Who?" step, and deposit/
  balance are collected ONCE and stored on `fx`.
- A stateless one-call save already exists: `TransactionHandler.record_transaction_web`
  (used by the Mini App) — records a full sale/purchase in ONE call composing
  db.save_transaction + _apply_stock_for_tx + _stamp_sale_cost +
  update_contact_totals + record_debt, with NO interactive follow-up and NO
  session writes. This is the ideal per-line saver for a basket.
- Bill-a-Customer (features/billdoc.py) already turns a set of tx_ids into ONE
  invoice/receipt via __GEN_INVOICE__/__GEN_RECEIPT__ (tx_ids list).

## The core design decision
Looping the interactive `_save_transaction` per line would fire N landing-cost
prompts and N cards — messy and stateful. Instead:

**A basket collects lines on `fx["basket"]`, takes ONE payment/who/confirm for
the whole basket, then saves each line via the STATELESS `record_transaction_web`
path (per-line stock/cost/CRM/debt intact), tagging every line with a shared
`group_id`.** After saving, offer ONE invoice/receipt for the group (reusing
billdoc's multi-doc generator).

This reuses proven money logic per line, avoids interactive-prompt-in-a-loop, and
keeps the single-item flow untouched (basket is opt-in).

---

## Flow (Telegram tidy-box, sale/purchase; NOT expense/service for v1)
```
item → (variant) → qty → price/total
     → [ ➕ Add another item ]  ── loops back to item pick, line pushed to basket
     → [ ✅ Done adding ]
     → payment (once) → who (once) → CONFIRM (lists all lines + total)
     → SAVE: loop lines → record_transaction_web(line + group_id)
     → receipt: "N items saved · ₦total" + [🧾 Invoice] [🧾 Receipt] (whole group)
                + Undo (reverses the whole group)
```
Single-item stays the default: the basket only forms if the user taps
"➕ Add another item". A one-line basket saves exactly like today.

## fx additions
- `fx["basket"]` = list of line dicts, each a frozen snapshot of the per-item
  fields (description, product_key, variant_label, quantity, unit_cost, amount,
  is_service, catalog_product…). A line is pushed when the user taps "Add
  another" (or "Done") at the price step — i.e. after each item is fully priced.
- Payment/vendor/deposit/balance stay top-level on `fx` (collected once for the
  whole basket).
- `group_id` = generated once at save; written into each line's
  `extra_details.group_id` (+ `basket_size`).

## New actions / buttons (__tgfx__ namespace)
- `additem`  — push the current priced line to `fx["basket"]`, reset item fields,
  go back to the item picker.
- `donebasket` — push the current line, then proceed to payment (once).
- The price step (and the "enter total" escape) gains an "➕ Add another item"
  button next to "Continue" (only on sale/purchase, Telegram).

## Save (the money-critical loop)
For each line in `fx["basket"]` (+ the final line):
  build a per-line `tx_data` (like `_build_tx_data` but per line) carrying the
  SHARED payment_method / vendor / has_credit / deposit split, plus
  `extra_details.group_id`; call `self.tx.record_transaction_web(phone, tx_data)`.
Guards:
  - **Part-payment across a basket:** deposit/balance are for the WHOLE basket.
    v1 decision: apply the deposit to the basket TOTAL — record the group's debt
    as (total − deposit) on ONE line (or a single debt entry keyed to the
    customer), NOT per line. Simplest correct approach: sum lines, record one
    debt for the balance. (Confirm in Decisions.)
  - **Stock double-deduct:** each line deducts its own qty via
    record_transaction_web's _apply_stock_for_tx — same as N separate sales.
    No shared state, so no double-deduct.
  - **Cost stamp:** each sale line stamps its own COGS (record_transaction_web
    already does). Landing-cost prompt is skipped (stateless path) — for cars in
    specific mode, the basket flow should let the user set each line's cost at
    the price step (carry unit_cost), so COGS is still captured.

## Invoice/receipt for the group
After save, the receipt card offers Invoice/Receipt that emit
__GEN_INVOICE__/__GEN_RECEIPT__ with the group's tx_ids (collected from the save
loop) — ONE document, each item its own line (Bug-3 descriptions apply).

## Undo (whole group)
Post-save Undo reverses EVERY line in the group. Since returns (build #4) already
reverse a sale cleanly, Undo can either (a) delete the just-saved group rows
(simplest, immediate) or (b) record returns for each. v1: a straight delete of
the group's rows + stock re-add, only available right after saving (before the
next action). (Confirm in Decisions.)

## Risk flags
- MONEY PATH: the save loop must reuse record_transaction_web verbatim per line —
  do NOT reimplement stock/cost/debt.
- Part-payment across multiple lines is the trickiest bit — keep it to ONE debt
  for the basket balance.
- Keep expenses/services OUT of v1 basket (they don't line up as multi-item
  goods sales); basket is sale/purchase only.
- Single-item + WhatsApp paths must be byte-for-byte unchanged.

## Build order
- **B1** — basket accumulation on fx + "➕ Add another / ✅ Done" at the price
  step; confirm card lists all lines + total. (No save change yet — dry-run the
  accumulation + confirm.)
- **B2** — save loop via record_transaction_web with a shared group_id; one
  payment/who/part applied to the basket; returns the saved tx_ids.
- **B3** — group invoice/receipt (emit markers with the group tx_ids) + whole-
  group Undo.
- Verify each with dry-runs; single-item + WhatsApp intact.

## Decisions needed (can proceed with the recommended defaults)
1. Part-payment on a basket → ONE debt for (total − deposit) keyed to the
   customer (recommended) vs pro-rated per line?
2. Undo → straight delete of the group rows (recommended, immediate) vs record
   returns per line?
3. Basket scope → sale + purchase only for v1, expenses/services excluded
   (recommended)?
