# Costing Method — Decision Doc (Weighted-Average vs FIFO vs Specific-ID)

_Drafted: 2026-09-11. Status: FOR OWNER DECISION (no code changes yet)._

## The question the owner raised
> "Stock is bought at different times at different prices. When I record a
> purchase at the original price, how does the bot know the price of the stock
> that's still available? I think that's where FIFO comes in, right?"

Good instinct — but for THIS business, FIFO is probably the wrong tool. This doc
lays out the three options in plain English, what the code does today, and a
recommendation.

---

## The three methods, in plain English

**1. Weighted-average (what Kashia uses by default today)**
All purchases of the same item blend into ONE running average cost. Buy 10 bags
at ₦100 then 10 at ₦120 → the item's cost becomes ₦110 each. Every sale is
costed at that blended ₦110. Simple, stable, and correct for goods you can't
tell apart.

**2. FIFO (First-In, First-Out)**
Keep each purchase as its own "layer" (lot): 10 @ ₦100, then 10 @ ₦120. When you
sell, you consume the OLDEST layer first — the first 10 sold cost ₦100 each, the
next 10 cost ₦120. Needs a running ledger of every lot and how much of each is
left. Best for interchangeable stock where the *order* of consumption matters.

**3. Specific-identification**
Each unit is costed at ITS OWN real purchase price. You don't guess or blend —
you know THIS car cost ₦44M and THAT one cost ₦46M, so each sale uses its exact
cost. Best for unique, high-value, serialized items (vehicles, machinery, land).

---

## Why FIFO is the wrong fit for a car dealership

- **A car is not interchangeable.** FIFO exists because you *can't* tell two
  identical cement bags apart, so you assume the oldest goes first. But a
  specific 2022 Highlander has its own known cost — you don't need to *assume*
  anything. FIFO would approximate what you already know exactly.
- **Specific-ID is strictly more accurate here**, and Kashia already supports it
  (see below). FIFO would be a *downgrade* in accuracy for unique goods.
- **FIFO is a big, risky build.** It needs a brand-new consumable lot ledger
  (per purchase: qty, unit cost, date, remaining) plus rewrites of the purchase
  path, the sale COGS resolver, returns, and all inventory valuation. That's a
  lot of money-critical surface for goods that don't benefit from it.

FIFO earns its keep only for **fungible bulk stock bought in lots at volatile
prices** (e.g. a shop reselling cartons of drinks where the buy price swings and
you want strict cost layering). Kashia can carry that later as an *optional* mode
— but it shouldn't be the answer to the car question.

---

## What the engine already does (grounded in the code)

- **Weighted-average is maintained on every purchase** (`catalog.update_stock`,
  `cost_mode` = average/new/keep) at three levels: variant-tree leaf cost, flat
  variant cost, or base product `landing_cost`. This is the default and it's
  right for fungible goods.
- **Every sale STAMPS its own COGS** onto the transaction
  (`cost_used_total` / `cost_unit` / `cost_source`, via
  `transactions._stamp_sale_cost` → `accounting.resolve_sale_cost_now`). Reports
  read this stamp FIRST (`accounting.cogs_for_sale`), so a sale's cost is FROZEN
  at sale time and never re-blends after a later restock. **This is already
  textbook specific-identification at the transaction level.**
- **A "Specific" costing mode exists** (Settings → Costing Method; stored as
  `costing_mode` on the user, default "average"). The settings copy already says
  Specific is "best for unique high-value items (e.g. vehicles)."
- **Position/inventory valuation** values unsold stock at its own leaf/variant/
  average cost as an asset (`accounting.position` / `_value_product` /
  `_value_tree`).

### The honest gap (what's approximate today)
- `costing_mode = "specific"` today mostly changes a **label**, not the math. It
  does NOT keep separate per-unit costs by itself.
- Real per-unit accuracy comes from ONE of:
  1. the user **typing the exact landing cost** on that sale (wins outright,
     stamped verbatim), OR
  2. the item being a **one-unit variant-tree leaf** that carries its own cost.
- If **two identical units share one leaf/product** (two white 1992 Siennas
  bought at different prices) and the user doesn't type the exact cost, the leaf
  holds a **blended average** — so that sale is costed at the average, not that
  specific car's price. There is no VIN/serial-level cost store.
- **Returns** (`transactions.record_return`) currently reverse COGS by copying
  the sale's stamped cost (good) but re-add stock into a *blended* average — in a
  true per-unit/lot world a return should reinstate the unit at its stamped cost.

---

## Recommendation (ranked)

**A. KEEP weighted-average as the default.** It's correct for the fungible/bulk
goods the app also serves (bottles, bags, litres, the whole unit-conversion
machinery) and it's the already-locked accounting decision. No change.

**B. LEAN ON specific-identification for cars — close the small collection gap.**
The method is already built; the fix is UX, not a rewrite. Smallest changes that
make car COGS exact per unit, in order of value:
  1. **On the sale of a high-value / serialized item, make capturing the exact
     cost the natural path** (prompt "what did THIS unit cost you?" with
     accept-the-known-cost or type-exact — the accept-or-manual principle). The
     stamp then freezes the real per-car cost.
  2. **Encourage one car = one unit** (qty 1, or a leaf per specific vehicle) so
     the leaf cost IS that car's real cost.
  3. **Make `costing_mode = "specific"` actually pin per-unit cost** where we can
     (prefer the typed/leaf cost and never blend it), so the mode does what its
     label promises — not just relabel.
  4. **Fix the returns interaction** so a returned unique unit reinstates its
     stamped cost rather than folding into a blended average.

**C. TREAT FIFO as optional and LAST.** Only build it if the owner later carries
genuinely fungible bulk stock where strict cost layering matters. It would be a
new opt-in `costing_mode = "fifo"` backed by a real lot ledger — a separate,
planned build with its own doc. Not needed for the car problem.

---

## If we ever DO build FIFO (scope sketch, not now)
New per-purchase **lot ledger** (per leaf/variant/product): `[{date, qty,
remaining, unit_cost}]`. Touch points: `catalog.update_stock` (push a lot on
purchase), `accounting.resolve_sale_cost_now` (consume oldest lots, sum COGS —
the stamp still freezes the result so reports barely change),
`transactions.record_return` (reinstate/rebuild lots), and
`accounting.position/_value_product/_value_tree` (value = sum of remaining lots).
Kept behind an opt-in mode so weighted-average and specific-ID users are
untouched. Backward-compatible: existing products get one opening lot from their
current stock × average cost.

---

## Decision needed from the owner
1. Confirm: **weighted-average stays default, specific-ID is the car answer,
   FIFO is parked** (recommended). ✅ / ✍️ change?
2. If yes to B, approve the small specific-ID improvements (prompt exact cost on
   high-value sales; make "specific" mode truly pin per-unit cost; fix returns).
3. Park FIFO unless/until fungible bulk stock makes it worthwhile.
