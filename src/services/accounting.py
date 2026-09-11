"""Accounting — the single source of truth for report numbers.

Every report surface (dashboard, in-chat P&L, PDF statement, Excel export) MUST
read its figures from here so they can never disagree. This module is pure
computation over a user's transactions + catalog; it does no formatting and
sends no messages.

Accounting model (locked with the owner — see docs/ACCOUNTING_REPORTS_PLAN.md):
  * ACCRUAL basis. Revenue is recognised when a SALE happens (not when cash is
    received). A purchase becomes inventory when it happens.
  * COGS is the cost of goods actually SOLD, matched per-sale — NOT the total
    of everything purchased in the period. (This is the fix for the "bought a
    Mercedes, looked like a loss" bug.)
  * WEIGHTED-AVERAGE cost basis. A product carries a running average unit cost.
    A sale's COGS = avg_unit_cost * qty. A SPECIFIC cost overrides the average
    only when the sale itself recorded a landing cost (unique high-value goods).
  * Credit is NOT a P&L item. Unpaid sales are receivables; unpaid purchases are
    payables (balance-sheet, handled by the position report — R5).
  * Never fake a zero cost. When no cost is known for a sold item, the sale is
    flagged UNCOSTED and excluded from COGS/margin rather than distorting it.

R1 scope: cogs_for_sale() + period_pnl(). Cash flow (R4) and position/inventory
(R5) land in later stages and will live here too.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Expense categories that represent Cost of Goods Sold inputs (stock/materials
# bought to resell or produce). Kept in sync with reports.COGS_CATEGORIES.
COGS_CATEGORIES = {
    "Goods & Stock",
    "Production & Manufacturing",
    "Service Costs",
}

# Cost source labels returned by cogs_for_sale — so callers can explain/flag.
COST_SALE_LANDING = "sale_landing_cost"   # the sale recorded its own cost (specific)
COST_WEIGHTED_AVG = "weighted_avg"        # product's running weighted-average cost
COST_CATALOG = "catalog"                  # catalog landing_cost fallback
COST_MISSING = "MISSING"                  # no cost known — flag, don't fake


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _qty_of(tx):
    """Best-effort integer quantity from a transaction (defaults to 1)."""
    m = re.match(r"^\s*(\d+)", str(tx.get("quantity", "1")))
    return int(m.group(1)) if m else 1


def _is_debt_settlement(tx):
    """True when a transaction is a debt repayment (in or out), not a real
    sale/expense. These are recorded as sale/expense rows for cash tracking but
    must be kept OUT of the P&L (revenue/expense) — the P&L already recognised
    the original credit sale/purchase. They're a cash event + a receivable/
    payable reduction."""
    desc = str(tx.get("description", "")).lower()
    cat = str(tx.get("category", "")).lower()
    return ("debt payment" in desc or "debt repayment" in desc
            or cat == "debt repayment")


def _payment_method(tx):
    return str(tx.get("payment_method", "")).lower()


def _cash_received(tx):
    """Cash actually received on a SALE tx (accrual-agnostic cash view):
      * cash/transfer sale  -> full amount
      * deposit sale        -> the deposit portion only (balance is a receivable)
      * credit sale         -> 0 (nothing received yet)
      * debt repayment (in) -> full amount (collection)
    """
    if _is_debt_settlement(tx):
        return int(float(tx.get("amount", 0)))
    pm = _payment_method(tx)
    if pm == "credit":
        return 0
    if pm == "deposit":
        return int(float(tx.get("deposit_amount", 0)))
    return int(float(tx.get("amount", 0)))


def _cash_paid(tx):
    """Cash actually paid on a PURCHASE/EXPENSE tx (mirror of _cash_received)."""
    if _is_debt_settlement(tx):
        return int(float(tx.get("amount", 0)))
    pm = _payment_method(tx)
    if pm == "credit":
        return 0
    if pm == "deposit":
        return int(float(tx.get("deposit_amount", 0)))
    return int(float(tx.get("amount", 0)))


def product_avg_cost(product):
    """Read a product's weighted-average unit cost.

    Lazy migration: older products only have `landing_cost`. When `avg_cost`
    isn't set yet, we treat the existing `landing_cost` as the opening average
    (read-through normalizer per the roadmap discipline — no destructive
    rewrite). Returns 0 when nothing is on record.
    """
    if not isinstance(product, dict):
        return 0
    if product.get("avg_cost") not in (None, "", 0, "0"):
        return _to_int(product.get("avg_cost"))
    return _to_int(product.get("landing_cost"))


class Accounting:
    """Shared computation engine. Constructed with a database handle; the
    catalog cost lookup is reused from CatalogHandler so cost resolution matches
    the sale flow exactly."""

    def __init__(self, database, session_mgr=None):
        self.db = database
        self.session = session_mgr

    # ── Cost resolution ─────────────────────────────────────────────────

    def cogs_for_sale(self, sale_tx, product=None):
        """Return (cost_total, source) for a single SALE transaction.

        Resolution order (most specific first):
          1. The sale's own recorded landing cost (specific-identification).
          2. The product's weighted-average unit cost * qty.
          3. Catalog landing_cost lookup * qty.
          4. MISSING (0, flagged) — never faked.
        """
        qty = _qty_of(sale_tx)

        # 1) Specific cost recorded on the sale itself.
        extra = sale_tx.get("extra_details", {}) or {}
        lc = extra.get("landing_cost")
        if lc in (None, ""):
            lc = sale_tx.get("landing_cost")
        if lc not in (None, "") and _to_int(lc) > 0:
            # Match the sale-flow convention: a landing_cost_per_unit marker means
            # landing_cost is already the TOTAL; otherwise it's per-unit.
            has_per_unit = extra.get("landing_cost_per_unit") or sale_tx.get("landing_cost_per_unit")
            total = _to_int(lc) if has_per_unit else _to_int(lc) * qty
            return total, COST_SALE_LANDING

        # 1b) Variant-TREE leaf cost. For a tree product, cost lives on the LEAF
        #     node, not the product level (product landing_cost/avg_cost are 0 by
        #     design). If the sale recorded which leaf it hit, look up that leaf's
        #     weighted-average cost. Without this, tree-product sales fall through
        #     to MISSING and profit is massively overstated.
        leaf_path = extra.get("variant") or extra.get("catalog_path") or sale_tx.get("variant")
        if isinstance(leaf_path, (list, tuple)):
            leaf_path = " / ".join(str(x) for x in leaf_path)
        has_tree = isinstance(product, dict) and isinstance(product.get("variant_tree"), dict) \
            and (product.get("variant_tree") or {}).get("children")
        if has_tree and leaf_path:
            try:
                from features.catalog import CatalogHandler
                cat = CatalogHandler(self.session, self.db)
                pkey = extra.get("catalog_product") or product.get("_key") or ""
                if not pkey:
                    # Resolve the product key from the catalog if not stored.
                    products = self._products(sale_tx.get("phone_number", ""))
                    pkey = cat._find_product_key(products, product.get("name", "")) or ""
                leaf_c = cat.leaf_cost(sale_tx.get("phone_number", ""), pkey, leaf_path) if pkey else 0
                if leaf_c and leaf_c > 0:
                    return leaf_c * qty, COST_WEIGHTED_AVG
            except Exception as e:
                logger.debug(f"tree leaf cost lookup failed: {e}")

        # 2) Product weighted-average cost.
        if product is not None:
            avg = product_avg_cost(product)
            if avg > 0:
                return avg * qty, COST_WEIGHTED_AVG

        # 3) Catalog landing_cost fallback (by name/brand), reusing the exact
        #    lookup the sale flow uses so numbers agree.
        try:
            from features.catalog import CatalogHandler
            cat = CatalogHandler(self.session, self.db)
            desc = sale_tx.get("description", sale_tx.get("item_name", "")) or ""
            brand = sale_tx.get("brand", "") or ""
            search = f"{brand} {desc}".strip() if brand else desc
            cc = cat.get_landing_cost(sale_tx.get("phone_number", ""), search) if search else 0
            if cc and cc > 0:
                return cc * qty, COST_CATALOG
        except Exception as e:
            logger.debug(f"catalog cost lookup failed: {e}")

        # 4) Nothing known.
        return 0, COST_MISSING

    # ── Profit & Loss (accrual) ─────────────────────────────────────────

    def period_pnl(self, phone_number, start_date, end_date, label=""):
        """Accrual P&L for a period. COGS is the cost of goods SOLD (per-sale,
        weighted-average), NOT total purchases.

        Returns a dict with the headline figures + an `uncosted_sales` list so
        callers can flag integrity honestly.
        """
        txns = self.db.get_transactions_by_period(phone_number, start_date, end_date) or []

        # Revenue = real sales only. A debt repayment is recorded as a `sale`
        # transaction (money collected) but its revenue was ALREADY recognised
        # at the original credit sale — counting it again would double-count.
        # Exclude debt-payment/repayment rows from the P&L (they're a cash event,
        # handled in period_cashflow, and a receivables reduction, not revenue).
        sales = [t for t in txns if t.get("type") == "sale"
                 and not _is_debt_settlement(t)]
        # Operating expenses = expenses that are NOT cost-of-goods inputs AND not
        # debt repayments (a repayment settles a payable, it's not a P&L expense).
        opex_txns = [t for t in txns if t.get("type") == "expense"
                     and t.get("category") not in COGS_CATEGORIES
                     and not _is_debt_settlement(t)]

        # Catalog products for weighted-average cost resolution.
        products = self._products(phone_number)

        revenue = sum(_to_int(t.get("amount", 0)) for t in sales)
        opex = sum(_to_int(t.get("amount", 0)) for t in opex_txns)

        cogs = 0
        costed_revenue = 0
        uncosted_sales = []
        cost_sources = {COST_SALE_LANDING: 0, COST_WEIGHTED_AVG: 0, COST_CATALOG: 0}
        for s in sales:
            product = self._match_product(products, s)
            cost, source = self.cogs_for_sale(s, product)
            if source == COST_MISSING:
                uncosted_sales.append(s)
                continue
            cogs += cost
            costed_revenue += _to_int(s.get("amount", 0))
            cost_sources[source] = cost_sources.get(source, 0) + 1

        gross_profit = costed_revenue - cogs   # margin on the SOLD-and-costed goods
        net_profit = revenue - cogs - opex     # note: uncosted sales inflate this;
                                               # the uncosted flag discloses that.

        def pct(numer, denom):
            return int(numer / denom * 100) if denom else 0

        return {
            "label": label,
            "start": start_date,
            "end": end_date,
            "revenue": revenue,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "gross_margin_pct": pct(gross_profit, costed_revenue),
            "opex": opex,
            "net_profit": net_profit,
            "net_margin_pct": pct(net_profit, revenue),
            "costed_revenue": costed_revenue,
            "uncosted_count": len(uncosted_sales),
            "uncosted_sales": uncosted_sales,
            "cost_sources": cost_sources,
            "sales_count": len(sales),
            "tx_count": len(txns),
            "sales": sales,
            "opex_txns": opex_txns,
        }

    def product_margins(self, phone_number, start_date, end_date, top=10):
        """Per-product margin for a period: revenue, COGS (weighted-avg), margin,
        units sold — ranked by revenue. Excludes debt-settlement rows. Uncosted
        sales still contribute revenue but 0 cost (flagged via has_uncosted)."""
        txns = self.db.get_transactions_by_period(phone_number, start_date, end_date) or []
        products = self._products(phone_number)
        agg = {}
        for t in txns:
            if t.get("type") != "sale" or _is_debt_settlement(t):
                continue
            name = (t.get("item_name") or t.get("description") or "Item").strip()
            key = name.lower()
            product = self._match_product(products, t)
            cost, source = self.cogs_for_sale(t, product)
            rev = _to_int(t.get("amount", 0))
            qty = _qty_of(t)
            row = agg.setdefault(key, {"name": name, "revenue": 0, "cogs": 0,
                                       "qty": 0, "has_uncosted": False})
            row["revenue"] += rev
            row["qty"] += qty
            if source == COST_MISSING:
                row["has_uncosted"] = True
            else:
                row["cogs"] += cost
        rows = []
        for r in agg.values():
            r["margin"] = r["revenue"] - r["cogs"]
            r["margin_pct"] = int(r["margin"] / r["revenue"] * 100) if r["revenue"] else 0
            rows.append(r)
        rows.sort(key=lambda x: x["revenue"], reverse=True)
        return rows[:top]

    # ── Cash Flow (paid-only) ───────────────────────────────────────────

    def period_cashflow(self, phone_number, start_date, end_date, label=""):
        """Cash flow for a period — money ACTUALLY received vs ACTUALLY paid.

        Unlike the P&L (accrual), this counts real money movement:
          IN  = cash/transfer sales (full) + deposit portions + debt collected.
          OUT = cash/transfer purchases + expenses paid (full) + deposit portions
                + debt repaid.
        Credit sales/purchases contribute 0 until paid (their unpaid balance is a
        receivable/payable, reported by the position report in R5). This is where
        buying unsold stock correctly shows as cash OUT.
        """
        txns = self.db.get_transactions_by_period(phone_number, start_date, end_date) or []

        cash_in = 0
        cash_out = 0
        collected = 0   # debt collected (subset of cash_in)
        repaid = 0      # debt repaid (subset of cash_out)
        for t in txns:
            ttype = t.get("type")
            if ttype in ("sale", "income"):
                amt = _cash_received(t)
                cash_in += amt
                if _is_debt_settlement(t):
                    collected += amt
            elif ttype in ("purchase", "expense"):
                amt = _cash_paid(t)
                cash_out += amt
                if _is_debt_settlement(t):
                    repaid += amt

        return {
            "label": label,
            "start": start_date,
            "end": end_date,
            "cash_in": cash_in,
            "cash_out": cash_out,
            "net_cash": cash_in - cash_out,
            "debt_collected": collected,
            "debt_repaid": repaid,
            "tx_count": len(txns),
        }

    def profit_trend(self, phone_number, months=6):
        """Net-profit series over the last `months` calendar months (oldest→
        newest), each computed via period_pnl so it's accrual-correct. Returns
        [(label, net_profit), ...] e.g. [("Apr", 120000), ("May", -5000), ...].
        Powers the trend chart (N2)."""
        from datetime import date
        try:
            from dateutil.relativedelta import relativedelta
        except Exception:
            relativedelta = None
        today = date.today()
        series = []
        for i in range(months - 1, -1, -1):
            if relativedelta is not None:
                first = (today.replace(day=1) - relativedelta(months=i))
            else:
                # Fallback: crude month stepping without dateutil.
                y, m = today.year, today.month - i
                while m <= 0:
                    m += 12
                    y -= 1
                first = date(y, m, 1)
            # last day of that month
            if relativedelta is not None:
                last = first + relativedelta(months=1) - relativedelta(days=1)
            else:
                ny, nm = (first.year + (1 if first.month == 12 else 0),
                          1 if first.month == 12 else first.month + 1)
                from datetime import timedelta
                last = date(ny, nm, 1) - timedelta(days=1)
            pnl = self.period_pnl(phone_number, first.isoformat(), last.isoformat(),
                                  first.strftime("%b"))
            series.append((first.strftime("%b"), pnl["net_profit"]))
        return series

    # ── Position / Inventory (balance-sheet snapshot) ───────────────────

    def position(self, phone_number, as_of=None):
        """A point-in-time position snapshot:
          * inventory_value  — stock on hand valued at weighted-average cost
          * inventory_units  — total units on hand
          * receivables      — total owed TO the user (debtors)
          * payables         — total the user OWES (creditors)
          * net_worth_proxy  — inventory + receivables − payables (a rough
                               owner's-equity signal, NOT a full balance sheet)

        Inventory is valued per product, walking (in order): a variant TREE's
        leaves (stock × leaf cost), else flat variant_stock × variant_costs, else
        the base stock × weighted-avg cost (landing_cost). Non-sellable items
        (raw materials/overhead) are still stock you hold, so they're included;
        callers can break them out later if needed.
        """
        products = self._products(phone_number)
        inv_value = 0
        inv_units = 0
        priced_items = []   # (name, units, value) for the top-items breakdown

        for key, p in (products.items() if isinstance(products, dict) else []):
            if not isinstance(p, dict):
                continue
            name = p.get("name", key)
            units, value = self._value_product(p)
            if units or value:
                inv_units += units
                inv_value += value
                priced_items.append((name, units, value))

        # Receivables / payables from the debt ledger (contacts).
        receivables = payables = 0
        try:
            receivables = sum(int(d.get("amount", 0)) for d in
                              (self.db.get_all_debtors(phone_number) or []))
            payables = sum(int(c.get("amount", 0)) for c in
                           (self.db.get_all_creditors(phone_number) or []))
        except Exception as e:
            logger.debug(f"position debt read failed: {e}")

        priced_items.sort(key=lambda x: x[2], reverse=True)

        return {
            "inventory_value": inv_value,
            "inventory_units": inv_units,
            "receivables": receivables,
            "payables": payables,
            "net_worth_proxy": inv_value + receivables - payables,
            "item_count": len(priced_items),
            "top_items": priced_items[:10],
        }

    def _value_product(self, product):
        """Return (units, value_at_cost) for one product across its stock model."""
        tree = product.get("variant_tree")
        if isinstance(tree, dict) and tree.get("children"):
            return self._value_tree(tree)

        # Flat variants: variant_stock{} × variant_costs{} (fall back to base cost).
        vstock = product.get("variant_stock") or {}
        if isinstance(vstock, dict) and vstock:
            vcosts = product.get("variant_costs") or {}
            base_cost = product_avg_cost(product)
            units = value = 0
            for variant, qty in vstock.items():
                q = _to_int(qty)
                c = _to_int(vcosts.get(variant, base_cost))
                units += q
                value += q * c
            return units, value

        # Base product: stock × weighted-average cost.
        qty = _to_int(product.get("stock", 0))
        cost = product_avg_cost(product)
        return qty, qty * cost

    def _value_tree(self, node):
        """Recursively value a variant tree: leaves contribute stock × cost."""
        children = node.get("children") or {}
        if not children:
            q = _to_int(node.get("stock", 0))
            c = _to_int(node.get("cost", 0))
            return q, q * c
        units = value = 0
        for child in children.values():
            u, v = self._value_tree(child)
            units += u
            value += v
        return units, value

    # ── Helpers ─────────────────────────────────────────────────────────

    def _products(self, phone_number):
        """Product catalog dict {key: product}. Empty on any miss."""
        user = self.db.get_user(phone_number) or {}
        catalog = user.get("product_catalog", {})
        if isinstance(catalog, dict):
            return catalog.get("products", {}) or {}
        return {}

    def _match_product(self, products, tx):
        """Find the catalog product for a sale tx (best-effort, name/brand).
        Reuses CatalogHandler's fuzzy matcher so it agrees with the sale flow."""
        if not products:
            return None
        desc = tx.get("item_name", "") or tx.get("description", "") or ""
        brand = tx.get("brand", "") or ""
        search = f"{brand} {desc}".strip() if brand else desc
        if not search:
            return None
        try:
            from features.catalog import CatalogHandler
            cat = CatalogHandler(self.session, self.db)
            key = cat._find_product_key(products, search)
            if not key:
                return None
            prod = products.get(key)
            # Stamp the key so callers (e.g. tree-leaf cost lookup) can address
            # the product without re-resolving. Don't mutate the shared dict in
            # a way that persists — a shallow copy is enough for read use.
            if isinstance(prod, dict) and "_key" not in prod:
                prod = dict(prod)
                prod["_key"] = key
            return prod
        except Exception:
            return None
