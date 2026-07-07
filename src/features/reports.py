# src/features/reports.py
"""Reports — daily, weekly, monthly, filtered, and dashboard views."""

import logging
from datetime import datetime, timedelta
from utils.whatsapp_ui import text_response, button_response, list_response, format_amount

logger = logging.getLogger(__name__)


class ReportsHandler:
    """Handles all report generation and display."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    def show(self, phone_number: str) -> list:
        """Show report options menu."""
        return [list_response(
            header="📊 Reports",
            body="Which report would you like?",
            button_text="Select Report",
            sections=[{
                "title": "Time Period",
                "rows": [
                    {"id": "report_today", "title": "📅 Today", "description": "Today's transactions"},
                    {"id": "report_week", "title": "📆 This Week", "description": "Last 7 days"},
                    {"id": "report_month", "title": "🗓️ This Month", "description": "Current month totals"},
                    {"id": "report_sales", "title": "💰 My Sales", "description": "All sales this month"},
                    {"id": "report_purchases", "title": "📦 My Purchases", "description": "All purchases this month"},
                ]
            }]
        )]

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle report button taps."""
        handlers = {
            "report_today": lambda: self._show_report(phone_number, "today"),
            "report_week": lambda: self._show_report(phone_number, "week"),
            "report_month": lambda: self._show_report(phone_number, "month"),
            "report_sales": lambda: self._filtered_report(phone_number, "sale"),
            "report_purchases": lambda: self._filtered_report(phone_number, "purchase"),
        }
        handler = handlers.get(button_id)
        if handler:
            return handler()
        return self.show(phone_number)

    def _show_report(self, phone_number: str, period: str) -> list:
        """Generate a report for the given period."""
        now = datetime.now()

        if period == "today":
            start_date = now.strftime("%Y-%m-%d")
            end_date = start_date
            label = "Today"
        elif period == "week":
            start_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            end_date = now.strftime("%Y-%m-%d")
            label = "This Week"
        else:  # month
            start_date = now.strftime("%Y-%m-01")
            end_date = now.strftime("%Y-%m-%d")
            label = now.strftime("%B %Y")

        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)

        if not transactions:
            return [text_response(f"📊 *{label}*\n\nNo transactions recorded yet.")]

        # Calculate totals
        total_sales = sum(float(t.get("amount", 0)) for t in transactions if t.get("type") == "sale")
        total_purchases = sum(float(t.get("amount", 0)) for t in transactions if t.get("type") == "purchase")
        total_expenses = sum(float(t.get("amount", 0)) for t in transactions if t.get("type") == "expense")
        net = total_sales - total_purchases - total_expenses

        # Build report
        lines = [
            f"📊 *{label} Report*",
            f"",
            f"💰 Sales: {format_amount(total_sales)}",
            f"📦 Purchases: {format_amount(total_purchases)}",
            f"💸 Expenses: {format_amount(total_expenses)}",
            f"",
            f"{'📈' if net >= 0 else '📉'} Net: {format_amount(abs(net))} {'profit' if net >= 0 else 'loss'}",
            f"",
            f"📝 {len(transactions)} transaction{'s' if len(transactions) != 1 else ''}",
        ]

        # Show last few transactions
        recent = sorted(transactions, key=lambda t: t.get("timestamp", ""), reverse=True)[:5]
        if recent:
            lines.append("")
            lines.append("*Recent:*")
            for tx in recent:
                emoji = {"sale": "💰", "purchase": "📦", "expense": "💸"}.get(tx.get("type", ""), "📝")
                desc = tx.get("description", "")[:30]
                amt = format_amount(tx.get("amount", 0))
                lines.append(f"{emoji} {desc} — {amt}")

        return [text_response("\n".join(lines))]

    def _filtered_report(self, phone_number: str, filter_type: str) -> list:
        """Show sales-only or purchases-only report."""
        now = datetime.now()
        start_date = now.strftime("%Y-%m-01")
        end_date = now.strftime("%Y-%m-%d")

        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)
        filtered = [t for t in transactions if t.get("type") == filter_type]

        label = "Sales" if filter_type == "sale" else "Purchases"
        emoji = "💰" if filter_type == "sale" else "📦"

        if not filtered:
            return [text_response(f"{emoji} *My {label} — {now.strftime('%B %Y')}*\n\nNo {label.lower()} this month.")]

        total = sum(float(t.get("amount", 0)) for t in filtered)
        lines = [
            f"{emoji} *My {label} — {now.strftime('%B %Y')}*",
            f"",
            f"Total: {format_amount(total)} ({len(filtered)} transactions)",
            f"",
        ]

        # Filter out false vendor names
        bad_vendors = {"sold", "bought", "paid", "received", "sale", "purchase", "expense"}

        for tx in sorted(filtered, key=lambda t: t.get("timestamp", ""), reverse=True)[:10]:
            desc = tx.get("description", "")[:25]
            amt = format_amount(tx.get("amount", 0))
            vendor = tx.get("vendor", "")
            if vendor.lower() in bad_vendors:
                vendor = ""
            vendor_str = f" ({vendor})" if vendor else ""
            date_str = tx.get("date", "")[-5:]  # MM-DD
            lines.append(f"• {desc}{vendor_str} — {amt}  _{date_str}_")

        if len(filtered) > 10:
            lines.append(f"\n_...and {len(filtered) - 10} more_")

        return [text_response("\n".join(lines))]
