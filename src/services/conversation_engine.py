# src/services/conversation_engine.py
"""Conversation Engine - state machine that manages all chat flows"""

import logging
from datetime import datetime

from utils.parser import parse_amount, detect_transaction_type, extract_vendor_name
from services.database import Database
from services.categorizer import TransactionCategorizer

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
    'greeting': ['hi', 'hello', 'hey', 'good morning', 'good afternoon', 'good evening', 'how are you', 'sup', 'whats up'],
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
    'change_category': ['change category', 'update category', 'change business type', 'update business'],
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

        elif state == "CHANGING_CATEGORY":
            return self._handle_category_change_response(phone_number, text)

        else:
            # Unknown state — reset to idle
            self.db.clear_session(phone_number)
            return self._handle_idle(phone_number, text)

    # ==========================================
    # STATE HANDLERS
    # ==========================================

    def _handle_new_user(self, phone_number, text):
        """Welcome a new user and start onboarding"""
        self.db.save_session(phone_number, STATE_ONBOARDING, {"step": "ask_business_name"})

        return [
            {"type": "text", "content": (
                "Welcome to *Kashia*! \U0001f389\n\n"
                "I'm your AI bookkeeper \u2014 I help you record and organize "
                "your business money automatically.\n\n"
                "Let's get you set up real quick (3 quick questions).\n\n"
                "\U0001f449 *What's your business name?*"
            )}
        ]

    def _handle_onboarding(self, phone_number, text, context):
        """Handle multi-step conversational onboarding"""
        step = context.get("step", "ask_business_name")

        if step == "ask_business_name":
            business_name = text.strip()

            if len(business_name) < 2:
                return [
                    {"type": "text", "content": "Please type your business name (e.g. \"Mama T Foods\", \"TechFix Solutions\"):"}
                ]

            self.db.save_session(phone_number, STATE_ONBOARDING, {
                "step": "ask_business_description",
                "business_name": business_name
            })

            return [
                {"type": "text", "content": (
                    f"Nice to meet you, *{business_name}*! \U0001f91d\n\n"
                    "\U0001f449 *What does your business do?*\n\n"
                    "Just describe it briefly, e.g.:\n"
                    "\u2022 \"I sell clothes and bags\"\n"
                    "\u2022 \"We do catering and event planning\"\n"
                    "\u2022 \"I repair phones and laptops\""
                )}
            ]

        elif step == "ask_business_description":
            business_name = context.get("business_name", "Your Business")
            description = text.strip()

            if len(description) < 3:
                return [
                    {"type": "text", "content": (
                        "Just give me a short description of what "
                        f"*{business_name}* does \u2014 even one sentence is fine!"
                    )}
                ]

            # Infer business type from description
            suggested_type = self._infer_business_type(description)

            # Save context and ask for confirmation
            self.db.save_session(phone_number, STATE_ONBOARDING, {
                "step": "confirm_category",
                "business_name": business_name,
                "description": description,
                "suggested_type": suggested_type
            })

            return [
                {"type": "text", "content": (
                    f"Based on your description, I'd categorize *{business_name}* "
                    f"as: *{suggested_type}*\n\n"
                    "Is that correct?\n\n"
                    "1\ufe0f\u20e3 Yes, that's right\n"
                    "2\ufe0f\u20e3 No \u2014 I buy & sell goods (trading)\n"
                    "3\ufe0f\u20e3 No \u2014 I offer services\n"
                    "4\ufe0f\u20e3 No \u2014 I'm in food & drinks\n"
                    "5\ufe0f\u20e3 None of these \u2014 let me type my own"
                )}
            ]

        elif step == "confirm_category":
            business_name = context.get("business_name", "Your Business")
            description = context.get("description", "")
            suggested_type = context.get("suggested_type", "trading")
            choice = text.strip().lower()

            # Map responses
            if choice in ['1', 'yes', 'yeah', 'correct', 'right', 'yep']:
                business_type = suggested_type
            elif choice in ['2', 'trading', 'buy and sell', 'buy & sell']:
                business_type = "trading"
            elif choice in ['3', 'services', 'service']:
                business_type = "services"
            elif choice in ['4', 'food', 'food & drinks', 'food and drinks']:
                business_type = "food"
            elif choice in ['5', 'none', 'other', 'custom', 'type my own']:
                # Move to custom category step
                self.db.save_session(phone_number, STATE_ONBOARDING, {
                    "step": "custom_category",
                    "business_name": business_name,
                    "description": description
                })
                return [
                    {"type": "text", "content": (
                        "\U0001f4dd No problem! Type your business category.\n\n"
                        "Examples: \"event planning\", \"logistics\", \"agro\", "
                        "\"real estate\", \"fashion\", \"crypto\", \"construction\""
                    )}
                ]
            else:
                # Try to match what they typed as a category directly
                business_type = choice if len(choice) >= 3 else suggested_type

            # Create user and finish onboarding
            return self._complete_onboarding(phone_number, business_name, business_type, description)

        elif step == "custom_category":
            business_name = context.get("business_name", "Your Business")
            description = context.get("description", "")
            custom_type = text.strip().lower()

            if len(custom_type) < 2:
                return [
                    {"type": "text", "content": "Please type a category name (e.g. \"logistics\", \"fashion\", \"construction\"):"}
                ]

            return self._complete_onboarding(phone_number, business_name, custom_type, description)

        else:
            # Unknown step — restart onboarding
            self.db.save_session(phone_number, STATE_ONBOARDING, {"step": "ask_business_name"})
            return [
                {"type": "text", "content": "Let's start over \u2014 *What's your business name?*"}
            ]

    def _complete_onboarding(self, phone_number, business_name, business_type, description=""):
        """Finish onboarding — create user and show welcome"""
        self.db.create_user(
            phone_number,
            business_type=business_type,
            business_name=business_name
        )
        self.db.save_session(phone_number, STATE_IDLE, {})

        return [
            {"type": "text", "content": (
                f"\u2705 All set, *{business_name}*! You're good to go.\n\n"
                f"Category: *{business_type}*\n"
                "This helps me sort your transactions accurately.\n\n"
                "Here's how to use me:\n\n"
                "\U0001f4dd *Record a transaction:*\n"
                "Just type naturally, e.g.:\n"
                "\u2022 \"I buy rice 3 bags 95K\"\n"
                "\u2022 \"Sold goods to Alhaji 350,000\"\n"
                "\u2022 \"Paid Femi 40K salary\"\n\n"
                "\U0001f4ca *See reports:* Type \"report\"\n"
                "\U0001f4cb *See contacts:* Type \"customers\" or \"suppliers\"\n"
                "\U0001f4ce *Export:* Type \"export\"\n"
                "\u2753 *Help:* Type \"help\"\n\n"
                "\U0001f504 *Change category later:* Type \"change category\"\n\n"
                "Try recording your first transaction now! \U0001f447"
            )}
        ]

    def _infer_business_type(self, description):
        """Infer business type from user description"""
        desc = description.lower()

        if any(w in desc for w in ['food', 'cook', 'restaurant', 'catering', 'drink',
                                     'bakery', 'bake', 'snack', 'shawarma', 'grill',
                                     'kitchen', 'eat', 'suya', 'pepper soup']):
            return "food"

        if any(w in desc for w in ['service', 'repair', 'fix', 'barb', 'salon',
                                     'hair', 'nail', 'tailor', 'sew', 'design',
                                     'clean', 'wash', 'laundry', 'teach', 'tutor',
                                     'consult', 'freelance', 'photography', 'event',
                                     'logistics', 'delivery', 'transport', 'tech',
                                     'software', 'web', 'digital', 'print', 'media']):
            return "services"

        if any(w in desc for w in ['sell', 'buy', 'trade', 'shop', 'store',
                                     'goods', 'product', 'wholesale', 'retail',
                                     'import', 'export', 'supply', 'cloth',
                                     'phone', 'provision', 'market', 'bag',
                                     'shoe', 'accessori', 'cosmetic', 'electronic']):
            return "trading"

        return "general"

    def _handle_change_category(self, phone_number, text):
        """Allow existing user to change their business category"""
        user = self.db.get_user(phone_number)
        business_name = user.get('business_name', 'your business') if user else 'your business'
        current_type = user.get('business_type', 'unknown') if user else 'unknown'

        self.db.save_session(phone_number, 'CHANGING_CATEGORY', {"awaiting": "new_category"})

        return [
            {"type": "text", "content": (
                f"*{business_name}* is currently set as: *{current_type}*\n\n"
                "What would you like to change it to?\n\n"
                "1\ufe0f\u20e3 Trading (buy & sell goods)\n"
                "2\ufe0f\u20e3 Services\n"
                "3\ufe0f\u20e3 Food & Drinks\n"
                "4\ufe0f\u20e3 Let me type a custom category"
            )}
        ]

    def _handle_category_change_response(self, phone_number, text):
        """Process the user's category change choice"""
        user = self.db.get_user(phone_number)
        business_name = user.get('business_name', 'your business') if user else 'your business'
        choice = text.strip().lower()

        if choice in ['1', 'trading', 'buy and sell', 'buy & sell']:
            new_type = "trading"
        elif choice in ['2', 'services', 'service']:
            new_type = "services"
        elif choice in ['3', 'food', 'food & drinks', 'food and drinks']:
            new_type = "food"
        elif choice in ['4', 'custom', 'type', 'other']:
            self.db.save_session(phone_number, 'CHANGING_CATEGORY', {"awaiting": "custom_input"})
            return [
                {"type": "text", "content": "Type your new business category (e.g. \"logistics\", \"fashion\", \"construction\"):"}
            ]
        else:
            # Treat whatever they typed as the custom category
            if len(choice) >= 3:
                new_type = choice
            else:
                return [
                    {"type": "text", "content": "Please pick 1-4 or type your custom category:"}
                ]

        # Update user
        self.db.update_user(phone_number, {"business_type": new_type})
        self.db.save_session(phone_number, STATE_IDLE, {})

        return [
            {"type": "text", "content": (
                f"\u2705 Done! *{business_name}* is now categorized as: *{new_type}*\n\n"
                "This will affect how I sort your future transactions."
            )}
        ]

    def _handle_idle(self, phone_number, text):
        """
        User is idle — detect if they're giving a command or recording a transaction.
        """
        text_lower = text.lower().strip()

        # Check for commands
        command = self._detect_command(text_lower)

        if command == 'greeting':
            return self._handle_greeting(phone_number)

        elif command == 'help':
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
        elif command == 'change_category':
            return self._handle_change_category(phone_number, text)

        # No command detected → treat as a transaction
        return self._handle_transaction(phone_number, text)

    def _handle_transaction(self, phone_number, text):
        """Parse a transaction with rich AI extraction and show confirmation"""
        # Parse amount from text
        amount = parse_amount(text)

        if not amount:
            # Couldn't find an amount — ask for it
            self.db.save_session(phone_number, STATE_RECORDING, {"description": text})
            return [{"type": "text", "content": (
                "\U0001f4b0 How much was it? (Just type the amount)\n\n"
                "E.g.: 95000 or 95K or \u20a695,000"
            )}]

        # Get user's business type for tailored parsing
        user = self.db.get_user(phone_number)
        business_type = user.get('business_type', 'trading') if user else 'trading'

        # Rich AI parsing — extracts everything
        result = self.categorizer.parse_transaction(text, phone_number, business_type)

        # Use AI's transaction type if available, fallback to rule-based
        tx_type = result.get('transaction_type') or detect_transaction_type(text)
        vendor = result.get('vendor_or_customer') or extract_vendor_name(text) or ""
        category = result.get('category', 'Uncategorized')
        sub_category = result.get('sub_category', '')
        confidence = result.get('confidence', 0)

        # Use AI's amount if it parsed one and we trust it
        ai_amount = result.get('total_amount')
        if ai_amount and abs(ai_amount - amount) < 100:
            amount = ai_amount

        # Store ALL rich data in pending session
        pending = {
            "amount": amount,
            "type": tx_type,
            "description": text,
            "category": category,
            "sub_category": sub_category,
            "vendor": vendor,
            "confidence": confidence,
            "item_name": result.get('item_name'),
            "brand": result.get('brand'),
            "model": result.get('model'),
            "size": result.get('size'),
            "color": result.get('color'),
            "quantity": result.get('quantity'),
            "unit_cost": result.get('unit_cost'),
            "payment_method": result.get('payment_method'),
            "payment_status": result.get('payment_status'),
            "extra_details": result.get('extra_details', {}),
            "tags": result.get('tags', []),
        }
        self.db.save_session(phone_number, STATE_AWAITING_CONFIRMATION, pending)

        # Build rich confirmation message
        type_emoji = "\U0001f4b0" if tx_type == "income" else "\U0001f4b8"
        cat_emoji = self._get_category_emoji(category)

        response_text = f"\U0001f4dd Got it!\n\n"
        response_text += f"{type_emoji} *\u20a6{amount:,}* ({tx_type.title()})\n"

        # Show item details
        item_name = result.get('item_name')
        brand = result.get('brand')
        model = result.get('model')

        if item_name:
            response_text += f"\U0001f4e6 {item_name}\n"
        if brand or model:
            brand_line = "\U0001f3f7\ufe0f "
            if brand:
                brand_line += brand
            if brand and model:
                brand_line += " | "
            if model:
                brand_line += model
            response_text += brand_line + "\n"

        # Show size/color/quantity
        # Show size/color/quantity
        details = []
        if result.get('size'):
            details.append(f"Size: {result['size']}")
        if result.get('color'):
            details.append(f"Color: {result['color']}")
        if result.get('quantity'):
            details.append(f"Qty: {result['quantity']}")
        if result.get('unit_cost'):
            details.append(f"Unit: \u20a6{int(result['unit_cost']):,}")
        if details:
            response_text += "\U0001f4cb " + " | ".join(details) + "\n"

        # Category
        response_text += f"{cat_emoji} {category}"
        if sub_category:
            response_text += f" \u2192 {sub_category}"
        response_text += "\n"

        # Vendor/Customer
        if vendor:
            response_text += f"\U0001f3ea {vendor}\n"

        # Payment method
        if result.get('payment_method'):
            response_text += f"\U0001f4b3 {result['payment_method'].title()}\n"

        response_text += "\n\u2705 Correct?"

        return [{"type": "buttons", "content": {
            "body": response_text,
            "buttons": [
                {"id": "confirm_yes", "title": "\u2705 Yes"},
                {"id": "confirm_change", "title": "\u270f\ufe0f Change"},
                {"id": "confirm_undo", "title": "\u21a9\ufe0f Cancel"},
            ]
        }}]


    def _handle_confirmation(self, phone_number, text, context):
        """Handle user confirming or rejecting AI suggestion"""
        text_lower = text.lower().strip()

        # Accept
        if text_lower in ['yes', 'y', 'correct', '✅ yes', 'confirm_yes', '1']:
            # Save the transaction with all rich data
            tx = self.db.save_transaction(
                phone_number=phone_number,
                amount=context['amount'],
                tx_type=context['type'],
                description=context['description'],
                category=context['category'],
                sub_category=context.get('sub_category', ''),
                vendor=context.get('vendor', ''),
                confidence=context.get('confidence', 0),
                item_name=context.get('item_name'),
                brand=context.get('brand'),
                model=context.get('model'),
                size=context.get('size'),
                color=context.get('color'),
                quantity=context.get('quantity'),
                unit_cost=context.get('unit_cost'),
                payment_method=context.get('payment_method'),
                payment_status=context.get('payment_status'),
                extra_details=context.get('extra_details'),
                tags=context.get('tags'),
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

    def _handle_greeting(self, phone_number):
        """Handle greetings like Hi, Hello, Hey"""
        user = self.db.get_user(phone_number)
        if user:
            name = user.get('business_name', '').strip()
            greeting = 'Hey ' + name + '! 👋' if name else 'Hey! 👋'
        else:
            greeting = 'Hey there! 👋'

        msg = greeting + chr(10) + chr(10)
        msg += 'What would you like to do?' + chr(10) + chr(10)
        msg += '📝 *Record a transaction* — just type it' + chr(10)
        msg += '📊 *Report* — type "report"' + chr(10)
        msg += '📋 *Contacts* — type "customers" or "suppliers"' + chr(10)
        msg += '📎 *Export* — type "export"' + chr(10)
        msg += '❓ *Help* — type "help"'

        return [{'type': 'text', 'content': msg}]

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
