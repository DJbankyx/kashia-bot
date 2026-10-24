"""Regression tests for the recalc-repair + cash-position-cost branch
(fix/recalc-and-cashcost).

TASK 1 — scripts/repair_recipe_costs.py repairs pre-fix inflated Mini-App recipe
costs (cost_per_unit stored as the UNCONVERTED base-unit landing_cost). Covers:
dry-run writes nothing but reports; --confirm converts to full precision and
re-stamps the product cost (50 cl of a ₦10/litre material contributes ₦5, not
₦500); a hand-typed line is left alone; a second run is a no-op (idempotent);
an unconvertible unit is flagged, not rewritten.

TASK 2 — Accounting.position(cash_at_hand=...) skips its internal cash_position
recompute when the caller already computed it, so /api/summary no longer runs
the O(all-transactions) strongly-consistent scan twice per request.

Written in the project's existing unittest style (see tests/test_money_fixes.py).
Run:  python -m unittest tests.test_recalc_and_cashcost -v   (from worktree root)
"""

import os
import sys
import importlib.util
import unittest

# Match the Lambda import root: src/ on the path (same as the other tests).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from features.production import ProductionHandler
from services.accounting import Accounting
from utils.money import money_round


# Import the repair script as a module (it lives in scripts/, not a package).
def _load_repair_module():
    path = os.path.join(_ROOT, "scripts", "repair_recipe_costs.py")
    spec = importlib.util.spec_from_file_location("repair_recipe_costs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


repair = _load_repair_module()


def _catalog_with_inflated_line():
    """A catalog where 'Water' is a ₦10/litre material and the finished
    'Sachet' recipe stores the UNCONVERTED cost (10) against a 50 cl line — the
    pre-fix inflation. Also carries a hand-TYPED overhead/material line that must
    never be touched."""
    return {
        "products": {
            "water": {
                "name": "Water", "item_type": "raw_material",
                "primary_unit": "l", "landing_cost": 10, "unit_defs": {},
            },
            "sachet": {
                "name": "Sachet", "item_type": "finished_product",
                "landing_cost": 500,     # inflated stamped cost (50 * 10)
                "recipe": [
                    # inflated: stored 10 == unconverted base cost, unit is cl
                    {"material": "water", "quantity": 50, "unit": "cl",
                     "type": "material", "cost_per_unit": 10},
                    # hand-typed material: cost does NOT equal any base cost →
                    # must be left alone (here a labour overhead, cost typed).
                    {"material": "labour", "quantity": 1, "unit": "unit",
                     "type": "overhead", "rate": 3},
                ],
            },
        }
    }


class RepairRecipeCostsTest(unittest.TestCase):

    def test_dryrun_reports_but_writes_nothing(self):
        catalog = _catalog_with_inflated_line()
        before = catalog["products"]["sachet"]["recipe"][0]["cost_per_unit"]
        report = repair.repair_catalog(catalog, confirm=False)
        # Reported the affected line…
        self.assertEqual(report["lines_affected"], 1)
        self.assertEqual(len(report["products"]), 1)
        # …but mutated nothing (dry-run).
        self.assertEqual(
            catalog["products"]["sachet"]["recipe"][0]["cost_per_unit"], before)
        self.assertEqual(catalog["products"]["sachet"]["landing_cost"], 500)
        self.assertFalse(report["changed"])

    def test_confirm_converts_fullprecision_and_restamps_product(self):
        catalog = _catalog_with_inflated_line()
        report = repair.repair_catalog(catalog, confirm=True)
        self.assertTrue(report["changed"])
        self.assertEqual(report["products_repaired"], 1)

        recipe = catalog["products"]["sachet"]["recipe"]
        # 10 ₦/litre * 0.01 litre/cl = 0.1 ₦/cl, FULL precision (not rounded to 0).
        self.assertAlmostEqual(recipe[0]["cost_per_unit"], 0.1, places=9)
        # 50 cl now contributes ₦5, NOT ₦500.
        line_total = recipe[0]["quantity"] * recipe[0]["cost_per_unit"]
        self.assertAlmostEqual(line_total, 5.0, places=9)
        # Product cost re-stamped once at the storage boundary: 5 (water) + 3
        # (typed overhead) = 8.
        self.assertEqual(catalog["products"]["sachet"]["landing_cost"],
                         money_round(ProductionHandler.recipe_unit_cost(recipe)))
        self.assertEqual(catalog["products"]["sachet"]["landing_cost"], 8)

    def test_handtyped_line_is_untouched(self):
        catalog = _catalog_with_inflated_line()
        repair.repair_catalog(catalog, confirm=True)
        # The overhead's typed rate (3) is never rewritten.
        overhead = catalog["products"]["sachet"]["recipe"][1]
        self.assertEqual(overhead["rate"], 3)

    def test_idempotent_second_run_is_noop(self):
        catalog = _catalog_with_inflated_line()
        repair.repair_catalog(catalog, confirm=True)       # first pass fixes it
        report2 = repair.repair_catalog(catalog, confirm=True)
        self.assertEqual(report2["lines_affected"], 0)
        self.assertFalse(report2["changed"])

    def test_sub_kobo_rate_not_flushed_to_zero(self):
        """A ₦1/litre material on a 1 ml line → ₦0.001/ml: must keep full
        precision so the line doesn't collapse to ₦0 (the BUG-1 discipline)."""
        catalog = {
            "products": {
                "water": {"name": "Water", "item_type": "raw_material",
                          "primary_unit": "l", "landing_cost": 1, "unit_defs": {}},
                "drop": {"name": "Drop", "item_type": "finished_product",
                         "landing_cost": 1,
                         "recipe": [{"material": "water", "quantity": 1,
                                     "unit": "ml", "type": "material",
                                     "cost_per_unit": 1}]},
            }
        }
        repair.repair_catalog(catalog, confirm=True)
        line = catalog["products"]["drop"]["recipe"][0]
        # 1 ₦/litre * 0.001 litre/ml = 0.001 ₦/ml, preserved (not 0).
        self.assertAlmostEqual(line["cost_per_unit"], 0.001, places=9)
        self.assertGreater(line["cost_per_unit"], 0)

    def test_unconvertible_unit_is_flagged_not_rewritten(self):
        """A cross-system unit (kg material used as a litre line) has no factor
        → flag it, never rewrite."""
        catalog = {
            "products": {
                "flour": {"name": "Flour", "item_type": "raw_material",
                          "primary_unit": "kg", "landing_cost": 20, "unit_defs": {}},
                "cake": {"name": "Cake", "item_type": "finished_product",
                         "landing_cost": 20,
                         "recipe": [{"material": "flour", "quantity": 2,
                                     "unit": "litre", "type": "material",
                                     "cost_per_unit": 20}]},
            }
        }
        report = repair.repair_catalog(catalog, confirm=True)
        self.assertEqual(report["lines_affected"], 0)
        self.assertEqual(report["lines_flagged"], 1)
        # Untouched.
        self.assertEqual(
            catalog["products"]["cake"]["recipe"][0]["cost_per_unit"], 20)


# ─────────────────────────────────────────────────────────────────────────
# TASK 2 — position(cash_at_hand=...) must not recompute.
# ─────────────────────────────────────────────────────────────────────────
class _SpyDB:
    """Minimal DB so Accounting.position() runs. Counts cash-path user reads to
    prove the internal cash_position recompute is skipped when cash is passed."""
    def __init__(self):
        self.get_user_calls = 0

    def get_user(self, phone_number):
        self.get_user_calls += 1
        return {"opening_cash": 0}

    def get_all_debtors(self, phone_number):
        return []

    def get_all_creditors(self, phone_number):
        return []


class PositionCashThreadingTest(unittest.TestCase):

    def test_position_uses_passed_cash_and_skips_recompute(self):
        db = _SpyDB()
        acct = Accounting(db)
        # Spy on cash_position so we can tell if position() recomputed it.
        calls = {"n": 0}
        orig = acct.cash_position

        def _counting_cash_position(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)
        acct.cash_position = _counting_cash_position

        pos = acct.position("tg:1", cash_at_hand=12345)
        # position() must NOT call cash_position when the value is supplied…
        self.assertEqual(calls["n"], 0)
        # …and must use the passed figure in the snapshot + net worth.
        self.assertEqual(pos["cash_at_hand"], 12345)
        self.assertEqual(pos["net_worth"],
                         12345 + pos["inventory_value"]
                         + pos["receivables"] - pos["payables"])

    def test_position_without_cash_still_recomputes(self):
        """Back-compat: callers that don't pass cash get the old behaviour."""
        db = _SpyDB()
        acct = Accounting(db)
        calls = {"n": 0}
        orig = acct.cash_position

        def _counting_cash_position(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)
        acct.cash_position = _counting_cash_position

        acct.position("tg:1")   # no cash_at_hand
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
