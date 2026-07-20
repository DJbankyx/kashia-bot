# src/industries/hybrid.py
"""Hybrid industry branch — full menu for businesses that sell goods AND services.

Key differences:
- Quick Actions split into PRODUCT sale vs SERVICE job
- "Purchases" covers both stock (for resale) and supplies (for services)
- Catalog has both Products (goods) and Supplies (consumables)
- Reports show combined revenue: Product Sales + Service Revenue
- Dashboard splits metrics by goods vs services

Examples: Phone shop + repairs, catering + food retail, salon + beauty products
"""

from industries.base import BaseIndustry
from utils.whatsapp_ui import list_response, text_response, button_response, format_amount


class HybridIndustry(BaseIndustry):
    """Combination of goods + services — sell products AND provide services."""

    INDUSTRY_KEY = "hybrid"
    EMOJI = "⚡"
    LABEL = "Hybrid (Goods + Services)"

    TERMS = {
        "sale": "Sale/Service",
        "purchase": "Purchase",
        "expense": "Expense",
        "catalog": "Products & Supplies",
        "catalog_item": "Product/Supply",
    }

    EXAMPLES = {
        "sale": "sold 5 phone cases to Emeka 25K",
        "purchase": "bought screen protectors from supplier 50K",
    }

    GUIDED_PROMPTS = {
        "ask_item_sale": "📦 What did you sell?\n\n_(e.g. phone case, charger, screen protector)_",
        "ask_item_service": "💼 What service did you provide?\n\n_(e.g. phone repair, screen fix, consultation)_",
        "ask_item_purchase": "📦 What did you buy?\n\n_(stock for resale OR supplies for services)_",
        "ask_amount": "💰 How much?\n\n_(e.g. 50000, 150K, 1.2M)_",
        "ask_vendor_sale": "👤 Who was it for?\n\n_(customer/client name or type *skip*)_",
        "ask_vendor_purchase": "👤 Who did you buy from?\n\n_(supplier name or type *skip*)_",
        "ask_vendor_expense": "👤 Who did you pay?\n\n_(name or type *skip*)_",
        "ask_details": "🏷️ Any extra details?\n\nBrand, model, color, description...\n\nType details or *skip*",
    }

    # ─────────────────────────────────────────────────────────
    # MAIN MENU — Dual actions (goods + services)
    # ─────────────────────────────────────────────────────────

    def show_home_menu(self, phone_number: str) -> list:
        """Hybrid home menu with product + service quick actions."""
        return [list_response(
            header="⚡ Kashia",
            body="What would you like to do?",
            button_text="☰ Menu",
            sections=[
                {
                    "title": "📝 Quick Actions",
                    "rows": [
                        {"id": "record_sale", "title": "💰 Sell Product",
                         "description": "Sold goods to a customer"},
                        {"id": "record_job", "title": "💼 Record Service",
                         "description": "Completed a job/service"},
                        {"id": "record_purchase", "title": "📦 Record Purchase",
                         "description": "Bought stock or supplies"},
                        {"id": "record_expense", "title": "💸 Record Expense",
                         "description": "Rent, transport, utilities"},
                    ]
                },
                {
                    "title": "📂 Sections",
                    "rows": [
                        {"id": "sec_personal", "title": "👤 Personal Info",
                         "description": "Profile, bank, address"},
                        {"id": "sec_business", "title": "⚡ Business",
                         "description": "Dashboard, reports, debts, docs"},
                        {"id": "sec_inventory", "title": "📊 Products & Supplies",
                         "description": "Stock levels, costs, catalog"},
                        {"id": "sec_crm", "title": "👥 Clients & Contacts",
                         "description": "Customers, suppliers, history"},
                        {"id": "sec_settings", "title": "⚙️ Help & Settings",
                         "description": "Tutorial, upgrade, PIN"},
                    ]
                }
            ]
        )]

    # ─────────────────────────────────────────────────────────
    # BUTTON ROUTER
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle hybrid-specific section buttons."""
        if button_id == "sec_personal":
            return None  # Router → profile summary

        if button_id == "sec_business":
            return self._show_business_menu(phone_number)

        if button_id == "sec_inventory":
            return None  # Router → catalog.show_menu()

        if button_id == "sec_crm":
            return self._show_crm_menu(phone_number)

        if button_id == "sec_settings":
            return self._show_settings_menu(phone_number)

        # ── Dashboard ──
        if button_id == "biz_dashboard":
            return self._show_dashboard(phone_number)

        # ── Sub-buttons delegate to router ──
        if button_id.startswith("pi_"):
            return None
        if button_id.startswith("biz_"):
            return None
        if button_id.startswith("crm_"):
            return None
        if button_id.startswith("set_"):
            return None

        return None

    # ─────────────────────────────────────────────────────────
    # SUB-MENUS
    # ─────────────────────────────────────────────────────────

    def _show_business_menu(self, phone_number: str) -> list:
        """Business sub-menu for hybrid."""
        return [list_response(
            header="⚡ Business",
            body="Your business tools — goods + services combined.",
            button_text="Select",
            sections=[{
                "title": "Business Tools",
                "rows": [
                    {"id": "biz_dashboard", "title": "📈 Dashboard",
                     "description": "Products + services overview"},
                    {"id": "biz_sales", "title": "💰 Product Sales",
                     "description": "All goods sold"},
                    {"id": "biz_purchases", "title": "📦 Purchases",
                     "description": "Stock + supplies bought"},
                    {"id": "biz_expenses", "title": "💸 Expenses",
                     "description": "Operating costs"},
                    {"id": "biz_reports", "title": "📊 Reports",
                     "description": "P&L, margins, combined view"},
                    {"id": "biz_debts", "title": "💳 Debts & Credits",
                     "description": "Who owes, payments"},
                    {"id": "biz_recurring", "title": "🔁 Recurring Services",
                     "description": "Regular clients & reminders"},
                    {"id": "biz_docs", "title": "🧾 Documents",
                     "description": "Invoice, receipt, statement"},
                    {"id": "biz_export", "title": "📁 Export Data",
                     "description": "Excel, CSV download"},
                ]
            }]
        )]

    def _show_crm_menu(self, phone_number: str) -> list:
        """CRM sub-menu for hybrid (clients + suppliers)."""
        return [list_response(
            header="👥 Clients & Contacts",
            body="Manage your customers and suppliers.",
            button_text="Select",
            sections=[{
                "title": "Contact Management",
                "rows": [
                    {"id": "crm_all", "title": "📇 All Contacts",
                     "description": "Browse customers & suppliers"},
                    {"id": "crm_add", "title": "➕ Add Contact",
                     "description": "Save name, phone, type"},
                    {"id": "crm_top_customers", "title": "💰 Top Customers",
                     "description": "Ranked by total spending"},
                    {"id": "crm_top_suppliers", "title": "🏪 Top Suppliers",
                     "description": "Where you buy stock/supplies"},
                    {"id": "crm_reminders", "title": "⏰ Debt Reminders",
                     "description": "Nudge debtors to pay"},
                    {"id": "crm_insights", "title": "📊 Customer Insights",
                     "description": "Frequency, avg spend, last seen"},
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
                    {"id": "set_tutorial", "title": "❓ How to Use",
                     "description": "Quick guide & tutorial"},
                    {"id": "set_usage", "title": "📊 Usage & Limits",
                     "description": "Tier, transactions left"},
                    {"id": "set_upgrade", "title": "⭐ Upgrade Plan",
                     "description": "Free → Basic → Pro"},
                    {"id": "set_password", "title": "🔒 Set Password",
                     "description": "Set or change your PIN"},
                    {"id": "set_industry", "title": "🔄 Change Industry",
                     "description": "Switch business type"},
                    {"id": "set_notify", "title": "🔔 Notifications",
                     "description": "Daily reports on/off"},
                    {"id": "set_bug", "title": "🐛 Report a Problem",
                     "description": "Send feedback"},
                    {"id": "set_reset", "title": "🗑️ Reset Account",
                     "description": "Clear all data"},
                ]
            }]
        )]

    # ─────────────────────────────────────────────────────────
    # HYBRID DASHBOARD — Split goods vs services
    # ─────────────────────────────────────────────────────────

    def _show_dashboard(self, phone_number: str) -> list:
        """Hybrid dashboard — combined goods + services metrics."""
        from services.database import Database
        from datetime import datetime

        db = Database()
        now = datetime.now()
        start_date = now.strftime("%Y-%m-01")
        end_date = now.strftime("%Y-%m-%d")

        user = db.get_user(phone_number) or {}
        business_name = user.get("business_name", "Business")

        transactions = db.get_transactions_by_period(phone_number, start_date, end_date) or []

        # Categorize — split sales by whether they look like services or products
        # Service indicators: category contains "Service", or description matches service patterns
        product_sales = []
        service_sales = []
        for t in transactions:
            if t.get("type") not in ("sale", "income"):
                continue
            cat = t.get("category", "")
            desc = (t.get("description", "") + " " + t.get("item_name", "")).lower()
            # Heuristic: if category mentions service, or item_type is service-like
            if "service" in cat.lower() or t.get("item_type") == "service":
                service_sales.append(t)
            else:
                product_sales.append(t)

        purchases = [t for t in transactions if t.get("type") == "purchase"]
        expenses = [t for t in transactions if t.get("type") == "expense"]

        product_revenue = sum(int(t.get("amount", 0)) for t in product_sales)
        service_revenue = sum(int(t.get("amount", 0)) for t in service_sales)
        total_revenue = product_revenue + service_revenue
        total_purchases = sum(int(t.get("amount", 0)) for t in purchases)
        total_expenses = sum(int(t.get("amount", 0)) for t in expenses)
        gross_profit = total_revenue - total_purchases
        net_profit = gross_profit - total_expenses

        # Build dashboard
        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"⚡  *{business_name}*",
            f"_{now.strftime('%B %Y')} Dashboard_",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"💰 *Product Sales:*  {format_amount(product_revenue)}",
            f"💼 *Service Revenue:* {format_amount(service_revenue)}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📈 *Total Revenue:*  {format_amount(total_revenue)}",
            f"",
            f"📦 *Purchases:*     {format_amount(total_purchases)}",
            f"💸 *Expenses:*      {format_amount(total_expenses)}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"{'📈' if net_profit >= 0 else '📉'} *Net Profit:*    {format_amount(net_profit)}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"📝 Transactions: {len(transactions)}",
            f"  💰 Product sales: {len(product_sales)}",
            f"  💼 Service jobs: {len(service_sales)}",
            f"  📦 Purchases: {len(purchases)}",
            f"  💸 Expenses: {len(expenses)}",
        ]

        # Low stock warning
        catalog = user.get("product_catalog", {})
        products = catalog.get("products", {}) if isinstance(catalog, dict) else {}
        low_stock = []
        for key, prod in products.items():
            stock = int(prod.get("stock", 0))
            if 0 <= stock <= 3:
                low_stock.append(f"  ⚠️ {prod.get('name', key)}: {stock} left")

        if low_stock:
            lines.append("")
            lines.append("🚨 *Low Stock:*")
            lines.extend(low_stock[:5])

        # Recurring services due
        recurring = user.get("recurring_services", [])
        if recurring:
            from datetime import timedelta
            due_soon = []
            for svc in recurring:
                next_due = svc.get("next_due", "")
                if next_due and next_due <= (now + timedelta(days=3)).strftime("%Y-%m-%d"):
                    client = svc.get("client", "")
                    service = svc.get("service", "")
                    due_soon.append(f"  🔔 {client}: {service}")

            if due_soon:
                lines.append("")
                lines.append("🔁 *Due Soon:*")
                lines.extend(due_soon[:3])

        return [
            text_response("\n".join(lines)),
            button_response("Quick actions:", [
                {"id": "record_sale", "title": "💰 Sell Product"},
                {"id": "record_job", "title": "💼 Record Service"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # Record rows (used by guided recording)
    # ─────────────────────────────────────────────────────────

    def _get_record_rows(self) -> list:
        return [
            {"id": "record_sale", "title": "💰 Sell Product",
             "description": "Sold goods to a customer"},
            {"id": "record_job", "title": "💼 Record Service",
             "description": "Completed a job/service"},
            {"id": "record_purchase", "title": "📦 Record Purchase",
             "description": "Bought stock or supplies"},
            {"id": "record_expense", "title": "💸 Record Expense",
             "description": "Rent, transport, utilities"},
        ]
