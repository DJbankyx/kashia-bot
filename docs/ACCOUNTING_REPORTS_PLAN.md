# Accounting & Reports — Correctness Plan

_Spec doc. Status: SCOPING. Supersedes the ad-hoc report math in reports.py /
pdf_generator.py. Build BEFORE Stage 4C. Telegram-first, WhatsApp-safe, verify
+ push each stage._

## Why this exists (the problem in one example)

From a real generated statement:

```
REVENUE      250,000,000   (2 Hondas sold)
COGS        (420,000,000)  (1 Mercedes BOUGHT this period)
GROSS PROFIT -170,000,000  ← WRONG
```

The Mercedes is **unsold inventory**, not the cost of the Hondas sold. The
report subtracted a purchase that hasn't been sold, turning a possibly
profitable month into a fake 170M "loss". This is the COGS trap. The fix is to
compute reports on **accounting-correct rules**, not raw cash buckets.

## Decisions (locked with the owner)

1. **Cost basis: WEIGHTED AVERAGE.** Each product carries a running average unit
   cost, updated on every purchase. A sale's COGS = avg_unit_cost × qty at the
   time of sale. Specific-cost overrides the average ONLY when a sale explicitly
   records its own landing cost (high-value unique goods, e.g. a specific car).
2. **Accrual basis.** Revenue is recognised when the SALE happens (not when cash
   is received). A purchase becomes inventory when it happens.
3. **Credit is NOT a P&L item.** Unpaid sales → **Accounts Receivable** (asset).
   Unpaid purchases → **Accounts Payable** (liability). These live on the
   balance-sheet side, never in the P&L. (Today debts are derived from contacts;
   we keep that as the receivables/payables source.)
4. **Strict on cost AND price.** Both are compulsory at entry:
   - A SALE cannot complete without a unit price (it's the sale amount) AND a
     cost basis resolved (from the sale, else the product's weighted-avg cost;
     if neither exists, the user is prompted and must provide/skip explicitly).
   - A PURCHASE cannot complete without a unit cost (needed to update the
     weighted average) and quantity.
   - Reports still FLAG anything that slipped through as "uncosted" — never fake
     a zero cost.
5. **Fix correctness FIRST, full set, nothing dropped**, then styling.

## The three reports (kept SEPARATE, correctly labeled)

### A. Profit & Loss (accrual) — "did I make money on what I sold?"
```
Revenue                    (sales in period, accrual)
− COGS                     (weighted-avg cost of goods SOLD, not bought)
= Gross Profit  (+ margin %)
− Operating Expenses       (rent, transport, salaries… NOT COGS)
= Net Profit    (+ net margin %)
[note] Excludes N sale(s) with no recorded cost.   ← only if any
```
- COGS is driven by SALES × their cost basis. Purchases NEVER appear as an
  expense here.
- Credit sales ARE revenue here (accrual). Their unpaid balance is receivables,
  shown on the balance-sheet report, not deducted here.

### B. Cash Flow — "did money move in/out this period?"
```
Cash In    (payments actually received — cash sales + debt collected)
Cash Out   (payments actually made — purchases paid + expenses paid + debt paid)
= Net Cash Movement
```
- This is where the 420M Mercedes purchase correctly appears as cash out.
- Explicitly labeled a CASH view (the current in-chat report already does this
  honestly — keep it, wire it to the shared module).

### C. Inventory / Position — "what am I holding, who owes what?"
```
Inventory on hand:  units + value at weighted-avg cost   (the unsold Mercedes = asset)
Accounts Receivable: total owed to you   (from debtors)
Accounts Payable:    total you owe       (from creditors)
```
- The unsold Mercedes shows here as a 420M asset — NOT a loss. This is the piece
  that makes the P&L make sense to the owner ("I'm not losing money, my cash is
  in stock").

### Reconciliation guarantee
All three read from ONE shared module (below) over the same transaction set, so
they never disagree. Inventory identity holds:
`COGS = Opening Inventory + Purchases − Closing Inventory` (per-item weighted-avg
should reconcile to this).

## The shared engine: `services/accounting.py` (new)

Single source of truth. Extends/absorbs `reports._period_totals`. Pure
computation, no I/O formatting.

```
class Accounting:
    def period_pnl(user_id, start, end) -> dict:
        # revenue, cogs_of_sold, gross_profit, opex, net_profit,
        # uncosted_sales[], margins
    def period_cashflow(user_id, start, end) -> dict:
        # cash_in, cash_out (paid only), net_cash
    def position(user_id, as_of) -> dict:
        # inventory_units, inventory_value, receivables, payables
    def cogs_for_sale(sale_tx, product) -> (cost, source)
        # source ∈ {"sale_landing_cost","weighted_avg","catalog","MISSING"}
```

- Reuses existing cost sources: sale `landing_cost` → product weighted-avg →
  catalog `get_landing_cost`. Returns MISSING (never 0) when nothing is known.
- `reports.py`, `pdf_generator.py`, `export_service.py`, and the dashboard all
  call THIS. No module computes its own totals anymore.

## Weighted-average cost — how it's maintained

Add to each catalog product: `avg_cost` (running weighted average) and
`stock_qty`. On a PURCHASE of qty Q at unit cost C:
```
new_avg = (stock_qty*avg_cost + Q*C) / (stock_qty + Q)
stock_qty += Q
avg_cost = new_avg
```
On a SALE of qty Q:
```
cogs = Q * avg_cost           (unless the sale carries a specific landing cost)
stock_qty -= Q                (avg_cost unchanged by a sale)
```
- Migration: existing products have `landing_cost` but maybe no `avg_cost`. Lazy
  backfill: treat existing `landing_cost` as the opening `avg_cost` when
  `avg_cost` is absent (read through a normalizer — per the roadmap discipline).
- Variants: maintain `avg_cost` per variant leaf (mirror existing
  `variant_costs`).

## Strict cost/price capture (entry-time)

- **Sale:** the sale amount = price (already captured). Cost basis resolved via
  `cogs_for_sale`. If MISSING and the item is a known product, prompt to set the
  cost now (extends the sale-cost flow already started). Allow an explicit
  "skip — no cost" that tags the sale `uncosted=true` so reports can flag it.
- **Purchase:** require unit cost + qty; on save, update weighted average. A
  purchase with no cost is rejected (can't average without it).
- Reports always surface an "N uncosted sales" note when `uncosted` sales exist,
  and exclude them from COGS/margin rather than distorting it.

## Report DETAILS — compulsory vs optional (applies to PDF + chat + Excel)

Compulsory (always shown):
- Business name, report title, period range, generated-on timestamp.
- P&L: Revenue, COGS, Gross Profit, Opex, Net Profit (+ margins).
- Cash Flow: Cash In, Cash Out, Net Cash.
- Position: Inventory value, Receivables, Payables.
- Currency label (NGN — never the ₦ glyph in ReportLab PDFs; it renders as ■).
- Uncosted-sales disclosure when applicable (integrity).

Optional (shown when set / toggled by the user):
- Logo (already), bank details, business address, TIN (per Documents plan:
  address compulsory on invoices, TIN optional/toggled — reports follow the
  same profile prefs).
- Per-category breakdowns (top expenses, revenue by category).
- Per-product margin table (from weighted-avg cost).
- Comparison to previous period (later).

## PDF/label correctness fixes (carry-overs, don't lose them)

- ReportLab currency: use "NGN", never ₦ (■ glyph bug) — DONE for the current
  statement; keep this rule for all new PDF sections.
- Never label cash math as "Profit". P&L = accrual; Cash Flow = cash. Distinct
  titles, distinct pages/cards.

## Build order (correctness first; verify + push each)

- **R1 — `services/accounting.py`** with `cogs_for_sale` (weighted-avg + specific
  + catalog fallback + MISSING) and `period_pnl`. Unit-test the Mercedes/Honda
  scenario: COGS should reflect only SOLD goods, and unsold stock stays out of
  P&L. Wire `reports._period_totals` to delegate here (dashboard reads correct
  numbers). WhatsApp/Telegram unchanged in shape, only the math corrected.
- **R2 — Weighted-average maintenance** on purchase/sale in catalog + the sale
  cost flow; lazy `avg_cost` backfill from `landing_cost`. Enforce strict cost
  at purchase; strict cost resolution + explicit uncosted tag at sale.
- **R3 — True P&L report** (chat card + PDF) reading `period_pnl`: Revenue, COGS
  (of sold), Gross Profit, Opex, Net Profit, margins, uncosted note. Fix the PDF
  P&L to stop treating purchases as COGS.
- **R4 — Cash Flow report** wired to `period_cashflow` (paid-only in/out),
  clearly labeled cash; keep the honest in-chat version, add a PDF section.
- **R5 — Position report** (`position`): inventory value at weighted-avg,
  receivables, payables. New chat card + PDF section. This is where the unsold
  Mercedes correctly appears as an asset.
- **R6 — Report details pass**: compulsory/optional fields, profile toggles
  (address/TIN), per-product margin table, category breakdowns.
- **R7 — Dashboard alignment**: headline = accrual Net Profit (with uncosted
  caveat); cash net as a secondary line; drill-downs read the shared module.
- **R8 — Verify + reconcile**: dry-run each report against crafted datasets;
  assert the inventory identity reconciles; confirm WhatsApp paths intact; owner
  live-tests real PDFs.

## Guardrails
- One shared computation module; no report computes its own totals.
- Accrual for P&L; cash view separate; credit = receivables/payables, never P&L.
- Weighted-average default; specific cost when a sale carries its own.
- Never fake a zero cost — resolve or flag.
- Read product cost through a normalizer (lazy avg_cost backfill) so old data
  adapts safely (roadmap discipline).
- NGN text in PDFs, never the ₦ glyph.
- Verify with mock-data dry-runs before deploy; owner live-tests.

## Out of scope (later)
- Multi-currency. FIFO/specific-lot ledger UI. Full double-entry GL. Tax
  computation/filing. Depreciation of fixed assets. Prior-period comparatives
  (R6+ candidate).
