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
        count = len(products)
        total_stock = sum(int(p.get("stock", 0)) for p in products.values())

        # Count by type
        finished_count = sum(1 for p in products.values() if p.get("item_type") == "finished_product")
        material_count = sum(1 for p in products.values() if p.get("item_type") == "raw_material")

        body = f"📊 *Inventory* — {count} item{'s' if count != 1 else ''}"
        if total_stock > 0:
            body += f" · {total_stock} total units"
        body += "\n\nWhat would you like to do?"

        # Check user's industry for manufacturing-specific options
        user = self.db.get_user(phone_number) or {}
        industry = user.get("industry_class", user.get("business_type", "trading"))
        is_manufacturing = industry in ("manufacturing", "hybrid")

        rows = []

        if is_manufacturing:
            # Manufacturing: separate view options
            rows.extend([
                {"id": "cat_view_products", "title": f"🏭 View Products ({finished_count})",
                 "description": "Finished goods you manufacture"},
                {"id": "cat_view_materials", "title": f"🧱 View Materials ({material_count})",
                 "description": "Raw materials & inputs"},
            ])
        else:
            # Trading/Services: single stock view
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

        # Manufacturing: add recipe option
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

        if button_id == "cat_remove":
            return self._start_remove_product(phone_number)

        if button_id == "cat_clear_all":
            return self._start_clear_catalog(phone_number)

        # ── New: View & Edit buttons ──
        if button_id == "cat_view_products":
            return self._view_products_list(phone_number)

        if button_id == "cat_view_materials":
            return self._view_materials_list(phone_number)

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
        if text_low in ("cancel", "exit", "done", "stop", "back"):
            self.session.reset(phone_number)
            return [
                text_response("✅ Done!"),
                button_response("What's next?", [
                    {"id": "cat_stock", "title": "📊 View Stock"},
                    {"id": "cat_add", "title": "➕ Add Product"},
                    {"id": "record_sale", "title": "💰 Record Sale"},
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

        # Edit product steps
        if step in ("editing_name", "editing_stock", "editing_cost"):
            return self._handle_edit_input(phone_number, text_s, context)

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
            other = {k: v for k, v in products.items() if v.get("item_type") not in ("finished_product", "raw_material")}

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
                {"id": "cat_add", "title": "➕ Add Product"},
                {"id": "cat_cost", "title": "🏷️ Set Cost"},
            ])
        ]

    def _format_stock_line(self, prod: dict, lines: list) -> tuple:
        """Format a single product's stock line. Returns (stock, value) for totals."""
        name = prod.get("name", "?")
        stock = int(prod.get("stock", 0))
        cost = int(prod.get("landing_cost", 0))
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

        if variant_stock:
            lines.append(f"   Stock: *{stock}* total")
            for v_name, v_stock in variant_stock.items():
                v_cost = int(variant_costs.get(v_name, 0))
                v_stock_int = int(v_stock)
                value += v_stock_int * v_cost
                v_ind = "🔴" if v_stock_int <= 0 else ("🟡" if v_stock_int <= 2 else "•")
                cost_str = f" · {format_amount(v_cost)}" if v_cost else ""
                lines.append(f"   {v_ind} {v_name}: {v_stock_int}{cost_str}")
        else:
            value = stock * cost
            lines.append(f"   Stock: *{stock}*" + (f" · {format_amount(cost)}/unit" if cost else ""))

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

    def _view_product_detail(self, phone_number: str, product_key: str) -> list:
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
            for mat in recipe:
                mat_cost = mat.get("cost_per_unit", 0)
                cost_str = f" @ {format_amount(mat_cost)}" if mat_cost else ""
                lines.append(f"  • {mat['quantity']} {mat.get('unit', '')} {mat['material']}{cost_str}")

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
                action = f"+{qty}"
            elif sub_match:
                qty = float(sub_match.group(1))
                prod["stock"] = max(0, current - qty)
                action = f"-{qty}"
            elif set_match:
                qty = float(set_match.group(1))
                prod["stock"] = qty
                action = f"set to {qty}"
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
                    {"id": "record_sale", "title": "💰 Record Sale"},
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
                {"id": "record_sale", "title": "💰 Record Sale"},
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
                {"id": "record_sale", "title": "💰 Record Sale"},
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
                    {"id": "record_sale", "title": "💰 Record Sale"},
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
        """Show product list for selection. Saves action to context."""
        products = self._get_products(phone_number)

        if not products:
            return [text_response(
                "📊 No products in inventory yet.\n\n"
                "Tap ➕ *Add Product* to get started."
            )]

        rows = []
        for key, prod in list(products.items())[:10]:
            name  = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            cost  = int(prod.get("landing_cost", 0))
            desc_parts = [f"Stock: {stock}"]
            if cost:
                desc_parts.append(f"Cost: ₦{cost:,}")
            rows.append({
                "id": f"cat_pick_{key}",
                "title": name[:24],
                "description": " · ".join(desc_parts)[:72],
            })

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "cat_step": "picking_product",
            "cat_action": action,
        })

        return [list_response(
            header="📦 Select Product",
            body=title,
            button_text="Select",
            sections=[{"title": "Products", "rows": rows}]
        )]

    def _handle_product_picked(self, phone_number: str, product_key: str,
                                action: str, context: dict) -> list:
        """Route after product is picked based on action."""
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
        if quantity_str:
            converted = self._apply_conversion(product, quantity_str, qty_change)
            if converted is not None:
                actual_qty = converted

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

    def get_product_list_for_recording(self, phone_number: str) -> list:
        """Get products as rows for the Record Sale/Purchase picker."""
        products = self._get_products(phone_number)
        if not products:
            return []

        rows = []
        for key, prod in list(products.items())[:9]:
            name  = prod.get("name", key)
            stock = int(prod.get("stock", 0))
            cost  = int(prod.get("landing_cost", 0))

            indicator = "🟢" if stock > 3 else ("🟡" if stock > 0 else "🔴")
            desc = f"{indicator} {stock} in stock"
            if cost:
                desc += f" · ₦{cost:,}"

            rows.append({
                "id": f"catrec_{key}",
                "title": f"📦 {name}"[:24],
                "description": desc[:72],
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

            indicator = "🟢" if stock > 5 else ("🟡" if stock > 0 else "🔴")
            desc = f"{indicator} {stock} in stock"
            if cost:
                desc += f" · ₦{cost:,}/unit"

            rows.append({
                "id": f"catrec_{key}",
                "title": f"🧱 {name}"[:24],
                "description": desc[:72],
            })

        return rows
