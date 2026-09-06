# src/features/catalog.py
"""Inventory & Catalog — Simple flat product system.

Products are a flat list. Each product has:
- name: display name
- stock: current quantity on hand
- landing_cost: what you pay per unit
- category: optional grouping tag (e.g. "SUV", "Sedan")
- variants: optional list of sub-types (e.g. ["Black", "White", "2019", "2024"])

No deep tree. No 5-level navigation. Just products + stock + cost.
"""

import logging
import re

from core import states
from utils.whatsapp_ui import (
    text_response, button_response, list_response, format_amount
)
from utils.parser import parse_amount

logger = logging.getLogger(__name__)

# State for catalog multi-step flows
CATALOG_STATE = "CATALOG_ADD_DATA"


class CatalogHandler:
    """Simple flat inventory system."""

    def __init__(self, session_mgr, database, categorizer=None):
        self.session = session_mgr
        self.db = database
        self.categorizer = categorizer

    # ─────────────────────────────────────────────────────────
    # MAIN MENU
    # ─────────────────────────────────────────────────────────

    def show_menu(self, phone_number: str) -> list:
        """Show inventory/catalog action menu."""
        products = self.ensure_item_types(phone_number)
        count = len([p for p in products.values() if p.get("item_type") != "overhead"])
        total_stock = sum(int(p.get("stock", 0)) for p in products.values() if p.get("item_type") != "overhead")

        # Check user's industry
        user = self.db.get_user(phone_number) or {}
        industry = user.get("industry_class", user.get("business_type", "trading"))
        is_manufacturing = industry in ("manufacturing", "hybrid")
        is_services = industry == "services"

        # ── Services industry: "My Services" focused menu ──
        if is_services:
            service_count = sum(1 for p in products.values() if p.get("item_type") == "service")
            supply_count = sum(1 for p in products.values() if p.get("item_type") in ("raw_material", "consumable", "supply"))
            # Count items without a type as services (from onboarding)
            untyped = sum(1 for p in products.values() if p.get("item_type") not in ("service", "raw_material", "consumable", "supply"))
            service_count += untyped

            body = f"💼 *My Services & Supplies* — {count} item{'s' if count != 1 else ''}"
            body += "\n\nWhat would you like to do?"

            rows = [
                {"id": "cat_view_services", "title": f"💼 View My Services ({service_count})",
                 "description": "Services you offer with pricing"},
                {"id": "cat_view_supplies", "title": f"📦 View Supplies ({supply_count})",
                 "description": "Consumables & equipment"},
                {"id": "cat_edit", "title": "✏️ Edit Service/Supply",
                 "description": "Change name, price, or delete"},
                {"id": "cat_add_service", "title": "➕ Add Service",
                 "description": "Add a new service with pricing"},
                {"id": "cat_add", "title": "📦 Add Supply",
                 "description": "Add consumable or equipment"},
                {"id": "cat_set_price", "title": "💰 Set Service Price",
                 "description": "Update your standard rates"},
                {"id": "cat_supply_template", "title": "📋 Set Supply Template",
                 "description": "What supplies each service uses"},
                {"id": "cat_adjust", "title": "📐 Adjust Supply Stock",
                 "description": "Update supply quantities"},
            ]

            return [list_response(
                header="💼 Services & Supplies",
                body=body,
                button_text="Select Action",
                sections=[{
                    "title": "Actions",
                    "rows": rows,
                }]
            )]

        # ── Manufacturing: products + materials ──
        # Count by type
        finished_count = sum(1 for p in products.values() if p.get("item_type") == "finished_product")
        material_count = sum(1 for p in products.values() if p.get("item_type") == "raw_material")

        body = f"📊 *Inventory* — {count} item{'s' if count != 1 else ''}"
        if total_stock > 0:
            body += f" · {total_stock} total units"
        body += "\n\nWhat would you like to do?"

        rows = []

        if is_manufacturing:
            rows.extend([
                {"id": "cat_view_products", "title": f"🏭 View Products ({finished_count})",
                 "description": "Finished goods you manufacture"},
                {"id": "cat_view_materials", "title": f"🧱 View Materials ({material_count})",
                 "description": "Raw materials & inputs"},
            ])
        else:
            # Trading: single stock view
            rows.append(
                {"id": "cat_stock", "title": "📊 View Stock Levels",
                 "description": "See all products with quantities"}
            )

        rows.append(
            {"id": "cat_edit", "title": "✏️ Edit Product",
             "description": "Change name, stock, cost, or delete"}
        )

        rows.append(
            {"id": "cat_add", "title": "➕ Add Product",
             "description": "Add a new product to inventory"}
        )

        if is_manufacturing:
            rows.append(
                {"id": "cat_recipe", "title": "📋 Set Recipe / BOM",
                 "description": "Define materials per product"}
            )

        rows.extend([
            {"id": "cat_cost", "title": "🏷️ Set Landing Cost",
             "description": "Set/update cost per unit"},
            {"id": "cat_adjust", "title": "📐 Adjust Stock",
             "description": "Manually add or set stock quantity"},
            {"id": "cat_unit", "title": "📏 Set Stock Unit",
             "description": "e.g. kg, litres, pieces"},
            {"id": "cat_conversion", "title": "📦 Set Conversion",
             "description": "e.g. 1 carton = 24 pieces"},
            {"id": "cat_remove", "title": "🗑️ Remove Product",
             "description": "Delete a product from inventory"},
        ])

        return [list_response(
            header="📊 Inventory",
            body=body,
            button_text="Select Action",
            sections=[{
                "title": "Inventory Actions",
                "rows": rows[:10],  # WhatsApp max 10 rows
            }]
        )]

    # ─────────────────────────────────────────────────────────
    # BUTTON ROUTER
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Route all cat_* buttons."""
        if button_id == "cat_stock":
            return self._show_stock_levels(phone_number)

        if button_id == "cat_add":
            return self._start_add_product(phone_number)

        if button_id == "cat_recipe":
            # Delegate to production handler's recipe setup
            # Return a marker that the router will handle
            return [{"type": "__START_RECIPE_SETUP__"}]

        if button_id == "cat_cost":
            return self._start_set_cost(phone_number)

        if button_id == "cat_adjust":
            return self._start_adjust_stock(phone_number)

        if button_id == "cat_variants":
            return self._start_add_variants(phone_number)

        if button_id == "cat_variant_cost":
            return self._start_variant_cost(phone_number)

        if button_id == "cat_conversion":
            return self._start_set_conversion(phone_number)

        if button_id == "cat_unit":
            return self._start_set_unit(phone_number)

        if button_id == "cat_remove":
            return self._start_remove_product(phone_number)

        if button_id == "cat_clear_all":
            return self._start_clear_catalog(phone_number)

        # ── New: View & Edit buttons ──
        if button_id == "cat_view_products":
            return self._view_products_list(phone_number)

        if button_id == "cat_view_materials":
            return self._view_materials_list(phone_number)

        if button_id == "cat_view_services":
            return self._view_services_list(phone_number)

        if button_id == "cat_view_supplies":
            return self._view_supplies_list(phone_number)

        if button_id == "cat_add_service":
            return self._start_add_service(phone_number)

        if button_id == "cat_set_price":
            return self._start_set_price(phone_number)

        if button_id == "cat_supply_template":
            return self._start_supply_template(phone_number)

        if button_id.startswith("cat_tmpl_"):
            return self._handle_template_button(phone_number, button_id)

        if button_id == "cat_edit":
            return self._start_edit_product(phone_number)

        if button_id.startswith("cat_detail_"):
            product_key = button_id[11:]
            return self._view_product_detail(phone_number, product_key)

        if button_id.startswith("cat_editpick_"):
            product_key = button_id[13:]
            return self._show_edit_options(phone_number, product_key)

        if button_id.startswith("cat_editfield_"):
            # Format: cat_editfield_{product_key}_{field}
            parts = button_id[14:].rsplit("_", 1)
            if len(parts) == 2:
                product_key, field = parts
                return self._handle_edit_field(phone_number, product_key, field)

        if button_id.startswith("cat_settype_"):
            # Format: cat_settype_{product_key}_{type}
            rest = button_id[12:]
            # Type could be "finished_product" or "raw_material" (contains underscore)
            if "_finished_product" in rest:
                product_key = rest.replace("_finished_product", "")
                new_type = "finished_product"
            elif "_raw_material" in rest:
                product_key = rest.replace("_raw_material", "")
                new_type = "raw_material"
            else:
                return [text_response("❓ Unknown type.")]

            products = self._get_products(phone_number)
            if product_key in products:
                products[product_key]["item_type"] = new_type
                self._save_products(phone_number, products)
                name = products[product_key].get("name", product_key)
                type_label = "🏭 Finished Product" if new_type == "finished_product" else "🧱 Raw Material"
                return [
                    text_response(f"✅ *{name}* is now: {type_label}"),
                    button_response("What's next?", [
                        {"id": f"cat_editpick_{product_key}", "title": "✏️ Edit More"},
                        {"id": "menu_catalog", "title": "← Catalog"},
                    ])
                ]
            return [text_response("❓ Product not found.")]

        if button_id.startswith("cat_removepick_"):
            product_key = button_id[15:]
            products = self._get_products(phone_number)
            if product_key in products:
                name = products[product_key].get("name", product_key)
                stock = int(products[product_key].get("stock", 0))
                self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                    "cat_step": "confirm_remove",
                    "cat_product_key": product_key,
                })
                return [button_response(
                    f"⚠️ Delete *{name}*?\n\nStock: {stock} units\n_This cannot be undone._",
                    [
                        {"id": "cat_confirm_remove", "title": "🗑️ Yes, Delete"},
                        {"id": f"cat_editpick_{product_key}", "title": "← Go Back"},
                    ]
                )]
            return [text_response("❓ Product not found.")]

        # Product-specific buttons (from product picker flows)
        if button_id.startswith("cat_pick_"):
            product_key = button_id[9:]
            session = self.session.get(phone_number)
            context = session.get("context", {})
            action = context.get("cat_action", "")
            return self._handle_product_picked(phone_number, product_key, action, context)

        if button_id == "cat_confirm_remove":
            session = self.session.get(phone_number)
            context = session.get("context", {})
            return self._execute_remove(phone_number, context)

        if button_id == "cat_confirm_clear":
            return self._execute_clear_catalog(phone_number)

        if button_id == "cat_cancel":
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # STATE HANDLER — text input during catalog flows
    # ─────────────────────────────────────────────────────────

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during catalog flows."""
        context = session.get("context", {})
        step    = context.get("cat_step", "")
        text_s  = text.strip()
        text_low = text_s.lower()

        # Command detection
        if text_low in ("cancel", "exit", "stop"):
            self.session.reset(phone_number)
            return [
                text_response("❌ Cancelled."),
                button_response("What's next?", [
                    {"id": "cat_stock", "title": "📊 View Stock"},
                    {"id": "cat_add", "title": "➕ Add Product"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]
        if text_low in ("done", "back"):
            self.session.reset(phone_number)
            return [
                text_response("✅ Done!"),
                button_response("What's next?", [
                    {"id": "cat_stock", "title": "📊 View Stock"},
                    {"id": "cat_add", "title": "➕ Add Product"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]
        if text_low in ("menu", "hi", "hello", "help"):
            self.session.reset(phone_number)
            return [{"type": "__SHOW_HOME_MENU__", "industry": "trading"}]
        if text_low in ("my catalog", "catalog", "inventory", "stock"):
            self.session.reset(phone_number)
            return self.show_menu(phone_number)

        # Route by step
        if step == "adding_products":
            return self._handle_add_products(phone_number, text_s, context)

        if step == "setting_cost":
            return self._handle_set_cost(phone_number, text_s, context)

        if step == "adjusting_stock":
            return self._handle_adjust_stock(phone_number, text_s, context)

        if step == "adding_variants":
            return self._handle_add_variants(phone_number, text_s, context)

        if step == "setting_variant_cost":
            return self._handle_variant_cost(phone_number, text_s, context)

        if step == "setting_conversion":
            return self._handle_set_conversion(phone_number, text_s, context)

        if step == "typing_product_name":
            # User typed a product name manually — find it and route to action
            action = context.get("cat_action", "")
            products = self._get_products(phone_number)
            # Try to find matching product
            matched_key = self._find_product_key(products, text_s)
            if matched_key:
                return self._handle_product_picked(phone_number, matched_key, action, context)
            else:
                return [text_response(
                    f"❓ *{text_s}* not found in your catalog.\n\n"
                    f"_Check the spelling or add it first._"
                )]

        if step == "setting_unit":
            return self._handle_set_unit(phone_number, text_s, context)

        if step in ("template_add_supply", "template_supply_qty"):
            return self._handle_supply_template(phone_number, text_s, context)

        # Edit product steps
        if step in ("editing_name", "editing_stock", "editing_cost"):
            return self._handle_edit_input(phone_number, text_s, context)

        # Add service steps (services industry)
        if step in ("adding_service_name", "adding_service_price"):
            return self._handle_add_service(phone_number, text_s, context)

        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # VIEW STOCK LEVELS
    # ─────────────────────────────────────────────────────────

    def _show_stock_levels(self, phone_number: str) -> list:
        """Show all products with stock, cost, and color indicators. Grouped by type for manufacturing."""
        products = self.ensure_item_types(phone_number)

        if not products:
            return [text_response(
                "📊 *Stock Levels*\n\n"
                "No products yet.\n\n"
                "Tap ➕ *Add Product* to get started."
            )]

        # Check if manufacturing — group by type
        user = self.db.get_user(phone_number) or {}
        industry = user.get("industry_class", user.get("business_type", "trading"))
        is_manufacturing = industry in ("manufacturing", "hybrid")

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "📊  *Stock Levels*",
            "━━━━━━━━━━━━━━━━━━━━",
        ]

        total_stock = 0
        total_value = 0

        if is_manufacturing:
            # Group products by type
            finished = {k: v for k, v in products.items() if v.get("item_type") == "finished_product"}
            raw_mats = {k: v for k, v in products.items() if v.get("item_type") == "raw_material"}
            overhead = {k: v for k, v in products.items() if v.get("item_type") == "overhead"}
            other = {k: v for k, v in products.items() if v.get("item_type") not in ("finished_product", "raw_material", "overhead")}

            if finished:
                lines.append("")
                lines.append("🏭 *FINISHED PRODUCTS:*")
                for key, prod in sorted(finished.items(), key=lambda x: x[1].get("name", "")):
                    s, v = self._format_stock_line(prod, lines)
                    total_stock += s
                    total_value += v

            if raw_mats:
                lines.append("")
                lines.append("🧱 *RAW MATERIALS:*")
                for key, prod in sorted(raw_mats.items(), key=lambda x: x[1].get("name", "")):
                    s, v = self._format_stock_line(prod, lines)
                    total_stock += s
                    total_value += v

            if overhead:
                lines.append("")
                lines.append("⚡ *OVERHEAD RATES:*")
                for key, prod in sorted(overhead.items(), key=lambda x: x[1].get("name", "")):
                    name = prod.get("name", key)
                    cost = float(prod.get("landing_cost", 0))
                    punit = prod.get("primary_unit", "unit")
                    if cost < 1 and cost > 0:
                        lines.append(f"⚡ {name}\n  Rate: ₦{cost:.4f}/{punit}")
                    elif cost > 0:
                        lines.append(f"⚡ {name}\n  Rate: ₦{cost:,.2f}/{punit}")
                    else:
                        lines.append(f"⚡ {name}\n  Rate: _not set_")

            if other:
                lines.append("")
                lines.append("📦 *OTHER:*")
                for key, prod in sorted(other.items(), key=lambda x: x[1].get("name", "")):
                    s, v = self._format_stock_line(prod, lines)
                    total_stock += s
                    total_value += v
        else:
            # Non-manufacturing: flat list (original behavior)
            lines.append("")
            for key, prod in sorted(products.items(), key=lambda x: x[1].get("name", "")):
                s, v = self._format_stock_line(prod, lines)
                total_stock += s
                total_value += v

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📦 Total: {total_stock} units")
        if total_value > 0:
            lines.append(f"💰 Stock value: {format_amount(total_value)}")

        return [
            text_response("\n".join(lines)),
            button_response("Actions:", [
                {"id": "cat_adjust", "title": "📐 Adjust Stock"},
                {"id": "cat_edit", "title": "✏️ Edit"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    def _format_stock_line(self, prod: dict, lines: list) -> tuple:
        """Format a single product's stock line. Returns (stock, value) for totals."""
        name = prod.get("name", "?")
        stock = int(prod.get("stock", 0))
        cost = int(prod.get("landing_cost", 0))
        primary_unit = prod.get("primary_unit", "")
        variant_stock = prod.get("variant_stock", {})
        variant_costs = prod.get("variant_costs", {})

        value = 0

        # Stock indicator
        if stock <= 0:
            indicator = "🔴"
        elif stock <= 3:
            indicator = "🟡"
        else:
            indicator = "🟢"

        lines.append(f"{indicator} *{name}*")

        unit_str = f" {primary_unit}" if primary_unit else ""
        # Only show variant_stock if it has real variants (not old tree/subcategory data)
        # Real variants are short strings, not product keys with underscores
        clean_variants = {k: v for k, v in variant_stock.items()
                         if isinstance(v, (int, float)) and len(k) < 30}
        if clean_variants and len(clean_variants) <= 10:
            lines.append(f"   Stock: *{stock}* total")
            for v_name, v_stock in clean_variants.items():
                v_cost = int(variant_costs.get(v_name, 0))
                v_stock_int = int(v_stock)
                value += v_stock_int * v_cost
                v_ind = "🔴" if v_stock_int <= 0 else ("🟡" if v_stock_int <= 2 else "•")
                cost_str = f" · {format_amount(v_cost)}" if v_cost else ""
                lines.append(f"   {v_ind} {v_name}: {v_stock_int}{cost_str}")
        else:
            value = stock * cost
            cost_label = f"/{primary_unit}" if primary_unit else "/unit"
            lines.append(f"   Stock: *{stock}{unit_str}*" + (f" · {format_amount(cost)}{cost_label}" if cost else ""))

        return stock, value

    # ─────────────────────────────────────────────────────────
    # VIEW PRODUCTS / MATERIALS — Separated lists
    # ─────────────────────────────────────────────────────────

    def _view_products_list(self, phone_number: str) -> list:
        """View finished products only (manufacturing)."""
        products = self.ensure_item_types(phone_number)
        finished = {k: v for k, v in products.items() if v.get("item_type") == "finished_product"}

        if not finished:
            return [text_response(
                "🏭 *Finished Products*\n\n"
                "No finished products yet.\n\n"
                "_Add products and set recipes to see them here._"
            )]

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "🏭  *Finished Products*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for key, prod in sorted(finished.items(), key=lambda x: x[1].get("name", "")):
            name = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            cost = int(prod.get("landing_cost", 0))
            recipe = prod.get("recipe", [])

            indicator = "🔴" if stock <= 0 else ("🟡" if stock <= 3 else "🟢")
            lines.append(f"{indicator} *{name}*")
            lines.append(f"   Stock: *{stock}*" + (f" · Cost: {format_amount(cost)}/unit" if cost else ""))
            if recipe:
                lines.append(f"   📋 Recipe: {len(recipe)} materials")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📦 {len(finished)} product{'s' if len(finished) != 1 else ''}")

        # Build tappable list of products for detail view
        rows = []
        for key, prod in list(finished.items())[:10]:
            name = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            rows.append({
                "id": f"cat_detail_{key}",
                "title": f"📦 {name}"[:24],
                "description": f"Stock: {stock} · Tap for full details"[:72],
            })

        if rows:
            return [
                text_response("\n".join(lines)),
                list_response(
                    header="🔍 View Details",
                    body="Tap a product for full info:",
                    button_text="Select",
                    sections=[{"title": "Products", "rows": rows}]
                )
            ]
        return [text_response("\n".join(lines))]

    def _view_materials_list(self, phone_number: str) -> list:
        """View raw materials only (manufacturing)."""
        products = self.ensure_item_types(phone_number)
        materials = {k: v for k, v in products.items() if v.get("item_type") == "raw_material"}

        if not materials:
            return [text_response(
                "🧱 *Raw Materials*\n\n"
                "No raw materials yet.\n\n"
                "_Buy raw materials or set recipes to see them here._"
            )]

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "🧱  *Raw Materials*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for key, prod in sorted(materials.items(), key=lambda x: x[1].get("name", "")):
            name = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            cost = int(prod.get("landing_cost", 0))
            primary_unit = prod.get("primary_unit", "")

            indicator = "🔴" if stock <= 0 else ("🟡" if stock <= 5 else "🟢")
            unit_str = f" {primary_unit}" if primary_unit else ""
            lines.append(f"{indicator} *{name}*")
            lines.append(f"   Stock: *{stock}{unit_str}*" + (f" · {format_amount(cost)}/{primary_unit or 'unit'}" if cost else ""))
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🧱 {len(materials)} material{'s' if len(materials) != 1 else ''}")

        # Build tappable list for detail view
        rows = []
        for key, prod in list(materials.items())[:10]:
            name = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            rows.append({
                "id": f"cat_detail_{key}",
                "title": f"🧱 {name}"[:24],
                "description": f"Stock: {stock} · Tap for full details"[:72],
            })

        if rows:
            return [
                text_response("\n".join(lines)),
                list_response(
                    header="🔍 View Details",
                    body="Tap a material for full info:",
                    button_text="Select",
                    sections=[{"title": "Materials", "rows": rows}]
                )
            ]
        return [text_response("\n".join(lines))]

    # ─────────────────────────────────────────────────────────
    # VIEW SERVICES / SUPPLIES — Services industry
    # ─────────────────────────────────────────────────────────

    def _view_services_list(self, phone_number: str) -> list:
        """View services offered with pricing (services industry)."""
        products = self._get_products(phone_number)
        # Services = items that are type "service" or untyped (from onboarding)
        services = {k: v for k, v in products.items()
                    if v.get("item_type") in ("service", "product", "") or not v.get("item_type")}

        if not services:
            return [text_response(
                "💼 *My Services*\n\n"
                "No services added yet.\n\n"
                "_Tap ➕ Add Service to list what you offer._"
            )]

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "💼  *My Services*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        total_revenue_potential = 0
        for key, prod in sorted(services.items(), key=lambda x: x[1].get("name", "")):
            name = prod.get("name", key)
            price = int(prod.get("landing_cost", 0))  # For services, landing_cost = standard price
            total_revenue_potential += price

            price_str = format_amount(price) if price else "_no price set_"
            lines.append(f"💼 *{name}*")
            lines.append(f"   💰 {price_str}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"💼 {len(services)} service{'s' if len(services) != 1 else ''}")

        # Tappable list for detail/edit
        rows = []
        for key, prod in list(services.items())[:10]:
            name = prod.get("name", key)
            price = int(prod.get("landing_cost", 0))
            price_str = format_amount(price) if price else "No price set"
            rows.append({
                "id": f"cat_detail_{key}",
                "title": f"💼 {name}"[:24],
                "description": price_str[:72],
            })

        if rows:
            return [
                text_response("\n".join(lines)),
                list_response(
                    header="🔍 Edit Service",
                    body="Tap a service to edit or set price:",
                    button_text="Select",
                    sections=[{"title": "Services", "rows": rows}]
                )
            ]
        return [text_response("\n".join(lines))]

    def _view_supplies_list(self, phone_number: str) -> list:
        """View supplies/consumables (services industry)."""
        products = self._get_products(phone_number)
        supplies = {k: v for k, v in products.items()
                    if v.get("item_type") in ("raw_material", "consumable", "supply")}

        if not supplies:
            return [text_response(
                "📦 *Supplies*\n\n"
                "No supplies tracked yet.\n\n"
                "_When you buy supplies (chemicals, tools, consumables),_\n"
                "_they'll appear here automatically._"
            )]

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "📦  *Supplies & Consumables*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for key, prod in sorted(supplies.items(), key=lambda x: x[1].get("name", "")):
            name = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            cost = int(prod.get("landing_cost", 0))

            indicator = "🔴" if stock <= 0 else ("🟡" if stock <= 5 else "🟢")
            lines.append(f"{indicator} *{name}*")
            lines.append(f"   Stock: *{stock}*" + (f" · {format_amount(cost)}/unit" if cost else ""))
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📦 {len(supplies)} supply item{'s' if len(supplies) != 1 else ''}")

        return [
            text_response("\n".join(lines)),
            button_response("Actions:", [
                {"id": "cat_add", "title": "📦 Add Supply"},
                {"id": "cat_adjust", "title": "📐 Adjust Stock"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    def _start_add_service(self, phone_number: str) -> list:
        """Add a new service with pricing."""
        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "cat_step": "adding_service_name",
        })
        return [text_response(
            "➕ *Add Service*\n\n"
            "What service do you offer?\n\n"
            "_e.g. Hair Braiding, Office Cleaning, Car Repair, Makeup_\n\n"
            "_Type *back* to cancel_"
        )]

    def _start_set_price(self, phone_number: str) -> list:
        """Pick a service to set/update price."""
        products = self._get_products(phone_number)
        services = {k: v for k, v in products.items()
                    if v.get("item_type") in ("service", "product", "") or not v.get("item_type")}

        if not services:
            return [text_response("No services to price. Add services first.")]

        rows = []
        for key, prod in list(services.items())[:10]:
            name = prod.get("name", key)
            price = int(prod.get("landing_cost", 0))
            desc = format_amount(price) if price else "No price set"
            rows.append({
                "id": f"cat_pick_{key}",
                "title": f"💼 {name}"[:24],
                "description": desc[:72],
            })

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "cat_step": "picking_product",
            "cat_action": "set_service_price",
        })

        return [list_response(
            header="💰 Set Price",
            body="Which service do you want to price?",
            button_text="Select",
            sections=[{"title": "Services", "rows": rows}]
        )]

    def _handle_add_service(self, phone_number: str, text: str, context: dict) -> list:
        """Handle service name input, then ask for price."""
        step = context.get("cat_step", "")

        if step == "adding_service_name":
            service_name = text.strip().title()
            if len(service_name) < 2:
                return [text_response("Please enter the service name (at least 2 characters):")]

            context["cat_step"] = "adding_service_price"
            context["service_name"] = service_name
            self.session.save(phone_number, states.CATALOG_ADD_DATA, context)

            return [text_response(
                f"💼 *{service_name}*\n\n"
                f"💰 How much do you charge for this service?\n\n"
                f"_e.g. 15000, 25K, 50K_\n\n"
                f"_Type *skip* if price varies per client_"
            )]

        if step == "adding_service_price":
            service_name = context.get("service_name", "Service")
            price = 0

            if text.lower() not in ("skip", "varies", "0"):
                price = parse_amount(text)
                if not price:
                    return [text_response("💰 Enter a price (e.g. 15000, 25K) or type *skip*:")]
                price = int(price)

            # Save to catalog
            products = self._get_products(phone_number)
            key = service_name.lower().replace(" ", "_")
            products[key] = {
                "name": service_name,
                "stock": 0,
                "landing_cost": price,  # For services, this = standard price
                "item_type": "service",
                "category": "",
                "variants": [],
                "recipe": [],
                "conversions": {},
            }
            self._save_products(phone_number, products)
            self.session.reset(phone_number)

            price_str = format_amount(price) if price else "Price varies"
            return [
                text_response(f"✅ Service added: *{service_name}* — {price_str}"),
                button_response("What's next?", [
                    {"id": "cat_add_service", "title": "➕ Add Another"},
                    {"id": "rec_add", "title": "🔁 Add Recurring"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]

        return self.show_menu(phone_number)
        """Show full detail for a single product/material."""
        products = self._get_products(phone_number)
        if product_key not in products:
            return [text_response("❓ Product not found.")]

        prod = products[product_key]
        name = prod.get("name", product_key)
        stock = int(prod.get("stock", 0))
        cost = int(prod.get("landing_cost", 0))
        item_type = prod.get("item_type", "product")
        recipe = prod.get("recipe", [])
        conversions = prod.get("conversions", {})
        primary_unit = prod.get("primary_unit", "")
        variants = prod.get("variants", [])
        variant_costs = prod.get("variant_costs", {})

        # Type label
        type_label = {"finished_product": "🏭 Finished Product", "raw_material": "🧱 Raw Material"}.get(item_type, "📦 Product")

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            f"*{name}*",
            f"{type_label}",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        # Stock
        unit_str = f" {primary_unit}" if primary_unit else ""
        indicator = "🔴" if stock <= 0 else ("🟡" if stock <= 5 else "🟢")
        lines.append(f"{indicator} Stock: *{stock}{unit_str}*")

        # Cost
        if cost > 0:
            lines.append(f"💰 Cost: *{format_amount(cost)}* per {primary_unit or 'unit'}")
        else:
            lines.append(f"💰 Cost: _not set_")

        # Recipe (for finished products)
        if recipe:
            lines.append("")
            lines.append("📋 *Recipe (per 1 unit):*")
            all_products = self._get_products(phone_number)
            for mat in recipe:
                mat_cost = float(mat.get("cost_per_unit", 0))
                mat_qty = float(mat.get("quantity", 0))
                mat_unit = mat.get("unit", "")
                mat_name = mat["material"]
                cost_str = f" @ {format_amount(mat_cost)}" if mat_cost else ""
                # Show stock unit hint if different
                mat_key = mat_name.lower().replace(" ", "_")
                mat_prod = all_products.get(mat_key, {})
                stock_unit = mat_prod.get("primary_unit", "")
                unit_hint = ""
                if stock_unit and stock_unit.lower() != mat_unit.lower().rstrip("s"):
                    unit_hint = f" _(stock: {stock_unit})_"
                qty_display = int(mat_qty) if mat_qty == int(mat_qty) else mat_qty
                lines.append(f"  • {qty_display} {mat_unit} {mat_name}{cost_str}{unit_hint}")

        # Conversions
        if conversions:
            lines.append("")
            lines.append("📦 *Conversions:*")
            for ck, cv in conversions.items():
                lines.append(f"  • {ck} = {cv['qty']} {cv['unit']}")

        # Variants
        if variants:
            lines.append("")
            lines.append(f"🏷️ *Variants:* {', '.join(variants)}")
            if variant_costs:
                for v, c in variant_costs.items():
                    lines.append(f"  • {v}: {format_amount(c)}")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")

        return [
            text_response("\n".join(lines)),
            button_response(
                f"Actions for *{name}*:",
                [
                    {"id": f"cat_editpick_{product_key}", "title": "✏️ Edit"},
                    {"id": f"cat_removepick_{product_key}", "title": "🗑️ Delete"},
                    {"id": "menu_catalog", "title": "← Catalog"},
                ]
            )
        ]

    # ─────────────────────────────────────────────────────────
    # EDIT PRODUCT — Multi-field editor
    # ─────────────────────────────────────────────────────────
    # VIEW PRODUCT DETAIL
    # ─────────────────────────────────────────────────────────

    def _view_product_detail(self, phone_number: str, product_key: str) -> list:
        """Show full detail for a single product/material."""
        products = self._get_products(phone_number)
        if product_key not in products:
            return [text_response("❓ Product not found.")]

        prod = products[product_key]
        name = prod.get("name", product_key)
        stock = int(prod.get("stock", 0))
        cost = float(prod.get("landing_cost", 0))
        item_type = prod.get("item_type", "product")
        primary_unit = prod.get("primary_unit", "")
        variants = prod.get("variants", [])
        variant_stock = prod.get("variant_stock", {})
        variant_costs = prod.get("variant_costs", {})
        recipe = prod.get("recipe", [])
        conversions = prod.get("conversions", {})
        cost_history = prod.get("cost_history", [])

        # Type label
        type_labels = {
            "finished_product": "🏭 Finished Product",
            "raw_material": "🧱 Raw Material",
            "overhead": "⚡ Overhead Rate",
            "consumable": "📦 Consumable",
            "service": "💼 Service",
            "product": "📦 Product",
        }
        type_label = type_labels.get(item_type, "📦 Product")

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📦  *{name}*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"📊 Type: {type_label}",
        ]

        if item_type != "overhead":
            unit_str = f" {primary_unit}" if primary_unit else ""
            lines.append(f"📐 Stock: *{stock}{unit_str}*")

        if cost > 0:
            if item_type == "overhead":
                unit_label = primary_unit or "unit"
                lines.append(f"💰 Rate: *{format_amount(cost)}/{unit_label}*")
            else:
                unit_label = primary_unit or "unit"
                lines.append(f"💰 Cost: *{format_amount(cost)}/{unit_label}*")

        if primary_unit:
            lines.append(f"📏 Unit: {primary_unit}")

        # Variants
        if variants:
            lines.append(f"")
            lines.append(f"🏷️ *Variants:* {', '.join(variants[:8])}")
            if variant_stock:
                for v, vs in list(variant_stock.items())[:5]:
                    vc = variant_costs.get(v, 0)
                    cost_str = f" · {format_amount(vc)}" if vc else ""
                    lines.append(f"  • {v}: {vs} in stock{cost_str}")

        # Recipe
        if recipe:
            lines.append(f"")
            lines.append(f"📋 *Recipe:* ({len(recipe)} materials)")
            for mat in recipe[:5]:
                qty = mat.get("quantity", 0)
                unit = mat.get("unit", "")
                mat_name = mat.get("material", "")
                lines.append(f"  • {qty} {unit} {mat_name}")

        # Conversions
        if conversions:
            lines.append(f"")
            lines.append(f"📦 *Conversions:*")
            for ck, cv in list(conversions.items())[:3]:
                lines.append(f"  • {ck} = {cv.get('qty', '')} {cv.get('unit', '')}")

        # Cost history (last 3)
        if cost_history:
            lines.append(f"")
            lines.append(f"📈 *Recent Costs:*")
            for ch in cost_history[-3:]:
                lines.append(f"  • {ch.get('date', '')}: {format_amount(ch.get('cost', 0))} × {ch.get('qty', '')}")

        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")

        return [
            text_response("\n".join(lines)),
            button_response("Actions:", [
                {"id": f"cat_editpick_{product_key}", "title": "✏️ Edit"},
                {"id": "menu_catalog", "title": "← Catalog"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # EDIT PRODUCT
    # ─────────────────────────────────────────────────────────

    def _start_edit_product(self, phone_number: str) -> list:
        """Pick a product to edit."""
        products = self._get_products(phone_number)
        if not products:
            return [text_response("📊 No products to edit. Add products first.")]

        rows = []
        for key, prod in list(products.items())[:10]:
            name = prod.get("name", key)
            item_type = prod.get("item_type", "product")
            icon = "🏭" if item_type == "finished_product" else ("🧱" if item_type == "raw_material" else "📦")
            stock = int(prod.get("stock", 0))
            rows.append({
                "id": f"cat_editpick_{key}",
                "title": f"{icon} {name}"[:24],
                "description": f"Stock: {stock} · Tap to edit"[:72],
            })

        return [list_response(
            header="✏️ Edit Product",
            body="Which product do you want to edit?",
            button_text="Select",
            sections=[{"title": "Products", "rows": rows}]
        )]

    def _show_edit_options(self, phone_number: str, product_key: str) -> list:
        """Show edit options for a specific product."""
        products = self._get_products(phone_number)
        if product_key not in products:
            return [text_response("❓ Product not found.")]

        prod = products[product_key]
        name = prod.get("name", product_key)
        stock = int(prod.get("stock", 0))
        cost = int(prod.get("landing_cost", 0))
        item_type = prod.get("item_type", "product")

        type_label = {"finished_product": "Finished Product", "raw_material": "Raw Material"}.get(item_type, "Product")

        return [list_response(
            header=f"✏️ Edit: {name}",
            body=f"Stock: {stock} · Cost: {format_amount(cost)}\nType: {type_label}\n\nWhat do you want to change?",
            button_text="Select",
            sections=[{
                "title": "Edit Options",
                "rows": [
                    {"id": f"cat_editfield_{product_key}_name", "title": "📝 Rename Product",
                     "description": f"Current: {name}"},
                    {"id": f"cat_editfield_{product_key}_stock", "title": "📐 Set Stock",
                     "description": f"Current: {stock}"},
                    {"id": f"cat_editfield_{product_key}_cost", "title": "💰 Set Cost/Unit",
                     "description": f"Current: {format_amount(cost)}" if cost else "Not set"},
                    {"id": f"cat_editfield_{product_key}_type", "title": "🏷️ Change Type",
                     "description": f"Current: {type_label}"},
                    {"id": f"cat_removepick_{product_key}", "title": "🗑️ Delete Product",
                     "description": "Remove from inventory"},
                ]
            }]
        )]

    def _handle_edit_field(self, phone_number: str, product_key: str, field: str) -> list:
        """Start editing a specific field."""
        products = self._get_products(phone_number)
        if product_key not in products:
            return [text_response("❓ Product not found.")]

        prod = products[product_key]
        name = prod.get("name", product_key)

        if field == "name":
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "editing_name",
                "cat_product_key": product_key,
            })
            return [text_response(
                f"📝 *Rename: {name}*\n\n"
                f"Type the new name:\n\n"
                f"_Type *back* to cancel_"
            )]

        if field == "stock":
            current = int(prod.get("stock", 0))
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "editing_stock",
                "cat_product_key": product_key,
            })
            return [text_response(
                f"📐 *Set Stock: {name}*\n\n"
                f"Current stock: *{current}*\n\n"
                f"Enter new stock level:\n"
                f"• _+10_ (add 10)\n"
                f"• _-5_ (remove 5)\n"
                f"• _50_ (set to 50)\n\n"
                f"_Type *back* to cancel_"
            )]

        if field == "cost":
            current = int(prod.get("landing_cost", 0))
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "editing_cost",
                "cat_product_key": product_key,
            })
            return [text_response(
                f"💰 *Set Cost: {name}*\n\n"
                f"Current cost: *{format_amount(current)}* per unit\n\n"
                f"Enter new cost per unit:\n"
                f"_e.g. 5000, 25K, 1.2M_\n\n"
                f"_Type *back* to cancel_"
            )]

        if field == "type":
            current_type = prod.get("item_type", "product")
            return [button_response(
                f"🏷️ *Change Type: {name}*\n\n"
                f"Current: {current_type}\n\n"
                f"What is this item?",
                [
                    {"id": f"cat_settype_{product_key}_finished_product", "title": "🏭 Finished Product"},
                    {"id": f"cat_settype_{product_key}_raw_material", "title": "🧱 Raw Material"},
                ]
            )]

        return [text_response("❓ Unknown field.")]

    def _handle_edit_input(self, phone_number: str, text: str, context: dict) -> list:
        """Handle text input for product editing."""
        import re
        step = context.get("cat_step", "")
        product_key = context.get("cat_product_key", "")
        text_s = text.strip()

        if text_s.lower() in ("back", "cancel"):
            self.session.reset(phone_number)
            return self._show_edit_options(phone_number, product_key)

        products = self._get_products(phone_number)
        if product_key not in products:
            self.session.reset(phone_number)
            return [text_response("❓ Product not found.")]

        prod = products[product_key]
        name = prod.get("name", product_key)

        if step == "editing_name":
            new_name = text_s.title()
            if len(new_name) < 2:
                return [text_response("Name must be at least 2 characters:")]

            # Update name (keep same key)
            prod["name"] = new_name
            self._save_products(phone_number, products)
            self.session.reset(phone_number)
            return [
                text_response(f"✅ Renamed to *{new_name}*"),
                button_response("What's next?", [
                    {"id": f"cat_editpick_{product_key}", "title": "✏️ Edit More"},
                    {"id": "menu_catalog", "title": "← Catalog"},
                ])
            ]

        if step == "editing_stock":
            add_match = re.match(r'^\+\s*([\d.]+)', text_s)
            sub_match = re.match(r'^-\s*([\d.]+)', text_s)
            set_match = re.match(r'^([\d.]+)$', text_s)

            current = float(prod.get("stock", 0))
            if add_match:
                qty = float(add_match.group(1))
                prod["stock"] = current + qty
                qty_display = int(qty) if qty == int(qty) else qty
                action = f"+{qty_display}"
            elif sub_match:
                qty = float(sub_match.group(1))
                prod["stock"] = max(0, current - qty)
                qty_display = int(qty) if qty == int(qty) else qty
                action = f"-{qty_display}"
            elif set_match:
                qty = float(set_match.group(1))
                prod["stock"] = qty
                qty_display = int(qty) if qty == int(qty) else qty
                action = f"set to {qty_display}"
            else:
                return [text_response("Enter: _+10_, _-5_, or _50_ (set to 50):")]

            self._save_products(phone_number, products)
            new_stock = prod["stock"]
            new_display = int(new_stock) if new_stock == int(new_stock) else new_stock
            self.session.reset(phone_number)
            return [
                text_response(f"✅ *{name}* stock {action}\n📊 New stock: *{new_display}*"),
                button_response("What's next?", [
                    {"id": f"cat_editpick_{product_key}", "title": "✏️ Edit More"},
                    {"id": "menu_catalog", "title": "← Catalog"},
                ])
            ]

        if step == "editing_cost":
            amount = parse_amount(text_s)
            if not amount:
                return [text_response("Enter cost per unit (e.g. 5000, 25K, 1.2M):")]

            prod["landing_cost"] = int(amount)
            self._save_products(phone_number, products)
            self.session.reset(phone_number)
            return [
                text_response(f"✅ *{name}* cost set to *{format_amount(amount)}* per unit"),
                button_response("What's next?", [
                    {"id": f"cat_editpick_{product_key}", "title": "✏️ Edit More"},
                    {"id": "menu_catalog", "title": "← Catalog"},
                ])
            ]

        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # ADD PRODUCT
    # ─────────────────────────────────────────────────────────

    def _start_add_product(self, phone_number: str) -> list:
        """Start adding products."""
        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "cat_step": "adding_products",
        })
        return [button_response(
            "➕ *Add Products*\n\n"
            "Type product names (comma-separated for multiple):\n\n"
            "_e.g. Toyota Prado, Honda Civic, Kia Sportage_\n"
            "_e.g. Detergent 1L, Soap Bar, Hand Wash_",
            [
                {"id": "cat_cancel", "title": "✅ Done"},
            ]
        )]

    def _handle_add_products(self, phone_number: str, text: str, context: dict) -> list:
        """Add one or multiple products."""
        items = [item.strip().title() for item in text.split(",") if item.strip()]

        if not items:
            return [text_response("Please type at least one product name:")]

        products = self._get_products(phone_number)
        added = []
        already_exists = []

        for item in items:
            key = item.lower().replace(" ", "_")
            if key in products:
                already_exists.append(item)
            else:
                products[key] = {
                    "name": item,
                    "stock": 0,
                    "landing_cost": 0,
                    "category": "",
                    "variants": [],
                }
                added.append(item)

        self._save_products(phone_number, products)

        lines = []
        if added:
            lines.append(f"✅ Added *{len(added)}* product{'s' if len(added) != 1 else ''}: {', '.join(added)}")
        if already_exists:
            lines.append(f"ℹ️ Already existed: {', '.join(already_exists)}")
        lines.append("\n_Add more, or tap Done._")

        return [button_response("\n".join(lines), [
            {"id": "cat_cancel", "title": "✅ Done"},
        ])]

    # ─────────────────────────────────────────────────────────
    # SET LANDING COST
    # ─────────────────────────────────────────────────────────

    def _start_set_cost(self, phone_number: str) -> list:
        """Pick product to set cost for."""
        return self._show_product_picker(phone_number, "set_cost",
                                          "🏷️ *Set Landing Cost*\n\nPick a product:")

    def _handle_set_cost(self, phone_number: str, text: str, context: dict) -> list:
        """Handle cost amount input."""
        product_key = context.get("cat_product_key", "")
        amount = parse_amount(text)
        if not amount:
            return [text_response("💰 Enter a valid amount (e.g. 50000, 150K, 10M):")]

        products = self._get_products(phone_number)
        if product_key in products:
            products[product_key]["landing_cost"] = int(amount)
            self._save_products(phone_number, products)
            name = products[product_key].get("name", product_key)
            self.session.reset(phone_number)
            return [
                text_response(f"✅ *{name}* cost set to *{format_amount(amount)}* per unit.\n\n_This updates your production cost calculations._"),
                button_response("What's next?", [
                    {"id": "cat_stock", "title": "📊 View Stock"},
                    {"id": "cat_cost", "title": "🏷️ Set Another Cost"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]

        self.session.reset(phone_number)
        return [text_response("❓ Product not found.")]

    # ─────────────────────────────────────────────────────────
    # ADJUST STOCK
    # ─────────────────────────────────────────────────────────

    def _start_adjust_stock(self, phone_number: str) -> list:
        """Pick product to adjust stock for."""
        return self._show_product_picker(phone_number, "adjust_stock",
                                          "📐 *Adjust Stock*\n\nPick a product:")

    def _handle_adjust_stock(self, phone_number: str, text: str, context: dict) -> list:
        """Handle stock adjustment input."""
        product_key = context.get("cat_product_key", "")
        text_low = text.lower().strip()

        # Parse: "+5", "-3", "set 10", or just "10" (set to)
        add_match = re.match(r'^\+\s*(\d+)', text)
        sub_match = re.match(r'^-\s*(\d+)', text)
        set_match = re.match(r'^(?:set\s+)?(\d+)$', text_low)

        products = self._get_products(phone_number)
        if product_key not in products:
            self.session.reset(phone_number)
            return [text_response("❓ Product not found.")]

        product = products[product_key]
        name = product.get("name", product_key)
        current = int(product.get("stock", 0))

        if add_match:
            qty = int(add_match.group(1))
            product["stock"] = current + qty
            action_str = f"+{qty}"
        elif sub_match:
            qty = int(sub_match.group(1))
            product["stock"] = max(0, current - qty)
            action_str = f"-{qty}"
        elif set_match:
            qty = int(set_match.group(1))
            product["stock"] = qty
            action_str = f"set to {qty}"
        else:
            return [text_response(
                "📐 Enter stock adjustment:\n\n"
                "• _+5_ (add 5 units)\n"
                "• _-3_ (remove 3 units)\n"
                "• _10_ (set stock to 10)\n"
            )]

        self._save_products(phone_number, products)
        new_stock = int(product["stock"])
        self.session.reset(phone_number)

        return [
            text_response(
                f"✅ *{name}* stock {action_str}\n"
                f"📊 New stock: *{new_stock}* units"
            ),
            button_response("What's next?", [
                {"id": "cat_stock", "title": "📊 View Stock"},
                {"id": "cat_adjust", "title": "📐 Adjust Another"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # ADD VARIANTS
    # ─────────────────────────────────────────────────────────

    def _start_add_variants(self, phone_number: str) -> list:
        """Pick product to add variants to."""
        return self._show_product_picker(phone_number, "add_variants",
                                          "🏷️ *Add Variants*\n\nPick a product:")

    def _handle_add_variants(self, phone_number: str, text: str, context: dict) -> list:
        """Handle variant input (comma-separated)."""
        product_key = context.get("cat_product_key", "")
        products = self._get_products(phone_number)

        if product_key not in products:
            self.session.reset(phone_number)
            return [text_response("❓ Product not found.")]

        variants = [v.strip().title() for v in text.split(",") if v.strip()]
        if not variants:
            return [text_response("Type variants separated by commas (e.g. Black, White, 2019, 2024):")]

        product = products[product_key]
        name = product.get("name", product_key)
        existing = product.get("variants", [])

        # Merge (no duplicates)
        new_variants = list(dict.fromkeys(existing + variants))
        product["variants"] = new_variants
        self._save_products(phone_number, products)

        self.session.reset(phone_number)
        return [
            text_response(
                f"✅ Variants for *{name}*:\n"
                f"🏷️ {', '.join(new_variants)}"
            ),
            button_response("What's next?", [
                {"id": "cat_stock", "title": "📊 View Stock"},
                {"id": "cat_variant_cost", "title": "💲 Set Variant Cost"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # VARIANT COSTS — Different landing cost per variant
    # ─────────────────────────────────────────────────────────

    def _start_variant_cost(self, phone_number: str) -> list:
        """Pick product to set variant costs for."""
        return self._show_product_picker(phone_number, "set_variant_cost",
                                          "💲 *Set Variant Cost*\n\nPick a product:")

    def _handle_variant_cost(self, phone_number: str, text: str, context: dict) -> list:
        """Handle variant cost input: 'variant = amount' or just 'amount' after picking variant."""
        product_key = context.get("cat_product_key", "")
        variant_name = context.get("cat_variant_name", "")
        products = self._get_products(phone_number)

        if product_key not in products:
            self.session.reset(phone_number)
            return [text_response("❓ Product not found.")]

        product = products[product_key]
        name = product.get("name", product_key)

        # If we don't have a variant picked yet, this text IS the variant name
        if not variant_name:
            # Check if user typed "variant = amount" format
            match = re.match(r'^(.+?)\s*=\s*(.+)$', text)
            if match:
                variant_name = match.group(1).strip().title()
                cost_text = match.group(2).strip()
                from utils.parser import parse_amount
                cost = parse_amount(cost_text)
                if cost:
                    return self._save_variant_cost(phone_number, product_key, variant_name, int(cost))
                # Have variant but bad cost
                context["cat_variant_name"] = variant_name
                self.session.save(phone_number, states.CATALOG_ADD_DATA, context)
                return [text_response(f"💰 Enter cost for *{name}* ({variant_name}):\n\n_e.g. 50000, 150K, 19M_")]

            # Just a variant name — save it and ask for cost
            variant_name = text.strip().title()
            context["cat_variant_name"] = variant_name
            self.session.save(phone_number, states.CATALOG_ADD_DATA, context)
            return [text_response(
                f"💰 Enter landing cost for *{name}* ({variant_name}):\n\n"
                f"_e.g. 50000, 150K, 19M_"
            )]

        # We have a variant — this text should be the cost
        from utils.parser import parse_amount
        cost = parse_amount(text)
        if not cost:
            return [text_response(
                f"💰 Enter cost for *{name}* ({variant_name}):\n\n"
                f"_e.g. 50000, 150K, 19M_\n\n"
                f"Or type *done* to finish."
            )]

        return self._save_variant_cost(phone_number, product_key, variant_name, int(cost))

    def _save_variant_cost(self, phone_number: str, product_key: str, variant_name: str, cost: int) -> list:
        """Save a variant-specific cost to the product."""
        products = self._get_products(phone_number)
        if product_key not in products:
            self.session.reset(phone_number)
            return [text_response("❓ Product not found.")]

        product = products[product_key]
        name = product.get("name", product_key)

        # Initialize variant_costs dict if needed
        variant_costs = product.setdefault("variant_costs", {})
        variant_costs[variant_name] = cost

        # Also add to variants list if not there
        variants = product.setdefault("variants", [])
        if variant_name not in variants:
            variants.append(variant_name)

        self._save_products(phone_number, products)

        # Show all variant costs
        from utils.whatsapp_ui import format_amount
        cost_lines = []
        for v, c in variant_costs.items():
            cost_lines.append(f"  • {v}: {format_amount(c)}")
        cost_display = "\n".join(cost_lines)

        # Ask if they want to add more
        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "cat_step": "setting_variant_cost",
            "cat_product_key": product_key,
            "cat_variant_name": "",  # Reset for next variant
        })

        return [text_response(
            f"✅ *{name}* — Variant Costs:\n\n"
            f"{cost_display}\n\n"
            f"Type another variant name to set its cost,\n"
            f"or type *done* to finish."
        )]

    # ─────────────────────────────────────────────────────────
    # SET CONVERSION
    # ─────────────────────────────────────────────────────────

    def _start_set_conversion(self, phone_number: str) -> list:
        """Pick product to set conversion for."""
        return self._show_product_picker(phone_number, "set_conversion",
                                          "📦 *Set Conversion*\n\nPick a product:")

    def _handle_set_conversion(self, phone_number: str, text: str, context: dict) -> list:
        """Handle conversion input like '1 carton = 24 pieces'."""
        product_key = context.get("cat_product_key", "")

        match = re.match(r'(\d+)\s*(.+?)\s*=\s*(\d+)\s*(.*)', text)
        if not match:
            return [text_response(
                "📦 Enter conversion format:\n\n"
                "_1 carton = 24 pieces_\n"
                "_1 dozen = 12 pieces_\n"
                "_1 crate = 20 bottles_\n\n"
                "Or type *done* to finish."
            )]

        qty_from = int(match.group(1))
        unit_from = match.group(2).strip().lower()
        qty_to = int(match.group(3))
        unit_to = match.group(4).strip().lower() or "pieces"

        products = self._get_products(phone_number)
        if product_key in products:
            product = products[product_key]
            conversions = product.setdefault("conversions", {})
            conv_key = f"{qty_from} {unit_from}"
            conversions[conv_key] = {"qty": qty_to, "unit": unit_to}
            # Also set primary unit
            product["primary_unit"] = unit_to
            self._save_products(phone_number, products)

            name = product.get("name", product_key)
            self.session.reset(phone_number)
            return [
                text_response(
                    f"✅ Conversion saved for *{name}*:\n\n"
                    f"📦 {qty_from} {unit_from} = {qty_to} {unit_to}\n\n"
                    f"_Now when you record '{qty_from} {unit_from} of {name}', "
                    f"stock will update by {qty_to} {unit_to}._"
                ),
                button_response("What's next?", [
                    {"id": "cat_conversion", "title": "📦 Another Conversion"},
                    {"id": "cat_stock", "title": "📊 View Stock"},
                    {"id": "record_purchase", "title": "📦 Record Purchase"},
                ])
            ]

        self.session.reset(phone_number)
        return [text_response("❓ Product not found.")]

    # ─────────────────────────────────────────────────────────
    # SERVICE SUPPLY TEMPLATES
    # ─────────────────────────────────────────────────────────

    def _start_supply_template(self, phone_number: str) -> list:
        """Pick a service to set its supply template (what supplies it uses per job)."""
        products = self._get_products(phone_number)
        services = {k: v for k, v in products.items()
                    if v.get("item_type") in ("service", "") or not v.get("item_type")}

        if not services:
            return [text_response(
                "📋 *Set Supply Template*\n\n"
                "No services found. Add services first via ➕ *Add Service*."
            )]

        rows = []
        for key, prod in list(services.items())[:9]:
            name = prod.get("name", key)
            template = prod.get("supplies_used", [])
            desc = f"📋 {len(template)} supplies defined" if template else "No template yet"
            rows.append({
                "id": f"cat_tmpl_{key}",
                "title": f"💼 {name}"[:24],
                "description": desc[:72],
            })

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "cat_step": "template_pick_service",
        })

        return [list_response(
            header="📋 Supply Template",
            body="Which service uses supplies?\n\n_A template defines what to auto-deduct per job._",
            button_text="Select Service",
            sections=[{"title": "Your Services", "rows": rows}]
        )]

    def _handle_template_button(self, phone_number: str, button_id: str) -> list:
        """Handle cat_tmpl_* buttons — service picked for template setup."""
        service_key = button_id[9:]  # after "cat_tmpl_"

        products = self._get_products(phone_number)
        if service_key not in products:
            return [text_response("❓ Service not found.")]

        service = products[service_key]
        service_name = service.get("name", service_key)
        template = service.get("supplies_used", [])

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "cat_step": "template_add_supply",
            "cat_template_service_key": service_key,
            "cat_template_service_name": service_name,
        })

        if template:
            lines = [f"📋 *Supply template for {service_name}:*\n"]
            for i, supply in enumerate(template):
                lines.append(f"  {i+1}. {supply['quantity']} {supply.get('unit', '')} {supply['supply']}")
            lines.append(f"\n_Add more or type *done* to finish._")
            lines.append(f"_Type *clear* to remove all._")
            return [text_response("\n".join(lines))]
        else:
            return [text_response(
                f"📋 *Set supply template for: {service_name}*\n\n"
                f"What supply is used per job?\n\n"
                f"Type the *supply name*:\n\n"
                f"_e.g. Blade, Chemical, Nylon, Thread_\n\n"
                f"_Type *done* when finished._"
            )]

    def _handle_supply_template(self, phone_number: str, text: str, context: dict) -> list:
        """Handle supply template input — add supplies step by step."""
        step = context.get("cat_step", "")
        service_key = context.get("cat_template_service_key", "")
        service_name = context.get("cat_template_service_name", "Service")

        if text.lower() == "done":
            self.session.reset(phone_number)
            products = self._get_products(phone_number)
            service = products.get(service_key, {})
            template = service.get("supplies_used", [])
            if template:
                return [text_response(
                    f"✅ *Supply template saved for {service_name}!*\n\n"
                    f"📋 {len(template)} supplies will be auto-deducted per job.\n\n"
                    f"_When you complete this service, supplies will be deducted automatically._"
                )]
            return [text_response("✅ Done. No supplies set.")]

        if text.lower() == "clear":
            products = self._get_products(phone_number)
            if service_key in products:
                products[service_key]["supplies_used"] = []
                self._save_products(phone_number, products)
            self.session.reset(phone_number)
            return [text_response(f"🗑️ Supply template cleared for *{service_name}*.")]

        if step == "template_add_supply":
            # User typed a supply name — ask for quantity
            supply_name = text.strip().title()
            if len(supply_name) < 2:
                return [text_response("Please type the supply name (at least 2 characters):")]

            context["cat_step"] = "template_supply_qty"
            context["cat_template_current_supply"] = supply_name
            self.session.save(phone_number, states.CATALOG_ADD_DATA, context)

            return [text_response(
                f"🧱 *{supply_name}*\n\n"
                f"How much *{supply_name}* is used per job?\n\n"
                f"Type: *quantity* and *unit*\n\n"
                f"_e.g. 2 pieces, 1 litre, 500 ml, 3 packs_\n\n"
                f"_Or just a number (e.g. 2) if no unit needed_"
            )]

        if step == "template_supply_qty":
            # User typed quantity — save and ask for next
            supply_name = context.get("cat_template_current_supply", "Supply")

            # Parse quantity and unit
            match = re.match(r'^([\d.]+)\s*(.*)', text.strip())
            if not match:
                return [text_response(f"Enter quantity for {supply_name} (e.g. 2, 500 ml, 1 litre):")]

            qty = float(match.group(1))
            unit = match.group(2).strip() or "pieces"

            # Save to catalog
            products = self._get_products(phone_number)
            if service_key in products:
                service = products[service_key]
                template = service.setdefault("supplies_used", [])

                # Check if supply already exists — update it
                found = False
                for existing in template:
                    if existing["supply"].lower() == supply_name.lower():
                        existing["quantity"] = qty
                        existing["unit"] = unit
                        found = True
                        break
                if not found:
                    template.append({
                        "supply": supply_name,
                        "quantity": qty,
                        "unit": unit,
                    })

                # Auto-create supply in catalog if it doesn't exist
                supply_key = supply_name.lower().replace(" ", "_")
                if supply_key not in products:
                    products[supply_key] = {
                        "name": supply_name,
                        "stock": 0,
                        "landing_cost": 0,
                        "item_type": "consumable",
                        "category": "",
                        "variants": [],
                        "recipe": [],
                        "conversions": {},
                    }

                self._save_products(phone_number, products)

            # Ask for next supply
            context["cat_step"] = "template_add_supply"
            context.pop("cat_template_current_supply", None)
            self.session.save(phone_number, states.CATALOG_ADD_DATA, context)

            qty_display = int(qty) if qty == int(qty) else qty
            return [text_response(
                f"✅ Added: *{qty_display} {unit} {supply_name}* per job\n\n"
                f"Add another supply or type *done* to finish."
            )]

        return [text_response("Type a supply name or *done* to finish.")]

    def deduct_service_supplies(self, phone_number: str, service_key: str, qty_jobs: int = 1) -> list:
        """
        Auto-deduct supplies based on service template after completing a job.
        Called by transactions after a service sale is recorded.
        
        Returns list of deduction result strings for display.
        """
        products = self._get_products(phone_number)
        if service_key not in products:
            return []

        service = products[service_key]
        template = service.get("supplies_used", [])
        if not template:
            return []

        deductions = []
        low_warnings = []

        for supply in template:
            supply_name = supply.get("supply", "")
            supply_qty = float(supply.get("quantity", 0)) * qty_jobs
            supply_unit = supply.get("unit", "")
            supply_key_cat = supply_name.lower().replace(" ", "_")

            if supply_key_cat in products:
                current_stock = float(products[supply_key_cat].get("stock", 0))
                new_stock = max(0, current_stock - supply_qty)
                products[supply_key_cat]["stock"] = new_stock

                qty_display = int(supply_qty) if supply_qty == int(supply_qty) else f"{supply_qty:.1f}"
                deductions.append(f"  • -{qty_display} {supply_unit} {supply_name}")

                if new_stock <= 3:
                    low_warnings.append(f"  ⚠️ *{supply_name}*: only {int(new_stock)} left!")

        if deductions:
            self._save_products(phone_number, products)

        return deductions + low_warnings

    # ─────────────────────────────────────────────────────────
    # SET STOCK UNIT
    # ─────────────────────────────────────────────────────────

    def _start_set_unit(self, phone_number: str) -> list:
        """Pick product to set stock unit for."""
        return self._show_product_picker(phone_number, "set_unit",
                                          "📏 *Set Stock Unit*\n\nPick a product:")

    def _handle_set_unit(self, phone_number: str, text: str, context: dict) -> list:
        """Handle unit input — a single word like 'kg', 'litres', 'pieces'."""
        product_key = context.get("cat_product_key", "")
        unit = text.strip().lower()

        if not unit or len(unit) > 20:
            return [text_response(
                "📏 Enter the unit (one word):\n\n"
                "_e.g. kg, litres, pieces, meters, bags, bottles, hours_"
            )]

        products = self._get_products(phone_number)
        if product_key in products:
            product = products[product_key]
            old_unit = product.get("primary_unit", "")
            product["primary_unit"] = unit
            self._save_products(phone_number, products)

            name = product.get("name", product_key)
            self.session.reset(phone_number)

            change_note = f"\n_Changed from: {old_unit}_" if old_unit and old_unit != unit else ""
            return [
                text_response(
                    f"✅ Stock unit set for *{name}*:\n\n"
                    f"📏 Unit: *{unit}*{change_note}\n\n"
                    f"_All stock, purchases, and production for {name} will be tracked in {unit}._"
                ),
                button_response("What's next?", [
                    {"id": "cat_unit", "title": "📏 Set Another"},
                    {"id": "cat_stock", "title": "📊 View Stock"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]

        self.session.reset(phone_number)
        return [text_response("❓ Product not found.")]

    # ─────────────────────────────────────────────────────────
    # REMOVE PRODUCT
    # ─────────────────────────────────────────────────────────

    def _start_remove_product(self, phone_number: str) -> list:
        """Pick product to remove."""
        return self._show_product_picker(phone_number, "remove_product",
                                          "🗑️ *Remove Product*\n\nPick a product to delete:")

    def _execute_remove(self, phone_number: str, context: dict) -> list:
        """Delete the product."""
        product_key = context.get("cat_product_key", "")
        products = self._get_products(phone_number)

        if product_key in products:
            name = products[product_key].get("name", product_key)
            del products[product_key]
            self._save_products(phone_number, products)
            self.session.reset(phone_number)
            return [
                text_response(f"🗑️ *{name}* removed from inventory."),
                button_response("What's next?", [
                    {"id": "cat_stock", "title": "📊 View Stock"},
                    {"id": "cat_add", "title": "➕ Add Product"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]

        self.session.reset(phone_number)
        return [text_response("❓ Product not found.")]

    # ─────────────────────────────────────────────────────────
    # CLEAR CATALOG — Delete all products with double confirmation
    # ─────────────────────────────────────────────────────────

    def _start_clear_catalog(self, phone_number: str) -> list:
        """Ask for confirmation before clearing entire catalog."""
        products = self._get_products(phone_number)
        count = len(products)

        if not products:
            return [text_response("📊 Catalog is already empty.")]

        total_stock = sum(int(p.get("stock", 0)) for p in products.values())

        return [button_response(
            f"⚠️ *Clear Entire Catalog?*\n\n"
            f"This will delete *ALL {count} products* and their data:\n"
            f"• Stock levels ({total_stock} total units)\n"
            f"• Landing costs\n"
            f"• Variants & conversions\n\n"
            f"⚠️ _This cannot be undone!_",
            [
                {"id": "cat_confirm_clear", "title": "🗑️ Yes, Clear All"},
                {"id": "cat_cancel", "title": "← Keep Catalog"},
            ]
        )]

    def _execute_clear_catalog(self, phone_number: str) -> list:
        """Delete all products from the catalog."""
        products = self._get_products(phone_number)
        count = len(products)

        # Clear the products dict
        self._save_products(phone_number, {})
        self.session.reset(phone_number)

        return [
            text_response(
                f"🗑️ *Catalog cleared!*\n\n"
                f"{count} product{'s' if count != 1 else ''} removed.\n\n"
                f"_Add new products to start fresh._"
            ),
            button_response("What's next?", [
                {"id": "cat_add", "title": "➕ Add Product"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # PRODUCT PICKER — shared UI for selecting a product
    # ─────────────────────────────────────────────────────────

    def _show_product_picker(self, phone_number: str, action: str, title: str) -> list:
        """Show product list for selection. Uses multiple sections to exceed 10-row limit."""
        products = self._get_products(phone_number)

        if not products:
            return [text_response(
                "📊 No products in inventory yet.\n\n"
                "Tap ➕ *Add Product* to get started."
            )]

        # Check industry for grouping
        user = self.db.get_user(phone_number) or {}
        industry = user.get("industry_class", user.get("business_type", "trading"))
        is_manufacturing = industry in ("manufacturing", "hybrid")

        # Build rows grouped by type for manufacturing
        if is_manufacturing:
            finished = []
            raw_mats = []
            for key, prod in sorted(products.items(), key=lambda x: x[1].get("name", "")):
                item_type = prod.get("item_type", "")
                if item_type == "overhead":
                    continue  # Skip overhead items from picker
                name = prod.get("name", key)
                stock = int(prod.get("stock", 0))
                row = {
                    "id": f"cat_pick_{key}",
                    "title": name[:24],
                    "description": f"Stock: {stock}"[:72],
                }
                if item_type == "raw_material":
                    raw_mats.append(row)
                else:
                    finished.append(row)

            sections = []
            if finished:
                sections.append({"title": "🏭 Finished Products", "rows": finished[:10]})
            if raw_mats:
                sections.append({"title": "🧱 Raw Materials", "rows": raw_mats[:10]})
            if not sections:
                sections = [{"title": "Products", "rows": []}]
        else:
            # Trading/Services: single section, sorted alphabetically
            rows = []
            for key, prod in sorted(products.items(), key=lambda x: x[1].get("name", "")):
                item_type = prod.get("item_type", "")
                if item_type == "overhead":
                    continue
                name = prod.get("name", key)
                stock = int(prod.get("stock", 0))
                cost = int(prod.get("landing_cost", 0))
                desc_parts = [f"Stock: {stock}"]
                if cost:
                    desc_parts.append(f"₦{cost:,}")
                rows.append({
                    "id": f"cat_pick_{key}",
                    "title": name[:24],
                    "description": " · ".join(desc_parts)[:72],
                })

            # Split into sections of 10 if more than 10 items
            if len(rows) > 10:
                sections = [
                    {"title": "Products (A-M)", "rows": rows[:10]},
                    {"title": "Products (N-Z)", "rows": rows[10:20]},
                ]
            else:
                sections = [{"title": "Products", "rows": rows}]

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "cat_step": "picking_product",
            "cat_action": action,
        })

        # Add "Type name" option to the last section
        if sections and sections[-1].get("rows"):
            sections[-1]["rows"].append({
                "id": "cat_pick___type__",
                "title": "📝 Type Name",
                "description": "Type the product name manually",
            })

        return [list_response(
            header="📦 Select Product",
            body=title + "\n\n_If your item isn't listed, tap 'Type Name' at the bottom._",
            button_text="Select",
            sections=sections
        )]

    def _handle_product_picked(self, phone_number: str, product_key: str,
                                action: str, context: dict) -> list:
        """Route after product is picked based on action."""
        # "Type Name" option — ask user to type the product name
        if product_key == "__type__":
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "typing_product_name",
                "cat_action": action,
            })
            return [text_response(
                "📝 *Type the product name:*\n\n"
                "_e.g. Floor Cleaner 4L, Sulphonic Acid, Dish Wash 1L_"
            )]

        products = self._get_products(phone_number)
        if product_key not in products:
            self.session.reset(phone_number)
            return [text_response("❓ Product not found.")]

        product = products[product_key]
        name = product.get("name", product_key)

        if action == "set_cost":
            current_cost = int(product.get("landing_cost", 0))
            cost_str = f"\nCurrent: *{format_amount(current_cost)}* per unit" if current_cost else ""
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "setting_cost",
                "cat_product_key": product_key,
            })
            return [button_response(
                f"🏷️ *{name}* — Cost Per Unit{cost_str}\n\n"
                f"Enter the cost to buy/produce *one unit*:\n_e.g. 50000, 150K, 10M_\n\n"
                f"_This is your cost price, not selling price._",
                [
                    {"id": "cat_cancel", "title": "← Cancel"},
                ]
            )]

        if action == "adjust_stock":
            current = int(product.get("stock", 0))
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "adjusting_stock",
                "cat_product_key": product_key,
            })
            return [button_response(
                f"📐 *{name}* — Current stock: *{current}*\n\n"
                f"Enter adjustment:\n"
                f"• _+5_ (add 5)\n"
                f"• _-3_ (remove 3)\n"
                f"• _10_ (set to 10)",
                [
                    {"id": "cat_adjust", "title": "← Pick Another"},
                    {"id": "cat_cancel", "title": "✕ Cancel"},
                ]
            )]

        if action == "add_variants":
            existing = product.get("variants", [])
            existing_str = f"\nCurrent: {', '.join(existing)}" if existing else ""
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "adding_variants",
                "cat_product_key": product_key,
            })
            return [text_response(
                f"🏷️ *{name}* — Variants{existing_str}\n\n"
                f"Type variants (comma-separated):\n"
                f"_e.g. Black, White, Red_\n"
                f"_e.g. 2019, 2020, 2024_\n"
                f"_e.g. 1L, 2L, 4L_"
            )]

        if action == "set_conversion":
            existing = product.get("conversions", {})
            existing_str = ""
            if existing:
                existing_str = "\n\n📦 *Current conversions:*\n"
                for ck, cv in existing.items():
                    existing_str += f"  • {ck} = {cv['qty']} {cv['unit']}\n"

            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "setting_conversion",
                "cat_product_key": product_key,
            })
            return [text_response(
                f"📦 *{name}* — Set Conversion{existing_str}\n\n"
                f"Type the conversion:\n"
                f"_e.g. 1 carton = 24 pieces_\n"
                f"_e.g. 1 dozen = 12 pieces_\n"
                f"_e.g. 1 crate = 20 bottles_"
            )]

        if action == "set_unit":
            current_unit = product.get("primary_unit", "")
            unit_str = f"\nCurrent unit: *{current_unit}*" if current_unit else "\n_No unit set yet._"
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "setting_unit",
                "cat_product_key": product_key,
            })
            return [text_response(
                f"📏 *{name}* — Stock Unit{unit_str}\n\n"
                f"What unit is this measured in?\n\n"
                f"_e.g. kg, litres, pieces, meters, bags, bottles, hours_\n\n"
                f"_This is the base unit for stock tracking and production._"
            )]

        if action == "remove_product":
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "confirm_remove",
                "cat_product_key": product_key,
            })
            stock = int(product.get("stock", 0))
            return [button_response(
                f"⚠️ Delete *{name}*?\n\n"
                f"Stock: {stock} units\n"
                f"_This cannot be undone._",
                [
                    {"id": "cat_confirm_remove", "title": "🗑️ Yes, Delete"},
                    {"id": "cat_cancel", "title": "← Keep It"},
                ]
            )]

        if action == "set_variant_cost":
            existing_costs = product.get("variant_costs", {})
            variants = product.get("variants", [])
            if existing_costs:
                from utils.whatsapp_ui import format_amount as _fmt
                cost_str = "\n\n💲 *Current variant costs:*\n"
                for v, c in existing_costs.items():
                    cost_str += f"  • {v}: {_fmt(c)}\n"
            elif variants:
                cost_str = f"\n\n🏷️ Existing variants: {', '.join(variants)}"
            else:
                cost_str = ""

            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "setting_variant_cost",
                "cat_product_key": product_key,
                "cat_variant_name": "",
            })
            return [text_response(
                f"💲 *{name}* — Variant Costs{cost_str}\n\n"
                f"Type: _variant name = cost_\n"
                f"Example: _2018 = 19M_\n"
                f"Example: _2026 = 30M_\n\n"
                f"Or just type the variant name:"
            )]

        self.session.reset(phone_number)
        if action == "set_service_price":
            current_price = int(product.get("landing_cost", 0))
            price_str = f"\nCurrent: *{format_amount(current_price)}*" if current_price else ""
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "setting_cost",
                "cat_product_key": product_key,
            })
            return [text_response(
                f"💰 *{name}* — Standard Price{price_str}\n\n"
                f"How much do you charge for this service?\n\n"
                f"_e.g. 15000, 25K, 50K_\n\n"
                f"_Type *back* to cancel_"
            )]

        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # INVENTORY UPDATE — called by transaction handler
    # ─────────────────────────────────────────────────────────

    def update_stock(self, phone_number: str, product_name: str, qty_change: int,
                     unit_cost: int = 0, quantity_str: str = "", variant: str = "") -> dict:
        """
        Update stock for a product. Called after purchase (+) or sale (-).
        Also updates landing_cost if provided.
        
        If quantity_str contains a unit (e.g. "3 cartons"), applies conversion.
        If variant is provided, updates variant_stock and syncs to total.
        On purchase with unit_cost, appends to cost_history and updates weighted avg.
        
        Returns: {"matched": True/False, "product": name, "new_stock": int, "variant": str}
        """
        products = self._get_products(phone_number)

        # Find product by name (case-insensitive fuzzy match)
        matched_key = self._find_product_key(products, product_name)

        if not matched_key:
            return {"matched": False, "product": product_name, "new_stock": 0}

        product = products[matched_key]

        # Apply conversion if quantity_str has a unit
        actual_qty = qty_change
        incoming_unit = ""
        if quantity_str:
            converted = self._apply_conversion(product, quantity_str, qty_change)
            if converted is not None:
                actual_qty = converted
            # Extract unit from quantity_str for standard conversion check
            import re
            unit_match = re.match(r'^[\d.]+\s+(.+)', str(quantity_str).strip())
            if unit_match:
                incoming_unit = unit_match.group(1).strip().lower()

        # ── Standard unit conversion to primary_unit ──
        primary_unit = product.get("primary_unit", "").lower().strip()
        _unit_warning = None
        if primary_unit and incoming_unit and incoming_unit.rstrip("s") != primary_unit.rstrip("s"):
            # Try to convert incoming unit to primary_unit using standard conversions
            factor = self._get_standard_conversion_factor(incoming_unit, primary_unit)
            if factor:
                actual_qty = abs(qty_change) * factor * (1 if qty_change >= 0 else -1)
                # Also adjust unit_cost to primary_unit
                if unit_cost and unit_cost > 0:
                    # Original cost was per incoming_unit, convert to per primary_unit
                    # e.g. ₦5/CL → ₦500/litre (factor=0.01 means 1CL=0.01L, so cost×(1/factor))
                    unit_cost = int(unit_cost / factor) if factor > 0 else unit_cost
            else:
                # No conversion found — flag unit mismatch warning
                _unit_warning = (
                    f"⚠️ *Unit mismatch:* You entered *{incoming_unit}* but "
                    f"*{product.get('name', matched_key)}* is stored in *{primary_unit}*.\n\n"
                    f"No conversion found — stock was added as-is.\n\n"
                    f"_To fix: go to Catalog → {product.get('name', matched_key)} → Set Conversion_\n"
                    f"_e.g. \"1 {incoming_unit} = X {primary_unit}\"_"
                )

        # ── Variant-level stock update ──
        variant_stock = product.get("variant_stock", {})
        resolved_variant = variant.strip() if variant else ""

        if resolved_variant and resolved_variant in variant_stock:
            # Update variant stock
            current_variant = int(variant_stock.get(resolved_variant, 0))
            new_variant_stock = max(0, current_variant + actual_qty)
            variant_stock[resolved_variant] = new_variant_stock
            product["variant_stock"] = variant_stock

            # Recalculate total stock from all variants
            product["stock"] = sum(int(v) for v in variant_stock.values())
        elif resolved_variant and actual_qty > 0:
            # New variant being added via purchase — initialize it
            variant_stock[resolved_variant] = max(0, actual_qty)
            product["variant_stock"] = variant_stock

            # Add to variants list if not there
            variants_list = product.get("variants", [])
            if resolved_variant not in variants_list:
                variants_list.append(resolved_variant)
                product["variants"] = variants_list

            # Recalculate total stock
            product["stock"] = sum(int(v) for v in variant_stock.values())
        else:
            # No variant specified or no variant_stock exists — update total directly
            current = int(product.get("stock", 0))
            new_stock = max(0, current + actual_qty)
            product["stock"] = new_stock

        # ── Landing cost update (from purchase) ──
        effective_unit_cost = unit_cost
        if unit_cost and unit_cost > 0:
            # If conversion was applied, adjust cost per base unit
            if actual_qty != qty_change and abs(qty_change) > 0:
                effective_unit_cost = int(unit_cost * abs(qty_change) / abs(actual_qty)) if actual_qty != 0 else unit_cost

            if resolved_variant:
                # Update variant-specific cost (weighted average)
                variant_costs = product.get("variant_costs", {})
                old_cost = int(variant_costs.get(resolved_variant, 0))
                old_stock = int(variant_stock.get(resolved_variant, 0)) - abs(actual_qty)
                old_stock = max(0, old_stock)

                if old_cost > 0 and old_stock > 0:
                    # Weighted average: (old_cost × old_stock + new_cost × new_qty) / total
                    total_units = old_stock + abs(actual_qty)
                    weighted_avg = int((old_cost * old_stock + effective_unit_cost * abs(actual_qty)) / total_units)
                    variant_costs[resolved_variant] = weighted_avg
                else:
                    variant_costs[resolved_variant] = effective_unit_cost

                product["variant_costs"] = variant_costs
            else:
                # Update base landing_cost (weighted average)
                old_cost = int(product.get("landing_cost", 0))
                old_stock = int(product.get("stock", 0)) - abs(actual_qty)
                old_stock = max(0, old_stock)

                if old_cost > 0 and old_stock > 0:
                    total_units = old_stock + abs(actual_qty)
                    weighted_avg = int((old_cost * old_stock + effective_unit_cost * abs(actual_qty)) / total_units)
                    product["landing_cost"] = weighted_avg
                else:
                    product["landing_cost"] = effective_unit_cost

            # ── Append to cost_history (purchases only) ──
            if actual_qty > 0:
                from datetime import datetime
                cost_history = product.get("cost_history", [])
                cost_history.append({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "cost": effective_unit_cost,
                    "qty": abs(actual_qty),
                    "variant": resolved_variant,
                })
                # Keep last 50 entries to avoid bloating
                product["cost_history"] = cost_history[-50:]

        self._save_products(phone_number, products)

        return {
            "matched": True,
            "product": product.get("name", matched_key),
            "new_stock": int(product.get("stock", 0)),
            "variant": resolved_variant,
            "landing_cost": int(product.get("variant_costs", {}).get(resolved_variant, product.get("landing_cost", 0))) if resolved_variant else int(product.get("landing_cost", 0)),
            "unit_warning": _unit_warning,
        }

    def get_landing_cost(self, phone_number: str, product_name: str) -> int:
        """Look up landing cost for a product by name. 
        Checks variant_costs first (if product_name contains a variant), 
        then falls back to the base product landing_cost.
        Returns 0 if not found.
        """
        products = self._get_products(phone_number)
        matched_key = self._find_product_key(products, product_name)
        if not matched_key:
            return 0

        product = products[matched_key]
        variant_costs = product.get("variant_costs", {})

        # Check if product_name contains a variant (e.g. "Toyota RAV4 2018")
        if variant_costs:
            product_name_lower = product_name.lower()
            for variant, cost in variant_costs.items():
                if variant.lower() in product_name_lower:
                    return int(cost)

        # Fallback to base landing_cost
        return int(product.get("landing_cost", 0))

    # Item types that are sellable in a manufacturing/hybrid sale picker.
    # Everything else (raw_material, overhead, consumable) is a production input,
    # not a finished good, so it must never appear when recording an output sale.
    _SELLABLE_ITEM_TYPES = ("finished_product", "product", "service", "")

    def get_product_list_for_recording(self, phone_number: str) -> list:
        """Get products as rows for the Record Sale/Purchase picker.
        For manufacturing: only shows finished products (not raw materials/overhead).
        """
        # Check industry — manufacturing sales should only show finished products
        user = self.db.get_user(phone_number) or {}
        industry = user.get("industry_class", user.get("business_type", "trading"))
        is_manufacturing = industry in ("manufacturing", "hybrid")

        # For manufacturing/hybrid, auto-classify items first so raw materials that
        # were only added via a recipe (and never explicitly tagged) get detected
        # and excluded. ensure_item_types tags recipe inputs as raw_material.
        if is_manufacturing:
            products = self.ensure_item_types(phone_number)
        else:
            products = self._get_products(phone_number)

        if not products:
            return []

        rows = []
        for key, prod in sorted(products.items(), key=lambda x: x[1].get("name", "")):
            item_type = prod.get("item_type", "")

            # For manufacturing: only show sellable finished goods, never inputs.
            # Use an allowlist so any input type (raw_material/overhead/consumable)
            # is excluded even if new input types are added later.
            if is_manufacturing and item_type not in self._SELLABLE_ITEM_TYPES:
                continue

            name = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            cost = int(prod.get("landing_cost", 0))
            primary_unit = prod.get("primary_unit", "")

            # Show the tracking unit in the title so the user knows what unit
            # this item is measured in — e.g. "Flour (kg)".
            if primary_unit:
                title = f"📦 {name} ({primary_unit})"
            else:
                title = f"📦 {name}"

            indicator = "🟢" if stock > 3 else ("🟡" if stock > 0 else "🔴")
            # Include the unit alongside the stock count: "50 kg in stock".
            unit_suffix = f" {primary_unit}" if primary_unit else ""
            desc = f"{indicator} {stock}{unit_suffix} in stock"
            if cost:
                unit_label = primary_unit if primary_unit else "unit"
                desc += f" · ₦{cost:,}/{unit_label}"

            rows.append({
                "id": f"catrec_{key}",
                "title": title[:24],
                "description": desc[:72],
            })

            if len(rows) >= 9:  # Leave room for "Other" option
                break

        return rows

    def get_services_list_for_recording(self, phone_number: str) -> list:
        """Get services as rows for the Record Job picker (services industry)."""
        products = self._get_products(phone_number)
        if not products:
            return []

        # Show only services (not supplies)
        services = {k: v for k, v in products.items()
                    if v.get("item_type") in ("service", "product", "") or not v.get("item_type")}

        rows = []
        for key, prod in list(services.items())[:9]:
            name = prod.get("name", key)
            price = int(prod.get("landing_cost", 0))
            price_str = f"₦{price:,}" if price else "No price set"

            rows.append({
                "id": f"catrec_{key}",
                "title": f"💼 {name}"[:24],
                "description": price_str[:72],
            })

        return rows

    # ─────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────

    def _find_product_key(self, products: dict, search_name: str) -> str:
        """Find a product key by name — case-insensitive fuzzy matching."""
        if not search_name:
            return ""

        search_lower = search_name.lower().strip()

        # Exact key match
        if search_lower.replace(" ", "_") in products:
            return search_lower.replace(" ", "_")

        # Exact name match (case-insensitive)
        for key, prod in products.items():
            if prod.get("name", "").lower() == search_lower:
                return key

        # Partial match — search name contains product name or vice versa
        for key, prod in products.items():
            name = prod.get("name", "").lower()
            if name in search_lower or search_lower in name:
                return key

        # Check variants
        for key, prod in products.items():
            variants = [v.lower() for v in prod.get("variants", [])]
            if search_lower in variants:
                return key
            # Check if search contains product name + variant
            name = prod.get("name", "").lower()
            for variant in variants:
                if f"{name} {variant}" == search_lower or f"{variant} {name}" == search_lower:
                    return key

        return ""

    def _get_products(self, phone_number: str) -> dict:
        """Get the product catalog from user profile."""
        user = self.db.get_user(phone_number)
        if not user:
            return {}
        catalog = user.get("product_catalog", {})
        if isinstance(catalog, dict):
            return catalog.get("products", {})
        return {}

    # ═════════════════════════════════════════════════════════
    # 2A — RICH PRODUCT MODEL (additive, backward-compatible)
    #
    # Old products are a FLAT dict: {name, stock, landing_cost, item_type,
    # category, variants[list], variant_stock{}, variant_costs{}, recipe,
    # conversions, primary_unit}. The redesign layers on OPTIONAL fields:
    #   attributes{}      — user-named variant axes, e.g. {"Colour":[...], "Year":[...]}
    #   sku, barcode      — optional codes for type-to-find at sale time
    #   reorder_level     — low-stock threshold (0 = not set)
    #   supplier          — default supplier name
    #   stock_movements[] — append-only history (+/- qty, reason, date, tx_id)
    #
    # NOTHING is bulk-rewritten. `normalize_product` reads ANY product (old flat
    # or new rich) into ONE consistent shape with safe defaults, so all higher
    # layers (browse, card, units) can rely on the same fields. Absence of a new
    # field just means its default (e.g. no attributes → simple product).
    # ═════════════════════════════════════════════════════════

    # The canonical rich shape + its safe defaults. Reading a product NEVER
    # crashes on a missing field because we always start from these defaults.
    _PRODUCT_DEFAULTS = {
        "name": "",
        "item_type": "",          # "" is treated as a sellable product
        "category": "",
        "stock": 0,
        "landing_cost": 0,        # what you PAY per unit (cost)
        "sale_price": 0,          # what you CHARGE per unit (0 = not set)
        "primary_unit": "",       # master/base unit (e.g. "piece", "kg")
        "conversions": {},        # e.g. {"1 bag": "20 pieces"}
        "attributes": {},         # user-named axes: {"Colour": ["Black","White"], ...}
        "variants": [],           # legacy flat variant list (read as one axis)
        "variant_stock": {},      # per-variant stock, keyed by variant/combo
        "variant_costs": {},      # per-variant cost
        "recipe": [],             # manufacturing bill-of-materials (unchanged)
        "sku": "",
        "barcode": "",
        "reorder_level": 0,       # 0 = no reorder threshold set
        "supplier": "",
        "stock_movements": [],    # append-only log
    }

    def normalize_product(self, product: dict, key: str = "") -> dict:
        """Return a product in the canonical rich shape (safe defaults filled).

        Works for BOTH old flat products and new rich ones. Purely a READ helper
        — it does not persist anything. Legacy `variants` (a flat list) is
        surfaced as a single implicit attribute axis named 'Variant' when no
        explicit `attributes` exist, so old products render in the new UI without
        migration.
        """
        if not isinstance(product, dict):
            product = {}
        norm = dict(self._PRODUCT_DEFAULTS)
        # Deep-copy the mutable defaults so callers can't mutate the class dict.
        norm["conversions"] = {}
        norm["attributes"] = {}
        norm["variants"] = []
        norm["variant_stock"] = {}
        norm["variant_costs"] = {}
        norm["recipe"] = []
        norm["stock_movements"] = []

        for k, default in self._PRODUCT_DEFAULTS.items():
            if k in product and product[k] is not None:
                norm[k] = product[k]

        # Coerce numerics safely (stored values are sometimes strings/Decimals).
        for num_key in ("stock", "landing_cost", "sale_price", "reorder_level"):
            norm[num_key] = self._as_int(norm.get(num_key), 0)

        # If no display name, fall back to the key (slug → Title Case).
        if not norm["name"]:
            norm["name"] = (key or "").replace("_", " ").title()

        # Legacy bridge: a flat `variants` list with no explicit `attributes`
        # becomes one implicit axis so the drill-down UI has something to show.
        if norm["variants"] and not norm["attributes"]:
            norm["attributes"] = {"Variant": list(norm["variants"])}

        # Derived, read-only conveniences (not stored):
        norm["_has_variants"] = bool(norm["attributes"])
        norm["_is_low_stock"] = (
            norm["reorder_level"] > 0 and norm["stock"] <= norm["reorder_level"]
        )
        norm["_stock_value"] = norm["stock"] * norm["landing_cost"]
        norm["_key"] = key
        return norm

    def _as_int(self, value, default: int = 0) -> int:
        """Coerce a possibly-string/Decimal/float value to int, safely."""
        try:
            if value is None or value == "":
                return default
            return int(float(value))
        except (ValueError, TypeError):
            return default

    def get_normalized_product(self, phone_number: str, key: str) -> dict:
        """Fetch one product by key and return it in the canonical rich shape."""
        products = self._get_products(phone_number)
        return self.normalize_product(products.get(key, {}), key=key)

    def record_stock_movement(self, phone_number: str, key: str, delta: int,
                              reason: str = "", tx_id: str = "") -> bool:
        """Append an entry to a product's stock_movements log (additive history).

        Data layer only — no UX yet (2G surfaces it). Best-effort: never blocks a
        sale. Does NOT change `stock` itself (the existing update_stock owns that);
        this is purely the audit trail. Absence of the log = "no history yet".
        """
        from datetime import datetime
        try:
            products = self._get_products(phone_number)
            prod = products.get(key)
            if not isinstance(prod, dict):
                return False
            movements = prod.get("stock_movements")
            if not isinstance(movements, list):
                movements = []
            movements.append({
                "delta": self._as_int(delta, 0),
                "reason": (reason or "")[:60],
                "tx_id": tx_id or "",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "at": datetime.now().isoformat(timespec="seconds"),
            })
            # Cap the log so the item doesn't grow unbounded (keep last 100).
            prod["stock_movements"] = movements[-100:]
            products[key] = prod
            self._save_products(phone_number, products)
            return True
        except Exception as e:
            logger.warning(f"record_stock_movement failed for {key}: {e}")
            return False

    # Item types that represent countable stock (ask "how many?" for these).
    # A "service" is delivered, not counted, so it never gets a quantity step.
    _COUNTED_STOCK_TYPES = ("finished_product", "product", "raw_material",
                            "consumable", "supply", "")

    def suggest_quantities(self, phone_number: str, item_name: str,
                           tx_type: str = "sale", limit: int = 40) -> list:
        """Suggest quantity buttons LEARNED from this item's recent history.

        Returns up to 6 quantities the user has actually used for this item
        (most frequent first). Empty list => caller uses a sensible default
        spread. This is what makes the boxes adapt to how the user really sells
        (e.g. 500 / 1000 / 2000 sachets) instead of a fixed 1–10.
        """
        return self._suggest_field(phone_number, item_name, "quantity", tx_type, limit)

    def suggest_prices(self, phone_number: str, item_name: str, product_key: str = "",
                       counted_stock: bool = True, tx_type: str = "sale",
                       limit: int = 40) -> list:
        """Suggest price buttons for an item.

        For counted stock we want the PRICE EACH: prefer the item's set price
        (landing_cost) + recent per-unit prices. For non-counted (services) we
        want recent TOTAL amounts. Empty => caller uses defaults.
        """
        suggestions = []
        # A set catalog price is the strongest signal for "price each".
        if counted_stock and product_key:
            info = self.get_item_info(phone_number, product_key)
            if info.get("landing_cost"):
                suggestions.append(int(info["landing_cost"]))
        field = "unit_cost" if counted_stock else "amount"
        suggestions += self._suggest_field(phone_number, item_name, field, tx_type, limit)
        # De-dupe preserving order.
        seen, out = set(), []
        for v in suggestions:
            v = int(v)
            if v > 0 and v not in seen:
                seen.add(v)
                out.append(v)
        return out[:6]

    def _suggest_field(self, phone_number: str, item_name: str, field: str,
                       tx_type: str, limit: int) -> list:
        """Pull the most-used values of `field` for this item from recent history."""
        try:
            txns = self.db.get_transactions(phone_number, limit=limit) or []
        except Exception as e:
            logger.warning(f"suggest: history read failed: {e}")
            return []
        name_l = (item_name or "").strip().lower()
        counts = {}
        for t in txns:
            if tx_type and t.get("type") != tx_type:
                continue
            tname = (t.get("item_name") or t.get("description") or "").strip().lower()
            if name_l and name_l not in tname and tname not in name_l:
                continue
            val = t.get(field)
            if val is None:
                continue
            try:
                n = int(float(val))
            except (ValueError, TypeError):
                continue
            if n > 0:
                counts[n] = counts.get(n, 0) + 1
        # Most frequent first, then larger values first as a tiebreak.
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], -kv[0]))
        return [n for n, _ in ordered[:6]]

    def get_item_info(self, phone_number: str, product_key: str) -> dict:
        """
        Look up a catalog item by key for the Telegram tidy-box flow.

        Returns a dict: {"key", "name", "item_type", "is_counted_stock",
        "landing_cost"}. If the key isn't found, returns a safe default that
        treats it as counted stock (so we don't wrongly skip quantity).
        """
        products = self._get_products(phone_number)
        prod = products.get(product_key)
        if not isinstance(prod, dict):
            return {
                "key": product_key,
                "name": product_key,
                "item_type": "",
                "is_counted_stock": True,
                "landing_cost": 0,
            }
        item_type = prod.get("item_type", "") or ""
        return {
            "key": product_key,
            "name": prod.get("name", product_key),
            "item_type": item_type,
            "is_counted_stock": item_type in self._COUNTED_STOCK_TYPES
                                and item_type != "service",
            "landing_cost": int(prod.get("landing_cost", 0) or 0),
        }

    def _apply_conversion(self, product: dict, quantity_str: str, raw_qty: int):
        """
        Check if quantity_str contains a unit that has a conversion defined.
        e.g. "3 cartons" with conversion "1 carton = 24 pieces" → returns 72 (or -72)
        
        Returns: converted quantity (int) or None if no conversion applies.
        """
        conversions = product.get("conversions", {})
        if not conversions or not quantity_str:
            return None

        qty_str = str(quantity_str).lower().strip()

        # Extract unit from quantity string (e.g. "3 cartons" → "carton")
        match = re.match(r'^(\d+)\s+(.+)', qty_str)
        if not match:
            return None

        unit = match.group(2).strip().rstrip("s")  # Remove trailing 's' for plural

        # Check conversions
        for conv_key, conv_val in conversions.items():
            # conv_key = "1 carton", conv_val = {"qty": 24, "unit": "pieces"}
            key_match = re.match(r'^(\d+)\s+(.+)', conv_key)
            if not key_match:
                continue
            conv_unit = key_match.group(2).strip().rstrip("s")
            conv_from_qty = int(key_match.group(1))
            conv_to_qty = conv_val.get("qty", 1)

            if unit == conv_unit or unit == conv_unit + "s" or conv_unit == unit + "s":
                # Match found — calculate
                # raw_qty is already the number of [units] (e.g. 3 cartons → raw_qty = 3)
                multiplier = conv_to_qty // conv_from_qty
                sign = 1 if raw_qty >= 0 else -1
                return abs(raw_qty) * multiplier * sign

        return None

    def _get_standard_conversion_factor(self, from_unit: str, to_unit: str) -> float:
        """
        Get conversion factor between two standard units.
        Returns the factor to multiply from_unit quantity to get to_unit quantity.
        e.g. _get_standard_conversion_factor("cl", "litres") → 0.01 (100 CL = 1 litre)
        Returns 0 if no conversion found.
        """
        from_normalized = from_unit.lower().strip().rstrip("s")
        to_normalized = to_unit.lower().strip().rstrip("s")

        if from_normalized == to_normalized:
            return 1.0

        # Standard metric conversions (same as production.py STANDARD_CONVERSIONS)
        CONVERSIONS = {
            # Volume
            ("ml", "l"): 0.001, ("ml", "litre"): 0.001, ("ml", "liter"): 0.001,
            ("l", "ml"): 1000, ("litre", "ml"): 1000, ("liter", "ml"): 1000,
            ("cl", "ml"): 10, ("ml", "cl"): 0.1,
            ("cl", "l"): 0.01, ("cl", "litre"): 0.01, ("litre", "cl"): 100, ("l", "cl"): 100,
            # Weight
            ("g", "kg"): 0.001, ("kg", "g"): 1000,
            ("mg", "g"): 0.001, ("g", "mg"): 1000,
            ("gram", "kg"): 0.001, ("kg", "gram"): 1000,
            ("tonne", "kg"): 1000, ("kg", "tonne"): 0.001,
            # Time
            ("min", "hour"): 1/60, ("hour", "min"): 60,
            ("minute", "hour"): 1/60, ("hour", "minute"): 60,
            ("hr", "min"): 60, ("min", "hr"): 1/60,
            ("hour", "day"): 1/24, ("day", "hour"): 24,
            # Energy
            ("kwh", "whr"): 1, ("whr", "kwh"): 1,
            ("wh", "kwh"): 0.001, ("kwh", "wh"): 1000,
            # Quantity synonyms
            ("piece", "unit"): 1, ("unit", "piece"): 1,
            ("pc", "piece"): 1, ("piece", "pc"): 1,
            # Volume larger
            ("gallon", "litre"): 3.785, ("litre", "gallon"): 0.264,
            ("drum", "litre"): 200, ("litre", "drum"): 0.005,
        }

        # Try direct match
        for (f, t), factor in CONVERSIONS.items():
            if from_normalized == f.rstrip("s") and to_normalized == t.rstrip("s"):
                return factor

        return 0

    def _save_products(self, phone_number: str, products: dict):
        """Save products dict to user profile."""
        self.db.update_user_field(phone_number, "product_catalog", {"products": products})

    def ensure_item_types(self, phone_number: str) -> dict:
        """
        Ensure all products have an item_type tag. Auto-detects:
        - 'finished_product': has a recipe defined
        - 'raw_material': appears in another product's recipe
        - 'product': default (not yet classified)
        
        Returns the updated products dict (also saves to DB if changes were made).
        """
        products = self._get_products(phone_number)
        if not products:
            return products

        # Identify raw materials (appear in any recipe)
        raw_material_keys = set()
        for key, data in products.items():
            recipe = data.get("recipe", [])
            for mat in recipe:
                mat_key = mat.get("material", "").lower().replace(" ", "_")
                raw_material_keys.add(mat_key)

        changed = False
        for key, data in products.items():
            has_recipe = bool(data.get("recipe", []))
            is_raw_material = key in raw_material_keys
            current_type = data.get("item_type", "")

            # Don't override manually-set types (overhead, consumable, service)
            if current_type in ("overhead", "consumable", "service"):
                continue

            if has_recipe and current_type != "finished_product":
                data["item_type"] = "finished_product"
                changed = True
            elif is_raw_material and not has_recipe and current_type != "raw_material":
                data["item_type"] = "raw_material"
                changed = True
            elif not current_type:
                # Unclassified — leave as "product" (user hasn't set recipe yet)
                data["item_type"] = "product"
                changed = True

        if changed:
            self._save_products(phone_number, products)

        return products

    def get_raw_materials(self, phone_number: str) -> dict:
        """Get only raw materials from catalog."""
        products = self.ensure_item_types(phone_number)
        return {k: v for k, v in products.items() if v.get("item_type") == "raw_material"}

    def get_finished_products(self, phone_number: str) -> dict:
        """Get only finished products from catalog."""
        products = self.ensure_item_types(phone_number)
        return {k: v for k, v in products.items() if v.get("item_type") == "finished_product"}

    def get_materials_list_for_purchase(self, phone_number: str) -> list:
        """Get raw materials as rows for the Buy Raw Materials picker."""
        products = self.ensure_item_types(phone_number)
        raw_materials = {k: v for k, v in products.items()
                         if v.get("item_type") == "raw_material"}

        if not raw_materials:
            return []

        rows = []
        for key, prod in list(raw_materials.items())[:9]:
            name = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            cost = int(prod.get("landing_cost", 0))
            primary_unit = prod.get("primary_unit", "")

            # Show primary unit in title: "Water (Liters)"
            if primary_unit:
                title = f"🧱 {name} ({primary_unit})"
            else:
                title = f"🧱 {name}"

            indicator = "🟢" if stock > 5 else ("🟡" if stock > 0 else "🔴")
            desc = f"{indicator} {stock} in stock"
            if cost:
                # Show cost with the primary unit label: "₦100/Liters"
                unit_label = primary_unit if primary_unit else "unit"
                desc += f" · ₦{cost:,}/{unit_label}"

            rows.append({
                "id": f"catrec_{key}",
                "title": title[:24],
                "description": desc[:72],
            })

        return rows
