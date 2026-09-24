# Money-Precision Phase — kobo-accurate money (2dp), engine-wide

_2026-09-23. Make MONEY kobo-precise (2 decimal places) so sub-naira per-unit
costs (electricity ₦0.06/kWh, a chemical ₦0.5/ml) are correct. Separate from the
shipped fractional-QUANTITY phase (that's done; don't touch `_as_num`/`_to_num`/
`to_qty`). WhatsApp must keep working. Owner's acceptance case: an electricity
recipe input with a sub-naira per-unit rate rolls up correctly._

## Decision: store money as Decimal NAIRA, quantized to 2dp at write

Why (grounded in this codebase):
- **DynamoDB already stores numbers as Decimal**, and `database._sanitize_for_dynamo`
  already emits `Decimal(str(x))` on write — so 0.06 round-trips faithfully. The
  precision loss is 100% from explicit `int(...)`/`//` in app code, never the DB.
- **`whatsapp_ui.format_amount` already renders decimals** (sub-naira up to 4dp,
  decimals 2dp, whole clean) — zero display migration for chat/Telegram.
- **`parse_amount` already returns naira floats** (0.06, 1_500_000) — no ×100.
- **No data migration**: existing integer-naira values are valid 2dp already.
  Integer-kobo would require ×100 of every historical money row — rejected.

Rules:
- Construct Decimals from **str** (`Decimal(str(x))`), never from a float.
- **Math stays unrounded Decimal** (weighted-avg, COGS = unit×qty, aggregates).
  Never round mid-calc — today's `int()` inside weighted-avg compounds error.
- **Quantize to `Decimal('0.01')` exactly once, at the storage boundary.**
- Paystack stays integer kobo (authored constants; convert only at its boundary).

## Shared helper (new): `utils/money.py`
- `to_money(v) -> Decimal` — safe parse (str/int/float/Decimal/None), via str.
- `money_round(v) -> Decimal` — quantize to 2dp (kobo), ROUND_HALF_UP.
- `money_num(v)` — return int when whole (so JSON/display stays "3500" not
  "3500.00"), else a 2dp float/Decimal — for the DB write + JSON.
- `fmt_money(v) -> str` — ₦ formatter (mirrors format_amount; single source).
Unit-test rounding: 0.1+0.2==0.30, 3-way split of 10.00, weighted-avg of odd kobo.

## The map (money-only; quantity already done)

### STORAGE (persisted — precision lost here today)
- **database.save_transaction**: `int(amount)`, `int(unit_cost)`,
  subtotal/discount/tax `int(...)`. → money_round.
- **database.update_contact_totals / record_debt / settle_debt**: `int(amount)`,
  debt balances int (server-side ADD). → money_round; confirm boto3 marshals
  Decimal for ADD (it does, as N).
- **catalog.set_cost_direct** `int(cost)`, **set_sale_price** `int(price)`,
  **update_stock weighted-avg** (base/variant/leaf) `int((old*qty+new*qty)/tot)`,
  **cost_history** cost, set_price/cost-fix handlers, variant-tree edit `int(val)`.
  → keep math Decimal, money_round at the store.
- **transactions**: `_save_transaction` recipe COGS `int(product_cost)*qty`;
  purchase `unit_cost = amt // qty` (floor!); `_apply_stock_for_tx` `amt // qty`;
  `_stamp_sale_cost`/`restamp_sale_cost` int; landing-cost sale flow `//`;
  returns COGS copy. → Decimal math, money_round at store; `//` → `/` then round.
- **miniapp**: `_transaction_write` `int(amount)`; `_product_write` money actions
  `int(value)`; `_debt_payment_write` `int(amount)`. → to_money + money_round.

### MATH (rounds each line to int today — compounds loss)
- **accounting._to_int** is the money coercer used across period_pnl /
  period_cashflow / position / _value_product / _value_tree / cogs_for_sale /
  resolve_sale_cost_now. Keep a money-precise read (to_money) and only round the
  final displayed figure (or keep 2dp through). `_to_num` (quantity) stays.

### DISPLAY (already decimal-aware — confirm, light touches)
- **whatsapp_ui.format_amount** — already correct; make `fmt_money` delegate to
  the same logic (or reuse it) so there's one formatter.
- **tg_ui** money label `f"₦{amount:,}"` / `//1000k` — int-assuming; light touch.
- **pdf_generator** invoice/receipt/statement — `int(...)` + `{:,}`; unit price
  `item_amount // qty`. → 2dp money format so documents show kobo.
- **miniapp JS `naira()`** — `Number(n).toLocaleString("en-NG")` truncates? No —
  toLocaleString shows decimals; but inputs use `parseInt` for price/cost/amount/
  deposit/leaf-cost → switch to parseFloat + step=any (recipe cost already is).
- **export.py** Excel money cells — 2dp format.

## Order of work (verify each; small commits)
1. `utils/money.py` + unit tests.  ← smallest blast radius
2. database.py storage boundary (save_transaction, debt, contact totals,
   _sanitize_for_dynamo passes Decimal through).
3. catalog.py money (set_cost/set_price/weighted-avg/leaf/cost_history/get).
4. transactions.py money (amt/qty unit cost, COGS stamp, restamp, returns).
5. accounting.py money reads/aggregates precise.
6. mini-app server money + JS inputs (parseFloat) + recipe roll-up display.
7. Display: pdf_generator, export, tg_ui, confirm format_amount; Paystack verify.
8. E2E: electricity sub-naira recipe rolls up; P&L round-trip; rounding tests;
   compile + check_syntax + UTF-8; spot-check WhatsApp + a PDF.

## Guardrails
- Quantity/stock already done — do NOT change `_as_num`/`_to_num`/`to_qty`.
- Decimal from str only; quantize once at storage; never mid-math.
- No historical data migration (whole-naira rows are valid).
- Paystack kobo boundary untouched.
- Compile + check_syntax + UTF-8/surrogate scan (mini-app) each step.

---

## ✅ PROGRESS LOG — phase COMPLETE (2026-09-23)

All 8 tasks done, verified, committed, and pushed to `origin/master`. **Not yet
deployed by the agent** — owner runs the deploy (see below).

### What shipped (commits, in order)
1. **`d7a2582`** — `utils/money.py` + this plan doc. `to_money()` (Decimal from
   str, strips ₦/commas, bool-guarded), `money_round()` (→ **int when whole**,
   else 2dp float — the DB sanitizer then stores it losslessly), `money_is_whole`,
   `fmt_money`. Note: the shipped helper is `money_round` (not `money_num`); it
   returns the JSON/DB-friendly int-or-float directly.
2. **`a6042aa`** — database storage boundary: `save_transaction` amount/unit_cost/
   subtotal/discount/tax, `update_contact_totals`, `record_debt`/`settle_debt`
   balances → `money_round`. Added module-level `_money_expr()` (int-or-Decimal,
   boto3-safe) for the server-side `ADD` expression values (boto3 rejects raw
   float in `ExpressionAttributeValue`).
3. **`72da5ba`** — catalog: `set_cost_direct`/`set_sale_price`/`get_landing_cost`/
   leaf cost + `update_stock` weighted-avg (base/variant/leaf, computed in Decimal
   then rounded once) + conversion-adjust + `cost_history` + chat set-cost &
   cost-fix handlers + variant-tree leaf edit → `money_round`/`to_money`.
4. **`fdcc24e`** — transactions: recipe COGS stamp, purchase `unit_cost` +
   `_apply_stock_for_tx` (`amt/qty` in Decimal, not `//`), `_stamp_sale_cost`,
   `restamp_sale_cost` (+ nested extra_details), returns COGS pro-rate, landing-
   cost sale flow (`//` → Decimal `/`), multi-item item_amount, credit-path
   call sites (removed pre-truncating `int()`), sale margin.
5. **`195ceb8`** — accounting: `_to_int` **redefined** to be kobo-precise (calls
   `money_round`) rather than renaming every call site — it's money-only in
   accounting (quantity uses `_to_num`). COGS/`resolve_sale_cost_now`/`_value_*`
   line calcs use `to_money` math, rounded once. (Verified `100 × 0.06 = 6`.)
6. **`baada9d`** — mini-app: server `_transaction_write`/`_product_write` (set_
   price/set_cost, leaf_cost)/`_debt_payment_write` amounts → `money_round`; JS
   `parseInt`→`parseFloat` for pay/rec/sheet/leaf money inputs; 7 money `<input>`
   fields → `inputmode="decimal" step="any"`. UTF-8/surrogate scan clean (0).
7. **`66ca2e4`** — display surfaces: `pdf_generator.py` + `export_service.py` got
   a module-level `_m()` (kobo-precise, symbol-less) and every money `int()`/`{:,}`/
   `item_amount // qty` replaced with `to_money`/`_m`/`money_round`. Excel money
   format `#,##0` → `#,##0.##` (kobo only when present). `tg_ui` reviewed — only
   formats integer **presets**, no stored-money display, left as-is.
   `whatsapp_ui.format_amount` confirmed already decimal-aware, unchanged.
8. **`e0f25e4`** — recipe/production (the acceptance case). Fixed two truncations
   the earlier map missed:
   - `production._save_recipe`: `landing_cost = int(round(recipe_unit_cost()))`
     → `money_round(...)`. This was the exact bug that would round the owner's
     electricity recipe (₦0.06/kWh) to **₦0**.
   - `production.record_production` stored `landing_cost`/tx amount/unit_cost via
     `int()` → `money_round` (Decimal `total_cost/good_qty`).
   - recipe-line display strings `f"₦{int(cost):,}"` → `format_amount(cost)`.
   - `transactions._update_recipe_costs`: `effective_cost = int(stored_cost)` →
     `float(to_money(stored_cost))` so a sub-naira material cost survives into the
     recipe (+ added a local `to_money` import).

### Verified (E2E, all green)
Ran a throwaway `_money_e2e.py` (deleted after) against the real functions:
- Rounding: `0.1+0.2=0.30`, HALF_UP `2.005→2.01`, whole→int type, `1500.5`
  preserved, weighted-avg `0.0566→0.06`, `is_whole` correct.
- **Electricity roll-up** via `ProductionHandler.recipe_unit_cost`:
  Nylon `0.002 kg × ₦1200` + Electricity `0.5 kWh × ₦0.06` = **₦2.43**, stamped
  `2.43`. Pure sub-naira recipe (`0.5 × 0.06`) = **₦0.03**, stamped `0.03` and
  **> 0** (old code stored 0). 
- P&L round-trip: 100 × cost 2.43 → COGS 243; rev 500; gross 257. Inventory
  `2.5 × 0.03 = 0.08`.
- Display: `fmt_money` → `₦0.06`, `₦1,500`, `₦1,500.50`, `₦2.43`.
- `py_compile` + `check_syntax.py` green on every changed file each step;
  mini-app UTF-8/surrogate scan = 0 lone surrogates.

### Guardrails honoured
Quantity helpers untouched. Decimals from str, quantized once at storage.
No data migration (whole-naira rows already valid). Paystack kobo boundary
untouched. WhatsApp render path unchanged (uses the already-decimal-aware
`format_amount`).

### DEPLOY (owner runs — agent does not deploy)
Engine + mini-app JS only; no `template.yaml` change in this phase.
```
cd ~/projects/kashia-bot
./deploy.sh dev
```
(No `set_telegram_commands.sh` needed — the command menu did not change.)
