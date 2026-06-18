# src/services/tier_manager.py
"""Tier Manager - enforces subscription limits and prompts upgrades"""

import logging
from datetime import datetime

from services.database import Database

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ==========================================
# TIER DEFINITIONS
# ==========================================

TIERS = {
    "free": {
        "name": "Free",
        "price": 0,
        "limits": {
            "transactions_per_month": 30,
            "exports_per_month": 5,
            "invoices_per_month": 0,
            "pdf_statements": False,
            "crm_insights": False,
        }
    },
    "basic": {
        "name": "Basic",
        "price": 3000,
        "limits": {
            "transactions_per_month": 999999,
            "exports_per_month": 999999,
            "invoices_per_month": 10,
            "pdf_statements": True,
            "crm_insights": False,
        }
    },
    "pro": {
        "name": "Pro",
        "price": 6000,
        "limits": {
            "transactions_per_month": 999999,
            "exports_per_month": 999999,
            "invoices_per_month": 999999,
            "pdf_statements": True,
            "crm_insights": True,
        }
    }
}


class TierManager:
    """Enforces subscription limits and handles upgrade prompts"""

    def __init__(self, database=None):
        self.db = database or Database()

    def get_user_tier(self, phone_number):
        """Get current tier for a user. Returns 'free' if not set."""
        user = self.db.get_user(phone_number)
        if user:
            return user.get('tier', 'free')
        return 'free'

    def get_tier_limits(self, tier):
        """Get the limits for a specific tier"""
        return TIERS.get(tier, TIERS['free'])['limits']

    # ==========================================
    # LIMIT CHECKS
    # ==========================================

    def check_can_record(self, phone_number):
        """
        Check if user can record a transaction (within monthly limit).
        Returns: (allowed: bool, message: str or None)
        """
        tier = self.get_user_tier(phone_number)
        limits = self.get_tier_limits(tier)
        max_transactions = limits['transactions_per_month']

        if max_transactions >= 999999:
            return True, None

        current_count = self.db.count_transactions_this_month(phone_number)

        if current_count >= max_transactions:
            message = self._upgrade_message(
                phone_number,
                "transactions",
                current_count,
                max_transactions
            )
            return False, message

        if current_count >= int(max_transactions * 0.8):
            remaining = max_transactions - current_count
            warning = f"⚠️ You have {remaining} free transactions left this month."
            return True, warning

        return True, None

    def check_can_export(self, phone_number):
        """
        Check if user can export (within monthly limit).
        Returns: (allowed: bool, message: str or None)
        """
        tier = self.get_user_tier(phone_number)
        limits = self.get_tier_limits(tier)
        max_exports = limits['exports_per_month']

        if max_exports >= 999999:
            return True, None

        user = self.db.get_user(phone_number)
        current_exports = int(user.get('exports_this_month', 0)) if user else 0

        if current_exports >= max_exports:
            message = self._upgrade_message(
                phone_number,
                "exports",
                current_exports,
                max_exports
            )
            return False, message

        self.db.update_user(phone_number, {
            'exports_this_month': current_exports + 1
        })

        return True, None

    def check_can_invoice(self, phone_number):
        """
        Check if user can generate an invoice.
        Returns: (allowed: bool, message: str or None)
        """
        tier = self.get_user_tier(phone_number)
        limits = self.get_tier_limits(tier)
        max_invoices = limits['invoices_per_month']

        if max_invoices == 0:
            message = (
                "📄 *Invoices are a paid feature.*\n\n"
                "Upgrade to Basic (₦3,000/month) to send up to 10 invoices/month.\n\n"
                "Or upgrade to Pro (₦6,000/month) for unlimited invoices!\n\n"
                "Type *UPGRADE* to see plans."
            )
            return False, message

        if max_invoices >= 999999:
            return True, None

        user = self.db.get_user(phone_number)
        current_invoices = int(user.get('invoices_this_month', 0)) if user else 0

        if current_invoices >= max_invoices:
            message = (
                f"📄 You've used {current_invoices}/{max_invoices} invoices this month.\n\n"
                f"Upgrade to *Pro* (₦6,000/month) for unlimited invoices!\n\n"
                f"Type *UPGRADE* to see plans."
            )
            return False, message

        self.db.update_user(phone_number, {
            'invoices_this_month': current_invoices + 1
        })

        return True, None

    def check_can_generate_pdf(self, phone_number):
        """
        Check if user can generate PDF statements.
        Returns: (allowed: bool, message: str or None)
        """
        tier = self.get_user_tier(phone_number)
        limits = self.get_tier_limits(tier)

        if not limits['pdf_statements']:
            message = (
                "📄 *PDF Statements are a paid feature.*\n\n"
                "Upgrade to Basic (₦3,000/month) to generate professional "
                "financial statements for your accountant or bank.\n\n"
                "Type *UPGRADE* to see plans."
            )
            return False, message

        return True, None

    # ==========================================
    # USAGE STATS
    # ==========================================

    def get_usage_summary(self, phone_number):
        """
        Get a formatted usage summary for the user.
        Returns: WhatsApp-formatted text
        """
        tier = self.get_user_tier(phone_number)
        tier_info = TIERS.get(tier, TIERS['free'])
        limits = tier_info['limits']
        tier_name = tier_info['name']

        tx_count = self.db.count_transactions_this_month(phone_number)
        user = self.db.get_user(phone_number)
        exports_used = int(user.get('exports_this_month', 0)) if user else 0
        invoices_used = int(user.get('invoices_this_month', 0)) if user else 0

        max_tx = limits['transactions_per_month']
        max_ex = limits['exports_per_month']
        max_inv = limits['invoices_per_month']

        tx_display = f"{tx_count}/{'∞' if max_tx >= 999999 else max_tx}"
        ex_display = f"{exports_used}/{'∞' if max_ex >= 999999 else max_ex}"
        inv_display = f"{invoices_used}/{'∞' if max_inv >= 999999 else max_inv}"

        result = f"📊 *Your Usage ({tier_name} Plan)*\n\n"
        result += f"📝 Transactions: {tx_display}\n"
        result += f"📎 Exports: {ex_display}\n"
        result += f"📄 Invoices: {inv_display}\n"
        result += f"📋 PDF Statements: {'✅' if limits['pdf_statements'] else '❌'}\n"
        result += f"🧠 CRM Insights: {'✅' if limits['crm_insights'] else '❌'}\n"

        if tier == 'free':
            result += f"\n💡 Upgrade to unlock more! Type *UPGRADE*"

        return result

    # ==========================================
    # UPGRADE FLOW
    # ==========================================

    def get_upgrade_options(self):
        """
        Show upgrade plans.
        Returns: WhatsApp-formatted text
        """
        return (
            "💎 *Kashia Plans*\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🆓 *FREE* (current)\n"
            "  • 30 transactions/month\n"
            "  • 5 exports/month\n"
            "  • Basic text reports\n"
            "  • No invoices\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💼 *BASIC — ₦3,000/month*\n"
            "  • Unlimited transactions\n"
            "  • Unlimited exports\n"
            "  • 10 invoices/month\n"
            "  • PDF financial statements\n"
            "  • Full CRM\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🏆 *PRO — ₦6,000/month*\n"
            "  • Everything in Basic\n"
            "  • Unlimited invoices\n"
            "  • CRM insights & alerts\n"
            "  • Branded documents\n"
            "  • Priority support\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Reply *BASIC* or *PRO* to upgrade."
        )

    def handle_upgrade_request(self, phone_number, plan):
        """
        Handle upgrade request. Generate payment link.
        Args: plan: "basic" or "pro"
        Returns: list of response dicts
        """
        plan_lower = plan.lower().strip()

        if plan_lower in ['basic', '1', 'basic plan']:
            price = 3000
            plan_name = "Basic"
        elif plan_lower in ['pro', '2', 'pro plan']:
            price = 6000
            plan_name = "Pro"
        else:
            return [{"type": "text", "content": "Please reply *BASIC* or *PRO* to choose a plan."}]

        # TODO: Replace with actual Paystack payment link (Step 21)
        payment_link = f"https://paystack.com/pay/kashia-{plan_lower}-{price}"

        return [{"type": "text", "content": (
            f"💳 *Upgrade to {plan_name} — ₦{price:,}/month*\n\n"
            f"Click to pay:\n{payment_link}\n\n"
            f"✅ Your account will be upgraded instantly after payment.\n"
            f"📱 Supports: Card, Bank Transfer, USSD\n\n"
            f"_Cancel anytime. No commitment._"
        )}]

    def upgrade_user(self, phone_number, new_tier):
        """Upgrade a user to a new tier (called after successful payment)"""
        self.db.update_user(phone_number, {
            'tier': new_tier,
            'tier_upgraded_at': datetime.now().isoformat(),
            'exports_this_month': 0,
            'invoices_this_month': 0,
        })
        logger.info(f"User {phone_number} upgraded to {new_tier}")

    def reset_monthly_counters(self, phone_number):
        """Reset monthly usage counters (call on 1st of each month)"""
        self.db.update_user(phone_number, {
            'exports_this_month': 0,
            'invoices_this_month': 0,
        })

    # ==========================================
    # PRIVATE HELPERS
    # ==========================================

    def _upgrade_message(self, phone_number, feature, current, maximum):
        """Generate a contextual upgrade message"""
        if feature == "transactions":
            return (
                f"⚠️ *Transaction limit reached!*\n\n"
                f"You've used {current}/{maximum} free transactions this month.\n\n"
                f"Upgrade to *Basic* (₦3,000/month) for *unlimited* transactions.\n\n"
                f"💡 That's ₦100/day — cheaper than a plate of rice!\n\n"
                f"Type *UPGRADE* to see plans."
            )
        elif feature == "exports":
            return (
                f"📎 *Export limit reached!*\n\n"
                f"You've used {current}/{maximum} free exports this month.\n\n"
                f"Upgrade to *Basic* (₦3,000/month) for unlimited exports.\n\n"
                f"Type *UPGRADE* to see plans."
            )
        else:
            return (
                f"⚠️ *Feature limit reached!*\n\n"
                f"Upgrade for unlimited access.\n\n"
                f"Type *UPGRADE* to see plans."
            )
