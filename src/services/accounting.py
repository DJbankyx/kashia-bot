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

        sales = [t for t in txns if t.get("type") == "sale"]
        # Operating expenses = expenses that are NOT cost-of-goods inputs.
        opex_txns = [t for t in txns if t.get("type") == "expense"
                     and t.get("category") not in COGS_CATEGORIES]

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
            return products.get(key) if key else None
        except Exception:
            return None
