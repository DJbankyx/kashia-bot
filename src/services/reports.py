# src/services/reports.py
"""Report Generator - financial summaries formatted for WhatsApp"""

import logging
from datetime import datetime, timedelta

from services.database import Database

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ReportGenerator:
    """Generates financial reports for users"""

    def __init__(self, database=None):
        self.db = database or Database()

    def generate_daily(self, phone_number):
        """Generate today's report"""
        today = datetime.now().strftime('%Y-%m-%d')
        return self._build_report(phone_number, today, today, "Today")

    def generate_weekly(self, phone_number):
        """Generate this week's report (Monday to today)"""
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        start_date = monday.strftime('%Y-%m-%d')
        end_date = now.strftime('%Y-%m-%d')
        return self._build_report(phone_number, start_date, end_date, "This Week")

    def generate_monthly(self, phone_number):
        """Generate this month's report"""
        now = datetime.now()
        start_date = now.strftime('%Y-%m-01')
        end_date = now.strftime('%Y-%m-%d')
        period_label = now.strftime('%B %Y')  # e.g., "June 2026"
        return self._build_report(phone_number, start_date, end_date, period_label)

    def generate_custom(self, phone_number, start_date, end_date):
        """Generate report for a custom date range"""
        label = f"{start_date} to {end_date}"
        return self._build_report(phone_number, start_date, end_date, label)

    def generate_category_breakdown(self, phone_number, period="month"):
        """
        Detailed breakdown by category.

        Returns:
            Formatted WhatsApp text
        """
        now = datetime.now()

        if period == "today":
            start_date = now.strftime('%Y-%m-%d')
            end_date = start_date
            label = "Today"
        elif period == "week":
            monday = now - timedelta(days=now.weekday())
            start_date = monday.strftime('%Y-%m-%d')
            end_date = now.strftime('%Y-%m-%d')
            label = "This Week"
        else:
            start_date = now.strftime('%Y-%m-01')
            end_date = now.strftime('%Y-%m-%d')
            label = now.strftime('%B %Y')

        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)

        if not transactions:
            return f"📊 No transactions for *{label}*."

        # Separate income and OPERATING-expense categories. Stock purchases and
        # cost-of-goods inputs are NOT operating expenses (they're inventory /
        # COGS) and must not be lumped into the expense breakdown. Debt
        # repayments are cash/receivable events, not income or expense.
        from services.accounting import COGS_CATEGORIES, _is_debt_settlement

        income_cats = {}
        expense_cats = {}

        for tx in transactions:
            amount = int(float(tx.get('amount', 0) or 0))
            category = tx.get('category', 'Other')
            ttype = tx.get('type')

            if _is_debt_settlement(tx):
                continue
            if ttype in ('income', 'sale'):
                income_cats[category] = income_cats.get(category, 0) + amount
            elif ttype == 'expense' and category not in COGS_CATEGORIES:
                expense_cats[category] = expense_cats.get(category, 0) + amount

        # Build report
        result = f"📊 *Category Breakdown — {label}*\n\n"

        # Income breakdown
        if income_cats:
            total_income = sum(income_cats.values())
            result += f"💰 *INCOME: ₦{total_income:,}*\n"
            sorted_income = sorted(income_cats.items(), key=lambda x: x[1], reverse=True)
            for cat, amount in sorted_income:
                pct = int((amount / total_income) * 100) if total_income > 0 else 0
                bar = self._progress_bar(pct)
                result += f"  {cat}: ₦{amount:,} ({pct}%)\n"
                result += f"  {bar}\n"
            result += "\n"

        # Expense breakdown
        if expense_cats:
            total_expense = sum(expense_cats.values())
            result += f"💸 *EXPENSES: ₦{total_expense:,}*\n"
            sorted_expense = sorted(expense_cats.items(), key=lambda x: x[1], reverse=True)
            for cat, amount in sorted_expense:
                emoji = self._get_category_emoji(cat)
                pct = int((amount / total_expense) * 100) if total_expense > 0 else 0
                bar = self._progress_bar(pct)
                result += f"  {emoji} {cat}: ₦{amount:,} ({pct}%)\n"
                result += f"  {bar}\n"

        return result

    def generate_comparison(self, phone_number):
        """
        Compare this month vs last month.

        Returns:
            Formatted WhatsApp text
        """
        now = datetime.now()

        # This month
        this_month_start = now.strftime('%Y-%m-01')
        this_month_end = now.strftime('%Y-%m-%d')
        this_month_txns = self.db.get_transactions_by_period(
            phone_number, this_month_start, this_month_end
        )

        # Last month
        if now.month == 1:
            last_month_start = f"{now.year - 1}-12-01"
            last_month_end = f"{now.year - 1}-12-31"
        else:
            last_month_start = f"{now.year}-{now.month - 1:02d}-01"
            last_month_end = f"{now.year}-{now.month - 1:02d}-28"  # Simplified
        last_month_txns = self.db.get_transactions_by_period(
            phone_number, last_month_start, last_month_end
        )

        # Calculate totals via the shared Accounting engine (accrual, COGS-aware)
        # so the comparison matches the dashboard/daily report. Income here means
        # net REVENUE; expenses means OPERATING expenses (not stock purchases);
        # profit means NET profit (revenue - COGS - opex).
        from services.accounting import Accounting
        acct = Accounting(self.db)
        this_pnl = acct.period_pnl(phone_number, this_month_start, this_month_end)
        last_pnl = acct.period_pnl(phone_number, last_month_start, last_month_end)

        this_income = this_pnl["revenue"]
        this_expense = this_pnl["opex"]
        this_profit = this_pnl["net_profit"]

        last_income = last_pnl["revenue"]
        last_expense = last_pnl["opex"]
        last_profit = last_pnl["net_profit"]

        # Calculate changes
        income_change = self._calc_change(last_income, this_income)
        expense_change = self._calc_change(last_expense, this_expense)
        profit_change = self._calc_change(last_profit, this_profit)

        result = "📊 *Month-over-Month Comparison*\n\n"

        result += f"💰 *Revenue:*\n"
        result += f"  Last month: ₦{last_income:,}\n"
        result += f"  This month: ₦{this_income:,} {income_change}\n\n"

        result += f"💸 *Operating expenses:*\n"
        result += f"  Last month: ₦{last_expense:,}\n"
        result += f"  This month: ₦{this_expense:,} {expense_change}\n\n"

        result += f"📈 *Net profit:*\n"
        result += f"  Last month: ₦{last_profit:,}\n"
        result += f"  This month: ₦{this_profit:,} {profit_change}\n"

        return result

    # ==========================================
    # HELPER METHODS
    # ==========================================

    def _build_report(self, phone_number, start_date, end_date, period_label):
        """
        Build a standard report for a period.

        Delegates ALL figures to the shared Accounting engine so the daily /
        weekly / monthly text report can NEVER disagree with the dashboard or
        the Mini App (they all read period_pnl / period_cashflow). The old
        code here computed profit = income - (expense + purchase), which lumped
        every stock PURCHASE into "expenses" and never computed COGS — so buying
        inventory showed a fake "Loss". That bug is gone: purchases are inventory
        (a cash event), not a P&L expense; COGS is the cost of goods SOLD.

        Returns:
            Formatted WhatsApp / Telegram text
        """
        from services.accounting import Accounting

        acct = Accounting(self.db)
        pnl = acct.period_pnl(phone_number, start_date, end_date, period_label)
        cf = acct.period_cashflow(phone_number, start_date, end_date, period_label)

        if pnl.get("tx_count", 0) == 0:
            return f"📊 No transactions recorded for *{period_label}* yet."

        revenue = pnl["revenue"]
        cogs = pnl["cogs"]
        gross_profit = pnl["gross_profit"]
        opex = pnl["opex"]
        net_profit = pnl["net_profit"]
        net_pct = pnl.get("net_margin_pct", 0)

        profit_emoji = "📈" if net_profit >= 0 else "📉"
        profit_label = "Net profit" if net_profit >= 0 else "Net loss"

        report = f"📊 *{period_label} Report*\n\n"
        report += f"💰 Revenue: ₦{revenue:,}\n"
        report += f"📦 Cost of goods sold: ₦{cogs:,}\n"
        report += f"📊 Gross profit: ₦{gross_profit:,}"
        if pnl.get("costed_revenue"):
            report += f" ({pnl.get('gross_margin_pct', 0)}%)"
        report += "\n"
        report += f"💸 Operating expenses: ₦{opex:,}\n"
        report += f"{profit_emoji} {profit_label}: ₦{abs(net_profit):,}"
        if revenue:
            report += f" ({net_pct}%)"
        report += "\n\n"

        # Cash view (money actually in vs out — purchases show here, correctly)
        report += f"💵 Cash in: ₦{cf['cash_in']:,}\n"
        report += f"💳 Cash out: ₦{cf['cash_out']:,}\n"
        net_cash = cf["net_cash"]
        cash_emoji = "🟢" if net_cash >= 0 else "🔴"
        report += f"{cash_emoji} Net cash flow: ₦{net_cash:,}\n"

        # Operating-expense breakdown (opex only — NOT stock purchases/COGS)
        categories = {}
        for tx in pnl.get("opex_txns", []):
            cat = tx.get('category', 'Other')
            categories[cat] = categories.get(cat, 0) + int(float(tx.get('amount', 0) or 0))
        sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)
        if sorted_cats:
            report += "\n📋 *Top Expenses:*\n"
            for i, (cat, amount) in enumerate(sorted_cats[:5], 1):
                emoji = self._get_category_emoji(cat)
                pct = int((amount / opex * 100)) if opex > 0 else 0
                report += f"  {i}. {emoji} {cat}: ₦{amount:,} ({pct}%)\n"

        # Honest integrity flag: sales with no known cost inflate net profit.
        uncosted = pnl.get("uncosted_count", 0)
        if uncosted:
            report += (f"\n⚠️ {uncosted} sale(s) missing a cost — profit above "
                       f"may be overstated until you set their cost.")

        report += f"\n📝 Total transactions: {pnl.get('tx_count', 0)}"

        return report

    def _format_currency(self, amount):
        """Format a number as Nigerian naira"""
        if amount < 0:
            return f"-₦{abs(amount):,}"
        return f"₦{amount:,}"

    def _progress_bar(self, percentage):
        """Create a text progress bar for WhatsApp"""
        filled = int(percentage / 10)
        empty = 10 - filled
        return "▓" * filled + "░" * empty + f" {percentage}%"

    def _calc_change(self, old_value, new_value):
        """Calculate percentage change and return formatted string"""
        if old_value == 0:
            if new_value > 0:
                return "🆕 (new)"
            return ""

        change = ((new_value - old_value) / abs(old_value)) * 100

        if change > 0:
            return f"⬆️ +{int(change)}%"
        elif change < 0:
            return f"⬇️ {int(change)}%"
        else:
            return "➡️ same"

    def _get_category_emoji(self, category):
        """Map category to emoji"""
        emojis = {
            'Goods & Stock': '📦',
            'Sales & Income': '💰',
            'Rent & Space': '🏠',
            'Utilities & Services': '⚡',
            'Transport & Logistics': '🚗',
            'People & Labour': '👥',
            'Equipment & Tools': '📱',
            'Money Matters': '🏦',
            'Marketing & Customers': '🎯',
            'Government & Compliance': '🏛️',
            'Personal': '👤',
        }
        return emojis.get(category, '📂')
