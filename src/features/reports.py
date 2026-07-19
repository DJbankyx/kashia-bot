# src/features/reports.py
"""Reports — P&L summary, business tabs, and filtered transaction views."""

import logging
from datetime import datetime, timedelta
from utils.whatsapp_ui import (
    text_response, button_response, list_response, format_amount
)

logger = logging.getLogger(__name__)

# Categories that represent Cost of Goods Sold (stock purchased to resell)
COGS_CATEGORIES = {
    "Goods & Stock",
    "Production & Manufacturing",
    "Service Costs",
}

# Bad vendor names that are really transaction verbs — filter these out of displays
BAD_VENDORS = {
    "sold", "bought", "paid", "received", "sale", "purchase",
    "expense", "income", "cash", "transfer",
}


class ReportsHandler:
    """Handles all report generation and display."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    # ─────────────────────────────────────────────────────────
    # ENTRY — period selector menu
    # ─────────────────────────────────────────────────────────

    def show(self, phone_number: str) -> list:
        """Show report period options."""
        return [list_response(
            header="📊 Reports",
            body="Which report would you like?",
            button_text="Select Period",
            sections=[{
                "title": "Time Period",
                "rows": [
                    {"id": "report_today", "title": "📅 Today",
                     "description": "Today's P&L summary"},
                    {"id": "report_week",  "title": "📆 This Week",
                     "description": "Last 7 days"},
                    {"id": "report_month", "title": "🗓️ This Month",
                     "description": now_month_label()},
                    {"id": "report_last_month", "title": "📅 Last Month",
                     "description": "Previous month summary"},
                ]
            }]
        )]

    # ─────────────────────────────────────────────────────────
    # BUTTON ROUTER
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Route all report_ and biz_ buttons."""
        # ── Period reports ──
        if button_id == "report_today":
            return self._pnl_report(phone_number, "today")
        if button_id == "report_week":
            return self._pnl_report(phone_number, "week")
        if button_id == "report_month":
            return self._pnl_report(phone_number, "month")
        if button_id == "report_last_month":
            return self._pnl_report(phone_number, "last_month")

        # ── Business tab buttons ──
        if button_id == "biz_sales":
            return self._tab_report(phone_number, "sale")
        if button_id == "biz_purchases":
            return self._tab_report(phone_number, "purchase")
        if button_id == "biz_expenses":
            return self._tab_report(phone_number, "expense")
        if button_id == "biz_reports":
            return self.show(phone_number)

        # ── Export from report (period stored in session context) ──
        if button_id.startswith("report_export_"):
            period = button_id.replace("report_export_", "")
            return self._export_report(phone_number, period)

        # ── PDF export from report ──
        if button_id.startswith("report_pdf_"):
            return [{"type": "__EXPORT_PDF_STATEMENT__", "content": {}}]

        # ── Edit records from tab (report_edit_sale, report_edit_purchase, etc) ──
        if button_id.startswith("report_edit_"):
            tx_type = button_id.replace("report_edit_", "")
            return [{"type": "__EDIT_RECORDS__", "content": {"tx_type": tx_type}}]

        return self.show(phone_number)

    # ─────────────────────────────────────────────────────────
    # REPORT A — Full P&L (the main report)
    # ─────────────────────────────────────────────────────────

    def _pnl_report(self, phone_number: str, period: str) -> list:
        """
        Generate the proper P&L report in DUAL FORMAT.

        Format A — Cash Flow:
          Revenue (Sales) - Purchases - Operating Expenses = Net Cash P&L

        Format B — True Accounting (Gross Margin):
          Revenue (Sales) - COGS (landing cost × qty) = Gross Profit - OpEx = Net Profit
        """
        start_date, end_date, label = _date_range(period)
        transactions = self.db.get_transactions_by_period(
            phone_number, start_date, end_date
        ) or []

        if not transactions:
            return [text_response(
                f"📊 *{label}*\n\n"
                f"No transactions recorded yet.\n\n"
                f"_Start recording sales and expenses to see your P&L._"
            )]

        # ── Separate into buckets ──
        sales       = [t for t in transactions if t.get("type") == "sale"]
        purchases   = [t for t in transactions if t.get("type") == "purchase"]
        expenses    = [t for t in transactions if t.get("type") == "expense"
                       and t.get("category") not in COGS_CATEGORIES]
        cogs_txns   = [t for t in transactions if t.get("type") == "expense"
                       and t.get("category") in COGS_CATEGORIES]
        # Purchases are always COGS for trading
        all_cogs    = purchases + cogs_txns

        # ── Totals ──
        revenue     = _sum(sales)
        cogs        = _sum(all_cogs)
        gross       = revenue - cogs
        opex        = _sum(expenses)
        net         = gross - opex
        tx_count    = len(transactions)

        # ── Gross margin % ──
        gross_pct   = f"{int(gross / revenue * 100)}%" if revenue > 0 else "—"
        net_pct     = f"{int(net / revenue * 100)}%" if revenue > 0 else "—"

        # ════════════════════════════════════════════════════
        # FORMAT A — Cash Flow Style
        # ════════════════════════════════════════════════════
        lines = [
            f"📊 *{label} — P&L Report*",
            f"",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"💰 *REVENUE*",
            f"  Sales:        {format_amount(revenue)}",
            f"",
            f"📦 *COST OF GOODS SOLD*",
            f"  Purchases:    {format_amount(cogs)}",
            f"",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"{'📈' if gross >= 0 else '📉'} *GROSS PROFIT*   "
            f"{format_amount(gross)} _({gross_pct})_",
            f"",
            f"💸 *OPERATING EXPENSES*",
            f"  Expenses:     {format_amount(opex)}",
            f"",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        # Net profit line
        if net >= 0:
            lines.append(
                f"📈 *NET PROFIT*      {format_amount(net)} _({net_pct})_"
            )
        else:
            lines.append(
                f"📉 *NET LOSS*       ({format_amount(abs(net))}) _({net_pct})_"
            )

        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"")
        lines.append(f"📝 {tx_count} transaction{'s' if tx_count != 1 else ''}")

        # ── Top expense categories breakdown ──
        if expenses:
            cat_totals = {}
            for t in expenses:
                cat = t.get("category", "Other")
                cat_totals[cat] = cat_totals.get(cat, 0) + float(t.get("amount", 0))
            top = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)[:4]
            lines.append(f"")
            lines.append(f"*Top Expenses:*")
            for cat, amt in top:
                pct = int(amt / opex * 100) if opex > 0 else 0
                lines.append(f"  • {cat}: {format_amount(amt)} ({pct}%)")

        responses = [text_response("\n".join(lines))]

        # ════════════════════════════════════════════════════
        # FORMAT B — True Margin (Landing Cost based)
        # Uses landing_cost stored on sales OR from catalog lookup
        # ════════════════════════════════════════════════════
        margin_report = self._build_margin_report_v2(phone_number, sales, label)
        if margin_report:
            responses.append(text_response(margin_report))

        # ════════════════════════════════════════════════════
        # MANUFACTURING: Production summary (if user has production records)
        # ════════════════════════════════════════════════════
        production_txns = [t for t in transactions if t.get("type") == "production"]
        if production_txns:
            prod_report = self._build_production_summary(production_txns, label)
            if prod_report:
                responses.append(text_response(prod_report))

        responses.append(button_response(
            "Export or drill into a section:",
            [
                {"id": f"report_pdf_{period}", "title": "📄 Download PDF"},
                {"id": f"report_export_{period}", "title": "📎 Export Excel"},
                {"id": "menu_home",    "title": "☰ Menu"},
            ]
        ))

        return responses

    # ─────────────────────────────────────────────────────────
    # BUSINESS TABS — Sales / Purchases / Expenses
    # ─────────────────────────────────────────────────────────

    def _tab_report(self, phone_number: str, tab_type: str) -> list:
        """
        Filtered view for one tab — Sales, Purchases, or Expenses.
        Shows this month by default with period switcher buttons.
        """
        now = datetime.now()
        start_date = now.strftime("%Y-%m-01")
        end_date   = now.strftime("%Y-%m-%d")
        label      = now.strftime("%B %Y")

        all_txns = self.db.get_transactions_by_period(
            phone_number, start_date, end_date
        ) or []

        # Filter
        if tab_type == "sale":
            filtered = [t for t in all_txns if t.get("type") == "sale"]
            emoji    = "💰"
            tab_name = "Sales"
        elif tab_type == "purchase":
            filtered = [t for t in all_txns if t.get("type") == "purchase"]
            emoji    = "📦"
            tab_name = "Purchases"
        else:  # expense
            filtered = [t for t in all_txns if t.get("type") == "expense"]
            emoji    = "💸"
            tab_name = "Expenses"

        if not filtered:
            return [
                text_response(
                    f"{emoji} *{tab_name} — {label}*\n\n"
                    f"No {tab_name.lower()} recorded this month.\n\n"
                    f"_Record a transaction from the main menu._"
                )
            ]

        total   = _sum(filtered)
        count   = len(filtered)
        avg     = total / count if count > 0 else 0

        # Header
        lines = [
            f"{emoji} *{tab_name} — {label}*",
            f"",
            f"Total:    {format_amount(total)}",
            f"Count:    {count} transaction{'s' if count != 1 else ''}",
            f"Average:  {format_amount(avg)}",
            f"",
            f"*Records:*",
        ]

        # List each transaction — newest first, max 15
        sorted_txns = sorted(
            filtered,
            key=lambda t: t.get("created_at", t.get("date", "")),
            reverse=True
        )[:15]

        for t in sorted_txns:
            desc    = _clean_desc(t)
            amt     = format_amount(t.get("amount", 0))
            vendor  = t.get("vendor", "")
            vendor  = "" if vendor.lower() in BAD_VENDORS else vendor
            date_s  = t.get("date", "")[-5:]   # MM-DD
            vendor_str = f" · {vendor}" if vendor else ""
            lines.append(f"• {desc}{vendor_str} — {amt}  _{date_s}_")

        if count > 15:
            lines.append(f"\n_...and {count - 15} more. Export for full list._")

        # Map tab_type to edit button ID
        edit_btn_id = f"report_edit_{tab_type}"

        return [
            text_response("\n".join(lines)),
            button_response(
                "Actions:",
                [
                    {"id": edit_btn_id,             "title": "✏️ Edit Records"},
                    {"id": "report_month",          "title": "🗓️ Full P&L"},
                    {"id": "menu_home",             "title": "☰ Menu"},
                ]
            )
        ]

    # ─────────────────────────────────────────────────────────
    # REPORT B — True Margin (landing cost based)
    # ─────────────────────────────────────────────────────────

    def _build_margin_report(self, sales: list, period_label: str) -> str:
        """Legacy — redirect to v2."""
        return None  # Replaced by _build_margin_report_v2

    def _build_margin_report_v2(self, phone_number: str, sales: list, period_label: str) -> str:
        """
        Build Report B — True Gross Margin using landing_cost data.
        
        Checks two sources for cost:
        1. landing_cost stored on the transaction itself
        2. landing_cost stored on the catalog product (fallback)
        
        Returns formatted text string or None if no data.
        """
        if not sales:
            return None

        # Get catalog for fallback cost lookup
        from features.catalog import CatalogHandler
        cat = CatalogHandler(self.session, self.db)

        costed_sales = []
        for t in sales:
            # Source 1: landing_cost on the transaction
            extra = t.get("extra_details", {}) or {}
            lc = extra.get("landing_cost") or t.get("landing_cost")

            # Source 2: Lookup from catalog by description/product name
            if not lc or int(lc) <= 0:
                desc = t.get("description", t.get("item_name", ""))
                brand = t.get("brand", "")
                search_name = f"{brand} {desc}".strip() if brand else desc
                catalog_cost = cat.get_landing_cost(phone_number, search_name)
                if catalog_cost > 0:
                    lc = catalog_cost
                    # Calculate total cost based on quantity
                    qty_str = t.get("quantity", "1")
                    qty = 1
                    if qty_str:
                        import re
                        match = re.match(r'^(\d+)', str(qty_str))
                        qty = int(match.group(1)) if match else 1
                    lc = catalog_cost * qty  # Total cost = unit cost × qty

            if lc and int(lc) > 0:
                costed_sales.append({
                    "description": t.get("description", t.get("item_name", "Item")),
                    "revenue": int(t.get("amount", 0)),
                    "cost": int(lc),
                    "vendor": t.get("vendor", ""),
                })

        if not costed_sales:
            return None  # No landing cost data — skip Report B

        total_revenue = sum(s["revenue"] for s in costed_sales)
        total_cost    = sum(s["cost"] for s in costed_sales)
        total_margin  = total_revenue - total_cost
        margin_pct    = int(total_margin / total_revenue * 100) if total_revenue > 0 else 0
        uncosted      = len(sales) - len(costed_sales)

        # Also calculate net after expenses
        # (expenses already shown in Format A, so this shows the accounting view)
        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📈  *TRUE MARGIN — {period_label}*",
            f"_(Proper Accounting View)_",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"_Based on {len(costed_sales)} sale{'s' if len(costed_sales) != 1 else ''} with recorded/catalog cost_",
            f"",
            f"💰 Revenue:       {format_amount(total_revenue)}",
            f"🏷️ COGS (Cost):   {format_amount(total_cost)}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📈 *Gross Profit:  {format_amount(total_margin)}  ({margin_pct}%)*",
            f"",
        ]

        # Show per-item breakdown (top 5)
        if len(costed_sales) > 1:
            lines.append("*Item Margins:*")
            sorted_items = sorted(costed_sales, key=lambda x: x["revenue"], reverse=True)
            for s in sorted_items[:5]:
                item_margin = s["revenue"] - s["cost"]
                item_pct    = int(item_margin / s["revenue"] * 100) if s["revenue"] > 0 else 0
                desc        = s["description"][:20]
                lines.append(
                    f"  • {desc}: {format_amount(s['revenue'])} - {format_amount(s['cost'])} "
                    f"= {format_amount(item_margin)} ({item_pct}%)"
                )
            lines.append("")

        if uncosted > 0:
            lines.append(
                f"⚠️ _{uncosted} sale{'s' if uncosted != 1 else ''} without cost data "
                f"(not included above)_"
            )

        lines.append("━━━━━━━━━━━━━━━━━━━━")

        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────
    # EXPORT from report button
    # ─────────────────────────────────────────────────────────

    def _export_report(self, phone_number: str, period: str) -> list:
        """Trigger Excel export for a given period."""
        # Export service is wired in main.py — use the existing export handler
        # We return a routing marker that main.py resolves
        return [{"type": "__EXPORT_REPORT__", "content": {"period": period}}]

    # ─────────────────────────────────────────────────────────
    # PRODUCTION SUMMARY — Manufacturing P&L addon
    # ─────────────────────────────────────────────────────────

    def _build_production_summary(self, production_txns: list, period_label: str) -> str:
        """
        Build production summary for manufacturing users.
        Shows: batches, total output, yield rate, cost breakdown.
        """
        total_produced = 0
        total_waste = 0
        total_cost = 0
        batch_count = len(production_txns)

        for p in production_txns:
            extra = p.get("extra_details", {}) or {}
            good_qty = int(extra.get("good_quantity", p.get("quantity", 0)) or 0)
            waste = int(extra.get("waste", 0) or 0)
            total_produced += good_qty
            total_waste += waste
            total_cost += int(p.get("amount", 0))

        total_attempted = total_produced + total_waste
        yield_rate = int(total_produced / total_attempted * 100) if total_attempted > 0 else 100
        cost_per_unit = total_cost / total_produced if total_produced > 0 else 0

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🏭  *PRODUCTION — {period_label}*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"🔄 Batches:     {batch_count}",
            f"📦 Output:      {total_produced} units",
            f"🗑️ Waste:       {total_waste} units",
            f"✅ Yield Rate:  {yield_rate}%",
            f"",
            f"💰 Total Cost:  {format_amount(total_cost)}",
            f"📐 Cost/Unit:   {format_amount(cost_per_unit)}",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# HELPERS (module-level, no state)
# ─────────────────────────────────────────────────────────

def _sum(transactions: list) -> float:
    """Sum amounts from a transaction list."""
    return sum(float(t.get("amount", 0)) for t in transactions)


def _date_range(period: str):
    """Return (start_date, end_date, label) for a named period."""
    now = datetime.now()

    if period == "today":
        d = now.strftime("%Y-%m-%d")
        return d, d, "Today"

    if period == "week":
        start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        end   = now.strftime("%Y-%m-%d")
        return start, end, "Last 7 Days"

    if period == "last_month":
        first_this = now.replace(day=1)
        last_month_end   = first_this - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        return (
            last_month_start.strftime("%Y-%m-%d"),
            last_month_end.strftime("%Y-%m-%d"),
            last_month_end.strftime("%B %Y"),
        )

    # Default: this month
    start = now.strftime("%Y-%m-01")
    end   = now.strftime("%Y-%m-%d")
    return start, end, now.strftime("%B %Y")


def now_month_label() -> str:
    return datetime.now().strftime("%B %Y")


def _clean_desc(tx: dict) -> str:
    """Get the best short description for a transaction row."""
    # Prefer structured fields over raw text
    item = tx.get("item_name", "")
    brand = tx.get("brand", "")
    if item and brand:
        return f"{brand} {item}"[:30]
    if item:
        return item[:30]
    desc = tx.get("description", tx.get("raw_text", ""))
    # Strip common prefixes
    import re
    desc = re.sub(r'^(?:sold|bought|paid|received)\s+', '', desc,
                  flags=re.IGNORECASE)
    return desc[:30].strip() or "Transaction"
