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
STATE_AWAITING_CRM_HINT = "AWAITING_CRM_HINT"
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
STATE_EDITING = 'editing'
STATE_EDIT_TRANSACTION = 'edit_transaction'
STATE_DELETE_CONFIRM = 'delete_confirm'

STATE_REG_ATTR_SUGGEST = 'reg_attr_suggest'

# States where only essential commands are allowed (catalog is being set up)
CATALOG_ACTIVE_STATES = {
    'reg_products', 'reg_subcategories', 'reg_series',
    'reg_attributes', 'reg_attr_values', 'reg_conversions',
    'reg_attr_suggest'
}

# Commands allowed to interrupt catalog setup
CATALOG_ALLOWED_COMMANDS = {'cancel', 'edit', 'delete_entry', 'undo', 'redo'}


# ==========================================
# COMMANDS (what the user can type)
# ==========================================

COMMANDS = {
    # Greetings (English + Pidgin + Yoruba + Igbo + Hausa)
    'greeting': [
        'hi', 'hello', 'hey', 'start', 'begin', 'good morning', 'good afternoon', 'good evening',
        'how are you', 'sup', 'whats up', 'how far', 'how you dey', 'wetin dey',
        'howdy', 'whats good', 'hows it going', 'morning', 'evening', 'afternoon',
        'e kaaro', 'e kaasan', 'bawo ni', 'kedu', 'sannu', 'nagode',
        'omo whats up', 'guy whats up', 'bro', 'bros', 'oga', 'boss',
        'menu', 'main menu', 'open menu', 'show menu',
    ],
    # Reports
    'report': ['report', 'summary', 'how much', 'balance', 'overview', 'dashboard'],
    'today': ['today', 'today report', 'todays record', 'what did i do today'],
    'week': ['this week', 'week', 'weekly', 'last 7 days'],
    'month': ['this month', 'month', 'monthly', 'last 30 days'],
    'my_sales': ['my sales', 'sales report', 'all sales', 'show sales', 'show my sales', 'today sales', 'todays sales', 'sales today', 'week sales', 'sales this week'],
    'my_purchases': ['my purchases', 'purchase report', 'all purchases', 'show purchases', 'show my purchases', 'today purchases', 'purchases today', 'week purchases', 'purchases this week'],
    'my_expenses': ['my expenses', 'expenses', 'expense report', 'show expenses', 'business expenses', 'operating expenses', 'today expenses', 'expenses today', 'week expenses', 'expenses this week'],
    # Help
    'help': ['help', 'menu', 'commands', 'what can you do', 'how does this work', 'guide', 'tutorial'],
    # Export
    'export': ['export', 'excel', 'csv', 'download', 'spreadsheet', 'send me file'],
    'invoice': ['invoice', 'generate invoice', 'create invoice'],
    'receipt': ['receipt', 'generate receipt'],
    'statement': ['statement', 'my statement', 'financial statement', 'account statement'],
    # CRM
    'customers': ['customers', 'customer', 'who buy from me', 'my buyers', 'client', 'clients', 'my customers', 'customer catalog'],
    'suppliers': ['suppliers', 'supplier', 'who i buy from', 'my vendors', 'vendor', 'vendors'],
    'contacts': ['contacts', 'contact', 'crm', 'people'],
    'who_owes_me': ['who owes me', 'who owe me', 'my debtors', 'outstanding debts', 'debtors', 'who hasn\'t paid', 'debt list', 'owes me', 'owe me'],
    'who_i_owe': ['who i owe', 'i owe', 'my creditors', 'what i owe', 'people i owe', 'suppliers i owe'],
    'i_owe': ['i owe', 'my debts', 'my debt', 'what i owe', 'who do i owe', 'what do i owe', 'my creditors', 'i am owing', 'i dey owe', 'creditors'],
    'debt_summary': ['debt summary', 'debt report', 'credit report', 'all debts', 'debt overview'],
    'remind_debtor': ['remind', 'send reminder', 'chase', 'follow up', 'ping', 'message debtor', 'remind debtor'],
    'remind_all_debtors': ['remind all', 'send reminders to all', 'chase everyone', 'remind everyone who owes'],
    'save_contact_phone': ['save number', 'add number', 'contact number', 'phone number for'],
    'record_debt': ['gave credit', 'sold on credit', 'gave goods', 'on credit', 'credit sale', 'owing me', 'took on credit'],
    'record_i_owe': ['i bought on credit', 'took on credit from', 'i owe', 'credit purchase', 'i am owing'],
    'debt_paid': ['paid me', 'has paid', 'settled debt', 'cleared debt', 'debt cleared', 'paid back', 'they paid'],
    'i_paid_debt': ['i paid', 'i have paid', 'i cleared', 'i settled', 'paid my debt', 'i paid back'],
    'contact_profile': ['profile', 'contact info', 'customer profile', 'show contact', 'tell me about'],
    'top_customers': ['top customers', 'best customers', 'biggest buyers', 'top clients'],
    'top_suppliers': ['top suppliers', 'best suppliers', 'biggest vendors'],
    'inactive_contacts': ['inactive customers', 'lost customers', 'who hasn\'t bought', 'not buying', 'dormant'],
    'set_credit_terms': ['set credit limit', 'credit limit for', 'set credit days', 'credit terms'],
    'contact_catalog': ['contact catalog', 'customer catalog', 'supplier catalog', 'all contacts', 'contact list'],
    'add_note': ['add note', 'note for', 'note about', 'remember about'],
    # Actions
    'undo': ['undo', 'delete last', 'cancel last', 'remove last', 'wrong one', 'mistake'],
    'redo': ['redo', 'restore last', 'bring back', 'i didnt mean that', 'restore deleted', 'undo the undo'],
    'upgrade': ['upgrade', 'plan', 'pricing', 'subscribe', 'premium', 'pro'],
    'change_category': ['change category', 'update category', 'change business type', 'update business'],
    'set_bank': ['set bank', 'bank details', 'my bank', 'payment details', 'set account', 'bank account'],
    'set_tax': ['set tax', 'set vat', 'default tax', 'tax rate', 'my tax rate', 'vat rate'],
    'set_unit': ['set unit', 'set units', 'set conversion', '1 carton', '1 pack', '1 dozen', '1 bag', '1 box', '1 crate'],
    'set_bank': ['set bank', 'bank details', 'my bank', 'add bank', 'payment details', 'account details'],
    'change_industry': ['change industry', 'switch industry', 'update industry', 'my industry', 'industry type'],
    'set_recipe': ['set recipe', 'add recipe', 'new recipe', 'create recipe', 'define recipe', 'bom', 'bill of materials'],
    'organize_product': ['organize', 'organize product', 'set attributes', 'product attributes', 'setup product'],
    'production': ['produced', 'production', 'manufactured', 'made', 'baked', 'produced today', 'production run'],
    'my_recipes': ['my recipes', 'recipes', 'show recipes', 'view recipes', 'all recipes'],
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


# ==========================================
# INTERACTIVE MENU ID → COMMAND MAPPING
# When user taps a list menu item, WhatsApp sends the row ID.
# Map these to the text commands the engine already handles.
# ==========================================
MENU_ID_MAP = {
    # Main menu items
    "menu_record_sale": "__PROMPT_RECORD_SALE__",
    "menu_record_purchase": "__PROMPT_RECORD_PURCHASE__",
    "menu_record_payment": "__PROMPT_RECORD_PAYMENT__",
    "menu_set_recipe": "__PROMPT_SET_RECIPE__",
    "menu_production": "__PROMPT_PRODUCTION__",
    "menu_my_recipes": "my recipes",
    "menu_record_service": "__PROMPT_RECORD_SERVICE__",
    # Guided flow IDs
    "guided_cash_sale": "__GUIDED_START_cash_sale__",
    "guided_credit_sale": "__GUIDED_START_credit_sale__",
    "guided_type_sale": "__PROMPT_TYPE_SALE__",
    "guided_cash_purchase": "__GUIDED_START_cash_purchase__",
    "guided_credit_purchase": "__GUIDED_START_credit_purchase__",
    "guided_type_purchase": "__PROMPT_TYPE_PURCHASE__",
    "guided_cash_expense": "__GUIDED_START_cash_expense__",
    "guided_type_expense": "__PROMPT_TYPE_EXPENSE__",
    "guided_skip": "__GUIDED_SKIP__",
    "menu_record_expense": "__PROMPT_RECORD_EXPENSE__",
    "menu_reports": "__REPORTS_MENU__",
    "menu_documents": "__DOCUMENTS_MENU__",
    "menu_debts": "__DEBTS_MENU__",
    "menu_contacts": "__CONTACTS_MENU__",
    "menu_catalog": "__CATALOG_MENU__",
    # Debts sub-menu
    "debt_who_owes_me": "who owes me",
    "debt_who_i_owe": "who i owe",
    # Contacts sub-menu
    "contacts_customers": "__SHOW_CUSTOMERS__",
    "contacts_suppliers": "__SHOW_SUPPLIERS__",
    "contacts_all": "contacts",
    # Reports sub-menu (choose type → then choose period)
    "rpt_sales": "__RPT_CHOOSE_PERIOD_sales__",
    "rpt_purchases": "__RPT_CHOOSE_PERIOD_purchases__",
    "rpt_expenses": "__RPT_CHOOSE_PERIOD_expenses__",
    "rpt_summary": "report",
    # Report period buttons
    "rpt_sales_today": "__RPT_EXEC_my_sales_today__",
    "rpt_sales_week": "__RPT_EXEC_my_sales_week__",
    "rpt_sales_month": "__RPT_EXEC_my_sales_month__",
    "rpt_sales_custom": "__RPT_CUSTOM_sales__",
    "rpt_purchases_today": "__RPT_EXEC_my_purchases_today__",
    "rpt_purchases_week": "__RPT_EXEC_my_purchases_week__",
    "rpt_purchases_month": "__RPT_EXEC_my_purchases_month__",
    "rpt_purchases_custom": "__RPT_CUSTOM_purchases__",
    "rpt_expenses_today": "__RPT_EXEC_my_expenses_today__",
    "rpt_expenses_week": "__RPT_EXEC_my_expenses_week__",
    "rpt_expenses_month": "__RPT_EXEC_my_expenses_month__",
    "rpt_expenses_custom": "__RPT_CUSTOM_expenses__",
    # Documents sub-menu options
    "doc_invoice": "invoice",
    "doc_receipt": "receipt",
    "doc_statement": "statement",
    "doc_export": "export",
    "doc_bank_details": "set bank",
    # Catalog sub-menu options
    "cat_all_products": "my catalog",
    "cat_customers": "customers",
    "cat_by_brand": "__CATALOG_BY_BRAND__",
    "cat_top_sellers": "__CATALOG_TOP_SELLERS__",
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
        # ─── RATE LIMITING ───
        # Prevent spam/abuse that burns OpenAI credits
        # Only apply to existing users (don't block onboarding)
        from datetime import datetime
        import time as _time
        
        _user_exists = self.db.user_exists(phone_number)
        rate_data = self.db.get_rate_limit(phone_number) if _user_exists else None
        now_ts = int(_time.time())
        
        if rate_data:
            # Check per-minute limit (30 msgs/min)
            minute_count = rate_data.get('minute_count', 0)
            minute_reset = rate_data.get('minute_reset', 0)
            if now_ts - minute_reset > 60:
                minute_count = 0
                minute_reset = now_ts
            
            # Check per-hour limit (200 msgs/hour)
            hour_count = rate_data.get('hour_count', 0)
            hour_reset = rate_data.get('hour_reset', 0)
            if now_ts - hour_reset > 3600:
                hour_count = 0
                hour_reset = now_ts

            # Check per-day limit (500 msgs/day)
            day_count = rate_data.get('day_count', 0)
            day_reset = rate_data.get('day_reset', 0)
            if now_ts - day_reset > 86400:
                day_count = 0
                day_reset = now_ts

            # Enforce limits
            if minute_count >= 30:
                return [{"type": "text", "content": "\u23f3 Slow down! You\'re sending messages too fast.\nPlease wait a moment and try again."}]
            if hour_count >= 200:
                return [{"type": "text", "content": "\u26a0\ufe0f You\'ve reached the hourly message limit (200).\nPlease wait a while before sending more."}]
            if day_count >= 500:
                return [{"type": "text", "content": "\u26a0\ufe0f Daily message limit reached (500).\nYour limit resets tomorrow. Contact support if you need more."}]

            # Update counts
            self.db.update_rate_limit(phone_number, {
                'minute_count': minute_count + 1,
                'minute_reset': minute_reset,
                'hour_count': hour_count + 1,
                'hour_reset': hour_reset,
                'day_count': day_count + 1,
                'day_reset': day_reset,
            })
        elif _user_exists:
            # First tracked message — initialize rate limit
            self.db.update_rate_limit(phone_number, {
                'minute_count': 1,
                'minute_reset': now_ts,
                'hour_count': 1,
                'hour_reset': now_ts,
                'day_count': 1,
                'day_reset': now_ts,
            })
        # else: new user, skip rate limiting (let them onboard)

        # Get or create session
        session = self.db.get_session(phone_number)

        # Determine state
        if session and session.get('state') == STATE_ONBOARDING:
            # Mid-onboarding — trust the session (user hasn't finished setup yet)
            state = STATE_ONBOARDING
            context = session.get('context', {})
        elif not self.db.user_exists(phone_number):
            # New user or wiped data — start onboarding
            state = STATE_NEW_USER
            context = {}
        elif session is None:
            state = STATE_IDLE
            context = {}
        else:
            state = session.get('state', STATE_IDLE)
            context = session.get('context', {})

        # Route to correct handler based on state
        # MENU ITEM TAP — always route to idle handler regardless of state
        text_check = text.lower().strip()
        if text_check in MENU_ID_MAP or text_check.startswith('filter_'):
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_idle(phone_number, text)

        # DYNAMIC CATALOG IDs (product/variant drill-down)
        if text_check.startswith('cat_product_'):
            product_key = text_check.replace('cat_product_', '')
            return self._show_product_detail(phone_number, product_key)
        elif text_check.startswith('cat_variant_'):
            parts = text_check.replace('cat_variant_', '').split('__')
            if len(parts) == 2:
                return self._show_variant_detail(phone_number, parts[0], parts[1])
        elif text_check.startswith('cat_sell_'):
            product_key = text_check.replace('cat_sell_', '')
            user = self.db.get_user(phone_number)
            cat = user.get('auto_catalog', {}).get('products', {}) if user else {}
            prod_name = cat.get(product_key, {}).get('name', 'product')
            self.db.save_session(phone_number, 'STATE_GUIDED', {
                'step': 'ask_quantity', 'flow_type': 'cash_sale',
                'tx_type': 'income', 'payment': 'cash', 'item': prod_name
            })
            return [{"type": "text", "content": f"\U0001f4b0 *Selling {prod_name}* \u2014 How many?"}]
        elif text_check.startswith('cat_restock_'):
            product_key = text_check.replace('cat_restock_', '')
            user = self.db.get_user(phone_number)
            cat = user.get('auto_catalog', {}).get('products', {}) if user else {}
            prod_name = cat.get(product_key, {}).get('name', 'product')
            self.db.save_session(phone_number, 'STATE_GUIDED', {
                'step': 'ask_quantity', 'flow_type': 'cash_purchase',
                'tx_type': 'expense', 'payment': 'cash', 'item': prod_name
            })
            return [{"type": "text", "content": f"\U0001f4e6 *Restocking {prod_name}* \u2014 How many?"}]
        elif text_check.startswith('cat_edit_'):
            product_key = text_check.replace('cat_edit_', '')
            user = self.db.get_user(phone_number)
            cat = user.get('auto_catalog', {}).get('products', {}) if user else {}
            prod = cat.get(product_key, {})
            prod_name = prod.get('name', 'product')
            return [{"type": "text", "content": (
                f"\u270f\ufe0f *Edit {prod_name}*\n\n"
                f"What would you like to update?\n\n"
                f"\u2022 *set unit {prod_name}: 1 carton = 12*\n"
                f"\u2022 *set tax {prod_name} 7.5%*\n"
                f"\u2022 *set recipe {prod_name}* (manufacturing)\n"
                f"\u2022 Type *add variant {prod_name}: Red, Large*"
            )}]
        elif text_check == 'cat_organize':
            # Show list of products to organize
            user = self.db.get_user(phone_number)
            catalog = user.get('auto_catalog', {}) if user else {}
            products = catalog.get('products', {})
            # Also check old product_catalog field and merge
            old_catalog = user.get('product_catalog', {}) if user else {}
            if old_catalog and isinstance(old_catalog, dict):
                for old_key, old_val in old_catalog.items():
                    if not isinstance(old_val, dict) or old_key in ('products', 'brands', 'categories', 'settings'):
                        continue
                    norm_key = old_key.lower().replace(' ', '-')
                    if norm_key not in products:
                        products[norm_key] = {'name': old_key.replace('-', ' ').title(), 'brand': '', 'item': old_key.title(),
                                           'category': '', 'sell_prices': [], 'buy_prices': [],
                                           'total_sold': 0, 'total_bought': 0, 'customers': [], 'suppliers': [],
                                           'last_activity': '', 'variants': {}}
            if not products:
                # Show add option instead of dead-end text
                self.db.save_session(phone_number, 'STATE_ADD_PRODUCT', {})
                return [{"type": "text", "content": (
                    "\U0001f4e6 *No products yet!*\n\n"
                    "Add a product first, then organize it.\n\n"
                    "\U0001f449 *Type your product name:*\n"
                    "_(e.g. Socks, Caps, Slides, Bags)_"
                )}]
            rows_org = []
            for key, prod in list(products.items())[:8]:
                name = prod.get('name', key.title())
                has_hierarchy = '\u2705 ' if prod.get('hierarchy') else ''
                hierarchy_info = ' > '.join(prod.get('hierarchy', [])).title() if prod.get('hierarchy') else 'Not organized yet'
                rows_org.append({"id": f"cat_org_{key}", "title": f"{has_hierarchy}{name}"[:24], "description": hierarchy_info[:72]})
            rows_org.append({"id": "cat_org_new", "title": "\u2795 New Product", "description": "Add & organize a new product"})
            return [{"type": "list", "content": {
                "header": "\u2699\ufe0f Organize Products",
                "body": "Choose a product to set up its attribute levels.\n\n\u2705 = already organized\n\nEach level is a drill-down: Pattern \u2192 Brand \u2192 Color \u2192 Size",
                "button_text": "\u2699\ufe0f Choose Product",
                "sections": [{"title": "Your Products", "rows": rows_org}]
            }}]
        elif text_check.startswith('cat_org_'):
            # User picked a product to organize
            key = text_check.replace('cat_org_', '')
            if key == 'new':
                self.db.save_session(phone_number, 'STATE_ORGANIZE', {'step': 'ask_product'})
                return [{"type": "text", "content": "\u2795 *New Product*\n\nType the product name:\n_Example: Socks, Caps, Slides, Bags_"}]
            return self._handle_organize_product(phone_number, f"organize {key}")
        elif text_check == 'cat_add_product':
            self.db.save_session(phone_number, 'STATE_ADD_PRODUCT', {})
            return [{"type": "text", "content": (
                "\u2795 *Add a Product*\n\n"
                "Type the product name:\n"
                "_Example: Nike Socks, Gucci Slides, iPhone 15_"
            )}]
        elif text_check == 'cat_all_products' or text_check == 'cat_browse':
            return self._show_product_list(phone_number)
        elif text_check == 'cat_top_sellers':
            return self._show_top_sellers(phone_number)
        elif text_check.startswith('org_attr_'):
            # Attribute selection during organize flow
            attr_name = text_check.replace('org_attr_', '')
            return self._handle_organize_attr_pick(phone_number, attr_name)
        elif text_check.startswith('org_done'):
            return self._handle_organize_done(phone_number)
        elif text_check.startswith('cat_tree_'):
            # Tree drill-down navigation
            return self._handle_tree_drilldown(phone_number, text_check)
        elif text_check.startswith('cat_add_'):
            # Add item at a tree level
            raw = text_check.replace('cat_add_', '')
            parts = raw.split('__')
            product_key = parts[0]
            path = [p for p in parts[1:] if p]
            # Save state and ask user what to add
            user = self.db.get_user(phone_number)
            catalog = user.get('auto_catalog', {}) if user else {}
            product = catalog.get('products', {}).get(product_key, {})
            hierarchy = product.get('hierarchy', [])
            depth = len(path)
            attr_labels = {'pattern': 'Pattern', 'brand': 'Brand', 'color': 'Color', 'size': 'Size', 'material': 'Material'}
            next_attr = hierarchy[depth] if depth < len(hierarchy) else 'item'
            attr_label = attr_labels.get(next_attr, next_attr.title())
            breadcrumb = product.get('name', 'Product')
            for p in path:
                breadcrumb += f" > {p.title()}"
            self.db.save_session(phone_number, 'STATE_TREE_ADD', {
                'product_key': product_key, 'path': path, 'attr_name': next_attr
            })
            return [{"type": "text", "content": (
                f"\u2795 *Add {attr_label}* to *{breadcrumb}*\n\n"
                f"Type the new {attr_label.lower()} name:\n"
                f"_(e.g. Nike, Blue, Size 15, Striped)_\n\n"
                f"Or type *cancel* to go back."
            )}]
        elif text_check.startswith('cat_editlvl_'):
            # Remove item from a tree level — show what can be removed
            raw = text_check.replace('cat_editlvl_', '')
            parts = raw.split('__')
            product_key = parts[0]
            path = [p for p in parts[1:] if p]
            user = self.db.get_user(phone_number)
            catalog = user.get('auto_catalog', {}) if user else {}
            product = catalog.get('products', {}).get(product_key, {})
            tree = product.get('tree', {})
            # Navigate to current node
            current = tree
            for p in path:
                if p in current:
                    current = current[p]
            # List children for removal
            children = [k for k in current.keys() if k != '_meta']
            if not children:
                return [{"type": "text", "content": "\u26a0\ufe0f Nothing to remove at this level."}]
            breadcrumb = product.get('name', 'Product')
            for p in path:
                breadcrumb += f" > {p.title()}"
            # Build removal list
            rows = []
            for child in children[:10]:
                child_meta = current[child].get('_meta', {}) if isinstance(current[child], dict) else {}
                stock = child_meta.get('stock', 0)
                path_str = '__'.join(path + [child]) if path else child
                rows.append({
                    "id": f"cat_rm_{product_key}__{path_str}"[:200],
                    "title": f"\U0001f5d1 {child.title()}"[:24],
                    "description": f"Stock: {stock} (will be deleted)"[:72]
                })
            return [{"type": "list", "content": {
                "header": f"\U0001f5d1 Remove from {breadcrumb}"[:60],
                "body": "Tap an item to remove it (and everything under it).",
                "button_text": "\U0001f5d1 Remove"[:20],
                "sections": [{"title": "Select to Remove", "rows": rows}]
            }}]
        elif text_check.startswith('cat_rm_'):
            # Confirm and execute removal
            raw = text_check.replace('cat_rm_', '')
            parts = raw.split('__')
            product_key = parts[0]
            path_to_remove = [p for p in parts[1:] if p]
            if not path_to_remove:
                return [{"type": "text", "content": "\u26a0\ufe0f Nothing to remove."}]
            item_to_remove = path_to_remove[-1]
            parent_path = path_to_remove[:-1]
            # Navigate to parent and delete child
            user = self.db.get_user(phone_number)
            catalog = user.get('auto_catalog', {}) if user else {}
            products = catalog.get('products', {})
            product = products.get(product_key, {})
            tree = product.get('tree', {})
            current = tree
            for p in parent_path:
                if p in current:
                    current = current[p]
            if item_to_remove in current:
                del current[item_to_remove]
                product['tree'] = tree
                catalog['products'][product_key] = product
                self.db.update_user_field(phone_number, 'auto_catalog', catalog)
                return [{"type": "text", "content": f"\u2705 *{item_to_remove.title()}* removed from catalog.\n\nType *catalog* to view updated tree."}]
            return [{"type": "text", "content": "\u26a0\ufe0f Item not found."}]

        # COMMAND BREAKOUT: escape stuck states with any known command
        # BUT during catalog setup, only allow cancel/edit/delete/undo
        if state not in [STATE_NEW_USER, STATE_ONBOARDING, STATE_IDLE, None, '']:
            cmd_check = self._detect_command(text.lower().strip())
            if cmd_check:
                # During catalog setup — block all commands except essentials
                if state in CATALOG_ACTIVE_STATES:
                    if cmd_check not in CATALOG_ALLOWED_COMMANDS:
                        # Tell user to finish or cancel
                        step_names = {
                            'reg_products': 'listing your products',
                            'reg_subcategories': 'adding subcategories/brands',
                            'reg_series': 'adding series/models',
                            'reg_attributes': 'setting attributes',
                            'reg_attr_values': 'entering attribute values',
                            'reg_conversions': 'setting unit conversions',
                        }
                        step = step_names.get(state, 'catalog setup')
                        return [{"type": "text", "content": (
                            "\u2699\ufe0f You're currently in the middle of *" + step + "*.\n\n"
                            "Please complete this step first, or type *cancel* to stop.\n\n"
                            "_Commands like reports, transactions etc. will work once you're done._"
                        )}]
                # Not in catalog — allow command breakout as normal
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

        elif state == STATE_AWAITING_CRM_HINT:
            return self._handle_crm_hint(phone_number, text, context)

        elif state == 'STATE_REPORT_CUSTOM':
            # User is typing a custom filter for a report
            text_lower = text.lower().strip()
            # Allow escape
            if text_lower in ('cancel', 'hi', 'hello', 'start', 'help', 'menu', 'back'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                if text_lower in ('hi', 'hello', 'start', 'menu', 'help'):
                    return self._handle_greeting(phone_number)
                return [{"type": "text", "content": "👍 Cancelled. What else can I help with?"}]
            # Execute report with whatever they typed as the time_text
            report_type = context.get('report_type', 'my_sales')
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_filtered_report(phone_number, report_type, time_text=text)

        elif state == 'STATE_CHANGE_INDUSTRY':
            # User picked new industry from list
            text_lower = text.lower().strip()
            industry_map = {
                'industry_trading': 'trading', 'industry_manufacturing': 'manufacturing',
                'industry_services': 'services', 'industry_hybrid': 'hybrid',
                'trading': 'trading', 'manufacturing': 'manufacturing',
                'services': 'services', 'hybrid': 'hybrid',
                'cancel': None, 'back': None,
            }
            new_industry = industry_map.get(text_lower, '')
            if new_industry is None:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Cancelled. Industry unchanged."}]
            if not new_industry:
                # Try partial match
                if any(w in text_lower for w in ['trad', 'retail', 'sell', 'buy']):
                    new_industry = 'trading'
                elif any(w in text_lower for w in ['manufact', 'produc', 'factory']):
                    new_industry = 'manufacturing'
                elif any(w in text_lower for w in ['service', 'consult', 'profess']):
                    new_industry = 'services'
                else:
                    new_industry = 'hybrid'
            
            self.db.update_user_field(phone_number, 'industry_class', new_industry)
            self.db.save_session(phone_number, STATE_IDLE, {})
            
            labels = {'trading': '\U0001f6cd\ufe0f Trading & Retail', 'manufacturing': '\U0001f3ed Manufacturing',
                     'services': '\U0001f4bc Services', 'hybrid': '\U0001f504 Hybrid'}
            
            return [{"type": "text", "content": (
                f"\u2705 Industry updated to: *{labels[new_industry]}*\n\n"
                f"Your categories, reports, and P&L are now tailored for this industry.\n\n"
                f"\U0001f4a1 Your existing transactions are preserved \u2014 only future categorization and reports change."
            )}]


        elif state == 'STATE_GUIDED':
            return self._handle_guided_step(phone_number, text, context)

        elif state == 'STATE_ORGANIZE':
            step = context.get('step', '')
            if step == 'ask_product':
                if text.lower().strip() in ('cancel', 'back'):
                    self.db.save_session(phone_number, STATE_IDLE, {})
                    return [{"type": "text", "content": "\U0001f44d Cancelled."}]
                return self._handle_organize_product(phone_number, f"organize {text}")
            elif step == 'custom_attr':
                if text.lower().strip() in ('cancel', 'back'):
                    self.db.save_session(phone_number, STATE_IDLE, {})
                    return [{"type": "text", "content": "\U0001f44d Cancelled."}]
                return self._handle_organize_attr_pick(phone_number, text.strip().lower())
            else:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return self._handle_organize_product(phone_number, f"organize {text}")

        elif state == 'STATE_TREE_ADD':
            # User typing new value to add at a tree level
            if text.lower().strip() in ('cancel', 'back', 'exit'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Cancelled."}]
            
            new_value = text.strip().lower()
            if len(new_value) < 1:
                return [{"type": "text", "content": "Please type a name to add:"}]
            
            product_key = context.get('product_key', '')
            path = context.get('path', [])
            attr_name = context.get('attr_name', '')
            
            # Navigate to parent node and add child
            user = self.db.get_user(phone_number)
            catalog = user.get('auto_catalog', {}) if user else {}
            products = catalog.get('products', {})
            product = products.get(product_key, {})
            tree = product.get('tree', {})
            
            current = tree
            for p in path:
                if p not in current:
                    current[p] = {'_meta': {'stock': 0, 'total_sold': 0, 'total_bought': 0}}
                current = current[p]
            
            # Add new child with empty _meta
            if new_value not in current:
                current[new_value] = {'_meta': {'stock': 0, 'total_sold': 0, 'total_bought': 0, 'sell_price': 0, 'buy_price': 0}}
            
            product['tree'] = tree
            catalog['products'][product_key] = product
            self.db.update_user_field(phone_number, 'auto_catalog', catalog)
            self.db.save_session(phone_number, STATE_IDLE, {})
            
            breadcrumb = product.get('name', 'Product')
            for p in path:
                breadcrumb += f" > {p.title()}"
            
            attr_labels = {'pattern': 'Pattern', 'brand': 'Brand', 'color': 'Color', 'size': 'Size', 'material': 'Material'}
            attr_label = attr_labels.get(attr_name, attr_name.title())
            
            return [{"type": "text", "content": (
                f"\u2705 *{new_value.title()}* added to *{breadcrumb}*\n\n"
                f"\U0001f4a1 Type *catalog* to see the updated tree.\n"
                f"Or add more: type the next {attr_label.lower()} name."
            )}]

        elif state == 'STATE_ADD_PRODUCT':
            # Manual product addition
            if text.lower().strip() in ('cancel', 'back', 'exit'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Cancelled."}]
            
            product_name = text.strip().title()
            if len(product_name) < 2:
                return [{"type": "text", "content": "Please type a product name (e.g. \"Nike Socks\"):"}]
            
            # Create product in catalog
            user = self.db.get_user(phone_number)
            catalog = user.get('auto_catalog', {}) if user else {}
            products = catalog.get('products', {})
            
            product_key = product_name.lower().replace(' ', '-')
            if product_key in products:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": f"\U0001f4e6 *{product_name}* already exists in your catalog!\n\nType *catalog* to view it."}]
            
            # Extract brand if multi-word (first word often is brand)
            words = product_name.split()
            brand = words[0] if len(words) >= 2 else ''
            item = ' '.join(words[1:]) if len(words) >= 2 else product_name
            
            products[product_key] = {
                'name': product_name,
                'brand': brand,
                'item': item,
                'category': '',
                'sell_prices': [],
                'buy_prices': [],
                'total_sold': 0,
                'total_bought': 0,
                'customers': [],
                'suppliers': [],
                'last_activity': '',
                'variants': {},
            }
            catalog['products'] = products
            self.db.update_user_field(phone_number, 'auto_catalog', catalog)
            self.db.save_session(phone_number, STATE_IDLE, {})
            
            return [{"type": "text", "content": (
                f"\u2705 *{product_name}* added to your catalog!\n\n"
                f"\U0001f4a1 It will auto-fill with prices and stats as you record transactions.\n\n"
                f"Optional next steps:\n"
                f"\u2022 *set unit {product_name}: 1 carton = 12*\n"
                f"\u2022 *set tax {product_name} 7.5%*\n"
                f"\u2022 Type *catalog* to view it"
            )}]

        elif state == 'STATE_RECIPE_NAME':
            # User is providing the product name for the recipe
            product_name = text.strip()
            if len(product_name) < 2:
                return [{"type": "text", "content": "Please type the product name (e.g. \"Bread\", \"Furniture\"):"}]
            if product_name.lower() in ('cancel', 'back', 'exit'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Recipe setup cancelled."}]
            self.db.save_session(phone_number, 'STATE_RECIPE_MATERIALS', {
                'product_name': product_name,
                'materials': []
            })
            return [{"type": "text", "content": (
                f"\U0001f4cb *Recipe for: {product_name}*\n\n"
                f"\U0001f449 List the raw materials needed for ONE BATCH.\n"
                f"Include quantity and unit for each.\n\n"
                f"_Example:_\n"
                f"1 bag flour\n"
                f"2kg sugar\n"
                f"1 litre oil\n"
                f"500g butter\n\n"
                f"\U0001f4a1 Type all materials (one per line or comma-separated)"
            )}]

        elif state == 'STATE_RECIPE_MATERIALS':
            # User listing raw materials
            if text.lower().strip() in ('cancel', 'back', 'exit'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Recipe setup cancelled."}]
            
            import re as _re
            product_name = context.get('product_name', 'Product')
            
            # Parse materials from text (one per line or comma-separated)
            raw_lines = _re.split(r'[,\n]', text)
            materials = []
            for line in raw_lines:
                line = line.strip()
                if not line:
                    continue
                # Try to parse: "1 bag flour", "2kg sugar", "500g butter"
                match = _re.match(r'(\d+\.?\d*)\s*([a-zA-Z]+)?\s+(.+)', line)
                if match:
                    qty = float(match.group(1))
                    unit = match.group(2) or 'units'
                    name = match.group(3).strip().title()
                    materials.append({'name': name, 'qty': qty, 'unit': unit, 'cost': 0})
                else:
                    # Just a name, no qty
                    materials.append({'name': line.title(), 'qty': 1, 'unit': 'units', 'cost': 0})

            if not materials:
                return [{"type": "text", "content": "I couldn't parse that. Try:\n_1 bag flour, 2kg sugar, 1 litre oil_"}]

            self.db.save_session(phone_number, 'STATE_RECIPE_COSTS', {
                'product_name': product_name,
                'materials': materials,
                'current_material_idx': 0
            })

            # Ask cost for first material
            mat = materials[0]
            return [{"type": "text", "content": (
                f"\u2705 Got {len(materials)} materials!\n\n"
                f"Now I need the cost of each.\n\n"
                f"\U0001f4b0 How much does *{mat['qty']} {mat['unit']} {mat['name']}* cost?\n"
                f"_(Just type the amount, e.g. 40000 or 40K)_"
            )}]

        elif state == 'STATE_RECIPE_COSTS':
            # User providing costs for each material one by one
            if text.lower().strip() in ('cancel', 'back', 'exit'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Recipe setup cancelled."}]

            from utils.parser import parse_amount
            product_name = context.get('product_name', 'Product')
            materials = context.get('materials', [])
            idx = context.get('current_material_idx', 0)

            # Parse the cost
            cost = parse_amount(text)
            if cost == 0:
                mat = materials[idx]
                return [{"type": "text", "content": f"\u26a0\ufe0f Couldn't get a number. How much does *{mat['qty']} {mat['unit']} {mat['name']}* cost?"}]

            materials[idx]['cost'] = cost

            # Move to next material or ask labour
            if idx + 1 < len(materials):
                next_mat = materials[idx + 1]
                self.db.save_session(phone_number, 'STATE_RECIPE_COSTS', {
                    'product_name': product_name,
                    'materials': materials,
                    'current_material_idx': idx + 1
                })
                return [{"type": "text", "content": (
                    f"\u2705 {materials[idx]['name']}: \u20a6{cost:,}\n\n"
                    f"\U0001f4b0 How much does *{next_mat['qty']} {next_mat['unit']} {next_mat['name']}* cost?"
                )}]
            else:
                # All materials costed — ask for labour
                self.db.save_session(phone_number, 'STATE_RECIPE_LABOUR', {
                    'product_name': product_name,
                    'materials': materials,
                })
                return [{"type": "text", "content": (
                    f"\u2705 {materials[idx]['name']}: \u20a6{cost:,}\n\n"
                    f"\U0001f4b0 *Labour cost* per batch?\n"
                    f"_(Workers, artisans, bakers, etc. Type 0 if none)_"
                )}]

        elif state == 'STATE_RECIPE_LABOUR':
            if text.lower().strip() in ('cancel', 'back', 'exit'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Recipe setup cancelled."}]

            from utils.parser import parse_amount
            product_name = context.get('product_name', 'Product')
            materials = context.get('materials', [])
            
            labour_cost = parse_amount(text) if text.strip() != '0' else 0

            self.db.save_session(phone_number, 'STATE_RECIPE_OVERHEAD', {
                'product_name': product_name,
                'materials': materials,
                'labour_cost': labour_cost,
            })
            return [{"type": "text", "content": (
                f"\U0001f4b0 *Overhead cost* per batch?\n"
                f"_(Power, gas, fuel, equipment wear, etc. Type 0 if none)_"
            )}]

        elif state == 'STATE_RECIPE_OVERHEAD':
            if text.lower().strip() in ('cancel', 'back', 'exit'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Recipe setup cancelled."}]

            from utils.parser import parse_amount
            product_name = context.get('product_name', 'Product')
            materials = context.get('materials', [])
            labour_cost = context.get('labour_cost', 0)
            
            overhead_cost = parse_amount(text) if text.strip() != '0' else 0

            self.db.save_session(phone_number, 'STATE_RECIPE_BATCH_SIZE', {
                'product_name': product_name,
                'materials': materials,
                'labour_cost': labour_cost,
                'overhead_cost': overhead_cost,
            })
            return [{"type": "text", "content": (
                f"\U0001f4e6 *How many units does one batch produce?*\n\n"
                f"_Example: 100 loaves, 50 chairs, 200 pieces_\n"
                f"_(Type number and unit, e.g. \"100 loaves\")_"
            )}]

        elif state == 'STATE_RECIPE_BATCH_SIZE':
            if text.lower().strip() in ('cancel', 'back', 'exit'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "\U0001f44d Recipe setup cancelled."}]

            import re as _re
            product_name = context.get('product_name', 'Product')
            materials = context.get('materials', [])
            labour_cost = context.get('labour_cost', 0)
            overhead_cost = context.get('overhead_cost', 0)

            # Parse: "100 loaves" or "100" or "50 pieces"
            match = _re.match(r'(\d+)\s*(\w+)?', text.strip())
            if not match:
                return [{"type": "text", "content": "Please type a number (e.g. \"100 loaves\" or just \"100\"):"}]

            batch_size = int(match.group(1))
            batch_unit = match.group(2) or 'units'
            if batch_size == 0:
                return [{"type": "text", "content": "Batch size can't be 0. How many units per batch?"}]

            # Calculate totals
            material_cost = sum(m['cost'] for m in materials)
            total_batch_cost = material_cost + labour_cost + overhead_cost
            cost_per_unit = round(total_batch_cost / batch_size)

            # Save recipe to catalog
            recipe = {
                'batch_size': batch_size,
                'batch_unit': batch_unit,
                'materials': materials,
                'labour_cost': labour_cost,
                'overhead_cost': overhead_cost,
                'total_batch_cost': total_batch_cost,
                'cost_per_unit': cost_per_unit,
            }

            # Store in auto_catalog
            user = self.db.get_user(phone_number)
            catalog = user.get('auto_catalog', {}) if user else {}
            products = catalog.get('products', {})
            
            product_key = product_name.lower().replace(' ', '-')
            product = products.get(product_key, {
                'name': product_name.title(),
                'brand': '', 'item': product_name.title(),
                'category': 'Production & Manufacturing',
                'sell_prices': [], 'buy_prices': [],
                'total_sold': 0, 'total_bought': 0,
                'customers': [], 'suppliers': [], 'last_activity': '',
            })
            product['recipe'] = recipe
            product['inventory'] = product.get('inventory', {'finished_goods': 0, 'last_production': ''})
            products[product_key] = product
            catalog['products'] = products
            self.db.update_user_field(phone_number, 'auto_catalog', catalog)
            self.db.save_session(phone_number, STATE_IDLE, {})

            # Build confirmation message
            mat_lines = []
            for m in materials:
                mat_lines.append(f"  \u2022 {m['qty']} {m['unit']} {m['name']} \u2014 \u20a6{m['cost']:,}")

            msg = (
                f"\u2705 *Recipe Saved: {product_name.title()}*\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                f"\U0001f4e6 *Batch: {batch_size} {batch_unit}*\n\n"
                f"\U0001f9f1 *Raw Materials:*\n{'\n'.join(mat_lines)}\n\n"
            )
            if labour_cost:
                msg += f"\U0001f477 *Labour:* \u20a6{labour_cost:,}\n"
            if overhead_cost:
                msg += f"\u26a1 *Overhead:* \u20a6{overhead_cost:,}\n"
            msg += (
                f"\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f4b0 *Total Batch Cost:* \u20a6{total_batch_cost:,}\n"
                f"\U0001f4ca *Cost Per Unit:* \u20a6{cost_per_unit:,}/{batch_unit[:-1] if batch_unit.endswith('s') else batch_unit}\n\n"
                f"\U0001f4a1 Now say \"produced {batch_size} {batch_unit}\" when you produce a batch!\n"
                f"Sales will auto-show your profit per unit."
            )

            return [{"type": "text", "content": msg}]


        elif state == STATE_RECORDING:
            # Allow user to escape this state with common commands
            text_check = text.lower().strip()
            if text_check in ('cancel', 'stop', 'exit', 'hi', 'hello', 'hey', 'start', 'begin', 'menu', 'help'):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return self._handle_idle(phone_number, text)
            # User is providing the amount for a transaction we couldn't parse amount for
            amount = parse_amount(text)
            if not amount:
                return [{"type": "text", "content": (
                    "\U0001f4b0 I need the amount. Just type a number:\n\n"
                    "E.g.: 95000 or 95K or \u20a695,000\n\n"
                    "_Or type *cancel* to start over._"
                )}]
            # Got amount — now re-process the original description with amount
            original_desc = context.get('description', '')
            is_credit = context.get('is_credit', False)
            name_hint = context.get('name_hint', '')
            
            if is_credit:
                # Credit sale with full details — use rich credit confirmation
                name = name_hint or self._extract_contact_name_from_text(original_desc, amount)
                if name:
                    self.db.save_session(phone_number, STATE_IDLE, {})
                    debt_type = 'owed_to_me'  # default for credit sales
                    if any(sig in original_desc.lower() for sig in ['i bought', 'i purchased', 'i owe', 'i took']):
                        debt_type = 'i_owe'
                    return self._build_rich_credit_confirmation(phone_number, original_desc, amount, name, debt_type)
                else:
                    # No name found — ask for it
                    self.db.save_session(phone_number, 'RECORDING_DEBT', {
                        'debt_type': 'owed_to_me',
                        'step': 'ask_name',
                        'amount': amount,
                        'description': original_desc,
                    })
                    return [{"type": "text", "content": "\U0001f4b0 \u20a6" + f"{amount:,} on credit.\n\nWho took goods on credit?\n_Type the customer's name_"}]
            else:
                # Normal transaction — re-run with amount in the text
                self.db.save_session(phone_number, STATE_IDLE, {})
                combined = f"{original_desc} {amount}"
                return self._handle_transaction(phone_number, combined)

        elif state == STATE_VIEWING_REPORT:
            return self._handle_report_selection(phone_number, text)

        elif state == STATE_EXPORTING:
            return self._handle_export_selection(phone_number, text)

        elif state == STATE_INVOICING:
            return self._handle_invoice_input(phone_number, text, context)

        elif state == "CHANGING_CATEGORY":
            return self._handle_category_change_response(phone_number, text)
        elif state == 'SETTING_BANK':
            return self._handle_setting_bank_state(phone_number, text, context)
        elif state == 'GENERATING_RECEIPT':
            return self._handle_receipt_selection(phone_number, text, context)
        elif state == 'CONFIRM_FORWARD':
            return self._handle_confirm_forward(phone_number, text, context)
        elif state == STATE_REG_PRODUCTS:
            return self._handle_reg_products(phone_number, text)
        elif state == 'CATALOG_SETUP_PRODUCTS':
            return self._handle_catalog_setup_products(phone_number, text, context)
        elif state == 'CATALOG_SETUP_DETAILS':
            return self._handle_catalog_setup_details(phone_number, text, context)
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
        elif state == STATE_REG_ATTR_SUGGEST:
            return self._handle_reg_attr_suggest(phone_number, text, context)
        elif state == 'RECORDING_DEBT':
            return self._handle_recording_debt_state(phone_number, text, context)
        elif state == 'REMINDING_DEBTOR':
            return self._handle_reminding_debtor_state(phone_number, text, context)
        elif state == 'AWAITING_BREAKDOWN':
            return self._handle_breakdown_state(phone_number, text, context)
        elif state == 'SETTING_CREDIT_TERMS':
            return self._handle_setting_credit_terms_state(phone_number, text, context)
        elif state == 'CONFIRMING_CREDIT_SALE':
            return self._handle_confirming_credit_sale_state(phone_number, text, context)
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
        """Handle multi-step conversational onboarding with industry classification"""
        step = context.get("step", "ask_business_name")

        if step == "ask_business_name":
            business_name = text.strip()
            if len(business_name) < 2:
                return [{"type": "text", "content": "Please type your business name (e.g. \"Mama T Foods\", \"TechFix Solutions\"):"}]

            self.db.save_session(phone_number, STATE_ONBOARDING, {
                "step": "ask_industry",
                "business_name": business_name
            })

            return [{"type": "list", "content": {
                "header": f"\U0001f91d Nice, {business_name}!",
                "body": "What type of business are you running?\n\nThis helps me tailor your bookkeeping, reports, and categories specifically for your industry.",
                "button_text": "\U0001f3ed Choose Industry",
                "sections": [
                    {
                        "title": "Choose Your Industry",
                        "rows": [
                            {"id": "industry_trading", "title": "\U0001f6cd\ufe0f Trading & Retail", "description": "Buy finished goods \u2192 sell to customers"},
                            {"id": "industry_manufacturing", "title": "\U0001f3ed Manufacturing", "description": "Buy raw materials \u2192 produce \u2192 sell"},
                            {"id": "industry_services", "title": "\U0001f4bc Services", "description": "Sell expertise, skills, or time"},
                            {"id": "industry_hybrid", "title": "\U0001f504 Hybrid / Mixed", "description": "Combination (e.g. sell goods + offer services)"},
                        ]
                    }
                ]
            }}]

        elif step == "ask_industry":
            business_name = context.get("business_name", "Your Business")
            text_lower = text.lower().strip()

            # Map responses to industry_class
            industry_map = {
                'industry_trading': 'trading',
                'industry_manufacturing': 'manufacturing', 
                'industry_services': 'services',
                'industry_hybrid': 'hybrid',
                # Also handle typed responses
                'trading': 'trading', 'retail': 'trading', 'buy and sell': 'trading',
                '1': 'trading', 'sell goods': 'trading', 'shop': 'trading',
                'manufacturing': 'manufacturing', 'production': 'manufacturing',
                'factory': 'manufacturing', '2': 'manufacturing', 'produce': 'manufacturing',
                'services': 'services', 'service': 'services', 'professional': 'services',
                '3': 'services', 'consulting': 'services', 'freelance': 'services',
                'hybrid': 'hybrid', 'mixed': 'hybrid', 'both': 'hybrid', '4': 'hybrid',
            }

            industry_class = industry_map.get(text_lower, '')
            if not industry_class:
                # Try partial matching
                if any(w in text_lower for w in ['sell', 'buy', 'trad', 'retail', 'shop', 'fashion', 'cloth', 'shoe', 'phone']):
                    industry_class = 'trading'
                elif any(w in text_lower for w in ['make', 'produc', 'manufactur', 'bak', 'factory', 'build']):
                    industry_class = 'manufacturing'
                elif any(w in text_lower for w in ['service', 'consult', 'repair', 'clean', 'salon', 'barb', 'tech', 'design', 'transport']):
                    industry_class = 'services'
                else:
                    industry_class = 'hybrid'  # Default if unclear

            self.db.save_session(phone_number, STATE_ONBOARDING, {
                "step": "ask_description",
                "business_name": business_name,
                "industry_class": industry_class
            })

            # Industry-specific follow-up question
            prompts = {
                'trading': "What do you sell? (e.g. \"fashion & shoes\", \"electronics\", \"groceries\")",
                'manufacturing': "What do you produce? (e.g. \"furniture\", \"bread & pastries\", \"clothing\")",
                'services': "What services do you offer? (e.g. \"phone repair\", \"catering\", \"logistics\")",
                'hybrid': "Describe your business briefly (e.g. \"sell phones + repair them\")",
            }

            industry_labels = {
                'trading': '\U0001f6cd\ufe0f Trading & Retail',
                'manufacturing': '\U0001f3ed Manufacturing',
                'services': '\U0001f4bc Services',
                'hybrid': '\U0001f504 Hybrid',
            }

            return [{"type": "text", "content": (
                f"\u2705 Industry: *{industry_labels[industry_class]}*\n\n"
                f"\U0001f449 *{prompts[industry_class]}*"
            )}]

        elif step == "ask_description":
            business_name = context.get("business_name", "Your Business")
            industry_class = context.get("industry_class", "trading")
            description = text.strip()

            if len(description) < 3:
                return [{"type": "text", "content": "Just give me a brief description \u2014 even a few words is fine!"}]

            # Infer specific business type from description
            business_type = self._infer_business_type(description)

            # Complete onboarding
            return self._complete_onboarding(phone_number, business_name, business_type, industry_class, description)

        else:
            # Unknown step — restart
            self.db.save_session(phone_number, STATE_ONBOARDING, {"step": "ask_business_name"})
            return [{"type": "text", "content": "Let\'s start over \u2014 *What\'s your business name?*"}]

    def _complete_onboarding(self, phone_number, business_name, business_type, industry_class, description=""):
        """Finish onboarding — create user with industry classification and show tailored welcome"""
        self.db.create_user(
            phone_number,
            business_type=business_type,
            business_name=business_name
        )
        # Save industry_class separately (create_user may not support it)
        self.db.update_user_field(phone_number, 'industry_class', industry_class)
        self.db.update_user_field(phone_number, 'business_description', description)
        self.db.save_session(phone_number, STATE_IDLE, {})

        # Industry-specific welcome and examples
        industry_configs = {
            'trading': {
                'emoji': '\U0001f6cd\ufe0f',
                'label': 'Trading & Retail',
                'features': [
                    '\U0001f4e6 *Stock tracking* \u2014 buy/sell prices, markup, turnover',
                    '\U0001f4ca *COGS reports* \u2014 what it cost vs what you earned',
                    '\U0001f465 *Customer catalog* \u2014 who buys what, how often',
                    '\U0001f4b3 *Credit sales* \u2014 track who owes you',
                ],
                'examples': [
                    'sold 10 Nike socks to Sandra 150K',
                    'bought 50 shirts from Alhaji 200K',
                    'Sandra owes me 50K',
                ],
            },
            'manufacturing': {
                'emoji': '\U0001f3ed',
                'label': 'Manufacturing',
                'features': [
                    '\U0001f9f1 *Raw material tracking* \u2014 inputs, quantities, costs',
                    '\u2699\ufe0f *Production costs* \u2014 labour, overhead, per-unit cost',
                    '\U0001f4ca *Yield reports* \u2014 input cost vs output revenue',
                    '\U0001f4b0 *Gross margin* \u2014 Revenue - (Materials + Labour + Overhead)',
                ],
                'examples': [
                    'bought 5 bags flour 60K',
                    'paid 3 bakers 45K salary',
                    'sold 200 loaves to Shoprite 80K',
                ],
            },
            'services': {
                'emoji': '\U0001f4bc',
                'label': 'Services',
                'features': [
                    '\U0001f4b0 *Revenue tracking* \u2014 by client, project, or service',
                    '\U0001f4b8 *Direct costs* \u2014 subcontractors, tools, materials',
                    '\U0001f465 *Client management* \u2014 who pays what, outstanding',
                    '\U0001f4ca *Profit per service* \u2014 what you earn after costs',
                ],
                'examples': [
                    'received 200K from GTBank for consulting',
                    'paid freelancer 50K for design work',
                    'bought fuel 15K for delivery',
                ],
            },
            'hybrid': {
                'emoji': '\U0001f504',
                'label': 'Hybrid Business',
                'features': [
                    '\U0001f6cd\ufe0f *Product sales* \u2014 buy/sell goods with stock tracking',
                    '\U0001f4bc *Service revenue* \u2014 track earnings from services',
                    '\U0001f4ca *Combined P&L* \u2014 goods margin + service margin',
                    '\U0001f4b3 *Full credit tracking* \u2014 debtors + creditors',
                ],
                'examples': [
                    'sold 2 phones to Bola 300K',
                    'received 15K from Femi for phone repair',
                    'bought screen protectors 40K',
                ],
            },
        }

        config = industry_configs.get(industry_class, industry_configs['trading'])
        
        features_text = '\n'.join(config['features'])
        examples_text = '\n'.join([f'\u2022 _\"{ex}\"_' for ex in config['examples']])

        msg = (
            f"\u2705 *{business_name} is ready!* \U0001f389\n\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"{config['emoji']} Industry: *{config['label']}*\n"
            f"\U0001f3f7\ufe0f Category: *{business_type}*\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            f"\U0001f31f *Tailored for you:*\n{features_text}\n\n"
            f"\U0001f4dd *Try recording a transaction:*\n{examples_text}\n\n"
            f"\U0001f4a1 Or tap *Menu* below to explore all features."
        )

        return [{"type": "text", "content": msg}]


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

    def _handle_set_bank(self, phone_number, text):
        """Handle setting bank details for invoices"""
        # Check if user already has bank details
        user = self.db.get_user(phone_number)
        bank_name = user.get('bank_name', '') if user else ''
        account_number = user.get('account_number', '') if user else ''
        account_name = user.get('account_name', '') if user else ''

        if bank_name and account_number:
            # Show current and ask to update
            self.db.save_session(phone_number, 'SETTING_BANK', {'step': 'confirm_update'})
            return [{"type": "text", "content": (
                f"🏦 *Your Payment Details:*\n\n"
                f"Bank: {bank_name}\n"
                f"Account: {account_number}\n"
                f"Name: {account_name}\n\n"
                "Want to update? Type your new details:\n"
                "*[Bank name] [Account number] [Account name]*\n\n"
                "Or type *cancel* to keep current."
            )}]
        else:
            self.db.save_session(phone_number, 'SETTING_BANK', {'step': 'ask_details'})
            return [{"type": "text", "content": (
                "🏦 *Set Your Bank Details*\n\n"
                "This will appear on your invoices.\n\n"
                "Type it like this:\n"
                "*[Bank name] [Account number] [Account name]*\n\n"
                "Example:\n"
                "_GTBank 0123456789 Banky Fashion House_\n\n"
                "Or type *cancel* to skip."
            )}]

    def _handle_setting_bank_state(self, phone_number, text, context):
        """Process bank details input — guided step by step"""
        if text.lower().strip() in ['cancel', 'exit', 'stop', 'back']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "✅ No changes made."}]

        step = context.get('step', 'ask_bank_name')

        if step == 'ask_bank_name':
            bank_name = text.strip()
            # Check if they typed everything in one go: "GTBank 0123456789 Iyiola"
            acc_match = re.search(r'(\d{10,})', text)
            if acc_match:
                account_number = acc_match.group(1)
                before = text[:acc_match.start()].strip()
                after = text[acc_match.end():].strip()
                bank_name = before if before else "Bank"
                user = self.db.get_user(phone_number)
                account_name = after if after else (user.get('business_name', '') if user else '')
                bank_details = {'bank_name': bank_name, 'account_number': account_number, 'account_name': account_name}
                self.db.update_user_field(phone_number, 'bank_details', bank_details)
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": (
                    f"✅ *Bank Details Saved!*\n\n"
                    f"🏦 {bank_name}\n"
                    f"💳 {account_number}\n"
                    f"👤 {account_name}\n\n"
                    "This will now appear on your invoices."
                )}]
            self.db.save_session(phone_number, 'SETTING_BANK', {'step': 'ask_account', 'bank_name': bank_name})
            return [{"type": "text", "content": f"🏦 Bank: *{bank_name}*\n\n👉 *What\'s your account number?*"}]

        elif step == 'ask_account':
            acc_match = re.search(r'(\d{8,})', text)
            if not acc_match:
                return [{"type": "text", "content": "Please type your account number (8-10 digits):"}]
            account_number = acc_match.group(1)
            bank_name = context.get('bank_name', 'Bank')
            self.db.save_session(phone_number, 'SETTING_BANK', {
                'step': 'ask_name', 'bank_name': bank_name, 'account_number': account_number
            })
            return [{"type": "text", "content": (
                f"💳 Account: *{account_number}*\n\n"
                f"👉 *Account holder name?*\n"
                f"_(Type \"skip\" to use your business name)_"
            )}]

        elif step == 'ask_name':
            bank_name = context.get('bank_name', 'Bank')
            account_number = context.get('account_number', '')
            if text.lower().strip() in ('skip', 'same', 'business name'):
                user = self.db.get_user(phone_number)
                account_name = user.get('business_name', 'Account Holder') if user else 'Account Holder'
            else:
                account_name = text.strip().title()

            bank_details = {'bank_name': bank_name, 'account_number': account_number, 'account_name': account_name}
            self.db.update_user_field(phone_number, 'bank_details', bank_details)
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": (
                f"✅ *Bank Details Saved!*\n\n"
                f"🏦 {bank_name}\n"
                f"💳 {account_number}\n"
                f"👤 {account_name}\n\n"
                "💡 This appears on your invoices automatically."
            )}]

        else:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "Something went wrong. Type *set bank* to try again."}]


    def _handle_set_tax(self, phone_number, text):
        """Handle 'set tax' command — sets default or product-specific tax rate.
        
        Default: "set tax 7.5% VAT" → applies to all transactions
        Product: "set tax Nike Socks 7.5%" → applies only to Nike Socks transactions
        """
        import re as _re
        user = self.db.get_user(phone_number)
        current_tax = user.get('default_tax_percent') if user else None
        current_tax_type = user.get('default_tax_type', 'VAT') if user else 'VAT'

        # Remove command keywords
        text_lower = text.lower()
        remainder = text
        for kw in ['set tax', 'set vat', 'default tax', 'tax rate', 'vat rate']:
            if text_lower.startswith(kw):
                remainder = text[len(kw):].strip()
                break
        else:
            remainder = ""

        if not remainder:
            # No args — show current rate
            if current_tax:
                msg = (
                    f"\U0001f4b1 *Your Default Tax Rate:* {current_tax}% {current_tax_type}\n\n"
                    f"To change: *set tax [rate]% [type]*\n"
                    f"For a product: *set tax [product] [rate]%*\n\n"
                    f"\u2022 set tax 7.5% VAT\n"
                    f"\u2022 set tax Nike Socks 7.5%\n"
                    f"\u2022 set tax 0 (disable)"
                )
            else:
                msg = (
                    "\U0001f4b1 *Set Tax Rate*\n\n"
                    "\u2022 *set tax 7.5% VAT* \u2014 default for all\n"
                    "\u2022 *set tax Nike Socks 7.5%* \u2014 product-specific\n"
                    "\u2022 *set tax 0* \u2014 disable"
                )
            return [{"type": "text", "content": msg}]

        # Check if it's product-specific: "Nike Socks 7.5% VAT"
        # Pattern: [product_name] [rate]% [type]?
        product_match = _re.match(r'(.+?)\s+([\d.]+)\s*%?\s*(vat|wht|withholding)?$', remainder, _re.IGNORECASE)
        
        # Also check for plain rate: "7.5% VAT" (no product name)
        plain_match = _re.match(r'([\d.]+)\s*%?\s*(vat|wht|withholding)?$', remainder, _re.IGNORECASE)

        if plain_match and (not product_match or product_match.group(1).strip().replace('.','').isdigit()):
            # It's a default rate (no product name, just number + type)
            rate = float(plain_match.group(1))
            tax_type_raw = (plain_match.group(2) or '').upper()
            tax_type = 'WHT' if tax_type_raw in ('WHT', 'WITHHOLDING') else 'VAT'

            self.db.update_user(phone_number, {
                'default_tax_percent': str(rate),
                'default_tax_type': tax_type
            })
            return [{"type": "text", "content": (
                f"\u2705 Default tax set: *{rate}% {tax_type}*\n\n"
                f"Now '+ tax' or '+ VAT' auto-applies {rate}%.\n\n"
                f"To change: *set tax [rate]%*\n"
                f"To remove: *set tax 0*"
            )}]

        elif product_match:
            # Product-specific tax
            product_name = product_match.group(1).strip()
            rate = float(product_match.group(2))
            tax_type_raw = (product_match.group(3) or '').upper()
            tax_type = 'WHT' if tax_type_raw in ('WHT', 'WITHHOLDING') else 'VAT'

            # Find product in catalog
            catalog = user.get('auto_catalog', {}) if user else {}
            products = catalog.get('products', {})
            
            # Fuzzy match
            product_lower = product_name.lower().replace(' ', '-')
            matched_key = None
            for key, prod in products.items():
                if product_lower in key or key in product_lower:
                    matched_key = key
                    break
                if product_lower in prod.get('name', '').lower():
                    matched_key = key
                    break

            if not matched_key:
                # Create entry
                matched_key = product_lower
                products[matched_key] = {
                    'name': product_name.title(),
                    'brand': '', 'item': product_name.title(),
                    'category': '', 'sell_prices': [], 'buy_prices': [],
                    'total_sold': 0, 'total_bought': 0,
                    'customers': [], 'suppliers': [], 'last_activity': '',
                }

            # Set product tax
            products[matched_key]['tax_rate'] = rate
            products[matched_key]['tax_type'] = tax_type
            catalog['products'] = products
            self.db.update_user_field(phone_number, 'auto_catalog', catalog)

            display_name = products[matched_key]['name']
            return [{"type": "text", "content": (
                f"\u2705 *Tax set for {display_name}:* {rate}% {tax_type}\n\n"
                f"Transactions with {display_name} will auto-apply {rate}% {tax_type}.\n"
                f"To remove: *set tax {product_name} 0*"
            )}]

        # Couldn't parse
        return [{"type": "text", "content": (
            "\u2699\ufe0f *Set Tax Rate*\n\n"
            "\u2022 *set tax 7.5% VAT* \u2014 default for all\n"
            "\u2022 *set tax Nike Socks 7.5%* \u2014 for specific product\n"
            "\u2022 *set tax 0* \u2014 disable"
        )}]

    def _handle_set_unit(self, phone_number, text):
        """Handle 'set unit' command — sets unit conversions for products.
        
        Examples:
            "set unit Nike Socks: 1 carton = 12 pairs"
            "1 carton Nike Socks = 12"
            "set unit: 1 bag cement = 50kg"
            "set conversion socks: dozen = 12"
        """
        import re as _re

        # Parse: "set unit [PRODUCT]: 1 [BULK_UNIT] = [QTY] [BASE_UNIT]"
        # or: "1 [BULK_UNIT] [PRODUCT] = [QTY] [BASE_UNIT]"
        text_clean = text.lower().strip()
        
        # Remove trigger keywords
        for kw in ['set unit', 'set units', 'set conversion']:
            text_clean = text_clean.replace(kw, '').strip()
        text_clean = text_clean.strip(':').strip()

        # Pattern 1: "Nike Socks: 1 carton = 12 pairs"
        # Pattern 2: "1 carton Nike Socks = 12 pairs"
        # Pattern 3: "Nike Socks 1 carton = 12"
        
        match = _re.search(
            r'(.+?)[:\s]+1\s*(\w+)\s*=\s*(\d+)\s*(\w+)?',
            text_clean
        )
        if not match:
            # Try: "1 carton product = 12"
            match = _re.search(
                r'1\s*(\w+)\s+(.+?)\s*=\s*(\d+)\s*(\w+)?',
                text_clean
            )
            if match:
                bulk_unit = match.group(1)
                product_name = match.group(2).strip()
                qty = int(match.group(3))
                base_unit = match.group(4) or 'pieces'
            else:
                return [{"type": "text", "content": (
                    "\u2699\ufe0f *Set Unit Conversion*\n\n"
                    "Format: *set unit [product]: 1 [bulk] = [qty] [unit]*\n\n"
                    "Examples:\n"
                    "\u2022 set unit Nike Socks: 1 carton = 12 pairs\n"
                    "\u2022 set unit Cement: 1 bag = 50 kg\n"
                    "\u2022 set unit Rice: 1 bag = 25 kg\n"
                    "\u2022 1 dozen socks = 12 pairs\n"
                    "\u2022 1 crate drinks = 24 bottles"
                )}]
        else:
            product_name = match.group(1).strip()
            bulk_unit = match.group(2)
            qty = int(match.group(3))
            base_unit = match.group(4) or 'pieces'

        # Find matching product in catalog
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})

        # Fuzzy match product name
        matched_key = None
        product_lower = product_name.lower().replace(' ', '-')
        for key, prod in products.items():
            if product_lower in key or key in product_lower:
                matched_key = key
                break
            if product_lower in prod.get('name', '').lower():
                matched_key = key
                break

        if not matched_key:
            # Create a new product entry just for the unit
            matched_key = product_lower
            products[matched_key] = {
                'name': product_name.title(),
                'brand': '',
                'item': product_name.title(),
                'category': '',
                'sell_prices': [],
                'buy_prices': [],
                'total_sold': 0,
                'total_bought': 0,
                'customers': [],
                'suppliers': [],
                'last_activity': '',
            }

        # Set units on the product
        units = products[matched_key].get('units', {'base_unit': 'pieces', 'conversions': {}})
        units['base_unit'] = base_unit
        units['conversions'][bulk_unit] = qty
        products[matched_key]['units'] = units

        # Save back
        catalog['products'] = products
        self.db.update_user_field(phone_number, 'auto_catalog', catalog)

        product_display = products[matched_key]['name']
        # Show all conversions for this product
        conv_lines = []
        for unit, count in units['conversions'].items():
            conv_lines.append(f"\u2022 1 {unit} = {count} {units['base_unit']}")

        return [{"type": "text", "content": (
            f"\u2705 *Unit set for {product_display}*\n\n"
            f"Base unit: *{units['base_unit']}*\n"
            f"{'\n'.join(conv_lines)}\n\n"
            f"\U0001f4a1 Now when you record \"bought 3 {bulk_unit}s {product_display}\", "
            f"I\'ll know that\'s {qty * 3} {base_unit}."
        )}]


    def _handle_set_bank(self, phone_number, text):
        """Handle 'set bank' — save payment details for invoices"""
        user = self.db.get_user(phone_number)
        bank = user.get('bank_details', {}) if user else {}

        # Check if they included details inline: "set bank GTBank 0123456789 Iyiola Bankole"
        import re as _re
        text_clean = text.lower().strip()
        for kw in ['set bank', 'bank details', 'my bank', 'add bank', 'payment details', 'account details']:
            if text_clean.startswith(kw):
                text_clean = text[len(kw):].strip()
                break

        if text_clean and len(text_clean) > 5:
            # Try to parse: "GTBank 0123456789 Iyiola Bankole"
            # or "0123456789 GTBank Iyiola Bankole"
            parts = text_clean.strip().split()
            account_number = ''
            bank_name = ''
            account_name = ''
            
            for part in parts:
                if part.replace('-','').isdigit() and len(part) >= 8:
                    account_number = part
                elif not bank_name and not part[0:1].isdigit():
                    bank_name = part.title()
                else:
                    account_name += ' ' + part
            
            if not account_name.strip():
                account_name = user.get('business_name', '') if user else ''
            
            if account_number and bank_name:
                bank_details = {
                    'bank_name': bank_name,
                    'account_number': account_number,
                    'account_name': account_name.strip().title() or bank_name,
                }
                self.db.update_user_field(phone_number, 'bank_details', bank_details)
                return [{"type": "text", "content": (
                    f"\u2705 *Bank details saved!*\n\n"
                    f"\U0001f3e6 {bank_details['bank_name']}\n"
                    f"\U0001f4b3 {bank_details['account_number']}\n"
                    f"\U0001f464 {bank_details['account_name']}\n\n"
                    f"This will appear on your invoices automatically."
                )}]

        # No inline details — start guided flow
        self.db.save_session(phone_number, 'SETTING_BANK', {'step': 'ask_bank_name'})
        
        if bank:
            return [{"type": "text", "content": (
                f"\U0001f3e6 *Current Bank Details:*\n"
                f"Bank: {bank.get('bank_name', 'Not set')}\n"
                f"Account: {bank.get('account_number', 'Not set')}\n"
                f"Name: {bank.get('account_name', 'Not set')}\n\n"
                f"\U0001f449 Type your bank name to update (or \"cancel\"):"
            )}]
        else:
            return [{"type": "text", "content": (
                "\U0001f3e6 *Set Bank Details*\n\n"
                "This shows on your invoices so customers know where to pay.\n\n"
                "\U0001f449 *What bank do you use?*\n"
                "_(e.g. GTBank, Access, First Bank, Zenith, Opay, Moniepoint)_"
            )}]


    def _handle_change_industry(self, phone_number):
        """Let user change their industry classification"""
        user = self.db.get_user(phone_number)
        current = user.get('industry_class', 'Not set') if user else 'Not set'
        labels = {'trading': '\U0001f6cd\ufe0f Trading & Retail', 'manufacturing': '\U0001f3ed Manufacturing',
                 'services': '\U0001f4bc Services', 'hybrid': '\U0001f504 Hybrid'}
        current_label = labels.get(current, current)

        self.db.save_session(phone_number, 'STATE_CHANGE_INDUSTRY', {})
        return [{"type": "list", "content": {
            "header": "\u2699\ufe0f Change Industry",
            "body": f"Current: *{current_label}*\n\nThis affects your categories, reports, and P&L structure.\n\nChoose your industry:",
            "button_text": "\U0001f3ed Choose Industry",
            "sections": [
                {
                    "title": "Industry Types",
                    "rows": [
                        {"id": "industry_trading", "title": "\U0001f6cd\ufe0f Trading & Retail", "description": "Buy finished goods \u2192 sell to customers"},
                        {"id": "industry_manufacturing", "title": "\U0001f3ed Manufacturing", "description": "Buy raw materials \u2192 produce \u2192 sell"},
                        {"id": "industry_services", "title": "\U0001f4bc Services", "description": "Sell expertise, skills, or time"},
                        {"id": "industry_hybrid", "title": "\U0001f504 Hybrid / Mixed", "description": "Combination (sell goods + offer services)"},
                    ]
                }
            ]
        }}]


    def _handle_organize_attr_pick(self, phone_number, attr_name):
        """User picked an attribute level — add to hierarchy and ask for next"""
        session = self.db.get_session(phone_number)
        context = session.get('context', {}) if session else {}
        product_key = context.get('product_key', '')
        product_name = context.get('product_name', 'Product')
        selected = context.get('selected_attrs', [])

        # Handle custom attribute
        if attr_name == 'custom':
            self.db.save_session(phone_number, 'STATE_ORGANIZE', {
                'step': 'custom_attr',
                'product_key': product_key,
                'product_name': product_name,
                'selected_attrs': selected,
            })
            return [{"type": "text", "content": "\u270d\ufe0f Type your custom attribute name:\n_(e.g. \"flavor\", \"grade\", \"finish\")_"}]

        # Map attr IDs to display names
        attr_labels = {
            'pattern': 'Pattern/Style', 'brand': 'Brand', 'color': 'Color',
            'size': 'Size', 'material': 'Material',
        }
        attr_label = attr_labels.get(attr_name, attr_name.title())

        # Add to selected list
        selected.append(attr_name)

        # Show current hierarchy + ask to add more or finish
        hierarchy_display = ' \u2192 '.join([attr_labels.get(a, a.title()) for a in selected])

        self.db.save_session(phone_number, 'STATE_ORGANIZE', {
            'step': 'pick_attributes',
            'product_key': product_key,
            'product_name': product_name,
            'selected_attrs': selected,
        })

        # Build remaining attribute options (exclude already selected)
        all_attrs = ['pattern', 'brand', 'color', 'size', 'material']
        remaining = [a for a in all_attrs if a not in selected]

        if not remaining or len(selected) >= 5:
            # Max depth reached — auto-finish
            return self._handle_organize_done(phone_number)

        rows = []
        for attr in remaining:
            rows.append({"id": f"org_attr_{attr}", "title": f"\U0001f3f7\ufe0f {attr_labels.get(attr, attr.title())}", "description": f"Add as level {len(selected)+1}"})
        rows.append({"id": "org_attr_custom", "title": "\u270d\ufe0f Custom", "description": "Type your own attribute"})
        rows.append({"id": "org_done", "title": "\u2705 Done", "description": "Finish setup with current levels"})

        return [{"type": "list", "content": {
            "header": f"\U0001f4e6 {product_name}",
            "body": f"Current hierarchy:\n*{product_name} \u2192 {hierarchy_display}*\n\nAdd another level or tap Done.",
            "button_text": "\U0001f3f7\ufe0f Next Level",
            "sections": [{"title": "Add Level or Finish", "rows": rows}]
        }}]

    def _handle_organize_done(self, phone_number):
        """Finish organizing — save hierarchy to product"""
        session = self.db.get_session(phone_number)
        context = session.get('context', {}) if session else {}
        product_key = context.get('product_key', '')
        product_name = context.get('product_name', 'Product')
        selected = context.get('selected_attrs', [])

        if not selected:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u26a0\ufe0f No attributes selected. Try again with *organize [product]*"}]

        # Save hierarchy to product
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})

        if product_key in products:
            products[product_key]['hierarchy'] = selected
            products[product_key]['tree'] = products[product_key].get('tree', {})
            catalog['products'] = products
            self.db.update_user_field(phone_number, 'auto_catalog', catalog)

        attr_labels = {'pattern': 'Pattern/Style', 'brand': 'Brand', 'color': 'Color', 'size': 'Size', 'material': 'Material'}
        hierarchy_display = ' \u2192 '.join([attr_labels.get(a, a.title()) for a in selected])

        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": (
            f"\u2705 *{product_name} — Organized!*\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            f"\U0001f4cb Hierarchy:\n"
            f"*{product_name} \u2192 {hierarchy_display}*\n\n"
            f"Now when you record transactions with {product_name}, I\'ll auto-sort them into this tree.\n\n"
            f"\U0001f4a1 Example: _\"sold 5 blue striped Nike socks size 15\"_\n"
            f"\u2192 Sorts into: Socks > Striped > Nike > Blue > Size 15\n\n"
            f"Type *catalog* to see your organized products."
        )}]

    def _handle_tree_drilldown(self, phone_number, text_check):
        """Navigate the product tree — each tap goes one level deeper"""
        # Format: cat_tree_{product_key}__{level1}__{level2}__...
        parts = text_check.replace('cat_tree_', '').split('__')
        product_key = parts[0] if parts else ''
        path = parts[1:] if len(parts) > 1 else []
        # Remove empty strings
        path = [p for p in path if p]

        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})
        product = products.get(product_key)

        if not product:
            return [{"type": "text", "content": "\u26a0\ufe0f Product not found."}]

        hierarchy = product.get('hierarchy', [])
        tree = product.get('tree', {})
        product_name = product.get('name', 'Product')

        if not hierarchy or not tree:
            # No tree set up — show flat detail
            return self._show_product_detail(phone_number, product_key)

        # Navigate to current position in tree
        current_node = tree
        for p in path:
            if isinstance(current_node, dict) and p in current_node:
                current_node = current_node[p]
            else:
                return [{"type": "text", "content": f"\u26a0\ufe0f Could not find \'{p}\' in tree."}]

        # Determine current depth and what's at this level
        current_depth = len(path)
        current_attr = hierarchy[current_depth] if current_depth < len(hierarchy) else None

        # Build breadcrumb
        attr_labels = {'pattern': 'Pattern', 'brand': 'Brand', 'color': 'Color', 'size': 'Size', 'material': 'Material'}
        breadcrumb = product_name
        for i, p in enumerate(path):
            breadcrumb += f" > {p.title()}"

        # Get children (exclude _meta)
        children = {k: v for k, v in current_node.items() if k != '_meta'} if isinstance(current_node, dict) else {}
        meta = current_node.get('_meta', {}) if isinstance(current_node, dict) else {}

        if not children or current_depth >= len(hierarchy):
            # Leaf level — show detail with stats
            stock = meta.get('stock', 0)
            sold = meta.get('total_sold', 0)
            sell_price = meta.get('sell_price', 0)
            buy_price = meta.get('buy_price', 0)

            msg = f"\U0001f4e6 *{breadcrumb}*\n"
            msg += "\u2501" * 15 + "\n\n"
            if sell_price:
                msg += f"\U0001f4b0 Sell: *\u20a6{sell_price:,}*\n"
            if buy_price:
                msg += f"\U0001f6d2 Buy: *\u20a6{buy_price:,}*\n"
            msg += f"\U0001f4e6 Stock: *{stock}*\n"
            msg += f"\U0001f4c8 Sold: *{sold}*\n"

            # If there are size/sub entries
            if children:
                msg += f"\n\U0001f4cf *Breakdown:*\n"
                for k, v in children.items():
                    if isinstance(v, dict):
                        s = v.get('stock', 0)
                        msg += f"  \u2022 {k.title()}: {s} in stock\n"
                    else:
                        msg += f"  \u2022 {k}: {v}\n"

            msg += "\n" + "\u2501" * 15
            return [
                {"type": "text", "content": msg},
                {"type": "buttons", "content": {
                    "body": f"Actions for {breadcrumb}:",
                    "buttons": [
                        {"id": f"cat_sell_{product_key}", "title": "\U0001f4b0 Sell"},
                        {"id": f"cat_restock_{product_key}", "title": "\U0001f4e6 Restock"},
                    ]
                }}
            ]

        # Non-leaf — show list of children with totals
        attr_label = attr_labels.get(current_attr, current_attr.title() if current_attr else 'Options')
        
        # Calculate totals for this level
        total_stock = sum(
            (v.get('_meta', {}).get('stock', 0) if isinstance(v, dict) else 0)
            for k, v in children.items()
        )
        total_sold = sum(
            (v.get('_meta', {}).get('total_sold', 0) if isinstance(v, dict) else 0)
            for k, v in children.items()
        )

        rows = []
        for key, child in list(children.items())[:10]:
            child_meta = child.get('_meta', {}) if isinstance(child, dict) else {}
            child_stock = child_meta.get('stock', 0)
            child_sold = child_meta.get('total_sold', 0)
            # Count sub-children
            sub_count = len([k for k in child.keys() if k != '_meta']) if isinstance(child, dict) else 0

            desc_parts = []
            if child_stock:
                desc_parts.append(f"Stock: {child_stock}")
            if child_sold:
                desc_parts.append(f"Sold: {child_sold}")
            if sub_count:
                desc_parts.append(f"{sub_count} types")
            description = " \u2022 ".join(desc_parts) if desc_parts else "Tap for details"

            # Build drill-down ID
            new_path = '__'.join(path + [key])
            row_id = f"cat_tree_{product_key}__{new_path}"

            rows.append({
                "id": row_id[:200],  # WhatsApp ID limit
                "title": key.title()[:24],
                "description": description[:72]
            })

        header = f"\U0001f4e6 {breadcrumb}"
        body = f"Choose {attr_label}:\n\n"
        if total_stock:
            body += f"\U0001f4e6 Total stock at this level: *{total_stock}*\n"
        if total_sold:
            body += f"\U0001f4c8 Total sold: *{total_sold}*"

        # Add management buttons after the list
        path_str = '__'.join(path) if path else ''
        responses = [{"type": "list", "content": {
            "header": header[:60],
            "body": body,
            "button_text": f"\U0001f4cb {attr_label}"[:20],
            "sections": [{"title": attr_label, "rows": rows}]
        }}]
        
        # Management buttons
        add_id = f"cat_add_{product_key}__{path_str}" if path_str else f"cat_add_{product_key}__"
        edit_id = f"cat_editlvl_{product_key}__{path_str}" if path_str else f"cat_editlvl_{product_key}__"
        responses.append({"type": "buttons", "content": {
            "body": f"Manage {attr_label}:",
            "buttons": [
                {"id": add_id[:200], "title": f"\u2795 Add {attr_label}"[:24]},
                {"id": edit_id[:200], "title": "\U0001f5d1 Remove Item"},
                {"id": f"cat_sell_{product_key}", "title": "\U0001f4b0 Sell"},
            ]
        }})
        return responses

    def _handle_organize_product(self, phone_number, text):
        """Start the product hierarchy setup — define attribute levels for a product"""
        import re as _re
        # Extract product name from command: "organize socks" or "organize product socks"
        text_clean = text.lower().strip()
        for kw in ['organize product', 'organize', 'set attributes', 'product attributes', 'setup product']:
            if text_clean.startswith(kw):
                text_clean = text[len(kw):].strip()
                break

        if text_clean and len(text_clean) >= 2:
            product_name = text_clean.title()
        else:
            # Ask for product name
            self.db.save_session(phone_number, 'STATE_ORGANIZE', {'step': 'ask_product'})
            return [{"type": "text", "content": (
                "\U0001f4e6 *Organize a Product*\n\n"
                "Which product do you want to organize with attributes?\n\n"
                "_Example: Socks, Caps, Slides, Bags_"
            )}]

        # Find or create product in catalog
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})

        # Find matching product
        product_key = product_name.lower().replace(' ', '-')
        matched_key = None
        for key in products:
            if product_key in key or key in product_key:
                matched_key = key
                break
        
        if not matched_key:
            # Create new product
            matched_key = product_key
            products[matched_key] = {
                'name': product_name, 'brand': '', 'item': product_name,
                'category': '', 'sell_prices': [], 'buy_prices': [],
                'total_sold': 0, 'total_bought': 0,
                'customers': [], 'suppliers': [], 'last_activity': '',
                'variants': {},
            }
            catalog['products'] = products
            self.db.update_user_field(phone_number, 'auto_catalog', catalog)

        # Show attribute selection
        self.db.save_session(phone_number, 'STATE_ORGANIZE', {
            'step': 'pick_attributes',
            'product_key': matched_key,
            'product_name': products[matched_key].get('name', product_name),
            'selected_attrs': [],
        })

        return [{"type": "list", "content": {
            "header": f"\U0001f4e6 Organize: {products[matched_key].get('name', product_name)}",
            "body": "Pick the FIRST level of your product tree.\n\nExample: If you sell Socks in different patterns first (Striped, Solid), pick Pattern.\n\nYou can add more levels after.",
            "button_text": "\U0001f3f7\ufe0f Pick Attribute",
            "sections": [{
                "title": "Attribute Levels",
                "rows": [
                    {"id": "org_attr_pattern", "title": "\U0001f3a8 Pattern/Style", "description": "Striped, Solid, Sport, Ankara..."},
                    {"id": "org_attr_brand", "title": "\U0001f3f7\ufe0f Brand", "description": "Nike, Gucci, Polo, Adidas..."},
                    {"id": "org_attr_color", "title": "\U0001f308 Color", "description": "Blue, Red, Black, White..."},
                    {"id": "org_attr_size", "title": "\U0001f4cf Size", "description": "14, 15, S, M, L, XL..."},
                    {"id": "org_attr_material", "title": "\U0001f9f5 Material", "description": "Cotton, Leather, Nylon..."},
                    {"id": "org_attr_custom", "title": "\u270d\ufe0f Custom", "description": "Type your own attribute name"},
                ]
            }]
        }}]

    def _handle_set_recipe_start(self, phone_number, text):
        """Start the recipe/BOM setup flow"""
        import re as _re
        
        # Check if product name was included: "set recipe bread"
        text_clean = text.lower().strip()
        for kw in ['set recipe', 'add recipe', 'new recipe', 'create recipe', 'define recipe', 'bom', 'bill of materials']:
            if text_clean.startswith(kw):
                remainder = text[len(kw):].strip().strip(':').strip()
                break
        else:
            remainder = ""

        if remainder and len(remainder) >= 2:
            # Product name provided inline — skip name step
            self.db.save_session(phone_number, 'STATE_RECIPE_MATERIALS', {
                'product_name': remainder.title(),
                'materials': []
            })
            return [{"type": "text", "content": (
                f"\U0001f4cb *Recipe for: {remainder.title()}*\n\n"
                f"\U0001f449 List the raw materials for ONE BATCH.\n"
                f"Include quantity and unit for each.\n\n"
                f"_Example:_\n"
                f"1 bag flour\n"
                f"2kg sugar\n"
                f"1 litre oil\n\n"
                f"\U0001f4a1 Type all materials (one per line or comma-separated)"
            )}]
        else:
            # Ask for product name
            self.db.save_session(phone_number, 'STATE_RECIPE_NAME', {})
            return [{"type": "text", "content": (
                "\U0001f3ed *Set Up Recipe / Bill of Materials*\n\n"
                "This defines the raw materials and costs needed\n"
                "to produce your product.\n\n"
                "\U0001f449 *What product are you making?*\n"
                "_(e.g. Bread, Furniture, Cake, Shoes, Soap)_"
            )}]

    def _handle_production_run(self, phone_number, text):
        """Record a production run — uses recipe to calculate costs and update inventory"""
        import re as _re
        
        # Parse: "produced 200 loaves" or "made 50 chairs" or "baked 100 bread"
        text_lower = text.lower().strip()
        for prefix in ['produced', 'manufactured', 'made', 'baked', 'production', 'production run']:
            if text_lower.startswith(prefix):
                text_lower = text_lower[len(prefix):].strip()
                break

        # Extract quantity and product
        match = _re.match(r'(\d+)\s*(.+)', text_lower)
        if not match:
            return [{"type": "text", "content": (
                "\U0001f3ed *Record Production*\n\n"
                "Type how many units you produced:\n"
                "\u2022 _produced 200 loaves_\n"
                "\u2022 _made 50 chairs_\n"
                "\u2022 _baked 100 cakes_"
            )}]

        quantity = int(match.group(1))
        product_hint = match.group(2).strip()

        # Find matching recipe in catalog
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})

        # Fuzzy match
        matched_key = None
        for key, prod in products.items():
            if not prod.get('recipe'):
                continue
            if product_hint in key or key in product_hint:
                matched_key = key
                break
            if product_hint in prod.get('name', '').lower():
                matched_key = key
                break
            # Check batch_unit
            batch_unit = prod.get('recipe', {}).get('batch_unit', '')
            if batch_unit and product_hint.startswith(batch_unit):
                matched_key = key
                break

        if not matched_key:
            # No recipe found
            recipe_names = [p['name'] for p in products.values() if p.get('recipe')]
            if recipe_names:
                return [{"type": "text", "content": (
                    f"\u26a0\ufe0f No recipe found for \"{product_hint}\".\n\n"
                    f"Your recipes: {', '.join(recipe_names)}\n\n"
                    f"_Try: \"produced {quantity} {recipe_names[0].lower()}\"_\n"
                    f"Or: *set recipe {product_hint}* to create one."
                )}]
            else:
                return [{"type": "text", "content": (
                    "\u26a0\ufe0f No recipes set up yet!\n\n"
                    f"Type *set recipe {product_hint}* to define the raw materials\n"
                    "and costs needed to produce it."
                )}]

        product = products[matched_key]
        recipe = product['recipe']
        batch_size = recipe['batch_size']
        cost_per_unit = recipe['cost_per_unit']
        batch_unit = recipe.get('batch_unit', 'units')

        # Calculate production cost
        batches_needed = quantity / batch_size
        total_cost = round(cost_per_unit * quantity)
        
        # Materials used
        materials_used = []
        for m in recipe.get('materials', []):
            used_qty = round(m['qty'] * batches_needed, 2)
            materials_used.append(f"  \u2022 {used_qty} {m['unit']} {m['name']}")

        # Update inventory
        inventory = product.get('inventory', {'finished_goods': 0, 'last_production': ''})
        inventory['finished_goods'] = inventory.get('finished_goods', 0) + quantity
        from datetime import datetime
        inventory['last_production'] = datetime.now().strftime('%Y-%m-%d')
        product['inventory'] = inventory
        products[matched_key] = product
        catalog['products'] = products
        self.db.update_user_field(phone_number, 'auto_catalog', catalog)

        # Also record as a transaction (expense - production cost)
        from datetime import datetime
        tx_data = {
            'type': 'expense',
            'amount': total_cost,
            'description': f'Production: {quantity} {batch_unit} of {product["name"]}',
            'category': 'Production & Manufacturing',
            'sub_category': 'Production run',
            'item_name': product['name'],
            'quantity': str(quantity),
            'unit_cost': str(cost_per_unit),
            'date': datetime.now().strftime('%Y-%m-%d'),
        }
        self.db.save_transaction(phone_number, tx_data)

        msg = (
            f"\u2705 *Production Recorded!*\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            f"\U0001f4e6 *{quantity} {batch_unit}* of *{product['name']}*\n"
            f"\U0001f4b0 Production Cost: *\u20a6{total_cost:,}*\n"
            f"\U0001f4ca Cost per unit: \u20a6{cost_per_unit:,}\n\n"
            f"\U0001f9f1 *Materials Used:*\n{'\n'.join(materials_used)}\n"
        )
        if recipe.get('labour_cost'):
            labour_used = round(recipe['labour_cost'] * batches_needed)
            msg += f"  \U0001f477 Labour: \u20a6{labour_used:,}\n"
        if recipe.get('overhead_cost'):
            overhead_used = round(recipe['overhead_cost'] * batches_needed)
            msg += f"  \u26a1 Overhead: \u20a6{overhead_used:,}\n"

        msg += (
            f"\n\U0001f4e6 *Inventory: {inventory['finished_goods']} {batch_unit}* in stock\n\n"
            f"\U0001f4a1 When you sell, I\'ll auto-calculate your profit per unit!"
        )

        return [{"type": "text", "content": msg}]

    def _show_recipes(self, phone_number):
        """Display all recipes/BOMs for the user"""
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})

        recipes = {k: v for k, v in products.items() if v.get('recipe')}

        if not recipes:
            return [{"type": "text", "content": (
                "\U0001f3ed *No recipes yet!*\n\n"
                "Set up a recipe to track your production costs:\n\n"
                "*set recipe [product name]*\n\n"
                "_Example: set recipe Bread_\n"
                "_Example: set recipe Chair_"
            )}]

        msg = "\U0001f3ed *Your Recipes / Bill of Materials*\n"
        msg += "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"

        for key, prod in recipes.items():
            recipe = prod['recipe']
            inventory = prod.get('inventory', {})
            name = prod['name']
            batch_size = recipe['batch_size']
            batch_unit = recipe.get('batch_unit', 'units')
            cost_per_unit = recipe['cost_per_unit']
            total_batch = recipe['total_batch_cost']
            stock = inventory.get('finished_goods', 0)

            msg += f"\U0001f4cb *{name}*\n"
            msg += f"   Batch: {batch_size} {batch_unit} @ \u20a6{total_batch:,}\n"
            msg += f"   Cost/unit: *\u20a6{cost_per_unit:,}*\n"

            # Materials summary
            materials = recipe.get('materials', [])
            mat_names = [f"{m['qty']}{m['unit']} {m['name']}" for m in materials[:4]]
            msg += f"   Materials: {', '.join(mat_names)}\n"

            if recipe.get('labour_cost'):
                msg += f"   Labour: \u20a6{recipe['labour_cost']:,}\n"

            if stock > 0:
                msg += f"   \U0001f4e6 In stock: *{stock} {batch_unit}*\n"

            # Show sell price and margin if available
            sell_prices = prod.get('sell_prices', [])
            if sell_prices:
                avg_sell = sum(sell_prices) // len(sell_prices)
                margin = round((avg_sell - cost_per_unit) / avg_sell * 100)
                msg += f"   \U0001f4b0 Avg sell: \u20a6{avg_sell:,} (margin: {margin}%)\n"

            msg += "\n"

        msg += (
            "\U0001f4a1 _Commands:_\n"
            "\u2022 *set recipe [product]* \u2014 new recipe\n"
            "\u2022 *produced [qty] [product]* \u2014 record production\n"
        )

        return [{"type": "text", "content": msg}]


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

        # ---- TIME FILTER BUTTON HANDLING ----
        # After viewing a report, user can tap Today/Week/Month filter buttons
        if text_lower.startswith('filter_'):
            # Parse: filter_my_sales_today, filter_my_purchases_week, etc.
            parts = text_lower.split('_', 2)  # ['filter', 'my', 'sales_today']
            if len(parts) >= 2:
                rest = text_lower[7:]  # Remove "filter_"
                # Find the filter type and time period
                for ft in ('my_sales', 'my_purchases', 'my_expenses'):
                    if rest.startswith(ft + '_'):
                        period = rest[len(ft)+1:]  # "today", "week", "month"
                        time_text = period  # "today", "week", "month"
                        return self._handle_filtered_report(phone_number, ft, time_text=time_text)
            # Fallback
            return self._handle_idle(phone_number, 'help')

        # ---- POST-SALE BUTTON HANDLING ----
        # After saving a sale, user can tap Invoice/Receipt/Done
        if text_lower == 'post_invoice':
            session = self.db.get_session(phone_number)
            ctx = session.get('context', {}) if session else {}
            tx_id = ctx.get('last_saved_tx_id', '')
            vendor = ctx.get('last_saved_vendor', 'Customer')
            amount = ctx.get('last_saved_amount', 0)
            if tx_id:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "invoice_from_transactions", "content": {"transaction_ids": [tx_id]}}]
            else:
                # Fallback to manual invoice flow
                return self._handle_idle(phone_number, 'invoice')
        elif text_lower == 'post_receipt':
            session = self.db.get_session(phone_number)
            ctx = session.get('context', {}) if session else {}
            tx_id = ctx.get('last_saved_tx_id', '')
            if tx_id:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "receipt_generate", "content": {"mode": "specific", "transaction_ids": [tx_id]}}]
            else:
                return [{"type": "receipt_generate", "content": {"mode": "last"}}]
        elif text_lower == 'post_done':
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\U0001f44d Ready for the next one! Record another transaction or type *menu*."}]

        # ---- INTERACTIVE MENU HANDLING ----
        # If user tapped a list menu item, map the ID to a command
        if text_lower in MENU_ID_MAP:
            mapped = MENU_ID_MAP[text_lower]
            # Special prompts that need custom responses
            if mapped == "__PROMPT_RECORD_SALE__":
                return [{"type": "list", "content": {
                    "header": "\U0001f4b0 Record Sale",
                    "body": "How was this sale paid?",
                    "button_text": "\U0001f4b0 Choose",
                    "sections": [
                        {
                            "title": "Payment Type",
                            "rows": [
                                {"id": "guided_cash_sale", "title": "\U0001f4b5 Cash Sale", "description": "Customer paid immediately"},
                                {"id": "guided_credit_sale", "title": "\U0001f4b3 Credit Sale", "description": "Customer owes you"},
                                {"id": "guided_type_sale", "title": "\u270d\ufe0f Type It Myself", "description": "Type full transaction in one go"},
                            ]
                        }
                    ]
                }}]
            elif mapped == "__PROMPT_RECORD_EXPENSE__":
                return [{"type": "list", "content": {
                    "header": "\U0001f4b8 Record Expense",
                    "body": "Record a business expense:",
                    "button_text": "\U0001f4b8 Choose",
                    "sections": [
                        {
                            "title": "How to Record",
                            "rows": [
                                {"id": "guided_cash_expense", "title": "\U0001f4b5 Guided Entry", "description": "Step by step \u2014 easy"},
                                {"id": "guided_type_expense", "title": "\u270d\ufe0f Type It Myself", "description": "Type full expense in one go"},
                            ]
                        }
                    ]
                }}]
            elif mapped == "__PROMPT_RECORD_PURCHASE__":
                return [{"type": "list", "content": {
                    "header": "\U0001f4e6 Record Purchase",
                    "body": "How was this purchase paid?",
                    "button_text": "\U0001f4e6 Choose",
                    "sections": [
                        {
                            "title": "Payment Type",
                            "rows": [
                                {"id": "guided_cash_purchase", "title": "\U0001f4b5 Cash Purchase", "description": "Paid immediately"},
                                {"id": "guided_credit_purchase", "title": "\U0001f4b3 On Credit", "description": "You owe the supplier"},
                                {"id": "guided_type_purchase", "title": "\u270d\ufe0f Type It Myself", "description": "Type full transaction in one go"},
                            ]
                        }
                    ]
                }}]
            elif mapped == "__PROMPT_RECORD_PAYMENT__":
                return [{"type": "text", "content": (
                    "\U0001f4b3 *Record a Payment*\n\n"
                    "Someone paid what they owe you:\n"
                    "\u2022 _Sandra paid me 50K_\n"
                    "\u2022 _received 100K from Ahmed_\n\n"
                    "Or you paid what you owe:\n"
                    "\u2022 _paid Dangote 200K_\n"
                    "\u2022 _I paid Alhaji 50,000_\n\n"
                    "\U0001f4a1 This reduces the outstanding debt automatically."
                )}]
            elif mapped == "__PROMPT_SET_RECIPE__":
                return self._handle_set_recipe_start(phone_number, "set recipe")
            elif mapped == "__PROMPT_PRODUCTION__":
                return [{"type": "text", "content": (
                    "\U0001f3ed *Record Production*\n\n"
                    "Type how many units you produced:\n"
                    "\u2022 _produced 200 loaves_\n"
                    "\u2022 _made 50 chairs_\n"
                    "\u2022 _manufactured 100 bars of soap_\n\n"
                    "\U0001f4a1 I\'ll auto-calculate the cost using your recipe."
                )}]
            elif mapped == "__PROMPT_RECORD_SERVICE__":
                return [{"type": "text", "content": (
                    "\U0001f4bc *Record Service Income*\n\n"
                    "Type the service you delivered:\n"
                    "\u2022 _received 200K from GTBank for consulting_\n"
                    "\u2022 _charged Bola 50K for phone repair_\n"
                    "\u2022 _design work for Femi 80K_\n\n"
                    "\U0001f4a1 Include client name and amount for best tracking."
                )}]
            elif mapped.startswith("__GUIDED_START_"):
                # Start guided flow: cash_sale, credit_sale, cash_purchase, etc.
                flow_type = mapped.replace("__GUIDED_START_", "").rstrip("_")
                return self._start_guided_flow(phone_number, flow_type)
            elif mapped == "__PROMPT_TYPE_SALE__":
                return [{"type": "text", "content": (
                    "\U0001f4b0 *Type your sale:*\n\n"
                    "_Example: sold 10 Nike shoes to Mama Tolu for 150K_\n\n"
                    "Or just: _shoes 15000_"
                )}]
            elif mapped == "__PROMPT_TYPE_PURCHASE__":
                return [{"type": "text", "content": (
                    "\U0001f4e6 *Type your purchase:*\n\n"
                    "_Example: bought 50 Nike socks from Alhaji 400K_"
                )}]
            elif mapped == "__PROMPT_TYPE_EXPENSE__":
                return [{"type": "text", "content": (
                    "\U0001f4b8 *Type your expense:*\n\n"
                    "_Example: paid rent 150K_ or _diesel 50000_"
                )}]
            elif mapped == "__DEBTS_MENU__":
                return self._show_debts_menu()
            elif mapped == "__CONTACTS_MENU__":
                return self._show_contacts_menu()
            elif mapped == "__SHOW_CUSTOMERS__":
                return self._show_contacts_filtered(phone_number, 'customer')
            elif mapped == "__SHOW_SUPPLIERS__":
                return self._show_contacts_filtered(phone_number, 'supplier')
            elif mapped == "__DOCUMENTS_MENU__":
                return self._show_documents_menu()
            elif mapped == "__REPORTS_MENU__":
                return self._show_reports_menu(phone_number)
            elif mapped.startswith("__RPT_CHOOSE_PERIOD_"):
                # User chose report type, now show period buttons
                rpt_type = mapped.replace("__RPT_CHOOSE_PERIOD_", "").rstrip("_")
                return self._show_period_chooser(rpt_type)
            elif mapped.startswith("__RPT_CUSTOM_"):
                # User wants to type a custom period — save report type, ask for input
                rpt_type = mapped.replace("__RPT_CUSTOM_", "").rstrip("_")
                self.db.save_session(phone_number, 'STATE_REPORT_CUSTOM', {'report_type': f'my_{rpt_type}'})
                return [{"type": "text", "content": "✍️ Type your filter — e.g.:\n\n• _last 5 days_\n• _June_\n• _Monday_\n• _sales to Sandra_\n• _Nike purchases this week_\n• _over 50K_"}]
            elif mapped.startswith("__RPT_EXEC_"):
                # User chose type + period — execute the report
                parts = mapped.replace("__RPT_EXEC_", "").rstrip("_")
                # Format: "my_sales_today" or "my_purchases_week"
                for ft in ('my_sales', 'my_purchases', 'my_expenses'):
                    if parts.startswith(ft + '_'):
                        period = parts[len(ft)+1:]
                        return self._handle_filtered_report(phone_number, ft, time_text=period)
                return self._handle_filtered_report(phone_number, 'my_sales', time_text='month')
            elif mapped == "__CATALOG_MENU__":
                return self._show_catalog_menu(phone_number)
            elif mapped == "__CATALOG_BY_BRAND__":
                return self._show_catalog_by_brand(phone_number)
            elif mapped == "__CATALOG_TOP_SELLERS__":
                return self._show_top_sellers(phone_number)
            else:
                # Re-process as if user typed the mapped command text
                return self._handle_idle(phone_number, mapped)

        # Check for commands
        command = self._detect_command(text_lower)

        if command == 'greeting':
            return self._handle_greeting(phone_number)

        elif command == 'help':
            return self._show_help()

        elif command == 'report' or command == 'today' or command == 'week' or command == 'month':
            return self._handle_report(phone_number, command)

        elif command in ('my_sales', 'my_purchases', 'my_expenses'):
            return self._handle_filtered_report(phone_number, command, time_text=text)

        elif command == 'export':
            # Check if user just viewed a filtered report — offer to export that
            session = self.db.get_session(phone_number)
            ctx = session.get('context', {}) if session else {}
            last_filter = ctx.get('last_filter_type')
            last_period = ctx.get('last_filter_period', '')

            if last_filter and last_period:
                filter_label = {'my_sales': 'Sales', 'my_purchases': 'Purchases', 'my_expenses': 'Expenses'}.get(last_filter, 'Data')
                self.db.save_session(phone_number, STATE_EXPORTING, {
                    'filtered_export': True,
                    'filter_type': last_filter,
                    'filter_start': ctx.get('last_filter_start', ''),
                    'filter_end': ctx.get('last_filter_end', ''),
                    'filter_period': last_period,
                })
                return [{"type": "buttons", "content": {
                    "body": f"\U0001f4ca Export *{filter_label} \u2014 {last_period}*\n\nChoose format:",
                    "buttons": [
                        {"id": "export_filtered_excel", "title": "\U0001f4ca Excel"},
                        {"id": "export_filtered_pdf", "title": "\U0001f4c4 PDF"},
                        {"id": "export_month", "title": "Full Month (Excel)"},
                    ]
                }}]
            else:
                self.db.save_session(phone_number, STATE_EXPORTING, {})
                return [{"type": "buttons", "content": {
                    "body": "\U0001f4ca What would you like to export?",
                    "buttons": [
                        {"id": "export_month", "title": "This Month (Excel)"},
                        {"id": "export_csv", "title": "Full History (CSV)"},
                        {"id": "export_contacts", "title": "Contacts List"},
                    ]
                }}]

        elif command == 'invoice':
            # Check if user typed "invoice #1,3,5" or "invoice 1,6,7" inline
            remaining = re.sub(r'^invoice\s*', '', text.lower().strip())
            inline_match = re.match(r'^#?([\d][\d,\s]*[\d]?)$', remaining.strip())
            if inline_match:
                # Direct multi-transaction invoice
                nums_text = inline_match.group(1)
                user = self.db.get_user(phone_number)
                tx_list = user.get('last_sales_list', user.get('last_tx_list', [])) if user else []
                if tx_list:
                    numbers = [int(n.strip()) for n in nums_text.split(',') if n.strip().isdigit()]
                    tx_ids = [tx_list[n-1] for n in numbers if 1 <= n <= len(tx_list)]
                    if tx_ids:
                        self.db.save_session(phone_number, STATE_IDLE, {})
                        return [{"type": "invoice_from_transactions", "content": {"transaction_ids": tx_ids}}]
            self.db.save_session(phone_number, STATE_INVOICING, {"step": "ask_details"})
            return [{"type": "text", "content": (
                "📄 Let's create an invoice.\n\n"
                "*Option 1* — Type details:\n"
                "*[Customer name] [amount] for [item/description]*\n"
                "_Example: Sandra 100,000 for 10 pairs Nike socks_\n\n"
                "*Option 2* — From your transactions:\n"
                "Type *#1,3,5* (numbers from \"my sales\" list)\n\n"
                "Or type *cancel* to exit."
            )}]

        elif command == 'statement':
            # Handled by main.py → pdf_generator
            return [{"type": "text", "content": "__STATEMENT_REQUEST__"}]

        elif command == 'receipt':
            # Check if user typed "receipt #3" or "receipt 1,6,7" inline
            remaining = re.sub(r'^receipt\s*', '', text.lower().strip())
            inline_match = re.match(r'^#?([\d][\d,\s]*[\d]?)$', remaining.strip())
            if inline_match and inline_match.group(1).strip():
                nums_text = inline_match.group(1).strip()
                user = self.db.get_user(phone_number)
                tx_list = user.get('last_tx_list', []) if user else []
                if tx_list:
                    numbers = [int(n.strip()) for n in nums_text.split(',') if n.strip().isdigit()]
                    tx_ids = [tx_list[n-1] for n in numbers if 1 <= n <= len(tx_list)]
                    if tx_ids:
                        self.db.save_session(phone_number, STATE_IDLE, {})
                        return [{"type": "receipt_generate", "content": {"mode": "specific", "transaction_ids": tx_ids}}]
            self.db.save_session(phone_number, 'GENERATING_RECEIPT', {"step": "ask_which"})
            return [{"type": "text", "content": (
                "🧾 Generate a receipt.\n\n"
                "• Type *last* — receipt for your last transaction\n"
                "• Type *#3* — receipt for transaction #3\n"
                "• Type *#1,3,5* — combined receipt for multiple\n\n"
                "_Run \"my sales\" first to see numbered list._\n\n"
                "Or type *cancel* to exit."
            )}]

        elif command == 'customers':
            return self._show_contacts(phone_number, "customer")

        elif command == 'who_owes_me':
            return self._handle_who_owes_me(phone_number)
        elif command == 'who_i_owe':
            return self._handle_i_owe(phone_number)
        elif command == 'contacts':
            return self._show_contacts_menu()

        elif command == 'i_owe':
            return self._handle_i_owe(phone_number)

        elif command == 'debt_summary':
            return self._handle_debt_summary(phone_number)

        elif command == 'record_debt':
            # If text has a number (amount), it's a full transaction — use AI parser
            # which has its own credit detection with rich confirmation
            import re as _re
            if _re.search(r'\d{3,}|\d+[kKmM]', text):
                return self._handle_transaction(phone_number, text)
            return self._handle_record_debt(phone_number, text)

        elif command == 'record_i_owe':
            # Same: if it has an amount, use the full AI parser
            import re as _re
            if _re.search(r'\d{3,}|\d+[kKmM]', text):
                return self._handle_transaction(phone_number, text)
            return self._handle_record_i_owe(phone_number, text)

        elif command == 'debt_paid':
            return self._handle_debt_paid(phone_number, text)

        elif command == 'i_paid_debt':
            return self._handle_i_paid_debt(phone_number, text)

        elif command == 'remind_debtor':
            return self._handle_remind_debtor(phone_number, text)

        elif command == 'remind_all_debtors':
            return self._handle_remind_all_debtors(phone_number)

        elif command == 'save_contact_phone':
            return self._handle_save_contact_phone(phone_number, text)

        elif command == 'top_customers':
            return self._handle_top_contacts(phone_number, 'customer')

        elif command == 'top_suppliers':
            return self._handle_top_contacts(phone_number, 'supplier')

        elif command == 'inactive_contacts':
            return self._handle_inactive_contacts(phone_number)

        elif command == 'set_credit_terms':
            return self._handle_set_credit_terms(phone_number, text)

        elif command == 'contact_catalog':
            return self._handle_contact_catalog(phone_number)

        elif command == 'contact_profile':
            return self._handle_contact_profile(phone_number, text)

        elif command == 'add_note':
            return self._handle_add_note(phone_number, text)

        elif command == 'suppliers':
            return self._show_contacts(phone_number, "supplier")

        elif command == 'undo':
            return self._handle_undo(phone_number)

        elif command == 'redo':
            return self._handle_redo(phone_number)

        elif command == 'upgrade':
            return self._show_upgrade_options()
        elif command == 'change_category':
            return self._handle_change_category(phone_number, text)
        elif command == 'set_bank':
            return self._handle_set_bank(phone_number, text)
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

        elif command == 'set_tax':
            return self._handle_set_tax(phone_number, text)
        elif command == 'set_unit':
            return self._handle_set_unit(phone_number, text)
        elif command == 'set_bank':
            return self._handle_set_bank(phone_number, text)
        elif command == 'change_industry':
            return self._handle_change_industry(phone_number)
        elif command == 'set_recipe':
            return self._handle_set_recipe_start(phone_number, text)
        elif command == 'organize_product':
            return self._handle_organize_product(phone_number, text)
        elif command == 'production':
            return self._handle_production_run(phone_number, text)
        elif command == 'my_recipes':
            return self._show_recipes(phone_number)

        elif command == 'compliment':
            return self._handle_emotion(phone_number, 'compliment')

        elif command == 'sad':
            return self._handle_emotion(phone_number, 'sad')

        elif command == 'excited':
            return self._handle_emotion(phone_number, 'excited')

        elif command == 'pidgin_chat':
            return self._handle_pidgin(phone_number, text_lower)


        # ---- SMART DEBTOR DETECTION ----
        # If user types "[Name] [amount]" and that name is a known debtor,
        # treat it as a debt payment (common Nigerian pattern)
        # e.g. "Mrs Omolabake 35000" → Mrs Omolabake paid me 35000
        name_amount_match = re.match(
            r'^([A-Za-z][A-Za-z\s\.]+?)\s+([\d,]+[kKmM]?)\s*$', text.strip()
        )
        if name_amount_match:
            potential_name = name_amount_match.group(1).strip()
            potential_amt_str = name_amount_match.group(2).replace(',', '')
            # Parse amount
            if potential_amt_str.lower().endswith('k'):
                potential_amt = int(potential_amt_str[:-1]) * 1000
            elif potential_amt_str.lower().endswith('m'):
                potential_amt = int(potential_amt_str[:-1]) * 1000000
            else:
                potential_amt = int(potential_amt_str)
            # Check if this name matches a known debtor
            debtors = self.db.get_all_debtors(phone_number)
            for debtor in debtors:
                debtor_name = debtor.get('name', '').lower()
                if (potential_name.lower() in debtor_name or
                    debtor_name in potential_name.lower()):
                    # Match! Route to debt payment
                    return self._handle_debt_paid(phone_number, f"{potential_name} paid me {potential_amt}")

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
            # No numbers, no transaction verbs — check if it's actually
            # an emotion/compliment, or just an unrecognized command
            emotion_cmd = self._detect_command(text_lower)
            if emotion_cmd in ['compliment', 'sad', 'excited']:
                return self._handle_emotion(phone_number, emotion_cmd)

            # Check if text contains known emotion keywords before defaulting
            compliment_words = ['thanks', 'thank you', 'well done', 'good job', 'nice', 'great', 'perfect', 'love it', 'fire', 'sharp']
            if any(w in text_lower for w in compliment_words):
                return self._handle_emotion(phone_number, 'compliment')

            # Short acknowledgements — just prompt for next action
            ack_words = ['okay', 'ok', 'alright', 'sure', 'cool', 'noted', 'fine', 'got it', 'understood', 'right', 'yep', 'yea', 'yeah', 'yes']
            if text_lower.strip().rstrip('!.') in ack_words:
                return [{"type": "text", "content": "👍 Ready when you are! Record a transaction or type *help* to see commands."}]

            # Truly unrecognized — give helpful response
            return [{"type": "text", "content": (
                "🤔 I'm not sure what to do with that.\n\n"
                "Try recording a transaction (e.g. _sold shoes 15K_) or type *help* for all commands."
            )}]

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

        # ---- Debt payment detection (existing debtor + "paid") ----
        if 'paid' in text_lower or 'pay' in text_lower:
            debtors = self.db.get_all_debtors(phone_number)
            for d in debtors:
                if d['name'].lower() in text_lower:
                    return self._handle_debt_paid(phone_number, text)
            creditors = self.db.get_all_creditors(phone_number)
            for c in creditors:
                if c['name'].lower() in text_lower and ('i paid' in text_lower or text_lower.startswith('paid')):
                    return self._handle_i_paid_debt(phone_number, text)

        # ---- Credit sale/purchase detection BEFORE AI categorizer ----
        # If the text has RICH DETAIL (numbers, quantities, product names, etc.),
        # skip this simple detection and let the AI parser handle it properly.
        # The AI parser has its own credit detection that preserves all context.
        has_credit_phrase = 'on credit' in text_lower or 'credit' in text_lower
        text_has_detail = (
            bool(re.search(r'\d', text))  # has numbers (amount or quantity)
            or len(text.split()) >= 6       # long sentence with context
        )
        # Only use simple credit routing for SHORT commands like "on credit" or "gave credit to Bola"
        if text_has_detail and has_credit_phrase:
            has_credit_phrase = False  # Skip — let AI parser handle it with full context
        explicit_purchase_signals = [
            'i owe', 'i am owing', 'i bought on credit', 'credit purchase',
            'took on credit from', 'i dey owe', 'collected from on credit',
            'bought', 'purchased', 'i took from',
        ]
        explicit_sale_signals = [
            'took goods', 'took items', 'collected goods', 'owes me',
            'owing me', 'credit sale', 'gave credit', 'sold on credit',
            'sold', 'i gave', 'i sold',
        ]

        if has_credit_phrase:
            has_purchase_direction = any(sig in text_lower for sig in explicit_purchase_signals)
            has_sale_direction = any(sig in text_lower for sig in explicit_sale_signals)

            if has_purchase_direction and not has_sale_direction:
                return self._handle_record_i_owe(phone_number, text)
            elif has_sale_direction and not has_purchase_direction:
                return self._handle_record_debt(phone_number, text)
            elif has_purchase_direction and has_sale_direction:
                # Both detected — trust the verb closer to the start of the sentence
                bought_idx = text_lower.find('bought') if 'bought' in text_lower else 999
                sold_idx = text_lower.find('sold') if 'sold' in text_lower else 999
                if bought_idx < sold_idx:
                    return self._handle_record_i_owe(phone_number, text)
                else:
                    return self._handle_record_debt(phone_number, text)
            elif 'on credit' in text_lower or 'took' in text_lower:
                # Plain "on credit" or "took" with no other signal — default
                # to sale (customer took goods), the more common phrasing
                return self._handle_record_debt(phone_number, text)

        # ---- Ambiguous transaction type detection ----
        # If no clear buy/sell signal, ask user
        sell_signals = ['sold', 'sell', 'sales', 'received from customer', 'customer paid', 'collected from']
        buy_signals = ['bought', 'purchased', 'paid for', 'spent on', 'buying']
        has_sell = any(s in text_lower for s in sell_signals)
        has_buy = any(s in text_lower for s in buy_signals)

        # ---- Vague goods/compound word detection ----
        vague_words = ['goods', 'items', 'things', 'stuff', 'supplies', 'materials', 'products', 'stock']
        has_vague = any(w in text_lower for w in vague_words)
        has_specific_product = any(p.lower() in text_lower for p in self._get_catalog_product_names(phone_number))

        if has_vague and not has_specific_product and re.search(r'\d', text_lower):
            # Ask for breakdown
            self.db.save_session(phone_number, 'AWAITING_BREAKDOWN', {
                'original_text': text,
                'amount': self._extract_amount_from_text(text)
            })
            return [{"type": "text", "content": (
                "\U0001f4dd What *specifically* were the goods/items?\n\n"
                "_Be specific so I can allocate correctly:_\n"
                "_Example: \"10 pairs Nike Airforce 1, 5 Gucci bags\"_\n\n"
                "_Or type \"skip\" to save as-is._"
            )}]

        # ---- Check for multi-transaction ----
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
        # Get user's business type and industry
        user = self.db.get_user(phone_number)
        business_type = user.get('business_type', 'trading') if user else 'trading'
        industry_class = user.get('industry_class', '') if user else ''

        # Call AI with the FULL text, ask for array response
        result = self.categorizer.parse_multi_transaction(text, phone_number, business_type, industry_class)

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
                # Discount & Tax
                "subtotal": item.get('subtotal'),
                "discount_amount": item.get('discount_amount'),
                "discount_percent": item.get('discount_percent'),
                "discount_type": item.get('discount_type'),
                "tax_amount": item.get('tax_amount'),
                "tax_percent": item.get('tax_percent'),
                "tax_type": item.get('tax_type'),
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
            # Preserve credit signal so we can handle it properly after amount is given
            is_credit = ('on credit' in text.lower() or 'credit sale' in text.lower() or
                         'gave credit' in text.lower() or 'took on credit' in text.lower())
            # Extract name early so we don't lose it
            name_hint = self._extract_contact_name_from_text(text, 0)
            self.db.save_session(phone_number, STATE_RECORDING, {
                "description": text,
                "is_credit": is_credit,
                "name_hint": name_hint or '',
            })
            return [{"type": "text", "content": (
                "\U0001f4b0 How much was it? (Just type the amount)\n\n"
                "E.g.: 95000 or 95K or \u20a695,000"
            )}]

        # Get user's business type for tailored parsing
        user = self.db.get_user(phone_number)
        business_type = user.get('business_type', 'trading') if user else 'trading'

        # Rich AI parsing — extracts everything
        try:
            result = self.categorizer.parse_transaction(text, phone_number, business_type)
        except Exception as e:
            logger.error(f"AI categorizer failed: {e}")
            result = {}

        # Use AI's transaction type if available, fallback to rule-based
        tx_type = result.get('transaction_type') or detect_transaction_type(text)
        vendor = result.get('vendor_or_customer') or extract_vendor_name(text) or ""
        # Filter out false vendor names (transaction verbs the AI sometimes returns)
        if vendor.lower() in ('sold', 'bought', 'paid', 'received', 'gave', 'sent', 'buy', 'sell', 'unknown'):
            vendor = ""
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
            # Discount & Tax
            "subtotal": result.get('subtotal'),
            "discount_amount": result.get('discount_amount'),
            "discount_percent": result.get('discount_percent'),
            "discount_type": result.get('discount_type'),
            "tax_amount": result.get('tax_amount'),
            "tax_percent": result.get('tax_percent'),
            "tax_type": result.get('tax_type'),
        }

        # ===== DEFAULT TAX APPLICATION =====
        # If user said "+ tax" or "+ VAT" but AI couldn't determine the rate,
        # apply the user's default tax rate
        text_has_tax_signal = any(kw in text.lower() for kw in ['+ tax', '+tax', '+ vat', '+vat', 'plus tax', 'plus vat'])
        if text_has_tax_signal and not pending.get('tax_percent') and not pending.get('tax_amount'):
            user_tax = user.get('default_tax_percent') if user else None
            if user_tax:
                try:
                    rate = float(user_tax)
                    tax_type = user.get('default_tax_type', 'VAT')
                    subtotal = pending['amount']
                    tax_amt = int(subtotal * rate / 100)
                    pending['subtotal'] = subtotal
                    pending['tax_percent'] = rate
                    pending['tax_type'] = tax_type
                    pending['tax_amount'] = tax_amt
                    pending['amount'] = subtotal + tax_amt
                    amount = pending['amount']
                except (ValueError, TypeError):
                    pass

        # ===== CREDIT/DEBT DETECTION =====
        # If the text indicates a credit transaction (but _detect_command
        # missed it because "on credit" isn't at the start), intercept here
        # and route to the debt system instead of the normal flow.
        text_lower_for_credit = text.lower()
        payment_status_ai = result.get('payment_status', '') or ''
        is_credit_ai = payment_status_ai.lower() in ['credit', 'on credit', 'unpaid']

        # Determine if this is a credit transaction at all
        has_credit_signal = ('on credit' in text_lower_for_credit or
                            'gave credit' in text_lower_for_credit or
                            'credit sale' in text_lower_for_credit or
                            'credit purchase' in text_lower_for_credit or
                            'took on credit' in text_lower_for_credit or
                            'owing me' in text_lower_for_credit or
                            'gave goods' in text_lower_for_credit or
                            'i am owing' in text_lower_for_credit or
                            is_credit_ai)

        # Determine direction: did USER buy on credit (I owe) or did someone else (they owe me)?
        user_is_buyer = ('i bought' in text_lower_for_credit or
                         'i purchased' in text_lower_for_credit or
                         'bought from' in text_lower_for_credit or
                         'credit purchase' in text_lower_for_credit or
                         'i am owing' in text_lower_for_credit or
                         tx_type == 'expense')

        if has_credit_signal and user_is_buyer:
            # Route to "I owe someone" debt flow
            name = self._extract_contact_name_from_text(text, amount)
            return self._build_rich_credit_confirmation(phone_number, text, amount, name, 'i_owe') if name else self._handle_record_i_owe(phone_number, text)

        if has_credit_signal and not user_is_buyer:
            # Route to "someone owes me" debt flow
            name = self._extract_contact_name_from_text(text, amount)
            return self._build_rich_credit_confirmation(phone_number, text, amount, name, 'owed_to_me') if name else self._handle_record_debt(phone_number, text)
        # ===== END CREDIT/DEBT DETECTION =====

        pending = self._enrich_with_unit_conversion(phone_number, pending)
        self.db.save_session(phone_number, STATE_AWAITING_CONFIRMATION, pending)

        # Build rich confirmation message
        type_emoji = "\U0001f4b0" if tx_type == "income" else "\U0001f4b8"
        cat_emoji = self._get_category_emoji(category)

        response_text = f"\U0001f4dd Got it!\n\n"
        response_text += f"{type_emoji} *\u20a6{amount:,}* ({tx_type.title()})\n"

        # Show item details — hierarchy-aware
        item_name = result.get('item_name', '')
        brand = result.get('brand', '')
        model = result.get('model', '')
        color = result.get('color', '')
        pattern = result.get('pattern', '') or result.get('style', '')
        size = result.get('size', '')

        # Check if product has a hierarchy defined
        _user_cat = self.db.get_user(phone_number)
        _auto_cat = _user_cat.get('auto_catalog', {}) if _user_cat else {}
        _products = _auto_cat.get('products', {})
        _hierarchy = None
        _product_key = ''
        if item_name:
            _item_lower = item_name.lower().rstrip('s')  # handle plural: "bags" → "bag"
            for _pk, _pv in _products.items():
                _pk_stripped = _pk.rstrip('s')  # "bags" → "bag"
                _name_lower = _pv.get('name', '').lower().rstrip('s')
                # Match: exact, contains, singular/plural
                if (_item_lower in _pk or _pk in _item_lower or
                    _item_lower == _pk_stripped or _pk_stripped in _item_lower or
                    _item_lower in _name_lower or _name_lower in _item_lower):
                    if _pv.get('hierarchy'):
                        _hierarchy = _pv['hierarchy']
                        _product_key = _pk
                    break

        if _hierarchy:
            # Build tree path display: "Socks > Striped > Nike > Blue > Size 15"
            attr_values = {
                'pattern': pattern or '', 'brand': brand or '',
                'color': color or '', 'size': str(size) if size else '',
                'material': result.get('material', ''),
                'model': result.get('model', ''),
                'condition': result.get('condition', ''),
                'type': result.get('item_type', '') or result.get('model', ''),
                'style': pattern or result.get('style', ''),
            }
            # If AI put pattern+color together (e.g. "blue striped"), try to split
            if not pattern and color:
                pattern_words = ['striped', 'solid', 'sport', 'plain', 'checked', 'ankara', 'floral']
                color_parts = color.lower().split()
                for pw in pattern_words:
                    if pw in color_parts:
                        attr_values['pattern'] = pw.title()
                        attr_values['color'] = ' '.join(w for w in color_parts if w != pw).title()
                        # Update result for saving
                        result['pattern'] = attr_values['pattern']
                        result['color'] = attr_values['color']
                        break

            path_parts = [item_name.title()]
            for attr in _hierarchy:
                val = attr_values.get(attr, '')
                if val:
                    path_parts.append(val.title())

            response_text += f"\U0001f4e6 *{' > '.join(path_parts)}*\n"

            # Show quantity and price on separate line
            qty_parts = []
            if result.get('quantity'):
                qty_display = f"Qty: {result['quantity']}"
                if pending.get('base_quantity'):
                    qty_display += f" (= {pending['base_quantity']} {pending['base_unit']})"
                qty_parts.append(qty_display)
            if result.get('unit_cost'):
                qty_parts.append(f"\u20a6{int(result['unit_cost']):,}/unit")
            if qty_parts:
                response_text += "\U0001f4cb " + " | ".join(qty_parts) + "\n"
        else:
            # No hierarchy — use flat display
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
            details = []
            if size:
                details.append(f"Size: {size}")
            if color:
                details.append(f"Color: {color}")
            if result.get('quantity'):
                qty_display = f"Qty: {result['quantity']}"
                if pending.get('base_quantity'):
                    qty_display += f" (= {pending['base_quantity']} {pending['base_unit']})"
                details.append(qty_display)
            if result.get('unit_cost'):
                details.append(f"Unit: \u20a6{int(result['unit_cost']):,}")
            if details:
                response_text += "\U0001f4cb " + " | ".join(details) + "\n"

        # Discount line
        discount_amt = result.get('discount_amount') or pending.get('discount_amount')
        discount_pct = result.get('discount_percent') or pending.get('discount_percent')
        discount_type = result.get('discount_type') or pending.get('discount_type')
        if discount_amt or discount_pct:
            disc_label = "Discount given" if discount_type == "given" else "Discount"
            disc_val = f"\u20a6{int(discount_amt):,}" if discount_amt else f"{discount_pct}%"
            response_text += f"\U0001f3f7\ufe0f {disc_label}: -{disc_val}\n"

        # Tax line
        tax_amt = result.get('tax_amount') or pending.get('tax_amount')
        tax_pct = result.get('tax_percent') or pending.get('tax_percent')
        tax_type = result.get('tax_type') or pending.get('tax_type')
        if tax_amt or tax_pct:
            tax_label = tax_type or "Tax"
            tax_val = f"\u20a6{int(tax_amt):,}" if tax_amt else f"{tax_pct}%"
            response_text += f"\U0001f4b1 {tax_label}: +{tax_val}\n"

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
                        subtotal=tx_data.get('subtotal'),
                        discount_amount=tx_data.get('discount_amount'),
                        discount_percent=tx_data.get('discount_percent'),
                        discount_type=tx_data.get('discount_type'),
                        tax_amount=tx_data.get('tax_amount'),
                        tax_percent=tx_data.get('tax_percent'),
                        tax_type=tx_data.get('tax_type'),
                    )
                    # Update contact totals
                    vendor = tx_data.get('vendor', '')
                    if vendor:
                        self.db.update_contact_totals(phone_number, vendor, tx_data['amount'], tx_data['type'])
                    # Auto-update product catalog
                    self._update_auto_catalog(phone_number, tx_data)
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
                subtotal=context.get('subtotal'),
                discount_amount=context.get('discount_amount'),
                discount_percent=context.get('discount_percent'),
                discount_type=context.get('discount_type'),
                tax_amount=context.get('tax_amount'),
                tax_percent=context.get('tax_percent'),
                tax_type=context.get('tax_type'),
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

            # Auto-update product catalog from this transaction
            self._update_auto_catalog(phone_number, context)

            # Smart CRM prompt: if amount >= 10K and no vendor/customer, ask
            amount = context['amount']
            tx_type = context['type']
            has_vendor = bool(vendor)

            if not has_vendor and amount >= 10000:
                # Save state with transaction ID so we can attach the name later
                tx_id = tx.get('transaction_id', '') if isinstance(tx, dict) else ''
                self.db.save_session(phone_number, STATE_AWAITING_CRM_HINT, {
                    'transaction_id': tx_id,
                    'tx_type': tx_type,
                    'amount': amount,
                    'crm_step': 'ask_name',
                })
                prompt = "sell to" if tx_type == "income" else "buy from"
                return [{"type": "text", "content": (
                    f"✅ Saved!\n\n"
                    f"💡 Who did you {prompt}?\n"
                    f"_Type their name, or just send your next transaction._"
                )}]
            else:
                # For SALES: offer invoice/receipt buttons (natural next step)
                tx_id = tx.get('transaction_id', '') if isinstance(tx, dict) else ''
                if tx_type == 'income' and vendor:
                    self.db.save_session(phone_number, STATE_IDLE, {'last_saved_tx_id': tx_id, 'last_saved_vendor': vendor, 'last_saved_amount': amount})
                    # Auto-COGS for products with recipes (manufacturing)
                    margin_info = ''
                    item_name_ctx = context.get('item_name', '')
                    if item_name_ctx:
                        user_data = self.db.get_user(phone_number)
                        _cat = user_data.get('auto_catalog', {}) if user_data else {}
                        for _pk, _pv in _cat.get('products', {}).items():
                            if _pv.get('recipe') and (item_name_ctx.lower() in _pk or _pk in item_name_ctx.lower()):
                                _cpu = _pv['recipe']['cost_per_unit']
                                _qty = int(context.get('quantity', 1) or 1)
                                _cogs = _cpu * _qty
                                _profit = amount - _cogs
                                _margin = round(_profit / amount * 100) if amount > 0 else 0
                                margin_info = f"\n\U0001f4ca COGS: \u20a6{_cogs:,} | Profit: \u20a6{_profit:,} ({_margin}%)"
                                # Deduct from inventory
                                _inv = _pv.get('inventory', {})
                                _inv['finished_goods'] = max(0, _inv.get('finished_goods', 0) - _qty)
                                _pv['inventory'] = _inv
                                _cat['products'][_pk] = _pv
                                self.db.update_user_field(phone_number, 'auto_catalog', _cat)
                                break
                    # Show receipt only for cash sales (not credit)
                    is_credit = context.get('payment', '').lower() == 'credit' or 'credit' in context.get('description', '').lower()
                    if is_credit:
                        doc_buttons = [
                            {"id": "post_invoice", "title": "📄 Invoice"},
                            {"id": "post_done", "title": "✅ Done"},
                        ]
                    else:
                        doc_buttons = [
                            {"id": "post_invoice", "title": "📄 Invoice"},
                            {"id": "post_receipt", "title": "🧾 Receipt"},
                            {"id": "post_done", "title": "✅ Done"},
                        ]
                    return [{"type": "buttons", "content": {
                        "body": f"✅ Saved! ₦{amount:,} from {vendor}{margin_info}\n\nGenerate a document?",
                        "buttons": doc_buttons
                    }}]
                elif tx_type == 'income':
                    self.db.save_session(phone_number, STATE_IDLE, {'last_saved_tx_id': tx_id, 'last_saved_amount': amount})
                    return [{"type": "buttons", "content": {
                        "body": f"✅ Saved! ₦{amount:,} income recorded.\n\nGenerate a document?",
                        "buttons": [
                            {"id": "post_invoice", "title": "📄 Invoice"},
                            {"id": "post_receipt", "title": "🧾 Receipt"},
                            {"id": "post_done", "title": "✅ Done"},
                        ]
                    }}]
                else:
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
            # Before giving up — check if user sent a NEW transaction
            # (they want to move on, not respond to the confirmation)
            potential_amount = parse_amount(text)
            if potential_amount and potential_amount > 0:
                # This looks like a new transaction — abandon pending and process fresh
                logger.info(f"User sent new transaction while in confirmation: {text}")
                self.db.save_session(phone_number, STATE_IDLE, {})
                return self._handle_idle(phone_number, text)

            # Also check if it's a debt/credit statement
            text_lower_check = text.lower().strip()
            credit_patterns = ['on credit', 'gave credit', 'sold on credit', 'credit sale']
            payment_patterns = ['paid me', 'has paid', 'settled', 'cleared']
            if any(p in text_lower_check for p in credit_patterns + payment_patterns):
                self.db.save_session(phone_number, STATE_IDLE, {})
                return self._handle_idle(phone_number, text)

            # Truly unclear response
            return [{"type": "buttons", "content": {
                "body": "I didn't understand. Is the category correct?",
                "buttons": [
                    {"id": "confirm_yes", "title": "✅ Yes"},
                    {"id": "confirm_change", "title": "✏️ Change"},
                    {"id": "confirm_undo", "title": "↩️ Cancel"},
                ]
            }}]

    def _handle_crm_hint(self, phone_number, text, context):
        """Handle the soft CRM prompts after a large transaction.
        Step 1: Ask for name → Step 2: Ask for payment method.
        User can skip, send a new transaction, or a command at any point."""
        text_lower = text.lower().strip()
        step = context.get('crm_step', 'ask_name')

        # Skip / dismiss
        if text_lower in ['skip', 'no', 'nah', 'none', 'no one', 'next']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "👍 No problem. Send your next transaction."}]

        # Check if it's a command — break out
        command = self._detect_command(text_lower)
        if command:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_idle(phone_number, text)

        # Check if it's a new transaction (has amount) — break out
        potential_amount = parse_amount(text)
        if potential_amount and potential_amount > 0:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_idle(phone_number, text)

        tx_id = context.get('transaction_id', '')
        tx_type = context.get('tx_type', 'expense')
        amount = context.get('amount', 0)

        # ---- STEP 1: User provides a name ----
        if step == 'ask_name':
            name = text.strip().title()
            if len(name) < 2 or len(name) > 50:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": "👍 Got it. Send your next transaction."}]

            # Attach name to the transaction
            if tx_id:
                try:
                    self.db.transactions.update_item(
                        Key={'phone_number': phone_number, 'transaction_id': tx_id},
                        UpdateExpression="SET vendor = :v",
                        ExpressionAttributeValues={':v': name}
                    )
                except Exception as e:
                    logger.error(f"Error attaching vendor to tx: {e}")

            # Update contact totals & merchant memory
            self.db.update_contact_totals(phone_number, name, amount, tx_type)
            self.db.save_merchant(phone_number, name, '', '')

            # Move to step 2: ask payment method
            context['crm_step'] = 'ask_payment'
            context['vendor_name'] = name
            self.db.save_session(phone_number, STATE_AWAITING_CRM_HINT, context)

            emoji = "👤" if tx_type == "income" else "🏪"
            # For large amounts (≥50K), include credit option
            if amount >= 50000:
                return [{"type": "text", "content": (
                    f"✅ {emoji} *{name}* noted.\n\n"
                    f"💳 How were you paid?\n"
                    f"_Cash / Transfer / POS / On credit_\n"
                    f"_Or just send your next transaction._"
                )}]
            else:
                return [{"type": "text", "content": (
                    f"✅ {emoji} *{name}* noted.\n\n"
                    f"💳 Cash or transfer?\n"
                    f"_Or just send your next transaction._"
                )}]

        # ---- STEP 2: User provides payment method ----
        elif step == 'ask_payment':
            payment_map = {
                'cash': 'cash', 'transfer': 'transfer', 'bank': 'transfer',
                'pos': 'POS', 'card': 'POS',
                'credit': 'credit', 'on credit': 'credit',
            }
            payment = payment_map.get(text_lower, text_lower if len(text_lower) <= 15 else None)

            if payment and tx_id:
                try:
                    update_expr = "SET payment_method = :pm"
                    expr_values = {':pm': payment}
                    # If on credit, also set payment_status
                    if payment == 'credit':
                        update_expr += ", payment_status = :ps"
                        expr_values[':ps'] = 'credit'
                    self.db.transactions.update_item(
                        Key={'phone_number': phone_number, 'transaction_id': tx_id},
                        UpdateExpression=update_expr,
                        ExpressionAttributeValues=expr_values
                    )
                except Exception as e:
                    logger.error(f"Error attaching payment method to tx: {e}")

            self.db.save_session(phone_number, STATE_IDLE, {})
            if payment == 'credit':
                vendor_name = context.get('vendor_name', '')
                # Also record as debt
                if vendor_name and tx_type == 'income':
                    self.db.record_debt(phone_number, vendor_name, amount, 'owed_to_me')
                    return [{"type": "text", "content": f"✅ 💳 On credit — *{vendor_name}* now owes you ₦{int(amount):,}.\n\n_Send your next transaction._"}]
                elif vendor_name and tx_type == 'expense':
                    self.db.record_debt(phone_number, vendor_name, amount, 'i_owe')
                    return [{"type": "text", "content": f"✅ 💳 On credit — you owe *{vendor_name}* ₦{int(amount):,}.\n\n_Send your next transaction._"}]
            return [{"type": "text", "content": f"✅ 💳 *{payment.title() if payment else 'Noted'}*. Send your next transaction."}]

        # Fallback — reset
        self.db.save_session(phone_number, STATE_IDLE, {})
        return self._handle_idle(phone_number, text)

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

    def _parse_time_period(self, text):
        """Parse time period from natural language text.
        Returns (start_date, end_date, label, time_start_hour, time_end_hour).
        time_start_hour/time_end_hour are None unless time-of-day filtering requested."""
        from datetime import datetime, timedelta
        import re
        now = datetime.now()
        text_lower = (text or '').lower().strip()
        time_start_hour = None
        time_end_hour = None

        # ---- TIME OF DAY ----
        # "2am to 6pm", "morning", "evening", "afternoon", "night"
        time_match = re.search(r'(\d{1,2})\s*([ap]m?)\s*(?:to|-|\u2014)\s*(\d{1,2})\s*([ap]m?)', text_lower)
        if time_match:
            h1 = int(time_match.group(1))
            p1 = time_match.group(2)
            h2 = int(time_match.group(3))
            p2 = time_match.group(4)
            time_start_hour = h1 + (12 if 'p' in p1 and h1 != 12 else 0)
            time_end_hour = h2 + (12 if 'p' in p2 and h2 != 12 else 0)
            # Remove time part from text for date parsing below
            text_lower = text_lower[:time_match.start()] + text_lower[time_match.end():]
        elif 'morning' in text_lower:
            time_start_hour, time_end_hour = 6, 12
        elif 'afternoon' in text_lower:
            time_start_hour, time_end_hour = 12, 17
        elif 'evening' in text_lower:
            time_start_hour, time_end_hour = 17, 21
        elif 'night' in text_lower:
            time_start_hour, time_end_hour = 21, 6  # wraps to next day

        # ---- SPECIFIC DATE: DD/MM/YYYY or DD-MM-YYYY ----
        date_match = re.search(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})', text_lower)
        if date_match:
            d, m, y = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            if y < 100:
                y += 2000
            try:
                dt = datetime(y, m, d)
                label = dt.strftime('%d %B %Y')
                ds = dt.strftime('%Y-%m-%d')
                return ds, ds, label, time_start_hour, time_end_hour
            except ValueError:
                pass

        # ---- SPECIFIC DATE: "3rd july", "july 3", "15th june 2026" ----
        months_map = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                      'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
                      'january': 1, 'february': 2, 'march': 3, 'april': 4,
                      'june': 6, 'july': 7, 'august': 8, 'september': 9,
                      'october': 10, 'november': 11, 'december': 12}

        # "3rd july" or "15th june 2026"
        specific_date = re.search(r'(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)(?:\s+(\d{4}))?', text_lower)
        if specific_date:
            day = int(specific_date.group(1))
            month_word = specific_date.group(2)[:3]
            year = int(specific_date.group(3)) if specific_date.group(3) else now.year
            if month_word in months_map and 1 <= day <= 31:
                month = months_map[month_word]
                try:
                    dt = datetime(year, month, day)
                    label = dt.strftime('%d %B %Y')
                    ds = dt.strftime('%Y-%m-%d')
                    return ds, ds, label, time_start_hour, time_end_hour
                except ValueError:
                    pass

        # "july 3" or "june 15"
        specific_date2 = re.search(r'(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?', text_lower)
        if specific_date2:
            month_word = specific_date2.group(1)[:3]
            day = int(specific_date2.group(2))
            year = int(specific_date2.group(3)) if specific_date2.group(3) else now.year
            if month_word in months_map and 1 <= day <= 31:
                month = months_map[month_word]
                try:
                    dt = datetime(year, month, day)
                    label = dt.strftime('%d %B %Y')
                    ds = dt.strftime('%Y-%m-%d')
                    return ds, ds, label, time_start_hour, time_end_hour
                except ValueError:
                    pass

        # ---- DATE RANGE: "1st to 15th" or "june 1 to june 30" ----
        range_match = re.search(r'(\d{1,2})(?:st|nd|rd|th)?\s*(?:to|-|\u2014)\s*(\d{1,2})(?:st|nd|rd|th)?', text_lower)
        if range_match:
            d1 = int(range_match.group(1))
            d2 = int(range_match.group(2))
            # Try to find month context
            month = now.month
            year = now.year
            for word in text_lower.split():
                if word[:3] in months_map:
                    month = months_map[word[:3]]
                    break
            try:
                start_dt = datetime(year, month, d1)
                end_dt = datetime(year, month, d2)
                label = f"{start_dt.strftime('%d')} to {end_dt.strftime('%d %B %Y')}"
                return start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d'), label, time_start_hour, time_end_hour
            except ValueError:
                pass

        # ---- TODAY / YESTERDAY ----
        if 'today' in text_lower or "today\'s" in text_lower:
            d = now.strftime('%Y-%m-%d')
            return d, d, 'Today', time_start_hour, time_end_hour
        if 'yesterday' in text_lower:
            d = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            return d, d, 'Yesterday', time_start_hour, time_end_hour

        # ---- DAY NAMES ----
        day_names = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                     'friday': 4, 'saturday': 5, 'sunday': 6}
        for day_name, day_num in day_names.items():
            if day_name in text_lower:
                # Find the most recent occurrence of that day
                days_ago = (now.weekday() - day_num) % 7
                if days_ago == 0 and 'last' in text_lower:
                    days_ago = 7
                elif days_ago == 0:
                    days_ago = 0  # today if it matches
                target = now - timedelta(days=days_ago)
                label = target.strftime('%A, %d %B')
                ds = target.strftime('%Y-%m-%d')
                return ds, ds, label, time_start_hour, time_end_hour

        # ---- THIS WEEK / LAST WEEK ----
        if 'last week' in text_lower:
            start = now - timedelta(days=now.weekday() + 7)
            end = start + timedelta(days=6)
            return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'), 'Last Week', time_start_hour, time_end_hour
        if 'this week' in text_lower or 'week' in text_lower or 'weekly' in text_lower:
            start = now - timedelta(days=now.weekday())
            return start.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d'), 'This Week', time_start_hour, time_end_hour

        # ---- LAST N DAYS ----
        days_match = re.search(r'last\s+(\d+)\s*days?', text_lower)
        if days_match:
            n = int(days_match.group(1))
            start = (now - timedelta(days=n)).strftime('%Y-%m-%d')
            return start, now.strftime('%Y-%m-%d'), f'Last {n} Days', time_start_hour, time_end_hour

        # ---- MONTH NAMES: "june", "may 2026", "last month" ----
        if 'last month' in text_lower:
            first_of_this_month = now.replace(day=1)
            last_month_end = first_of_this_month - timedelta(days=1)
            last_month_start = last_month_end.replace(day=1)
            label = last_month_start.strftime('%B %Y')
            return last_month_start.strftime('%Y-%m-%d'), last_month_end.strftime('%Y-%m-%d'), label, time_start_hour, time_end_hour

        for month_word, month_num in months_map.items():
            if month_word in text_lower and len(month_word) >= 3:
                # Check for year
                year_match = re.search(r'(\d{4})', text_lower)
                year = int(year_match.group(1)) if year_match else now.year
                # Full month range
                import calendar
                last_day = calendar.monthrange(year, month_num)[1]
                start_dt = datetime(year, month_num, 1)
                end_dt = datetime(year, month_num, last_day)
                label = start_dt.strftime('%B %Y')
                return start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d'), label, time_start_hour, time_end_hour

        # ---- DEFAULT: THIS MONTH ----
        start = now.strftime('%Y-%m-01')
        end = now.strftime('%Y-%m-%d')
        return start, end, now.strftime('%B %Y'), time_start_hour, time_end_hour


    def _handle_filtered_report(self, phone_number, filter_type, time_text=''):
        """Generate a sales/purchases/expenses report with granular time + entity filtering + export"""
        from datetime import datetime
        import re as _re

        # Parse time period (supports all granular ranges)
        result = self._parse_time_period(time_text)
        start_date, end_date, period_label, time_start_hour, time_end_hour = result

        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)

        # ---- SMART ENTITY FILTERS ----
        text_lower = (time_text or '').lower()
        entity_filter_label = ''

        # Vendor/Customer filter: "to Sandra", "from Alhaji", "Sandra sales"
        vendor_match = _re.search(r'(?:to|from|for)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)', time_text or '')
        vendor_filter = vendor_match.group(1).lower() if vendor_match else ''
        if not vendor_filter:
            # Check if a proper noun (capitalized word) is in the text that's not a keyword
            skip_words = {'my', 'sales', 'purchases', 'expenses', 'today', 'week', 'month', 'this', 'last', 'all', 'over', 'above', 'below', 'under', 'nike', 'gucci', 'prada'}
            words = (time_text or '').split()
            for w in words:
                if w[0:1].isupper() and w.lower() not in skip_words and len(w) > 2:
                    vendor_filter = w.lower()
                    break

        # Brand/Product filter: "Nike sales", "socks purchases"
        brand_filter = ''
        known_brands = ['nike', 'gucci', 'prada', 'adidas', 'fendi', 'd&g', 'balenciaga', 'zara', 'dangote']
        for brand in known_brands:
            if brand in text_lower:
                brand_filter = brand
                break

        # Amount threshold: "over 50K", "above 100K", "below 20K"
        amount_min = 0
        amount_max = float('inf')
        over_match = _re.search(r'(?:over|above|more than|greater than)\s+(\d+[kKmM]?)', text_lower)
        under_match = _re.search(r'(?:under|below|less than)\s+(\d+[kKmM]?)', text_lower)
        if over_match:
            val = over_match.group(1)
            amount_min = int(val[:-1]) * 1000 if val[-1].lower() == 'k' else int(val[:-1]) * 1000000 if val[-1].lower() == 'm' else int(val)
            entity_filter_label += f' over \u20a6{amount_min:,}'
        if under_match:
            val = under_match.group(1)
            amount_max = int(val[:-1]) * 1000 if val[-1].lower() == 'k' else int(val[:-1]) * 1000000 if val[-1].lower() == 'm' else int(val)
            entity_filter_label += f' under \u20a6{int(amount_max):,}'
        if vendor_filter:
            entity_filter_label += f' ({vendor_filter.title()})'
        if brand_filter:
            entity_filter_label += f' [{brand_filter.title()}]'

        # Apply time-of-day filter if specified
        if time_start_hour is not None and time_end_hour is not None:
            def in_time_range(tx):
                created = tx.get('created_at', '')
                if not created:
                    return True  # Include if no timestamp
                try:
                    hour = int(created[11:13])  # Extract hour from ISO format
                    if time_start_hour <= time_end_hour:
                        return time_start_hour <= hour < time_end_hour
                    else:  # Wraps midnight (e.g. 9pm to 6am)
                        return hour >= time_start_hour or hour < time_end_hour
                except (ValueError, IndexError):
                    return True
            transactions = [tx for tx in transactions if in_time_range(tx)]
            period_label += f" ({time_start_hour}:00-{time_end_hour}:00)"

        # Get user's industry for tailored labels and category grouping
        user = self.db.get_user(phone_number)
        industry_class = user.get('industry_class', 'trading') if user else 'trading'

        # Industry-specific COGS categories
        from services.categorizer import INDUSTRY_CATEGORIES
        ind_config = INDUSTRY_CATEGORIES.get(industry_class, INDUSTRY_CATEGORIES['trading'])
        COGS_CATEGORIES = ind_config.get('cogs', ['Goods & Stock'])

        # Industry-specific labels
        industry_labels = {
            'trading': {'sales': 'Sales', 'purchases': 'Purchases (Stock)', 'expenses': 'Operating Expenses'},
            'manufacturing': {'sales': 'Production Sales', 'purchases': 'Raw Materials & Production', 'expenses': 'Operating Expenses'},
            'services': {'sales': 'Service Revenue', 'purchases': 'Direct Service Costs', 'expenses': 'Operating Expenses'},
            'hybrid': {'sales': 'Revenue', 'purchases': 'Direct Costs', 'expenses': 'Operating Expenses'},
        }
        ind_labels = industry_labels.get(industry_class, industry_labels['trading'])

        if filter_type == 'my_sales':
            label = ind_labels['sales']
            emoji = '\U0001f4b0'
            filtered = [tx for tx in transactions
                       if tx.get('type') == 'income'
                       and 'Debt payment' not in tx.get('description', '')]
        elif filter_type == 'my_purchases':
            label = ind_labels['purchases']
            emoji = '\U0001f6d2'
            filtered = [tx for tx in transactions
                       if tx.get('type') == 'expense'
                       and tx.get('category', '') in COGS_CATEGORIES]
        elif filter_type == 'my_expenses':
            label = ind_labels['expenses']
            emoji = '\U0001f4b8'
            filtered = [tx for tx in transactions
                       if tx.get('type') == 'expense'
                       and tx.get('category', '') not in COGS_CATEGORIES]
        else:
            label = 'Transactions'
            emoji = '\U0001f4ca'
            filtered = transactions

        # Apply smart entity filters
        if vendor_filter:
            filtered = [tx for tx in filtered if vendor_filter in tx.get('vendor', '').lower()]
        if brand_filter:
            filtered = [tx for tx in filtered 
                       if brand_filter in tx.get('brand', '').lower() 
                       or brand_filter in tx.get('description', '').lower()
                       or brand_filter in tx.get('item_name', '').lower()]
        if amount_min > 0:
            filtered = [tx for tx in filtered if int(tx.get('amount', 0)) >= amount_min]
        if amount_max < float('inf'):
            filtered = [tx for tx in filtered if int(tx.get('amount', 0)) <= amount_max]

        # Update period label with entity filters
        if entity_filter_label:
            period_label += entity_filter_label

        if not filtered:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": f"\U0001f4ca No {label.lower()} found for *{period_label}*."}]

        total = sum(int(tx.get('amount', 0)) for tx in filtered)

        report = f"{emoji} *My {label} \u2014 {period_label}*\n\n"
        report += f"Total: \u20a6{total:,}\n"
        report += f"Transactions: {len(filtered)}\n\n"

        # Show transactions (last 15, NEWEST FIRST)
        report += f"\U0001f4cb *{label}:*\n"
        tx_ids = []
        recent = sorted(filtered, key=lambda x: x.get('created_at', x.get('date', '')), reverse=True)[:15]
        for i, tx in enumerate(recent, 1):
            amount = int(tx.get('amount', 0))
            raw_desc = tx.get('description', '')
            vendor = tx.get('vendor', '')
            desc = self._clean_description_for_display(raw_desc, vendor)
            tx_ids.append(tx.get('transaction_id', ''))
            line = f"*#{i}* \u20a6{amount:,}"
            if desc:
                line += f" \u2014 {desc}"
            bad_vendors = {'unknown', 'sold', 'bought', 'paid', 'received', 'gave', 'sent', ''}
            if vendor and vendor.lower() not in bad_vendors:
                line += f" ({vendor})"
            report += line + "\n"

        if len(filtered) > 15:
            report += f"\n_...and {len(filtered) - 15} more transactions_\n"

        report += f"\n\U0001f4a1 _Type: \"export\" to download as Excel/PDF_"

        # Save tx_ids and filter context for export
        if filter_type == 'my_sales':
            self.db.update_user(phone_number, {'last_tx_list': tx_ids, 'last_sales_list': tx_ids})
        else:
            self.db.update_user(phone_number, {'last_tx_list': tx_ids})

        self.db.save_session(phone_number, STATE_IDLE, {
            'last_filter_type': filter_type,
            'last_filter_period': period_label,
            'last_filter_start': start_date,
            'last_filter_end': end_date,
        })

        # Return report + time filter buttons
        return [
            {"type": "text", "content": report},
            {"type": "buttons", "content": {
                "body": f"\U0001f4c5 Filter *{label}* by period:",
                "buttons": [
                    {"id": f"filter_{filter_type}_today", "title": "\U0001f4c5 Today"},
                    {"id": f"filter_{filter_type}_week", "title": "\U0001f4c5 This Week"},
                    {"id": f"filter_{filter_type}_month", "title": "\U0001f4c5 This Month"},
                ]
            }}
        ]


    def _clean_description_for_display(self, description, vendor=''):
        """Clean raw transaction description for display in lists"""
        if not description:
            return ''

        desc = description.strip()

        # Remove leading "Sold"/"Bought"/"Paid"/"Received" etc.
        desc = re.sub(r'^(sold|bought|paid|received|gave|sent|got)\s+', '', desc, flags=re.IGNORECASE)

        # Remove amounts: ₦100,000 or 100000 or 100k
        desc = re.sub(r'₦?\d[\d,]*[kKmM]?', '', desc)

        # Remove "to/from [Name]" patterns
        if vendor:
            desc = re.sub(rf'(to|from)\s+{re.escape(vendor)}', '', desc, flags=re.IGNORECASE)
            desc = re.sub(rf'^{re.escape(vendor)}\s*', '', desc, flags=re.IGNORECASE)
            desc = re.sub(rf'\s*{re.escape(vendor)}$', '', desc, flags=re.IGNORECASE)
        else:
            desc = re.sub(r'(to|from)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?', '', desc)

        # Remove "for" prefix
        desc = re.sub(r'^for\s+', '', desc, flags=re.IGNORECASE)

        # Remove "on credit"
        desc = re.sub(r'\s*on\s+credit\s*', '', desc, flags=re.IGNORECASE)

        # Remove quantity phrases like "20 pairs of"
        desc = re.sub(r'^\d*\s*(pairs?|pieces?|cartons?|bags?|boxes?|packs?|bottles?|crates?|dozen)\s+(of\s+)?', '', desc, flags=re.IGNORECASE)

        # Clean up extra spaces and trailing connectors
        desc = re.sub(r'\s+', ' ', desc).strip()
        desc = re.sub(r'^(for|of|to|from)\s+', '', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\s+(for|of|to|from)$', '', desc, flags=re.IGNORECASE)

        return desc.title()[:40] if desc else ''

    def _detect_command(self, text_lower):
        """Check if the text matches a known command.
        Multi-word keywords are checked FIRST across all commands so that
        e.g. 'contact catalog' (multi-word) wins over 'contact' (single-word)
        from a different, unrelated command."""
        # Pass 1: exact matches
        for command, keywords in COMMANDS.items():
            for keyword in keywords:
                if text_lower == keyword:
                    return command

        # Pass 2: multi-word keyword startswith (more specific, checked first)
        # Sort by keyword length descending so the most specific phrase wins
        multi_word_matches = []
        for command, keywords in COMMANDS.items():
            for keyword in keywords:
                if ' ' in keyword and text_lower.startswith(keyword):
                    multi_word_matches.append((len(keyword), command))
        if multi_word_matches:
            multi_word_matches.sort(reverse=True)  # longest match wins
            return multi_word_matches[0][1]

        # Pass 3: single-word keyword startswith (only if message is short)
        for command, keywords in COMMANDS.items():
            for keyword in keywords:
                if ' ' not in keyword and text_lower.startswith(keyword + ' '):
                    remaining_words = text_lower[len(keyword):].strip().split()
                    if len(remaining_words) <= 2:
                        return command
        return None

    def _is_emoji(self, text):
        """Check if text is primarily emoji"""
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
        """Handle greetings — show interactive menu"""
        user = self.db.get_user(phone_number)
        if user:
            name = user.get('business_name', '').strip()
            greeting = 'Hey ' + name + '! \U0001f44b' if name else 'Hey! \U0001f44b'
        else:
            greeting = 'Hey there! \U0001f44b'

        return [
            {"type": "text", "content": greeting + "\nWhat would you like to do?"},
            {"type": "list", "content": {
                "header": "Quick Actions",
                "body": "Tap an option below \u2014 or just type naturally to record a transaction.",
                "button_text": "\U0001f4cb Open Menu",
                "sections": [
                    {
                        "title": "Record",
                        "rows": self._get_record_menu_rows(phone_number),
                    },
                    {
                        "title": "Business",
                        "rows": [
                            {"id": "menu_reports", "title": "\U0001f4ca Reports \u27a4", "description": "Sales, purchases, expenses"},
                            {"id": "menu_documents", "title": "\U0001f4c4 Documents \u27a4", "description": "Invoice, Receipt, Statement"},
                            {"id": "menu_debts", "title": "\U0001f4b3 Debts & Credits \u27a4", "description": "Who owes you + who you owe"},
                            {"id": "menu_contacts", "title": "\U0001f4d6 Contacts \u27a4", "description": "Customers & suppliers"},
                            {"id": "menu_catalog", "title": "\U0001f4e6 Catalog \u27a4", "description": "Products, brands, units"},
                        ]
                    },
                ]
            }}
        ]


    def _show_help(self):
        """Show interactive list menu with tappable options"""
        return [
            {"type": "text", "content": (
                "\U0001f916 *Kashia Menu*\n\n"
                "Tap the menu below \u2014 or just type naturally to record a transaction.\n\n"
                "_Example: sold 10 Nike shoes to Bola 50k_"
            )},
            {"type": "list", "content": {
                "header": "Kashia Menu",
                "body": "What would you like to do?",
                "button_text": "\U0001f4cb Open Menu",
                "sections": [
                    {
                        "title": "Record",
                        "rows": self._get_record_menu_rows(phone_number),
                    },
                    {
                        "title": "Business",
                        "rows": [
                            {"id": "menu_reports", "title": "\U0001f4ca Reports \u27a4", "description": "Sales, purchases, expenses"},
                            {"id": "menu_documents", "title": "\U0001f4c4 Documents \u27a4", "description": "Invoice, Receipt, Statement"},
                            {"id": "menu_debts", "title": "\U0001f4b3 Debts & Credits \u27a4", "description": "Who owes you + who you owe"},
                            {"id": "menu_contacts", "title": "\U0001f4d6 Contacts \u27a4", "description": "Customers & suppliers"},
                            {"id": "menu_catalog", "title": "\U0001f4e6 Catalog \u27a4", "description": "Products, brands, units"},
                        ]
                    },
                ]
            }}
        ]


    def _handle_undo(self, phone_number):
        """Delete the last transaction and save it for redo"""
        deleted = self.db.delete_last_transaction(phone_number)
        if deleted:
            amount = int(deleted.get('amount', 0))
            cat = deleted.get('category', '')
            desc = deleted.get('description', deleted.get('raw_text', ''))[:30]
            # Save deleted transaction so user can redo it
            self.db.update_user(phone_number, {'last_deleted_transaction': deleted})
            return [{"type": "text", "content": (
                f"↩️ Deleted: ₦{amount:,} ({cat})\n"
                f"_{desc}_\n\n"
                f"Transaction removed. Type *redo* to bring it back."
            )}]
        else:
            return [{"type": "text", "content": "❓ No recent transaction to undo."}]

    # ============================================================
    # CRM — DEBT & CREDIT TRACKING
    # ============================================================

    def _handle_who_owes_me(self, phone_number):
        """Show all customers who owe the user money — with rich details"""
        debtors = self.db.get_all_debtors(phone_number)
        if not debtors:
            return [{"type": "text", "content": (
                "\u2705 *No outstanding debts!*\n\n"
                "Nobody owes you money right now.\n\n"
                "_To record a credit sale:\n"
                "\"sold 10 Nike socks to Bola on credit 150K\"_"
            )}]

        # Sort by amount (highest first)
        debtors.sort(key=lambda d: d.get('amount', 0), reverse=True)
        total = sum(d['amount'] for d in debtors)

        msg = "\U0001f4b0 *People Who Owe You*\n"
        msg += f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"

        for i, d in enumerate(debtors[:12], 1):
            name = d.get('name', 'Unknown')
            amount = d.get('amount', 0)
            date = d.get('last_date', d.get('date', ''))
            desc = d.get('description', '')
            items = d.get('items', '')

            # Calculate days overdue
            days_text = ''
            if date:
                try:
                    from datetime import datetime
                    debt_date = datetime.strptime(date, '%Y-%m-%d')
                    days = (datetime.now() - debt_date).days
                    if days == 0:
                        days_text = 'today'
                    elif days == 1:
                        days_text = '1 day ago'
                    elif days < 7:
                        days_text = f'{days} days ago'
                    elif days < 30:
                        weeks = days // 7
                        days_text = f'{weeks} week{"s" if weeks > 1 else ""} ago'
                    else:
                        months = days // 30
                        days_text = f'{months} month{"s" if months > 1 else ""} overdue'
                except:
                    days_text = date

            msg += f"*{i}. {name}*\n"
            msg += f"   \U0001f4b5 *\u20a6{amount:,}*"
            if days_text:
                msg += f" \u2022 _{days_text}_"
            msg += "\n"
            
            # Show items/description
            if items:
                msg += f"   \U0001f4e6 {items}\n"
            elif desc:
                # Clean up raw descriptions
                clean_desc = desc.strip('_').strip()
                if len(clean_desc) > 50:
                    clean_desc = clean_desc[:67] + '...'
                msg += f"   \U0001f4e6 {clean_desc}\n"
            msg += "\n"

        msg += f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        msg += f"\U0001f4b0 *Total Owed to You: \u20a6{total:,}*\n"
        msg += f"\U0001f465 {len(debtors)} debtor{'s' if len(debtors) > 1 else ''}\n\n"
        msg += "_\u2022 \"Sandra paid me 50K\" \u2014 record payment_\n"
        msg += "_\u2022 \"remind Sandra\" \u2014 send reminder_"

        return [{"type": "text", "content": msg}]

    def _handle_i_owe(self, phone_number):
        """Show all creditors the user owes money to — with rich details"""
        creditors = self.db.get_all_creditors(phone_number)
        if not creditors:
            return [{"type": "text", "content": (
                "\u2705 *You don\'t owe anyone!*\n\n"
                "No outstanding credit purchases.\n\n"
                "_To record: \"bought cement from Dangote on credit 50K\"_"
            )}]

        # Sort by amount (highest first)
        creditors.sort(key=lambda c: c.get('amount', 0), reverse=True)
        total = sum(c['amount'] for c in creditors)

        msg = "\U0001f4b8 *People You Owe*\n"
        msg += "\u2501" * 15 + "\n\n"

        for i, c in enumerate(creditors[:12], 1):
            name = c.get('name', 'Unknown')
            amount = c.get('amount', 0)
            date = c.get('last_date', c.get('date', ''))
            desc = c.get('description', '')

            # Days since
            days_text = ''
            if date:
                try:
                    from datetime import datetime
                    debt_date = datetime.strptime(date, '%Y-%m-%d')
                    days = (datetime.now() - debt_date).days
                    if days == 0:
                        days_text = 'today'
                    elif days == 1:
                        days_text = '1 day ago'
                    elif days < 7:
                        days_text = f'{days} days ago'
                    elif days < 30:
                        weeks = days // 7
                        days_text = f'{weeks} week{"s" if weeks > 1 else ""} ago'
                    else:
                        months = days // 30
                        days_text = f'{months} month{"s" if months > 1 else ""} overdue'
                except Exception:
                    days_text = date

            msg += f"*{i}. {name}*\n"
            msg += f"   \U0001f4b5 *\u20a6{amount:,}*"
            if days_text:
                msg += f" \u2022 _{days_text}_"
            msg += "\n"
            if desc:
                clean_desc = desc.strip('_').strip()[:70]
                msg += f"   \U0001f4e6 {clean_desc}\n"
            msg += "\n"

        msg += "\u2501" * 15 + "\n"
        msg += f"\U0001f4b8 *Total You Owe: \u20a6{total:,}*\n"
        num = len(creditors)
        msg += f"\U0001f3ea {num} creditor{'s' if num > 1 else ''}\n\n"
        msg += "_\u2022 \"paid Dangote 50K\" \u2014 record payment_"

        return [{"type": "text", "content": msg}]


    def _handle_debt_summary(self, phone_number):
        """Show full debt overview"""
        summary = self.db.get_debt_summary(phone_number)
        owed_to_me = summary['total_owed_to_me']
        i_owe = summary['total_i_owe']
        net = summary['net']
        debtors = summary['debtors']
        creditors = summary['creditors']

        msg = "\U0001f4ca *Debt Overview*\n\n"
        msg += f"\U0001f7e2 *Owed to you:* \u20a6{owed_to_me:,} ({len(debtors)} people)\n"
        msg += f"\U0001f534 *You owe:* \u20a6{i_owe:,} ({len(creditors)} people)\n"
        msg += f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        if net >= 0:
            msg += f"\u2705 *Net position: +\u20a6{net:,}*"
        else:
            msg += f"\u26a0\ufe0f *Net position: -\u20a6{abs(net):,}*"
        msg += "\n\n_Type \"who owes me\" or \"i owe\" for full lists_"
        return [{"type": "text", "content": msg}]

    def _handle_record_debt(self, phone_number, text):
        """Parse a credit sale using the full AI categorizer for rich detail
        (product, brand, color, qty, unit price) — same as a normal sale —
        then show a confirmation and record it as a debt instead of cash income."""
        amount = self._extract_amount_from_text(text, strict=True)
        if amount == 0:
            self.db.save_session(phone_number, 'RECORDING_DEBT', {'debt_type': 'owed_to_me', 'step': 'ask_name'})
            return [{"type": "text", "content": "\U0001f4dd *Recording Credit Sale*\n\nWho took goods on credit?\n_Type the customer\'s name_"}]

        name = self._extract_contact_name_from_text(text, amount)
        if not name:
            self.db.save_session(phone_number, 'RECORDING_DEBT', {'debt_type': 'owed_to_me', 'step': 'ask_name', 'amount': amount, 'description': text})
            return [{"type": "text", "content": f"\U0001f4dd Got \u20a6{amount:,}. Who took this on credit? _Type their name_"}]

        return self._build_rich_credit_confirmation(phone_number, text, amount, name, 'owed_to_me')

    def _handle_record_i_owe(self, phone_number, text):
        """Parse a credit purchase using the full AI categorizer for rich
        detail, then show confirmation and record as a debt I owe."""
        amount = self._extract_amount_from_text(text, strict=True)
        if amount == 0:
            self.db.save_session(phone_number, 'RECORDING_DEBT', {'debt_type': 'i_owe', 'step': 'ask_name'})
            return [{"type": "text", "content": "\U0001f4dd *Recording Credit Purchase*\n\nWho did you buy from on credit?\n_Type the supplier\'s name_"}]

        name = self._extract_contact_name_from_text(text, amount)
        if not name:
            self.db.save_session(phone_number, 'RECORDING_DEBT', {'debt_type': 'i_owe', 'step': 'ask_name', 'amount': amount, 'description': text})
            return [{"type": "text", "content": f"\U0001f4dd Got \u20a6{amount:,}. Who did you buy from on credit? _Type their name_"}]

        return self._build_rich_credit_confirmation(phone_number, text, amount, name, 'i_owe')

    def _build_rich_credit_confirmation(self, phone_number, text, amount, name, debt_type):
        """Run text through the full AI categorizer to extract product
        details (brand, color, size, qty, unit price) — same as a normal
        transaction — then show a rich confirmation and save as a debt
        (instead of cash income/expense) once confirmed."""
        user = self.db.get_user(phone_number)
        business_type = user.get('business_type', 'trading') if user else 'trading'
        industry_class = user.get('industry_class', '') if user else ''

        try:
            result = self.categorizer.parse_transaction(text, phone_number, business_type, industry_class)
        except Exception:
            result = {}

        category = result.get('category') or ('Sales & Income' if debt_type == 'owed_to_me' else 'Goods & Stock')
        sub_category = result.get('sub_category', '')
        item_name = result.get('item_name')
        brand = result.get('brand')
        model = result.get('model')
        size = result.get('size')
        color = result.get('color')
        quantity = result.get('quantity')
        unit_cost = result.get('unit_cost')

        # Use AI amount only if close to our extracted amount (sanity check)
        ai_amount = result.get('total_amount')
        if ai_amount and abs(ai_amount - amount) < 100:
            amount = ai_amount

        pending = {
            "amount": amount,
            "name": name,
            "debt_type": debt_type,
            "description": text,
            "category": category,
            "sub_category": sub_category,
            "item_name": item_name,
            "brand": brand,
            "model": model,
            "size": size,
            "color": color,
            "quantity": quantity,
            "unit_cost": unit_cost,
        }
        self.db.save_session(phone_number, 'CONFIRMING_CREDIT_SALE', pending)

        type_emoji = "\U0001f7e2" if debt_type == 'owed_to_me' else "\U0001f7e1"
        cat_emoji = self._get_category_emoji(category)

        msg = "\U0001f4dd Got it! *On Credit*\n\n"
        msg += f"{type_emoji} \u20a6{amount:,}\n"

        if item_name:
            msg += f"\U0001f4e6 {item_name}\n"
        if brand or model:
            brand_line = "\U0001f3f7\ufe0f "
            if brand:
                brand_line += brand
            if brand and model:
                brand_line += " | "
            if model:
                brand_line += model
            msg += brand_line + "\n"

        details = []
        if size:
            details.append(f"Size: {size}")
        if color:
            details.append(f"Color: {color}")
        if quantity:
            details.append(f"Qty: {quantity}")
        if unit_cost:
            details.append(f"Unit: \u20a6{int(unit_cost):,}")
        if details:
            msg += "\U0001f4cb " + " | \u200c".join(details) + "\n"

        msg += f"{cat_emoji} {category}"
        if sub_category:
            msg += f" \u2192 {sub_category}"
        msg += "\n"

        if debt_type == 'owed_to_me':
            msg += f"\U0001f464 *{name}* owes you this on credit\n"
        else:
            msg += f"\U0001f3ea You owe *{name}* this on credit\n"

        msg += "\n\u2705 Correct?"

        return [{"type": "buttons", "content": {
            "body": msg,
            "buttons": [
                {"id": "confirm_yes", "title": "\u2705 Yes"},
                {"id": "confirm_undo", "title": "\u21a9\ufe0f Cancel"},
            ]
        }}]

    def _handle_confirming_credit_sale_state(self, phone_number, text, context):
        """Handle confirmation of a rich credit sale/purchase before saving"""
        text_lower = text.lower().strip()
        import traceback

        if text_lower in ['no', 'cancel', 'confirm_undo', '\u21a9\ufe0f cancel', 'undo']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Cancelled. Credit transaction not saved."}]

        if text_lower in ['yes', 'y', 'confirm_yes', '\u2705 yes', 'correct', 'ok']:
            try:
                amount = context.get('amount', 0)
                name = context.get('name', '')
                debt_type = context.get('debt_type') or 'owed_to_me'
                description = context.get('description', '')

                # Build a clean description from the rich details instead of
                # the raw typed sentence (so debt lists show structured info)
                parts = []
                if context.get('item_name'):
                    parts.append(context['item_name'])
                if context.get('brand'):
                    parts.append(context['brand'])
                if context.get('color'):
                    parts.append(f"({context['color']})")
                if context.get('quantity'):
                    parts.append(f"x{context['quantity']}")
                clean_description = ' '.join(str(p) for p in parts) if parts else description

                self.db.record_debt(
                    phone_number, name, int(amount), debt_type,
                    description=clean_description
                )

                # Auto-update catalog from credit sale too
                catalog_context = {
                    'type': 'income' if debt_type == 'owed_to_me' else 'expense',
                    'amount': int(amount),
                    'vendor': name,
                    'item_name': context.get('item_name'),
                    'brand': context.get('brand'),
                    'model': context.get('model'),
                    'quantity': context.get('quantity'),
                    'unit_cost': context.get('unit_cost'),
                    'size': context.get('size'),
                    'color': context.get('color'),
                    'category': context.get('category', 'Sales & Income'),
                    'sub_category': context.get('sub_category', ''),
                }
                self._update_auto_catalog(phone_number, catalog_context)

                if debt_type == 'owed_to_me':
                    # Credit SALE — offer invoice/receipt buttons
                    self.db.save_session(phone_number, STATE_IDLE, {'last_saved_vendor': name, 'last_saved_amount': int(amount)})
                    msg = f"\u2705 *Credit Sale Recorded*\n\n\U0001f464 *{name}* owes you *\u20a6{int(amount):,}*\n"
                    if clean_description and clean_description != description:
                        msg += f"_{clean_description}_\n"
                    msg += "\nGenerate a document?"
                    return [{"type": "buttons", "content": {
                        "body": msg,
                        "buttons": [
                            {"id": "post_invoice", "title": "\U0001f4c4 Invoice"},
                            {"id": "post_receipt", "title": "\U0001f9fe Receipt"},
                            {"id": "post_done", "title": "\u2705 Done"},
                        ]
                    }}]
                else:
                    self.db.save_session(phone_number, STATE_IDLE, {})
                    msg = f"\u2705 *Credit Purchase Recorded*\n\n\U0001f3ea You owe *{name}* \u20a6{int(amount):,}\n"
                    if clean_description and clean_description != description:
                        msg += f"_{clean_description}_\n"
                    msg += "\n_Type \"i owe\" to see all your debts_"
                    return [{"type": "text", "content": msg}]

            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"CREDIT CONFIRM ERROR: {e}\n{tb}")
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": f"\u274c Credit save failed:\n_{str(e)[:100]}_\n\nPlease try again."}]

        return [{"type": "text", "content": "Please reply *yes* to confirm or *cancel* to discard."}]

    def _handle_debt_paid(self, phone_number, text):
        """Handle when a customer pays their debt"""
        amount = self._extract_amount_from_text(text, strict=True)
        name = self._extract_contact_name_from_text(text, amount)

        if not name or amount == 0:
            self.db.save_session(phone_number, 'RECORDING_DEBT', {
                'debt_type': 'settling_owed_to_me', 'step': 'ask_name' if not name else 'ask_amount', 'name': name or ''
            })
            if not name:
                return [{"type": "text", "content": "Who paid you? _Type their name_"}]
            else:
                return [{"type": "text", "content": f"How much did *{name}* pay?"}]

        remaining = self.db.settle_debt(phone_number, name, amount, 'owed_to_me')
        self.db.save_transaction(phone_number, amount, 'income', f"Debt payment from {name}", 'Sales Revenue', vendor=name)
        self._handle_debt_payment_update(phone_number, name, amount)
        self.db.save_session(phone_number, STATE_IDLE, {})

        from datetime import datetime
        today = datetime.now().strftime('%d %b %Y')
        if remaining == 0:
            msg = (
                f"\u2705 *Payment Received!*\n\n"
                f"\U0001f464 {name} paid \u20a6{amount:,}\n"
                f"\U0001f4c5 Date: {today}\n"
                f"\U0001f7e2 *Debt fully cleared!*"
            )
        else:
            msg = (
                f"\u2705 *Payment Received!*\n\n"
                f"\U0001f464 {name} paid \u20a6{amount:,}\n"
                f"\U0001f4c5 Date: {today}\n"
                f"\U0001f4ca Remaining balance: *\u20a6{remaining:,}*"
            )
        return [{"type": "text", "content": msg}]

    def _handle_i_paid_debt(self, phone_number, text):
        """Handle when user pays off their own debt"""
        amount = self._extract_amount_from_text(text, strict=True)
        name = self._extract_contact_name_from_text(text, amount)

        if not name or amount == 0:
            self.db.save_session(phone_number, 'RECORDING_DEBT', {
                'debt_type': 'settling_i_owe', 'step': 'ask_name' if not name else 'ask_amount', 'name': name or ''
            })
            if not name:
                return [{"type": "text", "content": "Who did you pay? _Type their name_"}]
            else:
                return [{"type": "text", "content": f"How much did you pay *{name}*?"}]

        remaining = self.db.settle_debt(phone_number, name, amount, 'i_owe')
        self.db.save_transaction(phone_number, amount, 'expense', f"Debt payment to {name}", 'Goods & Stock', vendor=name)
        self.db.save_session(phone_number, STATE_IDLE, {})

        if remaining == 0:
            msg = f"\u2705 *Payment Made!*\n\n\U0001f3ea You paid *{name}* \u20a6{amount:,}\n\U0001f7e2 Debt fully cleared!"
        else:
            msg = f"\u2705 *Payment Made!*\n\n\U0001f3ea You paid *{name}* \u20a6{amount:,}\n\U0001f4ca You still owe: *\u20a6{remaining:,}*"
        return [{"type": "text", "content": msg}]

    def _handle_contact_profile(self, phone_number, text):
        """Show a full contact profile"""
        skip_words = {'profile', 'contact', 'info', 'show', 'tell', 'me', 'about', 'for'}
        words = [w for w in text.split() if w.lower() not in skip_words]
        name = ' '.join(words).strip()

        if not name:
            return [{"type": "text", "content": "Who do you want to see? _Type: profile [name]_"}]

        contact = self.db.get_contact_by_name(phone_number, name)
        if not contact:
            return [{"type": "text", "content": f"\u2753 No contact found for *{name}*."}]

        total_received = int(contact.get('total_received', 0))
        total_paid = int(contact.get('total_paid', 0))
        tx_count = int(contact.get('transaction_count', 0))
        debt_owed = int(contact.get('debt_owed_to_me', 0))
        debt_i_owe = int(contact.get('debt_i_owe', 0))
        last_date = contact.get('last_transaction_date', 'N/A')
        notes = contact.get('notes', '')
        contact_type = contact.get('type', 'contact').title()

        msg = f"\U0001f464 *{contact.get('name', name)}* ({contact_type})\n\n"
        msg += f"\U0001f4b0 Total business: \u20a6{total_received + total_paid:,}\n"
        msg += f"\U0001f4cb Transactions: {tx_count}\n"
        msg += f"\U0001f4c5 Last activity: {last_date}\n"
        if debt_owed:
            msg += f"\U0001f534 Owes you: \u20a6{debt_owed:,}\n"
        if debt_i_owe:
            msg += f"\U0001f7e1 You owe them: \u20a6{debt_i_owe:,}\n"
        if notes:
            msg += f"\U0001f4dd Note: _{notes}_\n"
        msg += "\n_Type \"add note [name] [note]\" to add a note_"

        # Show payment history if available
        payment_history = contact.get('payment_history', [])
        if payment_history:
            msg += "\n\n\U0001f4b3 *Recent Payments:*\n"
            for p in payment_history[-3:]:  # last 3
                msg += f"  • \u20a6{int(p.get('amount',0)):,} on {p.get('date','')}\n"

        last_payment_date = contact.get('last_payment_date', '')
        last_payment_amount = int(contact.get('last_payment_amount', 0))
        if last_payment_date and not payment_history:
            msg += f"\n\U0001f4c5 Last payment: \u20a6{last_payment_amount:,} on {last_payment_date}"

        return [{"type": "text", "content": msg}]

    def _handle_add_note(self, phone_number, text):
        """Add a note to a contact"""
        contacts = self.db.get_contacts(phone_number)
        matched_name = None
        note = ''
        clean_text = text.lower().replace('add note', '').replace('note for', '').replace('note about', '').strip()

        for c in contacts:
            cname = c.get('name', '')
            if cname.lower() in clean_text.lower():
                matched_name = cname
                note = clean_text.lower().replace(cname.lower(), '').strip()
                break

        if not matched_name:
            return [{"type": "text", "content": "\u2753 Could not find that contact.\n\n_Format: add note [name] [your note]_\n_Example: add note Alhaji pays cash only_"}]

        self.db.update_contact_note(phone_number, matched_name, note)
        return [{"type": "text", "content": f"\u2705 Note saved for *{matched_name}*:\n_{note}_"}]

    def _extract_amount_from_text(self, text, strict=False):
        """Extract the AMOUNT from transaction text — prioritizes numbers
        near currency markers, 'worth', 'for', or the largest number found.
        This avoids grabbing quantities like '5' in 'Sold 5 socks for 25000'.

        If strict=True, only returns an amount when there's an explicit
        price signal (₦, #, 'worth', 'for', 'is', 'of'). This is used for
        debt recording where we'd rather ask the user than guess wrong
        (e.g. 'Sold 2 pairs of socks to Femnix on credit' has no real
        amount — the '2' is a quantity, not a price)."""
        text_clean = text.replace(',', '')

        # Priority 1: number directly after ₦/# or before/after "worth"/"for"
        priority_patterns = [
            r'(?:worth|for|is|of)\s*[\u20a6#]?\s*(\d+)(k|K)?\b',
            r'(?:paid|pay|received|settled|cleared)\s+(?:\w+\s+){0,2}[\u20a6#]?\s*(\d{4,})(k|K)?\b',
            r'(?:paid|pay|received|settled|cleared)\s+(?:\w+\s+){0,2}[\u20a6#]?\s*(\d+)(k|K)\b',
            r'[\u20a6#]\s*(\d+)(k|K)?\b',
        ]
        for pattern in priority_patterns:
            match = re.search(pattern, text_clean, re.IGNORECASE)
            if match:
                amt_str = match.group(1)
                suffix = match.group(2)
                amt = int(amt_str)
                if suffix and suffix.lower() == 'k':
                    amt *= 1000
                if amt > 0:
                    return amt

        if strict:
            # No explicit price signal found — don't guess from quantities
            return 0

        # Priority 2: if multiple numbers exist, pick the LARGEST
        # (quantities are usually small, prices are usually larger)
        all_numbers = re.findall(r'(\d+)(k|K)?\b', text_clean)
        if all_numbers:
            amounts = []
            for amt_str, suffix in all_numbers:
                amt = int(amt_str)
                if suffix and suffix.lower() == 'k':
                    amt *= 1000
                amounts.append(amt)
            if amounts:
                return max(amounts)

        return 0

    def _extract_contact_name_from_text(self, text, amount=0):
        """Extract contact name from transaction text.
        Looks for the word right after 'to'/'from'/'by' first (most reliable),
        falls back to filtering out known noise words including product terms."""

        # Priority 1: name right after "to X" or "from X" (most reliable signal)
        to_from_match = re.search(r'\b(?:to|from)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})', text)
        if to_from_match:
            candidate = to_from_match.group(1).strip()
            # Make sure it's not immediately followed by "for" being mistaken as part of name
            return candidate.title()

        # Priority 2: fallback — remove numbers, known noise words, and known
        # product/catalog words, then take the remaining capitalized words
        clean = re.sub(r'[\u20a6#]?\s*\d+(?:k|K)?', '', text)
        skip_words = {
            'sold', 'bought', 'paid', 'received', 'gave', 'took', 'from', 'to',
            'on', 'credit', 'for', 'worth', 'goods', 'items', 'the', 'a', 'an',
            'and', 'i', 'me', 'my', 'naira', 'cash', 'transfer', 'bank', 'by',
            'sale', 'purchase', 'debt', 'owing', 'owes', 'has', 'with', 'of',
            'in', 'at', 'is', 'was', 'owe', 'that', 'this', 'their', 'record',
            'add', 'note', 'profile', 'contact', 'info', 'show', 'pairs',
            'pieces', 'units', 'each', 'per', 'total', 'pcs', 'piece',
        }
        words = [w.strip('.,!?') for w in clean.split() if w.strip('.,!?').lower() not in skip_words and len(w) > 1]
        if words:
            return ' '.join(words[:3]).title()
        return None

    def _handle_recording_debt_state(self, phone_number, text, context):
        """Handle multi-step debt recording flow"""
        step = context.get('step', '')
        debt_type = context.get('debt_type', 'owed_to_me')
        text_lower = text.lower().strip()

        if text_lower in ['cancel', 'exit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Cancelled."}]

        if step == 'ask_name':
            name = text.strip().title()
            amount = context.get('amount', 0)
            if amount:
                # We already have amount, save now
                self.db.record_debt(phone_number, name, amount, debt_type, description=context.get('description', ''))
                self.db.save_session(phone_number, STATE_IDLE, {})
                if debt_type == 'owed_to_me':
                    return [{"type": "text", "content": f"\u2705 *Credit Sale Recorded*\n\n\U0001f464 {name} owes you *\u20a6{amount:,}*"}]
                else:
                    return [{"type": "text", "content": f"\u2705 *Credit Purchase Recorded*\n\n\U0001f3ea You owe *{name}* \u20a6{amount:,}"}]
            else:
                # Need amount next
                context['name'] = name
                context['step'] = 'ask_amount'
                self.db.save_session(phone_number, 'RECORDING_DEBT', context)
                if debt_type in ['settling_owed_to_me', 'settling_i_owe']:
                    return [{"type": "text", "content": f"How much did *{name}* {'pay you' if debt_type == 'settling_owed_to_me' else 'receive from you'}? _Type the amount_"}]
                return [{"type": "text", "content": f"How much did *{name}* take on credit? _Type the amount_"}]

        elif step == 'ask_amount':
            # User is directly answering "how much?" — don't use strict mode
            amount = self._extract_amount_from_text(text, strict=False)
            name = context.get('name', '')
            if amount == 0:
                return [{"type": "text", "content": "Please type a valid amount. _Example: 15000_"}]

            if debt_type == 'settling_owed_to_me':
                remaining = self.db.settle_debt(phone_number, name, amount, 'owed_to_me')
                self.db.save_transaction(phone_number, amount, 'income', f"Debt payment from {name}", 'Sales Revenue', vendor=name)
                self._handle_debt_payment_update(phone_number, name, amount)
                self.db.save_session(phone_number, STATE_IDLE, {})
                msg = f"\u2705 {name} paid \u20a6{amount:,}. " + (f"Debt cleared!" if remaining == 0 else f"Remaining: \u20a6{remaining:,}")
                return [{"type": "text", "content": msg}]

            elif debt_type == 'settling_i_owe':
                remaining = self.db.settle_debt(phone_number, name, amount, 'i_owe')
                self.db.save_transaction(phone_number, amount, 'expense', f"Debt payment to {name}", 'Goods & Stock', vendor=name)
                self.db.save_session(phone_number, STATE_IDLE, {})
                msg = f"\u2705 You paid {name} \u20a6{amount:,}. " + (f"Debt cleared!" if remaining == 0 else f"You still owe: \u20a6{remaining:,}")
                return [{"type": "text", "content": msg}]

            else:
                self.db.record_debt(phone_number, name, amount, debt_type)
                self.db.save_session(phone_number, STATE_IDLE, {})
                if debt_type == 'owed_to_me':
                    return [{"type": "text", "content": f"\u2705 {name} owes you \u20a6{amount:,}"}]
                else:
                    return [{"type": "text", "content": f"\u2705 You owe {name} \u20a6{amount:,}"}]

        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": "\u2753 Something went wrong. Please try again."}]

    def _handle_redo(self, phone_number):
        """Restore the last deleted transaction"""
        user = self.db.get_user(phone_number)
        last_deleted = user.get('last_deleted_transaction') if user else None
        if not last_deleted:
            return [{"type": "text", "content": "\u2753 Nothing to restore. No recently deleted transaction found."}]
        try:
            self.db.transactions.put_item(Item=self.db._sanitize_for_dynamo(last_deleted))
            self.db.update_user(phone_number, {'last_deleted_transaction': None})
            amount = int(last_deleted.get('amount', 0))
            cat = last_deleted.get('category', '')
            return [{"type": "text", "content": f"\u21a9\ufe0f *Restored!* \u20a6{amount:,} ({cat}) has been brought back."}]
        except Exception as e:
            return [{"type": "text", "content": "\u274c Could not restore. Please re-enter manually."}]

    def _handle_reg_attr_suggest(self, phone_number, text, context):
        """Handle user response to attribute suggestion prompt"""
        text_lower = text.lower().strip()
        suggestions = context.get('pending_suggestions', [])
        suggestion_values = context.get('pending_suggestion_values', {})
        products = context.get('products', [])
        p_idx = int(context.get('p_idx', 0))
        current_product = products[p_idx] if p_idx < len(products) else ''
        subcategories = context.get('subcategories', [])
        sub_idx = int(context.get('sub_idx', 0))
        current_sub = subcategories[sub_idx] if sub_idx < len(subcategories) else ''
        phase = context.get('phase', 'product_attributes')
        current_target = context.get('current_target', '')

        if text_lower in ['cancel', 'exit', 'quit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Canceled."}]

        def save_and_proceed(attrs, values_map):
            for attr in attrs:
                vals = values_map.get(attr, [])
                if phase == 'series_attributes' and current_sub:
                    self.db.set_attributes(phone_number, current_product, attr, vals, subcategory=current_sub, series=current_target)
                elif phase == 'sub_attributes' and current_sub:
                    self.db.set_attributes(phone_number, current_product, attr, vals, subcategory=current_sub)
                else:
                    self.db.set_attributes(phone_number, current_product, attr, vals)
            context['attrs_to_fill'] = list(attrs)
            context['attr_fill_idx'] = 0
            context['pending_suggestions'] = []
            context['pending_suggestion_values'] = {}
            display = current_target if (phase == 'series_attributes' and current_target) else (current_sub if current_sub else current_product)
            if attrs:
                first_attr = list(attrs)[0]
                self.db.save_session(phone_number, STATE_REG_ATTR_VALUES, context)
                return [{"type": "text", "content": f"\ud83d\udcdd What *{first_attr}* values for *{display}*?\n\n_List separated by commas, or 'skip'._"}]
            else:
                target_name = current_sub if current_sub else current_product
                self.db.save_session(phone_number, STATE_REG_CONVERSIONS, context)
                return [{"type": "text", "content": f"\ud83d\udd04 Any unit conversions for *{target_name}*?\n\n_Type 'skip' if none._"}]

        if text_lower in ['1', 'yes', 'same', 'use same', 'use all', 'all']:
            return save_and_proceed(suggestions, suggestion_values)
        elif text_lower in ['2', 'pick', 'pick some', 'choose']:
            attr_list = '\n'.join([f"{i+1}. {a}" for i, a in enumerate(suggestions)])
            context['suggestion_mode'] = 'picking'
            self.db.save_session(phone_number, STATE_REG_ATTR_SUGGEST, context)
            return [{"type": "text", "content": f"Which attributes?\n\n{attr_list}\n\n_Type numbers e.g. 1,3_"}]
        elif text_lower in ['3', 'no', 'new', 'fresh', 'define new']:
            display = current_sub if current_sub else current_product
            context['pending_suggestions'] = []
            self.db.save_session(phone_number, STATE_REG_ATTRIBUTES, context)
            return [{"type": "text", "content": f"What attributes for *{display}*?\n\n_Examples: size, color, material. Type 'skip' for none._"}]
        elif text_lower in ['4', 'add', 'add more']:
            display = current_sub if current_sub else current_product
            context['suggestion_mode'] = 'adding'
            context['base_attributes'] = suggestions
            context['base_values'] = suggestion_values
            self.db.save_session(phone_number, STATE_REG_ATTR_SUGGEST, context)
            return [{"type": "text", "content": f"Already have: _{ ', '.join(suggestions)}_\n\nWhat extra attributes for *{display}*?"}]
        elif context.get('suggestion_mode') == 'picking':
            chosen = []
            for part in [p.strip() for p in text.split(',')]:
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(suggestions):
                        chosen.append(suggestions[idx])
                else:
                    for s in suggestions:
                        if s.lower() == part.lower():
                            chosen.append(s)
            if not chosen:
                return [{"type": "text", "content": "\u2753 Couldn't match. Type numbers e.g. 1,3"}]
            return save_and_proceed(chosen, {k: suggestion_values.get(k, []) for k in chosen})
        elif context.get('suggestion_mode') == 'adding':
            base = context.get('base_attributes', [])
            base_vals = context.get('base_values', {})
            extra = [a.strip().lower() for a in text.split(',') if a.strip()]
            all_attrs = base + [a for a in extra if a not in base]
            return save_and_proceed(all_attrs, base_vals)
        else:
            attrs = [a.strip().lower() for a in text.split(',') if a.strip()]
            if attrs:
                return save_and_proceed(attrs, {})
            return [{"type": "text", "content": "Please reply with 1, 2, 3, or 4."}]

    def _handle_remind_debtor(self, phone_number, text):
        """Send a WhatsApp reminder to a specific debtor"""
        skip = {'remind', 'send', 'reminder', 'chase', 'follow', 'up', 'ping', 'message', 'debtor', 'about', 'his', 'her', 'their', 'debt'}
        words = [w.strip('.,!?') for w in text.split() if w.lower().strip('.,!?') not in skip]
        name = ' '.join(words[:3]).strip().title() if words else ''

        if not name:
            debtors = self.db.get_all_debtors(phone_number)
            if not debtors:
                return [{"type": "text", "content": "\u2705 Nobody owes you money right now."}]
            lines = "\n".join([f"{i+1}. {d['name']} — \u20a6{d['amount']:,}" for i, d in enumerate(debtors[:8])])
            self.db.save_session(phone_number, 'REMINDING_DEBTOR', {'debtors': [d['name'] for d in debtors[:8]]})
            return [{"type": "text", "content": f"Who do you want to remind?\n\n{lines}\n\n_Type the number or name_"}]

        contact = self.db.get_contact_by_name(phone_number, name)
        debt_amount = int(contact.get('debt_owed_to_me', 0)) if contact else 0

        if not contact or debt_amount == 0:
            # Try fuzzy match against known debtors first
            debtors = self.db.get_all_debtors(phone_number)
            matches = [d for d in debtors if name.lower() in d['name'].lower()]
            if matches:
                name = matches[0]['name']
                contact = self.db.get_contact_by_name(phone_number, name)
                debt_amount = matches[0]['amount']
            elif contact:
                # Contact exists but has no debt — still allow a general reminder
                debt_amount = 0
            else:
                return [{"type": "text", "content": f"\u2753 No contact found named *{name}*.\n\n_Type \"who owes me\" to see your debtor list, or \"customers\" to see all contacts._"}]

        debtor_phone = contact.get('contact_phone', '') if contact else ''

        if not debtor_phone:
            self.db.save_session(phone_number, 'REMINDING_DEBTOR', {
                'debtor_name': name, 'debt_amount': debt_amount, 'step': 'ask_phone'
            })
            return [{"type": "text", "content": (
                f"\U0001f4f1 I need *{name}\'s* WhatsApp number to send the reminder.\n\n"
                f"Type their number with country code:\n_Example: 2348012345678_\n\n"
                f"_Or type \"skip\" to get reminder text to send yourself._"
            )}]

        return self._send_debt_reminder(phone_number, name, debtor_phone, debt_amount)

    def _handle_remind_all_debtors(self, phone_number):
        """Send WhatsApp reminders to all debtors who have phone numbers saved"""
        debtors = self.db.get_all_debtors(phone_number)
        if not debtors:
            return [{"type": "text", "content": "\u2705 Nobody owes you money right now."}]

        sent = []
        no_phone = []
        for d in debtors:
            contact = self.db.get_contact_by_name(phone_number, d['name'])
            debtor_phone = contact.get('contact_phone', '') if contact else ''
            if debtor_phone:
                self._send_debt_reminder(phone_number, d['name'], debtor_phone, d['amount'])
                sent.append(d['name'])
            else:
                no_phone.append(d['name'])

        msg = ""
        if sent:
            msg += "\u2705 *Reminders sent to:*\n" + "\n".join([f"  • {n}" for n in sent])
        if no_phone:
            msg += f"\n\n\u26a0\ufe0f *No number saved for:*\n" + "\n".join([f"  • {n}" for n in no_phone])
            msg += "\n\n_To add: say \"save number [name] [phone]\"_"
        if not sent:
            msg = (
                "\u26a0\ufe0f None of your debtors have phone numbers saved.\n\n"
                "To add: *save number [name] [phone]*\n"
                "Example: _save number Bola 2348012345678_"
            )
        return [{"type": "text", "content": msg}]

    def _send_debt_reminder(self, owner_phone, debtor_name, debtor_phone, amount):
        """Send the WhatsApp reminder message to the debtor"""
        from services.whatsapp_client import WhatsAppClient
        user = self.db.get_user(owner_phone)
        business_name = user.get('business_name', 'your supplier') if user else 'your supplier'
        if amount and amount > 0:
            reminder_text = (
                f"Hello {debtor_name},\n\n"
                f"This is a friendly reminder from *{business_name}* "
                f"that you have an outstanding balance of *\u20a6{amount:,}*.\n\n"
                f"Please make payment at your earliest convenience.\n\nThank you! \U0001f64f"
            )
        else:
            reminder_text = (
                f"Hello {debtor_name},\n\n"
                f"This is a message from *{business_name}*. "
                f"Thank you for being a valued customer! \U0001f64f"
            )
        try:
            whatsapp = WhatsAppClient()
            sent = whatsapp.send_text(debtor_phone, reminder_text)
            if sent:
                return [{"type": "text", "content": (
                    f"\u2705 *Reminder sent to {debtor_name}!*\n\n"
                    f"_{reminder_text[:100]}..._"
                )}]
            else:
                return [{"type": "text", "content": f"\u274c Failed to send to {debtor_name}. Try again later."}]
        except Exception as e:
            return [{"type": "text", "content": (
                f"\u26a0\ufe0f Could not send automatically. Copy and send manually:\n\n{reminder_text}"
            )}]

    def _handle_save_contact_phone(self, phone_number, text):
        """Save a phone number for a contact e.g. 'save number Bola 2348012345678' """
        # Search for phone number in original text (don't strip all spaces)
        phone_match = re.search(r'(\d[\d\s]{9,15}\d)', text)
        if not phone_match:
            return [{"type": "text", "content": "\u2753 No valid number found.\n\n_Format: save number [name] [phone]_\n_Example: save number Bola 2348012345678_"}]
        contact_phone = phone_match.group(1).replace(' ', '')
        if len(contact_phone) < 10 or len(contact_phone) > 14:
            return [{"type": "text", "content": "\u2753 No valid number found.\n\n_Format: save number [name] [phone]_\n_Example: save number Bola 2348012345678_"}]
        name_text = text.lower()
        for word in ['save', 'number', 'add', 'phone', 'for', contact_phone]:
            name_text = name_text.replace(word.lower(), '')
        name = name_text.strip().title()
        if not name:
            return [{"type": "text", "content": "\u2753 Please include the contact name.\n_Example: save number Bola 2348012345678_"}]
        contact_id = name.strip().lower().replace(' ', '_')
        try:
            self.db.contacts.update_item(
                Key={'phone_number': phone_number, 'contact_id': contact_id},
                UpdateExpression="SET contact_phone = :p, #n = if_not_exists(#n, :name)",
                ExpressionAttributeNames={'#n': 'name'},
                ExpressionAttributeValues={':p': contact_phone, ':name': name}
            )
            return [{"type": "text", "content": (
                f"\u2705 *Number saved for {name}*\n\n"
                f"Phone: {contact_phone}\n\n"
                f"_Now type: \"remind {name}\" to send them a reminder!_"
            )}]
        except Exception as e:
            return [{"type": "text", "content": "\u274c Could not save number. Please try again."}]

    def _handle_reminding_debtor_state(self, phone_number, text, context):
        """Multi-step debt reminder flow"""
        step = context.get('step', '')
        text_lower = text.lower().strip()

        if text_lower in ['cancel', 'stop', 'exit']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Cancelled."}]

        if step == 'ask_phone':
            debtor_name = context.get('debtor_name', '')
            debt_amount = context.get('debt_amount', 0)
            if text_lower == 'skip':
                user = self.db.get_user(phone_number)
                business_name = user.get('business_name', 'your supplier') if user else 'your supplier'
                reminder_text = (
                    f"Hello {debtor_name},\n\nThis is a friendly reminder from *{business_name}* "
                    f"that you have an outstanding balance of *\u20a6{debt_amount:,}*.\n\nThank you!"
                )
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": f"Copy and send this:\n\n{reminder_text}"}]
            phone_match = re.search(r'\b(\d{10,14})\b', text.replace(' ', ''))
            if not phone_match:
                return [{"type": "text", "content": "\u2753 Please enter a valid number (10-14 digits) or type \"skip\"."}]
            debtor_phone = phone_match.group(1)
            contact_id = debtor_name.strip().lower().replace(' ', '_')
            try:
                self.db.contacts.update_item(
                    Key={'phone_number': phone_number, 'contact_id': contact_id},
                    UpdateExpression="SET contact_phone = :p",
                    ExpressionAttributeValues={':p': debtor_phone}
                )
            except Exception:
                pass
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._send_debt_reminder(phone_number, debtor_name, debtor_phone, debt_amount)

        else:
            debtors = context.get('debtors', [])
            name = ''
            if text.strip().isdigit():
                idx = int(text.strip()) - 1
                if 0 <= idx < len(debtors):
                    name = debtors[idx]
            else:
                name = text.strip().title()
            if not name:
                return [{"type": "text", "content": "\u2753 Please type the number or name from the list."}]
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_remind_debtor(phone_number, f"remind {name}")

    def _get_catalog_product_names(self, phone_number):
        """Get list of product names from user's catalog for matching"""
        try:
            user = self.db.get_user(phone_number)
            catalog = user.get('product_catalog', {}) if user else {}
            return list(catalog.get('products', {}).keys())
        except Exception:
            return []

    def _handle_breakdown_state(self, phone_number, text, context):
        """Handle breakdown request for vague transaction descriptions"""
        text_lower = text.lower().strip()
        original_text = context.get('original_text', '')
        amount = context.get('amount', 0)

        if text_lower in ['cancel', 'exit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Cancelled."}]

        if text_lower == 'skip':
            # Process original text as-is
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_transaction(phone_number, original_text)

        # User provided breakdown - process with the specific details
        self.db.save_session(phone_number, STATE_IDLE, {})
        # Combine original context with breakdown
        enriched_text = f"{text} worth ₦{amount:,}" if amount and str(amount) not in text else text
        return self._handle_transaction(phone_number, enriched_text)

    def _handle_debt_payment_update(self, phone_number, contact_name, amount, payment_date=None):
        """Record a debt payment with date and update contact history"""
        from datetime import datetime
        payment_date = payment_date or datetime.now().strftime('%Y-%m-%d')
        contact_id = contact_name.strip().lower().replace(' ', '_')

        # Add payment to contact's payment history
        try:
            contact = self.db.get_contact_by_name(phone_number, contact_name)
            history = contact.get('payment_history', []) if contact else []
            history.append({
                'amount': int(amount),
                'date': payment_date,
                'type': 'received'
            })
            # Keep last 20 payments
            history = history[-20:]

            self.db.contacts.update_item(
                Key={'phone_number': phone_number, 'contact_id': contact_id},
                UpdateExpression="SET payment_history = :h, last_payment_date = :d, last_payment_amount = :a",
                ExpressionAttributeValues={
                    ':h': history,
                    ':d': payment_date,
                    ':a': int(amount)
                }
            )
        except Exception as e:
            pass  # Non-critical, don't crash

    def _handle_contact_catalog(self, phone_number):
        """Show full contact catalog with summary stats"""
        contacts = self.db.get_contacts(phone_number, limit=100)
        if not contacts:
            return [{"type": "text", "content": (
                "\U0001f4cb No contacts yet.\n\n"
                "_Contacts are created automatically when you record transactions._"
            )}]

        customers = [c for c in contacts if c.get('type') in ['customer', 'both']]
        suppliers = [c for c in contacts if c.get('type') in ['supplier', 'both']]
        total_receivable = sum(int(c.get('debt_owed_to_me', 0)) for c in contacts)
        total_payable = sum(int(c.get('debt_i_owe', 0)) for c in contacts)

        msg = f"\U0001f4d2 *Contact Catalog*\n\n"
        msg += f"\U0001f464 Customers: {len(customers)} | \U0001f3ea Suppliers: {len(suppliers)}\n"
        if total_receivable:
            msg += f"\U0001f7e2 Owed to you: \u20a6{total_receivable:,}\n"
        if total_payable:
            msg += f"\U0001f534 You owe: \u20a6{total_payable:,}\n"
        msg += "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"

        if customers:
            customers.sort(key=lambda x: int(x.get('total_received', 0)), reverse=True)
            msg += "\n*\U0001f464 Customers:*\n"
            for c in customers[:5]:
                name = c.get('name', 'Unknown')
                total = int(c.get('total_received', 0))
                tx = int(c.get('transaction_count', 0))
                debt = int(c.get('debt_owed_to_me', 0))
                msg += f"  • *{name}* — \u20a6{total:,} ({tx} orders)"
                if debt:
                    msg += f" | owes \u20a6{debt:,}"
                msg += "\n"

        if suppliers:
            suppliers.sort(key=lambda x: int(x.get('total_paid', 0)), reverse=True)
            msg += "\n*\U0001f3ea Suppliers:*\n"
            for s in suppliers[:5]:
                name = s.get('name', 'Unknown')
                total = int(s.get('total_paid', 0))
                tx = int(s.get('transaction_count', 0))
                debt = int(s.get('debt_i_owe', 0))
                msg += f"  • *{name}* — \u20a6{total:,} ({tx} orders)"
                if debt:
                    msg += f" | you owe \u20a6{debt:,}"
                msg += "\n"

        msg += "\n_Type \"profile [name]\" for full details_"
        msg += "\n_Type \"top customers\" for rankings_"
        return [{"type": "text", "content": msg}]

    def _handle_top_contacts(self, phone_number, contact_type):
        """Show top contacts ranked by lifetime value"""
        top = self.db.get_top_contacts(phone_number, contact_type, limit=10)
        if not top:
            return [{"type": "text", "content": f"\u2753 No {contact_type}s found yet."}]

        label = "Customers" if contact_type == "customer" else "Suppliers"
        field = "total_received" if contact_type == "customer" else "total_paid"
        msg = f"\U0001f3c6 *Top {label}*\n\n"

        for i, c in enumerate(top, 1):
            name = c.get('name', 'Unknown')
            total = int(c.get(field, 0))
            tx_count = int(c.get('transaction_count', 0))
            avg = int(c.get('avg_order_value', 0))
            last = c.get('last_transaction_date', '')
            debt = int(c.get('debt_owed_to_me', 0)) if contact_type == 'customer' else int(c.get('debt_i_owe', 0))

            msg += f"{i}. *{name}*\n"
            msg += f"   \u20a6{total:,} | {tx_count} orders"
            if avg:
                msg += f" | avg \u20a6{avg:,}"
            if last:
                msg += f" | last: {last}"
            if debt:
                msg += f" | \U0001f534 \u20a6{debt:,}"
            msg += "\n\n"

        msg += "_Type \"profile [name]\" for full details_"
        return [{"type": "text", "content": msg}]

    def _handle_inactive_contacts(self, phone_number):
        """Show contacts who haven't transacted in 30+ days"""
        inactive = self.db.get_inactive_contacts(phone_number, days=30)
        if not inactive:
            return [{"type": "text", "content": "\u2705 All contacts active in the last 30 days!"}]

        msg = "\U0001f550 *Inactive Contacts (30+ days)*\n\n"
        for c in inactive[:10]:
            name = c.get('name', 'Unknown')
            last = c.get('last_transaction_date', '')
            total = int(c.get('total_received', 0))
            phone = c.get('contact_phone', '')
            try:
                from datetime import datetime
                days = (datetime.now() - datetime.strptime(last, '%Y-%m-%d')).days
                days_str = f"{days} days ago"
            except Exception:
                days_str = last
            msg += f"• *{name}* — last seen {days_str}\n"
            if total:
                msg += f"  Lifetime: \u20a6{total:,}\n"
            msg += "\n"
        msg += "_Type \"remind [name]\" to reach out_"
        return [{"type": "text", "content": msg}]

    def _handle_set_credit_terms(self, phone_number, text):
        """Set credit limit and days for a contact"""
        amount_match = re.search(r'\b(\d+)\b', text)
        days_match = re.search(r'(\d+)\s*days?', text.lower())
        skip = {'set', 'credit', 'limit', 'for', 'terms', 'days', 'day', 'to', 'at', 'of'}
        words = [w.strip('.,') for w in text.split() if w.strip('.,').lower() not in skip and not w.strip('.,').isdigit()]
        name = ' '.join(words[:3]).strip().title()

        if not name:
            self.db.save_session(phone_number, 'SETTING_CREDIT_TERMS', {'step': 'ask_name'})
            return [{"type": "text", "content": "Who do you want to set credit terms for?"}]

        credit_limit = int(amount_match.group(1)) if amount_match else 0
        credit_days = int(days_match.group(1)) if days_match else 0

        if not credit_limit:
            self.db.save_session(phone_number, 'SETTING_CREDIT_TERMS', {'step': 'ask_limit', 'name': name})
            return [{"type": "text", "content": f"What is *{name}\'s* credit limit?\n_Example: 50000 or type \"skip\" for no limit_"}]

        self.db.set_credit_terms(phone_number, name, credit_limit, credit_days)
        self.db.save_session(phone_number, STATE_IDLE, {})
        msg = f"\u2705 *Credit terms set for {name}*\n\n"
        msg += f"\U0001f4b3 Limit: \u20a6{credit_limit:,}\n"
        if credit_days:
            msg += f"\U0001f4c5 Credit days: {credit_days} days\n"
        return [{"type": "text", "content": msg}]

    def _handle_contact_profile(self, phone_number, text):
        """Show a full enriched contact profile"""
        skip_words = {'profile', 'contact', 'info', 'show', 'tell', 'me', 'about', 'for'}
        words = [w for w in text.split() if w.lower() not in skip_words]
        name = ' '.join(words[:3]).strip()

        if not name:
            return [{"type": "text", "content": "Who do you want to see?\n_Type: profile [name]_"}]

        analytics = self.db.get_contact_analytics(phone_number, name)
        if not analytics:
            contacts = self.db.get_contacts(phone_number, limit=100)
            for c in contacts:
                if name.lower() in c.get('name', '').lower():
                    analytics = self.db.get_contact_analytics(phone_number, c.get('name', ''))
                    name = c.get('name', name)
                    break

        if not analytics:
            return [{"type": "text", "content": f"\u2753 No contact found for *{name}*."}]

        ct = analytics.get('type', 'contact').title()
        tr = analytics.get('total_received', 0)
        tp = analytics.get('total_paid', 0)
        tx = analytics.get('transaction_count', 0)
        avg = analytics.get('avg_order_value', 0)
        avg_days = analytics.get('avg_days_between', 0)
        first = analytics.get('first_purchase_date', '')
        last = analytics.get('last_transaction_date', '')
        inactive = analytics.get('days_inactive')
        rel_days = analytics.get('relationship_days')
        debt_owed = analytics.get('debt_owed_to_me', 0)
        debt_mine = analytics.get('debt_i_owe', 0)
        credit_limit = analytics.get('credit_limit', 0)
        credit_days = analytics.get('credit_days', 0)
        notes = analytics.get('notes', '')
        phone = analytics.get('contact_phone', '')
        payment_history = analytics.get('payment_history', [])
        last_pay_date = analytics.get('last_payment_date', '')
        last_pay_amt = analytics.get('last_payment_amount', 0)

        msg = f"\U0001f464 *{name}* ({ct})\n"
        if phone:
            msg += f"\U0001f4f1 {phone}\n"
        msg += "\n"

        msg += "\U0001f4b0 *Financials:*\n"
        if tr:
            msg += f"  Sales to them: \u20a6{tr:,}\n"
        if tp:
            msg += f"  Bought from them: \u20a6{tp:,}\n"
        if avg:
            msg += f"  Avg order: \u20a6{avg:,}\n"
        msg += f"  Orders: {tx}\n"

        if first or last:
            msg += f"\n\U0001f4c5 *Timeline:*\n"
            if first:
                msg += f"  First: {first}\n"
            if last:
                msg += f"  Last: {last}"
                if inactive == 0:
                    msg += " (today)"
                elif inactive == 1:
                    msg += " (yesterday)"
                elif inactive:
                    msg += f" ({inactive} days ago)"
                msg += "\n"
            if rel_days:
                msg += f"  Relationship: {rel_days} days\n"
            if avg_days:
                msg += f"  Buys every ~{avg_days} days\n"

        if debt_owed or debt_mine:
            msg += f"\n\U0001f4ca *Debt:*\n"
            if debt_owed:
                msg += f"  \U0001f534 Owes you: \u20a6{debt_owed:,}\n"
                if credit_limit:
                    msg += f"  Used: \u20a6{debt_owed:,}/\u20a6{credit_limit:,}"
                    if debt_owed > credit_limit:
                        msg += " \u26a0\ufe0f OVER LIMIT!"
                    msg += "\n"
            if debt_mine:
                msg += f"  \U0001f7e1 You owe: \u20a6{debt_mine:,}\n"

        if credit_limit or credit_days:
            msg += f"\n\U0001f4b3 *Credit Terms:*\n"
            if credit_limit:
                msg += f"  Limit: \u20a6{credit_limit:,}\n"
            if credit_days:
                msg += f"  Days: {credit_days}\n"

        if payment_history:
            msg += f"\n\U0001f4b3 *Recent Payments:*\n"
            for p in payment_history[-3:]:
                msg += f"  • \u20a6{int(p.get('amount',0)):,} on {p.get('date','')}\n"
        elif last_pay_date:
            msg += f"\n  Last payment: \u20a6{last_pay_amt:,} on {last_pay_date}\n"

        if notes:
            msg += f"\n\U0001f4dd *Note:* _{notes}_\n"

        first_name = name.split()[0]
        msg += f"\n_\"remind {first_name}\" | \"set credit limit {first_name} 50000\" | \"add note {first_name} ...\"_"
        return [{"type": "text", "content": msg}]

    def _handle_setting_credit_terms_state(self, phone_number, text, context):
        """Multi-step credit terms setting flow"""
        step = context.get('step', '')
        text_lower = text.lower().strip()

        if text_lower in ['cancel', 'stop', 'exit']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "\u274c Cancelled."}]

        if step == 'ask_name':
            name = text.strip().title()
            self.db.save_session(phone_number, 'SETTING_CREDIT_TERMS', {'step': 'ask_limit', 'name': name})
            return [{"type": "text", "content": f"What is *{name}\'s* credit limit?\n_Example: 50000_\n_Type \"skip\" for no limit_"}]

        elif step == 'ask_limit':
            name = context.get('name', '')
            if text_lower == 'skip':
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "text", "content": f"\u2714\ufe0f No credit limit set for {name}."}]
            try:
                limit = int(text.replace(',', '').replace('\u20a6', '').strip())
                self.db.save_session(phone_number, 'SETTING_CREDIT_TERMS', {'step': 'ask_days', 'name': name, 'limit': limit})
                return [{"type": "text", "content": f"How many credit days for *{name}*?\n_Example: 30_\n_Type \"skip\" for none_"}]
            except Exception:
                return [{"type": "text", "content": "Please enter a valid amount. _Example: 50000_"}]

        elif step == 'ask_days':
            name = context.get('name', '')
            limit = context.get('limit', 0)
            days = 0
            if text_lower != 'skip':
                try:
                    days = int(text.strip())
                except Exception:
                    pass
            self.db.set_credit_terms(phone_number, name, limit, days)
            self.db.save_session(phone_number, STATE_IDLE, {})
            msg = f"\u2705 *Credit terms saved for {name}*\n\n"
            msg += f"\U0001f4b3 Limit: \u20a6{limit:,}\n"
            if days:
                msg += f"\U0001f4c5 Credit days: {days} days\n"
            msg += f"\n_Bot will warn you if {name} exceeds their limit._"
            return [{"type": "text", "content": msg}]

        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": "\u2753 Something went wrong. Please try again."}]

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
        """Handle export option selection — dispatches to export_service"""
        from services.export_service import ExportService
        export_service = ExportService(self.db)
        
        text_lower = text.lower().strip()
        
        # Get any saved filter context
        session = self.db.get_session(phone_number)
        ctx = session.get('context', {}) if session else {}
        
        # Cancel/escape
        if text_lower in ('cancel', 'back', 'exit'):
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "👍 Export cancelled."}]

        # Filtered export (Excel or PDF for a specific report)
        if text_lower in ('export_filtered_excel', 'excel', 'spreadsheet'):
            filter_type = ctx.get('filter_type', 'my_sales')
            start = ctx.get('filter_start', '')
            end = ctx.get('filter_end', '')
            period = ctx.get('filter_period', 'This Month')
            self.db.save_session(phone_number, STATE_IDLE, {})
            filepath, filename = export_service.handle_filtered_export(
                phone_number, filter_type, start, end, period, 'excel')
            if filepath:
                return [{"type": "document", "content": {"filepath": filepath, "filename": filename, 
                         "caption": f"📊 {period} — Excel export"}}]
            return [{"type": "text", "content": "⚠️ No data to export for that period."}]

        elif text_lower in ('export_filtered_pdf', 'pdf'):
            filter_type = ctx.get('filter_type', 'my_sales')
            start = ctx.get('filter_start', '')
            end = ctx.get('filter_end', '')
            period = ctx.get('filter_period', 'This Month')
            self.db.save_session(phone_number, STATE_IDLE, {})
            filepath, filename = export_service.handle_filtered_export(
                phone_number, filter_type, start, end, period, 'pdf')
            if filepath:
                return [{"type": "document", "content": {"filepath": filepath, "filename": filename,
                         "caption": f"📄 {period} — PDF export"}}]
            return [{"type": "text", "content": "⚠️ No data to export for that period."}]

        elif text_lower in ('export_month', 'this month', 'this month (excel)', 'full month'):
            # Full month Excel export
            from datetime import datetime
            now = datetime.now()
            start = now.strftime('%Y-%m-01')
            end = now.strftime('%Y-%m-%d')
            period = now.strftime('%B %Y')
            self.db.save_session(phone_number, STATE_IDLE, {})
            filepath, filename = export_service.handle_filtered_export(
                phone_number, 'all', start, end, period, 'excel')
            if filepath:
                return [{"type": "document", "content": {"filepath": filepath, "filename": filename,
                         "caption": f"📊 Full Month ({period}) — Excel"}}]
            return [{"type": "text", "content": "⚠️ No transactions this month."}]

        elif text_lower in ('export_csv', 'full history', 'csv', 'full history (csv)'):
            # Full history as CSV
            self.db.save_session(phone_number, STATE_IDLE, {})
            filepath, filename = export_service.export_full_history_csv(phone_number)
            if filepath:
                return [{"type": "document", "content": {"filepath": filepath, "filename": filename,
                         "caption": "📄 Full transaction history (CSV)"}}]
            return [{"type": "text", "content": "⚠️ No transactions found."}]

        elif text_lower in ('export_contacts', 'contacts list', 'contacts'):
            # Export contacts
            self.db.save_session(phone_number, STATE_IDLE, {})
            filepath, filename = export_service.export_contacts(phone_number)
            if filepath:
                return [{"type": "document", "content": {"filepath": filepath, "filename": filename,
                         "caption": "📖 Your contacts list"}}]
            return [{"type": "text", "content": "⚠️ No contacts to export."}]

        # Unrecognized — show options
        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": (
            "📊 *Export Options:*\n\n"
            "• Type *excel* for spreadsheet\n"
            "• Type *pdf* for printable report\n"
            "• Type *csv* for full history\n\n"
            "_Or tap the options above._"
        )}]

    def _handle_invoice_input(self, phone_number, text, context):
        """Handle invoice details input"""
        text_lower = text.lower().strip()

        if text_lower in ['cancel', 'exit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "❌ Invoice cancelled."}]

        step = context.get('step', 'ask_details')

        if step == 'ask_details':
            # Check for # reference syntax: "#1,3,5" or "1,6,7" (with or without #)
            stripped = text.strip()
            ref_match = re.match(r'^#?([\d,\s]+)$', stripped)
            is_reference = False
            if ref_match:
                # Determine if this is a tx reference or just a number (amount)
                has_hash = stripped.startswith('#')
                has_comma = ',' in stripped
                numbers_parsed = [int(n.strip()) for n in ref_match.group(1).split(',') if n.strip().isdigit()]
                # It's a reference if: has # prefix, has commas, or all numbers are small (≤50)
                is_reference = has_hash or has_comma or (numbers_parsed and max(numbers_parsed) <= 50)
            if is_reference:
                # Get transaction IDs from user profile (saved by "my sales" command)
                user = self.db.get_user(phone_number)
                tx_list = user.get('last_sales_list', user.get('last_tx_list', [])) if user else []
                if not tx_list:
                    return [{"type": "text", "content": "No transaction list found. Run *my sales* first to see numbered transactions, then try again."}]

                # Parse numbers
                numbers = [int(n.strip()) for n in ref_match.group(1).split(',') if n.strip().isdigit()]
                tx_ids = []
                for n in numbers:
                    if 1 <= n <= len(tx_list):
                        tx_ids.append(tx_list[n-1])

                if not tx_ids:
                    return [{"type": "text", "content": "Invalid numbers. Use numbers from your last *my sales* list."}]

                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "invoice_from_transactions", "content": {"transaction_ids": tx_ids}}]

            # Parse: "[Customer name] [amount] for [description]"  
            # e.g. "Sandra Benede 100000 for 10 pairs of Nike socks"
            # OR transaction-style: "Sold 15 pieces of Gucci socks for 200000 to Dada"
            
            # Detect transaction-style input (starts with sold/bought)
            text_stripped = text.strip()
            tx_style = re.match(r'^(sold|bought|delivered|shipped)\s+', text_stripped, re.IGNORECASE)
            
            if tx_style:
                # Transaction-style: "Sold 15 pieces of Gucci socks for 200000 to Dada"
                # Find amount after "for" (the price, not the quantity)
                price_match = re.search(r'\bfor\s+(₦?\d[\d,]*[kKmM]?)\b', text_stripped, re.IGNORECASE)
                # Find customer after "to"
                name_match = re.search(r'\bto\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s*$', text_stripped)
                if not name_match:
                    name_match = re.search(r'\bto\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)', text_stripped)
                
                if price_match:
                    amt_str = price_match.group(1).replace('₦', '').replace(',', '')
                    if amt_str.lower().endswith('k'):
                        amount = int(amt_str[:-1]) * 1000
                    elif amt_str.lower().endswith('m'):
                        amount = int(amt_str[:-1]) * 1000000
                    else:
                        amount = int(amt_str)
                    customer_name = name_match.group(1).strip().title() if name_match else "Customer"
                    # Description: everything between "sold" and "for [amount]"
                    desc_part = text_stripped[tx_style.end():price_match.start()].strip()
                    # Remove leading quantity like "15 pieces of"
                    desc_part = re.sub(r'^\d+\s*(pairs?|pieces?|cartons?|bags?|packs?|bottles?|dozen)\s+(of\s+)?', '', desc_part, flags=re.IGNORECASE)
                    description = desc_part.strip() or "Goods/Services"
                    
                    self.db.save_session(phone_number, STATE_IDLE, {})
                    return [{"type": "invoice_generate", "content": {
                        "customer_name": customer_name, "amount": amount, "description": description
                    }}]

            # Standard format: [Name] [Amount] for [Description]
            amount_match = re.search(r'(₦?\d[\d,]*[kKmM]?)', text_stripped)
            if not amount_match:
                return [{"type": "text", "content": (
                    "I need an amount. Try:\n"
                    "*Sandra 100,000 for 20 pairs Nike socks*\n\n"
                    "Or type *cancel* to exit."
                )}]

            # Parse amount
            amt_str = amount_match.group(1).replace('₦', '').replace(',', '')
            if amt_str.lower().endswith('k'):
                amount = int(amt_str[:-1]) * 1000
            elif amt_str.lower().endswith('m'):
                amount = int(amt_str[:-1]) * 1000000
            else:
                amount = int(amt_str)

            # Split: text before amount = customer name
            customer_name = text_stripped[:amount_match.start()].strip().title()
            
            # Text after amount = description (remove leading "for" if present)
            after_amount = text_stripped[amount_match.end():].strip()
            if after_amount.lower().startswith('for '):
                description = after_amount[4:].strip()
            elif after_amount.lower().startswith('for'):
                description = after_amount[3:].strip()
            else:
                description = after_amount.strip()

            if not customer_name or len(customer_name) < 2:
                customer_name = "Customer"
            if not description:
                description = "Goods/Services"

            # Detect "+ tax" / "+ VAT" / "+ WHT" in description or original text
            tax_info = None
            tax_pattern = re.search(r'\+\s*(vat|tax|wht)\s*(?:([\d.]+)\s*%?)?', text_stripped, re.IGNORECASE)
            if tax_pattern:
                tax_kw = tax_pattern.group(1).upper()
                tax_rate = tax_pattern.group(2)
                if tax_rate:
                    rate = float(tax_rate)
                else:
                    # Use user's default
                    user = self.db.get_user(phone_number)
                    rate = float(user.get('default_tax_percent', '7.5')) if user and user.get('default_tax_percent') else 7.5
                tax_type = 'WHT' if tax_kw == 'WHT' else 'VAT'
                tax_amt = int(amount * rate / 100)
                tax_info = {"amount": tax_amt, "percent": rate, "type": tax_type}
                # Remove tax part from description
                description = re.sub(r'\+\s*(?:vat|tax|wht)\s*(?:[\d.]+\s*%?)?', '', description, flags=re.IGNORECASE).strip()
                if not description:
                    description = "Goods/Services"

            # Return marker for main.py to handle PDF generation
            self.db.save_session(phone_number, STATE_IDLE, {})
            invoice_data = {"customer_name": customer_name, "amount": int(amount), "description": description}
            if tax_info:
                invoice_data["tax"] = tax_info
            return [{"type": "invoice_generate", "content": invoice_data}]

        self.db.save_session(phone_number, STATE_IDLE, {})
        return [{"type": "text", "content": "Something went wrong. Please try again."}]

    def _handle_receipt_selection(self, phone_number, text, context):
        """Handle receipt generation — select which transaction(s)"""
        text_lower = text.lower().strip()

        if text_lower in ['cancel', 'exit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "❌ Cancelled."}]

        # "last" — generate for last transaction
        if text_lower == 'last':
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "receipt_generate", "content": {"mode": "last"}}]

        # "#3" or "#1,3,5" — specific transactions by number
        cleaned_input = re.sub(r'[\\\/\.\s]+$', '', text.strip())  # strip trailing \, /, ., spaces
        ref_match = re.match(r'^#?([\d,\s]+)$', cleaned_input)
        if ref_match:
            # Get transaction list from user profile (saved by "my sales" command)
            user = self.db.get_user(phone_number)
            tx_list = user.get('last_tx_list', []) if user else []
            if not tx_list:
                return [{"type": "text", "content": "No transaction list found.\n\nRun *my sales* first to see numbered transactions, then come back."}]

            numbers = [int(n.strip()) for n in ref_match.group(1).split(',') if n.strip().isdigit()]
            tx_ids = []
            for n in numbers:
                if 1 <= n <= len(tx_list):
                    tx_ids.append(tx_list[n-1])

            if not tx_ids:
                return [{"type": "text", "content": "Invalid numbers. Use numbers from your *my sales* list.\n\nExample: *#3* or *#1,3,5*"}]

            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "receipt_generate", "content": {"mode": "specific", "transaction_ids": tx_ids}}]

        return [{"type": "text", "content": "Type *last*, or *#3*, or *#1,3,5* to select transactions.\n\nOr *cancel* to exit."}]

    def _handle_confirm_forward(self, phone_number, text, context):
        """Handle confirmation to forward invoice/receipt to customer"""
        text_lower = text.lower().strip()

        customer_name = context.get('customer_name', '')
        contact_phone = context.get('contact_phone', '')
        s3_url = context.get('s3_url', '')
        filename = context.get('filename', '')

        # Yes — send to customer (button reply or text)
        yes_signals = ['yes', 'forward_yes', 'send', 'ok', 'sure', 'yea', 'yeah', 'yep']
        if text_lower in yes_signals or ('yes' in text_lower and 'no' not in text_lower):
            if contact_phone and s3_url:
                self.db.save_session(phone_number, STATE_IDLE, {})
                return [{"type": "forward_send", "content": {
                    "to_phone": contact_phone,
                    "customer_name": customer_name,
                    "s3_url": s3_url,
                    "filename": filename
                }}]
            else:
                # No phone on file — ask for it
                return [{"type": "text", "content": (
                    f"I don't have a WhatsApp number for *{customer_name}*.\n\n"
                    f"Send their number (e.g. 08012345678) and I'll deliver it.\n"
                    f"Or type *skip* to cancel."
                )}]

        # Cancel / Skip
        no_signals = ['skip', 'no', 'cancel', 'exit', 'forward_no', 'nah', 'nope']
        if text_lower in no_signals or 'no' in text_lower:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "👍 Got it. Document not forwarded."}]

        # User sent a phone number
        phone_match = re.match(r'^[\+]?(\d{10,15})$', text.replace(' ', '').replace('-', ''))
        if phone_match:
            new_phone = phone_match.group(1)
            # Normalize Nigerian numbers: 080... → 234...
            if new_phone.startswith('0') and len(new_phone) == 11:
                new_phone = '234' + new_phone[1:]
            # Save to contact
            if customer_name:
                self.db.update_contact_profile(phone_number, customer_name, {'contact_phone': new_phone})
            # Send the document
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "forward_send", "content": {
                "to_phone": new_phone,
                "customer_name": customer_name,
                "s3_url": s3_url,
                "filename": filename
            }}]

        # Unrecognized input
        return [{"type": "text", "content": (
            f"Send *{customer_name}*'s WhatsApp number, or type *skip* to cancel."
        )}]

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
            return self._show_help()
        elif command == 'report':
            return self._handle_report(phone_number, 'report')
        else:
            return self._handle_idle(phone_number, text)

    def _handle_setup_catalog(self, phone_number, text):
        """Start the free-form AI catalog setup (v2)"""
        user = self.db.get_user(phone_number)
        business_name = user.get('business_name', 'your business') if user else 'your business'

        # Check if user already has products — offer to add more
        existing = self.db.get_product_list(phone_number)
        if existing:
            self.db.save_session(phone_number, 'CATALOG_SETUP_DETAILS', {
                'products': existing
            })
            return [{"type": "text", "content": (
                f"📦 You already have {len(existing)} products.\n\n"
                f"Describe what you want to add or update:\n\n"
                f"• _\"Add Perfume, Body Cream to my products\"_\n"
                f"• _\"Airfreshener comes in 500ml, 4L, 25L\"_\n"
                f"• _\"1 carton = 12 pieces\"_\n\n"
                f"Type *done* when finished, or *reset catalog* to start fresh."
            )}]

        self.db.save_session(phone_number, 'CATALOG_SETUP_PRODUCTS', {})
        return [{"type": "text", "content": (
            f"🚀 *Product Catalog Setup*\n\n"
            f"What products does *{business_name}* sell?\n\n"
            f"List them separated by commas or one per line.\n\n"
            f"_Example: Airfreshener, Hand Wash, Dish Wash, Bleach_\n\n"
            f"Type 'cancel' to exit."
        )}]

    def _handle_catalog_setup_products(self, phone_number, text, context):
        """Step 1: Parse the product list"""
        if text.lower().strip() in ['cancel', 'exit', 'stop', 'quit']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "❌ Catalog setup cancelled."}]

        # Split by commas or newlines
        raw_items = [p.strip().title() for p in re.split(r'[,\n]+', text) if p.strip() and len(p.strip()) > 1]

        if not raw_items:
            return [{"type": "text", "content": "Please list at least one product. Separate with commas."}]

        # Smart grouping: detect "size + product" pattern (e.g., "500ml Airfreshener")
        size_pattern = re.compile(r'^(\d+(?:\.\d+)?)\s*(ml|l|kg|g|cl|oz)\s+(.+)$', re.IGNORECASE)
        grouped = {}
        ungrouped = []

        for item in raw_items:
            match = size_pattern.match(item.strip())
            if match:
                size_num = match.group(1)
                size_unit = match.group(2)
                product_name = match.group(3).strip().title()
                size_label = f"{size_num}{size_unit.upper() if size_unit.lower() == 'l' else size_unit.lower()}"
                if product_name not in grouped:
                    grouped[product_name] = []
                if size_label not in grouped[product_name]:
                    grouped[product_name].append(size_label)
            else:
                ungrouped.append(item)

        # If we detected size groupings, use them
        if grouped:
            products = list(grouped.keys()) + ungrouped
            # Save products and auto-set sizes
            for product in products:
                self.db.add_product(phone_number, product)
            for product_name, sizes in grouped.items():
                self.db.set_attributes(phone_number, product_name, 'size', sizes)
            for product in ungrouped:
                self.db.add_product(phone_number, product)
        else:
            products = ungrouped if ungrouped else raw_items
            for product in products:
                self.db.add_product(phone_number, product)

        # Build response
        size_note = ""
        if grouped:
            size_note = f"\n\n🔍 _I noticed sizes in your list and grouped them automatically!_"

        # Move to details phase
        self.db.save_session(phone_number, 'CATALOG_SETUP_DETAILS', {
            'products': products
        })

        return [{"type": "text", "content": (
            f"✅ *{len(products)} products saved:*\n"
            f"{', '.join(products)}\n\n"
            f"{size_note}\n"
            f"━━━━━━━━━━\n\n"
            f"Now tell me about sizes, brands, or variants.\n"
            f"Just describe naturally:\n\n"
            f"• _\"All come in 500ml, 4L, 25L\"_\n"
            f"• _\"Airfreshener brands: Charming, Alluring\"_\n"
            f"• _\"1 carton = 12 pieces for 500ml\"_\n\n"
            f"Type each detail, then *done* when finished."
        )}]

    def _handle_catalog_setup_details(self, phone_number, text, context):
        """Step 2: Parse free-form descriptions with AI"""
        text_lower = text.lower().strip()

        if text_lower in ['done', 'finish', 'finished', "that's all", 'thats all']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._show_catalog_summary(phone_number)

        if text_lower in ['cancel', 'exit', 'stop']:
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "✅ Catalog saved. Type *my catalog* to view."}]

        if text_lower == 'reset catalog':
            self.db.save_product_catalog(phone_number, {})
            self.db.save_session(phone_number, 'CATALOG_SETUP_PRODUCTS', {})
            return [{"type": "text", "content": "🗑️ Catalog cleared!\n\nList your products (comma-separated):"}]

        # Handle non-actionable responses (user asking for clarification)
        if text_lower in ['more details', 'more', 'details', 'yes', 'continue', 'go on', 'next', 'what else']:
            return [{"type": "text", "content": (
                "📝 Tell me about your products:\n\n"
                "• _\"Slides sizes: 35 to 50\"_\n"
                "• _\"Slides brands: Nike, Gucci, Prada\"_\n"
                "• _\"1 carton = 12 pieces\"_\n\n"
                "Or type *done* to finish."
            )}]

        # Get current product list
        products = context.get('products', self.db.get_product_list(phone_number))

        # Split multi-line messages and process each line
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        if len(lines) > 1:
            # Process each line separately, collect results
            responses = []
            last_added = context.get('last_added_product', '')
            for line in lines:
                result = self._parse_catalog_description(phone_number, line, products, last_added=last_added)
                if result and result.get('action') != 'unknown':
                    resp = self._apply_catalog_update(phone_number, result, products)
                    responses.append(resp)
            if responses:
                return [{"type": "text", "content": "\n".join(responses) + "\n\n_More details? Or type *done* to finish._"}]
            # If none parsed, fall through to single-line handling

        # Single line — use AI to parse
        last_added = context.get('last_added_product', '')
        result = self._parse_catalog_description(phone_number, text, products, last_added=last_added)

        if not result or result.get('action') == 'unknown':
            return [{"type": "text", "content": (
                "🤔 I couldn't parse that. Try:\n\n"
                "• _\"sizes: 500ml, 4L, 25L\"_\n"
                "• _\"Hand Wash brands: Soft Touch, Fruity\"_\n"
                "• _\"1 carton = 12 pieces\"_\n\n"
                "Or type *done* to finish."
            )}]

        # Apply the update
        response = self._apply_catalog_update(phone_number, result, products)

        # Track last added product for context
        if result.get('action') == 'add_products':
            new_products = result.get('data', {}).get('products', [])
            if new_products:
                context['last_added_product'] = new_products[-1].strip().title()
                context['products'] = products + [p.strip().title() for p in new_products]
                self.db.save_session(phone_number, 'CATALOG_SETUP_DETAILS', context)

        return [{"type": "text", "content": response + "\n\n_More details? Or type *done* to finish._"}]

    def _parse_catalog_description(self, phone_number, text, products, last_added=''):
        """Use AI to parse free-form catalog descriptions into structured data"""
        import json as _json
        from openai import OpenAI
        from utils.config import get_openai_key

        if not self.categorizer.client:
            self.categorizer.client = OpenAI(api_key=get_openai_key())

        product_list = ", ".join(products) if products else "none"
        context_hint = ""
        if last_added:
            context_hint = f"\n⚠️ The user JUST added '{last_added}' to their catalog. If they don't name a specific product, they probably mean '{last_added}'.\n"
        prompt = (
            f"You are parsing a product catalog description for a Nigerian business.\n"
            f"Their existing products (ONLY these exist): {product_list}\n{context_hint}\n"
            f"User said: \"{text}\"\n\n"
            f"Parse into JSON:\n"
            f'{{"action": "add_sizes"|"add_brands"|"add_attributes"|"add_conversions"|"add_products"|"unknown",\n'
            f' "targets": ["product1"] or ["all"],\n'
            f' "data": {{\n'
            f'   // add_sizes/add_attributes: {{"attributes": {{"size": ["500ml","4L"], "color": ["Red"]}}}}\n'
            f'   // add_brands: {{"brands": ["Charming", "Alluring"]}}\n'
            f'   // add_conversions: {{"conversions": {{"1 carton (500ml)": "12 pieces", "1 carton (4L)": "4 pieces"}}}}\n'
            f'   // add_products: {{"products": ["New Item"]}}\n'
            f' }}}}\n\n'
            f"Rules:\n"
            f"- \"comes in X, Y, Z\" or \"sizes: X, Y, Z\" → add_sizes with attribute 'size'\n"
            f"- \"brands: X, Y\" or \"types: X, Y\" → add_brands\n"
            f"- \"1 carton = 12 pieces for 500ml\" → add_conversions. ALWAYS include size in key: "
            f"{{\"1 carton (500ml)\": \"12 pieces\"}}. If no size mentioned, use plain key.\n"
            f"- \"Add X, Y to products\" → add_products\n"
            f"- TARGETING RULES (critical):\n"
            f"  * If user names specific products (e.g. 'Hand wash, dish wash come in 500ml'), "
            f"target ONLY those exact products from the product list above.\n"
            f"  * 'all' or 'everything' or 'all products' → target 'all'\n"
            f"  * If no product is named AND a product was just added (see context above), "
            f"target that recently-added product.\n"
            f"  * If a named product doesn't exist in the list, suggest add_products instead.\n"
            f"  * For conversions: target 'all' ONLY if user says 'all'. Otherwise target specific products.\n"
            f"  * Match product names case-insensitively to the existing product list.\n"
            f"- Return ONLY valid JSON, no explanation."
        )

        try:
            response = self.categorizer.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
            )
            content = response.choices[0].message.content.strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            return _json.loads(content)
        except Exception as e:
            logger.error(f"Catalog AI parse error: {e}")
            return None

    def _match_product_name(self, target, products):
        """Match a target name to an existing product — exact first, then partial"""
        target_lower = target.strip().lower()
        # Exact match
        for p in products:
            if p.lower() == target_lower:
                return p
        # Partial match — target is contained in a product name
        for p in products:
            if target_lower in p.lower() and len(target_lower) >= 3:
                return p
        return None

    def _apply_catalog_update(self, phone_number, result, products):
        """Apply a parsed catalog update and return confirmation"""
        action = result.get('action', '')
        targets = result.get('targets', [])
        data = result.get('data', {})

        # Resolve "all"
        if 'all' in [str(t).lower() for t in targets]:
            targets = products
        else:
            # Validate targets — match to existing products (exact then partial)
            resolved_targets = []
            for t in targets:
                matched = self._match_product_name(t, products)
                if matched and matched not in resolved_targets:
                    resolved_targets.append(matched)
            targets = resolved_targets if resolved_targets else [t.strip().title() for t in targets[:5]]

        if action in ['add_sizes', 'add_attributes']:
            attributes = data.get('attributes', {})
            if not attributes and 'attribute' in data:
                attributes = {data['attribute']: data.get('values', [])}
            for product in targets:
                for attr, values in attributes.items():
                    self.db.set_attributes(phone_number, product, attr, values)
            attr_summary = ", ".join(f"{k}: {', '.join(str(v) for v in vals)}" for k, vals in attributes.items())
            target_str = ", ".join(targets[:5])
            return f"✅ Added *{attr_summary}* to {target_str}"

        elif action == 'add_brands':
            brands = data.get('brands', [])
            for product in targets:
                for brand in brands:
                    self.db.add_subcategory(phone_number, product, brand)
            return f"✅ Added brands ({', '.join(brands)}) under *{', '.join(targets)}*"

        elif action == 'add_conversions':
            conversions = data.get('conversions', {})
            # Smart filtering: only apply size-specific conversions to products that have that size
            import re as _re
            catalog = self.db.get_product_catalog(phone_number)
            all_products_data = catalog.get('products', {})
            applied_to = set()
            for conv_key, conv_val in conversions.items():
                # Extract size from key like "1 carton (500ml)" → "500ml"
                size_match = _re.search(r'\((\d+(?:\.\d+)?(?:ml|l|L|kg|g|cl|oz))\)', conv_key, _re.IGNORECASE)
                if size_match and 'all' in [str(t).lower() for t in result.get('targets', [])]:
                    # Only apply to products that have this size
                    conv_size = size_match.group(1).lower()
                    for product in targets:
                        p_data = all_products_data.get(product, all_products_data.get(product.strip().title(), {}))
                        product_sizes = [s.lower() for s in p_data.get('attributes', {}).get('size', [])]
                        if conv_size in product_sizes:
                            self.db.set_conversions(phone_number, product, {conv_key: conv_val})
                            applied_to.add(product)
                else:
                    # No size in key or specific targets — apply to all targets
                    for product in targets:
                        self.db.set_conversions(phone_number, product, {conv_key: conv_val})
                        applied_to.add(product)
            conv_str = ", ".join(f"{k} = {v}" for k, v in conversions.items())
            target_note = f" ({', '.join(list(applied_to)[:5])})" if len(applied_to) < len(targets) else ""
            return f"✅ Conversions saved:\n{conv_str}{target_note}"

        elif action == 'add_products':
            new_products = data.get('products', [])
            for p in new_products:
                self.db.add_product(phone_number, p.strip().title())
            return f"✅ Added: {', '.join(new_products)}"

        return "✅ Updated."

    def _show_catalog_summary(self, phone_number):
        """Show catalog summary after setup completes"""
        return self._show_full_catalog(phone_number)

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
            # Check for attribute suggestions before asking
            suggestions = self._get_suggested_attributes(phone_number, current)
            if suggestions:
                suggestion_msg = self._format_attribute_suggestion(suggestions, current)
                context['pending_suggestions'] = list(suggestions.keys())
                context['pending_suggestion_values'] = {k: v for k, v in suggestions.items()}
                context['suggestion_target'] = current
                self.db.save_session(phone_number, STATE_REG_ATTR_SUGGEST, context)
                return [{"type": "text", "content": suggestion_msg}]
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
            # Check for attribute suggestions
            suggestions = self._get_suggested_attributes(phone_number, current_product, current_sub, 'sub_attributes')
            if suggestions:
                suggestion_msg = self._format_attribute_suggestion(suggestions, current_sub)
                context['pending_suggestions'] = list(suggestions.keys())
                context['pending_suggestion_values'] = {k: v for k, v in suggestions.items()}
                context['suggestion_target'] = current_sub
                self.db.save_session(phone_number, STATE_REG_ATTR_SUGGEST, context)
                return [{"type": "text", "content": suggestion_msg}]
            self.db.save_session(phone_number, STATE_REG_ATTRIBUTES, context)
            return [{"type": "text", "content": f"What attributes matter for *{current_sub}* ({current_product})?\n\n_Examples: size, color, material, condition_\n_Separate with commas. Type \'skip\' to move on._"}]

    def _get_suggested_attributes(self, phone_number, current_product, current_sub=None, phase=None):
        """
        Look up previously defined attributes from the user's catalog
        and suggest them for the current product/subcategory.
        Returns a list of (attr_name, values) tuples from similar products.
        """
        try:
            catalog = self.db.get_product_catalog(phone_number)
            products = catalog.get('products', {})
            suggestions = {}

            for prod_name, prod_data in products.items():
                # Skip the current product itself
                if prod_name.lower() == current_product.lower():
                    continue

                # Collect attributes at product level
                attrs = prod_data.get('attributes', {})
                for attr_name, attr_data in attrs.items():
                    values = attr_data.get('values', []) if isinstance(attr_data, dict) else []
                    if attr_name not in suggestions:
                        suggestions[attr_name] = values

                # Collect from subcategories too
                subcats = prod_data.get('subcategories', {})
                for sub_name, sub_data in subcats.items():
                    sub_attrs = sub_data.get('attributes', {})
                    for attr_name, attr_data in sub_attrs.items():
                        values = attr_data.get('values', []) if isinstance(attr_data, dict) else []
                        if attr_name not in suggestions:
                            suggestions[attr_name] = values

            return suggestions
        except Exception as e:
            return {}

    def _format_attribute_suggestion(self, suggestions, target_name):
        """Format attribute suggestions into a WhatsApp message"""
        if not suggestions:
            return None

        attr_lines = []
        for attr, values in list(suggestions.items())[:6]:  # max 6 suggestions
            if values:
                val_preview = ', '.join(str(v) for v in values[:4])
                if len(values) > 4:
                    val_preview += f' +{len(values)-4} more'
                attr_lines.append(f"  • *{attr}*: {val_preview}")
            else:
                attr_lines.append(f"  • *{attr}*")

        attrs_str = '\n'.join(attr_lines)
        attr_names = ', '.join(suggestions.keys())

        return (
            f"💡 I noticed you've used these attributes before:\n\n"
            f"{attrs_str}\n\n"
            f"For *{target_name}*, would you like to:\n\n"
            f"1️⃣ *Use same* — use all of these\n"
            f"2️⃣ *Pick some* — choose which ones to keep\n"
            f"3️⃣ *New* — define fresh attributes\n"
            f"4️⃣ *Add more* — use these + add extra\n\n"
            f"_Reply with 1, 2, 3, or 4_"
        )

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
            return self._handle_idle(phone_number, text)

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
        current_sub = subcategories[sub_idx] if sub_idx < len(subcategories) else ''
        phase = context.get('phase', 'product_attributes')
        attrs_to_fill = context.get('attrs_to_fill', [])
        attr_fill_idx = int(context.get('attr_fill_idx', 0))
        current_target = context.get('current_target', '')

        current_attr = attrs_to_fill[attr_fill_idx] if attr_fill_idx < len(attrs_to_fill) else ''

        # Save values
        if text.lower().strip() not in ['skip', '-', 'none', 'keep']:
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

    def _handle_guided_step(self, phone_number, text, context):
        """Handle each step of the guided transaction flow"""
        from utils.parser import parse_amount
        step = context.get('step', 'ask_item')
        text_lower = text.lower().strip()

        # Escape at any point
        if text_lower in ('cancel', 'back', 'exit', 'stop', 'menu', 'help'):
            self.db.save_session(phone_number, STATE_IDLE, {})
            if text_lower in ('menu', 'help'):
                return self._handle_greeting(phone_number)
            return [{"type": "text", "content": "\U0001f44d Cancelled. What else can I help with?"}]

        # ─── STEP 1: WHAT ITEM ───
        if step == 'ask_item':
            item = text.strip()
            if len(item) < 1:
                return [{"type": "text", "content": "Please type what you sold/bought:"}]

            context['item'] = item
            context['step'] = 'ask_quantity'
            self.db.save_session(phone_number, 'STATE_GUIDED', context)

            return [{"type": "buttons", "content": {
                "body": f"\U0001f4e6 *{item.title()}* \u2014 How many?\n\n_Type a number, or tap 1 if just one item_",
                "buttons": [
                    {"id": "guided_qty_1", "title": "1 (single)"},
                    {"id": "guided_qty_skip", "title": "Skip"},
                ]
            }}]

        # ─── STEP 2: QUANTITY ───
        elif step == 'ask_quantity':
            if text_lower in ('skip', 'guided_qty_skip', '1', 'guided_qty_1', 'one'):
                qty = 1 if text_lower in ('1', 'guided_qty_1', 'one') else ''
            else:
                # Try to parse number
                import re
                num_match = re.match(r'(\d+)', text.strip())
                qty = int(num_match.group(1)) if num_match else 1

            context['quantity'] = qty if qty else ''
            context['step'] = 'ask_amount'
            self.db.save_session(phone_number, 'STATE_GUIDED', context)

            item = context.get('item', 'item')
            qty_display = f" x{qty}" if qty and qty > 1 else ""
            return [{"type": "text", "content": (
                f"\U0001f4b0 *{item.title()}{qty_display}* \u2014 How much (total)?\n\n"
                f"_Type amount: e.g. 150000, 150K, 1.5M_"
            )}]

        # ─── STEP 3: AMOUNT ───
        elif step == 'ask_amount':
            amount = parse_amount(text)
            if amount == 0:
                return [{"type": "text", "content": "\u26a0\ufe0f Couldn't get a number. How much?\n_e.g. 50000, 50K, 1.5M_"}]

            context['amount'] = amount
            context['step'] = 'ask_vendor'
            self.db.save_session(phone_number, 'STATE_GUIDED', context)

            tx_type = context.get('tx_type', 'income')
            if tx_type == 'income':
                question = "Who bought it?"
            else:
                question = "Who did you buy from?"

            return [{"type": "buttons", "content": {
                "body": f"\U0001f465 *{question}*\n\n_Type their name, or skip_",
                "buttons": [
                    {"id": "guided_vendor_skip", "title": "\u23e9 Skip"},
                ]
            }}]

        # ─── STEP 4: VENDOR/CUSTOMER ───
        elif step == 'ask_vendor':
            if text_lower in ('skip', 'guided_vendor_skip', 'no', 'none'):
                vendor = ''
            else:
                vendor = text.strip()

            context['vendor'] = vendor
            context['step'] = 'ask_details'
            self.db.save_session(phone_number, 'STATE_GUIDED', context)

            return [{"type": "buttons", "content": {
                "body": "\U0001f3f7\ufe0f *Any extra details?*\n\nBrand, color, size, model, pattern...\n\n_Type details or skip_",
                "buttons": [
                    {"id": "guided_details_skip", "title": "\u23e9 No, save it"},
                ]
            }}]

        # ─── STEP 5: DETAILS (optional) ───
        elif step == 'ask_details':
            # Ignore generic/placeholder text (user copied the prompt)
            skip_words = {'skip', 'guided_details_skip', 'no', 'none', 'no, save it',
                         'brand', 'color', 'size', 'model', 'pattern',
                         'brand, color, size', 'brand, color, pattern',
                         'brand, color, size, model, pattern'}
            if text_lower in skip_words or text_lower.replace(',', '').replace(' ', '') in ('brandcolorsize', 'brandcolorsizemodel', 'brandcolorsizemodelpattern'):
                details = ''
            else:
                details = text.strip()

            context['details'] = details
            context['step'] = 'confirm'

            # Build the full natural language sentence and send to AI parser
            item = context.get('item', '')
            qty = context.get('quantity', '')
            amount = context.get('amount', 0)
            vendor = context.get('vendor', '')
            tx_type = context.get('tx_type', 'income')
            payment = context.get('payment', 'cash')

            # Construct natural sentence for AI parser
            # Format: "sold/bought [qty] [item] [details] to/from [vendor] [amount] [on credit]"
            if tx_type == 'income':
                verb = 'sold'
                preposition = 'to'
            else:
                verb = 'bought'
                preposition = 'from'

            sentence = f"{verb} "
            if qty and qty > 1:
                sentence += f"{qty} "
            sentence += f"{item} "
            if details:
                sentence += f"({details}) "
            if vendor:
                sentence += f"{preposition} {vendor} "
            sentence += f"for {amount}"
            if payment == 'credit':
                sentence += " on credit"

            # Reset state and process as a normal transaction
            self.db.save_session(phone_number, STATE_IDLE, {})
            return self._handle_idle(phone_number, sentence)

        else:
            # Unknown step — reset
            self.db.save_session(phone_number, STATE_IDLE, {})
            return [{"type": "text", "content": "Something went wrong. Let\'s start over."}]

    def _start_guided_flow(self, phone_number, flow_type):
        """Start a guided step-by-step transaction recording flow.
        flow_type: cash_sale, credit_sale, cash_purchase, credit_purchase, cash_expense
        """
        # Determine transaction category
        if 'sale' in flow_type:
            tx_type = 'income'
            payment = 'credit' if 'credit' in flow_type else 'cash'
            prompt_label = 'sold'
            emoji = '\U0001f4b0'
            item_question = "What did you sell?"
            item_examples = "_(e.g. shoes, socks, bread, phone)_"
        elif 'purchase' in flow_type:
            tx_type = 'expense'
            payment = 'credit' if 'credit' in flow_type else 'cash'
            prompt_label = 'bought'
            emoji = '\U0001f4e6'
            item_question = "What did you buy?"
            item_examples = "_(e.g. flour, fabric, cement, stock)_"
        else:
            tx_type = 'expense'
            payment = 'cash'
            prompt_label = 'paid for'
            emoji = '\U0001f4b8'
            item_question = "What did you pay for?"
            item_examples = "_(e.g. rent, fuel, salary, electricity)_"

        self.db.save_session(phone_number, 'STATE_GUIDED', {
            'step': 'ask_item',
            'flow_type': flow_type,
            'tx_type': tx_type,
            'payment': payment,
        })

        return [{"type": "text", "content": (
            f"{emoji} *{item_question}*\n\n"
            f"{item_examples}"
        )}]

    def _get_record_menu_rows(self, phone_number):
        """Return industry-specific Record menu rows"""
        user = self.db.get_user(phone_number)
        industry_class = user.get('industry_class', 'trading') if user else 'trading'

        # Base rows (everyone gets these)
        base_rows = [
            {"id": "menu_record_sale", "title": "\U0001f4b0 Record Sale", "description": "Income \u2014 cash or credit"},
        ]

        if industry_class == 'manufacturing':
            return [
                {"id": "menu_record_sale", "title": "\U0001f4b0 Record Sale", "description": "Sell finished goods"},
                {"id": "menu_record_purchase", "title": "\U0001f9f1 Buy Raw Materials", "description": "Materials for production"},
                {"id": "menu_production", "title": "\U0001f3ed Record Production", "description": "Log a production run"},
                {"id": "menu_set_recipe", "title": "\U0001f4cb Set Recipe / BOM", "description": "Define materials per batch"},
                {"id": "menu_record_expense", "title": "\U0001f4b8 Record Expense", "description": "Rent, utilities, admin"},
                {"id": "menu_record_payment", "title": "\U0001f4b3 Record Payment", "description": "Debt paid to/by you"},
            ]
        elif industry_class == 'services':
            return [
                {"id": "menu_record_service", "title": "\U0001f4bc Record Service", "description": "Fee, contract, or commission"},
                {"id": "menu_record_expense", "title": "\U0001f4b8 Record Expense", "description": "Rent, tools, subcontractors"},
                {"id": "menu_record_purchase", "title": "\U0001f6d2 Record Purchase", "description": "Materials or tools bought"},
                {"id": "menu_record_payment", "title": "\U0001f4b3 Record Payment", "description": "Debt paid to/by you"},
            ]
        elif industry_class == 'hybrid':
            return [
                {"id": "menu_record_sale", "title": "\U0001f4b0 Record Sale", "description": "Product sold (cash or credit)"},
                {"id": "menu_record_service", "title": "\U0001f4bc Record Service", "description": "Service delivered"},
                {"id": "menu_record_purchase", "title": "\U0001f4e6 Record Purchase", "description": "Stock or materials"},
                {"id": "menu_record_expense", "title": "\U0001f4b8 Record Expense", "description": "Rent, bills, salaries"},
                {"id": "menu_record_payment", "title": "\U0001f4b3 Record Payment", "description": "Debt paid to/by you"},
            ]
        else:
            # Trading (default)
            return [
                {"id": "menu_record_sale", "title": "\U0001f4b0 Record Sale", "description": "Income \u2014 cash or credit"},
                {"id": "menu_record_purchase", "title": "\U0001f4e6 Record Purchase", "description": "Stock & goods for business"},
                {"id": "menu_record_expense", "title": "\U0001f4b8 Record Expense", "description": "Rent, bills, salaries"},
                {"id": "menu_record_payment", "title": "\U0001f4b3 Record Payment", "description": "Debt paid to/by you"},
            ]

    def _show_debts_menu(self):
        """Show debts & credits sub-menu"""
        return [{"type": "list", "content": {
            "header": "\U0001f4b3 Debts & Credits",
            "body": "Track money owed \u2014 both ways:",
            "button_text": "\U0001f4b3 View Debts",
            "sections": [
                {
                    "title": "Debts & Credits",
                    "rows": [
                        {"id": "debt_who_owes_me", "title": "\U0001f4b0 Who Owes Me", "description": "People who owe you money"},
                        {"id": "debt_who_i_owe", "title": "\U0001f4b8 Who I Owe", "description": "Suppliers/people you owe"},
                    ]
                }
            ]
        }}]

    def _show_contacts_menu(self):
        """Show contacts sub-menu — Customers vs Suppliers"""
        return [{"type": "list", "content": {
            "header": "\U0001f4d6 Contacts",
            "body": "Your business contacts:",
            "button_text": "\U0001f4d6 View Contacts",
            "sections": [
                {
                    "title": "Contact Types",
                    "rows": [
                        {"id": "contacts_customers", "title": "\U0001f465 My Customers", "description": "People who buy from you"},
                        {"id": "contacts_suppliers", "title": "\U0001f3ea My Suppliers", "description": "People you buy from"},
                        {"id": "contacts_all", "title": "\U0001f4d6 All Contacts", "description": "Everyone combined"},
                    ]
                }
            ]
        }}]

    def _show_contacts_filtered(self, phone_number, contact_type):
        """Show customers OR suppliers from auto_catalog"""
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        contacts = catalog.get('customers', {})  # This stores both customers & suppliers

        if not contacts:
            return [{"type": "text", "content": (
                "\U0001f4d6 *No contacts yet!*\n\n"
                "Your contacts build automatically as you record transactions.\n"
                "_Just start recording and I\'ll learn your people._"
            )}]

        # Filter by type
        filtered = {k: v for k, v in contacts.items() 
                   if v.get('type', 'customer') == contact_type}

        if not filtered:
            label = 'customers' if contact_type == 'customer' else 'suppliers'
            return [{"type": "text", "content": f"\U0001f4d6 No {label} recorded yet."}]

        # Sort by transaction count (most active first)
        sorted_contacts = sorted(filtered.values(), key=lambda x: x.get('transaction_count', 0), reverse=True)

        emoji = "\U0001f465" if contact_type == 'customer' else "\U0001f3ea"
        label = "Customers" if contact_type == 'customer' else "Suppliers"
        msg = f"{emoji} *My {label}* ({len(sorted_contacts)})\n\n"

        for c in sorted_contacts[:15]:
            name = c.get('name', 'Unknown')
            total = c.get('total_amount', 0)
            count = c.get('transaction_count', 0)
            products = c.get('products', [])
            last = c.get('last_activity', '')

            msg += f"*{name}*\n"
            msg += f"   \U0001f4b0 \u20a6{total:,} ({count} transactions)\n"
            if products:
                msg += f"   \U0001f4e6 {', '.join(products[:3])}\n"
            if last:
                msg += f"   \U0001f4c5 Last: {last}\n"
            msg += "\n"

        if len(sorted_contacts) > 15:
            msg += f"_...and {len(sorted_contacts) - 15} more_"

        return [{"type": "text", "content": msg}]

    def _show_reports_menu(self, phone_number=None):
        """Show reports type selection — tailored to industry"""
        # Get industry for tailored labels
        industry_class = 'trading'
        if phone_number:
            user = self.db.get_user(phone_number)
            industry_class = user.get('industry_class', 'trading') if user else 'trading'

        # Industry-specific menu items
        menu_configs = {
            'trading': {
                'sales': {"title": "\U0001f4b0 My Sales", "description": "Cash & credit sales"},
                'purchases': {"title": "\U0001f6d2 My Purchases", "description": "Stock bought for resale"},
                'expenses': {"title": "\U0001f4b8 My Expenses", "description": "Rent, transport, utilities"},
                'summary': {"title": "\U0001f4ca P&L Statement", "description": "Revenue \u2192 COGS \u2192 Gross Profit \u2192 Net"},
            },
            'manufacturing': {
                'sales': {"title": "\U0001f4b0 Production Sales", "description": "Finished goods sold"},
                'purchases': {"title": "\U0001f9f1 Raw Materials", "description": "Materials, labour, overhead"},
                'expenses': {"title": "\U0001f4b8 Operating Expenses", "description": "Admin, transport, utilities"},
                'summary': {"title": "\U0001f4ca Manufacturing P&L", "description": "Revenue \u2192 Production Costs \u2192 Margin"},
            },
            'services': {
                'sales': {"title": "\U0001f4bc Service Revenue", "description": "Fees, contracts, commissions"},
                'purchases': {"title": "\U0001f4b8 Direct Costs", "description": "Subcontractors, tools, materials"},
                'expenses': {"title": "\U0001f4b8 Operating Expenses", "description": "Rent, transport, utilities"},
                'summary': {"title": "\U0001f4ca Revenue Statement", "description": "Revenue \u2192 Direct Costs \u2192 Margin"},
            },
            'hybrid': {
                'sales': {"title": "\U0001f4b0 All Revenue", "description": "Product sales + service fees"},
                'purchases': {"title": "\U0001f6d2 Direct Costs", "description": "Stock + service delivery costs"},
                'expenses': {"title": "\U0001f4b8 Operating Expenses", "description": "Rent, transport, utilities"},
                'summary': {"title": "\U0001f4ca Combined P&L", "description": "Products + Services combined"},
            },
        }

        config = menu_configs.get(industry_class, menu_configs['trading'])

        return [{"type": "list", "content": {
            "header": "\U0001f4ca Reports",
            "body": "What would you like to see?",
            "button_text": "\U0001f4ca Reports",
            "sections": [
                {
                    "title": "Choose Report Type",
                    "rows": [
                        {"id": "rpt_sales", **config['sales']},
                        {"id": "rpt_purchases", **config['purchases']},
                        {"id": "rpt_expenses", **config['expenses']},
                        {"id": "rpt_summary", **config['summary']},
                    ]
                },
            ]
        }}]

    def _show_period_chooser(self, report_type):
        """Show period selection list (Step 2: choose when) with Custom option"""
        type_labels = {'sales': 'Sales', 'purchases': 'Purchases', 'expenses': 'Expenses'}
        label = type_labels.get(report_type, 'Report')

        return [{"type": "list", "content": {
            "header": f"\U0001f4c5 {label}",
            "body": f"Choose a period for your {label.lower()} report:",
            "button_text": "\U0001f4c5 Choose Period",
            "sections": [
                {
                    "title": "Quick Periods",
                    "rows": [
                        {"id": f"rpt_{report_type}_today", "title": "\U0001f4c5 Today", "description": "Today\'s transactions"},
                        {"id": f"rpt_{report_type}_week", "title": "\U0001f4c5 This Week", "description": "Monday to now"},
                        {"id": f"rpt_{report_type}_month", "title": "\U0001f4c5 This Month", "description": "1st to today"},
                        {"id": f"rpt_{report_type}_custom", "title": "\u270d\ufe0f Custom Filter", "description": "Type your own (e.g. last 5 days, June, Nike)"},
                    ]
                }
            ]
        }}]

    def _show_documents_menu(self):
        """Show the documents sub-menu"""
        return [{"type": "list", "content": {
            "header": "\U0001f4c4 Documents",
            "body": "Generate professional business documents.",
            "button_text": "\U0001f4c4 Documents",
            "sections": [
                {
                    "title": "Generate",
                    "rows": [
                        {"id": "doc_invoice", "title": "\U0001f4c4 Invoice", "description": "Create invoice for a customer"},
                        {"id": "doc_receipt", "title": "\U0001f9fe Receipt", "description": "Payment receipt for a sale"},
                        {"id": "doc_statement", "title": "\U0001f4cb Statement", "description": "Monthly financial statement"},
                        {"id": "doc_export", "title": "\U0001f4ca Export to Excel", "description": "Download spreadsheet file"},
                    ]
                },
                {
                    "title": "Settings",
                    "rows": [
                        {"id": "doc_bank_details", "title": "\U0001f3e6 Bank Details", "description": "Payment info shown on invoices"},
                    ]
                },
            ]
        }}]

    def _show_catalog_menu(self, phone_number=None):
        """Show catalog sub-menu with 4 tappable options"""
        return [{"type": "list", "content": {
            "header": "\U0001f4e6 Catalog",
            "body": "Your product catalog \u2014 builds automatically from transactions.",
            "button_text": "\U0001f4e6 Catalog",
            "sections": [{
                "title": "Catalog Options",
                "rows": [
                    {"id": "cat_browse", "title": "\U0001f4cb Browse Products", "description": "View & manage your products"},
                    {"id": "cat_organize", "title": "\u2699\ufe0f Organize Products", "description": "Set up attributes (Brand, Color, Size)"},
                    {"id": "cat_add_product", "title": "\u2795 Add Product", "description": "Manually add a new product"},
                    {"id": "cat_top_sellers", "title": "\U0001f4ca Top Sellers", "description": "Best performing products"},
                ]
            }]
        }}]

    def _show_product_list(self, phone_number):
        """Show clickable product list — users tap to drill down into details"""
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})
        # Merge old product_catalog if exists (only proper product dicts)
        old_catalog = user.get('product_catalog', {}) if user else {}
        if old_catalog and isinstance(old_catalog, dict):
            for old_key, old_val in old_catalog.items():
                # Skip non-product entries (lists, strings, metadata keys)
                if not isinstance(old_val, dict) or old_key in ('products', 'brands', 'categories', 'settings'):
                    continue
                norm_key = old_key.lower().replace(' ', '-')
                if norm_key not in products:
                    products[norm_key] = {'name': old_key.replace('-', ' ').title(), 'brand': '', 'item': old_key.title(),
                                        'category': '', 'sell_prices': [], 'buy_prices': [],
                                        'total_sold': 0, 'total_bought': 0, 'customers': [], 'suppliers': [],
                                        'last_activity': '', 'variants': {}}

        if not products:
            return [{"type": "text", "content": (
                "\U0001f4e6 *Your Product Catalog*\n\n"
                "No products yet! Your catalog builds automatically as you record transactions.\n\n"
                "\u2022 Type *add product [name]* to add manually\n"
                "\u2022 Or just record a sale/purchase \u2014 I\'ll learn your products!\n\n"
                "_Example: sold 10 Nike socks to Sandra 150K_"
            )}]

        # Sort by most active
        sorted_products = sorted(products.items(),
                                key=lambda x: x[1].get('total_sold', 0) + x[1].get('total_bought', 0),
                                reverse=True)

        rows = []
        for key, prod in sorted_products[:10]:
            name = prod.get('name', 'Unknown')
            sold = prod.get('total_sold', 0)
            sell_prices = prod.get('sell_prices', [])
            has_tree = '\u2705 ' if prod.get('hierarchy') else ''
            variants = prod.get('variants', {})
            tree = prod.get('tree', {})

            desc_parts = []
            if sell_prices:
                desc_parts.append(f"\u20a6{sum(sell_prices)//len(sell_prices):,}")
            if sold:
                desc_parts.append(f"{sold} sold")
            if tree:
                children = [k for k in tree.keys() if k != '_meta']
                desc_parts.append(f"{len(children)} types")
            elif variants:
                desc_parts.append(f"{len(variants)} variants")

            description = " \u2022 ".join(desc_parts) if desc_parts else "Tap for details"

            rows.append({
                "id": f"cat_product_{key}",
                "title": f"{has_tree}{name}"[:24],
                "description": description[:72]
            })

        if not rows:
            return [{"type": "text", "content": "\U0001f4e6 No products yet. Record transactions to build your catalog!"}]

        return [{"type": "list", "content": {
            "header": f"\U0001f4e6 Products ({len(products)})",
            "body": "Tap a product for details, variants & stock.\n\n\u2705 = organized with attribute tree",
            "button_text": "\U0001f4e6 Browse",
            "sections": [{"title": "Your Products", "rows": rows}]
        }}]


    def _show_catalog_by_brand(self, phone_number):
        """Show catalog grouped by brand"""
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})

        if not products:
            return [{"type": "text", "content": "\U0001f4e6 No products in your catalog yet.\n\nStart recording transactions and your catalog will build automatically!"}]

        # Group by brand
        by_brand = {}
        for p_key, p_data in products.items():
            brand = p_data.get('brand', '') or 'Other'
            if brand not in by_brand:
                by_brand[brand] = []
            by_brand[brand].append(p_data)

        # Sort brands by total activity
        sorted_brands = sorted(by_brand.items(), key=lambda x: sum(p.get('total_sold', 0) + p.get('total_bought', 0) for p in x[1]), reverse=True)

        msg = "\U0001f3f7\ufe0f *Products by Brand*\n\n"
        for brand, prods in sorted_brands[:8]:
            msg += f"*{brand}*\n"
            for p in sorted(prods, key=lambda x: x.get('total_sold', 0), reverse=True)[:5]:
                name = p.get('item', p.get('name', ''))
                sold = p.get('total_sold', 0)
                sell_prices = p.get('sell_prices', [])
                price_str = f" \u2022 \u20a6{sell_prices[-1]:,}" if sell_prices else ""
                msg += f"  \u2022 {name} ({sold} sold){price_str}\n"
            msg += "\n"

        return [{"type": "text", "content": msg}]

    def _show_top_sellers(self, phone_number):
        """Show top selling products"""
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})

        if not products:
            return [{"type": "text", "content": "\U0001f4e6 No products in your catalog yet.\n\nStart recording sales and I\'ll track your top sellers!"}]

        # Sort by total_sold descending
        sorted_products = sorted(products.values(), key=lambda x: x.get('total_sold', 0), reverse=True)
        top = [p for p in sorted_products if p.get('total_sold', 0) > 0][:10]

        if not top:
            return [{"type": "text", "content": "\U0001f4ca No sales recorded yet for any product."}]

        msg = "\U0001f4ca *Top Sellers*\n\n"
        for i, p in enumerate(top, 1):
            name = p.get('name', 'Unknown')
            sold = p.get('total_sold', 0)
            sell_prices = p.get('sell_prices', [])
            total_revenue = sum(sell_prices)
            customers = p.get('customers', [])

            medal = "\U0001f947" if i == 1 else "\U0001f948" if i == 2 else "\U0001f949" if i == 3 else f"#{i}"
            msg += f"{medal} *{name}*\n"
            msg += f"   Sold: {sold}x"
            if total_revenue:
                msg += f" | Revenue: \u20a6{total_revenue:,}"
            msg += "\n"
            if customers:
                msg += f"   Top buyers: {', '.join(customers[:3])}\n"
            msg += "\n"

        return [{"type": "text", "content": msg}]

    def _update_auto_catalog(self, phone_number, context):
        """Auto-update product catalog from a confirmed transaction.
        Called after every confirmed transaction to build the catalog organically."""
        try:
            item_name = context.get('item_name', '')
            brand = context.get('brand', '')
            vendor = context.get('vendor', '')
            amount = int(context.get('amount', 0))
            tx_type = context.get('type', '')
            quantity = context.get('quantity', '')
            unit_cost = context.get('unit_cost', '')
            category = context.get('category', '')

            # Need at least an item name or brand to catalog
            if not item_name and not brand:
                return

            # Build product key (normalized)
            product_key = (brand + ' ' + item_name).strip().lower().replace(' ', '-')
            if not product_key or product_key == '-':
                return

            # Get existing catalog
            user = self.db.get_user(phone_number)
            catalog = user.get('auto_catalog', {}) if user else {}
            products = catalog.get('products', {})
            customers = catalog.get('customers', {})

            # Update product entry
            product = products.get(product_key, {
                'name': (brand + ' ' + item_name).strip().title(),
                'brand': brand or '',
                'item': item_name or '',
                'category': category,
                'sell_prices': [],
                'buy_prices': [],
                'total_sold': 0,
                'total_bought': 0,
                'customers': [],
                'suppliers': [],
                'last_activity': '',
            })

            from datetime import datetime
            product['last_activity'] = datetime.now().strftime('%Y-%m-%d')
            product['category'] = category or product.get('category', '')

            # Auto-detect and track variants (color, pattern, size)
            color = context.get('color', '')
            pattern = context.get('pattern', '') or context.get('style', '')
            size = context.get('size', '')
            model = context.get('model', '')

            if color or pattern or size or model:
                # Build variant key from attributes
                variant_parts = [p for p in [color, pattern, size, model] if p]
                variant_name = ' '.join(variant_parts).title()
                variant_key = variant_name.lower().replace(' ', '-')

                if variant_key:
                    variants = product.get('variants', {})
                    variant = variants.get(variant_key, {
                        'name': variant_name,
                        'color': color,
                        'pattern': pattern,
                        'size': size,
                        'model': model,
                        'sell_price': 0,
                        'buy_price': 0,
                        'stock': 0,
                        'total_sold': 0,
                        'total_bought': 0,
                        'last_sold': '',
                        'buyers': [],
                    })

                    # Update variant stats
                    unit_price = int(unit_cost) if unit_cost else amount
                    actual_qty_v = int(quantity) if quantity else 1

                    if tx_type == 'income':
                        variant['sell_price'] = unit_price
                        variant['total_sold'] = variant.get('total_sold', 0) + actual_qty_v
                        variant['stock'] = max(0, variant.get('stock', 0) - actual_qty_v)
                        variant['last_sold'] = datetime.now().strftime('%Y-%m-%d')
                        if vendor and vendor not in variant.get('buyers', []):
                            buyers = variant.get('buyers', [])
                            buyers.append(vendor)
                            variant['buyers'] = buyers[-5:]
                    else:
                        variant['buy_price'] = unit_price
                        variant['total_bought'] = variant.get('total_bought', 0) + actual_qty_v
                        variant['stock'] = variant.get('stock', 0) + actual_qty_v

                    variants[variant_key] = variant
                    product['variants'] = variants

            # === TREE POPULATION ===
            # If product has a hierarchy defined, populate the tree
            hierarchy = product.get('hierarchy', [])
            if hierarchy:
                tree = product.get('tree', {})
                
                # Map hierarchy attribute names to actual values from context
                attr_values = {
                    'pattern': (context.get('pattern', '') or context.get('style', '')).lower(),
                    'brand': (context.get('brand', '') or brand).lower(),
                    'color': (context.get('color', '') or color).lower(),
                    'size': str(context.get('size', '')).lower(),
                    'material': (context.get('material', '')).lower(),
                    'model': (context.get('model', '')).lower(),
                    'condition': (context.get('condition', '')).lower(),
                    'type': (context.get('item_type', '') or context.get('model', '')).lower(),
                    'style': (context.get('pattern', '') or context.get('style', '')).lower(),
                }

                # Navigate/create path in tree
                current = tree
                actual_qty_tree = int(quantity) if quantity else 1
                unit_price_tree = int(unit_cost) if unit_cost else amount

                for depth, attr_name in enumerate(hierarchy):
                    attr_val = attr_values.get(attr_name, '')
                    if not attr_val:
                        break  # Can't go deeper without a value

                    # Create node if doesn't exist
                    if attr_val not in current:
                        current[attr_val] = {'_meta': {'stock': 0, 'total_sold': 0, 'total_bought': 0, 'sell_price': 0, 'buy_price': 0}}
                    
                    # Update _meta at this level
                    meta = current[attr_val].get('_meta', {'stock': 0, 'total_sold': 0, 'total_bought': 0, 'sell_price': 0, 'buy_price': 0})
                    if tx_type == 'income':
                        meta['total_sold'] = meta.get('total_sold', 0) + actual_qty_tree
                        meta['stock'] = max(0, meta.get('stock', 0) - actual_qty_tree)
                        meta['sell_price'] = unit_price_tree
                    else:
                        meta['total_bought'] = meta.get('total_bought', 0) + actual_qty_tree
                        meta['stock'] = meta.get('stock', 0) + actual_qty_tree
                        meta['buy_price'] = unit_price_tree
                    current[attr_val]['_meta'] = meta

                    # Move deeper
                    current = current[attr_val]

                product['tree'] = tree

            # Apply unit conversion if bulk unit was used
            actual_qty = int(quantity) if quantity else 1
            units_info = product.get('units', {})
            conversions = units_info.get('conversions', {})
            # Check if the description mentions a bulk unit (carton, dozen, pack, etc.)
            desc_lower = (context.get('description', '') or '').lower()
            for bulk_unit, multiplier in conversions.items():
                if bulk_unit in desc_lower:
                    actual_qty = actual_qty * multiplier
                    break

            if tx_type == 'income':
                product['total_sold'] = product.get('total_sold', 0) + actual_qty
                # Track sell price per unit (keep last 5)
                sell_price = int(unit_cost) if unit_cost else amount
                if sell_price:
                    prices = product.get('sell_prices', [])
                    prices.append(sell_price)
                    product['sell_prices'] = prices[-5:]
                if vendor and vendor not in product.get('customers', []):
                    custs = product.get('customers', [])
                    custs.append(vendor)
                    product['customers'] = custs[-10:]  # Keep last 10
            else:
                product['total_bought'] = product.get('total_bought', 0) + actual_qty
                # Track buy price per unit (keep last 5)
                buy_price = int(unit_cost) if unit_cost else amount
                if buy_price:
                    prices = product.get('buy_prices', [])
                    prices.append(buy_price)
                    product['buy_prices'] = prices[-5:]
                if vendor and vendor not in product.get('suppliers', []):
                    supps = product.get('suppliers', [])
                    supps.append(vendor)
                    product['suppliers'] = supps[-10:]

            products[product_key] = product

            # Update customer/supplier entry
            if vendor:
                vendor_key = vendor.lower().replace(' ', '-')
                customer = customers.get(vendor_key, {
                    'name': vendor,
                    'type': 'customer' if tx_type == 'income' else 'supplier',
                    'total_amount': 0,
                    'transaction_count': 0,
                    'products': [],
                    'last_activity': '',
                })
                customer['total_amount'] = customer.get('total_amount', 0) + amount
                customer['transaction_count'] = customer.get('transaction_count', 0) + 1
                customer['last_activity'] = datetime.now().strftime('%Y-%m-%d')
                # Track which products they buy
                prod_name = (brand + ' ' + item_name).strip().title()
                if prod_name and prod_name not in customer.get('products', []):
                    prods = customer.get('products', [])
                    prods.append(prod_name)
                    customer['products'] = prods[-15:]
                # Update type: if they both buy and sell, mark as 'both'
                if tx_type == 'income' and customer.get('type') == 'supplier':
                    customer['type'] = 'both'
                elif tx_type == 'expense' and customer.get('type') == 'customer':
                    customer['type'] = 'both'
                customers[vendor_key] = customer

            # Save updated catalog
            catalog['products'] = products
            catalog['customers'] = customers
            self.db.update_user(phone_number, {'auto_catalog': catalog})

        except Exception as e:
            # Never crash the main flow for catalog
            import logging
            logging.getLogger(__name__).error(f"Auto-catalog error: {e}")

    def _show_full_catalog(self, phone_number):
        """Redirect to the new clickable catalog"""
        return self._show_catalog_menu(phone_number)

    def _show_product_detail(self, phone_number, product_key):
        """Show detailed view of a single product with variants and actions"""
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})
        product = products.get(product_key)

        if not product:
            return [{"type": "text", "content": "\u26a0\ufe0f Product not found."}]

        # If product has a hierarchy tree, show tree drill-down instead
        hierarchy = product.get('hierarchy', [])
        tree = product.get('tree', {})
        if hierarchy and tree:
            return self._handle_tree_drilldown(phone_number, f"cat_tree_{product_key}")

        name = product.get('name', 'Unknown')
        brand = product.get('brand', '')
        sold = product.get('total_sold', 0)
        bought = product.get('total_bought', 0)
        sell_prices = product.get('sell_prices', [])
        buy_prices = product.get('buy_prices', [])
        customers = product.get('customers', [])
        suppliers = product.get('suppliers', [])
        variants = product.get('variants', {})
        units = product.get('units', {})
        tax_rate = product.get('tax_rate', '')
        recipe = product.get('recipe', {})
        inventory = product.get('inventory', {})

        # Build detail message
        msg = f"\U0001f4e6 *{name}*\n"
        msg += "\u2501" * 15 + "\n\n"

        if brand:
            msg += f"\U0001f3f7\ufe0f Brand: *{brand}*\n"
        
        # Pricing
        if sell_prices:
            avg_sell = sum(sell_prices) // len(sell_prices)
            msg += f"\U0001f4b0 Sell Price: *\u20a6{avg_sell:,}*\n"
        if buy_prices:
            avg_buy = sum(buy_prices) // len(buy_prices)
            msg += f"\U0001f6d2 Buy Price: *\u20a6{avg_buy:,}*\n"
        if sell_prices and buy_prices:
            margin = round((avg_sell - avg_buy) / avg_sell * 100)
            msg += f"\U0001f4ca Margin: *{margin}%*\n"

        msg += "\n"

        # Stats
        if sold:
            msg += f"\U0001f4c8 Total Sold: {sold}\n"
        if bought:
            msg += f"\U0001f4e6 Total Bought: {bought}\n"
        if inventory.get('finished_goods'):
            msg += f"\U0001f3ed In Stock: {inventory['finished_goods']}\n"

        # Units
        if units and units.get('conversions'):
            conv_parts = [f"1 {u}={c} {units.get('base_unit', 'pcs')}" for u, c in units['conversions'].items()]
            msg += f"\u2696\ufe0f Units: {' | '.join(conv_parts)}\n"

        # Tax
        if tax_rate:
            msg += f"\U0001f4b1 Tax: {tax_rate}% {product.get('tax_type', 'VAT')}\n"

        # Recipe (manufacturing)
        if recipe:
            msg += f"\U0001f4cb Recipe: \u20a6{recipe.get('cost_per_unit', 0):,}/unit ({recipe.get('batch_size', 0)} per batch)\n"

        # Customers & Suppliers
        if customers:
            msg += f"\n\U0001f465 Buyers: {', '.join(customers[:5])}\n"
        if suppliers:
            msg += f"\U0001f3ea Suppliers: {', '.join(suppliers[:3])}\n"

        # Variants section
        if variants:
            msg += f"\n\U0001f3a8 *Variants ({len(variants)}):*\n"
            for vk, vv in list(variants.items())[:6]:
                v_name = vv.get('name', vk)
                v_sold = vv.get('total_sold', 0)
                v_stock = vv.get('stock', 0)
                v_price = vv.get('sell_price', 0)
                msg += f"  \u2022 {v_name}"
                if v_price:
                    msg += f" \u2014 \u20a6{v_price:,}"
                if v_sold:
                    msg += f" ({v_sold} sold)"
                if v_stock:
                    msg += f" [Stock: {v_stock}]"
                msg += "\n"

        msg += "\n" + "\u2501" * 15

        # Return detail + action buttons
        responses = [{"type": "text", "content": msg}]
        responses.append({"type": "buttons", "content": {
            "body": f"Actions for {name}:",
            "buttons": [
                {"id": f"cat_sell_{product_key}", "title": "\U0001f4b0 Sell This"},
                {"id": f"cat_restock_{product_key}", "title": "\U0001f4e6 Restock"},
                {"id": f"cat_edit_{product_key}", "title": "\u270f\ufe0f Edit"},
            ]
        }})

        return responses

    def _show_variant_detail(self, phone_number, product_key, variant_key):
        """Show detailed view of a specific variant"""
        user = self.db.get_user(phone_number)
        catalog = user.get('auto_catalog', {}) if user else {}
        products = catalog.get('products', {})
        product = products.get(product_key)

        if not product:
            return [{"type": "text", "content": "\u26a0\ufe0f Product not found."}]

        variants = product.get('variants', {})
        variant = variants.get(variant_key)
        if not variant:
            return [{"type": "text", "content": "\u26a0\ufe0f Variant not found."}]

        product_name = product.get('name', 'Product')
        v_name = variant.get('name', variant_key)
        color = variant.get('color', '')
        pattern = variant.get('pattern', '')
        size = variant.get('size', '')
        sell_price = variant.get('sell_price', 0)
        buy_price = variant.get('buy_price', 0)
        stock = variant.get('stock', 0)
        total_sold = variant.get('total_sold', 0)
        last_sold = variant.get('last_sold', '')
        buyers = variant.get('buyers', [])

        msg = f"\U0001f4e6 *{product_name} \u2014 {v_name}*\n"
        msg += "\u2501" * 15 + "\n\n"

        if color:
            msg += f"\U0001f3a8 Color: *{color}*\n"
        if pattern:
            msg += f"\U0001f4d0 Pattern: *{pattern}*\n"
        if size:
            msg += f"\U0001f4cf Size: *{size}*\n"
        msg += "\n"

        if sell_price:
            msg += f"\U0001f4b0 Sell Price: *\u20a6{sell_price:,}*\n"
        if buy_price:
            msg += f"\U0001f6d2 Buy Price: *\u20a6{buy_price:,}*\n"
        if sell_price and buy_price:
            margin = round((sell_price - buy_price) / sell_price * 100)
            msg += f"\U0001f4ca Margin: *{margin}%*\n"
        msg += "\n"

        msg += f"\U0001f4e6 In Stock: *{stock}*\n"
        msg += f"\U0001f4c8 Total Sold: *{total_sold}*\n"
        if last_sold:
            msg += f"\U0001f4c5 Last Sold: {last_sold}\n"
        if buyers:
            msg += f"\U0001f465 Buyers: {', '.join(buyers[:5])}\n"

        msg += "\n" + "\u2501" * 15

        return [
            {"type": "text", "content": msg},
            {"type": "buttons", "content": {
                "body": f"Quick actions for {v_name}:",
                "buttons": [
                    {"id": f"cat_sell_{product_key}", "title": "\U0001f4b0 Sell This"},
                    {"id": f"cat_restock_{product_key}", "title": "\U0001f4e6 Restock"},
                ]
            }}
        ]


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

