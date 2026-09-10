# src/features/reports.py
"""Reports — P&L summary, business tabs, and filtered transaction views."""

import logging
from datetime import datetime, timedelta
from utils.whatsapp_ui import (
    text_response, button_response, list_response, format_amount
)
from utils.parser import is_bad_vendor

logger = logging.getLogger(__name__)

# Categories that represent Cost of Goods Sold (stock purchased to resell)
COGS_CATEGORIES = {
    "Goods & Stock",
    "Production & Manufacturing",
    "Service Costs",
}


class ReportsHandler:
    """Handles all report generation and display."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    def _is_telegram(self, phone_number: str) -> bool:
        try:
            from services.messaging_client import platform_for_user
            return platform_for_user(phone_number) == "telegram"
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────
    # ENTRY — period selector menu
    # ─────────────────────────────────────────────────────────

    def show(self, phone_number: str) -> list:
        """Show report entry. Telegram gets the tap-first in-place DASHBOARD
        (Stage 3); WhatsApp keeps the classic period-selector list."""
        if self._is_telegram(phone_number):
            return self.dashboard(phone_number, "month")
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
        """Route all report_, dash_ and biz_ buttons."""
        # ── Stage 3 dashboard (Telegram) ──
        if button_id == "menu_dashboard" or button_id == "dash_open":
            return self.dashboard(phone_number, "month")
        if button_id.startswith("dash_period_"):
            return self.dashboard(phone_number, button_id[len("dash_period_"):])
        if button_id.startswith("dash_drill_"):
            # dash_drill_<what>_<period>
            rest = button_id[len("dash_drill_"):]
            what, _, period = rest.partition("_")
            return self._dash_drill(phone_number, what, period or "month")

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
            # Carry the period so the statement PDF matches what the user is
            # viewing (dashboard/report period) instead of always defaulting.
            period = button_id[len("report_pdf_"):] or "month"
            return [{"type": "__EXPORT_PDF_STATEMENT__", "content": {"period": period}}]

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

        # ── Buckets kept for the downstream sub-reports (hybrid split, margin,
        #    production). The headline numbers now come from the shared engine. ──
        sales       = [t for t in transactions if t.get("type") == "sale"]
        purchases   = [t for t in transactions if t.get("type") == "purchase"]
        expenses    = [t for t in transactions if t.get("type") == "expense"
                       and t.get("category") not in COGS_CATEGORIES]
        cogs_txns   = [t for t in transactions if t.get("type") == "expense"
                       and t.get("category") in COGS_CATEGORIES]
        tx_count    = len(transactions)

        user = self.db.get_user(phone_number) or {}
        industry = user.get("industry_class", user.get("business_type", "trading"))

        # ── Accrual P&L from the shared accounting engine (single source) ──
        from services.accounting import Accounting
        acct = Accounting(self.db, self.session)
        pnl = acct.period_pnl(phone_number, start_date, end_date, label)
        revenue  = pnl["revenue"]
        cogs     = pnl["cogs"]            # cost of goods SOLD (accrual), NOT purchases
        gross    = pnl["gross_profit"]
        opex     = pnl["opex"]
        net      = pnl["net_profit"]
        uncosted = pnl["uncosted_count"]

        # Industry-specific labels
        if industry == "manufacturing":
            cogs_label = "Cost of Output (sold)"
            revenue_label = "Output Sales"
        elif industry == "services":
            cogs_label = "Job Costs"
            revenue_label = "Service Revenue"
        else:
            cogs_label = "Cost of Goods Sold"
            revenue_label = "Sales"

        gm_pct  = f"{int(gross / pnl['costed_revenue'] * 100)}%" if pnl["costed_revenue"] > 0 else "—"
        net_pct = f"{int(net / revenue * 100)}%" if revenue > 0 else "—"

        # ════════════════════════════════════════════════════
        # SECTION 1 — Profit & Loss (ACCRUAL)
        # Revenue − COGS(of goods SOLD) − Expenses = Net Profit.
        # A stock purchase you haven't sold is inventory, not an expense here.
        # ════════════════════════════════════════════════════
        lines = [
            f"📊 *{label} — Profit & Loss*",
            f"",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"💰 {revenue_label}:  *{format_amount(revenue)}*",
            f"📦 {cogs_label}:  {format_amount(cogs)}",
            f"🟢 *Gross Profit:*  {format_amount(gross)}  _({gm_pct})_",
            f"💸 Operating Expenses:  {format_amount(opex)}",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]
        if net >= 0:
            lines.append(f"📈 *NET PROFIT:*  +{format_amount(net)}  _({net_pct})_")
        else:
            lines.append(f"📉 *NET LOSS:*  −{format_amount(abs(net))}")
        if uncosted > 0:
            lines.append(
                f"\n_⚠️ {uncosted} sale(s) have no recorded cost — excluded from "
                f"COGS/margin. Set their cost for an accurate profit._"
            )

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

        # ════════════════════════════════════════════════════
        # SECTION 2 — Cash Flow (money ACTUALLY in vs out, paid-only)
        # Credit sales/purchases contribute 0 until paid; deposits count only the
        # paid portion; debt collections/repayments count as cash. This is where
        # buying unsold stock correctly shows as cash out.
        # ════════════════════════════════════════════════════
        cf = acct.period_cashflow(phone_number, start_date, end_date, label)
        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"💵 *Cash Flow* _(money actually moved)_")
        lines.append(f"  Cash in:   {format_amount(cf['cash_in'])}")
        lines.append(f"  Cash out:  {format_amount(cf['cash_out'])}")
        if cf["debt_collected"]:
            lines.append(f"    _incl. {format_amount(cf['debt_collected'])} debt collected_")
        if cf["debt_repaid"]:
            lines.append(f"    _incl. {format_amount(cf['debt_repaid'])} debt repaid_")
        if cf["net_cash"] >= 0:
            lines.append(f"  *Net cash:*  +{format_amount(cf['net_cash'])}")
        else:
            lines.append(f"  *Net cash:*  −{format_amount(abs(cf['net_cash']))}")
        lines.append(f"")
        lines.append(f"📝 {tx_count} transaction{'s' if tx_count != 1 else ''}")
        lines.append(
            f"_P&L = profit on goods sold (accrual). Cash flow = money movement. "
            f"Credit sales count as profit but not yet cash; unsold stock is "
            f"inventory, not a loss._"
        )

        responses = [text_response("\n".join(lines))]

        # ════════════════════════════════════════════════════
        # HYBRID: Product vs Service revenue split
        # Uses the persisted sale_kind marker (falls back to a heuristic for
        # legacy sales), mirroring the hybrid dashboard.
        # ════════════════════════════════════════════════════
        if industry == "hybrid":
            split_report = self._build_hybrid_revenue_split(sales, label)
            if split_report:
                responses.append(text_response(split_report))

        # ════════════════════════════════════════════════════
        # REPORT B — True Profit Margin (Landing Cost based)
        # Shows actual profit per item sold (selling price - cost price)
        # Only available when landing costs are recorded
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
    # STAGE 3 — Shared totals + tap-first Telegram DASHBOARD
    # ─────────────────────────────────────────────────────────

    def _period_totals(self, phone_number: str, period: str) -> dict:
        """SINGLE source of truth for a period's headline numbers. Delegates the
        accrual P&L (revenue, COGS-of-SOLD, gross/net profit) to the shared
        services.accounting.Accounting engine so every surface agrees.

        Also computes the CASH view (money in/out) separately — the dashboard
        shows accrual profit as the headline and cash net as a secondary line.
        NOTE: `cogs` here is now the cost of goods SOLD (accrual), NOT the total
        of purchases (the old bug that made a big stock purchase look like a
        loss). The cash out for purchases lives in `cash_out`/`purchases`.
        """
        from services.accounting import Accounting
        start_date, end_date, label = _date_range(period)
        txns = self.db.get_transactions_by_period(phone_number, start_date, end_date) or []

        sales      = [t for t in txns if t.get("type") == "sale"]
        purchases  = [t for t in txns if t.get("type") == "purchase"]
        expenses   = [t for t in txns if t.get("type") == "expense"
                      and t.get("category") not in COGS_CATEGORIES]
        cogs_txns  = [t for t in txns if t.get("type") == "expense"
                      and t.get("category") in COGS_CATEGORIES]
        production = [t for t in txns if t.get("type") == "production"]

        # ── Accrual P&L (shared engine) ──
        acct = Accounting(self.db, self.session)
        pnl = acct.period_pnl(phone_number, start_date, end_date, label)

        revenue      = pnl["revenue"]
        cogs         = pnl["cogs"]              # cost of goods SOLD (accrual)
        opex         = pnl["opex"]
        net_profit   = pnl["net_profit"]        # accrual net — the headline
        gross_margin = pnl["gross_profit"]
        costed_rev   = pnl["costed_revenue"]
        uncosted     = pnl["uncosted_count"]

        # ── Cash view (money ACTUALLY moved, paid-only) from the shared engine ──
        cf = acct.period_cashflow(phone_number, start_date, end_date, label)
        cash_out = cf["cash_out"]
        net_cash = cf["net_cash"]

        # Debt position (who owes me / I owe) — receivables/payables.
        owed_to_me = owe_out = 0
        try:
            owed_to_me = sum(int(d.get("amount", 0)) for d in
                             (self.db.get_all_debtors(phone_number) or []))
            owe_out = sum(int(c.get("amount", 0)) for c in
                          (self.db.get_all_creditors(phone_number) or []))
        except Exception:
            pass

        user = self.db.get_user(phone_number) or {}
        industry = user.get("industry_class", user.get("business_type", "trading"))

        return {
            "period": period, "label": label,
            "revenue": revenue, "cogs": cogs, "opex": opex,
            # `net` = accrual net profit (the headline). Cash net is separate.
            "net": net_profit, "net_profit": net_profit,
            "cash_out": cash_out, "net_cash": net_cash,
            "gross_margin": gross_margin, "costed_revenue": costed_rev,
            "uncosted_sales": uncosted,
            "owed_to_me": owed_to_me, "owe_out": owe_out,
            "tx_count": len(txns), "sales_count": len(sales),
            "industry": industry,
            "sales": sales, "purchases": purchases, "expenses": expenses,
            "production": production,
        }

    def _costed_margin(self, phone_number: str, sales: list):
        """Return (total_gross_margin, costed_revenue, uncosted_count) using the
        same cost sources as the margin report: tx landing_cost, then catalog."""
        if not sales:
            return 0, 0, 0
        import re
        from features.catalog import CatalogHandler
        cat = CatalogHandler(self.session, self.db)
        total_margin = costed_rev = 0
        uncosted = 0
        for t in sales:
            extra = t.get("extra_details", {}) or {}
            lc = extra.get("landing_cost") or t.get("landing_cost")
            qty = 1
            m = re.match(r'^(\d+)', str(t.get("quantity", "1")))
            if m:
                qty = int(m.group(1))
            has_per_unit = extra.get("landing_cost_per_unit") or t.get("landing_cost_per_unit")
            if lc and int(lc) > 0 and not has_per_unit:
                lc = int(lc) * qty
            if not lc or int(lc) <= 0:
                desc = t.get("description", t.get("item_name", ""))
                brand = t.get("brand", "")
                search = f"{brand} {desc}".strip() if brand else desc
                cc = cat.get_landing_cost(phone_number, search)
                if cc > 0:
                    lc = cc * qty
            rev = int(t.get("amount", 0))
            if lc and int(lc) > 0:
                total_margin += rev - int(lc)
                costed_rev += rev
            else:
                uncosted += 1
        return total_margin, costed_rev, uncosted

    _PERIOD_LABELS = {"today": "Today", "week": "This Week",
                      "month": "This Month", "last_month": "Last Month"}

    def _dash_industry_lines(self, d: dict) -> list:
        """3F: compact per-industry lines for the dashboard card.
          - hybrid: product vs service revenue split,
          - manufacturing: production output (count + value),
          - services: jobs count.
        Kept to 1-2 lines so the card stays scannable; full detail is in the
        text P&L / drills."""
        industry = d.get("industry", "trading")
        out = []
        if industry == "hybrid":
            prod = svc = 0
            for t in d.get("sales", []):
                amt = int(t.get("amount", 0))
                sk = (t.get("extra_details") or {}).get("sale_kind", "")
                if sk == "service":
                    is_service = True
                elif sk == "product":
                    is_service = False
                else:
                    cat = (t.get("category", "") or "").lower()
                    is_service = ("service" in cat or t.get("item_type") == "service")
                if is_service:
                    svc += amt
                else:
                    prod += amt
            if prod or svc:
                out.append(f"⚡ Products {format_amount(prod)} · Services {format_amount(svc)}")
        elif industry == "manufacturing":
            prod_txns = d.get("production", [])
            if prod_txns:
                pval = sum(int(t.get("amount", 0)) for t in prod_txns)
                out.append(f"🏭 Production: {len(prod_txns)} run(s) · {format_amount(pval)}")
        elif industry == "services":
            n = d.get("sales_count", 0)
            if n:
                out.append(f"🛠️ Jobs done: {n}")
        return out

    def dashboard(self, phone_number: str, period: str = "month") -> list:
        """The tap-first, in-place Telegram dashboard card. Period toggles +
        drill-downs re-render this same card. Numbers from _period_totals."""
        if period not in ("today", "week", "month", "last_month"):
            period = "month"
        d = self._period_totals(phone_number, period)
        industry = d["industry"]

        rev_label = {"manufacturing": "Output sales", "services": "Service revenue"}\
            .get(industry, "Revenue")
        # `cogs` is now the cost of goods SOLD (accrual), so the label is
        # "Cost of sales" — NOT "Purchases" (purchases are cash-out, shown below).
        cogs_label = {"manufacturing": "Cost of output", "services": "Job costs"}\
            .get(industry, "Cost of sales")

        # NOTE: the title is supplied via the list_response `header`; we do NOT
        # repeat it as the body's first line. Previously both were present and the
        # send_list dedup (which compares stripped header vs body-first-line) could
        # miss on internal whitespace/dash differences, rendering the title twice.
        lines = [
            "────────────────────",
            f"💰 {rev_label}:  *{format_amount(d['revenue'])}*",
            f"📦 {cogs_label}:  {format_amount(d['cogs'])}",
            f"💸 Expenses:  {format_amount(d['opex'])}",
            "────────────────────",
        ]
        # Headline = ACCRUAL net profit (revenue − cost of sales − expenses).
        if d["net"] >= 0:
            net_pct = f" ({int(d['net']/d['revenue']*100)}%)" if d["revenue"] > 0 else ""
            lines.append(f"📈 *Net profit: +{format_amount(d['net'])}*{net_pct}")
        else:
            lines.append(f"📉 *Net profit: −{format_amount(abs(d['net']))}*")
        if d["costed_revenue"] > 0:
            gm_pct = int(d["gross_margin"] / d["costed_revenue"] * 100) if d["costed_revenue"] else 0
            lines.append(f"🟢 Gross margin: {format_amount(d['gross_margin'])} ({gm_pct}%)")
        # Secondary cash line so the owner still sees actual money movement
        # (this is where a big stock purchase shows up — as cash out, not a loss).
        nc = d.get("net_cash", d["net"])
        if nc >= 0:
            lines.append(f"💵 _Net cash flow: +{format_amount(nc)}_")
        else:
            lines.append(f"💵 _Net cash flow: −{format_amount(abs(nc))}_")
        if d["uncosted_sales"] > 0:
            lines.append(f"_⚠️ {d['uncosted_sales']} sale(s) missing cost — set costs for true profit._")
        if d["owed_to_me"] or d["owe_out"]:
            lines.append(f"🔴 Owed to you: {format_amount(d['owed_to_me'])}  ·  "
                         f"📝 You owe: {format_amount(d['owe_out'])}")

        # 3F: per-industry tailoring — compact lines on the card.
        lines.extend(self._dash_industry_lines(d))

        lines.append(f"\n_📝 {d['tx_count']} transaction(s) in this period._")

        # Rows: period toggle, drill-downs, exports, menu.
        def per_btn(pk, lbl):
            mark = "• " if pk == period else ""
            return {"id": f"dash_period_{pk}", "title": f"{mark}{lbl}"}
        rows = [
            per_btn("today", "Today"), per_btn("week", "Week"),
            per_btn("month", "Month"), per_btn("last_month", "Last"),
            {"id": f"dash_drill_top_{period}", "title": "🏆 Top Products"},
            {"id": f"dash_drill_profit_{period}", "title": "📈 Profit / Margin"},
            {"id": f"dash_drill_expenses_{period}", "title": "💸 Expenses by Category"},
            {"id": f"report_pdf_{period}", "title": "📄 PDF"},
            {"id": f"report_export_{period}", "title": "📎 Excel"},
            {"id": "menu_home", "title": "☰ Menu"},
        ]
        return [list_response(
            header=f"📊 Dashboard — {d['label']}",
            body="\n".join(lines),
            button_text="View",
            sections=[{"title": "", "rows": rows}],
            no_paginate=True,
        )]

    def _dash_drill(self, phone_number: str, what: str, period: str) -> list:
        """Drill-down views off the dashboard. Each ends with a ← Dashboard back."""
        d = self._period_totals(phone_number, period)
        # Back row keeps the drill on the SAME editable card (Telegram edits in
        # place; ← Dashboard re-renders the dashboard into this same message).
        back_rows = [
            {"id": f"dash_period_{period}", "title": "← Dashboard"},
            {"id": "menu_home", "title": "☰ Menu"},
        ]

        def _drill_card(header_title, lines):
            # A single list_response so the whole dashboard is one editable card.
            return [list_response(
                header=header_title,
                body="\n".join(lines),
                button_text="Back",
                sections=[{"title": "", "rows": back_rows}],
                no_paginate=True,
            )]

        if what == "expenses":
            cats = {}
            for t in d["expenses"]:
                c = t.get("category", "Other") or "Other"
                cats[c] = cats.get(c, 0) + float(t.get("amount", 0))
            lines = [f"💸 *Expenses — {d['label']}*", ""]
            if not cats:
                lines.append("_No expenses in this period._")
            else:
                total = sum(cats.values())
                for c, amt in sorted(cats.items(), key=lambda x: x[1], reverse=True):
                    pct = int(amt / total * 100) if total else 0
                    lines.append(f"  • {c}: {format_amount(amt)} ({pct}%)")
                lines.append(f"\n*Total: {format_amount(total)}*")
            return _drill_card(f"💸 Expenses — {d['label']}", lines)

        if what == "top":
            # Rank sales by revenue; show qty + margin where cost is known.
            import re
            agg = {}
            for t in d["sales"]:
                name = _clean_desc(t)
                rev = int(t.get("amount", 0))
                m = re.match(r'^(\d+)', str(t.get("quantity", "1")))
                qty = int(m.group(1)) if m else 1
                a = agg.setdefault(name, {"rev": 0, "qty": 0})
                a["rev"] += rev
                a["qty"] += qty
            lines = [f"🏆 *Top Products — {d['label']}*", ""]
            if not agg:
                lines.append("_No sales in this period._")
            else:
                ranked = sorted(agg.items(), key=lambda x: x[1]["rev"], reverse=True)[:10]
                for name, a in ranked:
                    lines.append(f"  • *{name}* — {format_amount(a['rev'])} ({a['qty']} sold)")
            return _drill_card(f"🏆 Top Products — {d['label']}", lines)

        if what == "profit":
            lines = [
                f"📈 *Profit / Margin — {d['label']}*", "",
                f"💰 Revenue (costed): {format_amount(d['costed_revenue'])}",
                f"🟢 Gross margin: {format_amount(d['gross_margin'])}"
                + (f" ({int(d['gross_margin']/d['costed_revenue']*100)}%)"
                   if d['costed_revenue'] else ""),
                f"💸 Expenses: {format_amount(d['opex'])}",
                (f"📈 Net (cash): +{format_amount(d['net'])}" if d['net'] >= 0
                 else f"📉 Net (cash): −{format_amount(abs(d['net']))}"),
            ]
            if d["uncosted_sales"] > 0:
                lines.append(f"\n_⚠️ {d['uncosted_sales']} sale(s) have no cost recorded._\n"
                             "_Set costs on those items for an accurate margin._")
            return _drill_card(f"📈 Profit / Margin — {d['label']}", lines)

        return self.dashboard(phone_number, period)

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
            vendor  = "" if is_bad_vendor(vendor) else vendor
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

            # Determine quantity for this transaction
            import re
            qty_str = t.get("quantity", "1")
            qty = 1
            if qty_str:
                match = re.match(r'^(\d+)', str(qty_str))
                qty = int(match.group(1)) if match else 1

            # Check if this transaction uses the new format (has landing_cost_per_unit)
            # New format: landing_cost is already total. Old format: per-unit, needs multiply.
            has_per_unit_field = extra.get("landing_cost_per_unit") or t.get("landing_cost_per_unit")

            if lc and int(lc) > 0:
                if not has_per_unit_field:
                    # Old format: landing_cost is per-unit, multiply by qty
                    lc = int(lc) * qty

            # Source 2: Lookup from catalog by description/product name
            if not lc or int(lc) <= 0:
                desc = t.get("description", t.get("item_name", ""))
                brand = t.get("brand", "")
                search_name = f"{brand} {desc}".strip() if brand else desc
                catalog_cost = cat.get_landing_cost(phone_number, search_name)
                if catalog_cost > 0:
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
            f"📈  *PROFIT MARGIN — {period_label}*",
            f"_(Sell Price vs Cost Price)_",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"_Based on {len(costed_sales)} sale{'s' if len(costed_sales) != 1 else ''} with known cost_",
            f"",
            f"💰 Sold for:      {format_amount(total_revenue)}",
            f"🏷️ Cost price:    {format_amount(total_cost)}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📈 *Profit:  {format_amount(total_margin)}  ({margin_pct}%)*",
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
    # HYBRID — Product vs Service revenue split
    # ─────────────────────────────────────────────────────────

    def _build_hybrid_revenue_split(self, sales: list, period_label: str) -> str:
        """
        Split sales revenue into Product vs Service for hybrid businesses.
        Prefers the persisted sale_kind marker; falls back to a category
        heuristic for legacy transactions recorded before the marker existed.
        Returns a formatted string, or None if there are no sales.
        """
        if not sales:
            return None

        product_total = 0
        service_total = 0
        product_count = 0
        service_count = 0

        for t in sales:
            amount = int(t.get("amount", 0))
            sale_kind = (t.get("extra_details") or {}).get("sale_kind", "")
            if sale_kind == "service":
                is_service = True
            elif sale_kind == "product":
                is_service = False
            else:
                # Legacy fallback: guess from category / item_type
                cat = (t.get("category", "") or "").lower()
                is_service = "service" in cat or t.get("item_type") == "service"

            if is_service:
                service_total += amount
                service_count += 1
            else:
                product_total += amount
                product_count += 1

        total = product_total + service_total
        if total <= 0:
            return None

        prod_pct = int(product_total / total * 100)
        svc_pct = int(service_total / total * 100)

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"⚡  *REVENUE SPLIT — {period_label}*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"📦 Product Sales:  {format_amount(product_total)} ({prod_pct}%)",
            f"    _{product_count} sale{'s' if product_count != 1 else ''}_",
            f"💼 Service Revenue: {format_amount(service_total)} ({svc_pct}%)",
            f"    _{service_count} job{'s' if service_count != 1 else ''}_",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📈 *Total Revenue:*  {format_amount(total)}",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]
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
        # Avoid duplication: if item already starts with brand
        if item.lower().startswith(brand.lower()):
            return item[:30]
        return f"{brand} {item}"[:30]
    if item:
        return item[:30]
    desc = tx.get("description", tx.get("raw_text", ""))
    # Strip common prefixes
    import re
    desc = re.sub(r'^(?:sold|bought|paid|received)\s+', '', desc,
                  flags=re.IGNORECASE)
    return desc[:30].strip() or "Transaction"
