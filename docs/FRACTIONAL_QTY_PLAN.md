# Fractional Quantity & Stock — Engine-wide Plan

_2026-09-23. Make quantity and stock decimal-safe (e.g. 0.5 kg nylon, 2.5 L)
across the SHARED engine — chat, WhatsApp, and mini-app — once and properly.
Money (cost/price/amount) stays integer naira; only QUANTITY and STOCK change._

## Design decision
- **Quantity & stock = numbers that may be fractional.** Stored as-is; displayed
  trimmed ("2" not "2.0", "0.5" stays "0.5", "2.5" stays "2.5").
- **Money stays int naira.** Cost, price, amount, COGS totals, weighted-average
  unit cost — unchanged. (COGS *total* = unit_cost × qty may be fractional in the
  multiply, but is rounded to int naira at the end, as today.)
- **Never break WhatsApp or chat.** All display already has a "whole vs
  fractional" helper pattern in places (catalog.py stock-adjust); we standardise
  it and apply everywhere.
- **Backward compatible.** Existing int stock values keep working (an int IS a
  valid number); no migration needed.

## Truncation points found (the map)

### services/accounting.py  (COGS + inventory) — HIGH priority (wrong numbers)
- `_qty_of(tx)` (line ~54): regex `^\s*(\d+)` + `int()` → **0.5 → 0** → COGS = 0
  for a fractional sale. FIX: parse decimals, return float.
- `_value_product` / `_value_tree`: `_to_int(stock)` for inventory units/value.
  FIX: use a numeric coercion (float) for units; value = units × cost stays int.

### features/transactions.py — HIGH (feeds stock + COGS)
- `_parse_qty()` (line ~2704): regex `^(\d+)` + `int`, min 1. Used in 13+ places
  (stock apply, returns remaining qty, COGS totals, restamp). FIX: decimal-aware,
  return float; keep "≥ smallest sane" behaviour (don't force 1 when 0.5 given).
- `_apply_stock_for_tx`: `unit_cost = amt // qty` (integer div). FIX: use
  round(amt / qty) so unit cost isn't skewed by fractional qty.

### features/catalog.py — HIGH (stock storage) + MED (display)
- Storage/math (must be float-safe):
  - `update_stock`: non-variant `current = int(stock)` then `+ actual_qty`;
    variant `int(v)` sums; tree leaf `_as_int(node.stock)`. FIX: numeric, not int.
  - `set_stock_exact`: `target = max(0, int(target))`, `current = int(...)`.
    FIX: numeric.
  - `_as_int` used for stock (leaf/write-off/rollup). ADD `_as_num` (float) and
    use it for STOCK; keep `_as_int` for money.
  - roll-up sums, `set_stock_exact` delta.
- Display (show whole-as-whole): the many `int(prod.get("stock",0))` in
  `_format_*`, card/detail/list/picker builders. FIX: a `_fmt_qty()` helper that
  renders whole as int, fractional trimmed. These are cosmetic but numerous.
- Cost stays int everywhere (weighted-avg, landing_cost) — DO NOT change.

### handlers/miniapp.py — MED (entry + display)
- JS: `saveRecord` `parseInt(rec-qty)` + `Math.max(1,…)`; `saveSheet`/`bump`/
  leaf editor `parseInt` stock. FIX: `parseFloat`, `step="any"`, min 0 (not 1)
  where a fraction is valid.
- Server `_product_write`: `int(data.get("value"))` for set_stock/set_stock_delta/
  set_leaf_stock. FIX: numeric for STOCK actions; keep int for price/cost/reorder.
- Row shape `_row_from_product`: `int(p.get("stock"))`, `stock_value`. FIX: numeric
  stock; value stays int.
- Display: `Number(p.stock).toLocaleString()` already handles decimals fine.

## Helpers to add (single source of truth)
- Python: a small module-level `to_qty(v)` (→ float, safe) and `fmt_qty(v)`
  (→ str, trims trailing zeros) reused by catalog/transactions/accounting.
  Keep them tiny + pure; unit-test them.
- JS: quantities already display via `Number(...).toLocaleString()`; inputs
  switch to `parseFloat` + `step="any"`.

## Order of work (each verified before the next)
1. Helpers + `transactions._parse_qty` decimal-aware.  ← smallest blast radius
2. `accounting._qty_of` + inventory units float.        ← fixes COGS = 0 bug
3. `catalog` stock storage/math float-safe (+ `_as_num`).
4. `catalog` display `_fmt_qty` (whole-as-whole).
5. mini-app JS + `_product_write` stock actions.
6. End-to-end tests: parse/format; 0.5-qty sale → COGS correct; 0.5 purchase →
   stock 0.5; production uses 0.5 kg; whole numbers still render whole; WhatsApp
   card path unaffected (spot-check a render).

## Guardrails
- Money (cost/price/amount/COGS naira) stays int — do not convert to float.
- Never force qty up to 1 when a valid fraction < 1 is given (that was the old
  `_parse_qty` min-1 behaviour that hid the truncation).
- Compile + check_syntax + UTF-8/surrogate scan each step; small commits.

---

## SHIPPED — 2026-09-23 (commits `3a28a8b` engine, `708c4d9` mini-app)

Quantity + stock are now decimal-safe end-to-end; money stays integer naira.

- **utils/quantity.py** (new): `to_qty` (parse '0.5'/'2.5 kg', bool-guarded),
  `fmt_qty` (whole→'2', fractional trimmed→'0.5'/'2.5'), `qty_is_whole`.
- **transactions._parse_qty**: decimal-aware — int when whole (display clean),
  float when fractional; empty/0/unparseable → 1.
- **accounting**: `_qty_of` decimal-aware (FIX: a 0.5-unit sale used to compute
  COGS = 0; now correct). Added `_to_num`; COGS line totals wrapped in `_to_int`
  (round to naira); `resolve_sale_cost_now` unit = total/qty (not //);
  `_value_product`/`_value_tree` units fractional, value int.
- **catalog**: `_as_num` + `_fmt_qty`; stock storage/rollup/normalize float-safe
  (`update_stock`, `set_stock_exact`, `_vt_node_total`, `_vt_value`,
  `normalize_product`, chat stock-adjust). Cost/price stay `_as_int`.
- **mini-app**: qty/stock inputs `inputmode=decimal step=any`; `saveRecord` qty
  `parseFloat`; `bump` keeps a fractional base; `_product_write` stock actions
  parse numeric; `_row_from_product` keeps fractional stock (incl. tree rollup).

**Verified:** QTY_OK, ACCT_OK (0.5×40 COGS=20, 3×40=120, inv 0.5 units=₦20),
E2E_OK (purchase +0.5→0.5, +0.25→0.75, sale −0.5→0.25, set-exact 2.5; whole
stays int(3); low-stock 0.25≤1 True), MINI_OK 200 (UTF-8 clean). py_compile +
check_syntax pass on all 5 files.

**Known limits (by design, this phase):**
- MONEY stays whole naira. Sub-naira per-unit costs (e.g. electricity ₦/kWh)
  round to naira until the separate **money-precision (kobo/Decimal) phase** —
  owner needs it next (electricity recipe).
- CHAT/WhatsApp catalog display builders still `int(stock)` — cosmetic only
  ("0" shown for 0.5 in chat menus); no data loss, no crash. WhatsApp not a
  priority; mini-app displays fractional stock correctly.

**Deploy:** `cd ~/projects/kashia-bot && ./deploy.sh dev` (owner). No template
change → JS refresh after deploy + reopen from ☰ Menu button.
