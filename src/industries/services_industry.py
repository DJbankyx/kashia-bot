# src/industries/services_industry.py
"""Services industry branch — full menu with job recording, supplies, clients.

Key differences from Trading:
- "Sales" = Jobs/Services performed (cleaning, repair, consultation, delivery)
- "Purchases" = Supply purchases (consumables + equipment)
- "Expenses" = Operating costs (transport, rent, utilities)
- Catalog = Services offered + Supplies (consumables & equipment)
- COGS = Service Costs (materials consumed per job)
- Reports show: Service Revenue - Direct Costs - Expenses = Net Profit
"""

from industries.base import BaseIndustry
from utils.whatsapp_ui import list_response, text_response, button_response, format_amount


class ServicesIndustry(BaseIndustry):
    """Provide services — cleaning, consulting, repair, logistics, salon, etc."""

    INDUSTRY_KEY = "services"
    EMOJI = "💼"
    LABEL = "Services"

    TERMS = {
        "sale": "Job/Service",
        "purchase": "Supply Purchase",
        "expense": "Operating Expense",
        "catalog": "Services & Supplies",
        "catalog_item": "Service/Supply",
    }

    EXAMPLES = {
        "sale": "cleaned Alhaji's office 25K",
        "purchase": "bought cleaning supplies 8K",
    }

    GUIDED_PROMPTS = {
        "ask_item_sale": "💼 What service did you provide?\n\n_(e.g. haircut, cleaning, repair, delivery, consultation)_",
        "ask_item_purchase": "📦 What supplies did you buy?\n\n_(e.g. blades, chemicals, tools, fuel, oil)_",
        "ask_amount": "💰 How much?\n\n_(e.g. 50000, 150K, 1.2M)_\n\n_Or type hours × rate (e.g. 4 x 5000)_",
        "ask_vendor_sale": "👤 Who was the client?\n\n_(client name or type *skip*)_",
        "ask_vendor_purchase": "👤 Where did you buy from?\n\n_(supplier name or type *skip*)_",
        "ask_vendor_expense": "👤 Who did you pay?\n\n_(name or type *skip*)_",
        "ask_details": "🏷️ Any extra details?\n\nLocation, duration, job description...\n\nType details or *skip*",
    }

    # ─────────────────────────────────────────────────────────
    # MAIN MENU — 4 sections
    # ─────────────────────────────────────────────────────────

    def show_home_menu(self, phone_number: str) -> list:
        """Services home menu with 4 sections."""
        return [list_response(
            header="💼 Kashia",
            body="What would you like to do?",
            button_text="☰ Menu",
            sections=[
                {
                    "title": "📝 Quick Actions",
                    "rows": [
                        {"id": "record_sale", "title": "💼 Record Job/Service",
                         "description": "Completed a service for a client"},
                        {"id": "record_purchase", "title": "📦 Buy Supplies",
                         "description": "Consumables, tools, materials"},
                        {"id": "record_expense", "title": "💸 Record Expense",
                         "description": "Transport, rent, utilities"},
                    ]
                },
                {
                    "title": "📂 Sections",
                    "rows": [
                        {"id": "sec_personal", "title": "👤 Personal Info",
                         "description": "Profile, bank, address"},
                        {"id": "sec_business", "title": "💼 Business",
                         "description": "Dashboard, reports, docs, export"},
                        {"id": "sec_supplies", "title": "📦 Supplies & Catalog",
                         "description": "Services, consumables, equipment"},
                        {"id": "sec_crm", "title": "👥 Clients",
                         "description": "Client contacts & history"},
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
        """Handle services-specific section buttons."""
        if button_id == "sec_personal":
            return None  # Router → profile summary

        if button_id == "sec_business":
            return self._show_business_menu(phone_number)

        if button_id == "sec_supplies":
            return None  # Router → catalog.show_menu()

        if button_id == "sec_crm":
            return self._show_crm_menu(phone_number)

        if button_id == "sec_settings":
            return self._show_settings_menu(phone_number)

        # ── Services dashboard ──
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
        """Business sub-menu for services."""
        return [list_response(
            header="💼 Business",
            body="Your service business tools.",
            button_text="Select",
            sections=[{
                "title": "Business Tools",
                "rows": [
                    {"id": "biz_dashboard", "title": "📈 Dashboard",
                     "description": "Revenue, jobs this month"},
                    {"id": "biz_sales", "title": "💼 Jobs/Services",
                     "description": "All completed jobs"},
                    {"id": "biz_expenses", "title": "💸 Expenses",
                     "description": "Operating costs"},
                    {"id": "biz_purchases", "title": "📦 Supply Purchases",
                     "description": "Consumables & equipment bought"},
                    {"id": "biz_reports", "title": "📊 Reports",
                     "description": "P&L, this week, this month"},
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
        """CRM sub-menu for services (clients focused)."""
        return [list_response(
            header="👥 Clients",
            body="Manage your clients and suppliers.",
            button_text="Select",
            sections=[{
                "title": "Client Management",
                "rows": [
                    {"id": "crm_all", "title": "📇 All Contacts",
                     "description": "Browse clients & suppliers"},
                    {"id": "crm_add", "title": "➕ Add Client",
                     "description": "Save name, phone, type"},
                    {"id": "crm_top_customers", "title": "💰 Top Clients",
                     "description": "Ranked by total spending"},
                    {"id": "crm_top_suppliers", "title": "🏪 Top Suppliers",
                     "description": "Where you buy supplies"},
                    {"id": "crm_reminders", "title": "⏰ Debt Reminders",
                     "description": "Nudge clients to pay"},
                    {"id": "crm_insights", "title": "📊 Client Insights",
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
    # SERVICES DASHBOARD
    # ─────────────────────────────────────────────────────────

    def _show_dashboard(self, phone_number: str) -> list:
        """Services dashboard — revenue, jobs, top clients, low supplies."""
        from services.database import Database
        from datetime import datetime

        db = Database()
        now = datetime.now()
        start_date = now.strftime("%Y-%m-01")
        end_date = now.strftime("%Y-%m-%d")

        user = db.get_user(phone_number) or {}
        business_name = user.get("business_name", "Business")

        transactions = db.get_transactions_by_period(phone_number, start_date, end_date) or []

        # Categorize
        jobs = [t for t in transactions if t.get("type") == "sale"]
        purchases = [t for t in transactions if t.get("type") == "purchase"]
        expenses = [t for t in transactions if t.get("type") == "expense"]

        revenue = sum(int(t.get("amount", 0)) for t in jobs)
        supply_cost = sum(int(t.get("amount", 0)) for t in purchases)
        opex = sum(int(t.get("amount", 0)) for t in expenses)
        net_profit = revenue - supply_cost - opex
        job_count = len(jobs)
        avg_job = revenue // job_count if job_count > 0 else 0

        # Top clients this month
        client_totals = {}
        for t in jobs:
            vendor = t.get("vendor", "")
            if vendor and vendor.lower() not in ("sold", "bought", "paid", "received"):
                client_totals[vendor] = client_totals.get(vendor, 0) + int(t.get("amount", 0))

        top_clients = sorted(client_totals.items(), key=lambda x: x[1], reverse=True)[:3]

        # Build dashboard
        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"💼  *{business_name}*",
            f"_{now.strftime('%B %Y')} Dashboard_",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"💰 *Revenue:*       {format_amount(revenue)}",
            f"💼 *Jobs Done:*     {job_count}",
            f"📊 *Avg per Job:*   {format_amount(avg_job)}",
            f"",
            f"📦 *Supplies:*      {format_amount(supply_cost)}",
            f"💸 *Expenses:*      {format_amount(opex)}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"{'📈' if net_profit >= 0 else '📉'} *Net Profit:*    {format_amount(net_profit)}",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        if top_clients:
            lines.append("")
            lines.append("🏆 *Top Clients:*")
            for i, (name, total) in enumerate(top_clients, 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                lines.append(f"  {medal} {name} — {format_amount(total)}")

        # Low supplies warning
        catalog = user.get("product_catalog", {})
        products = catalog.get("products", {}) if isinstance(catalog, dict) else {}
        low_supplies = []
        for key, prod in products.items():
            item_type = prod.get("item_type", "")
            stock = int(prod.get("stock", 0))
            if item_type == "consumable" and stock <= 5:
                low_supplies.append(f"  ⚠️ {prod.get('name', key)}: {stock} left")

        if low_supplies:
            lines.append("")
            lines.append("🚨 *Low Supplies:*")
            lines.extend(low_supplies[:5])

        # Recurring services due
        recurring = user.get("recurring_services", [])
        due_soon = []
        if recurring:
            from datetime import timedelta
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
                {"id": "record_sale", "title": "💼 Record Job"},
                {"id": "record_purchase", "title": "📦 Buy Supplies"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # Record rows
    # ─────────────────────────────────────────────────────────

    def _get_record_rows(self) -> list:
        return [
            {"id": "record_sale", "title": "💼 Record Job/Service",
             "description": "Completed a service for a client"},
            {"id": "record_purchase", "title": "📦 Buy Supplies",
             "description": "Consumables, tools, materials"},
            {"id": "record_expense", "title": "💸 Record Expense",
             "description": "Transport, rent, utilities"},
        ]
