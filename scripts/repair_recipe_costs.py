#!/usr/bin/env python3
"""Kashia one-off repair — fix pre-fix inflated Mini-App recipe costs.

WHY
  Before the BUG-1 fix, the Mini-App recipe editor (web_add_material) stored a
  recipe line's `cost_per_unit` as the material's UNCONVERTED base-unit
  `landing_cost`. When the recipe line used a DIFFERENT unit from the material's
  base unit (e.g. a ₦10/litre material used as a 'cl' line), the stored per-unit
  cost was inflated — up to 100x — because the base→recipe-unit conversion was
  skipped. web_add_material now converts correctly, but recipes CREATED BEFORE
  the fix (and never re-edited) still hold the inflated figure. The chat-only
  "Recalculate Costs" is not a safe repair (it clobbers hand-typed costs, never
  re-stamps the product cost, and rounds sub-kobo per-unit rates to ₦0), and
  Mini-App-only owners can't trigger it anyway. This script is the backfill.

WHAT IT DOES
  For each affected recipe LINE it rewrites `cost_per_unit` to the correctly
  converted value (landing_cost × factor_to_base(recipe_unit, base_unit)) at
  FULL precision, then re-stamps the finished product's `landing_cost` the same
  way ProductionHandler._save_recipe does (money_round(recipe_unit_cost(recipe))
  — rounded ONCE at the storage boundary). A line is only touched when its
  stored cost ≈ the material's UNCONVERTED base-unit landing_cost, so a
  hand-TYPED per-recipe-unit cost is left alone. Idempotent: a second run finds
  nothing to change.

USAGE
  # Dry-run a single account (reports, writes NOTHING)
  python scripts/repair_recipe_costs.py --user tg:1072412276
  # Dry-run every account
  python scripts/repair_recipe_costs.py --all --stage dev
  # Actually apply (NOTHING is written without --confirm)
  python scripts/repair_recipe_costs.py --user tg:1072412276 --confirm
  python scripts/repair_recipe_costs.py --all --confirm

SAFETY
  * DRY-RUN by default. Writes only when --confirm is given.
  * Rewrites ONLY lines whose stored cost matches the unconverted base cost
    signature; typed costs and already-correct lines are never touched.
  * Unconvertible units (factor None) are FLAGGED, never rewritten.
  * Runs against --stage dev by default.
"""

import argparse
import os
import sys

# Make the engine importable whether run from repo root or scripts/.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

# Relative tolerance for "stored ≈ unconverted base cost" / "already correct".
_TOL = 1e-6


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _close(a, b, tol=_TOL):
    """Relative closeness; handles the b==0 edge."""
    a = _num(a)
    b = _num(b)
    if b == 0:
        return abs(a) <= tol
    return abs(a - b) / abs(b) < tol


def analyze_product(prod, products):
    """Classify every recipe line of one product.

    Returns a dict with:
      affected   -> [{material, recipe_unit, base_unit, stored, expected, factor}]
      flagged    -> [{material, recipe_unit, base_unit, reason}]  (needs attention)
    `expected` is the FULL-precision converted per-unit cost.
    Pure (no I/O); the caller decides whether to write.
    """
    from utils import units

    affected = []
    flagged = []
    recipe = prod.get("recipe") or []
    if not isinstance(recipe, list):
        return {"affected": affected, "flagged": flagged}

    for line in recipe:
        if not isinstance(line, dict):
            continue
        # Overhead lines use `rate`, not a convertible material cost — skip.
        if str(line.get("type", "material")) == "overhead":
            continue

        # Recipe lines store the material by DISPLAY NAME (e.g. "Nylon",
        # "Machine Hour"); the catalog is keyed by SLUG. Derive the key exactly
        # as the engine does in ProductionHandler._recalculate_all_costs:
        #   mat_key = mat_name.lower().replace(" ", "_")
        mat_name = line.get("material") or ""
        mat_key = mat_name.lower().replace(" ", "_") if mat_name else None
        mat_product = products.get(mat_key) if mat_key else None
        if not isinstance(mat_product, dict):
            # Can't resolve the material → can't verify; flag, don't touch.
            flagged.append({
                "material": mat_name, "recipe_unit": line.get("unit"),
                "base_unit": None, "reason": "material not found in catalog",
            })
            continue

        landing_cost = _num(mat_product.get("landing_cost", 0))
        if landing_cost <= 0:
            # No catalog cost to derive from (likely a hand-typed line) — leave.
            continue

        recipe_unit = line.get("unit") or ""
        try:
            base_unit, unit_defs = units.product_units(mat_product)
        except Exception:
            base_unit, unit_defs = (mat_product.get("primary_unit") or ""), {}

        # Same unit → nothing to convert, nothing to repair.
        if recipe_unit.rstrip("s").lower() == (base_unit or "").rstrip("s").lower():
            continue

        factor = units.factor_to_base(recipe_unit, base_unit, unit_defs)
        if factor is None or factor <= 0:
            flagged.append({
                "material": mat_name, "recipe_unit": recipe_unit,
                "base_unit": base_unit,
                "reason": "unconvertible unit (no factor) — not rewritten",
            })
            continue

        expected = landing_cost * factor          # FULL precision
        stored = _num(line.get("cost_per_unit", 0))

        # AFFECTED = stored still equals the UNCONVERTED base-unit cost AND is
        # not already the correctly-converted value. A hand-typed per-recipe-unit
        # cost won't match the base-unit landing_cost, so it is left alone.
        if _close(stored, landing_cost) and not _close(stored, expected):
            # Store the display name (not the slug) so the write-back key
            # (line["material"], unit) matches the recipe line exactly.
            affected.append({
                "material": mat_name, "recipe_unit": recipe_unit,
                "base_unit": base_unit, "stored": stored,
                "expected": expected, "factor": factor,
            })

    return {"affected": affected, "flagged": flagged}


def repair_catalog(catalog, confirm=False):
    """Repair every affected recipe line in a user's product_catalog IN PLACE
    (only when confirm=True), re-stamping each touched product's landing_cost.

    Returns a report dict:
      {products: [{product_key, product_name, affected:[...], flagged:[...],
                   new_product_cost}], lines_affected, lines_flagged,
       products_repaired, changed}
    When confirm=False the catalog is NOT mutated (pure analysis).
    """
    from features.production import ProductionHandler
    from utils.money import money_round

    products = (catalog or {}).get("products", {}) or {}
    report = {"products": [], "lines_affected": 0, "lines_flagged": 0,
              "products_repaired": 0, "changed": False}

    for pkey, prod in products.items():
        if not isinstance(prod, dict) or not prod.get("recipe"):
            continue
        res = analyze_product(prod, products)
        affected = res["affected"]
        flagged = res["flagged"]
        if not affected and not flagged:
            continue

        entry = {
            "product_key": pkey,
            "product_name": prod.get("name") or pkey,
            "affected": affected,
            "flagged": flagged,
            "new_product_cost": None,
        }
        report["lines_affected"] += len(affected)
        report["lines_flagged"] += len(flagged)

        if affected and confirm:
            # Rewrite each affected line's per-unit cost at FULL precision.
            idx = {(a["material"], a["recipe_unit"]): a for a in affected}
            for line in prod["recipe"]:
                if not isinstance(line, dict):
                    continue
                key = (line.get("material"), line.get("unit") or "")
                if key in idx:
                    line["cost_per_unit"] = idx[key]["expected"]
            # Re-stamp the finished product cost ONCE at the storage boundary,
            # exactly like _save_recipe (money_round of the rolled-up recipe).
            new_cost = money_round(ProductionHandler.recipe_unit_cost(prod["recipe"]))
            prod["landing_cost"] = new_cost
            entry["new_product_cost"] = new_cost
            report["products_repaired"] += 1
            report["changed"] = True

        report["products"].append(entry)

    return report


def _print_report(user_id, report, confirm):
    verb = "REPAIRED" if confirm else "WOULD REPAIR"
    print("\n" + "=" * 64)
    print(f"ACCOUNT {user_id}")
    print("=" * 64)
    if not report["products"]:
        print("  No affected recipes — nothing to do.")
        return
    for p in report["products"]:
        print(f"\n  Product: {p['product_name']}  [{p['product_key']}]")
        for a in p["affected"]:
            print(f"    {verb}: material '{a['material']}' "
                  f"{a['recipe_unit']} vs base {a['base_unit']} | "
                  f"stored {a['stored']:.6g} -> {a['expected']:.6g} "
                  f"(x{a['factor']:.6g})")
        if p["new_product_cost"] is not None:
            print(f"    product landing_cost re-stamped -> {p['new_product_cost']}")
        for f in p["flagged"]:
            print(f"    ⚠️  FLAGGED: material '{f['material']}' "
                  f"({f['recipe_unit']} -> {f['base_unit']}): {f['reason']}")
    print(f"\n  Lines affected: {report['lines_affected']}  "
          f"Lines flagged: {report['lines_flagged']}  "
          f"Products {'repaired' if confirm else 'to repair'}: "
          f"{report['products_repaired'] if confirm else len(report['products'])}")


def _scan_all_users(db):
    """Enumerate every account id. Low-volume admin work — a Scan is fine
    (same pattern as scripts/audit_user.py)."""
    ids = []
    try:
        resp = db.users.scan(ProjectionExpression="phone_number")
        ids.extend(i.get("phone_number") for i in resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = db.users.scan(ProjectionExpression="phone_number",
                                 ExclusiveStartKey=resp["LastEvaluatedKey"])
            ids.extend(i.get("phone_number") for i in resp.get("Items", []))
    except Exception as e:
        print(f"⚠️  Could not scan users table: {e}")
    return [i for i in ids if i]


def process_user(db, user_id, confirm):
    """Analyze (and, with confirm, repair + persist) one account. Returns the
    report dict (or None if the user/catalog is missing)."""
    user = db.get_user(user_id)
    if not user:
        print(f"❌ No user found for id: {user_id}")
        return None
    catalog = user.get("product_catalog", {}) or {}
    report = repair_catalog(catalog, confirm=confirm)
    if confirm and report["changed"]:
        db.update_user_field(user_id, "product_catalog", catalog)
    return report


def main():
    ap = argparse.ArgumentParser(
        description="Repair pre-fix inflated Mini-App recipe costs "
                    "(dry-run unless --confirm).")
    ap.add_argument("--user", help="tg:<chat_id> or bare WhatsApp number")
    ap.add_argument("--all", action="store_true",
                    help="Scan and process every account")
    ap.add_argument("--stage", default="dev", help="DynamoDB stage (default: dev)")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually write repairs (otherwise dry-run)")
    args = ap.parse_args()

    if not args.user and not args.all:
        ap.error("provide --user <id> or --all")

    from services.database import Database
    db = Database(stage=args.stage)

    if not args.confirm:
        print("🔎 DRY-RUN — no changes will be written. Re-run with --confirm to apply.")

    targets = [args.user] if args.user else _scan_all_users(db)
    totals = {"affected": 0, "flagged": 0, "products": 0, "users_touched": 0}
    for uid in targets:
        report = process_user(db, uid, args.confirm)
        if not report:
            continue
        if report["products"]:
            _print_report(uid, report, args.confirm)
            totals["affected"] += report["lines_affected"]
            totals["flagged"] += report["lines_flagged"]
            totals["products"] += (report["products_repaired"]
                                   if args.confirm else len(report["products"]))
            totals["users_touched"] += 1

    print("\n" + "#" * 64)
    print(f"SUMMARY  users scanned: {len(targets)}  "
          f"users with affected recipes: {totals['users_touched']}")
    print(f"         lines affected: {totals['affected']}  "
          f"lines flagged (unconvertible): {totals['flagged']}  "
          f"products {'repaired' if args.confirm else 'to repair'}: "
          f"{totals['products']}")
    if not args.confirm and totals["affected"]:
        print("         (dry-run) re-run with --confirm to apply these repairs.")
    print("#" * 64)


if __name__ == "__main__":
    main()
