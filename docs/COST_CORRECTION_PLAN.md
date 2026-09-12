# Cost Correction Plan — fixing a wrong cost/price after the fact

_Created 2026-09-12. For approval before building. Money-adjacent (touches COGS
and therefore profit), so built safest-first, verified against the existing
cost-stamp path, WhatsApp untouched (Telegram-gated UI; shared-engine
correctness fixes benefit both)._

Answers the owner's live-test question:
> "What if the last cost/selling price I entered was a mistake? And the ORIGINAL
> intended question: what if there was an error in the cost I entered in the
> catalog and a transaction has ALREADY used those details — what happens?"

---

## The problem, precisely

Cost lives in TWO places and they are deliberately decoupled:

1. **Catalog cost** — the product's current `landing_cost` (weighted-average) or
   a variant-tree leaf cost. Used to VALUE unsold stock and to resolve the COGS
   of a NEW sale.
2. **Stamped COGS on a past sale** — `cost_used_total` / `cost_unit` /
   `cost_source`, written ONCE at save time by `transactions._stamp_sale_cost`
   → `accounting.resolve_sale_cost_now`. This is authoritative and intentionally
   NEVER re-blends when the catalog cost later changes (so a restock can't
   silently rewrite historical profit). Reports + returns read this stamp first
   (`accounting.py` reads `cost_used_total` before anything else).

**Consequence today:** if the owner typed the wrong cost, the correction paths
are incomplete:
- `catalog.set_cost_direct` fixes the CATALOG cost → only affects FUTURE sales.
- The saved-tx editor (`transactions.handle_edit`, `field=="landing_cost"`)
  writes ONLY `landing_cost` on the tx — it does NOT rewrite the authoritative
  `cost_used_total` / `cost_unit` / `cost_source`. So editing it changes nothing
  the P&L actually reads. **This is the real bug.**
- There is NO path that says "this past sale's COGS was wrong — restamp it."

So a wrong cost already used by a sale is effectively stuck: profit stays wrong
until the owner deletes and re-records. That's the gap this plan closes.

---

## Design principles (locked)

1. **Never silently rewrite history.** A correction is an explicit, owner-driven
   action with a confirm — same discipline as the "never re-blend after restock"
   rule. We only restamp when the user asks.
2. **The stamp stays authoritative.** We fix by RE-STAMPING the sale's
   `cost_used_total`/`cost_unit`/`cost_source` (and leaving a marker that it was
   corrected), not by making reports re-resolve live.
3. **Offer BOTH, never force one** (owner's core principle): when a cost is
   corrected, let the user choose whether to (a) fix just the catalog going
   forward, (b) also restamp specific past sale(s), or (c) both.
4. **One shared engine.** Correction logic is a new `transactions` method reused
   by chat + (later) Mini App; no forked math.
5. **Auditable.** Every correction stamps `cost_corrected_at` + keeps the prior
   value in `cost_corrected_from`, so an audit can see what changed.

---

## Two entry points (both land in the same engine method)

### A. "This SALE's cost was wrong" (per-transaction)
From the existing saved-tx editor. Fix `handle_edit` so editing a sale's cost
ACTUALLY restamps COGS, not just `landing_cost`:
- New engine method `transactions.restamp_sale_cost(phone, tx_id, new_unit_cost)`:
  loads the sale, computes `total = new_unit_cost * qty`, writes
  `cost_used_total`, `cost_unit`, `cost_source="corrected"`,
  `cost_corrected_at`, `cost_corrected_from` (prior total). Also updates the
  visible `landing_cost` for consistency. Pro-rating aware (uses `_qty_of`).
- `handle_edit` `field=="landing_cost"` (sale) → call `restamp_sale_cost` instead
  of the bare `update_transaction({"landing_cost": ...})`.
- Guard: if the tx already has RETURNS against it, warn (returns copied the old
  stamped cost). v1: block the restamp with a clear message ("this sale has a
  recorded return; correct by reversing the return first") — avoids desyncing
  R1's copied cost. (Full return-aware restamp = later.)

### B. "This CATALOG cost was wrong" (product-level, with optional backfill)
From the catalog product card `Set Cost`. After `set_cost_direct` succeeds AND
the product has past sales stamped with the OLD cost, offer:
- `[🔧 Fix catalog only]` (current behaviour — future sales only)
- `[↩️ Also fix past sales]` → restamps every past sale of this product/leaf
  that used the old cost, via `restamp_sale_cost` in a loop (bounded; see limits).
- `[✖️ Cancel]`
New helper `transactions.get_stamped_sales_for_product(phone, product_key,
variant=None)` — finds sales whose `catalog_product`/`variant` match and that
carry a stamp, so we know how many would change and can preview
("This will recompute profit on N past sales.").

---

## Sequencing (each verified before the next)

- **C1 [engine]** `restamp_sale_cost(phone, tx_id, new_unit_cost)` — pure,
  reused by both entry points. Writes the 3 COGS fields + audit markers +
  `landing_cost`. Return-guard. Dry-run: stamp changes, prior kept, P&L shifts.
- **C2 [chat A]** wire the saved-tx editor's sale-cost edit to C1 (the actual
  bug fix). Verify a corrected sale's profit updates in the dashboard.
- **C3 [chat B]** catalog `Set Cost` → post-set "also fix past sales?" fork +
  `get_stamped_sales_for_product` preview + bounded backfill loop.
- **C4 [verify]** reconciliation: correct a cost, assert `period_pnl` COGS moves
  by exactly the delta; returns-present sale is blocked with the right message;
  WhatsApp editor path unchanged (correctness fix helps it too); compile.
- **C5 [Mini App, optional/later]** surface the same correction in the web edit
  sheet (the deferred "cost-choice popup" note in the roadmap is adjacent).

---

## Limits / guardrails
- **Backfill is bounded** — cap the past-sales restamp (e.g. most recent 200) and
  state it; a giant history correction becomes an admin/backfill-script job
  (reuse the monthly-reset Lambda shape), not an inline loop.
- **Only the owner's own data** (all reads/writes keyed by phone/tg id).
- **Never touch a returned sale's cost inline** (v1 blocks; R1's copied stamp).
- **Audit trail** via `cost_corrected_at` / `cost_corrected_from`.
- **Weighted-average default preserved**; correction is manual + explicit.

## Out of scope (v1)
- Auto-detecting "this cost looks wrong" (no anomaly guess on cost).
- Correcting a cost that then cascades into already-recorded RETURNS (blocked).
- Bulk correction UI beyond the single-product backfill (script instead).
