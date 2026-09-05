# src/industries/base.py
"""Base industry class — shared interface all industries implement."""

from utils.whatsapp_ui import list_response


class BaseIndustry:
    """
    Each industry implements this interface.
    
    The industry class only handles UI differences:
    - Menu structure
    - Terminology
    - Examples
    - Guided flow prompts
    
    Business logic (saving transactions, generating reports) is handled
    by feature modules — industries just customize the presentation layer.
    """

    # Override in subclass
    INDUSTRY_KEY = "base"
    EMOJI = "📒"
    LABEL = "Business"

    # Terminology — override in subclass
    TERMS = {
        "sale": "Sale",
        "purchase": "Purchase",
        "expense": "Expense",
        "catalog": "Catalog",
        "catalog_item": "Product",
    }

    # Example transactions — override in subclass
    EXAMPLES = {
        "sale": "sold goods 50K",
        "purchase": "bought stock 30K",
    }

    # Guided flow prompts — override in subclass
    GUIDED_PROMPTS = {
        "ask_item_sale": "📦 What did you sell?",
        "ask_item_purchase": "📦 What did you buy?",
        "ask_amount": "💰 How much?",
        "ask_vendor_sale": "👤 Who did you sell to? (or type *skip*)",
        "ask_vendor_purchase": "👤 Who did you buy from? (or type *skip*)",
        "ask_vendor_expense": "👤 Who did you pay? (or type *skip*)",
        "ask_details": "🏷️ Any extra details? (brand, size, color — or type *skip*)",
    }

    def show_home_menu(self, phone_number: str) -> list:
        """The greeting/main menu with industry-specific buttons."""
        return [list_response(
            header=f"{self.EMOJI} Kashia",
            body="What would you like to do?",
            button_text="☰ Menu",
            sections=self._build_menu_sections()
        )]

    def _build_menu_sections(self) -> list:
        """Override to customize menu sections."""
        return [
            {
                "title": "📝 Record",
                "rows": self._get_record_rows(),
            },
            {
                "title": "💼 Business",
                "rows": [
                    {"id": "menu_profile", "title": "👤 My Dashboard", "description": "Sales overview & profile"},
                    {"id": "menu_report", "title": "📊 Reports", "description": "Today, this week, this month"},
                    {"id": "menu_debts", "title": "💳 Debts & Credits", "description": "Who owes, who I owe"},
                    {"id": "menu_contacts", "title": "📇 Contacts", "description": "Customers & suppliers"},
                    {"id": "menu_catalog", "title": "📋 Catalog", "description": self.TERMS['catalog']},
                    {"id": "menu_export", "title": "📁 Export & Docs", "description": "Excel, invoices, receipts"},
                ]
            }
        ]

    def _get_record_rows(self) -> list:
        """Override to customize record section buttons."""
        return [
            {"id": "record_sale", "title": "💰 Record Sale", "description": "Sold goods to customer"},
            {"id": "record_purchase", "title": "📦 Record Purchase", "description": "Bought stock/goods"},
            {"id": "record_expense", "title": "💸 Record Expense", "description": "Rent, transport, bills"},
        ]

    def start_guided_recording(self, phone_number: str, button_id: str) -> list:
        """Start industry-specific guided recording. Override if needed."""
        # Default: handled by router._start_guided_recording
        return None

    # ─────────────────────────────────────────────────────────
    # Telegram "One Tidy Box" fast-entry spec (Telegram-only presentation).
    # Returns the per-industry wording + defaults the tap-first flow needs, so
    # the flow never hardcodes "sell"/"buy"/"Sales & Income". WhatsApp ignores
    # this entirely (it uses GUIDED_PROMPTS via the existing guided flow).
    # ─────────────────────────────────────────────────────────
    def fastentry_spec(self, tx_type: str, is_service: bool = False) -> dict:
        """
        Describe how the tidy-box flow should present a sale/purchase for THIS
        industry. `is_service` is only meaningful for hybrid (product vs service).

        Keys:
          title        — header shown at the top of the card (e.g. "Record sale")
          item_prompt  — the "what?" question
          person_label — who the counterparty is (customer / buyer / client)
          category     — default ledger category for this tx_type
          is_service   — True => a service/job (the flow skips quantity)
        """
        terms = self.get_terms()
        if tx_type == "sale":
            title = f"🧾 Record {terms.get('sale', 'sale')}"
            item_prompt = self.get_guided_prompt("ask_item_sale")
            person_label = "customer"
            category = "Sales & Income"
        elif tx_type == "purchase":
            title = f"📦 Record {terms.get('purchase', 'purchase')}"
            item_prompt = self.get_guided_prompt("ask_item_purchase")
            person_label = "supplier"
            category = "Goods & Stock"
        else:
            title = f"💸 Record {terms.get('expense', 'expense')}"
            item_prompt = "💸 What was the expense for?"
            person_label = "payee"
            category = "Other Expenses"

        return {
            "title": title,
            "item_prompt": item_prompt,
            "person_label": person_label,
            "category": category,
            "is_service": is_service,
        }

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle industry-specific buttons not in shared map. Override if needed."""
        return None

    def get_terms(self) -> dict:
        """Return terminology dict."""
        return self.TERMS

    def get_examples(self) -> dict:
        """Return examples dict."""
        return self.EXAMPLES

    def get_guided_prompt(self, prompt_key: str) -> str:
        """Get a guided flow prompt by key."""
        return self.GUIDED_PROMPTS.get(prompt_key, "Continue:")

    def get_profile_label(self) -> str:
        """Label shown on profile page."""
        return f"{self.EMOJI} {self.LABEL}"
