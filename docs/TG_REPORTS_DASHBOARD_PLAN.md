# Stage 3 — Reports & Dashboard (Telegram, tap-first)

_Plan doc. Status: NEXT / ACTIVE. Owner: Kashia Telegram Elevation._

## Goal (plain English)

Give the business owner a **living dashboard** they can read at a glance and tap
into — not a wall of text. One card that answers "how is my business doing?",
switchable between Today / This Week / This Month / Last Month with a tap, and
drill-downs into the parts that matter (top products, profit/margin, expenses by
category, who owes what). It must feel like an app screen, not a bot dump.

Everything reuses the **existing engine report math** (P&L, true margin, cost
lookup). We change presentation and interaction only. **WhatsApp is untouched** —
this is gated to Telegram (`_is_telegram` / `platform_for_user`); WhatsApp keeps
the current `reports.show()` list + text reports.

## What already exists (reuse, don't rebuild)

In `src/features/reports.py`:
- `show()` — period selector list (`report_today/week/month/last_month`).
- `_pnl_report(period)` — dual P&L: **Net Cash** (revenue − purchases − opex) and
  **True Margin** (revenue − COGS = gross − opex = net), with margin %.
- `_build_margin_report_v2()` — per-item margins (top 5), cost from
  `landing_cost` / variant / tree leaf.
- `_build_hybrid_revenue_split()` — product vs service revenue (hybrid).
- `_build_production_summary()` — manufacturing output summary.
- `_tab_report(tab)` — Sales / Purchases / Expenses filtered views.
- `_date_range(period)` → (start, end, label); `db.get_transactions_by_period`.
- Excel export (`__EXPORT_REPORT__`) + PDF (`report_pdf_<period>`).

Also available: catalog cost/price/stock (`get_normalized_product`), debt summary
(`get_all_debtors` / `get_all_creditors`), contact analytics.

## The design — "one dashboard card, edited in place"

Same language as the tidy-box and the catalog shelf: a **single message** that
re-renders as you tap period/drill buttons (via `edit_message_text`), so the chat
doesn't fill with report dumps. Reserved callback namespace: `__tgdash__`
(mirrors `__tgfx__` / `__tgpg__`), routed by `telegram_webhook` to a dashboard
handler; WhatsApp never emits these.

### The dashboard card (period = This Month by default)

```
📊 Dashboard — This Month
────────────────────────
💰 Revenue     ₦X
📦 Purchases   ₦Y
💸 Expenses    ₦Z
────────────────────────
📈 Net (cash)  ₦N   (n% )
🟢 Gross margin  ₦G  (g%)   ← when costs on record
🔴 Owed to you ₦D  ·  📝 You owe ₦C

[📅 Today] [📆 Week] [🗓️ Month] [Last]      ← period toggle (re-renders card)
[🏆 Top Products] [📈 Profit] [💸 Expenses]  ← drill-downs
[🧾 Statement/PDF] [📎 Excel] [☰ Menu]
```

Period toggle taps just re-run the numbers for the new range and edit the card.

### Drill-downs (each edits the card, with a ← Back to dashboard)

1. **🏆 Top Products** — ranked by revenue for the period (qty sold, revenue,
   margin each). Reuses margin-v2 per-item logic; extend to rank + show units.
2. **📈 Profit / Margin** — the true-margin breakdown (revenue, COGS, gross,
   opex, net) + margin ranking (best/worst margin items). Flag uncosted sales
   ("N sales missing cost — set costs for accurate profit").
3. **💸 Expenses by category** — group opex by category for the period, sorted;
   show each category's share. (Category derived at save time already.)
4. **(hybrid)** revenue split product vs service; **(manufacturing)** production
   output + material usage summary.

### Per-industry tailoring (presentation only)
- Trading/Retail: Revenue / COGS / Gross margin / Top products.
- Services: Revenue (jobs) / Supplies used / Net; "Top services" instead of
  products; no COGS-heavy framing.
- Manufacturing: add Production output + material cost; margin on finished goods.
- Hybrid: product vs service split up top.
Sourced from the industry classes' terms; the number crunching is shared.

## Build order (safest-first, verify + push each)

- **3A — Dashboard entry + card.** On Telegram, `menu_report` / `/report` opens
  the in-place dashboard card (This Month) instead of the old list. Numbers from
  `_pnl_report` internals refactored into a `_period_totals(period)` helper that
  returns a dict (revenue, purchases, expenses, net, gross, margin%, owed,
  owe) so both the card and the legacy text report share one source of truth.
  WhatsApp still calls `show()`.
- **3B — Period toggle.** `__tgdash__:period:<today|week|month|last>` re-renders
  the card in place. Stash current period in session.
- **3C — Drill: Top Products.** Rank period sales by revenue; show qty + margin.
- **3D — Drill: Profit/Margin.** Reuse margin-v2; add best/worst margin ranking
  + uncosted-sales nudge.
- **3E — Drill: Expenses by category.** Group + sort opex.
- **3F — Industry tailoring** (hybrid split, manufacturing production) + the
  debt line (owed/owe) wired to `get_all_debtors/creditors`.
- **3G — Export hooks.** Keep the existing Excel/PDF export reachable from the
  card (period-aware).
- **3H — Verify + polish.** Dry-run each period + drill with mock transactions;
  confirm WhatsApp `reports.show()` unchanged; live-test.

## Decisions / guardrails
- **No new money math.** All totals come from the engine's existing P&L + margin
  logic (refactored into a shared `_period_totals`), so the dashboard can never
  disagree with the text report or the exports.
- **In-place editing** (edit_message_text) — one card, no chat spam. Long lists
  (top products) can page with the existing `__tgpg__` pager.
- **Charts:** deferred. Telegram can't render inline charts cheaply; we use
  compact text bars (e.g. `▓▓▓▓░░ 62%`) if useful, images only much later.
- **WhatsApp untouched.** Dashboard is Telegram-gated; `__tgdash__` callbacks
  never reach WhatsApp.
- Verify with mock-transaction dry-runs before deploy; user live-tests after.

## Out of scope (later stages)
- Interactive invoice/receipt builder → Stage 4 (Documents).
- Debt board with tap-to-remind / aging buckets → Stage 5.
- Image charts / mini-app dashboards → Stage 7 (Mini App).
