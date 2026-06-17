# src/services/conversation_engine.py
"""Conversation Engine - state machine that manages all chat flows"""

import logging
from datetime import datetime

from src.utils.parser import parse_amount, detect_transaction_type, extract_vendor_name
from src.services.database import Database
from src.services.categorizer import TransactionCategorizer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ==========================================
# STATES
# ==========================================

STATE_NEW_USER = "NEW_USER"
STATE_ONBOARDING = "ONBOARDING"
STATE_IDLE = "IDLE"
STATE_RECORDING = "RECORDING"
STATE_AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
STATE_AWAITING_CORRECTION = "AWAITING_CORRECTION"
STATE_VIEWING_REPORT = "VIEWING_REPORT"
STATE_EXPORTING = "EXPORTING"
STATE_INVOICING = "INVOICING"


# ==========================================
# COMMANDS (what the user can type)
# ==========================================

COMMANDS = {
    'report': ['report', 'summary', 'how much', 'balance'],
    'today': ['today', 'today report'],
    'week': ['this week', 'week', 'weekly'],
    'month': ['this month', 'month', 'monthly'],
    'help': ['help', 'menu', 'commands', 'what can you do'],
    'export': ['export', 'excel', 'csv', 'download', 'spreadsheet'],
    'invoice': ['invoice', 'generate invoice'],
    'receipt': ['receipt'],
    'statement': ['statement', 'financial statement'],
    'customers': ['customers', 'customer', 'who buy from me'],
    'suppliers': ['suppliers', 'supplier', 'who i buy from'],
    'contacts': ['contacts', 'contact', 'crm'],
    'undo': ['undo', 'delete last', 'cancel last', 'remove last'],
    'upgrade': ['upgrade', 'plan', 'pricing', 'subscribe'],
}


class ConversationEngine:
    """Manages the chat flow state machine"""

    def __init__(self):
        self.db = Database()
        self.categorizer = TransactionCategorizer(database=self.db)

    def process_message(self, phone_number, text, message_type="text"):
        """
        Main entry point — processes a message and returns response(s).

        Args:
            phone_number: sender's phone (e.g., "2348012345678")
            text: message content
            message_type: "text", "button_reply", "list_reply"

        Returns:
            list of response dicts:
            [{"type": "text", "content": "..."}, {"type": "buttons", ...}]
        """
        # Get or create session
        session = self.db.get_session(phone_number)

        # Determine state
        if session is None:
            # Check if user exists but session expired
            if self.db.user_exists(phone_number):
                state = STATE_IDLE
                context = {}
            else:
                state = STATE_NEW_USER
                context = {}
        else:
            state = session.get('state', STATE_IDLE)
            context = session.get('context', {})

        # Route to correct handler based on state
        if state == STATE_NEW_USER:
            return self._handle_new_user(phone_number, text)

        elif state == STATE_ONBOARDING:
            return self._handle_onboarding(phone_number, text, context)

        elif state == STATE_IDLE:
            return self._handle_idle(phone_number, text)

        elif state == STATE_AWAITING_CONFIRMATION:
            return self._handle_confirmation(phone_number, text, context)

        elif state == STATE_AWAITING_CORRECTION:
            return self._handle_correction(phone_number, text, context)

        elif state == STATE_VIEWING_REPORT:
            return self._handle_report_selection(phone_number, text)

        elif state == STATE_EXPORTING:
            return self._handle_export_selection(phone_number, text)

        elif state == STATE_INVOICING:
            return self._handle_invoice_input(phone_number, text, context)

        else:
            # Unknown state — reset to idle
            self.db.clear_session(phone_number)
            return self._handle_idle(phone_number, text)

    # ==========================================
    # STATE HANDLERS
    # ==========================================

    def _handle_new_user(self, phone_number, text):
        """Welcome a new user and start onboarding"""
        # Save session as onboarding
        self.db.save_session(phone_number, STATE_ONBOARDING, {"step": 1})

        return [
            {"type": "text", "content": (
                "Welcome to *Kashia*! 🎉\n\n"
                "I help you record and organize your business money.\n\n"
                "Just tell me what you spent or received — in your own words — "
                "and I'll handle the accounting for you."
            )},
            {"type": "buttons", "content": {
                "body": "What type of business do you run?",
                "buttons": [
                    {"id": "biz_trading", "title": "Buy & Sell Goods"},
                    {"id": "biz_services", "title": "I Offer Services"},
                    {"id": "biz_food", "title": "Food & Drinks"},
                ]
            }}
        ]

    def _handle_onboarding(self, phone_number, text, context):
        """Handle onboarding flow (business type selection)"""
        # Map responses to business types
        business_types = {
            "biz_trading": "trading",
            "buy & sell goods": "trading",
            "biz_services": "services",
            "i offer services": "services",
            "biz_food": "food",
            "food & drinks": "food",
            "1": "trading",
            "2": "services",
            "3": "food",
        }

        # Normalize input
        business_type = business_types.get(text.lower().strip(), None)

        if not business_type:
            # Try to match partial
            text_lower = text.lower()
            if any(w in text_lower for w in ['buy', 'sell', 'trad', 'goods']):
                business_type = "trading"
            elif any(w in text_lower for w in ['service', 'barb', 'tailor', 'repair']):
                business_type = "services"
            elif any(w in text_lower for w in ['food', 'drink', 'cook', 'restaurant']):
                business_type = "food"
            else:
                business_type = "trading"  # Default

        # Create the user
        self.db.create_user(phone_number, business_type=business_type)
        self.db.save_session(phone_number, STATE_IDLE, {})

        return [
            {"type": "text", "content": (
                f"✅ Great! I've set you up as a *{business_type}* business.\n\n"
                "You're ready to go! Here's how to use me:\n\n"
                "📝 *Record a transaction:*\n"
                "Just type naturally, e.g.:\n"
                '• "I buy rice 3 bags 95K"\n'
                '• "Sold goods to Alhaji 350,000"\n'
                '• "Paid Femi 40K salary"\n\n'
                "📊 *See reports:* Type \"report\"\n"
                "📋 *See contacts:* Type \"customers\" or \"suppliers\"\n"
                "📎 *Export:* Type \"export\"\n"
                "❓ *Help:* Type \"help\"\n\n"
                "Try recording your first transaction now! 👇"
            )}
        ]

    def _handle_idle(self, phone_number, text):
        """
        User is idle — detect if they're giving a command or recording a transaction.
        """
        text_lower = text.lower().strip()

        # Check for commands
        command = self._detect_command(text_lower)

        if command == 'help':
            return self._show_help()

        elif command == 'report' or command == 'today' or command == 'week' or command == 'month':
            return self._handle_report(phone_number, command)

        elif command == 'export':
            self.db.save_session(phone_number, STATE_EXPORTING, {})
            return [{"type": "buttons", "content": {
                "body": "📊 What would you like to export?",
                "buttons": [
                    {"id": "export_month", "title": "This Month (Excel)"},
                    {"id": "export_csv", "title": "Full History (CSV)"},
                    {"id": "export_contacts", "title": "Contacts List"},
                ]
            }}]

        elif command == 'invoice':
            self.db.save_session(phone_number, STATE_INVOICING, {"step": "ask_details"})
            return [{"type": "text", "content": (
                "📄 Let's create an invoice.\n\n"
                "Type it like this:\n"
                "*[Customer name] [amount] for [item/description]*\n\n"
                "Example: \"Alhaji Musa 350,000 for cement supply\""
            )}]

        elif command == 'customers':
            return self._show_contacts(phone_number, "customer")

        elif command == 'suppliers':
            return self._show_contacts(phone_number, "supplier")

        elif command == 'undo':
            return self._handle_undo(phone_number)

        elif command == 'upgrade':
            return self._show_upgrade_options()

        # No command detected → treat as a transaction
        return self._handle_transaction(phone_number, text)

    def _handle_transaction(self, phone_number, text):
        """Parse a transaction and show AI suggestion for confirmation"""
        # Parse the message
        amount = parse_amount(text)

        if not amount:
            # Couldn't find an amount — ask for it
            self.db.save_session(phone_number, STATE_RECORDING, {"description": text})
            return [{"type": "text", "content": (
                "💰 How much was it? (Just type the amount)\n\n"
                "E.g.: 95000 or 95K or ₦95,000"
            )}]

        # Got amount — categorize
        tx_type = detect_transaction_type(text)
        vendor = extract_vendor_name(text)
        result = self.categorizer.categorize(text, phone_number)

        category = result['category']
        sub_category = result.get('sub_category', '')
        confidence = result.get('confidence', 0)

        # Store pending transaction in session
        pending = {
            "amount": amount,
            "type": tx_type,
            "description": text,
            "category": category,
            "sub_category": sub_category,
            "vendor": vendor or "",
            "confidence": confidence,
        }
        self.db.save_session(phone_number, STATE_AWAITING_CONFIRMATION, pending)

        # Format response
        type_emoji = "💰" if tx_type == "income" else "💸"
        cat_emoji = self._get_category_emoji(category)

        response_text = (
            f"📝 Recorded!\n\n"
            f"{type_emoji} *₦{amount:,}* ({tx_type.title()})\n"
            f"{cat_emoji} {category}"
        )
        if sub_category:
            response_text += f" → {sub_category}"
        if vendor:
            response_text += f"\n🏪 {vendor}"

        response_text += "\n\n✅ Correct?"

        return [{"type": "buttons", "content": {
            "body": response_text,
            "buttons": [
                {"id": "confirm_yes", "title": "✅ Yes"},
                {"id": "confirm_change", "title": "✏️ Change"},
                {"id": "confirm_undo", "title": "↩️ Cancel"},
            ]
        }}]

    def _handle_confirmation(self, phone_number, text, context):
        """Handle user confirming or rejecting AI suggestion"""
        text_lower = text.lower().strip()

        # Accept
        if text_lower in ['yes', 'y', 'correct', '✅ yes', 'confirm_yes', '1']:
            # Save the transaction
            tx = self.db.save_transaction(
                phone_number=phone_number,
                amount=context['amount'],
                tx_type=context['type'],
                description=context['description'],
                category=context['category'],
                sub_category=context.get('sub_category', ''),
                vendor=context.get('vendor', ''),
                confidence=context.get('confidence', 0)
            )

            # Save merchant memory
            vendor = context.get('vendor', '')
            if vendor:
                self.db.save_merchant(
                    phone_number, vendor,
                    context['category'],
                    context.get('sub_category', '')
                )

            # Update contact totals if vendor exists
            if vendor:
                self.db.update_contact_totals(
                    phone_number, vendor,
                    context['amount'], context['type']
                )

            self.db.save_session(phone_number, STATE_IDLE, {})

            return [{"type": "text", "content": (
                f"✅ Saved!\n\n"
                f"Record another transaction or type *help* for options."
            )}]

        # Change category
        elif text_lower in ['change', 'no', 'n', 'wrong', '✏️ change', 'confirm_change', '2']:
            self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)

            # Show category options
            categories_text = "\n".join([
                f"{i+1}. {cat}" for i, cat in enumerate(
                    ["Goods & Stock", "Sales & Income", "Rent & Space",
                     "Utilities & Services", "Transport & Logistics",
                     "People & Labour", "Equipment & Tools", "Money Matters",
                     "Marketing & Customers", "Government & Compliance", "Personal"]
                )
            ])

            return [{"type": "text", "content": (
                f"📂 What's the correct category?\n\n"
                f"{categories_text}\n\n"
                f"Reply with the *number* or *name*."
            )}]

        # Cancel
        elif text_lower in ['cancel', 'undo', '↩️ cancel', 'confirm_undo', '3']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "↩️ Cancelled. Transaction not saved."}]

        else:
            # Unclear response
            return [{"type": "buttons", "content": {
                "body": "I didn't understand. Is the category correct?",
                "buttons": [
                    {"id": "confirm_yes", "title": "✅ Yes"},
                    {"id": "confirm_change", "title": "✏️ Change"},
                    {"id": "confirm_undo", "title": "↩️ Cancel"},
                ]
            }}]

    def _handle_correction(self, phone_number, text, context):
        """Handle user providing the correct category"""
        # Category mapping (number or name)
        category_map = {
            "1": "Goods & Stock", "goods": "Goods & Stock", "goods & stock": "Goods & Stock",
            "2": "Sales & Income", "sales": "Sales & Income", "income": "Sales & Income",
            "3": "Rent & Space", "rent": "Rent & Space",
            "4": "Utilities & Services", "utilities": "Utilities & Services", "utility": "Utilities & Services",
            "5": "Transport & Logistics", "transport": "Transport & Logistics",
            "6": "People & Labour", "labour": "People & Labour", "labor": "People & Labour", "salary": "People & Labour",
            "7": "Equipment & Tools", "equipment": "Equipment & Tools",
            "8": "Money Matters", "bank": "Money Matters", "money": "Money Matters",
            "9": "Marketing & Customers", "marketing": "Marketing & Customers",
            "10": "Government & Compliance", "government": "Government & Compliance", "tax": "Government & Compliance",
            "11": "Personal", "personal": "Personal",
        }

        text_lower = text.lower().strip()
        correct_category = category_map.get(text_lower)

        if not correct_category:
            # Try partial match
            for key, cat in category_map.items():
                if key in text_lower:
                    correct_category = cat
                    break

        if not correct_category:
            return [{"type": "text", "content": (
                "❓ I didn't recognize that category.\n"
                "Please reply with a number (1-11) or category name."
            )}]

        # Record the correction (AI learns)
        wrong_category = context.get('category', '')
        self.categorizer.record_correction(
            phone_number,
            context.get('description', ''),
            wrong_category,
            correct_category,
            vendor=context.get('vendor')
        )

        # Save transaction with corrected category
        self.db.save_transaction(
            phone_number=phone_number,
            amount=context['amount'],
            tx_type=context['type'],
            description=context['description'],
            category=correct_category,
            sub_category="",
            vendor=context.get('vendor', ''),
            confidence=100  # User-confirmed
        )

        # Update contact totals
        vendor = context.get('vendor', '')
        if vendor:
            self.db.update_contact_totals(phone_number, vendor, context['amount'], context['type'])

        self.db.save_session(phone_number, STATE_IDLE, {})

        return [{"type": "text", "content": (
            f"✅ Got it! Saved as *{correct_category}*.\n"
            f"I'll remember this for next time! 🧠\n\n"
            f"Record another transaction or type *help* for options."
        )}]

    # ==========================================
    # REPORT HANDLER
    # ==========================================

    def _handle_report(self, phone_number, period_command):
        """Generate and return a report summary"""
        now = datetime.now()

        if period_command == 'today':
            start_date = now.strftime('%Y-%m-%d')
            end_date = start_date
            period_label = "Today"
        elif period_command == 'week':
            # Start of week (Monday)
            start_of_week = now - __import__('datetime').timedelta(days=now.weekday())
            start_date = start_of_week.strftime('%Y-%m-%d')
            end_date = now.strftime('%Y-%m-%d')
            period_label = "This Week"
        else:  # month or report
            start_date = now.strftime('%Y-%m-01')
            end_date = now.strftime('%Y-%m-%d')
            period_label = now.strftime('%B %Y')

        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)

        if not transactions:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": f"📊 No transactions recorded for *{period_label}* yet."}]

        # Calculate totals
        income = sum(int(tx.get('amount', 0)) for tx in transactions if tx.get('type') == 'income')
        expenses = sum(int(tx.get('amount', 0)) for tx in transactions if tx.get('type') == 'expense')
        profit = income - expenses

        # Category breakdown (expenses only)
        categories = {}
        for tx in transactions:
            if tx.get('type') == 'expense':
                cat = tx.get('category', 'Other')
                categories[cat] = categories.get(cat, 0) + int(tx.get('amount', 0))

        # Sort by amount
        sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)

        # Format report
        profit_emoji = "📈" if profit >= 0 else "📉"

        report = f"📊 *{period_label} Report*\n\n"
        report += f"💰 Income: ₦{income:,}\n"
        report += f"💸 Expenses: ₦{expenses:,}\n"
        report += f"{profit_emoji} Profit: ₦{profit:,}\n\n"

        if sorted_cats:
            report += "📋 *Top Expenses:*\n"
            for i, (cat, amount) in enumerate(sorted_cats[:5], 1):
                emoji = self._get_category_emoji(cat)
                pct = int((amount / expenses * 100)) if expenses > 0 else 0
                report += f"{i}. {emoji} {cat}: ₦{amount:,} ({pct}%)\n"

        report += f"\n📝 Transactions: {len(transactions)}"

        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": report}]

    # ==========================================
    # HELPER METHODS
    # ==========================================

    def _detect_command(self, text_lower):
        """Check if the text matches a known command"""
        for command, keywords in COMMANDS.items():
            for keyword in keywords:
                if text_lower == keyword or text_lower.startswith(keyword):
                    return command
        return None

    def _show_help(self):
        """Show available commands"""
        return [{"type": "text", "content": (
            "🤖 *Kashia Commands:*\n\n"
            "📝 *Record:* Just type naturally\n"
            "   e.g. \"Bought rice 95K\"\n\n"
            "📊 *Reports:*\n"
            "   • \"report\" — this month\n"
            "   • \"today\" — today only\n"
            "   • \"week\" — this week\n\n"
            "📋 *Contacts:*\n"
            "   • \"customers\" — who buys from you\n"
            "   • \"suppliers\" — who you buy from\n\n"
            "📎 *Export:*\n"
            "   • \"export\" — get Excel/CSV\n\n"
            "📄 *Documents:*\n"
            "   • \"invoice\" — create invoice\n"
            "   • \"statement\" — financial statement\n\n"
            "↩️ *Other:*\n"
            "   • \"undo\" — delete last transaction\n"
            "   • \"upgrade\" — see plans\n"
            "   • \"help\" — show this message"
        )}]

    def _handle_undo(self, phone_number):
        """Delete the last transaction"""
        deleted = self.db.delete_last_transaction(phone_number)
        if deleted:
            amount = int(deleted.get('amount', 0))
            cat = deleted.get('category', '')
            return [{"type": "text", "content": (
                f"↩️ Deleted: ₦{amount:,} ({cat})\n\n"
                f"Transaction removed."
            )}]
        else:
            return [{"type": "text", "content": "❓ No recent transaction to undo."}]

    def _show_contacts(self, phone_number, contact_type):
        """Show top customers or suppliers"""
        contacts = self.db.get_contacts(phone_number)

        # Filter by type
        filtered = [c for c in contacts if c.get('type', '') in [contact_type, 'both']]

        if not filtered:
            return [{"type": "text", "content": (
                f"📋 No {contact_type}s found yet.\n"
                f"They'll appear automatically as you record transactions!"
            )}]

        # Sort by total amount
        if contact_type == "customer":
            filtered.sort(key=lambda x: int(x.get('total_received', 0)), reverse=True)
        else:
            filtered.sort(key=lambda x: int(x.get('total_paid', 0)), reverse=True)

        # Format list
        type_label = "Customers" if contact_type == "customer" else "Suppliers"
        result = f"📋 *Your Top {type_label}:*\n\n"

        for i, contact in enumerate(filtered[:5], 1):
            name = contact.get('name', 'Unknown')
            if contact_type == "customer":
                total = int(contact.get('total_received', 0))
            else:
                total = int(contact.get('total_paid', 0))
            result += f"{i}. {name} — ₦{total:,}\n"

        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": result}]

    def _show_upgrade_options(self):
        """Show pricing tiers"""
        return [{"type": "text", "content": (
            "💎 *Kashia Plans:*\n\n"
            "🆓 *Free* (current)\n"
            "   • 30 transactions/month\n"
            "   • Basic reports\n"
            "   • 5 exports/month\n\n"
            "💼 *Basic — ₦1,500/month*\n"
            "   • Unlimited transactions\n"
            "   • Full CRM\n"
            "   • Unlimited exports\n"
            "   • 10 invoices/month\n\n"
            "🏆 *Pro — ₦3,500/month*\n"
            "   • Everything in Basic\n"
            "   • Unlimited invoices\n"
            "   • PDF statements\n"
            "   • CRM insights\n\n"
            "Reply *BASIC* or *PRO* to upgrade."
        )}]

    def _handle_report_selection(self, phone_number, text):
        """Handle period selection for reports"""
        text_lower = text.lower().strip()
        if 'today' in text_lower:
            return self._handle_report(phone_number, 'today')
        elif 'week' in text_lower:
            return self._handle_report(phone_number, 'week')
        else:
            return self._handle_report(phone_number, 'month')

    def _handle_export_selection(self, phone_number, text):
        """Handle export option selection"""
        # TODO: Implement in Step 13 (Export Service)
        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": "📊 Export feature coming soon! For now, use 'report' to see your summary."}]

    def _handle_invoice_input(self, phone_number, text, context):
        """Handle invoice details input"""
        # TODO: Implement in Step 14 (PDF Generator)
        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": "📄 Invoice feature coming soon!"}]

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
