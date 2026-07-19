# src/features/debt.py
"""Debt & Credit tracking — who owes whom, payments, reminders."""

import logging
import re
import traceback
from datetime import datetime

from core import states
from utils.parser import parse_amount
from utils.whatsapp_ui import text_response, button_response, list_response, format_amount

logger = logging.getLogger(__name__)


class DebtHandler:
    """Handles debt/credit tracking and payments."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    def show_summary(self, phone_number: str) -> list:
        """Show debt summary with action buttons."""
        debts = self.db.get_all_debtors(phone_number) or []
        i_owe = self.db.get_all_creditors(phone_number) or []

        total_owed_to_me = sum(float(d.get("amount", 0)) for d in debts if not d.get("paid"))
        total_i_owe = sum(float(d.get("amount", 0)) for d in i_owe if not d.get("paid"))

        lines = ["💳 *Debts & Credits*", ""]

        if total_owed_to_me > 0:
            lines.append(f"💰 *Owed to you:* {format_amount(total_owed_to_me)}")
            unpaid = [d for d in debts if not d.get("paid")][:5]
            for d in unpaid:
                name = d.get("name", "Unknown")
                amt = format_amount(d.get("amount", 0))
                lines.append(f"  • {name}: {amt}")
            if len([d for d in debts if not d.get("paid")]) > 5:
                lines.append(f"  _...and {len(unpaid) - 5} more_")
        else:
            lines.append("💰 No one owes you right now.")

        lines.append("")

        if total_i_owe > 0:
            lines.append(f"📝 *You owe:* {format_amount(total_i_owe)}")
            unpaid = [d for d in i_owe if not d.get("paid")][:5]
            for d in unpaid:
                name = d.get("name", "Unknown")
                amt = format_amount(d.get("amount", 0))
                lines.append(f"  • {name}: {amt}")
        else:
            lines.append("📝 You don't owe anyone.")

        return [button_response(
            "\n".join(lines),
            [
                {"id": "debt_record", "title": "➕ Record Debt"},
                {"id": "debt_payment", "title": "💵 Record Payment"},
                {"id": "debt_remind", "title": "⏰ Send Reminder"},
            ]
        )]

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle debt-related states."""
        state = session.get("state", "")
        context = session.get("context", {})
        text_lower = text.lower().strip()

        if state == states.DEBT_RECORDING:
            return self._handle_recording(phone_number, text, context)

        if state == states.DEBT_CONFIRMING:
            return self._handle_confirming(phone_number, text, context)

        if state == states.DEBT_PAYMENT:
            return self._handle_payment(phone_number, text, context)

        return self.show_summary(phone_number)

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle debt-related buttons."""
        if button_id == "debt_record":
            self.session.save(phone_number, states.DEBT_RECORDING, {
                "debt_step": "ask_direction",
            })
            return [button_response(
                "💳 Who owes whom?",
                [
                    {"id": "debt_they_owe", "title": "They owe me"},
                    {"id": "debt_i_owe", "title": "I owe them"},
                    {"id": "btn_cancel", "title": "❌ Cancel"},
                ]
            )]

        if button_id in ("debt_they_owe", "debt_i_owe"):
            direction = "they_owe" if button_id == "debt_they_owe" else "i_owe"
            self.session.save(phone_number, states.DEBT_RECORDING, {
                "debt_step": "ask_name",
                "direction": direction,
            })
            return [text_response("👤 Who? (Type their name)")]

        if button_id == "debt_payment":
            return self._start_payment_flow(phone_number)

        if button_id == "debt_remind":
            return self._show_remind_list(phone_number)

        # ── Send reminder to specific debtor (debt_remind_[contact_id]) ──
        if button_id.startswith("debt_remind_"):
            contact_id = button_id[12:]  # after "debt_remind_"
            return self._send_reminder(phone_number, contact_id)

        return self.show_summary(phone_number)

    def _handle_recording(self, phone_number: str, text: str, context: dict) -> list:
        """Step through debt recording."""
        step = context.get("debt_step", "ask_direction")

        if step == "ask_name":
            context["name"] = text.strip()
            context["debt_step"] = "ask_amount"
            self.session.save(phone_number, states.DEBT_RECORDING, context)
            return [text_response(f"💰 How much does *{text.strip()}* {'owe you' if context.get('direction') == 'they_owe' else 'you owe them'}?")]

        if step == "ask_amount":
            amount = parse_amount(text)
            if not amount:
                return [text_response("Please enter a valid amount (e.g. 50000, 150K):")]

            context["amount"] = float(amount)
            context["debt_step"] = "ask_reason"
            self.session.save(phone_number, states.DEBT_RECORDING, context)
            return [text_response("📝 What was it for? (or type *skip*)")]

        if step == "ask_reason":
            reason = text.strip() if text.lower().strip() != "skip" else ""
            context["reason"] = reason
            name = context.get("name", "")
            amount = context.get("amount", 0)
            direction = context.get("direction", "they_owe")

            self.session.save(phone_number, states.DEBT_CONFIRMING, context)

            dir_label = f"*{name}* owes you" if direction == "they_owe" else f"You owe *{name}*"
            reason_line = f"\n📝 Reason: {reason}" if reason else ""

            return [button_response(
                f"💳 Confirm:\n\n{dir_label} {format_amount(amount)}{reason_line}",
                [
                    {"id": "btn_yes", "title": "✅ Confirm"},
                    {"id": "btn_no", "title": "❌ Cancel"},
                ]
            )]

        return self.show_summary(phone_number)

    def _handle_confirming(self, phone_number: str, text: str, context: dict) -> list:
        """Confirm and save debt, then ask if they want to also record as transaction."""
        if text.lower() in ("yes", "y", "confirm", "btn_yes", "✅ confirm"):
            try:
                name = context.get("name", "")
                amount = context.get("amount", 0)
                direction = context.get("direction", "they_owe")
                reason = context.get("reason", "")

                if direction == "they_owe":
                    self.db.record_debt(phone_number, name, float(amount), 'owed_to_me', reason)
                    # Also record as a credit sale transaction
                    desc = reason or f"Credit sale to {name}"
                    self.db.save_transaction(
                        phone_number, int(amount), "sale", desc,
                        "Sales & Income", vendor=name,
                        payment_method="credit",
                    )
                    self.session.reset(phone_number)
                    return [
                        text_response(
                            f"✅ Recorded! *{name}* owes you {format_amount(amount)}.\n"
                            f"💰 Also saved as a credit sale."
                        ),
                        button_response("What's next?", [
                            {"id": "record_sale", "title": "💰 Record Sale"},
                            {"id": "menu_debts", "title": "💳 View Debts"},
                            {"id": "menu_home", "title": "☰ Menu"},
                        ])
                    ]
                else:
                    self.db.record_debt(phone_number, name, float(amount), 'i_owe', reason)
                    # Also record as a credit purchase transaction
                    desc = reason or f"Credit purchase from {name}"
                    self.db.save_transaction(
                        phone_number, int(amount), "purchase", desc,
                        "Goods & Stock", vendor=name,
                        payment_method="credit",
                    )
                    self.session.reset(phone_number)
                    return [
                        text_response(
                            f"✅ Recorded! You owe *{name}* {format_amount(amount)}.\n"
                            f"📦 Also saved as a credit purchase."
                        ),
                        button_response("What's next?", [
                            {"id": "record_purchase", "title": "📦 Record Purchase"},
                            {"id": "menu_debts", "title": "💳 View Debts"},
                            {"id": "menu_home", "title": "☰ Menu"},
                        ])
                    ]
            except Exception as e:
                logger.error(f"Debt save error: {e}\n{traceback.format_exc()}")
                self.session.reset(phone_number)
                return [text_response(f"❌ Error saving debt. Please try again.")]

        self.session.reset(phone_number)
        return [text_response("👍 Cancelled.")]

    def _handle_payment(self, phone_number: str, text: str, context: dict) -> list:
        """Handle payment recording from text (e.g. 'Dangote paid 10000')."""
        payment_text = context.get("payment_text", text)
        text_lower = payment_text.lower()

        # Try to extract name and amount
        # Pattern: "[name] paid [amount]" or "received [amount] from [name]"
        amount = parse_amount(payment_text)
        if not amount:
            self.session.reset(phone_number)
            return [text_response("💰 How much was paid? (Please include an amount)")]

        # Extract name — everything before "paid/settled/cleared"
        name = ""
        for verb in ["paid", "settled", "cleared"]:
            if verb in text_lower:
                parts = text_lower.split(verb)
                if parts[0].strip():
                    name = payment_text[:len(parts[0])].strip()
                break

        if not name:
            # Try "received from [name]"
            match = re.search(r'from\s+(.+?)(?:\s+\d|\s*$)', payment_text, re.IGNORECASE)
            if match:
                name = match.group(1).strip()

        if name:
            # Record payment — settle debt AND record as income transaction
            try:
                self.db.settle_debt(phone_number, name, float(amount), 'owed_to_me')
                # Also record as income transaction (debt payment received)
                self.db.save_transaction(
                    phone_number, int(amount), "sale",
                    f"Debt payment from {name}",
                    "Sales & Income", vendor=name,
                    payment_method="cash",
                )
                self.session.reset(phone_number)
                return [
                    text_response(
                        f"✅ Payment recorded! *{name}* paid {format_amount(amount)}.\n"
                        f"💰 Debt reduced and payment logged."
                    ),
                    button_response("What's next?", [
                        {"id": "menu_debts", "title": "💳 View Debts"},
                        {"id": "record_sale", "title": "💰 Record Sale"},
                        {"id": "menu_home", "title": "☰ Menu"},
                    ])
                ]
            except Exception as e:
                logger.error(f"Payment record error: {e}")
                self.session.reset(phone_number)
                return [text_response(f"✅ Got it — {format_amount(amount)} payment from *{name}*.")]
        else:
            self.session.reset(phone_number)
            return [text_response(f"✅ {format_amount(amount)} payment noted.")]

    def _start_payment_flow(self, phone_number: str) -> list:
        """Show debtors list for payment recording."""
        debts = self.db.get_all_debtors(phone_number) or []
        unpaid = [d for d in debts if not d.get("paid")]

        if not unpaid:
            return [text_response("No outstanding debts to record payment for.")]

        rows = []
        for d in unpaid[:10]:
            name = d.get("name", "Unknown")
            amt = format_amount(d.get("amount", 0))
            rows.append({"id": f"debt_pay_{name}", "title": name, "description": f"Owes {amt}"})

        return [list_response(
            header="💵 Record Payment",
            body="Who made a payment?",
            button_text="Select Person",
            sections=[{"title": "Debtors", "rows": rows}]
        )]

    def _show_remind_list(self, phone_number: str) -> list:
        """Show debtors as tappable list for sending reminders."""
        debts = self.db.get_all_debtors(phone_number) or []
        unpaid = [d for d in debts if not d.get("paid") and d.get("amount", 0) > 0]

        if not unpaid:
            return [text_response("✅ No outstanding debts to remind about.")]

        rows = []
        for d in unpaid[:10]:
            name       = d.get("name", "Unknown")
            amount     = d.get("amount", 0)
            contact_id = d.get("contact_id", name.lower().replace(" ", "_"))
            rows.append({
                "id": f"debt_remind_{contact_id}",
                "title": f"⏰ {name}"[:24],
                "description": f"Owes {format_amount(amount)}"[:72],
            })

        return [list_response(
            header="⏰ Send Reminder",
            body="Pick a debtor to send a payment reminder:",
            button_text="Select Person",
            sections=[{"title": "Debtors", "rows": rows}]
        )]

    def _send_reminder(self, phone_number: str, contact_id: str) -> list:
        """Send a payment reminder to a debtor via WhatsApp (or provide copy text)."""
        # Look up contact
        contact_name = contact_id.replace("_", " ").title()
        contact = self.db.get_contact_by_name(phone_number, contact_name)

        if not contact:
            return [text_response(f"❓ Contact *{contact_name}* not found.")]

        name         = contact.get("name", contact_name)
        debt_amount  = int(contact.get("debt_owed_to_me", 0))
        debtor_phone = contact.get("contact_phone", "")

        if debt_amount <= 0:
            return [text_response(f"✅ *{name}* doesn't owe you anything!")]

        # Get business name for the reminder message
        user = self.db.get_user(phone_number)
        business_name = user.get("business_name", "your supplier") if user else "your supplier"

        # Build the reminder message
        reminder_text = (
            f"Hello {name},\n\n"
            f"This is a friendly reminder from *{business_name}* "
            f"that you have an outstanding balance of *₦{debt_amount:,}*.\n\n"
            f"Please make payment at your earliest convenience.\n\n"
            f"Thank you! 🙏"
        )

        if debtor_phone and len(debtor_phone) >= 10:
            # Has phone number — return marker for main.py to send via WhatsApp
            return [
                {"type": "__SEND_REMINDER__", "content": {
                    "debtor_phone": debtor_phone,
                    "debtor_name": name,
                    "reminder_text": reminder_text,
                    "amount": debt_amount,
                }},
                text_response(
                    f"✅ *Reminder sent to {name}!*\n\n"
                    f"📱 Sent to: {debtor_phone}\n"
                    f"💰 Amount: ₦{debt_amount:,}\n\n"
                    f"_They'll receive the message on WhatsApp._"
                )
            ]
        else:
            # No phone number — show copy-paste text
            return [text_response(
                f"⏰ *Reminder for {name}*\n\n"
                f"📱 No phone number saved for {name}.\n\n"
                f"Copy and send this message manually:\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{reminder_text}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"_To save their number: type \"save number {name} 08012345678\"_"
            )]
