# src/core/router.py
"""The Router — state machine + button dispatcher. The brain of Kashia v2."""

import logging
import re
from core import states
from core.session import SessionManager
from core.onboarding import OnboardingHandler
from utils.whatsapp_ui import text_response
from utils.parser import parse_amount

logger = logging.getLogger(__name__)

# Text shortcuts that bypass NLP (user can still type these)
GREETING_WORDS = {'hi', 'hello', 'hey', 'good morning', 'good evening', 'good afternoon'}
CANCEL_WORDS = {'cancel', 'exit', 'stop', 'quit', 'back', 'nevermind', 'never mind', 'nvm'}
ACK_WORDS = {'okay', 'ok', 'alright', 'sure', 'cool', 'noted', 'fine', 'got it', 'understood', 'right', 'yep', 'yea', 'yeah', 'yes'}
HELP_WORDS = {'help', 'menu', 'what can you do', 'commands'}
DEBT_WORDS = {'who owes me', 'who owe me', 'debtors', 'my debtors'}
I_OWE_WORDS = {'who do i owe', 'what do i owe', 'my debt', 'i owe', 'creditors'}
REPORT_WORDS = {'report', 'today', 'this week', 'this month', 'my sales', 'my purchases'}


class Router:
    """
    Central router. Every message flows through here.
    
    Responsibilities:
    1. Detect message type (button tap vs text)
    2. Check state → route to correct feature handler
    3. For IDLE state text → route to transaction parser (the only NLP path)
    """

    def __init__(self, database, categorizer):
        self.db = database
        self.session = SessionManager(database)
        self.onboarding = OnboardingHandler(self.session, database)
        self.categorizer = categorizer

        # Feature handlers — set after construction by main.py
        self.transactions = None
        self.reports = None
        self.debt = None
        self.catalog = None
        self.contacts = None
        self.export = None
        self.invoices = None
        self.profile = None
        self.personal_info = None
        self.settings = None
        self.production = None

        # Industry handlers — set after construction by main.py
        self.industries = {}  # {"trading": TradingIndustry, ...}

    def process(self, phone_number: str, text: str, message_type: str = "text") -> list:
        """
        Main entry point. Returns list of response dicts.
        
        Each response: {"type": "text"|"buttons"|"list"|"document", "content": ...}
        """
        session = self.session.get(phone_number)
        state = session.get("state", "")
        context = session.get("context", {})
        text_stripped = text.strip()
        text_lower = text_stripped.lower()

        logger.info(f"Router: phone={phone_number}, state={state}, type={message_type}, text={text_stripped[:50]}")

        # ═══════════════════════════════════════════════════════
        # 1. NEW USER — hasn't completed onboarding
        # ═══════════════════════════════════════════════════════
        if state in (states.NEW_USER, "") or not self._user_exists(phone_number):
            return self.onboarding.handle(phone_number, text_stripped, session)

        if state == states.ONBOARDING:
            return self.onboarding.handle(phone_number, text_stripped, session)

        # ═══════════════════════════════════════════════════════
        # 2. BUTTON TAPS — deterministic routing (no NLP)
        # ═══════════════════════════════════════════════════════
        if message_type in ('interactive', 'button_reply', 'list_reply'):
            return self._route_button(phone_number, text_stripped, session)

        # ═══════════════════════════════════════════════════════
        # 3. GLOBAL CANCEL — works in ANY state
        # ═══════════════════════════════════════════════════════
        if text_lower in CANCEL_WORDS and state != states.IDLE:
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled. Send a transaction or tap the menu below.")]

        # ═══════════════════════════════════════════════════════
        # 4. STATE-BASED ROUTING — user is mid-flow
        # ═══════════════════════════════════════════════════════
        if state == states.AWAITING_CONFIRMATION:
            return self.transactions.handle_confirmation(phone_number, text_stripped, session)

        if state == states.AWAITING_CORRECTION:
            return self.transactions.handle_correction(phone_number, text_stripped, session)

        if state == states.GUIDED_RECORDING:
            return self.transactions.handle_guided_step(phone_number, text_stripped, session)

        if state == states.CRM_HINT:
            return self._handle_crm_hint(phone_number, text_stripped, session)

        if state in (states.DEBT_RECORDING, states.DEBT_CONFIRMING, states.DEBT_PAYMENT):
            return self.debt.handle(phone_number, text_stripped, session)

        if state in (states.CATALOG_MENU, states.CATALOG_SETUP_PRODUCTS,
                     states.CATALOG_SETUP_DETAILS, states.CATALOG_ORGANIZE,
                     states.CATALOG_ADD_DATA):
            return self.catalog.handle(phone_number, text_stripped, session)

        if state == states.INVOICING:
            return self.invoices.handle(phone_number, text_stripped, session)

        if state == states.EXPORTING:
            return self.export.handle(phone_number, text_stripped, session)

        if state in (states.EDITING, states.EDIT_TRANSACTION, states.DELETE_CONFIRM):
            return self.transactions.handle_edit(phone_number, text_stripped, session)

        # ── Personal Info flows ──
        if state == states.PERSONAL_INFO:
            return self.personal_info.handle(phone_number, text_stripped, session)

        # ── Settings flows ──
        if state == states.SETTINGS_FLOW:
            return self.settings.handle(phone_number, text_stripped, session)

        # ── CRM Add Contact flow ──
        if state == states.CRM_ADDING:
            return self.contacts.handle(phone_number, text_stripped, session)

        # ── Catalog-aware recording flow ──
        if state == states.CATALOG_RECORDING:
            return self.transactions.handle_catalog_recording(phone_number, text_stripped, session)

        # ── PIN verification flow ──
        if state == states.PIN_VERIFYING:
            from core.pin_guard import verify_pin
            return verify_pin(self.db, self.session, phone_number, text_stripped, session)

        # ── Landing cost flow (after sale) ──
        if state == states.LANDING_COST:
            return self.transactions.handle_landing_cost(phone_number, text_stripped, session)

        # ── Payment method flow (cash/credit after confirmation) ──
        if state == states.PAYMENT_METHOD:
            return self.transactions.handle_payment_method(phone_number, text_stripped, session)

        # ── Production recording flow (manufacturing) ──
        if state == states.PRODUCTION_RECORDING:
            return self.production.handle(phone_number, text_stripped, session)

        # ═══════════════════════════════════════════════════════
        # 5. IDLE STATE — the default
        # ═══════════════════════════════════════════════════════
        # "Save number" text shortcut
        if text_lower.startswith("save number") or text_lower.startswith("save contact"):
            return self.contacts.save_contact_from_text(phone_number, text_stripped)

        # Debt shortcuts — very common to type naturally
        if text_lower in DEBT_WORDS:
            return self.debt.show_summary(phone_number)

        if text_lower in I_OWE_WORDS:
            return self.debt.show_summary(phone_number)

        # Report shortcuts — users still type these
        if text_lower in REPORT_WORDS:
            return self.reports.show(phone_number)

        # Catalog shortcuts
        CATALOG_WORDS = {'my catalog', 'catalog', 'show catalog', 'my products',
                         'products', 'inventory', 'stock'}
        if text_lower in CATALOG_WORDS:
            return self.catalog.show_menu(phone_number)

        # Export shortcuts
        if text_lower in {'export', 'excel', 'csv', 'download'}:
            return self.export.show_options(phone_number)

        # CRM shortcuts
        if text_lower in {'contacts', 'customers', 'suppliers', 'crm',
                          'all contacts', 'my contacts', 'contact list'}:
            return self.contacts.show(phone_number)


        # Greetings → show home menu
        if text_lower in GREETING_WORDS or text_lower in HELP_WORDS:
            return self._show_home_menu(phone_number)

        # Acknowledgements → friendly nudge
        if text_lower in ACK_WORDS:
            return [text_response("👍 Ready when you are! Type what you bought or sold, or tap the menu.")]

        # Conversion pattern (e.g. "1 carton = 12 pieces") → redirect
        if re.match(r'^\d+\s*(carton|dozen|bag|pack|bundle|box|crate|set|case)s?\s*=\s*\d+', text_lower):
            return [text_response(
                "📦 That looks like a unit conversion!\n\n"
                "To set conversions, go to *Catalog* in the menu and use *Add Product Data*."
            )]

        # ═══════════════════════════════════════════════════════
        # 6. THE ONLY NLP PATH — Transaction Recording
        # ═══════════════════════════════════════════════════════
        # Everything that isn't a button, greeting, cancel, or known state
        # gets treated as a transaction to record.
        return self.transactions.record(phone_number, text_stripped, session)

    # ─────────────────────────────────────────────────────────
    # Button Routing
    # ─────────────────────────────────────────────────────────

    def _route_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Route interactive button/list taps to the correct handler."""
        bid = button_id.lower().strip()
        state = session.get("state", states.IDLE)

        logger.info(f"Button route: {bid} (state={state})")

        # ── Confirmation buttons (from transaction confirmation card) ──
        if bid in ("confirm_yes", "yes", "✅ yes"):
            if state == states.AWAITING_CONFIRMATION:
                return self.transactions.handle_confirmation(phone_number, "yes", session)
            if state == states.DEBT_CONFIRMING:
                return self.debt.handle(phone_number, "yes", session)

        if bid in ("confirm_edit", "edit", "✏️ edit"):
            if state == states.AWAITING_CONFIRMATION:
                return self.transactions.handle_confirmation(phone_number, "edit", session)

        if bid in ("confirm_cancel", "btn_cancel", "cancel", "❌ cancel"):
            self.session.reset(phone_number)
            return [text_response("❌ Cancelled. Send a transaction or tap the menu.")]

        # ── Edit field selection (from pending transaction correction flow) ──
        if bid.startswith("edit_") and state == states.AWAITING_CORRECTION:
            return self.transactions.handle_correction(phone_number, bid, session)

        # ── Done button ──
        if bid == "btn_done":
            if state in (states.CATALOG_SETUP_DETAILS, states.CATALOG_ORGANIZE, states.CATALOG_ADD_DATA):
                return self.catalog.handle(phone_number, "done", session)

        # ── Back button ──
        if bid == "btn_back":
            if state == states.GUIDED_RECORDING:
                return self.transactions.handle_guided_step(phone_number, "__BACK__", session)

        # ── Yes/No buttons ──
        if bid == "btn_yes":
            if state == states.DEBT_CONFIRMING:
                return self.debt.handle(phone_number, "yes", session)
            if state == states.DELETE_CONFIRM:
                return self.transactions.handle_edit(phone_number, "yes", session)

        if bid == "btn_no":
            if state == states.DEBT_CONFIRMING:
                return self.debt.handle(phone_number, "no", session)
            self.session.reset(phone_number)
            return [text_response("👍 Okay. Send a transaction or tap the menu.")]

        # ── Recording buttons (from home menu) ──
        if bid.startswith("record_"):
            return self._start_guided_recording(phone_number, bid)

        # ── Feature menu buttons ──
        feature_map = {
            "menu_report": lambda: self.reports.show(phone_number),
            "menu_profile": lambda: self.profile.show(phone_number),
            "menu_catalog": lambda: self.catalog.show_menu(phone_number),
            "menu_debts": lambda: self.debt.show_summary(phone_number),
            "menu_contacts": lambda: self.contacts.show(phone_number),
            "menu_export": lambda: self.export.show_options(phone_number),
            "menu_invoice": lambda: self.invoices.start(phone_number),
            "menu_home": lambda: self._show_home_menu(phone_number),
        }

        handler = feature_map.get(bid)
        if handler:
            # Reset state when navigating to menu (user is leaving current flow)
            if bid == "menu_home":
                self.session.reset(phone_number)
            return handler()

        # ── Industry-specific buttons ──
        industry = self._get_industry_handler(phone_number)
        if industry:
            result = industry.handle_button(phone_number, bid, session)
            if result:
                return result

        # ── CRM buttons ──
        if bid.startswith("crm_"):
            # CRM hint buttons (cash/transfer/credit) from transaction flow
            if bid in ("crm_cash", "crm_transfer", "crm_credit"):
                return self._handle_crm_button(phone_number, bid, session)
            # CRM type selection during add contact flow
            if bid.startswith("crm_type_"):
                session = self.session.get(phone_number)
                return self.contacts.handle(phone_number, bid, session)
            # All other crm_ buttons → contacts handler
            return self.contacts.handle_button(phone_number, bid, session)

        # ── Catalog buttons ──
        if bid.startswith("cat_"):
            return self.catalog.handle_button(phone_number, bid, session)

        # ── Catalog Recording buttons (catrec_*) ──
        if bid.startswith("catrec_"):
            return self.transactions.handle_catalog_recording(phone_number, bid, session)

        # ── Transaction Edit buttons (txedit_* and txact_*) ──
        if bid.startswith("txedit_") or bid.startswith("txact_"):
            return self.transactions.handle_edit_button(phone_number, bid, session)

        # ── Landing Cost buttons (lc_*) ──
        if bid.startswith("lc_"):
            return self.transactions.handle_landing_cost(phone_number, bid, session)

        # ── Payment Method buttons (pm_*) ──
        if bid.startswith("pm_"):
            return self.transactions.handle_payment_method(phone_number, bid, session)

        # ── Generate Invoice/Receipt after sale (gen_*) ──
        if bid.startswith("gen_invoice_"):
            tx_id = bid[12:]
            return [{"type": "__GEN_INVOICE__", "content": {"tx_id": tx_id}}]
        if bid.startswith("gen_receipt_"):
            tx_id = bid[12:]
            return [{"type": "__GEN_RECEIPT__", "content": {"tx_id": tx_id}}]
        if bid == "gen_skip":
            return [text_response("👍 _Send your next transaction or tap ☰ Menu._")]

        # ── Production buttons (prod_*) ──
        if bid.startswith("prod_"):
            return self.production.handle_button(phone_number, bid, session)

        # ── Debt buttons ──
        if bid.startswith("debt_"):
            return self.debt.handle_button(phone_number, bid, session)

        # ── Report buttons ──
        if bid.startswith("report_"):
            return self.reports.handle_button(phone_number, bid, session)

        # ── Section sub-menu buttons (industry-specific) ──
        if bid.startswith("sec_") or bid.startswith("pi_") or bid.startswith("biz_") or bid.startswith("crm_") or bid.startswith("set_"):
            # Try industry handler first
            industry = self._get_industry_handler(phone_number)
            if industry:
                result = industry.handle_button(phone_number, bid, session)
                if result:
                    return result

            # ── sec_personal → show profile summary + sub-menu ──
            if bid == "sec_personal" and self.personal_info:
                return self.personal_info.show_profile(phone_number)

            # ── sec_inventory → show inventory/catalog menu ──
            if bid == "sec_inventory":
                return self.catalog.show_menu(phone_number)

            # ── Personal Info buttons ──
            if bid.startswith("pi_") or bid == "set_password":
                if self.personal_info:
                    return self.personal_info.handle_button(phone_number, bid)

            # ── Settings buttons ──
            if bid.startswith("set_") or bid.startswith("set_ind_"):
                if self.settings:
                    return self.settings.handle_button(phone_number, bid)

            # ── Industry change list selections ──
            if bid.startswith("set_ind_"):
                if self.settings:
                    return self.settings.handle_button(phone_number, bid)

            # ── Business tab buttons → reports handler ──
            biz_map = {
                "biz_dashboard":  lambda: self.profile.show(phone_number),
                "biz_reports":    lambda: self.reports.show(phone_number),
                "biz_sales":      lambda: self.reports.handle_button(phone_number, "biz_sales", session),
                "biz_purchases":  lambda: self.reports.handle_button(phone_number, "biz_purchases", session),
                "biz_expenses":   lambda: self.reports.handle_button(phone_number, "biz_expenses", session),
                "biz_debts":      lambda: self.debt.show_summary(phone_number),
                "biz_docs":       lambda: self.export.show_options(phone_number),
                "biz_export":     lambda: self.export.show_options(phone_number),
            }
            handler = biz_map.get(bid)
            if handler:
                return handler()

        # ── Export buttons ──
        if bid.startswith("export_"):
            return self.export.handle_button(phone_number, bid, session)

        # ── Industry change list taps (set_ind_trading etc) ──
        if bid.startswith("set_ind_"):
            if self.settings:
                return self.settings.handle_button(phone_number, bid)

        # ── pi_bank flow — bank step confirm button ──
        if bid == "pi_bank_start_flow":
            if self.personal_info:
                return self.personal_info._bank_step_1(phone_number)

        # ── Unknown button — show home menu ──
        logger.warning(f"Unknown button: {bid}")
        return self._show_home_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # Home Menu
    # ─────────────────────────────────────────────────────────

    def _show_home_menu(self, phone_number: str) -> list:
        """Show the industry-specific home menu."""
        industry = self._get_industry_handler(phone_number)
        if industry:
            return industry.show_home_menu(phone_number)

        # Fallback — generic menu
        from utils.whatsapp_ui import list_response
        return [list_response(
            header="📒 Kashia",
            body="What would you like to do?",
            button_text="☰ Menu",
            sections=[{
                "title": "Quick Actions",
                "rows": [
                    {"id": "record_sale", "title": "💰 Record Sale"},
                    {"id": "record_expense", "title": "💸 Record Expense"},
                    {"id": "menu_report", "title": "📊 Reports"},
                    {"id": "menu_profile", "title": "👤 Dashboard"},
                ]
            }]
        )]

    # ─────────────────────────────────────────────────────────
    # Guided Recording
    # ─────────────────────────────────────────────────────────

    def _start_guided_recording(self, phone_number: str, button_id: str) -> list:
        """Start a button-driven guided recording flow.
        If user has a catalog with products, show product list first (catalog-aware).
        Otherwise fall back to free-text guided flow.
        """
        # Map button IDs to transaction types
        type_map = {
            "record_sale": "sale",
            "record_purchase": "purchase",
            "record_expense": "expense",
            "record_production": "production",
            "record_job": "sale",  # services "job" = sale
        }

        tx_type = type_map.get(button_id, "sale")

        # Production uses its own dedicated handler (manufacturing only)
        if tx_type == "production":
            return self.production.start_production(phone_number)

        # Expenses don't use catalog — always free-text
        if tx_type == "expense":
            return self._start_freetext_guided(phone_number, tx_type)

        # Check if user has catalog products
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {}) if isinstance(catalog, dict) else {}

        # Filter to valid products (must be dicts with at least a name)
        valid_products = {k: v for k, v in products.items() if isinstance(v, dict)}

        if valid_products:
            # Show catalog products as selection list
            return self._start_catalog_recording(phone_number, tx_type, valid_products)
        else:
            # No catalog — use free-text guided flow
            return self._start_freetext_guided(phone_number, tx_type)

    def _start_freetext_guided(self, phone_number: str, tx_type: str) -> list:
        """Original free-text guided recording (no catalog)."""
        industry = self._get_industry_handler(phone_number)

        prompt = "What did you sell/buy?"
        if industry:
            if tx_type == "sale":
                prompt = industry.get_guided_prompt("ask_item_sale")
            elif tx_type == "purchase":
                prompt = industry.get_guided_prompt("ask_item_purchase")
            elif tx_type == "expense":
                prompt = "💸 What was the expense for?\n\n_(e.g. transport, rent, airtime, fuel)_"

        self.session.save(phone_number, states.GUIDED_RECORDING, {
            "guided_type": tx_type,
            "guided_step": "item",
            "guided_data": {},
        })

        return [text_response(prompt)]

    def _start_catalog_recording(self, phone_number: str, tx_type: str, products: dict) -> list:
        """Show flat product list for sale/purchase recording."""
        from utils.whatsapp_ui import list_response

        label = "sell" if tx_type == "sale" else "buy"

        # Get product rows from catalog handler
        rows = self.catalog.get_product_list_for_recording(phone_number)

        # Add "Other" option
        rows.append({
            "id": "catrec___other__",
            "title": "📝 Other / Not Listed",
            "description": "Type the item name manually",
        })

        self.session.save(phone_number, states.CATALOG_RECORDING, {
            "cat_rec_type": tx_type,
            "cat_rec_step": "pick_product",
            "cat_rec_path": [],
        })

        return [list_response(
            header=f"📦 What did you {label}?",
            body="Pick from your catalog:",
            button_text="Select Product",
            sections=[{"title": "Your Products", "rows": rows}]
        )]

    # ─────────────────────────────────────────────────────────
    # CRM Hint (post-transaction prompt)
    # ─────────────────────────────────────────────────────────

    def _handle_crm_hint(self, phone_number: str, text: str, session: dict) -> list:
        """Handle the optional CRM prompt after saving a transaction."""
        context = session.get("context", {})
        step = context.get("crm_step", "ask_name")
        text_lower = text.lower().strip()

        # Skip / break out — greetings, cancel, or skip words
        if text_lower in GREETING_WORDS or text_lower in HELP_WORDS:
            self.session.reset(phone_number)
            return self._show_home_menu(phone_number)

        if text_lower in ('skip', 'no', 'nah', 'nope') or text_lower in CANCEL_WORDS:
            self.session.reset(phone_number)
            return [text_response("👍 No problem. Send your next transaction anytime!")]

        # If user sent a new transaction (has an amount), break out
        amount = parse_amount(text)
        if amount:
            self.session.reset(phone_number)
            return self.transactions.record(phone_number, text, self.session.get(phone_number))

        if step == "ask_name":
            # Save the name to the transaction
            name = text.strip()
            tx_id = context.get("transaction_id")
            tx_type = context.get("tx_type", "sale")

            if tx_id and name:
                self.db.update_transaction(phone_number, tx_id, {"vendor": name})

            amount_val = context.get("amount", 0)

            # If large amount, ask about payment method
            if float(amount_val) >= 50000:
                self.session.save(phone_number, states.CRM_HINT, {
                    **context,
                    "crm_step": "ask_payment",
                    "vendor_name": name,
                })
                from utils.whatsapp_ui import button_response
                return [button_response(
                    f"👤 *{name}* noted!\n\n💳 How did they pay?",
                    [
                        {"id": "crm_cash", "title": "💵 Cash"},
                        {"id": "crm_transfer", "title": "🏦 Transfer"},
                        {"id": "crm_credit", "title": "📝 On Credit"},
                    ]
                )]

            # Small amount — done
            self.session.reset(phone_number)
            return [
                text_response(f"👤 *{name}* noted!"),
                button_response("What's next?", [
                    {"id": "record_sale", "title": "💰 Record Sale"},
                    {"id": "record_purchase", "title": "📦 Record Purchase"},
                    {"id": "record_expense", "title": "💸 Record Expense"},
                ])
            ]

        if step == "ask_payment":
            self.session.reset(phone_number)
            from utils.whatsapp_ui import button_response
            return [
                text_response("👍 Got it!"),
                button_response("What's next?", [
                    {"id": "record_sale", "title": "💰 Record Sale"},
                    {"id": "record_purchase", "title": "📦 Record Purchase"},
                    {"id": "record_expense", "title": "💸 Record Expense"},
                ])
            ]

        # Fallback
        self.session.reset(phone_number)
        return [text_response("👍 Ready for your next transaction!")]

    def _handle_crm_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle CRM prompt buttons (cash/transfer/credit)."""
        context = session.get("context", {})

        if button_id == "crm_credit":
            # Record as debt
            vendor_name = context.get("vendor_name", "Customer")
            amount = context.get("amount", 0)
            tx_type = context.get("tx_type", "sale")
            tx_id = context.get("transaction_id")

            # Record the debt
            if tx_type == "sale":
                self.db.record_debt(phone_number, vendor_name, float(amount), 'owed_to_me',
                                    f"Credit sale - tx#{tx_id[:8] if tx_id else 'unknown'}")

            self.session.reset(phone_number)
            return [text_response(
                f"📝 Recorded! *{vendor_name}* owes you *{self._fmt_amount(amount)}*.\n\n"
                f"Send your next transaction anytime."
            )]

        # Cash or transfer — just note it and move on
        self.session.reset(phone_number)
        payment_type = "cash" if button_id == "crm_cash" else "transfer"
        # Optionally update the transaction with payment method
        tx_id = context.get("transaction_id")
        if tx_id:
            self.db.update_transaction(phone_number, tx_id, {"payment_method": payment_type})

        from utils.whatsapp_ui import button_response
        return [
            text_response("👍 Got it!"),
            button_response("What's next?", [
                {"id": "record_sale", "title": "💰 Record Sale"},
                {"id": "record_purchase", "title": "📦 Record Purchase"},
                {"id": "record_expense", "title": "💸 Record Expense"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────

    def _user_exists(self, phone_number: str) -> bool:
        """Check if user has completed onboarding."""
        user = self.db.get_user(phone_number)
        if not user:
            return False
        return user.get("onboarding_complete", False)

    def _get_industry_handler(self, phone_number: str):
        """Get the industry handler for this user."""
        user = self.db.get_user(phone_number)
        if not user:
            return self.industries.get("trading")  # default
        # Check both field names (industry_class is v2, business_type is legacy)
        industry_key = user.get("industry_class", user.get("business_type", "trading"))
        return self.industries.get(industry_key, self.industries.get("trading"))

    def _fmt_amount(self, amount) -> str:
        """Quick amount formatter."""
        try:
            num = float(amount)
            return f"₦{num:,.0f}"
        except (ValueError, TypeError):
            return f"₦{amount}"
