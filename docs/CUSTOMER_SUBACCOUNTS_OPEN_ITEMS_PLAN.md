# Customer sub-accounts + open-item debt — PLAN (design, not yet built)

_Drafted 2026-09-27. Grew out of payment-collection testing: owner needs
per-item debt ("rice paid, oil still owing"), "I pick" what a payment covers,
multi-item invoices assembled from sales made at different times, and a way to
keep the SAME customer's balances separate by site/branch/project (gas→mall→
tenant was the example; must be generic)._

## The one insight
Three asks that sounded separate are ONE feature:
- "rice is paid, oil still owing" — per-ITEM debt, not a lump number.
- "I pick what this payment is for" — apply a payment to a chosen item/account.
- "same tenant at Delta vs Asaba are different balances" — a GROUP layer under a
  customer.
- "one invoice for several items bought at different times" — assemble a document
  from several open items.
All four are the same underlying change: **track a customer's debt as individual
OPEN ITEMS you can name, group, and pay against selectively**, instead of one
running number per contact.

## Generic model (mall is just one instance)
The middle layer is a label the OWNER names — site / branch / project / location /
outlet. Same shape fits many businesses:
- gas distribution: customer → mall (site) → tenant balance
- landlord: tenant → property/unit
- supplier: company → branch / PO
- contractor: client → project/site
- wholesaler: shop owner → delivery batch

Proposed structure (backward-compatible):
```
Customer (existing contact)
  └─ Account / Sub-account (NEW, optional)  e.g. "Ikeja Mall", "Delta shop"
        └─ Open items (unpaid sales)         each: what, amount, date, balance
```
A customer with NO sub-accounts behaves exactly as today (one balance) — so every
existing flat customer + all of WhatsApp is untouched until the owner opts in.

## What already exists to build on (grounded in the code)
- A credit sale ALREADY stores `balance_owed` on its transaction row
  (`features/transactions.py:588`). Open-item tracking can build on THIS per-sale
  balance rather than inventing a new store — an "open item" ≈ a credit-sale tx
  whose `balance_owed > 0`.
- Debt today is a lump field per contact: `record_debt` increases it
  (`database.py:1434`), `settle_debt` decreases it floored at 0 (`:1488`), both
  MUTATE IN PLACE (this is why "who owed me on date X" isn't answerable today, and
  why same-name customers collapse — `contact_id = name.lower().replace(' ','_')`,
  one per name, `:1331`).
- Accounting receivables = SUM of contact debt fields (`accounting.py:677`), NOT
  derived from open transactions. So if we move the source of truth to open items,
  receivables must sum open items instead (or we keep the contact field as a
  cached roll-up of its items). DECISION NEEDED (see options).
- Money is kobo-precise (`utils/money`); `_is_debt_settlement` keeps repayments out
  of P&L; soft-delete/retention apply. Any new item type must respect these.

## Design options (pick before building)
### Option 1 — Sub-account as a TAG on the contact + open items on transactions
- Add an optional `account`/`site` string to a contact and to each credit-sale tx.
- Debt per (customer, account) = sum of that account's open items' `balance_owed`.
- "I pick" payment = choose account (and optionally specific items) → reduce those
  items' `balance_owed` oldest-first WITHIN the picked account.
- Smallest change; reuses existing tx `balance_owed`; the contact lump field
  becomes a cached total (or is derived on read).
- Limit: an "account" is just a label, not its own directory entry.

### Option 2 — Sub-account as a first-class record (parent contact + child accounts)
- A customer can have child "account" records, each with its own balance + open
  items; CRM shows customer → accounts → items.
- Cleaner directory ("Tenant X: Ikeja ₦40k, Delta ₦12k"), better reports by site.
- Bigger: new record type, new CRM screens, more migration care.

### Option 3 — Full open-item ledger (source of truth = items, no lump field)
- Debt is ALWAYS the sum of open items; contact lump field retired/derived.
- Enables true statements, "who owed on date X", aging. Biggest change; touches
  accounting.position + every balance surface.

## Recommendation (staged, low-risk)
1. **Stage 1 — Open items (foundation).** Treat each credit sale's `balance_owed`
   as an open item; a payment can target specific item(s) ("I pick") instead of the
   lump. Contact lump field kept as a cached roll-up so nothing else breaks. This
   alone delivers "rice paid, oil owing" + "I pick".
2. **Stage 2 — Sub-accounts (Option 1 tag first).** Add the optional site/branch
   label to contacts + credit sales; group open items + balances by it. Delivers
   "same customer, different site". Start as a tag (Option 1); promote to
   first-class (Option 2) only if the tag proves too thin.
3. **Stage 3 — Multi-item invoice/receipt from open items.** Assemble one document
   from several open items (any dates) for a customer/account, reusing the
   `tg_invoice` line-item flow + `pdf_generator`. Delivers the speed win.
4. **Stage 4 — Pay-link on an account / multi-item bill.** The pay-link (A, done)
   points at a chosen account or a set of open items; the collection webhook
   settles THOSE items ("I pick" at payment time). This is the real "Build B",
   now resting on a model that can actually hold the answer.

## Backward-compatibility (hard requirements)
- Customers with no sub-account behave exactly as today (one balance). WhatsApp
  path unchanged; sub-accounts are a Telegram-first, opt-in concept.
- Keep the contact lump field working as a cached roll-up during Stages 1–2 so
  reports/mini-app/PDF keep reading it until they're migrated deliberately.
- Kobo-precise throughout; `_is_debt_settlement` still excludes repayments from P&L;
  soft-delete/retention respected for any new item/record type.

## Owner decisions (LOCKED 2026-09-27)
1. **Bills/payments can span sites — flexibly, user's choice.** A bill can stay
   within one site OR mix open items from several sites; the owner decides per bill.
   → Design for the general case: a bill / a payment targets a chosen SET of open
   items (wherever they sit); "all items of one site" is just a convenient preset,
   not a hard rule. A payment therefore reduces the specific items it's applied to,
   which may live under different sites.
2. **Reports: BOTH** — balances by site AND by customer-across-all-sites. Roll-ups
   at both levels.
3. **The middle-layer label is USER-DEFINED.** No hard-coded word. Owner names it
   (Mall / Property / Project / Outlet / Branch…); default to a neutral word (e.g.
   "Location") if unset. Store the owner's chosen label; render it everywhere.
4. **Flat by default; sites only when needed.** Existing + new customers stay flat
   (one balance, no sites) until the owner explicitly adds a site to that customer.
   Simple/walk-in customers never see the site concept. WhatsApp path unchanged.

## (historical) Open questions — now answered above
1. tree vs span → SPAN allowed, user's choice (#1).
2. reports → BOTH (#2).
3. naming → user-defined label (#3).
4. migration → flat until necessary (#4).

## Status
- **Stage 1 — SHIPPED (engine, commit fbbe48d).** `features/open_items.py`
  (`OpenItems`): each unpaid credit sale/purchase (`balance_owed>0`) is an open
  item. `list_open_items` (oldest-first), `open_total`, and `settle_open_items`
  (pay CHOSEN items first = "I pick", spill oldest-first, reconcile the lump debt
  field). `transactions.record_transaction_web` now stamps `balance_owed` +
  `open_item=True` on every credit sale. Backward-compatible, engine-only (no UI),
  kobo-precise; lump field stays authoritative for reports. Verified via a
  round-trip test (list, cash excluded, "I pick" 4000 on oil spills 1000 to rice,
  lump reconciled 10k→6k).
- **Stage 2 — SHIPPED (engine, commit 0080a06).** Option 1 (site as a TAG, no
  contact re-keying). A credit sale carries an optional `site`; the SAME customer
  stays one contact with one lump total, the per-site breakdown derived from the
  items' `site`. `open_items.sites_summary` returns BOTH views (per-site balances +
  customer total); `settle_open_items(site=…)` pays ONE site's items only (Delta
  paid, Asaba untouched); `get_site_label`/`set_site_label` store the owner's
  user-defined layer name (default "Location"). Engine, backward-compatible,
  kobo-precise. Verified round-trip (Delta 8k / Asaba 4k stay separate; pay 6k to
  Delta → Delta 2k + Asaba 4k, lump 12k→6k).
- **Stage 1+2 MINI-APP UI — SHIPPED (commit 7cc0b20).** Record a credit/part/
  pay-link SALE with an optional Site (labelled with the owner's layer name); the
  contact card shows the open balance grouped BY SITE with a per-site "Pay" that
  settles only that site's items; a "⚙ <label>" control names the layer. New routes:
  GET /app/api/open-items, POST /app/api/settle-open, POST /app/api/site-label
  (template.yaml) → DEPLOY REQUIRED. CHAT surface for site is still deferred (a
  follow-on; the engine supports it).
- **Stage 3 (multi-item invoice from open items)** — NOT STARTED.
- **Stage 4 (pay-link on an account / "Build B")** — NOT STARTED; rests on 1–2.

Sequenced so each stage ships value and stays backward-compatible. Pay-link A is
live. Allocation is "I pick" (what the owner wanted) rather than a guessed rule.
Related: docs/PAYLINK_PRODUCT_PICKER_PLAN.md, docs/PAYMENT_COLLECTION_PLAN.md.
