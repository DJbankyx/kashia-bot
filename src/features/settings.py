# src/features/settings.py
"""Help & Settings — tutorial, usage, upgrade, industry change, notifications, reset."""

import logging

from core import states
from utils.whatsapp_ui import (
    text_response, button_response, list_response
)

logger = logging.getLogger(__name__)

SETTINGS_STATE = "SETTINGS_FLOW"

# All industry options
INDUSTRY_OPTIONS = {
    "1": ("trading",       "🛍️ Trading & Retail",     "Buy and sell goods"),
    "2": ("services",      "🔧 Services",              "Consulting, repairs, skills"),
    "3": ("food",          "🍽️ Food & Drinks",         "Restaurant, catering, sales"),
    "4": ("manufacturing", "🏭 Manufacturing",         "Production, fabrication"),
    "5": ("hybrid",        "🔀 Hybrid",                "Products + services combined"),
}


class SettingsHandler:
    """Handles all Help & Settings section flows."""

    def __init__(self, session_mgr, database, tier_manager):
        self.session      = session_mgr
        self.db           = database
        self.tier_manager = tier_manager

    # ─────────────────────────────────────────────────────────
    # BUTTON ENTRY POINTS
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str) -> list:
        """Route all set_* buttons."""

        if button_id == "set_tutorial":
            return self._show_tutorial()

        if button_id == "set_usage":
            return self._show_usage(phone_number)

        if button_id == "set_upgrade":
            return self._show_upgrade(phone_number)

        if button_id == "set_industry":
            return self._start_change_industry(phone_number)

        if button_id == "set_notify":
            return self._show_notifications(phone_number)

        if button_id == "set_bug":
            return self._show_bug_report()

        if button_id == "set_reset":
            from core.pin_guard import requires_pin
            pin_check = requires_pin(self.db, self.session, phone_number, "set_reset")
            if pin_check:
                return pin_check
            return self._confirm_reset(phone_number)

        # Confirm buttons from within flows
        if button_id == "set_reset_yes":
            return self._execute_reset(phone_number)

        if button_id == "set_reset_no":
            self.session.reset(phone_number)
            return [text_response("👍 Your account data is safe.")]

        if button_id == "set_notify_on":
            return self._set_notifications(phone_number, True)

        if button_id == "set_notify_off":
            return self._set_notifications(phone_number, False)

        if button_id.startswith("set_upgrade_"):
            plan = button_id.replace("set_upgrade_", "")
            return self._handle_upgrade_request(phone_number, plan)

        return [text_response("👆 Pick an option from the Settings menu.")]

    # ─────────────────────────────────────────────────────────
    # STATE HANDLER — called by router when state == SETTINGS_FLOW
    # ─────────────────────────────────────────────────────────

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during a settings flow."""
        context  = session.get("context", {})
        step     = context.get("set_step", "")
        text_s   = text.strip()
        text_low = text_s.lower()

        if text_low in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        if step == "change_industry":
            return self._finish_change_industry(phone_number, text_s)

        self.session.reset(phone_number)
        return [text_response("Something went wrong. Please try again.")]

    # ─────────────────────────────────────────────────────────
    # TUTORIAL
    # ─────────────────────────────────────────────────────────

    def _show_tutorial(self) -> list:
        """How to use Kashia — concise guide."""
        pages = [
            text_response(
                "📖 *How to Use Kashia* — Part 1 of 3\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📝 *Recording Transactions*\n\n"
                "Just type what happened naturally:\n\n"
                "  _\"sold 10 Nike shoes to Sandra 150K\"_\n"
                "  _\"bought 5 bags rice from Alhaji 95K\"_\n"
                "  _\"paid transport 5000\"_\n\n"
                "Kashia reads it, asks you to confirm, then saves.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💳 *Recording Credit Sales*\n\n"
                "  _\"Bola took goods worth 20K on credit\"_\n"
                "  _\"I owe Dangote 50K for flour\"_\n\n"
                "Kashia tracks the debt automatically."
            ),
            text_response(
                "📖 *How to Use Kashia* — Part 2 of 3\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📊 *Reports*\n\n"
                "Tap *Business → Reports* or type:\n"
                "  • _\"report\"_ — this month's P&L\n"
                "  • _\"today\"_ — today only\n"
                "  • _\"this week\"_ — last 7 days\n\n"
                "The report shows:\n"
                "  💰 Revenue  📦 Purchases  💸 Expenses\n"
                "  📈 Gross Profit  →  Net Profit\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "💳 *Debt Tracking*\n\n"
                "  • Tap *Business → Debts & Credits*\n"
                "  • Or type _\"who owes me\"_\n"
                "  • Record payment: _\"Sandra paid me 10K\"_"
            ),
            text_response(
                "📖 *How to Use Kashia* — Part 3 of 3\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📦 *Product Catalog*\n\n"
                "Set up your products once:\n"
                "  Tap *Business → Product Catalog*\n\n"
                "Once set up, recording is faster —\n"
                "Kashia knows your products and formats.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🧾 *Invoices & Receipts*\n\n"
                "  Tap *Business → Documents*\n"
                "  → Generate Invoice / Receipt / Statement\n\n"
                "Sent as PDF via WhatsApp.\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "❓ Type *help* at any time to see this menu again."
            ),
        ]
        return pages

    # ─────────────────────────────────────────────────────────
    # USAGE & LIMITS
    # ─────────────────────────────────────────────────────────

    def _show_usage(self, phone_number: str) -> list:
        """Show tier usage stats."""
        summary = self.tier_manager.get_usage_summary(phone_number)
        user    = self.db.get_user(phone_number) or {}
        tier    = user.get("tier", "free")

        responses = [text_response(summary)]

        # Nudge free users to upgrade
        if tier == "free":
            responses.append(button_response(
                "Want unlimited transactions and more?",
                [
                    {"id": "set_upgrade_basic", "title": "💼 Go Basic ₦3,000"},
                    {"id": "set_upgrade_pro",   "title": "🏆 Go Pro ₦6,000"},
                ]
            ))

        return responses

    # ─────────────────────────────────────────────────────────
    # UPGRADE PLAN
    # ─────────────────────────────────────────────────────────

    def _show_upgrade(self, phone_number: str) -> list:
        """Show upgrade options."""
        user = self.db.get_user(phone_number) or {}
        tier = user.get("tier", "free")

        plan_text = self.tier_manager.get_upgrade_options()

        responses = [text_response(plan_text)]

        if tier == "free":
            responses.append(button_response(
                "Choose your plan:",
                [
                    {"id": "set_upgrade_basic", "title": "💼 Basic — ₦3,000/mo"},
                    {"id": "set_upgrade_pro",   "title": "🏆 Pro — ₦6,000/mo"},
                ]
            ))
        elif tier == "basic":
            responses.append(button_response(
                "You're on Basic. Upgrade to Pro?",
                [
                    {"id": "set_upgrade_pro", "title": "🏆 Upgrade to Pro"},
                ]
            ))
        else:
            responses.append(text_response("✅ You're already on the Pro plan!"))

        return responses

    def _handle_upgrade_request(self, phone_number: str, plan: str) -> list:
        """Generate payment link for upgrade."""
        result = self.tier_manager.handle_upgrade_request(phone_number, plan)
        return result

    # ─────────────────────────────────────────────────────────
    # CHANGE INDUSTRY
    # ─────────────────────────────────────────────────────────

    def _start_change_industry(self, phone_number: str) -> list:
        """Show industry options."""
        user = self.db.get_user(phone_number) or {}
        current = user.get(
            "industry_class",
            user.get("business_type", "trading")
        )

        rows = []
        for num, (key, label, desc) in INDUSTRY_OPTIONS.items():
            title = f"{'✅ ' if key == current else ''}{label}"
            rows.append({"id": f"set_ind_{key}", "title": title[:24], "description": desc})

        self.session.save(phone_number, SETTINGS_STATE, {
            "set_step": "change_industry",
        })

        return [list_response(
            header="🔄 Change Industry",
            body=f"Current: *{current.title()}*\n\nSwitch to a different business type:",
            button_text="Select",
            sections=[{"title": "Industry Types", "rows": rows}]
        )]

    def _finish_change_industry(self, phone_number: str, text: str) -> list:
        """Handle industry selection from list or text input."""
        text_low = text.lower().strip()

        # Came from a list button (set_ind_trading etc)
        if text_low.startswith("set_ind_"):
            new_industry = text_low.replace("set_ind_", "")
        else:
            # Try to match typed text to an industry
            new_industry = None
            for num, (key, label, desc) in INDUSTRY_OPTIONS.items():
                if text_low in (num, key, key.lower(), label.lower()):
                    new_industry = key
                    break

        if not new_industry or new_industry not in [v[0] for v in INDUSTRY_OPTIONS.values()]:
            return [text_response(
                "❌ Didn't recognise that. Please pick from the list."
            )]

        # Save to both fields for backward compatibility
        self.db.update_user(phone_number, {
            "industry_class": new_industry,
            "business_type":  new_industry,
        })
        self.session.reset(phone_number)

        label = next(
            (v[1] for v in INDUSTRY_OPTIONS.values() if v[0] == new_industry),
            new_industry.title()
        )
        return [text_response(
            f"✅ *Industry updated to {label}!*\n\n"
            f"_Your menu and reports will now reflect your business type._\n\n"
            f"_Tap the menu button to see your updated home screen._"
        )]

    # ─────────────────────────────────────────────────────────
    # NOTIFICATIONS
    # ─────────────────────────────────────────────────────────

    def _show_notifications(self, phone_number: str) -> list:
        """Show notification toggle."""
        user      = self.db.get_user(phone_number) or {}
        daily_on  = user.get("notify_daily",  True)
        weekly_on = user.get("notify_weekly", True)

        daily_label  = "✅ Daily Report:  ON"  if daily_on  else "❌ Daily Report:  OFF"
        weekly_label = "✅ Weekly Report: ON" if weekly_on else "❌ Weekly Report: OFF"

        return [button_response(
            f"🔔 *Notifications*\n\n"
            f"{daily_label}\n"
            f"{weekly_label}\n\n"
            f"_Daily reports arrive at 7PM every day._\n"
            f"_Weekly reports arrive Sunday evenings._",
            [
                {"id": "set_notify_on",  "title": "🔔 Turn All ON"},
                {"id": "set_notify_off", "title": "🔕 Turn All OFF"},
            ]
        )]

    def _set_notifications(self, phone_number: str, on: bool) -> list:
        """Toggle all notifications on or off."""
        self.db.update_user(phone_number, {
            "notify_daily":  on,
            "notify_weekly": on,
        })
        self.session.reset(phone_number)
        status = "ON 🔔" if on else "OFF 🔕"
        return [text_response(
            f"✅ Notifications turned *{status}*\n\n"
            f"_You can change this anytime from Help & Settings._"
        )]

    # ─────────────────────────────────────────────────────────
    # REPORT A BUG
    # ─────────────────────────────────────────────────────────

    def _show_bug_report(self) -> list:
        """Give user a way to report bugs."""
        return [text_response(
            "🐛 *Report a Problem*\n\n"
            "Sorry you're having trouble!\n\n"
            "Please describe the issue by typing a message here and "
            "we'll look into it.\n\n"
            "Or contact support directly:\n"
            "📧 support@kashia.app\n\n"
            "_Common fixes:_\n"
            "• If the bot is stuck, type *cancel*\n"
            "• If a transaction was wrong, type *undo*\n"
            "• If the menu disappeared, type *hi*"
        )]

    # ─────────────────────────────────────────────────────────
    # RESET ACCOUNT
    # ─────────────────────────────────────────────────────────

    def _confirm_reset(self, phone_number: str) -> list:
        """Show reset warning with confirmation buttons."""
        return [button_response(
            "⚠️ *Reset Account*\n\n"
            "This will permanently delete:\n"
            "  • All your transactions\n"
            "  • All contacts & debts\n"
            "  • Your product catalog\n"
            "  • All reports\n\n"
            "*Your account login will remain.*\n\n"
            "This cannot be undone. Are you sure?",
            [
                {"id": "set_reset_yes", "title": "🗑️ Yes, Delete All"},
                {"id": "set_reset_no",  "title": "← Keep My Data"},
            ]
        )]

    def _execute_reset(self, phone_number: str) -> list:
        """Delete all user data except the account record."""
        try:
            # 1. Delete all transactions (scan + delete in batches)
            self._delete_all_transactions(phone_number)

            # 2. Delete all contacts
            self._delete_all_contacts(phone_number)

            # 3. Wipe catalog, debts, sessions from user record
            self.db.update_user(phone_number, {
                "product_catalog":         {},
                "transaction_count":       0,
                "exports_this_month":      0,
                "invoices_this_month":     0,
                "last_deleted_transaction": None,
            })

            # 4. Reset session
            self.session.reset(phone_number)

            logger.info(f"Account reset executed for {phone_number}")

            return [text_response(
                "🗑️ *Account reset complete.*\n\n"
                "All transactions, contacts, and catalog data have been deleted.\n\n"
                "Your account is still active. Type *hi* to start fresh."
            )]

        except Exception as e:
            logger.error(f"Reset error for {phone_number}: {e}")
            self.session.reset(phone_number)
            return [text_response(
                "❌ Reset failed. Please try again or contact support."
            )]

    def _delete_all_transactions(self, phone_number: str):
        """Batch delete all transactions for a user."""
        try:
            from boto3.dynamodb.conditions import Key
            response = self.db.transactions.query(
                KeyConditionExpression=Key("phone_number").eq(phone_number)
            )
            items = response.get("Items", [])

            # Paginate
            while "LastEvaluatedKey" in response:
                response = self.db.transactions.query(
                    KeyConditionExpression=Key("phone_number").eq(phone_number),
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                items.extend(response.get("Items", []))

            # Delete in batches of 25 (DynamoDB limit per batch_write)
            with self.db.transactions.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={
                        "phone_number":   phone_number,
                        "transaction_id": item["transaction_id"],
                    })

            logger.info(f"Deleted {len(items)} transactions for {phone_number}")
        except Exception as e:
            logger.error(f"Error deleting transactions: {e}")

    def _delete_all_contacts(self, phone_number: str):
        """Batch delete all contacts for a user."""
        try:
            from boto3.dynamodb.conditions import Key
            response = self.db.contacts.query(
                KeyConditionExpression=Key("phone_number").eq(phone_number)
            )
            items = response.get("Items", [])

            while "LastEvaluatedKey" in response:
                response = self.db.contacts.query(
                    KeyConditionExpression=Key("phone_number").eq(phone_number),
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                items.extend(response.get("Items", []))

            with self.db.contacts.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={
                        "phone_number": phone_number,
                        "contact_id":   item["contact_id"],
                    })

            logger.info(f"Deleted {len(items)} contacts for {phone_number}")
        except Exception as e:
            logger.error(f"Error deleting contacts: {e}")
