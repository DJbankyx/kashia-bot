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
            "transactions_per_month": 5,
            "exports_per_month": 5,
            "invoices_per_month": 0,
            "pdf_statements": False,
            "crm_insights": False,
        }
    },
    "basic": {
        "name": "Basic",
        "price": 3500,
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
        "price": 6500,
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

        # ── FULL-ACCESS TRIAL (retention), length = TRIAL_DAYS ──
        # A hard transaction wall on day 3 kills the daily-logging habit before it
        # forms. Instead, every new (free) account gets TRIAL_DAYS of UNLIMITED
        # recording. We convert on value realised, not a random count. After the
        # trial the soft monthly cap applies — but we NEVER block viewing history
        # or reports (those are separate paths and stay open regardless).
        trial_days, trial_left = self._trial_status(phone_number)
        if trial_left is not None and trial_left > 0:
            # In trial: unlimited. Gently remind near the end (last 3 days).
            if trial_left <= 3:
                return True, (f"🎁 Your free trial has {trial_left} day"
                              f"{'s' if trial_left != 1 else ''} left — "
                              f"then you can log up to {max_transactions} sales a "
                              f"month free, or upgrade for unlimited. Type "
                              f"*UPGRADE* to see plans.")
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

    # Trial length for a new free account (days of unlimited recording).
    TRIAL_DAYS = 10

    def _trial_status(self, phone_number):
        """Return (trial_days, days_left) for a user's free trial.

        days_left is:
          * > 0  while still inside the trial window,
          * 0    once the trial has ended,
          * None if we can't tell (no clock) — caller treats None as
            "no trial bypass" so we fail safe to the normal cap.

        ANTI-BYPASS: read `trial_started_at` FIRST — an immutable clock set once
        at first-ever account creation that survives Clear/Full Reset, re-
        onboarding, and Transfer/Recover. Only fall back to `created_at` for
        legacy rows that predate it. This means wiping data or moving it to a new
        Telegram account can NOT hand out a fresh trial.
        Never raises."""
        try:
            user = self.db.get_user(phone_number) or {}
            created = user.get("trial_started_at") or user.get("created_at")
            if not created:
                return self.TRIAL_DAYS, None
            created_dt = datetime.fromisoformat(str(created))
            elapsed = (datetime.now() - created_dt).days
            left = self.TRIAL_DAYS - elapsed
            return self.TRIAL_DAYS, max(0, left)
        except Exception as e:
            logger.warning(f"_trial_status failed for {phone_number}: {e}")
            return self.TRIAL_DAYS, None

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
                "Upgrade to Basic (from ₦3,500/month) to send up to 10 invoices/month.\n\n"
                "Or upgrade to Pro (from ₦6,500/month) for unlimited invoices!\n\n"
                "_Tip: yearly billing saves you ~2.5 months._\n\n"
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
                f"Upgrade to *Pro* (from ₦6,500/month) for unlimited invoices!\n\n"
                f"Type *UPGRADE* to see plans."
            )
            return False, message

        self.db.update_user(phone_number, {
            'invoices_this_month': current_invoices + 1
        })

        return True, None

    def check_can_use_insights(self, phone_number):
        """Check if a user can use AI Smart Insights (Pro-only, via the
        crm_insights tier flag). Returns (allowed, message_or_None). Mirrors the
        PDF paywall — Free/Basic get an upgrade prompt."""
        tier = self.get_user_tier(phone_number)
        limits = self.get_tier_limits(tier)
        if not limits.get('crm_insights'):
            message = (
                "🧠 *Smart Insights (AI) is a Pro feature.*\n\n"
                "Upgrade to *Pro* (from ₦6,500/month) and Kashia will read your numbers "
                "and tell you what's really going on — best/worst margins, cash to "
                "chase, who's gone quiet, and what to do about it.\n\n"
                "Tap below or type *UPGRADE* to see plans."
            )
            return False, message
        return True, None

    def check_can_generate_pdf(self, phone_number):
        """
        Check if user can generate PDF statements.
        Returns: (allowed: bool, message: str or None)

        Owner ENABLED the paywall (2026-09-11): PDF financial statements are a
        paid (Basic/Pro) feature. The beta short-circuit was removed.
        """
        tier = self.get_user_tier(phone_number)
        limits = self.get_tier_limits(tier)

        if not limits['pdf_statements']:
            message = (
                "📄 *PDF Statements are a paid feature.*\n\n"
                "Upgrade to Basic (from ₦3,500/month) to generate professional "
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

        # For a PAID tier, lead with the live subscription window (period +
        # renewal date + days left) so an upgraded user actually SEES their
        # subscription — the Usage screen used to show only bare limits, which
        # read like nothing had changed after paying. Reuses subscription_status
        # (already the single source of truth); no new date math here.
        if tier != 'free':
            try:
                sub = self.subscription_status(phone_number)
                period = str(sub.get('period') or 'monthly').capitalize()
                state = sub.get('state')
                ends = sub.get('ends')
                days_left = sub.get('days_left')
                if state == 'grandfathered' or not ends:
                    result += f"⭐ Subscription: *{period}* — active\n"
                else:
                    ends_str = str(ends)[:10]
                    if state == 'active':
                        left = f" ({days_left} day{'s' if days_left != 1 else ''} left)" \
                               if isinstance(days_left, int) else ""
                        result += f"⭐ *{period}* — renews {ends_str}{left}\n"
                    elif state == 'grace':
                        result += (f"⚠️ *{period}* — expired {ends_str}; in grace period. "
                                   f"Type *UPGRADE* to renew.\n")
                    else:  # expired
                        result += (f"⚠️ *{period}* — expired {ends_str}. "
                                   f"Type *UPGRADE* to renew.\n")
                result += "\n"
            except Exception:
                pass  # never let subscription display break the usage screen

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

    def get_upgrade_options(self, tier="free"):
        """
        Show upgrade plans.

        `tier` marks which plan the user is CURRENTLY on with "(current)" — the
        old version hardcoded "(current)" on FREE, so a paying Basic/Pro user
        still saw FREE flagged as their plan (wrong). The FREE bullets now also
        state the 14-day full-access trial (the pricing redesign gives new
        accounts unlimited for 14 days; the "30 sales/month" cap only applies
        AFTER the trial), so the screen matches actual behaviour.
        Returns: WhatsApp-formatted text
        """
        tier = str(tier or "free").lower()
        cur = lambda t: "  ← *current*" if tier == t else ""
        return (
            "💎 *Kashia Plans*\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🆓 *FREE*{cur('free')}\n"
            "  • 10 days FREE unlimited to start\n"
            "  • Then log up to 5 sales a month\n"
            "  • 5 exports/month\n"
            "  • Basic text reports\n"
            "  • Your full history is always visible\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💼 *BASIC — from ₦3,500/month*{cur('basic')}\n"
            "  • Unlimited transactions\n"
            "  • Unlimited exports\n"
            "  • 10 invoices/month\n"
            "  • PDF financial statements\n"
            "  • Full CRM\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🏆 *PRO — from ₦6,500/month*{cur('pro')}\n"
            "  • Everything in Basic\n"
            "  • Unlimited invoices\n"
            "  • CRM insights & alerts\n"
            "  • Branded documents\n"
            "  • Priority support\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 *Pay yearly and save ~2.5 months.*\n\n"
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
                     "yearly": "Yearly (best value — save ~2.5 months)"}
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
                f"Upgrade to *Basic* (from ₦3,500/month) for *unlimited* transactions.\n\n"
                f"💡 Pay yearly and it's about ₦82/day.\n\n"
                f"Type *UPGRADE* to see plans."
            )
        elif feature == "exports":
            return (
                f"📎 *Export limit reached!*\n\n"
                f"You've used {current}/{maximum} free exports this month.\n\n"
                f"Upgrade to *Basic* (from ₦3,500/month) for unlimited exports.\n\n"
                f"Type *UPGRADE* to see plans."
            )
        else:
            return (
                f"⚠️ *Feature limit reached!*\n\n"
                f"Upgrade for unlimited access.\n\n"
                f"Type *UPGRADE* to see plans."
            )
