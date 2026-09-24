# Unit-Conversion Redesign — one canonical engine, built-in units, NL entry

_2026-09-23. Conversions are a HEARTBEAT feature (stock, cost/unit, recipes,
reports all depend on getting units right). Today's system is fragile and
confusing. This plan replaces it with ONE canonical-unit engine, a built-in
standard-unit library, multi-hop custom rules, and natural-language entry.
Separate, deliberate phase — like the money-precision and fractional-quantity
phases. WhatsApp must keep working. Fractional quantity (`utils/quantity.py`)
and kobo money (`utils/money.py`) already shipped and stay authoritative for
their concerns._

---

## Why (what's broken today — confirmed in code)

Three overlapping, drifted conversion mechanisms:

1. **Per-product taught rules** (`catalog._handle_set_conversion` /
   `_apply_conversion`): store `conversions["1 carton"] = {"qty":24,"unit":"pieces"}`.
   - Integer-only; `multiplier = to_qty // from_qty` → **floor division** (a
     `200 bags = 1 truck` rule computes `1 // 200 = 0` and silently adds nothing).
   - **One hop only**, and only "from → to" direction.
   - **Every rule overwrites `primary_unit`** to its right-hand side, so the last
     rule you type silently redefines the canonical stock unit.
2. **Two hard-coded metric tables** — `production.STANDARD_CONVERSIONS` and an
   inline copy in `catalog._get_standard_conversion_factor`. They have **already
   diverged** (one has `wh`, the other doesn't; tonne/gram plurals differ). And
   neither contains trade units (bag, carton, crate, truck, load).
3. **Legacy DB-layer conversions** (`database.convert_to_base/convert_from_base`)
   with a **different data shape** (`{"1 carton": "10 pairs"}`, str→str) that only
   feeds the AI prompt. Also `//`-truncated.

Plus: **no natural-language understanding.** A unit is recognised ONLY as the
leading token of a rigid `N unit` regex, AND only if taught per-product or present
in a metric table. "two bags", "a truckload", "2.5 kg of nylon" don't parse.
`primary_unit` is written from **7 sites** with inconsistent normalisation.

Net effect for the owner's real example (`1 bag = 20 pieces`, `200 bags = 1 truck`,
`1 truck = 4000 pieces`, `1 load = 5 bags`): primary unit ends up as whatever the
last rule's RHS was; each entry converts by a single uncoordinated hop; mismatched
units pile into one `stock` scalar; the `200 bags = 1 truck` rule adds 0.

---

## Target design

### 1. Canonical base unit per product
Each product has ONE **base unit** (the old `primary_unit`, kept for
back-compat). All stock and per-unit cost live in the base unit. Every other unit
the product uses is expressed as a **factor to the base**, computed once.

### 2. A unit GRAPH resolved to the base (multi-hop, bidirectional)
Store rules as edges `1 A = k B`. To get any unit's factor-to-base, walk the
graph (BFS) from that unit to the base, multiplying/dividing along the path.
- Owner example, base = **pieces**:
  `bag→20`, `truck→4000`, `load = 5 bags → 100 pieces`, and
  `200 bags = 1 truck` is **validated** (200×20 = 4000 ✓), not silently zeroed.
- Order of entry no longer matters; contradictions are detected (a rule that
  makes a unit resolve to two different factors is flagged at entry).

### 3. Built-in STANDARD unit library (ships with the bot)
One shared module `utils/units.py` with the universal, ratio-based systems the
user should NEVER have to teach:
- **Mass:** mg, g, kg, tonne (+ optional lb, oz)
- **Volume:** ml, cl, l/litre, gallon
- **Length:** mm, cm, m, km
- **Time:** min, hour, day, week
- **Energy:** wh, kwh
- **Count synonyms:** piece = pcs = pc = unit = units
Each system has an internal base (g, ml, m, min, wh, piece) with fixed factors.
Cross-system conversion is refused (kg→litre is meaningless → clear warning).
**Temperature is intentionally excluded** (offset math, not ratios; a stock/
finance bot buys quantities, not temperatures). The engine is ratio-only; temp
can be added later as a special case if a real need appears.
Custom per-product rules layer ON TOP of and can extend the standard library
(e.g. a product whose base is `kg` can still define `1 bag = 25 kg`).

### 4. Natural-language entry — deterministic parser first, LLM fallback
A **deterministic parser** = plain code (regex + the standard-unit lookup) that
always returns the same result for the same text, instantly, free, offline. It
handles the normal cases:
- Rules: `1 bag = 20 pieces`, `20 bags = 1 truck`, `a bag is 20 pieces`,
  `1 crate = 24 bottles`, fractional (`1 bag = 2.5 kg`).
- Quantities at record time: `2 bags`, `2.5 kg`, `three cartons` (small
  number-words), plural/singular, `pcs`/`pc`.
Only when the deterministic parser CAN'T resolve it do we fall back to the
**LLM** (one OpenAI call) to interpret messier phrasing, then re-validate the
LLM's output against the graph and echo it back for confirmation. Cheap and
predictable normally; flexible when needed.

### 5. One engine, all consumers
`utils/units.py` becomes the single source. `catalog.update_stock`,
`production._convert_to_stock_unit`, recipe-cost conversion
(`transactions._update_recipe_costs` + `production`), the categorizer prompt, and
the mini-app all call it. The two duplicated tables and the legacy DB-layer
converters are removed/redirected. Fractional-safe (via `utils/quantity`) and
kobo-safe (via `utils/money`) throughout.

### 6. Consistency & UX
- `primary_unit` is set ONCE (chosen explicitly, or inferred as the smallest/
  most-granular unit); secondary rules only add edges — they NEVER silently
  redefine the base.
- On first use of an unknown unit: **ask** for its conversion (accurate stock is
  the point) rather than silently recording a mismatched raw number.
- Rule entry validates against the graph and echoes: "Got it — 1 truck = 4,000
  pieces (via 200 bags). ✓" or "That conflicts with 1 bag = 20 pieces…".

---

## Data model (target)

Product dict gains a normalised, engine-owned block (old fields kept for
back-compat + auto-migrated on read):
```
"base_unit": "pieces",                 # canonical (mirrors legacy primary_unit)
"unit_defs": {                         # custom edges, factor TO base_unit
    "bag":   20,                       # 1 bag   = 20 pieces
    "truck": 4000,                     # 1 truck = 4000 pieces
    "load":  100                       # 1 load  = 100 pieces (5 bags, resolved)
}
```
- `unit_defs` values are precomputed factors-to-base (fraction-safe). Standard
  units (kg, litre…) are NOT stored here — the shared library provides them.
- **Migration:** on read, if a product still has the old `conversions`
  (`{"1 carton":{"qty":24,"unit":...}}` or the legacy str→str), convert it into
  `unit_defs` once and persist. No bulk migration script; upgrade-on-touch. Old
  whole-number data stays valid.

---

## Order of work (small, verified commits — each compiles + check_syntax)

1. **`utils/units.py`** — standard-unit library (systems + factors), the graph
   resolver (`factor_to_base`, multi-hop BFS, contradiction detection), the
   deterministic rule/quantity parser, and a `convert(qty, from_unit, to_base)`
   API. Fraction-safe. Unit tests: metric round-trips, multi-hop
   (load→bag→pieces), contradiction, cross-system refusal, fractional.
2. **Migration shim** — read-time upgrade of legacy `conversions` (both shapes)
   → `unit_defs`; keep `primary_unit`↔`base_unit` in sync. Non-destructive.
3. **catalog** — `update_stock` + `_apply_conversion` + `_get_standard_conversion_factor`
   replaced by `utils.units`; `_handle_set_conversion` becomes NL rule entry
   (deterministic parser + validate + echo); stop overwriting base on each rule.
4. **production / recipe cost** — `_convert_to_stock_unit`, recipe-cost
   conversion, and `transactions._update_recipe_costs` call `utils.units`; drop
   the cross-import of `ProductionHandler.STANDARD_CONVERSIONS`.
5. **database legacy converters** — redirect `convert_to_base`/`convert_from_base`/
   `get_catalog_for_ai` unit serialization to `utils.units` (or retire).
6. **mini-app** — catalog edit can define/read `unit_defs` (web parity for units);
   `set_unit` sets base without clobbering rules. UTF-8/surrogate scan.
7. **LLM fallback** — only when the deterministic parser fails: one categorizer/
   OpenAI call to interpret a unit phrase, re-validated against the graph.
8. **E2E + docs** — owner's bag/truck/load scenario as the acceptance test;
   recipe with kg↔g material; sale in a custom unit; py_compile + check_syntax +
   UTF-8; update roadmap + project steering; hand deploy command.

## Guardrails
- One shared engine; delete the duplicated/divergent tables.
- Ratio-only (no temperature). Cross-system conversion refused with a clear msg.
- Fraction-safe (`utils/quantity`) + kobo-safe (`utils/money`); never `//` a unit
  factor, never `int()` a converted cost.
- `base_unit` set once; rules only add edges.
- No bulk data migration; upgrade legacy conversions on read.
- WhatsApp path keeps working; deterministic parser first, LLM only as fallback.
- Compile + check_syntax + mini-app UTF-8/surrogate scan each step.

## Acceptance test (the owner's scenario)
Base = pieces. Teach in ANY order: `1 bag = 20 pieces`, `200 bags = 1 truck`,
`1 truck = 4000 pieces`, `1 load = 5 bags`. Then:
- `bag`→20, `truck`→4000, `load`→100 (all in pieces); order-independent.
- `200 bags = 1 truck` validates (no silent 0).
- Record `2 trucks` → stock +8,000 pieces; `3 bags` → +60; `1 load` → +100.
- Sell `1.5 bags` → −30 pieces (fractional-safe); per-unit cost stays kobo-precise.
- `1 kg = 1000 g` works with NO teaching (built-in); `kg → litre` refused.

---

## ✅ PROGRESS LOG — phase COMPLETE (2026-09-23)

All 8 tasks done, verified, committed, and pushed to `origin/master`. **Not
deployed by the agent** — owner runs the deploy (below).

### What shipped (commits, in order)
1. **`d28268a`** — `utils/units.py` (the engine) + this plan. Standard library
   (`_SYSTEMS`: mass/volume/length/time/energy/count — ratio-only, NO temp),
   `factor_to_base` (self→custom `unit_defs`→standard, same-system),
   `standard_factor` (None on cross-system → refused), `convert(qty,from,base,
   defs)->(qty,ok)`, `build_unit_defs(base, edges)->(defs, conflicts)` (multi-hop
   relaxation, both directions, >0.5% contradiction detection),
   `parse_rule`/`parse_quantity` (deterministic, plural + number-words).
2. **`8a4d4f6`** — read-time migration: `upgrade_product_units(product)` +
   `product_units(product)` upgrade BOTH legacy shapes (Model-A dict values and
   Model-B str values) to `base_unit` + `unit_defs`; idempotent, non-destructive,
   base inferred from the rules' RHS. No bulk script.
3. **`3a9ad53`** — catalog: `update_stock` conversion now one engine call;
   `_handle_set_conversion` is natural-language rule entry that stores RAW edges
   (`unit_edges`) and rebuilds `unit_defs` each time (order-independent multi-hop),
   rejects contradictions, echoes the resolved factor, and NEVER clobbers the base
   unit. `_apply_conversion`/`_get_standard_conversion_factor` kept as thin shims
   over the engine (a stock-take caller still uses one).
4. **`77aab1d`** — production + recipe cost use the engine; DELETED the duplicated
   `ProductionHandler.STANDARD_CONVERSIONS` table and the cross-import in
   transactions. Recipe cost/recipe_unit = cost/base × factor_to_base(recipe_unit,
   base). Added `drum` to the volume system.
5. **`6d96349`** — `get_catalog_for_ai` serializes `base=… , 1 bag=20 pieces` for
   the LLM prompt; RETIRED the dead legacy converters (`convert_to_base`,
   `convert_from_base`, `get_conversions_for_product` — no callers, `//`-truncation);
   `set_primary_unit` now also sets `base_unit`.
6. **`307d615`** — mini-app parity: server `set_unit` (base-safe, no rule clobber)
   + new `set_conversion` action (same engine); `_row_from_product` exposes
   `base_unit`+`unit_defs`; edit sheet shows current rules and an "Add" box to
   teach a new one. UTF-8/surrogate scan clean.
7. **`4c5467d`** — `parse_rule_llm`: deterministic-first, ONE gpt-4o-mini call ONLY
   when the parser fails, fully defensive (no key/network/bad-json → None), result
   still validated by `build_unit_defs`. Wired into chat + web rule entry.

### The old system (replaced)
Three drifted mechanisms are gone/unified: per-product `//`-truncating one-hop
rules that overwrote the base unit; TWO duplicated + diverged `STANDARD_CONVERSIONS`
tables; and a dead DB-layer str→str converter that fed the AI. One engine now.

### Verified (acceptance E2E, all green — throwaway harness, deleted)
Owner's scenario through the REAL `CatalogHandler`/`ProductionHandler`:
- Rules taught in messy order (`1 load = 5 bags` before `1 bag = 20 pieces`):
  `bag→20`, `truck→4000`, `load→100` — order-independent multi-hop.
- `200 bags == 1 truck` consistent (no silent 0 from the old `//`).
- Record `2 trucks`→8,000; `+3 bags`→8,060; `+1 load`→8,160; `−1.5 bags`→8,130
  (fractional). Cost `₦400/bag → ₦20/piece` (kobo-precise, single rescale).
- Contradiction `1 bag = 25 pieces` rejected. Standard `500 g → 0.5 kg` with NO
  teaching. Recipe `250 g → 0.25 kg`. `kg → litre` refused (cross-system warn).
- Legacy product (old `conversions`) upgrades on first touch.
`py_compile` + `check_syntax.py` green each step; mini-app UTF-8/surrogate scan = 0.

### Answers to the owner's questions (captured)
- **Deterministic parser** = plain code (regex + lookup), same result every time,
  free/instant/offline. LLM is the fallback only when that fails.
- **Standard units ship built-in** (weight/volume/length/time/energy/count) — the
  user never teaches `1 kg = 1000 g`. Only business-specific units (bag, carton,
  truck) are taught. **Temperature intentionally excluded** (offset math, not a
  ratio; a stock/finance bot doesn't buy temperatures).

### DEPLOY (owner runs — agent does not deploy)
Engine + mini-app JS only; no `template.yaml` change in this phase.
```
cd ~/projects/kashia-bot
./deploy.sh dev
```
(No `set_telegram_commands.sh` — the command menu did not change.)

---

## 🐞 POST-SHIP FIX + DEPLOY (2026-09-24)

**Mini-app showed "Loading…" / no data after the units work.** Investigated:
- Confirmed the server was healthy — `_summary` and `_inventory` both return 200
  and encode cleanly (probed with a mocked DB; all imports load fine).
- Root cause of the units NOT showing in the web: `catalog.normalize_product`
  only copies keys in `_PRODUCT_DEFAULTS`, so the new `base_unit` / `unit_defs` /
  `unit_edges` were **stripped** on the inventory read path (the web lost custom
  units; `_row_from_product` then saw an empty product for unit purposes).
- **Fix (`d1678c9`):** added `base_unit`, `unit_defs`, `unit_edges` to
  `_PRODUCT_DEFAULTS` so they survive normalization. Verified: inventory now
  returns Cement `base=pieces, defs={bag,truck}` and legacy `carton=24` upgrades.
- The broader "Loading…" was consistent with a stale/incomplete deploy of the
  units+money batch. **Deployed** `./deploy.sh dev` — build `20260924105552`,
  "Successfully created/updated stack", all 8 Lambdas updated including
  MiniAppFunction. The app now serves the current engine.

If the app still shows "Loading…" after this: it's the documented Telegram
WebView cache / expired-init case — close and reopen the mini app from the ☰
Menu button (the HMAC init data is re-issued on reopen).
