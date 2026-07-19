# src/industries/manufacturing.py
"""Manufacturing industry branch — full 4-section menu.

Key differences from Trading:
- "Purchases" = Raw Materials (inputs for production)
- "Sales" = Finished Goods (output sold to customers/retailers)
- "Expenses" = Production costs (labour, overhead, utilities, maintenance)
- Catalog = Product Recipes / Bill of Materials
- COGS = Production & Manufacturing (not Goods & Stock)
- Reports show: Revenue - Production Costs = Gross Margin
"""

from industries.base import BaseIndustry
from utils.whatsapp_ui import list_response, text_response


class ManufacturingIndustry(BaseIndustry):
    """Produce/make goods — factories, workshops, food production."""

    INDUSTRY_KEY = "manufacturing"
    EMOJI = "🏭"
    LABEL = "Manufacturing"

    TERMS = {
        "sale": "Output Sale",
        "purchase": "Raw Material",
        "expense": "Production Cost",
        "catalog": "Product & Materials",
        "catalog_item": "Product/Material",
    }

    EXAMPLES = {
        "sale": "sold 200 bottles detergent to Shoprite 80K",
        "purchase": "bought 5 drums sulphonic acid 1.3M",
    }

    GUIDED_PROMPTS = {
        "ask_item_sale": "📦 What finished product did you sell?\n\n_(e.g. detergent, bread, furniture, bags)_",
        "ask_item_purchase": "🧱 What raw material/input did you buy?\n\n_(e.g. sulphonic acid, flour, wood, fabric)_",
        "ask_amount": "💰 How much?\n\n_(e.g. 50000, 150K, 1.2M)_",
        "ask_vendor_sale": "👤 Who did you sell to?\n\n_(buyer/retailer name or type *skip*)_",
        "ask_vendor_purchase": "👤 Who did you buy from?\n\n_(supplier name or type *skip*)_",
        "ask_vendor_expense": "👤 Who did you pay?\n\n_(name or type *skip*)_",
        "ask_details": "🏷️ Any extra details?\n\nBatch, quantity, grade, size...\n\nType details or *skip*",
    }

    # ─────────────────────────────────────────────────────────
    # MAIN MENU — 4 sections
    # ─────────────────────────────────────────────────────────

    def show_home_menu(self, phone_number: str) -> list:
        """Manufacturing home menu with 4 sections."""
        return [list_response(
            header="🏭 Kashia",
            body="What would you like to do?",
            button_text="☰ Menu",
            sections=[
                {
                    "title": "📝 Quick Actions",
                    "rows": [
                        {"id": "record_sale", "title": "💰 Sell Output",
                         "description": "Sold finished goods to buyer"},
                        {"id": "record_production", "title": "🏭 Record Production",
                         "description": "Produced/manufactured items"},
                        {"id": "record_purchase", "title": "🧱 Buy Raw Materials",
                         "description": "Purchased inputs/supplies"},
                        {"id": "record_expense", "title": "💸 Production Cost",
                         "description": "Labour, overhead, utilities"},
                    ]
                },
                {
                    "title": "📂 Sections",
                    "rows": [
                        {"id": "sec_personal", "title": "👤 Personal Info",
                         "description": "Profile, bank, address"},
                        {"id": "sec_business", "title": "🏭 Production",
                         "description": "Reports, materials, costs, docs"},
                        {"id": "sec_crm", "title": "👥 CRM",
                         "description": "Buyers, suppliers, contacts"},
                        {"id": "sec_settings", "title": "⚙️ Help & Settings",
                         "description": "Tutorial, upgrade, PIN"},
                    ]
                }
            ]
        )]

    # ─────────────────────────────────────────────────────────
    # SUB-MENUS
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle manufacturing-specific section buttons."""
        if button_id == "sec_personal":
            return None  # Router → profile summary

        if button_id == "sec_business":
            return self._show_business_menu(phone_number)

        if button_id == "sec_crm":
            return self._show_crm_menu(phone_number)

        if button_id == "sec_settings":
            return self._show_settings_menu(phone_number)

        # ── Manufacturing dashboard ──
        if button_id == "biz_dashboard":
            return self._show_dashboard(phone_number)

        # ── All sub-buttons delegate to router ──
        if button_id.startswith("pi_"):
            return None
        if button_id == "biz_production_history":
            return self._show_production_history(phone_number)
        if button_id == "biz_material_usage":
            return self._show_material_usage(phone_number)
        if button_id.startswith("biz_"):
            return None
        if button_id.startswith("crm_"):
            return None
        if button_id.startswith("set_"):
            return None

        return None

    def _show_business_menu(self, phone_number: str) -> list:
        """Production/Business sub-menu."""
        return [list_response(
            header="🏭 Production",
            body="Your manufacturing business tools.",
            button_text="Select",
            sections=[{
                "title": "Production Tools",
                "rows": [
                    {"id": "biz_dashboard", "title": "📈 Dashboard",
                     "description": "Today's output, month summary"},
                    {"id": "biz_sales", "title": "💰 Output Sales",
                     "description": "Finished goods sold"},
                    {"id": "biz_purchases", "title": "🧱 Raw Materials",
                     "description": "Inputs purchased"},
                    {"id": "biz_expenses", "title": "💸 Production Costs",
                     "description": "Labour, overhead, utilities"},
                    {"id": "biz_production_history", "title": "🏭 Production History",
                     "description": "Past production runs"},
                    {"id": "biz_material_usage", "title": "📊 Material Usage",
                     "description": "What materials were consumed"},
                    {"id": "biz_reports", "title": "📊 Reports",
                     "description": "P&L, margins, costs breakdown"},
                    {"id": "biz_debts", "title": "💳 Debts & Credits",
                     "description": "Who owes, supplier credits"},
                    {"id": "menu_catalog", "title": "📋 Products & Materials",
                     "description": "Product catalog & recipes"},
                    {"id": "biz_docs", "title": "🧾 Documents",
                     "description": "Invoice, receipt, statement"},
                    {"id": "biz_export", "title": "📁 Export Data",
                     "description": "Excel, CSV download"},
                ]
            }]
        )]

    def _show_crm_menu(self, phone_number: str) -> list:
        """CRM sub-menu for manufacturing."""
        return [list_response(
            header="👥 CRM",
            body="Manage your buyers and suppliers.",
            button_text="Select",
            sections=[{
                "title": "Contacts",
                "rows": [
                    {"id": "crm_all", "title": "📇 All Contacts",
                     "description": "Browse buyers & suppliers"},
                    {"id": "crm_add", "title": "➕ Add Contact",
                     "description": "Save name, phone, type"},
                    {"id": "crm_top_customers", "title": "💰 Top Buyers",
                     "description": "Ranked by total orders"},
                    {"id": "crm_top_suppliers", "title": "🧱 Top Suppliers",
                     "description": "Ranked by material purchases"},
                    {"id": "crm_reminders", "title": "⏰ Debt Reminders",
                     "description": "Nudge debtors to pay"},
                    {"id": "crm_insights", "title": "📊 Buyer Insights",
                     "description": "Frequency, avg order, last seen"},
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
    # Record rows (used by guided recording)
    # ─────────────────────────────────────────────────────────

    def _get_record_rows(self) -> list:
        return [
            {"id": "record_sale", "title": "💰 Sell Output",
             "description": "Sold finished goods to buyer"},
            {"id": "record_purchase", "title": "🧱 Buy Raw Materials",
             "description": "Purchased inputs/supplies"},
            {"id": "record_expense", "title": "💸 Production Cost",
             "description": "Labour, overhead, utilities"},
        ]

    # ─────────────────────────────────────────────────────────
    # MANUFACTURING DASHBOARD
    # ─────────────────────────────────────────────────────────

    def _show_dashboard(self, phone_number: str) -> list:
        """Manufacturing dashboard — output summary, material costs, yield rates."""
        from services.database import Database
        from utils.whatsapp_ui import text_response, button_response, format_amount
        from datetime import datetime, timedelta

        db = Database()
        now = datetime.now()
        start_date = now.strftime("%Y-%m-01")
        end_date = now.strftime("%Y-%m-%d")

        user = db.get_user(phone_number) or {}
        business_name = user.get("business_name", "Factory")

        transactions = db.get_transactions_by_period(phone_number, start_date, end_date) or []

        # Categorize
        sales = [t for t in transactions if t.get("type") == "sale"]
        productions = [t for t in transactions if t.get("type") == "production"]
        purchases = [t for t in transactions if t.get("type") == "purchase"]
        expenses = [t for t in transactions if t.get("type") == "expense"]

        revenue = sum(int(t.get("amount", 0)) for t in sales)
        material_cost = sum(int(t.get("amount", 0)) for t in purchases)
        production_cost = sum(int(t.get("amount", 0)) for t in productions)
        opex = sum(int(t.get("amount", 0)) for t in expenses)
        total_cogs = material_cost + production_cost
        gross_profit = revenue - total_cogs
        net_profit = gross_profit - opex

        # Production stats
        total_produced = 0
        total_waste = 0
        for p in productions:
            extra = p.get("extra_details", {}) or {}
            total_produced += int(extra.get("good_quantity", p.get("quantity", 0)) or 0)
            total_waste += int(extra.get("waste", 0) or 0)

        yield_rate = int(total_produced / (total_produced + total_waste) * 100) if (total_produced + total_waste) > 0 else 0

        # Build dashboard
        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🏭  *{business_name}*",
            f"_{now.strftime('%B %Y')} Dashboard_",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"💰 *Revenue:*        {format_amount(revenue)}",
            f"🧱 *Materials:*      {format_amount(material_cost)}",
            f"🏭 *Production:*     {format_amount(production_cost)}",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📈 *Gross Profit:*   {format_amount(gross_profit)}",
            f"💸 *Expenses:*       {format_amount(opex)}",
            f"{'📈' if net_profit >= 0 else '📉'} *Net Profit:*     {format_amount(net_profit)}",
            f"",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🏭 *Production Stats:*",
            f"  📦 Output: {total_produced} units",
            f"  🗑️ Waste: {total_waste} units",
            f"  ✅ Yield: {yield_rate}%",
            f"  🔄 Batches: {len(productions)}",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        # Low stock materials warning
        catalog = user.get("product_catalog", {})
        products = catalog.get("products", {}) if isinstance(catalog, dict) else {}
        low_materials = []
        for key, prod in products.items():
            recipe_used_in = any(
                any(m.get("material", "").lower().replace(" ", "_") == key
                    for m in p.get("recipe", []))
                for p in products.values()
            )
            # Check if it's a raw material (used in recipes or has no recipe of its own)
            stock = int(prod.get("stock", 0))
            if stock <= 5 and stock >= 0:
                low_materials.append(f"  ⚠️ {prod.get('name', key)}: {stock} left")

        if low_materials:
            lines.append("")
            lines.append("🚨 *Low Materials:*")
            lines.extend(low_materials[:5])

        return [
            text_response("\n".join(lines)),
            button_response("Quick actions:", [
                {"id": "record_production", "title": "🏭 Produce"},
                {"id": "record_purchase", "title": "🧱 Buy Materials"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # MANUFACTURING-SPECIFIC REPORTS
    # ─────────────────────────────────────────────────────────

    def _show_production_history(self, phone_number: str) -> list:
        """Show recent production runs."""
        from services.database import Database
        from utils.whatsapp_ui import text_response, format_amount
        from datetime import datetime, timedelta

        db = Database()
        now = datetime.now()
        start_date = now.strftime("%Y-%m-01")
        end_date = now.strftime("%Y-%m-%d")

        transactions = db.get_transactions_by_period(phone_number, start_date, end_date) or []
        productions = [t for t in transactions if t.get("type") == "production"]

        if not productions:
            return [text_response(
                "🏭 *Production History*\n\n"
                "No production recorded this month.\n\n"
                "_Tap 🏭 Record Production from the menu to log a production run._"
            )]

        total_produced = 0
        total_cost = 0
        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "🏭  *Production History*",
            f"_{now.strftime('%B %Y')}_",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for p in sorted(productions, key=lambda x: x.get("created_at", ""), reverse=True)[:10]:
            desc = p.get("description", "")
            amount = int(p.get("amount", 0))
            date = p.get("date", "")[-5:]
            extra = p.get("extra_details", {}) or {}
            qty = extra.get("production_quantity", p.get("quantity", ""))
            cost_per = extra.get("cost_per_unit", 0)

            total_cost += amount
            if qty:
                total_produced += int(qty) if str(qty).isdigit() else 0

            lines.append(f"• {desc}")
            if cost_per:
                lines.append(f"  💰 Cost: {format_amount(amount)} ({format_amount(cost_per)}/unit)")
            elif amount:
                lines.append(f"  💰 Cost: {format_amount(amount)}")
            lines.append(f"  📅 {date}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📦 Total produced: *{total_produced} units*")
        lines.append(f"💰 Total cost: *{format_amount(total_cost)}*")
        if total_produced > 0:
            lines.append(f"📐 Avg cost/unit: *{format_amount(total_cost // total_produced)}*")

        return [text_response("\n".join(lines))]

    def _show_material_usage(self, phone_number: str) -> list:
        """Show raw material consumption and stock levels."""
        from services.database import Database
        from utils.whatsapp_ui import text_response, format_amount

        db = Database()
        user = db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if not products:
            return [text_response(
                "📊 *Material Usage*\n\n"
                "No products in catalog yet.\n\n"
                "_Add your raw materials and finished products to track usage._"
            )]

        # Find products that are used as materials (appear in recipes)
        materials_used = {}  # material_key → {name, stock, used_in}
        for key, data in products.items():
            recipe = data.get("recipe", [])
            for mat in recipe:
                mat_name = mat.get("material", "")
                mat_key = mat_name.lower().replace(" ", "_")
                if mat_key not in materials_used:
                    materials_used[mat_key] = {
                        "name": mat_name,
                        "used_in": [],
                        "stock": 0,
                        "landing_cost": 0,
                    }
                materials_used[mat_key]["used_in"].append(data.get("name", key))

        # Get stock levels for materials
        for mat_key, mat_info in materials_used.items():
            if mat_key in products:
                mat_info["stock"] = products[mat_key].get("stock", products[mat_key].get("stock_count", 0))
                mat_info["landing_cost"] = products[mat_key].get("landing_cost", 0)

        if not materials_used:
            return [text_response(
                "📊 *Material Usage*\n\n"
                "No recipes set yet.\n\n"
                "_Set a recipe for your products to track material usage._\n"
                "_Go to: 🏭 Record Production → 📋 Set/Edit Recipe_"
            )]

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "📊  *Material Usage & Stock*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for mat_key, info in materials_used.items():
            name = info["name"]
            stock = info["stock"]
            cost = info["landing_cost"]
            used_in = info["used_in"]

            # Stock indicator
            if stock <= 0:
                indicator = "🔴"
            elif stock <= 10:
                indicator = "🟡"
            else:
                indicator = "🟢"

            lines.append(f"{indicator} *{name}*")
            lines.append(f"  Stock: {stock}")
            if cost:
                lines.append(f"  Cost: {format_amount(cost)} per unit")
            lines.append(f"  Used in: {', '.join(used_in[:3])}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("_Tap 🏭 Record Production to update stock automatically._")

        return [text_response("\n".join(lines))]
