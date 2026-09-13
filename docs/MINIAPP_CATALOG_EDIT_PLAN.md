# Mini App Catalog Editing Plan — full CRUD + variant-leaf editing in the web view

_Created 2026-09-13. For review. Reuses the existing CatalogHandler engine + the
same product data model as chat (no forked logic). All writes are per-user
(initData auth) and money-safe. WhatsApp + chat catalog untouched._

Answers the owner's ask:
> "The catalog items are just basics. I can't edit anything, can't add, delete,
>  do normal catalog stuff. Update it with the catalog stuff."

---

## Current reality (verified)

The Mini App catalog is nearly read-only:
- Non-variant products open an edit sheet (stock / price / cost) via
  `POST /app/api/product` (`_product_write`: set_price / set_cost / set_stock /
  set_stock_delta).
- **Variant (tree) products open a READ-ONLY viewer** — and ALL of this owner's
  products are variant products, so effectively nothing is editable.
- **No add-product, no delete, no rename, no set-unit, no set-reorder** in the app.

So for a car business (every product is a variant tree), the app catalog is a
dead end. This plan makes the web catalog match the chat catalog's power.

## The engine we reuse (no forking)

All in `src/features/catalog.py`, all db-only (the miniapp builds
`CatalogHandler(None, db)` — no session):
- `_get_products(phone)` → dict keyed by `product_key` (`name.lower().replace(" ","_")`).
- `_save_products(phone, products)` → persists via `db.update_user_field("product_catalog", …)`.
- `set_sale_price(phone, key, price)`, `set_cost_direct(phone, name, cost, variant="")`
  (variant-tree aware), `set_stock_exact(phone, name, target, variant="")`,
  `update_stock(phone, name, delta, variant="", cost_mode=…)`.
- Variant tree: `_COMBO_SEP = " / "`; a leaf path is a ` / `-joined value string
  (e.g. `"Sienna / 1992 / White"`). `_vt_get_node(root, path_list)`;
  `_vt_node_total(node)` rolls up stock. `GET /app/api/tree` already returns a
  node's children `{value, stock, cost, is_leaf}` for a `key`+`path`.
- Product fields: `name, stock, landing_cost, sale_price, primary_unit,
  reorder_level, category, item_type, variant_tree`.

Add / rename / delete / set-unit / set-reorder are session-driven chat handlers,
so for the **web** we mutate the products dict directly then `_save_products`
(same persisted shape) — never call the chat handlers.

## Design principles (locked)

1. **One engine, one data model.** Web writes produce the exact same product
   records as chat, so both surfaces stay consistent.
2. **Money-safe.** Cost/stock writes go through the same
   `set_cost_direct`/`set_stock_exact`/`update_stock`, honoring the variant tree.
   COGS on past sales is untouched (that's the separate cost-correction feature).
3. **Per-user + validated.** Every write is initData-authed to the caller's own
   catalog; server validates action + numbers (never trust the client).
4. **Echo the truth.** After a write, return the recomputed row (or node) so the
   UI reflects stored state, not the optimistic input.
5. **Variant leaves are editable, adding new axes/nodes stays in chat (v1).**
   Editing a leaf's stock/cost from the web is safe (unambiguous path). Building
   NEW tree structure (new axis/sub-variant) is complex tap-tree UX — keep that
   in chat for v1; the web can still drill + edit existing leaves.
6. **Confirm destructive actions.** Delete asks "are you sure" in the sheet.

---

## Sequencing (each verified before the next)

### CAT1 — Backend: extend `POST /app/api/product` with new actions
Add to `_product_write` (same auth/validation frame):
- `add` — body `{action:"add", name, category?}` → key = slug(name); reject if
  exists; seed `{name, stock:0, landing_cost:0, sale_price:0, category, variants:[]}`;
  `_save_products`. Return the new row.
- `rename` — `{action:"rename", key, name}` → move the record to the new slug key
  (or just update `name` if we keep the key stable; keep key stable to avoid
  breaking references — update `product["name"]` only). Return row.
- `delete` — `{action:"delete", key}` → `del products[key]` + save. Return `{ok}`.
- `set_unit` — `{action:"set_unit", key, unit}` → `product["primary_unit"]=unit`.
- `set_reorder` — `{action:"set_reorder", key, value}` → `product["reorder_level"]=int`.
- `set_category` — `{action:"set_category", key, category}` (nice-to-have).
Existing set_price/set_cost/set_stock/set_stock_delta stay.

### CAT2 — Backend: variant-leaf WRITE endpoint
- Reuse `GET /app/api/tree` for reading (already exists).
- New `POST /app/api/tree` (or extend product write with `variant` + a
  `leaf_stock`/`leaf_cost` action): body `{key, path:[...], stock?, cost?}` →
  build `variant = " / ".join(path)` → `set_stock_exact(phone, name, stock,
  variant)` and/or `set_cost_direct(phone, name, cost, variant)`. These already
  resync `product["stock"]` via the tree roll-up. Return the updated node.

### CAT3 — Frontend: add-product + richer edit sheet (non-variant)
- Catalog tab gets a **"➕ Add product"** button → small sheet (name + optional
  category) → `add` → refresh list.
- The existing edit sheet (non-variant) gains: **Rename**, **Unit**, **Reorder
  level**, **Category**, and a **🗑️ Delete** (with confirm). Plus the current
  stock/price/cost.

### CAT4 — Frontend: EDITABLE variant drill
- Variant products currently open a read-only viewer. Make each node tappable to
  drill; at a **leaf**, show editable **stock** + **cost** fields (Save →
  CAT2 write) and show roll-up totals at parent nodes.
- Adding a new axis/sub-variant → a note "Add new variants in chat for now"
  (v1 scope line), keeping the money-safe boundary.

### CAT5 — Verify
- Dry-run each action (add/rename/delete/unit/reorder/price/cost/stock + leaf
  stock/cost) against a fake db: correct product-dict mutation, key stability,
  tree leaf write reaches `_vt_get_node`, roll-up resync. `_PAGE_HTML` utf-8
  safe. Chat catalog + WhatsApp untouched. Compile.

---

## Guardrails / scope
- **v1 web = edit existing structure + add/delete whole products + edit leaves.**
  Building NEW variant axes/nodes stays in chat (complex tree-builder UX).
- Rename keeps the product KEY stable (updates display `name` only) so historical
  references (`catalog_product` on past sales) don't break.
- Delete is soft in UX (confirm) but a real removal from the catalog dict (matches
  chat delete); past transactions are unaffected (they carry their own snapshot).
- All numbers validated server-side; Decimal-safe JSON already in place.

## Out of scope (v1)
- New variant-axis creation from the web (chat only).
- Bulk import/CSV catalog upload (separate parked feature).
- Per-leaf rename/delete from the web (chat only for now).
