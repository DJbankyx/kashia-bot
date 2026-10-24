"""Regression tests for the money-correctness branch (fix/money-correctness).

Covers the review findings the branch set out to fix (report items #1/#2/#3/#4/
#5/#7/#10). Written in the project's existing unittest style (see
tests/test_miniapp_auth.py). The core money + recipe cases run with a light
in-memory FakeDB so the suite is self-contained and always runnable; the
DynamoDB-layer cases (strongly-consistent recompute, soft-delete, date filter)
use moto when it is installed and skip cleanly otherwise.

Run:  python -m unittest tests.test_money_fixes -v   (from the worktree root)
"""

import os
import sys
import unittest
from decimal import Decimal

# Match the Lambda import root: src/ on the path (same as the other tests).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from services.database import Database            # for the REAL _sanitize_for_dynamo
from services.accounting import _cash_received, _cash_paid
from features.production import ProductionHandler
from features.transactions import TransactionHandler


# ─────────────────────────────────────────────────────────────────────────
# In-memory doubles. Only the methods the code under test actually calls are
# implemented. save_transaction / update_transaction route MONEY through the
# REAL Database._sanitize_for_dynamo, so the float→Decimal guard (report #2) is
# exercised exactly as in production — a kobo deposit that boto3 would reject as
# a float will surface here too.
# ─────────────────────────────────────────────────────────────────────────
class FakeDB:
    def __init__(self, user=None):
        self._user = user or {}
        self.rows = {}            # tx_id -> row dict (post-sanitise, like DynamoDB)
        self.debts = []           # (contact, amount, direction, source_type)
        self._seq = 0

    # ── users / catalog ──
    def get_user(self, phone_number):
        return dict(self._user)

    def update_user_field(self, phone_number, field, value):
        self._user[field] = value
        return True

    # ── transactions ──
    def save_transaction(self, phone_number, amount, tx_type, description, category,
                         sub_category="", vendor="", confidence=0, item_name=None,
                         brand=None, model=None, size=None, color=None, quantity=None,
                         unit_cost=None, payment_method=None, payment_status=None,
                         extra_details=None, tags=None, subtotal=None,
                         discount_amount=None, discount_percent=None, discount_type=None,
                         tax_amount=None, tax_percent=None, tax_type=None,
                         deposit_amount=None):
        from utils.money import money_round
        self._seq += 1
        tx_id = f"tx{self._seq}"
        row = {
            "phone_number": phone_number, "transaction_id": tx_id,
            "amount": money_round(amount), "type": tx_type,
            "description": description, "category": category, "vendor": vendor,
        }
        if payment_method:
            row["payment_method"] = payment_method
        if extra_details:
            row["extra_details"] = extra_details
        if deposit_amount is not None:
            row["deposit_amount"] = money_round(deposit_amount)
        # Persist exactly as DynamoDB would — through the real sanitiser. This is
        # what proves report #2: a fractional (float) deposit becomes Decimal
        # instead of raising "Float types are not supported".
        self.rows[tx_id] = Database._sanitize_for_dynamo(row)
        return {"transaction_id": tx_id}

    def update_transaction(self, phone_number, transaction_id, updates):
        # Mirror the real method: sanitise the whole updates dict (report #2) so a
        # float value never reaches the store.
        updates = Database._sanitize_for_dynamo(dict(updates))
        row = self.rows.get(transaction_id)
        if row is None:
            return False
        row.update(updates)
        return True

    def record_debt(self, phone_number, contact_name, amount, debt_type,
                    description="", due_date=None, source_type=None):
        self.debts.append({"contact": contact_name, "amount": amount,
                           "direction": debt_type, "source_type": source_type})
        return True

    def update_contact_totals(self, phone_number, contact_name, amount, tx_type):
        return True


class FakeSession:
    def save(self, *a, **k):
        return True

    def reset(self, *a, **k):
        return True

    def get(self, *a, **k):
        return None


def _mat(name, primary_unit, landing_cost):
    """A catalog raw-material row shaped the way the recipe engine reads it."""
    return {
        "name": name, "stock": 0, "landing_cost": landing_cost,
        "item_type": "raw_material", "primary_unit": primary_unit,
        "variants": [], "recipe": [], "conversions": {},
    }


def _catalog_user(products):
    return {"product_catalog": {"products": products}}


# ─────────────────────────────────────────────────────────────────────────
# Recipe unit-cost conversion (report #5 + #10)
# ─────────────────────────────────────────────────────────────────────────
class TestRecipeUnitCost(unittest.TestCase):
    def _handler(self, products):
        return ProductionHandler(FakeSession(), FakeDB(_catalog_user(products)))

    def test_50cl_against_10_per_litre_is_5_not_500(self):
        # ₦10/litre, recipe line 50 cl. 50 cl = 0.5 L → ₦5 (NOT 50×₦10 = ₦500).
        products = {
            "juice": {"name": "Juice", "item_type": "product", "recipe": [],
                      "variants": [], "conversions": {}},
            "water": _mat("Water", "litre", 10),
        }
        h = self._handler(products)
        res = h.web_add_material("p", "juice", "water", 50, unit="cl")
        self.assertTrue(res.get("ok"), res)
        self.assertAlmostEqual(res["unit_cost"], 5, places=4)

    def test_sub_kobo_rate_does_not_round_to_zero(self):
        # ₦1/litre, 1 ml line. Per-unit rate = ₦0.001/ml — must NOT be stored as
        # ₦0 (the mid-calculation rounding bug). Line total = ₦0.001.
        products = {
            "mix": {"name": "Mix", "item_type": "product", "recipe": [],
                    "variants": [], "conversions": {}},
            "additive": _mat("Additive", "litre", 1),
        }
        h = self._handler(products)
        res = h.web_add_material("p", "mix", "additive", 1, unit="ml")
        self.assertTrue(res.get("ok"), res)
        line = res["materials"][0]
        self.assertGreater(float(line.get("cost_per_unit", 0)), 0,
                           "sub-kobo per-unit rate rounded to zero")
        # 1000 ml of the same additive must cost ~₦1, not ₦0.
        res2 = h.web_add_material("p", "mix", "additive", 1000, unit="ml")
        self.assertAlmostEqual(res2["unit_cost"], 1, places=4)

    def test_explicit_typed_cost_is_not_converted(self):
        # An explicitly typed cost is already per the chosen recipe unit — the
        # engine must store it VERBATIM (no base→unit conversion).
        products = {
            "mix": {"name": "Mix", "item_type": "product", "recipe": [],
                    "variants": [], "conversions": {}},
            "oil": _mat("Oil", "litre", 10),
        }
        h = self._handler(products)
        res = h.web_add_material("p", "mix", "oil", 50, unit="cl", cost_per_unit=3)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(float(res["materials"][0]["cost_per_unit"]), 3.0)
        self.assertAlmostEqual(res["unit_cost"], 150, places=4)  # 50 × 3

    def test_overhead_rate_line_is_untouched(self):
        # Overhead carries its cost in `rate` (per usage-unit) and is NEVER
        # converted base→unit.
        products = {
            "mix": {"name": "Mix", "item_type": "product", "recipe": [],
                    "variants": [], "conversions": {}},
            "power": {"name": "Power", "item_type": "overhead", "landing_cost": 50,
                      "primary_unit": "kwh", "variants": [], "recipe": [],
                      "conversions": {}},
        }
        h = self._handler(products)
        res = h.web_add_material("p", "mix", "power", 2, unit="kwh",
                                 cost_per_unit=50, mat_type="overhead")
        self.assertTrue(res.get("ok"), res)
        line = res["materials"][0]
        self.assertEqual(float(line.get("rate")), 50.0)
        self.assertNotIn("cost_per_unit", line)

    def test_unresolvable_unit_is_rejected_not_silently_stored(self):
        # base=litre, recipe unit=piece → no standard/custom factor. The engine
        # must REJECT (warning) rather than store the unconverted base-unit cost
        # (the up-to-100× inflation of report #10).
        products = {
            "mix": {"name": "Mix", "item_type": "product", "recipe": [],
                    "variants": [], "conversions": {}},
            "stuff": _mat("Stuff", "litre", 10),
        }
        h = self._handler(products)
        res = h.web_add_material("p", "mix", "stuff", 3, unit="piece")
        self.assertFalse(res.get("ok"), res)
        self.assertTrue(res.get("needs_unit"))


# ─────────────────────────────────────────────────────────────────────────
# Cash readers: kobo-precise deposits (report #7) + method rules
# ─────────────────────────────────────────────────────────────────────────
class TestCashReaders(unittest.TestCase):
    def test_deposit_with_kobo_is_not_truncated(self):
        tx = {"type": "sale", "payment_method": "deposit",
              "amount": 100000, "deposit_amount": 50000.50}
        self.assertEqual(_cash_received(tx), 50000.50)

    def test_purchase_deposit_with_kobo_is_not_truncated(self):
        tx = {"type": "purchase", "payment_method": "deposit",
              "amount": 100000, "deposit_amount": 10.75}
        self.assertEqual(_cash_paid(tx), 10.75)

    def test_credit_moves_no_cash(self):
        tx = {"type": "purchase", "payment_method": "credit", "amount": 150000}
        self.assertEqual(_cash_paid(tx), 0)


# ─────────────────────────────────────────────────────────────────────────
# Part-payment persistence across BOTH surfaces (report #1/#2/#4/#14)
# ─────────────────────────────────────────────────────────────────────────
class TestPartPaymentPersistence(unittest.TestCase):
    def _tx_handler(self, db):
        return TransactionHandler(FakeSession(), db, categorizer=None,
                                  get_industry_fn=lambda p: "trading")

    def test_web_150k_purchase_50k_deposit(self):
        # Mini App path: ₦150k purchase, ₦50k deposit → ₦50k cash out, ₦100k payable.
        db = FakeDB(_catalog_user({}))
        th = self._tx_handler(db)
        res = th.record_transaction_web("user1", {
            "type": "purchase", "amount": 150000, "description": "Cement",
            "vendor": "BUA", "has_credit": True, "deposit_amount": 50000,
            "payment_method": "deposit",
        })
        self.assertTrue(res.get("ok"), res)
        row = db.rows[res["transaction_id"]]
        self.assertEqual(row.get("payment_method"), "deposit")
        self.assertEqual(_cash_paid(row), 50000)                      # cash out
        self.assertEqual(sum(d["amount"] for d in db.debts), 100000)  # payable

    def test_web_deposit_with_kobo_persists_and_counts(self):
        # ₦50,000.50 deposit (report #2): must persist as Decimal (not raise) and
        # be counted kobo-precise.
        db = FakeDB(_catalog_user({}))
        th = self._tx_handler(db)
        res = th.record_transaction_web("user1", {
            "type": "purchase", "amount": 150000, "description": "Rods",
            "vendor": "BUA", "has_credit": True, "deposit_amount": 50000.50,
            "payment_method": "deposit",
        })
        self.assertTrue(res.get("ok"), res)
        row = db.rows[res["transaction_id"]]
        self.assertIsInstance(row["deposit_amount"], Decimal)   # sanitised, not float
        self.assertEqual(_cash_paid(row), 50000.50)

    def test_chat_credit_path_150k_purchase_50k_deposit(self):
        # SAME economics via the chat/_save_credit_transaction path (report #4):
        # the surface must agree with the web path — ₦50k cash out, ₦100k payable.
        db = FakeDB(_catalog_user({}))
        th = self._tx_handler(db)
        th._save_credit_transaction("user1", {
            "type": "purchase", "amount": 150000, "description": "Cement",
            "category": "Goods & Stock", "vendor": "BUA", "has_credit": True,
            "deposit_amount": 50000, "balance_owed": 100000,
        })
        # Exactly one real transaction row was written; it must book the deposit.
        rows = list(db.rows.values())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.get("payment_method"), "deposit")
        self.assertEqual(_cash_paid(row), 50000)
        self.assertEqual(sum(d["amount"] for d in db.debts), 100000)


# ─────────────────────────────────────────────────────────────────────────
# DynamoDB layer: consistent recompute, soft-delete, date filter
# (report #3). Needs moto; skipped cleanly when it is not installed.
# ─────────────────────────────────────────────────────────────────────────
try:
    import boto3
    from moto import mock_aws
    _HAS_MOTO = True
except Exception:
    _HAS_MOTO = False


@unittest.skipUnless(_HAS_MOTO, "moto not installed")
class TestConsistentReadLayer(unittest.TestCase):
    def setUp(self):
        # Pin the region so the moto table and Database()'s own
        # boto3.resource('dynamodb') (which takes no explicit region) resolve to
        # the SAME mock account — otherwise put_item hits a region with no table.
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
        self.mock = mock_aws()
        self.mock.start()
        self.ddb = boto3.resource("dynamodb", region_name="us-east-1")
        # Real schema: phone_number (HASH) + transaction_id (RANGE), with the
        # date-index GSI the non-consistent path uses.
        self.ddb.create_table(
            TableName="kashia-transactions-dev",
            KeySchema=[{"AttributeName": "phone_number", "KeyType": "HASH"},
                       {"AttributeName": "transaction_id", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "phone_number", "AttributeType": "S"},
                {"AttributeName": "transaction_id", "AttributeType": "S"},
                {"AttributeName": "date", "AttributeType": "S"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "date-index",
                "KeySchema": [{"AttributeName": "phone_number", "KeyType": "HASH"},
                              {"AttributeName": "date", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Minimal users table so save_transaction's counter update doesn't blow up.
        self.ddb.create_table(
            TableName="kashia-users-dev",
            KeySchema=[{"AttributeName": "phone_number", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "phone_number", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.db = Database(stage="dev")

    def tearDown(self):
        self.mock.stop()

    def _put(self, phone, tx_id, date, **extra):
        item = {"phone_number": phone, "transaction_id": tx_id, "date": date,
                "type": "sale", "amount": 1000, "created_at": date + "T00:00:00"}
        item.update(extra)
        self.db.transactions.put_item(Item=Database._sanitize_for_dynamo(item))

    def test_soft_deleted_row_vanishes_from_consistent_recompute(self):
        self._put("u", "a", "2026-06-01")
        self._put("u", "b", "2026-06-02", deleted=True, deleted_at="2026-06-03T00:00:00")
        rows = self.db.get_transactions_by_period("u", "1970-01-01", "2026-12-31",
                                                  consistent=True)
        ids = {r["transaction_id"] for r in rows}
        self.assertIn("a", ids)
        self.assertNotIn("b", ids)   # soft-deleted row is filtered out

    def test_consistent_read_date_filtering(self):
        self._put("u", "in", "2026-06-15")
        self._put("u", "before", "2026-05-31")
        self._put("u", "after", "2026-07-01")
        rows = self.db.get_transactions_by_period("u", "2026-06-01", "2026-06-30",
                                                  consistent=True)
        ids = {r["transaction_id"] for r in rows}
        self.assertEqual(ids, {"in"})

    def test_consistent_read_failure_raises_not_empty(self):
        # A genuine read failure on the consistent path must RAISE (so
        # cash_position can fall back) instead of masquerading as an empty ledger.
        orig = self.db.transactions.query

        def boom(*a, **k):
            raise RuntimeError("throttled")

        self.db.transactions.query = boom
        try:
            with self.assertRaises(Exception):
                self.db.get_transactions_by_period("u", "1970-01-01", "2026-12-31",
                                                   consistent=True)
        finally:
            self.db.transactions.query = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
