# Mini App — Audit & Improvement Backlog

_2026-09-23. Full audit of `src/handlers/miniapp.py` (endpoints + embedded
HTML/CSS/JS) cross-checked against the engine (transactions/production/catalog/
accounting/contacts) and `template.yaml`. Owner is testing in parallel; this is
the running list of what to fix/add. Ordered by priority within each section._

## Good news (verified solid)
- Wiring is clean: every JS `api()/apiPost()` call maps to a routed endpoint,
  every route is declared in template.yaml, no orphaned routes, no missing
  onclick handlers.
- No forbidden JS money math — cost/margin always come from the server.
- The two money-writes (transaction, debt-payment) are idempotent via
  `claim_web_submit(submit_id)`; save buttons disable on click.
- Stock-value roll-ups come from the engine (`_row_from_product` → `tree_rollup`),
  not recomputed in JS.

---

## P1 — Correctness (can produce WRONG numbers) — do first
1. **Fractional quantities are truncated on sale/purchase/stock.** `saveRecord`
   uses `parseInt(rec-qty)` then `Math.max(1,…)`, so 0.5 kg → 1 whole unit;
   `saveSheet`/`bump`/`set_stock` also `parseInt` stock+cost, and cost like
   ₦12.50 → 12. Recipe qty is already `parseFloat` (correct) — make sales/
   purchases/stock/cost match. FIXED for recipe cost input (`1c7a884`); the
   sale/purchase/stock inputs still need it (needs care: engine + stock
   deduction path expect the value; verify `record_transaction_web` handles a
   float qty end-to-end).
2. **Recipe↔stock unit mismatch is unvalidated.** A material created with unit
   "g" but stocked in "kg" makes recipe cost off by 1000× and breaks production
   deduction. MITIGATED with a hint (`1c7a884`); real fix = validate/convert the
   recipe unit against the material's stock unit + its `conversions` on save,
   or force the recipe unit = the material's stock unit for existing materials
   (already read-only in the UI for existing; new materials still free-typed).

## P2 — Missing / half-wired
3. **`is_service_job` never sent from the web.** `_transaction_write` reads it
   and `record_transaction_web` stamps `sale_kind="service"`, but `saveRecord`
   never sets it → Services web sales are miscategorised as product sales.
   Fix: for Services (and Hybrid service sales) send `is_service_job:true`.
4. **Purchases can't pass a real unit cost.** Web purchase only sends total
   amount + qty; unit cost is derived `amount // qty` (integer division). Add an
   optional unit-cost field OR make the derivation float.
5. **Records list capped at 50 with no pagination.** `_records` supports
   limit/offset + returns `has_more`, but JS never sends a limit and has no
   "load more". Busy months: user sees 50 + a hint, never the rest in-app.
   Add a "Load more" (offset) control.
6. **Export status is string-matched off chat copy.** `_export`→
   `handle_filtered_export` returns human text the app greps for
   ("exported"/"couldn't deliver") to decide ok/fail. Brittle: reword the chat
   and the app lies. Return a structured `{ok, delivered}` from the exporter.
7. **Picker empty-state points away from the app.** `renderPickerProducts`
   still says "Add one in chat first" though the app has Add-product. Update copy.

## P3 — Industry-awareness gaps (Trading is the control; these hit svc/mfg/hybrid)
8. **Services sale still forces Quantity + catalog product pick.** `recSyncLabels`
   only strips qty/cost for `expense`. A Services "Job/service" sale shouldn't
   require a stocked catalog item or a quantity. Consider: for Services sale,
   hide qty + allow a free-text description instead of forcing a product pick.
9. **Catalog tab tiles are trading-worded** ("Products / Total stock / Stock
   value (at cost) / Low stock / Add product / Search catalog…") — never
   re-labelled by `applyIndustryLabels()`. Services ("Services & supplies")
   sees stock/valuation framing that's meaningless for pure services.
10. **Picker sheet title + empty-state hardcoded "product"** (only the button
    placeholder uses `t('pick_hint')`).
11. **Add/edit product is inventory-shaped for services** (Stock/Reorder/Selling
    price/Cost). A pure service has no stock. `_catalog_health` also nags a
    services item for `no_unit`/`no_price`.
12. **Item-type re-tagging is chat-only.** A mfg finished product NOT tagged
    `finished_product` (legacy, or created as raw in web) never shows the recipe
    button in the web → stuck. Allow setting item type in the web edit sheet.

## P4 — Robustness
13. **Catalog/recipe writes lack idempotency.** `_product_write` (esp.
    `set_stock_delta`) and `_recipe_write` have no submit_id; only button-disable
    guards. A lost-response retry can double-apply a delta. (`set_stock` exact +
    recipe-add-by-name replace mitigate most; still worth a guard.)
14. **Multi-field edit isn't transactional.** `saveSheet` chains rename→stock→
    price→cost→unit→reorder→category; a mid-chain failure leaves a partial save
    with only a toast. Consider a single batched write endpoint.
15. **CRM not refreshed after a credit sale from the dashboard.** `saveRecord`
    reloads summary + inventory (if visible) but not CRM, so debt figures on the
    Customers tab go stale until reopened. (`savePay` does it right.)
16. **No token refresh across the 24h initData expiry** — long-open app 401s
    every call until reopened; a mid-multi-op expiry can partial-commit.

## P5 — Smart features worth adding (all reuse existing engine data)
- **Low-stock action list**: tap the "Low stock" card → filter to low items →
  each opens its edit sheet to restock. (`low_stock`/`reorder_level` already returned.)
- **Uncosted-sales drill**: dashboard already shows "N sales have no cost" →
  make it tap into Records filtered to those, fix COGS inline.
- **Profit-per-product / margin ranking** (roadmap #6): thin read endpoint over
  `accounting.period_pnl` per-item + cost stamps.
- **Catalog-health as an actionable checklist**: each `_catalog_health` tip →
  a filter into the catalog (e.g. "3 products have no price" → list → tap to set).
- **Debt aging (overdue / due soon)** on Customers: `_contacts` already returns
  `due_date`/`last_date`.
- **Cash-flow mini-trend** on the dashboard: near-copy of the existing
  profit-trend chart, over `accounting.period_cashflow`.
- **Send statement/receipt for a contact** from the contact sheet (reuse the
  chat statement flow + export-to-chat delivery).

## Done in this pass (2026-09-23)
- Added hints: recipe unit-match (`kg here must equal kg in stock`), record-form
  quantity + COGS hints. (`1c7a884`)
- Recipe cost input → decimal/parseFloat (small per-unit rates no longer
  truncate to 0). (`1c7a884`)
- Corrected stale "set recipe in chat" copy → point to the in-app "Set / edit
  recipe" button (edit sheet hint, add-product hint, server 409 message). (`1c7a884`)
