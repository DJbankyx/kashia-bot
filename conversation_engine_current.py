# src/services/conversation_engine.py
"""Conversation Engine - state machine that manages all chat flows"""

import logging
import re
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

# Product Registry states (full hierarchy)
STATE_REG_PRODUCTS = 'reg_products'
STATE_REG_SUBCATEGORIES = 'reg_subcategories'
STATE_REG_SERIES = 'reg_series'
STATE_REG_ATTRIBUTES = 'reg_attributes'
STATE_REG_ATTR_VALUES = 'reg_attr_values'
STATE_REG_CONVERSIONS = 'reg_conversions'


# ==========================================
# COMMANDS (what the user can type)
# ==========================================

COMMANDS = {
    # Greetings (English + Pidgin + Yoruba + Igbo + Hausa)
    'greeting': [
        'hi', 'hello', 'hey', 'good morning', 'good afternoon', 'good evening',
        'how are you', 'sup', 'whats up', 'how far', 'how you dey', 'wetin dey',
        'howdy', 'whats good', 'hows it going', 'morning', 'evening', 'afternoon',
        'e kaaro', 'e kaasan', 'bawo ni', 'kedu', 'sannu', 'nagode',
        'omo whats up', 'guy whats up', 'bro', 'bros', 'oga', 'boss',
    ],
    # Reports
    'report': ['report', 'summary', 'how much', 'balance', 'overview', 'dashboard'],
    'today': ['today', 'today report', 'todays record', 'what did i do today'],
    'week': ['this week', 'week', 'weekly', 'last 7 days'],
    'month': ['this month', 'month', 'monthly', 'last 30 days'],
    # Help
    'help': ['help', 'menu', 'commands', 'what can you do', 'how does this work', 'guide', 'tutorial'],
    # Export
    'export': ['export', 'excel', 'csv', 'download', 'spreadsheet', 'send me file'],
    'invoice': ['invoice', 'generate invoice', 'create invoice'],
    'receipt': ['receipt', 'generate receipt'],
    'statement': ['statement', 'financial statement', 'account statement'],
    # CRM
    'customers': ['customers', 'customer', 'who buy from me', 'my buyers', 'client', 'clients'],
    'suppliers': ['suppliers', 'supplier', 'who i buy from', 'my vendors', 'vendor', 'vendors'],
    'contacts': ['contacts', 'contact', 'crm', 'people'],
    # Actions
    'undo': ['undo', 'delete last', 'cancel last', 'remove last', 'wrong one', 'mistake'],
    'upgrade': ['upgrade', 'plan', 'pricing', 'subscribe', 'premium', 'pro'],
    'change_category': ['change category', 'update category', 'change business type', 'update business'],
    # Compliments & Gratitude
    'compliment': [
        'well done', 'good job', 'nice one', 'you try', 'thanks', 'thank you',
        'god bless', 'appreciate', 'you are the best', 'great job', 'perfect',
        'brilliant', 'e se', 'dalu', 'na gode', 'bless you', 'i appreciate',
        'you dey try', 'sharp', 'you sharp', 'correct', 'on point',
        'nice work', 'keep it up', 'sweet', 'love it', 'fire',
        'you the best', 'respect', 'big ups', 'kudos',
    ],
    # Sadness / Struggle (but NOT "business is good")
    'sad': [
        'business is slow', 'no sales today', 'things are hard', 'i am broke',
        'no money', 'struggling', 'frustrated', 'stressed', 'tired',
        'business no dey move', 'market no dey', 'wahala', 'problem',
        'i dont know what to do', 'lost', 'confused about money',
        'debt is killing me', 'owing people', 'cant pay',
        'business is bad', 'sales is low', 'no customer', 'dry season',
        'things are tough', 'money is tight', 'broke', 'nothing is working',
        'i give up', 'its not easy', 'life is hard',
    ],
    # Excitement / Positive energy
    'excited': [
        'lets go', 'we made it', 'finally', 'great news', 'yay', 'wonderful',
        'business is good', 'business is booming', 'sales is up', 'we move',
        'god is good', 'thank god', 'e don happen', 'we don blow',
        'money dey come', 'things are looking up', 'good news',
        'big win', 'celebration', 'major sale', 'best day',
        'profit', 'i made profit', 'business is moving',
    ],
    # Pidgin/Informal (non-transactional)
    'pidgin_chat': [
        'ehen', 'shebi', 'na so', 'e don do', 'no wahala', 'no worry',
        'oya now', 'abeg', 'wetin', 'how e dey go', 'which one',
        'you dey mad', 'lol', 'lmao', 'haha', 'hmm', 'ok o', 'alright',
        'i hear you', 'noted', 'roger', 'sure', 'bet',
    ],
    # Product Registry
    'setup_catalog': ['setup catalog', 'setup products', 'register products'],
    'show_catalog': ['my catalog', 'catalog', 'show catalog', 'my products', 'list products'],
    'add_product': ['add product', 'new product'],
    'remove_product': ['remove product', 'delete product'],
    'add_subcategory': ['add subcategory', 'add brand', 'add sub'],
    'add_series': ['add series', 'add model'],
            'remove_subcategory': ['remove subcategory', 'remove brand', 'delete subcategory', 'delete brand'],
            'remove_series': ['remove series', 'remove model', 'delete series', 'delete model'],
            'set_unit': ['set unit', 'change unit', 'primary unit'],
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
        # COMMAND BREAKOUT: escape stuck states with any known command
        if state not in [STATE_NEW_USER, STATE_ONBOARDING, STATE_IDLE, None, '']:
            cmd_check = self._detect_command(text.lower().strip())
            if cmd_check and cmd_check not in ['greeting']:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return self._route_command(phone_number, text, cmd_check)

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
        elif state == STATE_REG_PRODUCTS:
            return self._handle_reg_products(phone_number, text)
        elif state == STATE_REG_SUBCATEGORIES:
            return self._handle_reg_subcategories(phone_number, text)
        elif state == STATE_REG_SERIES:
            return self._handle_reg_series(phone_number, text)
        elif state == STATE_REG_ATTRIBUTES:
            return self._handle_reg_attributes(phone_number, text)
        elif state == STATE_REG_ATTR_VALUES:
            return self._handle_reg_attr_values(phone_number, text)
        elif state == STATE_REG_CONVERSIONS:
            return self._handle_reg_conversions(phone_number, text)




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
        elif command == 'setup_catalog':
            return self._handle_setup_catalog(phone_number, text)
        elif command == 'show_catalog':
            return self._handle_show_catalog(phone_number, text)
        elif command == 'add_product':
            return self._handle_add_product_cmd(phone_number, text)
        elif command == 'remove_product':
            return self._handle_remove_product_cmd(phone_number, text)
        elif command == 'add_subcategory':
            return self._handle_add_subcategory_cmd(phone_number, text)
        elif command == 'add_series':
            return self._handle_add_series_cmd(phone_number, text)
        elif command == 'remove_subcategory':
            return self._handle_remove_subcategory_cmd(phone_number, text)
        elif command == 'remove_series':
            return self._handle_remove_series_cmd(phone_number, text)
        elif command == 'set_unit':
            return self._handle_set_unit_cmd(phone_number, text)

        elif command == 'compliment':
            return self._handle_emotion(phone_number, 'compliment')

        elif command == 'sad':
            return self._handle_emotion(phone_number, 'sad')

        elif command == 'excited':
            return self._handle_emotion(phone_number, 'excited')

        elif command == 'pidgin_chat':
            return self._handle_pidgin(phone_number, text_lower)


        # ---- Fallthrough: attempt transaction parsing ----
        # Check if message has financial signals before sending to AI categorizer
        financial_patterns = [
            r'\d',              # any digit
            r'bought|sold|paid|received|spent|ordered|delivered|shipped',
            r'invoice|credit|debit|transfer|refund',
            r'\u20a6|naira|NGN',
            r'\bk\b',           # "50k"
            r'dozen|carton|pieces|pairs|bags|units',
            r'each|per|total',
        ]
        has_financial_signal = any(re.search(p, text_lower) for p in financial_patterns)

        if not has_financial_signal:
            # No numbers, no transaction verbs — treat as casual chat
            return self._handle_emotion(phone_number, 'compliment')

        # Check if message is an emoji or reaction
        if text_lower.startswith('reaction:') or self._is_emoji(text_lower):
            emoji = text_lower.replace('reaction:', '').strip()
            return self._handle_emoji(phone_number, emoji)

        # Check for category management
        if text_lower.startswith('add category') or text_lower.startswith('new category'):
            cat_name = text.split(':', 1)[-1].strip() if ':' in text else text.split('category', 1)[-1].strip()
            return self._handle_add_category(phone_number, cat_name)

        if text_lower in ['my categories', 'categories', 'list categories', 'show categories']:
            return self._handle_list_categories(phone_number)

        # Clean slang/filler words before processing as transaction
        cleaned_text = self._clean_slang(text)

        # Check for multi-transaction (contains "and" with multiple amounts)
        if self._is_multi_transaction(cleaned_text):
            return self._handle_multi_transaction(phone_number, cleaned_text)

        # No command detected → treat as a transaction
        return self._handle_transaction(phone_number, cleaned_text)

    def _clean_slang(self, text):
        """Remove Nigerian filler words/slang that confuse transaction parsing"""
        # Filler words to strip (at start or scattered)
        fillers = [
            'omo', 'abeg', 'sha', 'ehen', 'shebi', 'o', 'na',
            'like', 'just', 'please', 'pls', 'biko', 'jor',
            'mehn', 'guy', 'bros', 'bro', 'oga', 'boss',
            'so', 'well', 'actually', 'basically',
        ]
        words = text.split()
        # Only strip fillers from the beginning (up to 3 words)
        start_strip = 0
        for i, word in enumerate(words[:3]):
            if word.lower().rstrip('.,!') in fillers:
                start_strip = i + 1
            else:
                break
        cleaned = ' '.join(words[start_strip:]) if start_strip > 0 else text
        return cleaned.strip() if cleaned.strip() else text

    def _is_multi_transaction(self, text):
        """Detect if message contains multiple transactions"""
        text_lower = text.lower()
        import re
        # Count number of amount-like patterns
        amount_patterns = re.findall(r'\d+[kK]|\d{4,}|\d+,\d{3}', text)
        # Check for connecting words/separators
        has_separator = (' and ' in text_lower or '&' in text_lower or
                        'also' in text_lower or
                        text_lower.count(',') >= 2)  # Multiple commas suggest list
        # Multiple amounts + separator = multi-transaction
        if len(amount_patterns) >= 2 and has_separator:
            return True
        # If text has commas creating a list pattern with "each" or "at"
        if text_lower.count(',') >= 1 and len(amount_patterns) >= 2:
            return True
        return False

    def _handle_multi_transaction(self, phone_number, text):
        """Send full message to AI and get array of transactions back"""
        # Get user's business type
        user = self.db.get_user(phone_number)
        business_type = user.get('business_type', 'trading') if user else 'trading'

        # Call AI with the FULL text, ask for array response
        result = self.categorizer.parse_multi_transaction(text, phone_number, business_type)

        if not result or len(result) < 2:
            # AI couldn\'t parse multiple — fall back to single transaction
            return self._handle_transaction(phone_number, text)

        # Build pending list from AI results
        all_pending = []
        for item in result:
            amount = item.get('total_amount')
            if not amount:
                continue
            amount = int(float(amount))
            pending = {
                "amount": amount,
                "type": item.get('transaction_type', 'expense'),
                "description": item.get('description', text),
                "category": item.get('category', 'Uncategorized'),
                "sub_category": item.get('sub_category', ''),
                "vendor": item.get('vendor_or_customer', ''),
                "confidence": item.get('confidence', 0),
                "item_name": item.get('item_name'),
                "brand": item.get('brand'),
                "model": item.get('model'),
                "size": item.get('size'),
                "color": item.get('color'),
                "quantity": item.get('quantity'),
                "unit_cost": int(float(item["unit_cost"])) if item.get("unit_cost") else None,
                "payment_method": item.get('payment_method'),
                "payment_status": item.get('payment_status'),
                "extra_details": item.get('extra_details', {}),
                "tags": item.get('tags', []),
            }
            all_pending.append(pending)

        if len(all_pending) < 2:
            return self._handle_transaction(phone_number, text)

        # Store in session
        self.db.save_session(phone_number, STATE_AWAITING_CONFIRMATION, {
            "multi": True,
            "transactions": all_pending,
            "original_text": text,
        })

        # Build rich summary message
        total = sum(p['amount'] for p in all_pending)
        response_text = f"\U0001f4dd I found *{len(all_pending)} transactions*:\n\n"
        for i, p in enumerate(all_pending, 1):
            type_emoji = "\U0001f4b0" if p['type'] == "income" else "\U0001f4b8"
            cat_emoji = self._get_category_emoji(p['category'])
            item_name = p.get('item_name') or p['description'][:25]

            response_text += f"*{i}.* {type_emoji} *\u20a6{p['amount']:,}* ({p['type'].title()})\n"
            response_text += f"    \U0001f4e6 {item_name}"
            if p.get('brand'):
                response_text += f" | \U0001f3f7\ufe0f {p['brand']}"
            response_text += "\n"
            details = []
            if p.get('size'):
                details.append(f"Size: {p['size']}")
            if p.get('quantity'):
                details.append(f"Qty: {p['quantity']}")
            if p.get('unit_cost'):
                details.append(f"Unit: \u20a6{int(p['unit_cost']):,}")
            if details:
                response_text += f"    \U0001f4cb {' | '.join(details)}\n"
            response_text += f"    {cat_emoji} {p['category']}"
            if p.get('sub_category'):
                response_text += f" \u2192 {p['sub_category']}"
            response_text += "\n"
            if p.get('vendor'):
                response_text += f"    \U0001f3ea {p['vendor']}\n"
            response_text += "\n"

        response_text += f"\U0001f4b0 *Total: \u20a6{total:,}*"
        response_text += "\n\n\u2705 Save all?"

        return [{"type": "buttons", "content": {
            "body": response_text,
            "buttons": [
                {"id": "confirm_yes", "title": "\u2705 Save All"},
                {"id": "confirm_change", "title": "\u270f\ufe0f Edit"},
                {"id": "confirm_undo", "title": "\u21a9\ufe0f Cancel"},
            ]
        }}]

    def _handle_pidgin(self, phone_number, text):
        """Respond to pidgin/informal chat that isn't a transaction"""
        user = self.db.get_user(phone_number)
        name = user.get('business_name', '') if user else ''
        greeting = f"*{name}*" if name else ""

        import random
        responses = {
            'ehen': [
                "Ehen? \U0001f440 Wetin you wan tell me?",
                "I dey listen... wetin dey?",
            ],
            'shebi': [
                "Shebi na so! \U0001f44d You wan record transaction or check something?",
                "Na so o! How I fit help?",
            ],
            'na so': [
                "Na so life be! \U0001f64f Anything I fit help you with?",
                "E be like say you get gist. Record transaction or type *help*?",
            ],
            'e don do': [
                "Alright, e don do! \U0001f91d I dey here if you need me.",
                "OK boss! Call me anytime.",
            ],
            'no wahala': [
                "No wahala at all! \U0001f91d\nAnything else?",
                "We dey together! \U0001f4aa",
            ],
            'abeg': [
                "No wahala, I dey here to help! \U0001f64f\nWetin you need?",
                "Talk to me, wetin I fit do for you?",
            ],
            'oya': [
                "Oya! \U0001f525 Let's go!\nRecord a transaction or type *help* for options.",
                "I'm ready! Wetin we dey do?",
            ],
        }

        # Find matching response
        text_lower = text.lower().strip()
        for key, resp_list in responses.items():
            if key in text_lower:
                return [{"type": "text", "content": random.choice(resp_list)}]

        # Generic pidgin response
        generic = [
            "I hear you! \U0001f44d\nWant to record a transaction or need help with something?",
            "No wahala! \U0001f91d Type *help* to see what I can do.",
            f"{'Oya ' + greeting + ', ' if greeting else ''}wetin we dey do next?",
        ]
        return [{"type": "text", "content": random.choice(generic)}]

    def _handle_add_category(self, phone_number, category_name):
        """Allow user to create a custom category"""
        if not category_name or len(category_name) < 2:
            return [{"type": "text", "content": (
                "\U0001f4c2 To add a category, type:\n"
                "*add category: [name]*\n\n"
                "Example: add category: Vehicle Maintenance"
            )}]

        # Capitalize properly
        category_name = category_name.strip().title()

        # Check if it already exists
        existing = self.db.get_user_categories(phone_number)
        if category_name in existing:
            return [{"type": "text", "content": f"\u2139\ufe0f *{category_name}* already exists in your categories."}]

        # Add it
        self.db.add_custom_category(phone_number, category_name)

        return [{"type": "text", "content": (
            f"\u2705 New category created: *{category_name}*\n\n"
            "I'll use this when categorizing your transactions from now on.\n"
            "Type *my categories* to see all your categories."
        )}]

    def _handle_list_categories(self, phone_number):
        """Show user all their categories (default + custom)"""
        categories = self.db.get_user_categories(phone_number)
        from services.categorizer import CATEGORIES
        default_count = len(CATEGORIES)

        msg = "\U0001f4c2 *Your Categories:*\n\n"
        for i, cat in enumerate(categories, 1):
            if i <= default_count:
                msg += f"{i}. {cat}\n"
            else:
                msg += f"{i}. {cat} \u2728\n"  # Star for custom ones

        msg += "\n\u2728 = custom categories you created\n"
        msg += "\nTo add a new one: *add category: [name]*"

        return [{"type": "text", "content": msg}]

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
        if ai_amount:
            # Trust AI if: amounts are close, OR AI calculated a higher total
            # (e.g. "28K each" x 10 = 280K total)
            if abs(ai_amount - amount) < 100:
                amount = ai_amount
            elif ai_amount > amount and result.get('unit_cost'):
                # AI likely calculated total from unit_cost x quantity
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
        pending = self._enrich_with_unit_conversion(phone_number, pending)
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
            qty_display = f"Qty: {result['quantity']}"
            if pending.get('base_quantity'):
                qty_display += f" (= {pending['base_quantity']} {pending['base_unit']})"
            details.append(qty_display)
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

        # If user sends a command or emotional message, break out of confirmation
        command = self._detect_command(text_lower)
        if command and command not in ['undo']:
            # User wants to do something else — cancel pending and handle normally
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_idle(phone_number, text)

        # Accept
        if text_lower in ['yes', 'y', 'correct', '✅ yes', 'confirm_yes', '1',
                          'na', 'sure', 'roger', 'no wahala', 'ok', 'oya', 'save all',
                          '✅ save all', 'save']:
            # Check if this is a multi-transaction
            if context.get('multi'):
                if not context.get('transactions'):
                    # Broken multi-transaction session — reset
                    self.db.save_session(phone_number, STATE_IDLE, {})
                    return [{"type": "text", "content": "Something went wrong with that batch. Let\'s try again — type your transaction."}]
                transactions = context.get('transactions', [])
                saved_count = 0
                for tx_data in transactions:
                    self.db.save_transaction(
                        phone_number=phone_number,
                        amount=tx_data['amount'],
                        tx_type=tx_data['type'],
                        description=tx_data['description'],
                        category=tx_data['category'],
                        sub_category=tx_data.get('sub_category', ''),
                        vendor=tx_data.get('vendor', ''),
                        confidence=tx_data.get('confidence', 0),
                        item_name=tx_data.get('item_name'),
                        brand=tx_data.get('brand'),
                        model=tx_data.get('model'),
                        size=tx_data.get('size'),
                        color=tx_data.get('color'),
                        quantity=tx_data.get('quantity'),
                        unit_cost=tx_data.get('unit_cost'),
                        payment_method=tx_data.get('payment_method'),
                        payment_status=tx_data.get('payment_status'),
                        extra_details=tx_data.get('extra_details'),
                        tags=tx_data.get('tags'),
                    )
                    # Update contact totals
                    vendor = tx_data.get('vendor', '')
                    if vendor:
                        self.db.update_contact_totals(phone_number, vendor, tx_data['amount'], tx_data['type'])
                    saved_count += 1

                self.db.save_session(phone_number, STATE_IDLE, {})
                total = sum(t['amount'] for t in transactions)
                return [{"type": "text", "content": (
                    f"\u2705 Saved *{saved_count} transactions* (\u20a6{total:,} total)!\n\n"
                    f"Record more or type *help* for options."
                )}]

            # Save the transaction with all rich data
            if 'amount' not in context:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "Something went wrong. Let\'s try again \u2014 type your transaction."}]
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
        # Change — show correction menu
        elif text_lower in ['change', 'no', 'n', 'wrong', '\u270f\ufe0f change', 'confirm_change', '2']:
            context['correction_step'] = 'choose_field'
            self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)

            return [{"type": "text", "content": (
                "\u270f\ufe0f What would you like to change?\n\n"
                "1. Category\n"
                "2. Amount\n"
                "3. Type (Income/Expense)\n"
                "4. Item/Brand details\n"
                "5. Vendor/Customer name\n"
                "6. Tell me what's wrong (free text)"
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
        """Handle multi-field corrections"""
        text_lower = text.lower().strip()
        correction_step = context.get('correction_step', 'choose_field')

        # Step 1: User is choosing what field to change
        if correction_step == 'choose_field':
            if text_lower in ['1', 'category']:
                context['correction_step'] = 'fix_category'
                self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)

                categories_text = "\n".join([
                    f"{i+1}. {cat}" for i, cat in enumerate(
                        ["Goods & Stock", "Sales & Income", "Rent & Space",
                         "Utilities & Services", "Transport & Logistics",
                         "People & Labour", "Equipment & Tools", "Money Matters",
                         "Marketing & Customers", "Government & Compliance", "Personal"]
                    )
                ])
                return [{"type": "text", "content": (
                    f"\U0001f4c2 What's the correct category?\n\n"
                    f"{categories_text}\n\n"
                    f"Reply with the *number* or *name*."
                )}]

            elif text_lower in ['2', 'amount']:
                context['correction_step'] = 'fix_amount'
                self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)
                return [{"type": "text", "content": (
                    "\U0001f4b0 What's the correct amount?\n\n"
                    "E.g.: 95000 or 95K or \u20a695,000"
                )}]

            elif text_lower in ['3', 'type', 'income', 'expense']:
                context['correction_step'] = 'fix_type'
                self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)
                return [{"type": "text", "content": (
                    "\U0001f504 Is this transaction:\n\n"
                    "1. Income (money coming IN)\n"
                    "2. Expense (money going OUT)"
                )}]

            elif text_lower in ['4', 'item', 'brand', 'details']:
                context['correction_step'] = 'fix_item'
                self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)
                return [{"type": "text", "content": (
                    "\U0001f4e6 Tell me the correct item/brand details.\n\n"
                    "E.g.: \"Nike Air Max size 42 black\"\n"
                    "or just the part that's wrong."
                )}]

            elif text_lower in ['5', 'vendor', 'customer', 'name']:
                context['correction_step'] = 'fix_vendor'
                self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)
                return [{"type": "text", "content": (
                    "\U0001f3ea What's the correct vendor/customer name?"
                )}]

            elif text_lower in ['6', 'free', 'text', 'other']:
                context['correction_step'] = 'fix_freetext'
                self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)
                return [{"type": "text", "content": (
                    "\U0001f4ac Tell me what's wrong and I'll fix it.\n\n"
                    "E.g.: \"The amount should be 280K not 28K\" or "
                    "\"It's expense not income\""
                )}]

            else:
                return [{"type": "text", "content": (
                    "Please pick a number (1-6):\n\n"
                    "1. Category\n2. Amount\n3. Type\n"
                    "4. Item/Brand\n5. Vendor\n6. Free text"
                )}]

        # Step 2: Handle specific field corrections
        elif correction_step == 'fix_category':
            return self._apply_category_correction(phone_number, text, context)

        elif correction_step == 'fix_amount':
            new_amount = parse_amount(text)
            if not new_amount:
                return [{"type": "text", "content": "I couldn't read that amount. Try again (e.g. 95K or 95000):"}]
            context['amount'] = new_amount
            return self._save_corrected_transaction(phone_number, context, f"Amount updated to \u20a6{new_amount:,}")

        elif correction_step == 'fix_type':
            if text_lower in ['1', 'income', 'in']:
                context['type'] = 'income'
            elif text_lower in ['2', 'expense', 'out']:
                context['type'] = 'expense'
            else:
                return [{"type": "text", "content": "Please reply:\n1. Income\n2. Expense"}]
            return self._save_corrected_transaction(phone_number, context, f"Type changed to {context['type'].title()}")

        elif correction_step == 'fix_vendor':
            context['vendor'] = text.strip()
            return self._save_corrected_transaction(phone_number, context, f"Vendor updated to {text.strip()}")

        elif correction_step == 'fix_item':
            # Store the correction as description update
            context['item_name'] = text.strip()
            return self._save_corrected_transaction(phone_number, context, f"Item details updated")

        elif correction_step == 'fix_freetext':
            # Parse the free text correction intelligently
            if 'amount' in text_lower or 'k' in text_lower or '\u20a6' in text_lower:
                new_amount = parse_amount(text)
                if new_amount:
                    context['amount'] = new_amount
            if 'income' in text_lower:
                context['type'] = 'income'
            elif 'expense' in text_lower:
                context['type'] = 'expense'
            if 'not' in text_lower and any(word in text_lower for word in ['income', 'expense']):
                # Flip the type
                context['type'] = 'expense' if context.get('type') == 'income' else 'income'
            return self._save_corrected_transaction(phone_number, context, "Updated based on your correction")

        else:
            context['correction_step'] = 'choose_field'
            self.db.save_session(phone_number, STATE_AWAITING_CORRECTION, context)
            return [{"type": "text", "content": (
                "What would you like to change?\n\n"
                "1. Category\n2. Amount\n3. Type\n"
                "4. Item/Brand\n5. Vendor\n6. Free text"
            )}]

    def _apply_category_correction(self, phone_number, text, context):
        """Handle category correction specifically (AI learns from this)"""
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
            for key, cat in category_map.items():
                if key in text_lower:
                    correct_category = cat
                    break

        if not correct_category:
            return [{"type": "text", "content": (
                "\u2753 I didn't recognize that category.\n"
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

        context['category'] = correct_category
        return self._save_corrected_transaction(phone_number, context, f"Category changed to *{correct_category}*")

    def _save_corrected_transaction(self, phone_number, context, change_message):
        """Save the corrected transaction and confirm"""
        self.db.save_transaction(
            phone_number=phone_number,
            amount=context['amount'],
            tx_type=context['type'],
            description=context['description'],
            category=context['category'],
            sub_category=context.get('sub_category', ''),
            vendor=context.get('vendor', ''),
            confidence=100,
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

        # Update contact totals
        vendor = context.get('vendor', '')
        if vendor:
            self.db.update_contact_totals(phone_number, vendor, context['amount'], context['type'])

        self.db.save_session(phone_number, STATE_IDLE, {})

        return [{"type": "text", "content": (
            f"\u2705 {change_message}\n"
            f"Transaction saved! \U0001f9e0 I'll remember this.\n\n"
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
                # Exact match always wins
                if text_lower == keyword:
                    return command
                # For multi-word keywords, startswith is fine
                if ' ' in keyword:
                    if text_lower.startswith(keyword):
                        return command
                else:
                    # Single-word keyword: only match if message is short
                    # "help" matches "help me" but NOT "help me analyze this transaction"
                    if text_lower.startswith(keyword + ' '):
                        remaining_words = text_lower[len(keyword):].strip().split()
                        if len(remaining_words) <= 2:
                            return command
        return None

    def _is_emoji(self, text):
        """Check if text is primarily emoji"""
        import re
        # Remove whitespace and check if remaining chars are emoji
        cleaned = text.strip()
        if not cleaned:
            return False
        # Common emoji unicode ranges
        emoji_pattern = re.compile(
            "[\U0001f600-\U0001f64f"  # emoticons
            "\U0001f300-\U0001f5ff"  # symbols & pictographs
            "\U0001f680-\U0001f6ff"  # transport & map
            "\U0001f1e0-\U0001f1ff"  # flags
            "\U00002702-\U000027b0"  # dingbats
            "\U000024c2-\U0001f251"  # enclosed chars
            "\U0001f900-\U0001f9ff"  # supplemental symbols
            "\U0001fa00-\U0001fa6f"  # chess symbols
            "\U0001fa70-\U0001faff"  # symbols extended
            "\U00002600-\U000026ff"  # misc symbols
            "]+", re.UNICODE
        )
        # If the entire text (stripped) matches emoji pattern, it's an emoji message
        return bool(emoji_pattern.fullmatch(cleaned))

    def _handle_emoji(self, phone_number, emoji):
        """Respond to emoji messages and reactions"""
        user = self.db.get_user(phone_number)
        name = user.get('business_name', '') if user else ''

        # Map emojis to responses
        positive = ['\U0001f44d', '\u2705', '\U0001f44c', '\U0001f4af', '\U0001f389', '\U0001f60a']
        love = ['\u2764\ufe0f', '\u2764', '\U0001f495', '\U0001f60d', '\U0001f618']
        sad = ['\U0001f622', '\U0001f62d', '\U0001f614', '\U0001f625', '\U0001f61e']
        angry = ['\U0001f621', '\U0001f624', '\U0001f620']
        fire = ['\U0001f525']
        laugh = ['\U0001f602', '\U0001f923', '\U0001f604', '\U0001f606']
        pray = ['\U0001f64f', '\U0001f932']
        money = ['\U0001f4b0', '\U0001f4b5', '\U0001f4b8', '\U0001f911']
        thumbs_down = ['\U0001f44e']

        emoji_clean = emoji.strip()

        if any(e in emoji_clean for e in positive):
            msg = "Glad everything's good! \U0001f4aa\n\nReady when you need me."
        elif any(e in emoji_clean for e in love):
            greeting = f"*{name}*" if name else "you"
            msg = f"Love working with {greeting} too! \U0001f60a\n\nLet's keep the business growing!"
        elif any(e in emoji_clean for e in sad):
            msg = (
                "I see you're going through it. \U0001f64f\n\n"
                "Remember \u2014 every successful business has tough days. "
                "You're doing the right thing by tracking your money.\n\n"
                "Want to check your report? Type *report* to see how things look."
            )
        elif any(e in emoji_clean for e in angry):
            msg = (
                "I hear you! \U0001f64f Let me know what went wrong.\n\n"
                "If I made an error, type *undo* to fix the last transaction "
                "or tell me what to change."
            )
        elif any(e in emoji_clean for e in fire):
            msg = "Business is on FIRE! \U0001f525\U0001f525\U0001f525\n\nKeep going!"
        elif any(e in emoji_clean for e in laugh):
            msg = "\U0001f604 Glad I could make you smile!\n\nAnything else I can help with?"
        elif any(e in emoji_clean for e in pray):
            greeting = f"*{name}*" if name else ""
            msg = f"You're welcome{', ' + greeting if greeting else ''}! \U0001f91d\n\nAlways here to help."
        elif any(e in emoji_clean for e in money):
            msg = "\U0001f4b0 Money on your mind?\n\nRecord a transaction or type *report* to see your numbers."
        elif any(e in emoji_clean for e in thumbs_down):
            msg = "Something's not right? Tell me what happened and I'll fix it. \U0001f6e0\ufe0f"
        else:
            msg = "\U0001f44d Got it!\n\nNeed anything? Just type *help* for options."

        return [{"type": "text", "content": msg}]

    def _handle_emotion(self, phone_number, emotion_type):
        """Respond to emotional messages with varied, rich responses"""
        import random
        user = self.db.get_user(phone_number)
        name = user.get('business_name', '') if user else ''

        if emotion_type == 'compliment':
            responses = [
                "Thank you" + (", *" + name + "*" if name else "") + "! \U0001f60a\nI'm here to make your business life easier. Keep recording those transactions \u2014 your future self will thank you! \U0001f4aa",
                "You're too kind! \U0001f64f\n" + ("*" + name + "* appreciation received! " if name else "") + "My job is to make your money make sense. Let's keep going!",
                "E se! \U0001f60a Na you be the real boss! I'm always here to help. What's next?",
                "Big ups" + (" *" + name + "*" if name else "") + "! \U0001f91d\nWe're building something great together. Your records are looking sharp!",
                "Na you be the real MVP! \U0001f3c6\nKeeping records like a pro. What else can I help with?",
                "God bless you too! \U0001f64f\nI dey for " + ("*" + name + "*" if name else "you") + " always!",
                "Respect! \U0001f91d You dey try, and I go always dey here to support. Keep winning!",
            ]
        elif emotion_type == 'sad':
            responses = [
                ("*" + name + "*, " if name else "") + "I understand. Business can be tough sometimes. \U0001f64f\n\nHere are a few things that might help:\n\n\U0001f4ca Type *report* to see your full picture\n\U0001f4a1 Sometimes the numbers tell a story you can't see day-to-day\n\U0001f4aa Every naira you track brings you closer to understanding your business\n\nKeep pushing!",
                "Tough times don't last, but tough business owners do. \U0001f4aa\n\nSome tips:\n\u2022 Check which products bring the most profit\n\u2022 Look for patterns in your slow days\n\u2022 Small wins count \u2014 every sale matters\n\nType *report* to see your numbers.",
                "E go better! \U0001f64f Every successful business has had dry seasons.\n\nWhat separates winners:\n1. They track everything (you're doing this! \u2705)\n2. They adjust when things are slow\n3. They don't give up\n\nYou're ahead of 90% of businesses. Chin up! \U0001f4aa",
                "Na so e dey be sometimes. \U0001f91d But rain no dey fall forever.\n\nI suggest:\n\U0001f4ca Type *report* \u2014 let's see where your money goes\n\U0001f4a1 Cut small expenses that add up\n\U0001f4b0 Focus on your best-selling items\n\nI believe in " + ("*" + name + "*" if name else "you") + "! \U0001f525",
                "I hear you. Business wahala can be real. \U0001f622\n\nBut " + ("*" + name + "*" if name else "you") + " has survived tough times before.\n\nWant me to pull up your *report*? Sometimes seeing the data helps you spot opportunities.",
                "Omo, I feel you. \U0001f64f The fact that you're tracking your money means you're already smarter than most.\n\nType *report* to see what's working. You got this! \U0001f4aa",
                "Don't let today discourage you. \U0001f91d Every billionaire had broke days. The difference? They kept showing up.\n\n" + ("*" + name + "* is" if name else "You are") + " still in the game. That's what matters. \U0001f525",
            ]
        elif emotion_type == 'excited':
            responses = [
                "Let's gooo" + (", *" + name + "*" if name else "") + "! \U0001f525\U0001f525\U0001f525\nLove the energy! Keep that momentum going.\nReady to record more wins? Just tell me! \U0001f4b0",
                "Ayeee! \U0001f389\U0001f389\U0001f389 " + ("*" + name + "* is" if name else "Business is") + " MOVING!\nKeep recording everything \u2014 success leaves receipts! \U0001f4b0",
                "\U0001f525\U0001f525\U0001f525 We move" + (", *" + name + "*" if name else "") + "!\nNothing can stop you. Record that win! \U0001f4aa",
                "God is faithful! \U0001f64f " + ("*" + name + "* is winning!" if name else "You're winning!") + "\nKeep pushing, keep recording, keep growing! \U0001f680",
                "The vibes are immaculate! \U0001f4af Success loading... \U0001f4c8\n\nWhat's the good news? Record it! \U0001f4b0",
                "Omo! " + ("*" + name + "*" if name else "You") + " dey do well o! \U0001f525\nThis is what happens when you stay consistent. Keep growing! \U0001f4aa",
                "E DON HAPPEN! \U0001f389\U0001f525 I'm happy for " + ("*" + name + "*" if name else "you") + "!\nLet's capture this win \u2014 record the transaction! \U0001f4b0",
            ]
        else:
            responses = [
                "\U0001f44d Anything I can help with? Type *help* for options.",
                "I'm here! What can I do for you? \U0001f91d",
                "No wahala! Ready when you are. \U0001f4aa",
            ]

        return [{"type": "text", "content": random.choice(responses)}]

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
        msg += '\U0001f4e6 *Catalog* \u2014 type "setup catalog"' + chr(10)
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
            "📦 *Product Catalog:*\n"
            "   • \"setup catalog\" — set up your products\n"
            "   • \"my catalog\" — view your catalog\n"
            "   • \"add product\" — add a product\n"
            "   • \"add subcategory X under Y\" — add brand\n"
            "   • \"add series X under Y\" — add model\n\n"
            "↩️ *Other:*\n"
            "   • \"undo\" — delete last transaction\n"
            "   • \"change category\" — change business type\n"
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

    # ============================================================
    # PRODUCT REGISTRY — FULL HIERARCHY HANDLERS
    # ============================================================

    def _route_command(self, phone_number, text, command):
        """Route a detected command to its handler"""
        if command == 'setup_catalog':
            return self._handle_setup_catalog(phone_number, text)
        elif command == 'show_catalog':
            return self._handle_show_catalog(phone_number, text)
        elif command == 'add_product':
            return self._handle_add_product_cmd(phone_number, text)
        elif command == 'remove_product':
            return self._handle_remove_product_cmd(phone_number, text)
        elif command == 'add_subcategory':
            return self._handle_add_subcategory_cmd(phone_number, text)
        elif command == 'add_series':
            return self._handle_add_series_cmd(phone_number, text)
        elif command == 'help':
            return self._handle_help(phone_number)
        elif command == 'report':
            return self._handle_report(phone_number, 'report')
        else:
            return self._handle_idle(phone_number, text)

    def _handle_setup_catalog(self, phone_number, text):
        """Start catalog setup — Step 1: What products do you sell?"""
        user = self.db.get_user(phone_number)
        business_name = user.get('business_name', 'your business')
        business_type = user.get('business_type', 'general')

        examples = {
            'trading': 'Sneakers, T-Shirts, Bags, Watches, Phones',
            'fashion': 'Shoes, Clothes, Bags, Socks, Accessories',
            'food': 'Rice, Palm Oil, Drinks, Snacks, Flour',
            'services': 'Haircut, Braiding, Manicure, Laundry',
            'general': 'List what you sell or offer',
        }
        example = examples.get(business_type, examples['general'])

        self.db.save_session(phone_number, STATE_REG_PRODUCTS, {})
        return [{"type": "text", "content": f"\ud83d\ude80 *Product Catalog Setup for {business_name}*\n\n*What products/items do you sell?*\n\nList them separated by commas.\n\n_Example: {example}_\n\n_Type \'cancel\' to exit setup._"}]

    def _handle_reg_products(self, phone_number, text):
        """Process product list, ask for subcategories of first product"""
        if text.lower().strip() in ['cancel', 'quit', 'exit']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Setup cancelled. Type *setup catalog* anytime to restart."}]

        products = [p.strip().title() for p in text.split(',') if p.strip()]
        if not products:
            return [{"type": "text", "content": "Please list at least one product, separated by commas."}]

        for product in products:
            self.db.add_product(phone_number, product)

        # Start with first product — ask for subcategories/brands
        context = {
            'products': products,
            'p_idx': 0,
            'phase': 'subcategories',
        }
        current = products[0]
        self.db.save_session(phone_number, STATE_REG_SUBCATEGORIES, context)
        return [{"type": "text", "content": f"\u2705 Products saved: {', '.join(products)}\n\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n*Setting up: {current}*\n\nDo you have different brands/types of *{current}*?\n\n_List them separated by commas._\n_Type \'skip\' if you just sell {current} without specific brands._\n\n_Example: Nike, Adidas, Puma_"}]

    def _handle_reg_subcategories(self, phone_number, text):
        """Process subcategories for current product"""

        # Cancel check
        if text.lower().strip() in ['cancel', 'exit', 'quit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Catalog setup cancelled.\n\nType *setup catalog* to start again, or *my catalog* to see what\'s saved."}]
        session = self.db.get_session(phone_number)
        context = session.get('context', {})
        products = context.get('products', [])
        p_idx = int(context.get('p_idx', 0))
        current = products[p_idx] if p_idx < len(products) else ''

        if text.lower().strip() in ['skip', '-', 'none', 'no']:
            # No subcategories — ask for attributes at product level
            context['subcategories'] = []
            context['sub_idx'] = 0
            context['phase'] = 'product_attributes'
            self.db.save_session(phone_number, STATE_REG_ATTRIBUTES, context)
            return [{"type": "text", "content": f"\u2705 No brands/types for {current}.\n\nWhat details do you want to track for *{current}*?\n\n_Examples: size, color, material, weight, condition_\n_Separate with commas. Type \'skip\' for none._"}]

        subcategories = [s.strip().title() for s in text.split(',') if s.strip()]
        # Filter out names matching the product itself
        subcategories = [s for s in subcategories if s.lower() != current.lower()]
        if not subcategories:
            context['phase'] = 'product_attributes'
            self.db.save_session(phone_number, STATE_REG_CONVERSIONS, context)
            return [{"type": "text", "content": f"\ud83d\udd04 Any unit conversions for *{current_product}*?\n\n_Example: 1 carton = 10 pairs, 1 dozen = 12 pieces_\n_Type 'skip' if none._"}]
        # Filter out names matching the product itself
        subcategories = [s for s in subcategories if s.lower() != current.lower()]
        if not subcategories:
            context['phase'] = 'product_attributes'
            self.db.save_session(phone_number, STATE_REG_CONVERSIONS, context)
            return [{"type": "text", "content": f"\ud83d\udd04 Any unit conversions for *{current_product}*?\n\n_Example: 1 carton = 10 pairs, 1 dozen = 12 pieces_\n_Type 'skip' if none._"}]
        for sub in subcategories:
            self.db.add_subcategory(phone_number, current, sub)

        # Ask if first subcategory has series/models
        context['subcategories'] = subcategories
        context['sub_idx'] = 0
        first_sub = subcategories[0]
        self.db.save_session(phone_number, STATE_REG_SERIES, context)
        return [{"type": "text", "content": f"\u2705 {current} brands: {', '.join(subcategories)}\n\nDoes *{first_sub}* have different models/series?\n\n_Example for Nike: Air Force 1, Jordan 4, Air Max, Dunk_\n_Example for Birkin: Birkin 25, Birkin 30, Birkin 35_\n\n_Type \'skip\' if no specific models._"}]

    def _handle_reg_series(self, phone_number, text):
        """Process series for current subcategory"""

        # Cancel check
        if text.lower().strip() in ['cancel', 'exit', 'quit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Catalog setup cancelled.\n\nType *setup catalog* to start again, or *my catalog* to see what\'s saved."}]
        session = self.db.get_session(phone_number)
        context = session.get('context', {})
        products = context.get('products', [])
        p_idx = int(context.get('p_idx', 0))
        current_product = products[p_idx] if p_idx < len(products) else ''
        subcategories = context.get('subcategories', [])
        sub_idx = int(context.get('sub_idx', 0))
        current_sub = subcategories[sub_idx] if sub_idx < len(subcategories) else ''

        if text.lower().strip() not in ['skip', '-', 'none', 'no']:
            series_list = [s.strip().title() for s in text.split(',') if s.strip()]
            for series in series_list:
                self.db.add_series(phone_number, current_product, current_sub, series)
            context['series_list'] = series_list
            context['series_idx'] = 0
        else:
            context['series_list'] = []
            context['series_idx'] = 0

        series_list = context.get('series_list', [])

        if series_list:
            # Ask attributes for FIRST series
            first_series = series_list[0]
            context['phase'] = 'series_attributes'
            context['current_target'] = first_series
            self.db.save_session(phone_number, STATE_REG_ATTRIBUTES, context)
            return [{"type": "text", "content": f"What attributes matter for *{first_series}* ({current_sub})?\n\n_Examples: size, color, material, condition_\n_Separate with commas. Type \'skip\' to move on._"}]
        else:
            # No series — ask attributes for the subcategory itself
            context['phase'] = 'sub_attributes'
            context['current_target'] = current_sub
            self.db.save_session(phone_number, STATE_REG_ATTRIBUTES, context)
            return [{"type": "text", "content": f"What attributes matter for *{current_sub}* ({current_product})?\n\n_Examples: size, color, material, condition_\n_Separate with commas. Type \'skip\' to move on._"}]

    def _handle_reg_attributes(self, phone_number, text):
        """Process attributes, then ask for values"""

        # Cancel check
        if text.lower().strip() in ['cancel', 'exit', 'quit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Catalog setup cancelled.\n\nType *setup catalog* to start again, or *my catalog* to see what\'s saved."}]
        session = self.db.get_session(phone_number)
        context = session.get('context', {})
        products = context.get('products', [])
        p_idx = int(context.get('p_idx', 0))
        current_product = products[p_idx] if p_idx < len(products) else ''
        subcategories = context.get('subcategories', [])
        sub_idx = int(context.get('sub_idx', 0))
        phase = context.get('phase', 'product_attributes')

        if text.lower().strip() in ['skip', '-', 'none', 'no']:
            # Skip attributes — check if more series first
            series_list = context.get('series_list', [])
            series_idx = int(context.get('series_idx', 0))
            subcategories = context.get('subcategories', [])
            sub_idx = int(context.get('sub_idx', 0))

            if phase == 'series_attributes' and series_list:
                next_series_idx = series_idx + 1
                if next_series_idx < len(series_list):
                    next_series = series_list[next_series_idx]
                    context['series_idx'] = next_series_idx
                    context['current_target'] = next_series
                    context['phase'] = 'series_attributes'
                    self.db.save_session(phone_number, STATE_REG_ATTRIBUTES, context)
                    current_sub = subcategories[sub_idx] if sub_idx < len(subcategories) else ''
                    return [{"type": "text", "content": f"What attributes for *{next_series}* ({current_sub})?\n\n_Examples: size, color, material_\n_Type \'skip\' to move on._"}]

            # No more series — go to conversions
            target = current_product
            if (phase == 'sub_attributes' or phase == 'series_attributes') and sub_idx < len(subcategories):
                target = subcategories[sub_idx]
            self.db.save_session(phone_number, STATE_REG_CONVERSIONS, context)
            return [{"type": "text", "content": f"\ud83d\udd04 Any unit conversions for *{target}*?\n\n_Example: 1 carton = 10 pairs, 1 dozen = 12 pieces_\n_Type \'skip\' if none._"}]

        # Validate input — detect if user sent a transaction instead of attributes
        transaction_signals = ['bought', 'sold', 'for ', 'from ', r'\d{4,}']
        looks_like_transaction = any(
            re.search(signal, text.lower()) for signal in transaction_signals
        ) and len(text) > 40

        if looks_like_transaction:
            # User likely sent a transaction, not attributes — break out
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_message_internal(phone_number, text)

        attributes = [a.strip().lower() for a in text.split(',') if a.strip()]

        # Save empty attributes (we'll fill values next)
        subcategories = context.get('subcategories', [])
        sub_idx = int(context.get('sub_idx', 0))
        current_sub = subcategories[sub_idx] if sub_idx < len(subcategories) else ''
        current_target = context.get('current_target', '')
        if phase == 'series_attributes' and current_sub:
            # Save at series level
            for attr in attributes:
                self.db.set_attributes(phone_number, current_product, attr, [], subcategory=current_sub, series=current_target)
        elif phase == 'sub_attributes' and current_sub:
            for attr in attributes:
                self.db.set_attributes(phone_number, current_product, attr, [], subcategory=current_sub)
        else:
            for attr in attributes:
                self.db.set_attributes(phone_number, current_product, attr, [])

        # Ask for values of first attribute
        context['attrs_to_fill'] = attributes
        context['attr_fill_idx'] = 0
        first_attr = attributes[0]
        # Use current_target for display (series name or subcategory name)
        current_target = context.get('current_target', '')
        if current_target:
            display_target = current_target
        elif current_sub:
            display_target = current_sub
        else:
            display_target = current_product
        self.db.save_session(phone_number, STATE_REG_ATTR_VALUES, context)
        return [{"type": "text", "content": f"\ud83d\udcdd What *{first_attr}* values are available for *{display_target}*?\n\n_List them separated by commas._\n_Example for size: 38, 39, 40, 41, 42, 43, 44, 45_\n_Example for color: Black, White, Red, Blue_\n\n_Type \'skip\' to leave open (any value accepted)._"}]

    def _handle_reg_attr_values(self, phone_number, text):
        """Process attribute values, then ask for next attribute or move on"""

        # Cancel check
        if text.lower().strip() in ['cancel', 'exit', 'quit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Catalog setup cancelled.\n\nType *setup catalog* to start again, or *my catalog* to see what\'s saved."}]
        session = self.db.get_session(phone_number)
        context = session.get('context', {})
        products = context.get('products', [])
        p_idx = int(context.get('p_idx', 0))
        current_product = products[p_idx] if p_idx < len(products) else ''
        subcategories = context.get('subcategories', [])
        sub_idx = int(context.get('sub_idx', 0))
        phase = context.get('phase', 'product_attributes')
        attrs_to_fill = context.get('attrs_to_fill', [])
        attr_fill_idx = int(context.get('attr_fill_idx', 0))

        current_attr = attrs_to_fill[attr_fill_idx] if attr_fill_idx < len(attrs_to_fill) else ''

        # Save values
        if text.lower().strip() not in ['skip', '-', 'none']:
            values = [v.strip() for v in text.split(',') if v.strip()]
            values = [v.strip() for v in text.split(',') if v.strip()]
            # Expand range patterns (e.g., "38-50" -> ["38","39",...,"50"])
            expanded = []
            for v in values:
                range_match = re.match(r'^(\d+)\s*-\s*(\d+)$', v)
                if range_match:
                    start, end = int(range_match.group(1)), int(range_match.group(2))
                    if end > start and (end - start) <= 50:
                        expanded.extend([str(i) for i in range(start, end + 1)])
                    else:
                        expanded.append(v)
                else:
                    expanded.append(v)
            values = expanded
            current_target = context.get('current_target', '')
            if current_target:
                target = current_target
            elif phase == 'sub_attributes' and sub_idx < len(subcategories):
                target = subcategories[sub_idx]
            else:
                target = current_product
            self.db.set_attributes(phone_number, current_product, current_attr, values, subcategory=current_sub if phase in ('sub_attributes', 'series_attributes') else None)
            next_fill_idx = attr_fill_idx + 1
            context['attr_fill_idx'] = next_fill_idx
            if next_fill_idx < len(attrs_to_fill):
                next_attr = attrs_to_fill[next_fill_idx]
                self.db.save_session(phone_number, STATE_REG_ATTR_VALUES, context)
                return [{"type": "text", "content": f"\ud83d\udcdd What *{next_attr}* values for *{target}*?\n\n_List separated by commas, or \'skip\'._"}]

        # All attributes filled — check if more series to configure
        series_list = context.get('series_list', [])
        series_idx = int(context.get('series_idx', 0))

        if phase == 'series_attributes' and series_list:
            next_series_idx = series_idx + 1
            if next_series_idx < len(series_list):
                # Move to next series
                next_series = series_list[next_series_idx]
                context['series_idx'] = next_series_idx
                context['current_target'] = next_series
                context['phase'] = 'series_attributes'
                context['attrs_to_fill'] = []
                context['attr_fill_idx'] = 0
                self.db.save_session(phone_number, STATE_REG_ATTRIBUTES, context)
                current_sub = subcategories[sub_idx] if sub_idx < len(subcategories) else ''
                return [{"type": "text", "content": f"\u2705 Done with {series_list[series_idx]}!\n\nWhat attributes for *{next_series}* ({current_sub})?\n\n_Examples: size, color, material_\n_Type \'skip\' to move on._"}]

        # No more series — ask for conversions at subcategory level
        current_sub = subcategories[sub_idx] if sub_idx < len(subcategories) else ''
        target = current_sub if current_sub else current_product
        self.db.save_session(phone_number, STATE_REG_CONVERSIONS, context)
        return [{"type": "text", "content": f"\ud83d\udd04 Any unit conversions for *{target}*?\n\n_Example: 1 carton = 10 pairs, 1 dozen = 12 pieces_\n_Type \'skip\' if none._"}]

    def _handle_reg_conversions(self, phone_number, text):
        """Process conversions, then move to next subcategory or next product"""

        # Cancel check
        if text.lower().strip() in ['cancel', 'exit', 'quit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Catalog setup cancelled.\n\nType *setup catalog* to start again, or *my catalog* to see what\'s saved."}]
        session = self.db.get_session(phone_number)
        context = session.get('context', {})
        products = context.get('products', [])
        p_idx = int(context.get('p_idx', 0))
        current_product = products[p_idx] if p_idx < len(products) else ''
        subcategories = context.get('subcategories', [])
        sub_idx = int(context.get('sub_idx', 0))
        phase = context.get('phase', 'product_attributes')

        # Save conversions
        if text.lower().strip() not in ['skip', '-', 'none', 'no']:
            conversions = {}
            parts = text.split(',')
            for part in parts:
                if '=' in part:
                    left, right = part.split('=', 1)
                    conversions[left.strip()] = right.strip()
            if conversions:
                sub = subcategories[sub_idx] if phase == 'sub_attributes' and sub_idx < len(subcategories) else None
                self.db.set_conversions(phone_number, current_product, conversions, subcategory=sub)

                # Auto-detect primary unit from conversion values
                for conv_val in conversions.values():
                    val_match = re.match(r'^(\d+)\s+(.+)$', conv_val.strip())
                    if val_match:
                        detected_unit = val_match.group(2).strip()
                        self.db.set_primary_unit(phone_number, current_product, detected_unit)
                        break

        # NEXT: move to next subcategory or next product
        if phase in ('sub_attributes', 'series_attributes') and subcategories:
            next_sub = sub_idx + 1
            if next_sub < len(subcategories):
                # Configure next subcategory
                context['sub_idx'] = next_sub
                next_sub_name = subcategories[next_sub]
                self.db.save_session(phone_number, STATE_REG_SERIES, context)
                return [{"type": "text", "content": f"\u2705 Done with {subcategories[sub_idx]}!\n\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n*Next: {next_sub_name}* (under {current_product})\n\nDoes *{next_sub_name}* have different models/series?\n\n_List them or type \'skip\'._"}]

        # Move to next product
        next_p = p_idx + 1
        if next_p < len(products):
            next_product = products[next_p]
            context['p_idx'] = next_p
            context['subcategories'] = []
            context['sub_idx'] = 0
            context['phase'] = 'subcategories'
            self.db.save_session(phone_number, STATE_REG_SUBCATEGORIES, context)
            return [{"type": "text", "content": f"\u2705 *{current_product}* setup complete!\n\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n*Next product: {next_product}*\n\nDo you have different brands/types of *{next_product}*?\n\n_List them or type \'skip\'._"}]

        # ALL DONE
        self.db.save_session(phone_number, STATE_IDLE, {})
        return self._show_full_catalog(phone_number)

    def _handle_set_unit_cmd(self, phone_number, text):
        """Handle 'set unit Socks pairs' command"""
        clean = text.lower()
        for prefix in ['set unit', 'change unit', 'primary unit']:
            clean = clean.replace(prefix, '')
        clean = clean.strip()

        if not clean:
            # Show current units
            catalog = self.db.get_product_catalog(phone_number)
            products = catalog.get('products', {})
            if not products:
                return [{"type": "text", "content": "No products in catalog. Type *setup catalog* first."}]
            msg = "\ud83d\udccf *Primary Units:*\n\n"
            for p_name, p_data in products.items():
                unit = p_data.get('primary_unit', 'pieces')
                msg += f"  \u2022 *{p_name}*: {unit}\n"
            msg += "\n_To change: *set unit [product] [unit]*_\n"
            msg += "_Example: set unit Socks pairs_"
            return [{"type": "text", "content": msg}]

        # Parse: "Socks pairs" or "Socks to pairs"
        clean = clean.replace(' to ', ' ')
        parts = clean.split()
        if len(parts) >= 2:
            product_name = parts[0].title()
            unit = ' '.join(parts[1:]).lower()
            if self.db.set_primary_unit(phone_number, product_name, unit):
                return [{"type": "text", "content": f"\u2705 Primary unit for *{product_name}* set to *{unit}*.\n\nAll quantities will be stored/displayed in {unit}."}]
            else:
                return [{"type": "text", "content": f"\u274c *{product_name}* not found in catalog. Type *my catalog* to check."}]
        else:
            return [{"type": "text", "content": "\u2139\ufe0f Use format: *set unit [product] [unit]*\n\nExample: set unit Socks pairs\nExample: set unit Bags pieces"}]

    def _handle_remove_subcategory_cmd(self, phone_number, text):
        """Handle 'remove subcategory X from Y'"""
        clean = text.lower()
        for prefix in ['remove subcategory', 'remove brand', 'remove sub', 'delete subcategory', 'delete brand']:
            clean = clean.replace(prefix, '')
        clean = clean.strip()
        if ' from ' in clean:
            parts = clean.split(' from ')
            sub_name = parts[0].strip().title()
            product_name = parts[1].strip().title()
            if self.db.remove_subcategory(phone_number, product_name, sub_name):
                return [{"type": "text", "content": f"\u2705 Removed *{sub_name}* from *{product_name}*.\n\nType *my catalog* to see your catalog."}]
            else:
                return [{"type": "text", "content": f"\u274c *{sub_name}* not found under *{product_name}*. Type *my catalog* to check."}]
        elif clean:
            return [{"type": "text", "content": f"\u2139\ufe0f Use format: *remove subcategory {clean} from [product]*\n\nExample: remove subcategory Cancel from Bags"}]
        else:
            return [{"type": "text", "content": "\u2139\ufe0f Use format: *remove subcategory [name] from [product]*\n\nExample: remove subcategory Cancel from Bags"}]

    def _handle_remove_series_cmd(self, phone_number, text):
        """Handle 'remove series X from Y'"""
        clean = text.lower()
        for prefix in ['remove series', 'remove model', 'delete series', 'delete model']:
            clean = clean.replace(prefix, '')
        clean = clean.strip()
        if ' from ' in clean:
            parts = clean.split(' from ')
            series_name = parts[0].strip().title()
            sub_name = parts[1].strip().title()
            catalog = self.db.get_product_catalog(phone_number)
            products = catalog.get('products', {})
            found_product = None
            for p_name, p_data in products.items():
                if sub_name in p_data.get('subcategories', {}):
                    found_product = p_name
                    break
            if found_product:
                sub_data = products[found_product]['subcategories'][sub_name]
                series = sub_data.get('series', {})
                if series_name in series:
                    del series[series_name]
                    self.db.save_product_catalog(phone_number, catalog)
                    return [{"type": "text", "content": f"\u2705 Removed *{series_name}* from *{sub_name}*."}]
                else:
                    return [{"type": "text", "content": f"\u274c *{series_name}* not found under *{sub_name}*. Type *my catalog* to check."}]
            else:
                return [{"type": "text", "content": f"\u274c *{sub_name}* not found. Type *my catalog* to check."}]
        else:
            return [{"type": "text", "content": "\u2139\ufe0f Use format: *remove series [name] from [brand]*\n\nExample: remove series Jordan from Nike"}]

    def _enrich_with_unit_conversion(self, phone_number, pending):
        """Add base unit conversion info to a pending transaction"""
        quantity = pending.get('quantity')
        if not quantity:
            return pending

        # Extract numeric part and unit from quantity
        qty_str = str(quantity)
        qty_match = re.match(r'^([\d.]+)\s*(.*)', qty_str)
        if not qty_match:
            return pending

        qty_num = float(qty_match.group(1))
        qty_unit = qty_match.group(2).strip()

        if not qty_unit:
            return pending

        # Find the product in catalog
        product_name = pending.get('category_product') or pending.get('item_name', '')
        # Try to match against catalog products
        catalog = self.db.get_product_catalog(phone_number)
        products = catalog.get('products', {})

        matched_product = None
        matched_sub = None
        for p_name in products:
            if p_name.lower() in pending.get('description', '').lower():
                matched_product = p_name
                break
            # Check subcategories
            for sub_name in products[p_name].get('subcategories', {}):
                if sub_name.lower() in pending.get('description', '').lower():
                    matched_product = p_name
                    matched_sub = sub_name
                    break

        if not matched_product:
            # Try matching via the category field
            cat = pending.get('category', '')
            for p_name in products:
                if p_name.lower() == cat.lower():
                    matched_product = p_name
                    break

        if matched_product:
            base_qty, base_unit, conv_used = self.db.convert_to_base(
                phone_number, matched_product, int(qty_num), qty_unit, subcategory=matched_sub
            )
            if conv_used:
                pending['base_quantity'] = base_qty
                pending['base_unit'] = base_unit
                pending['conversion_used'] = conv_used

        return pending

    def _show_full_catalog(self, phone_number):
        """Display the complete catalog tree"""
        catalog = self.db.get_product_catalog(phone_number)
        products = catalog.get('products', {})

        if not products:
            return [{"type": "text", "content": "\ud83d\udce6 *Your Product Catalog is empty!*\n\nType *setup catalog* to get started."}]

        msg = "\ud83c\udf89 *Your Product Catalog*\n\n"

        for p_name, p_data in products.items():
            unit = p_data.get('primary_unit', '')
            unit_str = f" _({unit})_" if unit else ""
            msg += f"\ud83d\udce6 *{p_name}*{unit_str}\n"
            subcats = p_data.get('subcategories', {})
            p_attrs = p_data.get('attributes', {})
            p_convs = p_data.get('conversions', {})

            if p_attrs:
                for attr, vals in p_attrs.items():
                    if vals:
                        msg += f"  \u2022 {attr}: {', '.join(vals[:8])}"
                        if len(vals) > 8:
                            msg += f" (+{len(vals)-8})"
                        msg += "\n"
                    else:
                        msg += f"  \u2022 {attr}: _(any)_\n"

            if subcats:
                for sub_name, sub_data in subcats.items():
                    series = sub_data.get('series', {})
                    sub_attrs = sub_data.get('attributes', {})
                    sub_convs = sub_data.get('conversions', {})

                    if series:
                        msg += f"  \ud83c\udff7\ufe0f *{sub_name}*: {', '.join(series.keys())}\n"
                    else:
                        msg += f"  \ud83c\udff7\ufe0f *{sub_name}*\n"

                    if sub_attrs:
                        for attr, vals in sub_attrs.items():
                            if vals:
                                msg += f"    {attr}: {', '.join(vals[:6])}\n"

                    if sub_convs:
                        conv_str = ', '.join([f"{k} = {v}" for k, v in sub_convs.items()])
                        msg += f"    \ud83d\udd04 {conv_str}\n"

            if p_convs:
                conv_str = ', '.join([f"{k} = {v}" for k, v in p_convs.items()])
                msg += f"  \ud83d\udd04 {conv_str}\n"

            msg += "\n"

        msg += "\ud83d\udca1 I'll use this to parse your transactions accurately!\n\n"
        msg += "_Commands: setup catalog | add product | add subcategory | add series | my catalog_"
        return [{"type": "text", "content": msg}]

    def _handle_show_catalog(self, phone_number, text):
        """Show catalog command"""
        return self._show_full_catalog(phone_number)

    def _handle_add_product_cmd(self, phone_number, text):
        """Handle 'add product X' command"""
        parts = text.lower().replace('add product', '').replace('new product', '').strip()
        if parts:
            products = [p.strip().title() for p in parts.split(',') if p.strip()]
            added = []
            for p in products:
                if self.db.add_product(phone_number, p):
                    added.append(p)
            if added:
                return [{"type": "text", "content": f"\u2705 Added: {', '.join(added)}\n\nType *add subcategory [brand] under [product]* to add brands.\nType *my catalog* to see your catalog."}]
            else:
                return [{"type": "text", "content": "\u2139\ufe0f Those products already exist in your catalog."}]
        else:
            self.db.save_session(phone_number, STATE_REG_PRODUCTS, {})
            return [{"type": "text", "content": "\ud83d\udcdd What product do you want to add?\n_Type the name (or multiple separated by commas)._"}]

    def _handle_remove_product_cmd(self, phone_number, text):
        """Handle 'remove product X' command"""
        parts = text.lower().replace('remove product', '').replace('delete product', '').strip()
        if parts:
            name = parts.title()
            if self.db.remove_product(phone_number, name):
                return [{"type": "text", "content": f"\u2705 Removed *{name}* from your catalog."}]
            else:
                return [{"type": "text", "content": f"\u274c *{name}* not found. Type *my catalog* to see products."}]
        else:
            products = self.db.get_product_list(phone_number)
            if products:
                plist = '\n'.join([f"  {i+1}. {p}" for i, p in enumerate(products)])
                return [{"type": "text", "content": f"Which product to remove?\n\n{plist}\n\n_Type the name._"}]
            return [{"type": "text", "content": "Your catalog is empty."}]

    def _handle_add_subcategory_cmd(self, phone_number, text):
        """Handle 'add subcategory X under Y' or 'add brand X under Y'"""
        clean = text.lower().replace('add subcategory', '').replace('add brand', '').replace('add sub', '').strip()
        
        if ' under ' in clean:
            parts = clean.split(' under ')
            sub_name = parts[0].strip().title()
            product_name = parts[1].strip().title()
            if self.db.add_subcategory(phone_number, product_name, sub_name):
                return [{"type": "text", "content": f"\u2705 Added *{sub_name}* under *{product_name}*.\n\nAdd models: *add series [model] under {sub_name}*\nOr type *my catalog* to see your catalog."}]
            else:
                return [{"type": "text", "content": f"\u2139\ufe0f *{sub_name}* already exists under *{product_name}*."}]
        elif clean:
            return [{"type": "text", "content": f"\u2139\ufe0f Use format: *add subcategory {clean} under [product]*\n\nExample: add subcategory Nike under Shoes"}]
        else:
            return [{"type": "text", "content": "\u2139\ufe0f Use format: *add subcategory [name] under [product]*\n\nExample: add subcategory Nike under Shoes"}]

    def _handle_add_series_cmd(self, phone_number, text):
        """Handle 'add series X under Y'"""
        clean = text.lower().replace('add series', '').replace('add model', '').strip()
        
        if ' under ' in clean:
            parts = clean.split(' under ')
            series_name = parts[0].strip().title()
            sub_name = parts[1].strip().title()
            
            # Find which product this subcategory belongs to
            catalog = self.db.get_product_catalog(phone_number)
            products = catalog.get('products', {})
            found_product = None
            for p_name, p_data in products.items():
                if sub_name in p_data.get('subcategories', {}):
                    found_product = p_name
                    break
            
            if found_product:
                if self.db.add_series(phone_number, found_product, sub_name, series_name):
                    return [{"type": "text", "content": f"\u2705 Added *{series_name}* under *{sub_name}* ({found_product})."}]
                else:
                    return [{"type": "text", "content": f"\u2139\ufe0f *{series_name}* already exists under *{sub_name}*."}]
            else:
                return [{"type": "text", "content": f"\u274c *{sub_name}* not found. Type *my catalog* to check."}]
        else:
            return [{"type": "text", "content": "\u2139\ufe0f Use format: *add series [name] under [brand]*\n\nExample: add series Air Force 1 under Nike"}]

