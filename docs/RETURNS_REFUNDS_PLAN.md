# Returns / Refunds — Plan

_Created 2026-09-11. Build #4 on the active plan. Record a return/refund that
correctly reverses stock, cash/debt, revenue AND COGS — as an auditable
reversing event, not a delete._

## Why this matters for Kashia
You sell cars; a customer returns one, or you send a car back to a supplier.
Today there's NO way to record that — the sale/purchase stays booked, stock is
wrong, and profit is overstated. This adds a proper, accounting-correct reversal.

## Core design decisions (from a full code investigation)
1. **New transaction TYPES, not negative amounts.** Add `sale_return` and
   `purchase_return`, each stored with a POSITIVE amount (the whole codebase
   assumes positive amounts). A negative-amount "sale" would corrupt the COGS
   loop, sales_count, and top-products. Distinct types = explicit, safe control.
2. **A return is a reversing EVENT, not an undo/delete.** It's a new dated
   transaction that references the original — keeps a full audit trail. (The old
   `delete_last_transaction` is dead code and restores nothing — not used.)
3. **Reverse COGS from the ORIGINAL sale's STAMPED cost.** The sale we're
   reversing already stamped `cost_used_total`/`cost_unit`/`cost_source`/`variant`
   (the cost-stamp feature). The return COPIES those, so COGS backs out at the
   exact cost the sale used — critical for variant-tree leaves / unique cars
   where the weighted-average may have since drifted.
4. **Stock add-back must NOT change the weighted-average cost.** Restoring sold
   goods to the shelf restores QUANTITY only. Call `update_stock` with
   `cost_mode="keep"` (unit_cost=0) so the average is untouched.
5. **Cash vs debt reversal depends on how the original was paid:**
   - Original was PAID → refund is real money: sale_return = cash OUT;
     purchase_return = cash IN.
   - Original was CREDIT/part (open receivable/payable) → NO cash moves; instead
     cancel the ledger: `settle_debt(..., 'owed_to_me')` (sale) /
     `settle_debt(..., 'i_owe')` (purchase). Record 0 cash.

## How each accounting surface reverses (services/accounting.py)
- **P&L (`period_pnl`):** `revenue -= Σ sale_return.amount`; `cogs -= Σ
  sale_return.cost_used_total`. Both back out together → gross profit stays
  correct. `purchase_return` has NO direct P&L line (purchases aren't in P&L;
  only COGS-of-sold is) — it's purely a stock + cash/payable event.
- **Cash flow (`period_cashflow`):** extend the type checks — sale_return adds to
  cash_out (refund paid); purchase_return adds to cash_in (refund received);
  credit-cancel returns record 0 cash.
- **Position:** no code change — it values the catalog + debt ledger, both fixed
  by the stock + debt reversals.
- **Product margins / top products:** net returns per-product (later polish;
  optional for v1).
- Keep return `category` = "Sales Return"/"Purchase Return" so `_is_debt_settlement`
  never mis-catches them.

## Stock reversal (catalog.update_stock — already supports both directions)
- **sale_return:** `update_stock(+qty, cost_mode="keep", variant=<leaf path>)` →
  stock back IN to the exact leaf, average cost untouched.
- **purchase_return:** `update_stock(-qty, cost_mode="keep", variant=...)` → stock
  OUT, average untouched (a quantity-only outflow doesn't recompute the average).

## Linking to the original sale (accurate COGS + leaf)
Building blocks exist: `db.get_transactions(phone, limit)` (recent, newest
first), `db.get_transaction(phone, tx_id)` (one by id). No per-contact/product
query — filter in memory by type=="sale". Flow: pick "Record a Return" → list
recent SALES (tappable) → on tap, load that tx, copy amount/quantity/item_name/
variant + stamped cost into the return. **Partial returns:** scale qty and
pro-rate `cost_used_total`.

## Entry points
- **Primary (cleanest):** a post-sale receipt button `return_from_<tx_id>` — the
  original tx id is in hand, so COGS reversal is exact.
- **Secondary:** a menu row (e.g. under Catalog & Stock or the main record menu)
  "↩️ Record a Return" → lists recent sales/purchases to reverse.
- New `return_*` button namespace routed to a small ReturnsHandler
  (`handle_button`), mirroring contacts/reports/production handlers. Telegram-
  gated tap-first; WhatsApp can get a simpler text path later.

## Staged build order (each verified before the next)
- **R1 — Engine core (no UI):** the reversing-save logic. A
  `transactions.record_return(phone, original_tx_id, qty, refund_mode)` that:
  loads the original, builds a `sale_return`/`purchase_return` tx (positive
  amount, copied stamped cost + leaf + original_tx_id), saves it, reverses stock
  (`cost_mode="keep"`), and reverses cash-vs-debt per the original's payment.
  Pure + unit-testable with mocks. Ship.
- **R2 — Accounting recognises returns:** `period_pnl` nets revenue+COGS;
  `period_cashflow` nets cash. Verify P&L/cash/position all reconcile after a
  return. Ship.
- **R3 — UI:** post-sale `return_from_<tx_id>` button + a "Record a Return" menu
  entry that lists recent sales (and purchases) → confirm qty (full/partial) →
  refund mode (cash refunded / cancel debt) → record. Telegram tap-first. Ship.
- **R4 (optional later):** returns in the Mini App; per-product margin netting;
  WhatsApp text path.

## Money-safety checklist
- [ ] Return references a real original tx owned by the same user.
- [ ] Can't return more than was sold/bought (qty clamp vs original quantity;
      guard against multiple returns exceeding the original).
- [ ] COGS reversed from the STAMPED cost (copied), never re-resolved.
- [ ] Stock add-back uses cost_mode="keep" (average cost never altered).
- [ ] Cash XOR debt reversed (never both) based on original payment.
- [ ] Return is a new row (audit trail); original tx is left intact.
- [ ] Idempotency for the web/rapid-tap case (reuse the submit_id pattern later).

## Open decisions (confirm before R1)
1. **v1 scope = sale-returns + purchase-returns, full AND partial?** (Recommended
   yes — partial is common; it just scales qty + pro-rates cost.)
2. **Refund mode prompt:** when the original was PAID, ask "Refunded cash?" vs
   "Store credit/keep as balance"? Or assume cash refund by default? (Recommended:
   default cash-refund; offer "cancel debt" automatically when the original was
   on credit. Keep it simple for v1.)
3. **Guard against over-returning:** track returned qty against the original, or
   trust the user for v1? (Recommended: soft check — warn if return qty > original
   remaining, but allow.)
4. **Telegram-first, WhatsApp later?** (Recommended yes, matches every other
   recent build.)
