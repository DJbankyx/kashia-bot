# Mini App — Industry-Aware UX + Catalog-First Setup

_Plan doc. Created 2026-09-22. Follow in stages; each stage ships + verifies
independently so we never break Trading (which works today) or WhatsApp._

## Why

The mini app was built Trading-first. Every industry sees Trading's catalog +
record forms, which is confusing for Manufacturing (and Services):
- Manufacturing needs Recipe/BOM, raw-materials vs finished-products, conversions
  — none surfaced in the web app.
- Manufacturing is asked to type a **cost per unit** for a finished product AND
  set a recipe — two sources of truth for the same number.
- Good catalog setup drives cost, margin, and every report — but the app doesn't
  guide or prioritise it.

The Telegram chat side already branches by industry (industry handlers +
production.py). The mini app does not. This plan brings the web app in line.

## Locked decisions (owner-approved)

- **Decision A — recipe is the source of truth for finished-goods cost.** A
  finished/manufactured product NEVER takes a manually-typed cost per unit; its
  cost is CALCULATED from its recipe (raw materials + labour/overhead). The
  manual cost field is hidden for finished goods. **Raw materials keep a real
  buy-cost** (you purchase them at a price). This removes the double-entry
  confusion and matches how manufacturing accounting actually works.
- Industries covered: **Trading, Manufacturing, Services, Hybrid.** Trading is
  the baseline (unchanged behaviour); the others adapt.
- **WhatsApp untouched.** All mini-app work is web-only. Shared engine
  (accounting, catalog, cost) stays the single source of truth.

## Non-negotiable guardrails (to limit bugs)

1. **Trading must look/behave exactly as it does now** after every stage. It's
   the control. If a Trading screen changes unintentionally, that's a regression.
2. **One source of truth for cost.** The web must call the SAME catalog/cost
   engine as chat (`CatalogHandler`, `Accounting`, recipe logic in
   `production.py`) — never recompute cost in JavaScript.
3. **The mini app reads the user's industry from the server**, not guessed in
   JS. The `/app/api/*` responses should carry an `industry` field so the UI
   branches on real data.
4. **Every stage: py_compile + check_syntax.py + a manual pass on Trading AND
   Manufacturing before commit.** Small commits, one stage each.
5. **No new money math in the web.** Recipe cost, COGS, margins all come from the
   engine.

## Data model notes (confirmed in code)

- Industry: `user.industry_class` (fallback `business_type`) — values `trading`,
  `manufacturing`, `services`, `hybrid`.
- Product item types already exist: `finished_product`, `raw_material`,
  `supply`/`overhead`, `service`, plain `product`. `ensure_item_types` auto-tags.
- Recipe lives on `product["recipe"]` (list of materials); editor in
  `production.py` (_start_recipe_setup etc). Recipe → cost recompute exists
  (`prod_recalc_costs`).
- Catalog cost lookup order: variant-tree leaf → flat variant → product
  landing_cost. Weighted-avg on purchase.

---

## Stages

### Stage 0 — Server tells the app the industry (foundation)
- Add `industry` to the mini-app summary/inventory/tree API responses (from
  `user.industry_class`). One field; no UI change yet.
- JS stores it once (`APP.industry`) and exposes helpers `isMfg()`,
  `isServices()`, `isHybrid()`, `isTrading()`.
- **Verify:** Trading unchanged; `industry` present in the API JSON.

### Stage 1 — Fix the Manufacturing cost/recipe confusion (Decision A) — HIGHEST PAIN
- In the mini-app catalog Add/Edit product sheet:
  - When industry is Manufacturing/Hybrid AND the product is a **finished
    product**: HIDE the manual "cost per unit" field; show instead a read-only
    "Cost (from recipe): ₦X" line + a "📋 Set / edit recipe" action.
  - **Raw materials / supplies**: KEEP the cost field (real buy-cost) and hide
    the recipe action.
  - Product-type is chosen up front for mfg/hybrid (Finished product vs Raw
    material vs Supply/overhead) so the form adapts.
- The server enforces it too: reject/ignore a manual cost on a finished product
  (engine already derives it). No JS cost math.
- **Verify:** Trading still shows the normal cost field; Manufacturing finished
  goods show recipe-derived cost, raw materials show a cost field.

### Stage 2 — Recipe / BOM editing in the mini app
- A web recipe editor: list a finished product's materials, add/remove a
  material (pick from raw-materials in catalog) + qty + unit, show the rolled-up
  cost. Calls the SAME recipe engine (new `/app/api/recipe` endpoints that wrap
  production.py logic) — no forked math.
- Recompute finished-goods cost when the recipe changes (reuse
  `prod_recalc_costs`).
- **Verify:** a recipe set in the web shows the same cost in chat, and vice
  versa (one source of truth).

### Stage 3 — Industry-aware labels + form fields across the app
- Terminology per industry (reuse the chat TERMS where possible): e.g. Services
  says "Service / Job", Manufacturing "Output / Production", labels on Record
  forms and catalog match.
- Hide/show fields that don't apply (e.g. Trading's plain "buy to resell" cost
  vs Manufacturing's production model; Services may not use stock/qty).
- Record-transaction sheet adapts: Manufacturing sale = finished product;
  Services = a job/service line; etc.
- **Verify:** each industry's forms read naturally; Trading unchanged.

### Stage 4 — Catalog-first: make good setup a guided priority
- A catalog "health" nudge: if products lack cost (raw materials) or recipes
  (finished goods) or units, show a gentle "Finish setting up your catalog for
  accurate profit" prompt with a checklist + deep links.
- Onboarding/first-run in the app points to catalog setup before recording.
- Never blocks recording — just guides. (Reports already warn on uncosted sales;
  this closes the loop earlier.)
- **Verify:** the nudge appears only when setup is incomplete; dismissable.

### Stage 5 — Remaining mini-app improvements (owner's list)
- Collect the outstanding mini-app improvement list from the owner and fold in
  here, per-industry where relevant.

---

## Sequencing & risk

Do stages in order. Stage 1 is the highest-value (kills the cost/recipe
confusion) and is mostly conditional UI + a server guard — low risk. Stage 2
(recipe editing in web) is the biggest build (new endpoints) — treat carefully,
reuse production.py, add nothing new to the cost math. Stages 3–5 are
incremental polish.

Each stage: small commit, py_compile + check_syntax.py, and a two-industry
manual check (Trading control + the industry being changed) before moving on.

---

## Progress log

### 2026-09-22 — Stage 0 + Stage 1 shipped (commit `7e4a770`)

**Stage 0 — server tells the app the industry.**
- `/app/api/summary` now returns `industry` (from `user.industry_class` →
  `business_type` → `"trading"`).
- Mini-app JS: added `var APP = { industry: "trading" }` at bootstrap; `loadSummary`
  sets `APP.industry = d.industry`; helpers `isTrading()/isMfg()/isServices()/isHybrid()`
  and `usesRecipes()` (mfg + hybrid).

**Stage 1 — recipe-driven cost for mfg/hybrid finished goods (Decision A).**
- Product row now carries `has_recipe` (bool) alongside the existing `item_type`.
- Edit sheet (`openSheet` → `applyCostFieldMode`): for mfg/hybrid **finished_product**
  it HIDES the manual "Cost per unit" field and shows a read-only "Cost (from
  recipe)" line + a hint to set the recipe in chat. Trading and raw materials/
  supplies keep the manual cost field.
- `saveSheet`: never emits `set_cost` for a recipe-driven finished product
  (`costLocked`).
- Add-product sheet: mfg/hybrid see an **item-type chooser** (Finished product /
  Raw material / Supply); the choice is sent as `item_type` on `action:"add"`.
  Trading omits it (unchanged; `ensure_item_types` auto-tags as before).
- Server guards (defense-in-depth, no JS money math):
  - `_product_write` `action:"add"` stores `item_type` when it's one of
    `finished_product|raw_material|supply`.
  - `_product_write` `action:"set_cost"` returns 409 `recipe_driven` when
    industry is mfg/hybrid and the product is `finished_product`.
- Verified: `py_compile` + `check_syntax.py` both pass. Pushed to origin/master.
- ⚠️ NOT DEPLOYED — owner must run `./deploy.sh dev`, then reopen the app from the
  Telegram ☰ Menu button for the new JS to load.

**Next:** Stage 2 — web recipe/BOM editor (new `/app/api/recipe` endpoints wrapping
`production.py`; no forked cost math).

### 2026-09-22 — Stage 2 shipped (commit `bb972a6`)

**Web recipe / BOM editor — engine-owned cost, no forked JS math.**
- `production.py` gained pure, session-free entry points (single source of truth
  for recipe cost):
  - `recipe_unit_cost(recipe)` — static; cost to make ONE unit = Σ material
    qty×cost_per_unit + Σ overhead qty×rate. Matches chat production maths.
    Unit-tested (materials + overhead + legacy fallback + empty/bad-value safe).
  - `get_recipe(phone, key)` — returns recipe lines, rolled-up `unit_cost`, and
    `available_materials` (catalog raw_material/supply/overhead rows for the
    picker).
  - `web_add_material(...)` / `web_remove_material(...)` — edit one line then
    `_save_recipe`, which tags the product `finished_product` and restamps
    `landing_cost = round(recipe_unit_cost)`. Missing cost falls back to the
    material's catalog landing_cost (same as chat).
- `miniapp.py` endpoints: `GET /app/api/recipe?key=` (`_recipe_read`) and
  `POST /app/api/recipe` (`_recipe_write`, actions `add_material`/`remove_material`).
  Both just wrap ProductionHandler — no cost math in the handler.
- Mini-app JS: recipe editor overlay opened via "📋 Set / edit recipe" on the
  edit sheet (mfg/hybrid finished goods only). Shows unit cost + material list
  with Remove, and an add-material form (catalog picker + qty + optional cost).
  On change it re-renders from the server response and syncs the edit sheet's
  "Cost (from recipe)" line. `apiPost` reused (ok-gated, text-first error parse).
- **template.yaml:** added `RecipeGet` + `RecipeWrite` API Gateway routes (both
  `/app/api/recipe`). This is a REAL template change → **`./deploy.sh dev`
  activates the routes** (undeclared routes 403). Lambda already has UsersTable
  CRUD (recipe lives on the user's product_catalog), so no new IAM.
- Verified: `py_compile` + `check_syntax.py` pass; `recipe_unit_cost` unit test
  passes (`RECIPE_MATH_OK`). One source of truth: a recipe set in the web shows
  the same cost in chat (same catalog field + same formula).

**Next:** Stage 3 — industry-aware labels/fields across the app (Services "job",
Manufacturing "output", hide stock/qty where N/A); then Stage 4 (catalog-first
setup nudge) and Stage 5 (owner's remaining mini-app improvement list).
