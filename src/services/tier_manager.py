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

# ==========================================
# SUBSCRIPTION PERIODS (build #S1/S3)
# ==========================================
# How many days each billing period adds to a subscription. Monthly is the
# default and the only one the picker offers until S3 wires quarterly/yearly.
PERIOD_DAYS = {
    "monthly": 30,
    "quarterly": 90,
    "yearly": 365,
}
# Days AFTER subscription_ends before the hard auto-downgrade to Free (owner
# decision: 3-day grace with a "payment overdue" nudge during the window).
GRACE_DAYS = 3


def _period_days(period: str) -> int:
    """Days a billing period adds (default monthly)."""
    return PERIOD_DAYS.get(str(period or "monthly").lower(), 30)


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
        # TODO: Re-enable tier check after beta testing
        return True, None
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

    def handle_upgrade_request(self, phone_number, plan, period=None):
        """
        Handle an upgrade request.
        Args:
            plan: "basic" or "pro"
            period: None → show the period picker (Monthly/Quarterly/Yearly);
                    "monthly"|"quarterly"|"yearly" → generate the Paystack link.
        Returns: list of response dicts
        """
        plan_lower = str(plan).lower().strip()
        if plan_lower in ['basic', '1', 'basic plan']:
            plan_key, plan_name = "basic", "Basic"
        elif plan_lower in ['pro', '2', 'pro plan']:
            plan_key, plan_name = "pro", "Pro"
        else:
            return [{"type": "text", "content": "Please reply *BASIC* or *PRO* to choose a plan."}]

        # ── No period yet → offer Monthly / Quarterly / Yearly (S3) ──
        if not period:
            from services.paystack import PLANS, PERIODS
            periods = PLANS[plan_key]["periods"]
            lines = [f"💳 *{plan_name}* — choose how long:", ""]
            buttons = []
            label = {"monthly": "Monthly", "quarterly": "Quarterly (save)",
                     "yearly": "Yearly (2 months free)"}
            for per in PERIODS:
                disp = periods[per]["price_display"]
                lines.append(f"• *{label[per]}* — {disp}")
                buttons.append({"id": f"set_upgrade_{plan_key}_{per}",
                                "title": f"{label[per].split(' ')[0]} · {disp.split('/')[0].replace('₦','₦')}"[:24]})
            return [{
                "type": "buttons",
                "content": {"body": "\n".join(lines), "buttons": buttons[:3]},
            }]

        period = str(period).lower()
        if period not in ("monthly", "quarterly", "yearly"):
            period = "monthly"

        from services.paystack import plan_period_amount
        amount_kobo, price_display = plan_period_amount(plan_key, period)
        price_naira = int((amount_kobo or 0) / 100)

        # Get user email if available
        user = self.db.get_user(phone_number)
        email = user.get("email", "") if user else ""

        # Initialize Paystack transaction (period-aware)
        try:
            from services.paystack import PaystackService
            paystack = PaystackService()
            result = paystack.initialize_transaction(phone_number, plan_key, email, period=period)

            if result.get("success"):
                payment_url = result["payment_url"]
                return [{"type": "text", "content": (
                    f"💳 *Upgrade to {plan_name} — {price_display}*\n\n"
                    f"Tap to pay:\n{payment_url}\n\n"
                    f"✅ Your account upgrades instantly after payment.\n"
                    f"📱 Supports: Card, Bank Transfer, USSD\n\n"
                    f"_Cancel anytime. No commitment._"
                )}]
            else:
                # Paystack failed — show fallback link
                logger.warning(f"Paystack init failed: {result.get('error')}")
                return [{"type": "text", "content": (
                    f"💳 *Upgrade to {plan_name} — {price_display}*\n\n"
                    f"Payment link generation failed. Please try again later.\n\n"
                    f"Or contact support: support@kashia.app"
                )}]

        except Exception as e:
            logger.error(f"Upgrade request error: {e}")
            return [{"type": "text", "content": (
                f"💳 *Upgrade to {plan_name} — {price_display}*\n\n"
                f"Something went wrong. Please try again later.\n\n"
                f"Or contact support: support@kashia.app"
            )}]

    def upgrade_user(self, phone_number, new_tier, period="monthly"):
        """Upgrade (or renew) a user to a paid tier after a successful payment.

        Also stamps the subscription WINDOW (build #S1): subscription_ends,
        subscription_period, subscription_started, subscription_source. A RENEWAL
        while still active EXTENDS from the current subscription_ends so remaining
        days aren't lost; a renewal after lapse extends from today. Zeroing the
        monthly usage counters is preserved (fresh cycle on pay).
        """
        from datetime import timedelta
        now = datetime.now()
        days = _period_days(period)

        # Extend from whichever is later: now, or the current end date (so a user
        # who renews early keeps the days they already paid for).
        base = now
        try:
            existing = self.db.get_user(phone_number) or {}
            cur_end = existing.get("subscription_ends")
            if cur_end:
                cur_dt = datetime.fromisoformat(str(cur_end))
                if cur_dt > now:
                    base = cur_dt
        except Exception:
            base = now

        new_end = base + timedelta(days=days)
        self.db.update_user(phone_number, {
            'tier': new_tier,
            'tier_upgraded_at': now.isoformat(),      # kept for backward-compat
            'subscription_started': now.isoformat(),
            'subscription_period': str(period or "monthly").lower(),
            'subscription_ends': new_end.isoformat(),
            'subscription_source': 'paystack',
            'exports_this_month': 0,
            'invoices_this_month': 0,
        })
        logger.info(f"User {phone_number} upgraded to {new_tier} "
                    f"({period}); ends {new_end.date().isoformat()}")

    def downgrade_user(self, phone_number, reason="expired"):
        """Downgrade a user back to Free (build #S1). Called by the expiry job
        after the grace window. Sets tier='free' and records downgraded_at +
        reason. NEVER deletes transactions/contacts/catalog — Free only caps NEW
        activity via the existing check_can_* limits (which read tier live)."""
        self.db.update_user(phone_number, {
            'tier': 'free',
            'downgraded_at': datetime.now().isoformat(),
            'downgrade_reason': reason,
        })
        logger.info(f"User {phone_number} downgraded to free ({reason})")

    def subscription_status(self, phone_number):
        """Read a user's subscription state (build #S1). Returns a dict:
          {tier, period, ends (iso|None), days_left (int|None), state}
        state ∈:
          'free'          — not a paid tier.
          'grandfathered' — paid tier but NO subscription_ends on record (an
                            existing paid user from before this feature; treated
                            as active, never auto-downgraded until they renew).
          'active'        — paid + ends in the future (days_left ≥ 0).
          'grace'         — expired within the last GRACE_DAYS (still served, but
                            nudged; not yet downgraded).
          'expired'       — past ends + grace (should be downgraded).
        """
        user = self.db.get_user(phone_number) or {}
        tier = user.get('tier', 'free')
        period = user.get('subscription_period', 'monthly')
        ends = user.get('subscription_ends')

        if tier == 'free':
            return {"tier": "free", "period": period, "ends": None,
                    "days_left": None, "state": "free"}
        if not ends:
            return {"tier": tier, "period": period, "ends": None,
                    "days_left": None, "state": "grandfathered"}
        try:
            end_dt = datetime.fromisoformat(str(ends))
        except Exception:
            # Unparseable date — treat as grandfathered (never wrongly expire).
            return {"tier": tier, "period": period, "ends": ends,
                    "days_left": None, "state": "grandfathered"}

        days_left = (end_dt.date() - datetime.now().date()).days
        if days_left >= 0:
            state = "active"
        elif days_left >= -GRACE_DAYS:
            state = "grace"
        else:
            state = "expired"
        return {"tier": tier, "period": period, "ends": ends,
                "days_left": days_left, "state": state}

    def reset_monthly_counters(self, phone_number):
        """Reset monthly usage counters (call on 1st of each month)"""
        self.db.update_user(phone_number, {
            'exports_this_month': 0,
            'invoices_this_month': 0,
        })

    # ==========================================
    # SUBSCRIBE NUDGE (build #S4) — rate-limited
    # ==========================================
    def should_nudge_subscribe(self, phone_number, user=None):
        """True at most ONCE per day per user, so the 'subscribe' CTA (shown at
        the ~80% free-cap warning and the 100% block) isn't spammy. Stamps
        last_subscribe_nudge = today on a True result. Best-effort; on any error
        returns False so we never spam."""
        try:
            u = user if user is not None else (self.db.get_user(phone_number) or {})
            today = datetime.now().strftime("%Y-%m-%d")
            if u.get("last_subscribe_nudge") == today:
                return False
            self.db.update_user(phone_number, {"last_subscribe_nudge": today})
            return True
        except Exception as e:
            logger.warning(f"should_nudge_subscribe failed: {e}")
            return False

    def subscribe_cta(self):
        """The buttons for a Subscribe CTA — routes into the S3 period picker.
        (set_upgrade_<plan> with no period → Monthly/Quarterly/Yearly picker.)"""
        return [
            {"id": "set_upgrade_basic", "title": "🚀 Go Basic"},
            {"id": "set_upgrade_pro", "title": "⭐ Go Pro"},
        ]

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
