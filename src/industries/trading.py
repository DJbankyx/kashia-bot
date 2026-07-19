# src/industries/trading.py
"""Trading & Retail industry branch — full 4-section menu."""

from industries.base import BaseIndustry
from utils.whatsapp_ui import list_response, text_response


class TradingIndustry(BaseIndustry):
    """Buy and sell goods — shops, markets, online stores."""

    INDUSTRY_KEY = "trading"
    EMOJI = "🛍️"
    LABEL = "Trading & Retail"

    TERMS = {
        "sale": "Sale",
        "purchase": "Purchase",
        "expense": "Expense",
        "catalog": "Product Catalog",
        "catalog_item": "Product",
    }

    EXAMPLES = {
        "sale": "sold 10 Nike shoes to Sandra for 150K",
        "purchase": "bought 50 pairs socks from Alhaji 200K",
    }

    GUIDED_PROMPTS = {
        "ask_item_sale": "📦 What did you sell?\n\n_(e.g. shoes, bags, clothes, cement)_",
        "ask_item_purchase": "📦 What did you buy?\n\n_(e.g. flour, fabric, stock, goods)_",
        "ask_amount": "💰 How much?\n\n_(e.g. 50000, 150K, 1.2M)_",
        "ask_vendor_sale": "👤 Who did you sell to?\n\n_(customer name or type *skip*)_",
        "ask_vendor_purchase": "👤 Who did you buy from?\n\n_(supplier name or type *skip*)_",
        "ask_vendor_expense": "👤 Who did you pay?\n\n_(name or type *skip*)_",
        "ask_details": "🏷️ Any extra details?\n\nBrand, color, size, model...\n\nType details or *skip*",
    }

    # ─────────────────────────────────────────────────────────
    # MAIN MENU — 4 sections
    # ─────────────────────────────────────────────────────────

    def show_home_menu(self, phone_number: str) -> list:
        """Trading home menu with 4 sections."""
        return [list_response(
            header="🛍️ Kashia",
            body="What would you like to do?",
            button_text="☰ Menu",
            sections=[
                {
                    "title": "📝 Quick Actions",
                    "rows": [
                        {"id": "record_sale", "title": "💰 Record Sale", "description": "Sold goods to customer"},
                        {"id": "record_purchase", "title": "📦 Record Purchase", "description": "Bought stock/goods"},
                        {"id": "record_expense", "title": "💸 Record Expense", "description": "Rent, transport, bills"},
                    ]
                },
                {
                    "title": "📂 Sections",
                    "rows": [
                        {"id": "sec_personal", "title": "👤 Personal Info", "description": "Profile, bank, address"},
                        {"id": "sec_business", "title": "💼 Business", "description": "Reports, catalog, debts, docs"},
                        {"id": "sec_crm", "title": "👥 CRM", "description": "Contacts, customers, suppliers"},
                        {"id": "sec_settings", "title": "⚙️ Help & Settings", "description": "Tutorial, upgrade, PIN"},
                    ]
                }
            ]
        )]

    # ─────────────────────────────────────────────────────────
    # SUB-MENUS — one per section
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle trading-specific section buttons."""
        if button_id == "sec_personal":
            return None  # Router handles → shows profile summary + sub-menu

        if button_id == "sec_business":
            return self._show_business_menu(phone_number)

        if button_id == "sec_crm":
            return self._show_crm_menu(phone_number)

        if button_id == "sec_settings":
            return self._show_settings_menu(phone_number)

        # ── Personal Info sub-buttons — delegate to router/personal_info handler ──
        if button_id.startswith("pi_"):
            return None  # Router handles via personal_info handler

        # ── Business sub-buttons ──
        if button_id == "biz_dashboard":
            return None  # Router → profile.show()
        if button_id == "biz_sales":
            return None  # Router → reports.handle_button()
        if button_id == "biz_purchases":
            return None  # Router → reports.handle_button()
        if button_id == "biz_expenses":
            return None  # Router → reports.handle_button()
        if button_id == "biz_reports":
            return None  # Router → reports.show()
        if button_id == "biz_debts":
            return None  # Router → debt.show_summary()
        if button_id == "biz_docs":
            return None  # Router → export.show_options()
        if button_id == "biz_export":
            return None  # Router → export.show_options()

        # ── CRM sub-buttons — delegate to router/contacts handler ──
        if button_id.startswith("crm_"):
            return None  # Router handles via contacts handler

        # ── Settings sub-buttons — delegate to router/settings handler ──
        if button_id.startswith("set_"):
            return None  # Router handles via settings handler

        return None  # Not handled — router will try other handlers

    def _pi_label(self, bid: str) -> str:
        labels = {"pi_business_name": "Business Name", "pi_phone": "Phone Number", "pi_bank": "Bank Details", "pi_address": "Address", "pi_email": "Email", "pi_logo": "Logo Upload", "pi_edit": "Edit Profile"}
        return labels.get(bid, "Personal Info")

    def _crm_label(self, bid: str) -> str:
        labels = {"crm_all": "All Contacts", "crm_add": "Add Contact", "crm_top_customers": "Top Customers", "crm_top_suppliers": "Top Suppliers", "crm_reminders": "Debt Reminders", "crm_insights": "Customer Insights"}
        return labels.get(bid, "CRM")

    def _set_label(self, bid: str) -> str:
        labels = {"set_tutorial": "How to Use", "set_usage": "Usage & Limits", "set_upgrade": "Upgrade Plan", "set_password": "Password", "set_industry": "Change Industry", "set_notify": "Notifications", "set_bug": "Report Problem", "set_reset": "Reset Account"}
        return labels.get(bid, "Settings")

    def _show_personal_menu(self, phone_number: str) -> list:
        """Personal Information sub-menu."""
        return [list_response(
            header="👤 Personal Info",
            body="Manage your profile and business details.",
            button_text="Select",
            sections=[{
                "title": "Personal Information",
                "rows": [
                    {"id": "pi_business_name", "title": "🏢 Business Name", "description": "View or change"},
                    {"id": "pi_phone", "title": "📱 Phone Number", "description": "Your registered number"},
                    {"id": "pi_bank", "title": "🏦 Bank Details", "description": "Account for invoices"},
                    {"id": "pi_address", "title": "📍 Business Address", "description": "Shop/market location"},
                    {"id": "pi_email", "title": "📧 Email", "description": "For digital receipts"},
                    {"id": "pi_logo", "title": "🖼️ Business Logo", "description": "Upload for documents"},
                    {"id": "pi_edit", "title": "✏️ Edit Profile", "description": "Change any detail"},
                ]
            }]
        )]

    def _show_business_menu(self, phone_number: str) -> list:
        """Business sub-menu."""
        return [list_response(
            header="💼 Business",
            body="Your core business tools.",
            button_text="Select",
            sections=[{
                "title": "Business Tools",
                "rows": [
                    {"id": "biz_dashboard", "title": "📈 Dashboard", "description": "Today + month overview"},
                    {"id": "biz_sales", "title": "💰 Sales", "description": "View all sales records"},
                    {"id": "biz_purchases", "title": "📦 Purchases", "description": "View all purchase records"},
                    {"id": "biz_expenses", "title": "💸 Expenses", "description": "View all expenses"},
                    {"id": "biz_reports", "title": "📊 Reports", "description": "Today, week, month, custom"},
                    {"id": "biz_debts", "title": "💳 Debts & Credits", "description": "Who owes, payments"},
                    {"id": "menu_catalog", "title": "📋 Catalog", "description": "Product catalog & inventory"},
                    {"id": "biz_docs", "title": "🧾 Documents", "description": "Invoice, receipt, statement"},
                    {"id": "biz_export", "title": "📁 Export Data", "description": "Excel, CSV download"},
                ]
            }]
        )]

    def _show_crm_menu(self, phone_number: str) -> list:
        """CRM sub-menu."""
        return [list_response(
            header="👥 CRM",
            body="Manage your customers and suppliers.",
            button_text="Select",
            sections=[{
                "title": "Customer Management",
                "rows": [
                    {"id": "crm_all", "title": "📇 All Contacts", "description": "Browse customers & suppliers"},
                    {"id": "crm_add", "title": "➕ Add Contact", "description": "Save name, phone, type"},
                    {"id": "crm_top_customers", "title": "💰 Top Customers", "description": "Ranked by spending"},
                    {"id": "crm_top_suppliers", "title": "🏪 Top Suppliers", "description": "Ranked by purchases"},
                    {"id": "crm_reminders", "title": "⏰ Debt Reminders", "description": "Nudge debtors to pay"},
                    {"id": "crm_insights", "title": "📊 Customer Insights", "description": "Frequency, avg spend"},
                ]
            }]
        )]

    def _show_settings_menu(self, phone_number: str) -> list:
        """Help & Settings sub-menu."""
        return [list_response(
            header="⚙️ Help & Settings",
            body="Configuration and support.",
            button_text="Select",
            sections=[{
                "title": "Help & Settings",
                "rows": [
                    {"id": "set_tutorial", "title": "❓ How to Use", "description": "Quick guide & tutorial"},
                    {"id": "set_usage", "title": "📊 Usage & Limits", "description": "Tier, transactions left"},
                    {"id": "set_upgrade", "title": "⭐ Upgrade Plan", "description": "Free → Basic → Pro"},
                    {"id": "set_password", "title": "🔒 Set Password", "description": "Set or change your PIN"},
                    {"id": "set_industry", "title": "🔄 Change Industry", "description": "Switch business type"},
                    {"id": "set_notify", "title": "🔔 Notifications", "description": "Daily reports on/off"},
                    {"id": "set_bug", "title": "🐛 Report a Problem", "description": "Send feedback"},
                    {"id": "set_reset", "title": "🗑️ Reset Account", "description": "Clear all data"},
                ]
            }]
        )]

    # ─────────────────────────────────────────────────────────
    # Record rows (used by guided recording)
    # ─────────────────────────────────────────────────────────

    def _get_record_rows(self) -> list:
        return [
            {"id": "record_sale", "title": "💰 Record Sale", "description": "Sold goods to customer"},
            {"id": "record_purchase", "title": "📦 Record Purchase", "description": "Bought stock/goods"},
            {"id": "record_expense", "title": "💸 Record Expense", "description": "Rent, transport, bills"},
        ]
