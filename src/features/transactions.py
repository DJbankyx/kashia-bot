# src/features/transactions.py
"""Transaction recording — the ONLY natural language feature.

Handles:
- Free-text transaction parsing (AI-powered)
- Confirmation flow (yes/edit/cancel buttons)
- Guided recording flow (button-driven step-by-step)
- Credit/debt detection
- CRM prompt trigger (after save)
- Edit/delete existing transactions
"""

import logging
import re
import traceback
from datetime import datetime

from core import states
from utils.parser import parse_amount, detect_transaction_type, extract_vendor_name
from utils.whatsapp_ui import (
    text_response, button_response, confirm_buttons, format_amount
)

logger = logging.getLogger(__name__)

# Credit signals anywhere in text
CREDIT_SIGNALS = ['on credit', 'credit', 'owe', 'owes', 'debt', 'balance', 'not paid', 'unpaid']
# Payment signals
PAYMENT_SIGNALS = ['paid', 'pay', 'received', 'settled', 'cleared', 'payment']
# Buyer direction signals
BUYER_SIGNALS = ['i bought', 'i purchased', 'bought from', 'i owe']


class TransactionHandler:
    """Handles all transaction recording flows."""

    def __init__(self, session_mgr, database, categorizer, get_industry_fn):
        self.session = session_mgr
        self.db = database
        self.categorizer = categorizer
        self._get_industry = get_industry_fn  # function(phone_number) → industry handler

    # ═══════════════════════════════════════════════════════════
    # RECORD — The NLP path (free text → AI parse → confirm)
    # ═══════════════════════════════════════════════════════════

    def record(self, phone_number: str, text: str, session: dict) -> list:
        """Parse free text as a transaction and show confirmation."""
        try:
            # Check for payment/debt signals first
            text_lower = text.lower()
            if self._is_payment(text_lower):
                return self._handle_payment_text(phone_number, text)

            # Parse amount
            amount = parse_amount(text)
            if not amount:
                # No financial signal — not a transaction
                return [text_response(
                    "💬 Just type what you bought or sold and I'll record it!\n\n"
                    "Example: _sold shoes 50K to Sandra_\n\n"
                    "Or tap ☰ Menu below for other options."
                )]

            # AI categorization — try rich parse first
            # Get user's industry for better categorization
            user = self.db.get_user(phone_number)
            industry_class = user.get("industry_class", "trading") if user else "trading"

            ai_result = self.categorizer._call_openai_rich(
                text,
                business_type=industry_class,
                phone_number=phone_number,
                industry_class=industry_class
            )

            if ai_result:
                # Rich parse returns: transaction_type, item_name, brand, quantity,
                # vendor_or_customer, category, sub_category, confidence
                tx_type = ai_result.get("transaction_type", detect_transaction_type(text))
                description = ai_result.get("item_name", text)
                category = ai_result.get("category", "Uncategorized")
                vendor = ai_result.get("vendor_or_customer", "") or extract_vendor_name(text)
                quantity = ai_result.get("quantity", "")
                brand = ai_result.get("brand", "")
            else:
                # Fallback to simple categorize + parser
                simple_result = self.categorizer.categorize(text, phone_number) or {}
                tx_type = detect_transaction_type(text)
                description = text
                category = simple_result.get("category", "Uncategorized")
                vendor = extract_vendor_name(text)
                quantity = ""
                brand = ""

            # Check for credit signals
            has_credit = any(sig in text_lower for sig in CREDIT_SIGNALS)

            # Normalize tx_type naming
            if tx_type in ("income", "sale", "sales"):
                tx_type = "sale"
            elif tx_type in ("expense", "cost"):
                tx_type = "expense"
            elif tx_type in ("purchase", "buy"):
                tx_type = "purchase"

            tx_data = {
                "amount": float(amount),
                "type": tx_type,
                "description": description,
                "category": category,
                "vendor": vendor,
                "quantity": str(quantity) if quantity else "",
                "brand": brand,
                "raw_text": text,
                "has_credit": has_credit,
            }

            # Save state
            self.session.save(phone_number, states.AWAITING_CONFIRMATION, {
                "pending_transaction": tx_data,
            })

            # Build confirmation message
            return self._build_confirmation(tx_data, has_credit)

        except Exception as e:
            logger.error(f"Transaction record error: {e}\n{traceback.format_exc()}")
            return [text_response(
                "😅 I had trouble understanding that. Try something like:\n\n"
                "_sold shoes 50K_\n_bought rice from Alhaji 30000_\n_transport 5K_"
            )]

    def _build_confirmation(self, tx_data: dict, has_credit: bool) -> list:
        """Build the transaction confirmation card."""
        tx_type = tx_data["type"]
        amount = tx_data["amount"]
        description = tx_data["description"]
        category = tx_data["category"]
        vendor = tx_data["vendor"]
        quantity = tx_data.get("quantity", "")
        brand = tx_data.get("brand", "")

        # Type emoji
        type_emoji = {"sale": "💰", "expense": "💸", "purchase": "📦"}.get(tx_type, "📝")
        type_label = tx_type.capitalize()

        # Build message
        lines = [
            f"{type_emoji} *{type_label}* — {format_amount(amount)}",
            f"📦 {description}",
        ]
        if brand:
            lines.append(f"🏷️ {brand}")
        if quantity:
            lines.append(f"📐 Qty: {quantity}")
        if vendor:
            lines.append(f"👤 {vendor}")
        lines.append(f"📁 {category}")

        if has_credit:
            lines.append("\n📝 _On credit_")

        lines.append("\n✅ *Correct?*")

        body = "\n".join(lines)

        return [button_response(body, confirm_buttons())]

    # ═══════════════════════════════════════════════════════════
    # CONFIRMATION — User taps Yes/Edit/Cancel
    # ═══════════════════════════════════════════════════════════

    def handle_confirmation(self, phone_number: str, text: str, session: dict) -> list:
        """Handle response to the confirmation card."""
        text_lower = text.lower().strip()
        context = session.get("context", {})
        tx_data = context.get("pending_transaction", {})

        # ── YES — Save the transaction ──
        if text_lower in ("yes", "y", "correct", "confirm_yes", "✅ yes"):
            return self._save_transaction(phone_number, tx_data)

        # ── EDIT — Show edit options ──
        if text_lower in ("edit", "change", "fix", "confirm_edit", "✏️ edit"):
            return self._show_edit_options(phone_number, tx_data)

        # ── CANCEL ──
        if text_lower in ("no", "n", "cancel", "wrong", "confirm_cancel", "❌ cancel"):
            self.session.reset(phone_number)
            return [text_response("❌ Cancelled. Send another transaction or tap the menu.")]

        # ── ESCAPE: User sent a new transaction (has an amount) ──
        amount = parse_amount(text)
        if amount:
            self.session.reset(phone_number)
            # Re-process as new transaction
            new_session = self.session.get(phone_number)
            return self.record(phone_number, text, new_session)

        # ── ESCAPE: Credit/payment pattern ──
        if any(sig in text_lower for sig in CREDIT_SIGNALS + PAYMENT_SIGNALS):
            self.session.reset(phone_number)
            new_session = self.session.get(phone_number)
            return self.record(phone_number, text, new_session)

        # ── Didn't understand — nudge with buttons ──
        return [button_response(
            "👆 Tap a button above, or type *yes*, *edit*, or *cancel*.",
            confirm_buttons()
        )]

    def _save_transaction(self, phone_number: str, tx_data: dict) -> list:
        """Save confirmed transaction to DynamoDB."""
        try:
            has_credit = tx_data.get("has_credit", False)
            vendor = tx_data.get("vendor", "")

            # Determine credit direction
            if has_credit and vendor:
                return self._save_credit_transaction(phone_number, tx_data)

            # Normal save
            tx_id = self.db.save_transaction(phone_number, {
                "amount": tx_data["amount"],
                "type": tx_data["type"],
                "description": tx_data["description"],
                "category": tx_data["category"],
                "vendor": vendor,
                "quantity": tx_data.get("quantity", ""),
                "brand": tx_data.get("brand", ""),
                "raw_text": tx_data.get("raw_text", ""),
                "date": datetime.now().strftime("%Y-%m-%d"),
                "timestamp": datetime.now().isoformat(),
            })

            self.session.reset(phone_number)

            # Trigger CRM hint for large transactions without vendor
            if not vendor and tx_data["amount"] >= 10000 and tx_data["type"] in ("sale", "purchase"):
                return self._trigger_crm_hint(phone_number, tx_id, tx_data)

            return [text_response(
                f"✅ Saved! {format_amount(tx_data['amount'])} {tx_data['type']}."
            )]

        except Exception as e:
            logger.error(f"Save transaction error: {e}\n{traceback.format_exc()}")
            self.session.reset(phone_number)
            return [text_response(f"❌ Error saving: {str(e)[:100]}. Please try again.")]

    def _save_credit_transaction(self, phone_number: str, tx_data: dict) -> list:
        """Save a transaction + record as debt."""
        try:
            vendor = tx_data["vendor"]
            amount = tx_data["amount"]
            tx_type = tx_data["type"]
            description = tx_data["description"]
            raw_text = tx_data.get("raw_text", "").lower()

            # Save the transaction
            tx_id = self.db.save_transaction(phone_number, {
                "amount": amount,
                "type": tx_type,
                "description": description,
                "category": tx_data["category"],
                "vendor": vendor,
                "raw_text": tx_data.get("raw_text", ""),
                "date": datetime.now().strftime("%Y-%m-%d"),
                "timestamp": datetime.now().isoformat(),
                "payment_method": "credit",
            })

            # Determine direction: who owes whom?
            is_buyer = any(sig in raw_text for sig in BUYER_SIGNALS) or tx_type == "purchase"

            if is_buyer:
                # I owe them
                self.db.record_debt(phone_number, vendor, amount, 'i_owe', f"Credit purchase: {description}")
                self.session.reset(phone_number)
                return [text_response(
                    f"✅ Saved! {format_amount(amount)} purchase on credit.\n"
                    f"📝 You owe *{vendor}* {format_amount(amount)}."
                )]
            else:
                # They owe me
                self.db.record_debt(phone_number, vendor, amount, 'owed_to_me', f"Credit sale: {description}")
                self.session.reset(phone_number)
                return [text_response(
                    f"✅ Saved! {format_amount(amount)} sale on credit.\n"
                    f"📝 *{vendor}* owes you {format_amount(amount)}."
                )]

        except Exception as e:
            logger.error(f"Credit save error: {e}\n{traceback.format_exc()}")
            self.session.reset(phone_number)
            return [text_response(f"❌ Error: {str(e)[:100]}. Please try again.")]

    def _trigger_crm_hint(self, phone_number: str, tx_id: str, tx_data: dict) -> list:
        """Ask who the transaction was with (optional, skippable)."""
        self.session.save(phone_number, states.CRM_HINT, {
            "transaction_id": tx_id,
            "tx_type": tx_data["type"],
            "amount": tx_data["amount"],
            "crm_step": "ask_name",
        })

        label = "sell to" if tx_data["type"] == "sale" else "buy from"
        return [text_response(
            f"✅ Saved! {format_amount(tx_data['amount'])} {tx_data['type']}.\n\n"
            f"💡 Who did you {label}?\n\n"
            f"_Type their name, or just send your next transaction._"
        )]

    def _show_edit_options(self, phone_number: str, tx_data: dict) -> list:
        """Show what can be edited."""
        from utils.whatsapp_ui import list_response
        self.session.save(phone_number, states.AWAITING_CORRECTION, {
            "pending_transaction": tx_data,
            "edit_step": "choose_field",
        })

        rows = [
            {"id": "edit_amount", "title": "💰 Amount", "description": f"Currently: {format_amount(tx_data['amount'])}"},
            {"id": "edit_type", "title": "📝 Type", "description": f"Currently: {tx_data['type']}"},
            {"id": "edit_description", "title": "📦 Description", "description": f"Currently: {tx_data['description'][:50]}"},
            {"id": "edit_category", "title": "📁 Category", "description": f"Currently: {tx_data['category']}"},
        ]
        if tx_data.get("vendor"):
            rows.append({"id": "edit_vendor", "title": "👤 Name", "description": f"Currently: {tx_data['vendor']}"})

        return [list_response(
            header="✏️ Edit Transaction",
            body="What would you like to change?",
            button_text="Select Field",
            sections=[{"title": "Fields", "rows": rows}]
        )]

    # ═══════════════════════════════════════════════════════════
    # CORRECTION — After user taps "Edit"
    # ═══════════════════════════════════════════════════════════

    def handle_correction(self, phone_number: str, text: str, session: dict) -> list:
        """Handle the edit flow — user picks a field, types new value."""
        context = session.get("context", {})
        tx_data = context.get("pending_transaction", {})
        edit_step = context.get("edit_step", "choose_field")
        edit_field = context.get("edit_field", "")

        text_lower = text.lower().strip()

        # Field selection (from list button)
        if edit_step == "choose_field" or text_lower.startswith("edit_"):
            field_map = {
                "edit_amount": "amount",
                "edit_type": "type",
                "edit_description": "description",
                "edit_category": "category",
                "edit_vendor": "vendor",
            }
            field = field_map.get(text_lower, "")
            if field:
                self.session.save(phone_number, states.AWAITING_CORRECTION, {
                    "pending_transaction": tx_data,
                    "edit_step": "enter_value",
                    "edit_field": field,
                })
                prompts = {
                    "amount": "💰 Enter the correct amount:",
                    "type": "📝 Enter type: *sale*, *purchase*, or *expense*",
                    "description": "📦 Enter the correct description:",
                    "category": "📁 Enter the correct category:",
                    "vendor": "👤 Enter the correct name:",
                }
                return [text_response(prompts.get(field, "Enter new value:"))]

            # Unrecognized — show options again
            return self._show_edit_options(phone_number, tx_data)

        # Value entry
        if edit_step == "enter_value" and edit_field:
            if edit_field == "amount":
                amount = parse_amount(text)
                if not amount:
                    return [text_response("Please enter a valid amount (e.g. 50000, 150K):")]
                tx_data["amount"] = float(amount)
            elif edit_field == "type":
                if text_lower in ("sale", "purchase", "expense"):
                    tx_data["type"] = text_lower
                else:
                    return [text_response("Please enter: *sale*, *purchase*, or *expense*")]
            else:
                tx_data[edit_field] = text.strip()

            # Show updated confirmation
            self.session.save(phone_number, states.AWAITING_CONFIRMATION, {
                "pending_transaction": tx_data,
            })
            return self._build_confirmation(tx_data, tx_data.get("has_credit", False))

        # Fallback
        self.session.reset(phone_number)
        return [text_response("👍 Let's start over. Send your transaction again.")]

    # ═══════════════════════════════════════════════════════════
    # GUIDED RECORDING — Button-driven step-by-step
    # ═══════════════════════════════════════════════════════════

    def handle_guided_step(self, phone_number: str, text: str, session: dict) -> list:
        """Handle guided recording flow (button-initiated)."""
        context = session.get("context", {})
        step = context.get("guided_step", "item")
        guided_type = context.get("guided_type", "sale")
        guided_data = context.get("guided_data", {})
        text_lower = text.lower().strip()

        # Skip
        if text_lower in ("skip", "no", "nah"):
            return self._advance_guided(phone_number, step, guided_type, guided_data, skip=True)

        # Back
        if text_lower == "__back__" or text_lower == "back":
            return self._go_back_guided(phone_number, step, guided_type, guided_data)

        # Process current step
        if step == "item":
            guided_data["item"] = text.strip()
            return self._advance_guided(phone_number, "item", guided_type, guided_data)

        if step == "amount":
            amount = parse_amount(text)
            if not amount:
                return [text_response("💰 Please enter the amount (e.g. 50000, 150K, 1.2M):")]
            guided_data["amount"] = float(amount)
            return self._advance_guided(phone_number, "amount", guided_type, guided_data)

        if step == "vendor":
            guided_data["vendor"] = text.strip()
            return self._advance_guided(phone_number, "vendor", guided_type, guided_data)

        if step == "details":
            guided_data["details"] = text.strip()
            return self._advance_guided(phone_number, "details", guided_type, guided_data)

        # Unknown step — build transaction from what we have
        return self._finalize_guided(phone_number, guided_type, guided_data)

    def _advance_guided(self, phone_number: str, completed_step: str, guided_type: str, data: dict, skip: bool = False) -> list:
        """Move to next guided step."""
        # Step order: item → amount → vendor → details → confirm
        steps = ["item", "amount", "vendor", "details"]
        current_idx = steps.index(completed_step) if completed_step in steps else -1
        next_idx = current_idx + 1

        if next_idx >= len(steps):
            return self._finalize_guided(phone_number, guided_type, data)

        next_step = steps[next_idx]
        industry = self._get_industry(phone_number)

        # Get prompt for next step
        prompt_keys = {
            "amount": "ask_amount",
            "vendor": f"ask_vendor_{guided_type}" if guided_type in ("sale", "purchase") else "ask_vendor_sale",
            "details": "ask_details",
        }
        prompt_key = prompt_keys.get(next_step, "ask_amount")
        prompt = industry.get_guided_prompt(prompt_key) if industry else f"Enter {next_step}:"

        self.session.save(phone_number, states.GUIDED_RECORDING, {
            "guided_type": guided_type,
            "guided_step": next_step,
            "guided_data": data,
        })

        return [text_response(prompt)]

    def _go_back_guided(self, phone_number: str, current_step: str, guided_type: str, data: dict) -> list:
        """Go back one step in guided flow."""
        steps = ["item", "amount", "vendor", "details"]
        current_idx = steps.index(current_step) if current_step in steps else 0

        if current_idx <= 0:
            # Already at first step — cancel
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled. Send a transaction or tap the menu.")]

        prev_step = steps[current_idx - 1]
        # Remove the data for current step
        data.pop(current_step, None)

        industry = self._get_industry(phone_number)
        prompt_key = f"ask_item_{guided_type}" if prev_step == "item" else f"ask_{prev_step}"
        prompt = industry.get_guided_prompt(prompt_key) if industry else f"Enter {prev_step}:"

        self.session.save(phone_number, states.GUIDED_RECORDING, {
            "guided_type": guided_type,
            "guided_step": prev_step,
            "guided_data": data,
        })

        return [text_response(f"⬅️ Back!\n\n{prompt}")]

    def _finalize_guided(self, phone_number: str, guided_type: str, data: dict) -> list:
        """Build a transaction text from guided data and run through normal flow."""
        item = data.get("item", "item")
        amount = data.get("amount")
        vendor = data.get("vendor", "")
        details = data.get("details", "")

        if not amount:
            # Shouldn't happen, but handle gracefully
            self.session.save(phone_number, states.GUIDED_RECORDING, {
                "guided_type": guided_type,
                "guided_step": "amount",
                "guided_data": data,
            })
            return [text_response("💰 How much was it? (e.g. 50000, 150K)")]

        # Build natural text for AI parser
        parts = []
        if guided_type == "sale":
            parts.append(f"sold {item}")
        elif guided_type == "purchase":
            parts.append(f"bought {item}")
        else:
            parts.append(item)

        if vendor:
            if guided_type == "sale":
                parts.append(f"to {vendor}")
            elif guided_type == "purchase":
                parts.append(f"from {vendor}")
            else:
                parts.append(vendor)

        if details and details.lower() != "skip":
            parts.append(details)

        parts.append(str(int(amount)))

        text = " ".join(parts)

        # Reset state and process as normal transaction
        self.session.reset(phone_number)
        new_session = self.session.get(phone_number)
        return self.record(phone_number, text, new_session)

    # ═══════════════════════════════════════════════════════════
    # EDIT/DELETE EXISTING — (from report/list view)
    # ═══════════════════════════════════════════════════════════

    def handle_edit(self, phone_number: str, text: str, session: dict) -> list:
        """Handle edit/delete of existing saved transactions."""
        context = session.get("context", {})
        state = session.get("state", "")

        if state == states.DELETE_CONFIRM:
            if text.lower() in ("yes", "y", "confirm", "btn_yes"):
                tx_id = context.get("delete_tx_id")
                if tx_id:
                    self.db.delete_transaction(tx_id, phone_number)
                    self.session.reset(phone_number)
                    return [text_response("🗑️ Deleted! Send a transaction or tap the menu.")]
            self.session.reset(phone_number)
            return [text_response("👍 Kept it. Send a transaction or tap the menu.")]

        # For now, basic edit handling — can be expanded later
        self.session.reset(phone_number)
        return [text_response("✏️ Edit feature coming in the next update. Send a transaction or tap the menu.")]

    # ═══════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════

    def _is_payment(self, text_lower: str) -> bool:
        """Check if text looks like a debt payment rather than a new transaction."""
        # Pattern: "[name] paid [amount]" or "received [amount] from [name]"
        payment_patterns = [
            r'\b\w+\s+paid\s+\d',
            r'\breceived\s+\d',
            r'\b\w+\s+settled\s+\d',
            r'\b\w+\s+cleared\s+\d',
        ]
        return any(re.search(p, text_lower) for p in payment_patterns)

    def _handle_payment_text(self, phone_number: str, text: str) -> list:
        """Route payment text to debt handler. Returns marker for router."""
        # This will be routed to debt.handle_payment by the router
        # For now, save state and return marker
        self.session.save(phone_number, states.DEBT_PAYMENT, {
            "payment_text": text,
        })
        return [{"type": "__ROUTE_TO_DEBT__", "content": text}]
