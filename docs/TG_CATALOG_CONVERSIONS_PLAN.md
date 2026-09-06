# Catalog "Shelf & Counter" + Units & Conversions — Plan (plain English)

_Created 2026-09-06. For approval before building. THE flagship stage — deepest
and most money-adjacent (catalog stock feeds every sale's profit/stock). Built
safest-first, each step shipped + live-tested before the next. WhatsApp untouched.
Consolidates docs/TELEGRAM_MASTER_PLAN.md (Stage 2) + docs/TG_CONVERSIONS_PLAN.md._

---

## Why this stage exists (the two problems it solves)

**Problem 1 — "Cars = one item".** Onboarding intentionally seeds broad
categories (Cars, Trucks, Buses). But a car isn't one SKU — the real stock unit
is "Camry 2020, black". The catalog needs real product depth: a category holds
products, and a product can have variants (model / colour / year / size), each
with its own stock and cost. Today the catalog is a FLAT list (name, stock,
landing_cost, category, variants[list]) with only a shallow variant layer.

**Problem 2 — the bot isn't "conversant" with units.** A business buys, produces,
and sells the SAME item in DIFFERENT units. Real example (water):
- buys power in **KWh**, recipe records it in **Wh** → wants production in **KWh (master)**
- produces water in **units**; 20 units = 1 bag; 400 bags = 1 truck
- wants to enter production OR a sale in ANY registered unit, and the bot converts
- shows everything in ONE **master unit** per item
The plumbing partly exists (`primary_unit`, `conversions`, `convert_to_base`,
`convert_from_base` in database.py / catalog.py) but is NOT wired through the
Telegram tidy-box flows, which currently ask a plain number with no unit awareness.

---

## Guiding rules (non-negotiable — same as every stage)
1. **One engine, reuse the money logic.** Never fork stock-deduction, cost, or
   margin math. Only change how the catalog is structured, presented, collected.
2. **WhatsApp stays working.** All new tap-first catalog UX gated on the `tg:`
   namespace; WhatsApp keeps today's catalog.
3. **Backward-compatible, lazy data.** Old flat products keep working untouched.
   New fields are optional with safe defaults. A read-helper normalises old→new
   in memory. No bulk rewrite; migrations are lazy and reversible.
4. **Money path verified.** Any change touching stock/cost is verified against the
   existing save path before shipping (it feeds every sale's profit).
5. **Incremental, each sub-step shipped + live-tested before the next.**

---

## Part A — The "Shelf & Counter" Catalog (product depth)

### Target data model (additive on top of today's flat product dict)
Keep the flat product dict as the base. Layer on, all OPTIONAL:
- **category** promoted to a real navigable dimension (already stored, unused for nav).
- **attributes** — USER-NAMED variant axes (flexible depth, 0..N): e.g.
  `{"Model":[...], "Colour":[...], "Year":[...]}`. Generalises today's flat
  `variants` list (old `variants` read as one axis). The stockable unit is a
  COMBINATION of one value per axis.
- **per-variant stock + cost** — extend the existing `variant_stock{}` / `variant_costs{}`
  keyed by the value-combination.
- **reorder_level, supplier** — optional.
- **sku, barcode** — optional text per product/variant; power a "type/paste the
  code to find the item" shortcut at sale time.
- **stock_movements** — append-only log (+/- qty, reason, date, tx_id) for history;
  absence just means "no history yet".
- A **normalising read-helper** so old flat products and new attribute-shaped
  products both render through one code path.

### Telegram catalog UX (tap-first, "shelf & counter")
1. **Shelf view:** categories as a grid → tap a category → paginated product grid
   → tap a product → its card. Search box at the shelf ("type to filter").
2. **Product card (edited in place):** name, category, stock (tap −/+ and "set
   exact"), cost, price, margin %, unit + conversions, variants/attributes,
   reorder flag, supplier. Every field editable via inline taps — no
   "pick product → type value" round trips.
3. **Variants drill-down:** Model → Colour → Size handled by drilling within one
   edited message (breadcrumb + back). Each leaf = its own stock/cost.
4. **Stock intelligence:** low-stock 🔴, reorder-soon flags, total stock value,
   per-product movement history.
5. **Counter mode:** quick stock-take / bulk adjust.

### Engine work (the root part)
- **[DE-BIAS]** Remove the `[:9]/[:10]` caps from recording/management pickers for
  Telegram (WhatsApp keeps them via the guard). List builders return full data;
  the Telegram layer paginates (reuse the pager we already use for CRM).
- **[EXTEND]** `search_products(query)`, `get_products(category=...)`.
- **[EXTEND]** `record_stock_movement()` + reorder checks (feeds low-stock alerts
  and history).
- **[EXTEND]** the normalised product read-model (old flat ↔ new attributes).
- **REUSE untouched:** all save/adjust/cost math, `ensure_item_types`,
  `_find_product_key`, stock deduction on sale (the money path).

---

## Part B — Units & Conversions (master unit + convert everywhere)

### The model (per item)
- A **master (base) unit** + a set of **conversion factors** to the base — a small
  "unit graph": base=piece; 1 bag = 20 pieces; 1 truck = 400 bags = 8,000 pieces.
- SAME conversions apply at ALL levels:
  1. **Buying inputs** — record a purchase in any unit → store in base.
  2. **Production / recipe** — inputs recorded in one unit, produced in another →
     convert to base for stock + cost.
  3. **Selling output** — sell in any unit → convert to base to deduct stock +
     compute cost/profit per base unit.
- Reports, stock, cost-per-unit shown in the **master unit** (entered unit visible
  too, e.g. "20 bags = 400 pieces").

### Where units get set (ties into Catalog + Onboarding)
- Set/edit an item's master unit + conversions from the **product card** (Catalog).
- The recipe-registration flow's unit question gets reworked here (user asked to
  change it; it's WhatsApp-shaped today).

### The tidy-box flow change (sell/buy/produce quantity step)
When an item has registered units, the quantity step shows a **unit toggle**:
after "How many?" offer the registered units as chips — e.g. [pieces][bags][truck].
Tap a unit, type/tap the count, bot converts to base. If an item has only a base
unit (no conversions) → skip the toggle (keep simple items simple). This is the
proposal from TG_CONVERSIONS_PLAN.md, refined:
- "sold 20 bags" → 20 × 20 = 400 pieces → deduct 400 from stock, price per the
  entered unit or per base, consistently.

### Engine work
- **REUSE** `convert_to_base` / `convert_from_base` (already exist) — wire them into
  the tidy-box quantity step + production + purchase, all behind the `tg:` guard.
- **[EXTEND]** store master unit + conversions on the product (fields exist:
  `primary_unit`, `conversions`) and surface them in the product card.

---

## Sequencing (safest-first — each verified + live-tested before next)
**Stage 2A — Data model + read-helper (no visible UX).**
  Additive fields + normalisation + lazy migration. Prove old products still load.
**Stage 2B — Shelf browse + search + paginated grid (READ-ONLY).** Deploy + test.
**Stage 2C — Product card (read-only, rich, debt-first-style clean).** Deploy + test.
**Stage 2D — In-place editing: stock −/+, cost, price, unit.** Deploy + test.
**Stage 2E — Variants/attributes drill-down + editing.** Deploy + test.
**Stage 2F — Units & Conversions wired into sell/buy/produce quantity step.**
  Deploy + test (money-adjacent — verify stock/profit carefully).
**Stage 2G — Stock intelligence: reorder flags, stock value, movement history.**
**Stage 2H — Counter mode / bulk ops.** (optional, last)

Rollback safety: every sub-step is behind the `tg:` guard and additive; WhatsApp's
catalog is never touched; old product records always render.

## Risk notes
- Catalog stock feeds money (sale deductions). Every model change ships behind
  `tg:` + additive fields, verified against the existing save path.
- Units maths must be exact — a wrong conversion mis-deducts stock and mis-prices.
  Reuse the tested convert_to_base/from_base; add unit tests around them.
- This is the biggest stage — expect it to span several build+test rounds. We do
  NOT try to ship it all at once.

## Decisions (agreed 2026-09-06)
1. **Variant depth = FLEXIBLE, not fixed.** Each product defines its OWN named
   attribute axes (0, 1, or many). The user names the axes + values; the
   sellable/stockable unit is a COMBINATION. Examples: Cars → Model/Colour/Year;
   Shoes → Size/Colour; Water → none; Rice → Bag size. Simple products never see
   variants; complex ones get as many levels as they need. The drill-down walks
   whatever axes that product has (one tap per axis).
2. **Order: CATALOG depth FIRST (2A–2E), THEN Units (2F).** Confirmed.
3. **Reorder / low-stock: INCLUDE in this stage** (reorder level per product +
   low-stock 🔴 flags + reorder-soon nudges).
4. **SKU + barcode: INCLUDE as searchable fields + type-to-find.** SKU = a short
   code the user assigns (e.g. CAM-BLK-20); barcode = the packaged-goods number.
   Store both as optional text on a product/variant; when recording a sale, the
   user can type/paste the SKU or barcode to jump straight to the item (fast for
   shops with many items). CAMERA/photo barcode scanning (send a photo → bot reads
   the number via the existing vision pipeline) is NOTED but deferred — heavier,
   optional later enhancement, not this stage.
5. **Redesign fully (it's WhatsApp-shaped today).** Agreed changes:
   - REDESIGN: flat product list → categories → products → variants ("shelf");
     kill the A–M/N–Z split + 9/10 caps → pagination + search; text-blob stock
     views → tappable product cards edited in place.
   - ADD: named attributes/variants (flexible), SKU + barcode + type-to-find,
     reorder level + low-stock flags, inventory value + per-product movement
     history, stock-take/counter mode.
   - IMPROVE: set price + unit right on the product card; real category browsing.
   - REMOVE/SIMPLIFY: overlapping catalog sub-menus + text-heavy management split
     → one clean shelf + card model.

## What we are NOT doing here
- **Camera/photo barcode scanning** — noted, deferred (leans on vision; optional).
- The **Mini App** (in-chat web dashboard/grid) — later ceiling (Stage 7), after
  the catalog data model is settled.
- Per-contact statement PDF (Documents stage), returns/refunds, loyalty (post-launch).
