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
        transactions = self.db.get_transactions_by_period(phone_number, today, today)
        return self._build_report(transactions, "Today")

    def generate_weekly(self, phone_number):
        """Generate this week's report (Monday to today)"""
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        start_date = monday.strftime('%Y-%m-%d')
        end_date = now.strftime('%Y-%m-%d')
        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)
        return self._build_report(transactions, "This Week")

    def generate_monthly(self, phone_number):
        """Generate this month's report"""
        now = datetime.now()
        start_date = now.strftime('%Y-%m-01')
        end_date = now.strftime('%Y-%m-%d')
        period_label = now.strftime('%B %Y')  # e.g., "June 2026"
        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)
        return self._build_report(transactions, period_label)

    def generate_custom(self, phone_number, start_date, end_date):
        """Generate report for a custom date range"""
        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)
        label = f"{start_date} to {end_date}"
        return self._build_report(transactions, label)

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

        # Separate income and expense categories
        income_cats = {}
        expense_cats = {}

        for tx in transactions:
            amount = int(tx.get('amount', 0))
            category = tx.get('category', 'Other')

            if tx.get('type') == 'income':
                income_cats[category] = income_cats.get(category, 0) + amount
            else:
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

        # Calculate totals
        this_income = sum(int(tx.get('amount', 0)) for tx in this_month_txns if tx.get('type') == 'income')
        this_expense = sum(int(tx.get('amount', 0)) for tx in this_month_txns if tx.get('type') == 'expense')
        this_profit = this_income - this_expense

        last_income = sum(int(tx.get('amount', 0)) for tx in last_month_txns if tx.get('type') == 'income')
        last_expense = sum(int(tx.get('amount', 0)) for tx in last_month_txns if tx.get('type') == 'expense')
        last_profit = last_income - last_expense

        # Calculate changes
        income_change = self._calc_change(last_income, this_income)
        expense_change = self._calc_change(last_expense, this_expense)
        profit_change = self._calc_change(last_profit, this_profit)

        result = "📊 *Month-over-Month Comparison*\n\n"

        result += f"💰 *Income:*\n"
        result += f"  Last month: ₦{last_income:,}\n"
        result += f"  This month: ₦{this_income:,} {income_change}\n\n"

        result += f"💸 *Expenses:*\n"
        result += f"  Last month: ₦{last_expense:,}\n"
        result += f"  This month: ₦{this_expense:,} {expense_change}\n\n"

        result += f"📈 *Profit:*\n"
        result += f"  Last month: ₦{last_profit:,}\n"
        result += f"  This month: ₦{this_profit:,} {profit_change}\n"

        return result

    # ==========================================
    # HELPER METHODS
    # ==========================================

    def _build_report(self, transactions, period_label):
        """
        Build a standard report from a list of transactions.

        Returns:
            Formatted WhatsApp text
        """
        if not transactions:
            return f"📊 No transactions recorded for *{period_label}* yet."

        # Calculate totals
        income = sum(int(tx.get('amount', 0)) for tx in transactions if tx.get('type') == 'income')
        expenses = sum(int(tx.get('amount', 0)) for tx in transactions if tx.get('type') == 'expense')
        profit = income - expenses

        # Category breakdown (expenses)
        categories = {}
        for tx in transactions:
            if tx.get('type') == 'expense':
                cat = tx.get('category', 'Other')
                categories[cat] = categories.get(cat, 0) + int(tx.get('amount', 0))

        sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)

        # Format
        profit_emoji = "📈" if profit >= 0 else "📉"
        profit_label = "Profit" if profit >= 0 else "Loss"

        report = f"📊 *{period_label} Report*\n\n"
        report += f"💰 Income: ₦{income:,}\n"
        report += f"💸 Expenses: ₦{expenses:,}\n"
        report += f"{profit_emoji} {profit_label}: ₦{abs(profit):,}\n\n"

        if sorted_cats:
            report += "📋 *Top Expenses:*\n"
            for i, (cat, amount) in enumerate(sorted_cats[:5], 1):
                emoji = self._get_category_emoji(cat)
                pct = int((amount / expenses * 100)) if expenses > 0 else 0
                report += f"  {i}. {emoji} {cat}: ₦{amount:,} ({pct}%)\n"

        report += f"\n📝 Total transactions: {len(transactions)}"

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
