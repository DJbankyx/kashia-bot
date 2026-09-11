# Kashia — Mini App M6: Editing from the Web View (write phase) — PLAN

_Created 2026-09-11. The Mini App's first WRITE capability. Read-only v1
(M1–M5.1) is live; M6 lets the owner change data FROM the web view. This is the
money-write boundary, so it is staged narrow-first and reuses the shared engine._

## Guiding principles (money-write specific — additions to the v1 plan)
1. **Still one engine.** Web writes go through the SAME functions the chat uses
   (catalog.update_stock / set_cost_direct / sale-price persistence, and later
   transactions._save_transaction). No forked save/stock/cost math.
2. **Auth guards MONEY now, not just views.** Every write validates initData
   (M1) exactly like reads, then derives tg:<chat_id>; a user can only ever
   write to their OWN data. No admin/cross-user writes, ever.
3. **Narrow first.** v1 write = the two LOW-ambiguity, catalog-only operations
   (already have clean engine methods): **Adjust stock** and **Set price / cost**.
   Full transaction entry (sale/purchase with payment/credit/CRM) is a LATER
   phase — it's the most engine-entangled and gets its own sub-stage.
4. **Idempotent + confirmed.** Money accuracy > convenience: every write is an
   explicit user action (a Save tap), guarded against double-submit, and echoes
   the resulting value back so the user sees what changed.
5. **Least privilege, widened only as needed.** MiniAppFunction currently has
   DynamoDB READ policies. M6 adds WRITE only to the tables the write path
   touches (users table — the catalog lives on the user record). No new tables.
6. **WhatsApp untouched.** Telegram-only surface throughout.

## Change-type legend: [UI] page-only · [API] new write endpoint · [PERM] IAM

---

## Scope

### M6a — Catalog writes (v1 of the write phase) ← BUILD FIRST
Two operations, both catalog-only, both already have engine methods:
- **Adjust stock** — set a product's (or variant-tree leaf's) stock to an exact
  count, or +/- a delta. Reuses `catalog.update_stock(pn, name, delta, ...)` (for
  a tree leaf, pass the leaf path as `variant`; the method resyncs the roll-up).
  For "set exact" we compute delta = target − current and call update_stock, OR
  add a thin `set_stock_exact` helper mirroring set_cost_direct. Logs a stock
  movement (update_stock already does).
- **Set price / cost** — set `sale_price` and/or the weighted-avg `landing_cost`
  (or tree-leaf cost). Reuses the sale-price persistence and
  `catalog.set_cost_direct(pn, name, cost, variant)`.

Why these first: they touch ONE product record, have no payment/credit/CRM
entanglement, are trivially idempotent (a write sets a value, not appends a row),
and reuse methods that already exist and are unit-safe.

### M6b — Transaction entry from the web (LATER, separate go-ahead)
Record a full sale/purchase/expense from the web view. This must run through
`transactions._save_transaction` to inherit COGS/weighted-avg/CRM/debt/
confirmation. Higher risk (appends ledger rows → double-submit = double booking;
payment method + credit + who-owes flow). NOT in M6a. Planned as its own stage
with an idempotency key per submit.

---

## Architecture (M6a)

```
Inventory tab (web)                         MiniAppFunction (Lambda)
  tap a product row  ──────────────▶  (page already has the row data)
  open an edit sheet (stock / price / cost)
  tap Save  ──POST /app/api/product──▶  validate initData (M1) → tg:<id>
                                        route by action:
                                          set_stock  → catalog.update_stock / set_stock_exact
                                          set_price  → persist sale_price
                                          set_cost   → catalog.set_cost_direct
                                        return the updated product row (JSON)
  update the row in place  ◀───────────  { ok, product: {...} }
```

- **New endpoint: `POST /app/api/product`** on MiniAppFunction / WebhookApi.
  Body: `{action, key, variant?, value}` (JSON). Actions: `set_stock`
  (value=exact count), `set_stock_delta` (value=+/-), `set_price`, `set_cost`.
  Auth: initData in the `X-Telegram-Init-Data` header, same as reads. Method
  POST (so template gets a POST event; the page fetch uses method:'POST').
- **Idempotency:** these are value-SETS (not row appends), so a repeated submit
  is naturally safe (writes the same value twice = same result). The page still
  disables Save while in-flight to avoid confusing double taps. No idempotency
  key needed for M6a (it WILL be needed for M6b transaction appends).
- **[PERM]** MiniAppFunction gains `DynamoDBCrudPolicy` on UsersTable (the
  product_catalog lives on the user record via `_save_products` →
  `update_user_field(pn, "product_catalog", ...)`). Transactions/Contacts stay
  READ-only for now (M6a doesn't write them).
- **[UI]** Inventory rows become tappable → a small edit sheet (stock stepper +
  "set exact"; price field; cost field) → Save. Re-fetch or patch the row.

---

## Money-safety checklist (M6a)
- [ ] Validate initData on EVERY write; reject (401) on any failure — reuse
      _authenticate() unchanged.
- [ ] Coerce/validate `value` server-side (non-negative int for stock; positive
      int for price/cost); reject bad input, never write garbage.
- [ ] Resolve the product by `key` on the AUTH'D user's catalog only (never a
      key from another user).
- [ ] Tree products: writes target the LEAF via `variant`; product-level stock
      stays a roll-up (update_stock already enforces this).
- [ ] Return the recomputed row so the UI reflects the true stored state (not
      the optimistic client value).
- [ ] Save button disabled while the request is in flight (double-tap guard).
- [ ] Never expose another user's data or accept a user_id from the client.

## Staged build order (each verified before the next)
- **M6a-1 [PERM+API]** — POST /app/api/product with `set_price` + `set_cost`
  only (simplest: pure value sets, reuse set_cost_direct + sale-price persist).
  Add UsersTable CRUD policy. Verify with a signed test POST. Ship.
- **M6a-2 [API]** — add `set_stock` / `set_stock_delta` (reuse update_stock;
  add a small set_stock_exact helper if cleaner). Tree-leaf aware. Ship.
- **M6a-3 [UI]** — tappable inventory rows → edit sheet (stock/price/cost) →
  Save → patch row in place. Deploy + live test. Ship.
- **M6b (LATER)** — transaction entry through _save_transaction with a
  per-submit idempotency key. Separate plan + go-ahead.

Rollback safety: M6a is additive (one POST route + a UsersTable write policy +
page JS). Removing the route + policy fully disables web writes; reads and chat
untouched. All writes reuse existing, tested engine methods.

## Open decisions (confirm before M6a-1)
1. **M6a scope = stock + price/cost only?** (Recommended yes; transaction entry
   = M6b later.)
2. **Stock edit: "set exact" only, or also +/- steppers?** (Recommended both —
   steppers for quick ±1/±5, plus a set-exact field; all map to update_stock.)
3. **Confirm-on-save?** For catalog value-sets, an inline Save (no extra confirm)
   is fine since it's not appending money rows and the new value is echoed back.
   (Recommended: inline Save, echo result. Transaction entry in M6b WILL confirm.)
4. **Keep transactions/contacts read-only on MiniAppFunction for M6a?**
   (Recommended yes — only UsersTable needs write for catalog.)
