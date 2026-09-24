# Manufacturing Fixes — mini-app testing round (2026-09-25)

_Owner testing of the satchet-water manufacturing flow ("Banky Water") surfaced
one serious accounting bug plus a batch of mini-app UX gaps. All fixed below.
Agent committed + pushed; **owner runs `./deploy.sh dev`** (a real API-Gateway
route was added, so a deploy IS required this round)._

Design lens: built generically for ALL industries (trading / manufacturing /
services / hybrid) and any unit model — not just this one business.

---

## The ₦65,000,000 "production cost" (COGS) bug — root cause

Dashboard showed NET PROFIT −₦64,970,000, PRODUCTION COST ₦65,000,000 for
₦80,000 revenue. Traced via live DynamoDB (kashia-users-dev / -transactions-dev,
eu-west-1):

- The finished product's per-unit cost (`landing_cost` ₦325) was **correct**.
- The bad SALE stored `quantity = 200000` (should have been 200 bags). COGS =
  `cost_unit 325 × qty 200000 = ₦65,000,000`. The quantity was the only wrong
  value.
- **Why 200000:** changing a product's `base_unit` did a BARE swap
  (set `primary_unit` + `base_unit`) WITHOUT rebuilding `unit_defs`. `unit_defs`
  stores each unit's factor-TO-BASE, so old factors silently became wrong vs the
  new base. A product taught "1 bag = 20 pieces" (base=piece → `{"bag":20}`)
  that was later re-based to `bag` kept `{"bag":20}` — now read as "1 bag = 20
  bags", a ×20 blow-up on every quantity and cost.

### Fix (commit a104445)
- **`utils/units.rebase_product(product, new_base)`** — rebuilds `unit_defs`
  from the base-independent raw `unit_edges` against the new base, drops any
  self-edge (unit == base). Never raises.
- `miniapp` `set_unit` action + `database.set_primary_unit()` now rebase instead
  of a bare swap.
- `transactions._stamp_sale_cost()` adds a NON-BLOCKING `cost_sanity` flag when
  stamped COGS exceeds the sale amount > 10× (catches a bad qty/unit entry;
  never blocks a genuine clearance/loss sale).

### Live data corrected (owner's account, tg:1072412276)
- Sale `1790267468058_cf546f1c`: qty 200000 → 200, cost_used_total 65,000,000 →
  65,000 (revenue ₦80,000, COGS ₦65,000, profit ₦15,000).
- Product `35cl_satchet_water`: stock → 200 bags (400 produced − 200 sold);
  `unit_defs` `{bag:20, truck:8000}` → `{piece:0.05, truck:400}`
  (1 piece = 0.05 bag i.e. 1 bag = 20 pieces; 1 truck = 400 bags).

**Unit model (owner):** produce in pieces, sell in bags; 1 bag = 20 pieces.
Option A chosen — `base_unit = bag`, cost ₦325/bag, piece = ÷20; both bag and
piece metrics derivable.

---

## The rest of the round

| # | Fix | Commit |
|---|-----|--------|
| 3 | Record-transaction picker filters by tx type (sale → sellable; purchase → raw/supply; produce → finished-with-recipe) | 366f455 |
| 8 | Sale/purchase **unit selector** — record in bags OR pieces; entered qty converted to base units before save (stock + COGS stay in base) | 7de9485 |
| 9 | Name the stock unit on purchase/sale when there's no unit picker (buy Nylon in kg, consistent with the recipe) | 3a44b12 |
| 7 | Row cost reads `avg_cost` then `landing_cost` (matches accounting); Power ₦0.5 was already stored — display hardened | b0b2a70 |
| 10 | All bottom-sheets scrollable (`max-height:90vh; overflow-y:auto` on base `.sheet`) — no more scroll trap | b0b2a70 |
| 4 | Expense quantity + free-text unit ("50 litres"); **Spend by expense** grouped drill-down in Records | 7e4419b |
| 6 | Export: PDF paywall message surfaced (was raw "Error 403"); Excel/PDF chat-delivery failure now returns a **download link** fallback | 7456ae1 |
| 5a | **Production Records** tab (mfg/hybrid) — batch #, good/waste, cost/unit, materials used | 13c6a68 |
| 5b | **Record production in the web** — `produce_web()` (stateless engine mirror) + `POST /app/api/produce` + a "🏭 Produce" record type | 228f44c |
| 11 | **Cash at hand** dashboard card — all-time running cash balance via `accounting.cash_position()` (combined cash+bank pool, opening 0) | 78e2c97 |

### Notes / by-design
- **PDF export 403** is the tier gate: PDF is a Basic/Pro feature; the owner's
  tier is Free. Excel export is free. Now shows the friendly paywall message.
- **Excel "couldn't deliver"** was Telegram timing out fetching the S3 URL for
  xlsx; the file is on S3, so a Download link is now offered.
- **Cash at hand** can be negative when raw-material spend precedes finished-good
  sales — an optional `opening_cash` figure (default 0) can offset that later.

## Deploy
```
cd ~/projects/kashia-bot
./deploy.sh dev          # REQUIRED — adds the /app/api/produce route + all code
```
`./set_telegram_commands.sh` NOT needed (the "/" command menu didn't change).

## Verification discipline (every mini-app edit)
- `py_compile` + `check_syntax.py`
- build `_PAGE_HTML` and assert `.encode("utf-8")` succeeds + 0 lone surrogates
  (0xD800–0xDFFF) — emoji in served JS MUST use double-backslash escapes
  (`"\\ud83d\\udca1"`), single-escape becomes a lone surrogate → 500.
- `esprima.parseScript()` on each `<script>` block.
- `python3 -m utils.units` → ALL_UNITS_OK.

---

## Follow-up (2026-09-25): cash adjustments + true net worth

Owner point: cash-at-hand alone misleads — the owner moves cash that isn't a
sale/purchase/expense, and cash is only one part of what the business owns.

- **`cash_adjustment` transaction type** (`transactions.record_cash_adjustment`):
  a manual cash move — Owner withdrawal (out), Capital injection (in), Bank↔cash
  transfer (either), Correction (either). Counted by `accounting.cash_position`
  (in → +, out → −). EXCLUDED from the P&L (`period_pnl` reads only sale/
  sale_return/expense) and from Records-by-type. Verified: sale 80k + injection
  20k in / expense 10k + withdrawal 50k out → cash at hand 40k, while P&L stays
  revenue 80k / opex 10k.
- **True net worth**: `accounting.position()` now folds cash in →
  `net_worth = cash + inventory + receivables − payables` (`net_worth_proxy`
  kept for back-compat). Mini App `net_position` uses it; the dashboard card is
  relabelled **"Net worth"** with a breakdown hint. Live Banky Water: cash
  −185,000 + inventory 394,000 − payables 100,000 = **net worth 109,000** (the
  negative cash is offset by the inventory it bought — the honest picture).
- **UI**: a "± Adjust cash" button on the Cash-at-hand card → a sheet (reason
  chips + direction + amount) → `POST /app/api/cash-adjust`. Withdrawal defaults
  to out, injection to in; transfer/correction let the owner pick.

Commits: `17f3d1b` (engine + net worth), `0f71b6a` (UI + `/app/api/cash-adjust`
route → **deploy required**).

### Editable opening cash balance (2026-09-25)

Cash at hand started at "since you began recording" (opening 0). Now the owner
can set the actual starting cash: the cash-adjust sheet gains a **"🏁 Set
opening balance"** reason that switches to absolute-value mode (relabelled
"Opening cash balance", direction hidden, prefilled with the current value) and
POSTs to **`/app/api/opening-cash`** → `db.update_user_field(user, opening_cash)`.
`cash_position(opening=user.opening_cash)` already adds it, so both cash at hand
AND net worth shift by the new opening (0 is allowed — clears it). Summary
payload now returns `cash.opening`. Verified: opening 100k + 80k sale → cash at
hand 180k; net worth includes it. New route → **deploy required**.

### Part-payment guard + delete/void a transaction (2026-09-25)

Owner recorded a part-payment deposit LARGER than the sale and it went through
(negative debt), and there was no way to fix a mistaken entry.

- **Server-side part-payment guard** (`record_transaction_web`, commit `f67d8c1`):
  the JS guarded deposit≥amount but the server trusted the client. Now the
  server validates: deposit must be 0..amount (negative or >amount → 400), the
  balance owed is ALWAYS derived server-side (amount − deposit), and deposit ≥
  amount is treated as fully paid (no debt).
- **Delete / void a transaction** (commit `2a68463`):
  `transactions.void_transaction_web(phone, tx_id)` reverses side-effects then
  deletes — sale → add stock back + reduce owed-to-me by the outstanding balance
  + reverse contact total; purchase → remove stock + reduce i-owe + reverse
  contact; expense/cash_adjustment → delete only. Production is BLOCKED (unsafe
  to auto-reverse materials+stock — do it in chat); a sale/purchase that already
  has a return is blocked (reverse the return first). `POST /app/api/void-transaction`
  (new route → deploy required); Records rows gained a 🗑 delete with a two-tap
  confirm; summary + records refresh after.
- Also: web expenses/purchases now auto-categorise (commit `a72b987`) — the form
  has no category picker, so the server runs TransactionCategorizer when none is
  sent (Fuel → Utilities & Services etc.). Deleted 27 Honda/Toyota/Howo
  car-dealer TEST transactions from the owner's live data (the ₦65M in the chat
  monthly report was those, not the satchet water — the water sale is ₦65k).

New/updated routes needing deploy: /app/api/produce, /app/api/cash-adjust,
/app/api/opening-cash, /app/api/void-transaction.
