# Kashia — Telegram Elevation Master Plan

_Created 2026-09-03. The authoritative plan for turning the Telegram bot into a
rich, app-like experience — beyond WhatsApp's limits — without breaking WhatsApp._

## Guiding principles (non-negotiable)
1. **One engine, two platforms.** Never fork business logic (save, stock, margin,
   CRM math, PDF generation). Reuse it; only change presentation/collection.
2. **WhatsApp stays working.** Every Telegram-specific change is gated by the
   `tg:` user-id namespace (`platform_for_user`). WhatsApp behaves byte-for-byte
   as before unless we deliberately, separately improve it.
3. **Incremental, always shippable.** One feature area at a time. Each ships,
   gets live-tested, and is committed before the next begins. No big-bang.
4. **Safest-first ordering.** Prove the pattern on low-risk, self-contained
   features before the deep, money-adjacent ones (catalog).
5. **Backward-compatible data.** Any data-model change layers on top of existing
   data; old records keep working; migrations are lazy and reversible.
6. **Verify every step.** Compile + logic checks with mocks before deploy; the
   user live-tests the real UX after each deploy (mocks can't prove feel).

## Change-type legend
- **[UI]** presentation only (Telegram layer; WhatsApp untouched).
- **[EXTEND]** engine gains a new, additive capability (WhatsApp ignores it).
- **[DE-BIAS]** remove a WhatsApp assumption from shared logic (careful refactor).

## Reusable foundations already built (this session)
- MessagingClient abstraction; independent WhatsApp + Telegram bots on one engine.
- Telegram: command menu, typing indicator, grid keyboards, **message editing +
  paginated pickers** (`edit_message_text`, `send_and_get_id`, `page_keyboard`),
  recurring reminders, voice transcription, receipt scanning (Phase A).
- `utils/tg_ui.py` fast-entry keyboard builders (`__tgfx__` namespace).
- `tg_fastentry.py` sale/purchase fast-entry pattern (the template for all
  tap-first flows) → assembles `tx_data`, converges on the engine's
  `_build_confirmation` → save chain unchanged.
- `main._deliver_engine_responses` shared send pipeline.
- Root cause of all WhatsApp shaping: `utils/whatsapp_ui.py` `buttons[:3]` and the
  `[:9]/[:10]` row slices scattered across handlers.

---

## BUILD SEQUENCE

### Stage 0 — Stabilize the pilot (IN PROGRESS)
- [ ] User live-tests the SALE fast-entry flow; fix any bugs found.
- [ ] Confirm the edit-in-place UX + hand-off to the confirmation card feel right.
- [ ] Extend the proven pattern to PURCHASE and EXPENSE fast-entry.
Gate: nothing below starts until the fast-entry pattern is validated live.

### Stage 1 — CRM / Contacts  (warm-up; self-contained; low risk)
Why first: contacts barely touch money-critical logic, so smallest blast radius,
and it proves the "rich card edited in place" design on safe ground.
- [ ] **[UI]** Paginated, browsable contact list (reuse `page_keyboard`).
- [ ] **[UI]** Rich contact CARD edited in place: financials, timeline, debt,
      with a full action fan-out (Remind · Record sale · History · Edit ·
      Statement) — no 3-button cap.
- [ ] **[UI]** Actionable reminders: one tappable "Remind" per debtor.
- [ ] **[EXTEND]** In-UI contact search (engine: `search_contacts` intent).
- [ ] **[EXTEND]** Edit-contact flow (engine: contact-update path; name/phone/
      type/notes editable).
Reuse: `db.get_contacts/get_contact_analytics/get_top_contacts/get_all_debtors`.
Verify + deploy + user test before Stage 2.

### Stage 2 — Product Catalog  ★ FLAGSHIP ★  (the "shelf & counter")
The original ambitious vision, restored for Telegram. This is the deepest change
(data model + money-adjacent stock), so it gets the most care and its own
sub-plan below. Built AFTER CRM proves the pattern.

### Stage 3 — Reports & Dashboard  (highest-value addition)
- [ ] **[UI]** Live dashboard edited in place: tappable Today/Week/Month toggles.
- [ ] **[UI]** Drill-downs (tap Sales → breakdown; tap a category → detail).
- [ ] **[EXTEND]** Chart images (profit trend, top products) generated server-side
      and sent as photos; later a Mini App for full interactivity.

### Stage 4 — Documents (Invoice / Receipt / Quote)
- [ ] **[UI]** Clean inline delivery (drop the raw-link noise); paginated pickers.
- [ ] **[EXTEND]** Interactive invoice builder: add item / edit qty / remove /
      preview / generate (engine: invoice-DRAFT state + intent).
- [ ] **[EXTEND]** Real PDF quotes (reuse invoice rendering) vs the text blob.

### Stage 5 — Debt / Credit board
- [ ] **[UI]** Interactive "who owes me" board: tap-to-remind, log partial
      payment, aging buckets (30/60/90).

### Stage 6 — Onboarding polish (Telegram-native)
- [ ] **[UI]** Tap-first onboarding: industry grid, guided first-product card.

### Stage 7 — Mini App (the ceiling; later, bigger lift)
- [ ] In-chat web view for a full inventory grid + dashboard/charts. Essentially
      a small web app talking to the same engine/data. Revisit once Stages 1–5
      are live and the data model (esp. catalog) is settled.

---

## ★ STAGE 2 SUB-PLAN — The "Shelf & Counter" Catalog

### Vision
A real inventory system: browsable shelves by category, searchable, deep product
cards with variants/attributes, live stock intelligence, and fast counter ops —
all tap-first and edited in place. Restores the design abandoned for WhatsApp
(catalog.py docstring: "No deep tree. No 5-level navigation.").

### Current state (must preserve/migrate)
- Flat dict keyed by product_key. Fields: name, stock, landing_cost, category,
  variants[list], variant_stock{}, variant_costs{}, item_type, primary_unit,
  conversions, recipe, supplies_used. Shallow variant layer only.
- Feeds stock deduction on EVERY sale → money-adjacent → change carefully.
- WhatsApp caps: "9 + Other" recording picker; A–M/N–Z management split;
  10-row menu; text-blob stock/detail views.

### Target data model (backward-compatible, additive)
- Keep the flat product dict as the base (old products keep working untouched).
- Layer on, all OPTIONAL with safe defaults:
  - `category` promoted to a first-class navigable dimension (already stored).
  - `attributes` (structured variant axes, e.g. {size:[...], color:[...]}) —
    generalizes the current flat `variants` list; old `variants` auto-read as a
    single axis.
  - `reorder_level`, `supplier`, `sku`/barcode (optional).
  - `stock_movements` (append-only log: +/- qty, reason, date, tx_id) for history
    — additive; absence just means "no history yet."
- Migration is LAZY: a product without the new fields is treated with defaults;
  we never bulk-rewrite. A read helper normalizes old→new shape in memory.

### Telegram UX (all [UI] on top of the new model)
1. **Shelf view**: categories as a grid → tap a category → paginated product
   grid → tap a product → its card. Search box (type to filter) at the shelf.
2. **Product CARD (edited in place)**: name, category, stock (with tap −/+ and
   a "set exact" option), cost, price, margin %, variants/attributes, unit +
   conversions, reorder flag, supplier. Every field editable via inline taps —
   no "pick product → type value" round trips.
3. **Variants/attributes drill-down**: Brand → Model → Color → Size handled by
   drilling within one edited message, with breadcrumb + back.
4. **Stock intelligence**: low-stock 🔴 highlighting, reorder-soon flags, total
   stock value, per-product movement history (from `stock_movements`).
5. **Counter mode**: quick stock-take / bulk adjust.

### Engine work required (the root part)
- **[DE-BIAS]** Remove the `[:9]/[:10]` caps from the recording/management paths
  for Telegram (WhatsApp keeps them via the platform guard). The list builders
  return full data; the Telegram layer paginates.
- **[EXTEND]** `search_products(query)` and `get_products(category=...)` intents.
- **[EXTEND]** `record_stock_movement()` + reorder-level checks (feeds Stage 3
  low-stock alerts and the movement history).
- **[EXTEND]** A normalized product read-model (old flat ↔ new attributes) so both
  shapes coexist.
- REUSE untouched: all save/adjust/cost math, `ensure_item_types`,
  `_find_product_key`, stock deduction on sale (the money path).

### Stage 2 execution order (each verified before the next)
1. Additive data model + normalization read-helper + lazy migration. (no UX yet)
2. Shelf browse + search + paginated grid (read-only). Deploy + test.
3. Product card view (read-only, rich). Deploy + test.
4. In-place editing: stock −/+, cost, price, unit. Deploy + test.
5. Variants/attributes drill-down + editing. Deploy + test.
6. Stock intelligence: reorder flags, value, movement history. Deploy + test.
7. Counter mode / bulk ops. Deploy + test.
Rollback safety: each sub-step is behind the `tg:` guard and additive; WhatsApp's
catalog is never touched; old product records always render.

---

## Risk notes
- Catalog stock feeds money (sale deductions). Any model change ships behind the
  `tg:` guard and additive fields, verified against the existing save path.
- Mini App is a separate runtime (web view) — largest effort; deliberately last.
- "Rich card edited in place" is the shared design language across CRM, Catalog,
  Debt — proving it in CRM (Stage 1) de-risks everything after.
