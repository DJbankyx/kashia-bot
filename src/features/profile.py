# src/features/profile.py
"""Profile & Dashboard — business overview at a glance."""

import logging
from datetime import datetime, timedelta
from utils.whatsapp_ui import text_response, button_response, format_amount

logger = logging.getLogger(__name__)


class ProfileHandler:
    """Show business profile and dashboard."""

    def __init__(self, session_mgr, database, get_industry_fn):
        self.session = session_mgr
        self.db = database
        self._get_industry = get_industry_fn

    def show(self, phone_number: str) -> list:
        """Show business dashboard — the at-a-glance view."""
        user = self.db.get_user(phone_number)
        if not user:
            return [text_response("Please complete onboarding first.")]

        business_name = user.get("business_name", "My Business")
        industry = self._get_industry(phone_number)
        industry_label = industry.get_profile_label() if industry else "Business"
        tier = user.get("tier", "free").capitalize()

        # Get today's stats
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        month_start = now.strftime("%Y-%m-01")

        today_txs = self.db.get_transactions_by_period(phone_number, today, today) or []
        month_txs = self.db.get_transactions_by_period(phone_number, month_start, today) or []

        # Calculate today
        today_sales = sum(float(t.get("amount", 0)) for t in today_txs if t.get("type") == "sale")
        today_expenses = sum(float(t.get("amount", 0)) for t in today_txs if t.get("type") in ("expense", "purchase"))

        # Calculate month
        month_sales = sum(float(t.get("amount", 0)) for t in month_txs if t.get("type") == "sale")
        month_expenses = sum(float(t.get("amount", 0)) for t in month_txs if t.get("type") in ("expense", "purchase"))
        month_profit = month_sales - month_expenses

        # Get debt info
        debts = self.db.get_all_debtors(phone_number) or []
        i_owe_list = self.db.get_all_creditors(phone_number) or []
        total_owed_to_me = sum(float(d.get("amount", 0)) for d in debts)
        total_i_owe = sum(float(d.get("amount", 0)) for d in i_owe_list)

        # Build dashboard
        lines = [
            f"{'─' * 20}",
            f"👤 *{business_name}*",
            f"{industry_label} • {tier} Plan",
            f"📱 {phone_number}",
            f"{'─' * 20}",
            f"",
            f"📅 *Today*",
            f"  💰 Sales: {format_amount(today_sales)}",
            f"  💸 Expenses: {format_amount(today_expenses)}",
            f"  📝 Transactions: {len(today_txs)}",
            f"",
            f"🗓️ *{now.strftime('%B %Y')}*",
            f"  💰 Sales: {format_amount(month_sales)}",
            f"  💸 Expenses: {format_amount(month_expenses)}",
            f"  {'📈' if month_profit >= 0 else '📉'} Net: {format_amount(abs(month_profit))} {'profit' if month_profit >= 0 else 'loss'}",
            f"  📝 Transactions: {len(month_txs)}",
        ]

        if total_owed_to_me > 0 or total_i_owe > 0:
            lines.append(f"")
            lines.append(f"💳 *Debts*")
            if total_owed_to_me > 0:
                lines.append(f"  💰 Owed to you: {format_amount(total_owed_to_me)}")
            if total_i_owe > 0:
                lines.append(f"  📝 You owe: {format_amount(total_i_owe)}")

        lines.append(f"\n{'─' * 20}")

        # No export buttons here — just quick view
        return [text_response("\n".join(lines))]
