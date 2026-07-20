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
        products = self._get_products(phone_number)
        count = len(products)
        total_stock = sum(p.get("stock", 0) for p in products.values())

        body = f"📊 *Inventory* — {count} product{'s' if count != 1 else ''}"
        if total_stock > 0:
            body += f" · {total_stock} total units"
        body += "\n\nWhat would you like to do?"

        return [list_response(
            header="📊 Inventory",
            body=body,
            button_text="Select Action",
            sections=[{
                "title": "Inventory Actions",
                "rows": [
                    {"id": "cat_stock", "title": "📊 View Stock Levels",
                     "description": "See all products with quantities"},
                    {"id": "cat_add", "title": "➕ Add Product",
                     "description": "Add a new product to inventory"},
                    {"id": "cat_cost", "title": "🏷️ Set Landing Cost",
                     "description": "Set/update cost price per product"},
                    {"id": "cat_adjust", "title": "📐 Adjust Stock",
                     "description": "Manually add or set stock quantity"},
                    {"id": "cat_conversion", "title": "📦 Set Conversion",
                     "description": "e.g. 1 carton = 24 pieces"},
                    {"id": "cat_variants", "title": "🏷️ Add Variants",
                     "description": "Add models/types to a product"},
                    {"id": "cat_variant_cost", "title": "💲 Set Variant Cost",
                     "description": "Different cost per model/year"},
                    {"id": "cat_remove", "title": "🗑️ Remove Product",
                     "description": "Delete a product from inventory"},
                    {"id": "cat_clear_all", "title": "🗑️ Clear All Products",
                     "description": "Delete entire catalog (asks confirmation)"},
                ]
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

        # Product-specific buttons
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

        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # VIEW STOCK LEVELS
    # ─────────────────────────────────────────────────────────

    def _show_stock_levels(self, phone_number: str) -> list:
        """Show all products with stock, cost, and color indicators."""
        products = self._get_products(phone_number)

        if not products:
            return [text_response(
                "📊 *Stock Levels*\n\n"
                "No products yet.\n\n"
                "Tap ➕ *Add Product* to get started."
            )]

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "📊  *Stock Levels*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        total_stock = 0
        total_value = 0

        for key, prod in sorted(products.items(), key=lambda x: x[1].get("name", "")):
            name    = prod.get("name", key)
            stock   = int(prod.get("stock", 0))
            cost    = int(prod.get("landing_cost", 0))
            category = prod.get("category", "")
            variant_stock = prod.get("variant_stock", {})
            variant_costs = prod.get("variant_costs", {})
            cost_history = prod.get("cost_history", [])

            total_stock += stock

            # Stock indicator
            if stock <= 0:
                indicator = "🔴"
            elif stock <= 3:
                indicator = "🟡"
            else:
                indicator = "🟢"

            line = f"{indicator} *{name}*"
            if category:
                line += f" _{category}_"
            lines.append(line)

            # If variant_stock exists, show per-variant breakdown
            if variant_stock:
                lines.append(f"   Stock: *{stock}* total")
                for v_name, v_stock in variant_stock.items():
                    v_cost = int(variant_costs.get(v_name, 0))
                    v_stock_int = int(v_stock)
                    total_value += v_stock_int * v_cost
                    # Variant stock indicator
                    v_ind = "🔴" if v_stock_int <= 0 else ("🟡" if v_stock_int <= 2 else "•")
                    cost_str = f" · {format_amount(v_cost)}" if v_cost else ""
                    lines.append(f"   {v_ind} {v_name}: {v_stock_int}{cost_str}")
                # Show last cost update if history exists
                if cost_history:
                    last = cost_history[-1]
                    lines.append(f"   _Last cost: {last.get('variant', '')} {format_amount(last.get('cost', 0))} on {last.get('date', '')}_")
            else:
                # No variants — simple display
                total_value += stock * cost
                lines.append(f"   Stock: *{stock}*" + (f" · Cost: {format_amount(cost)}/unit" if cost else ""))

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
                text_response(f"✅ *{name}* cost set to *{format_amount(amount)}* per unit."),
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
            cost_str = f"\nCurrent: *{format_amount(current_cost)}*" if current_cost else ""
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "cat_step": "setting_cost",
                "cat_product_key": product_key,
            })
            return [button_response(
                f"🏷️ *{name}* — Landing Cost{cost_str}\n\n"
                f"Enter cost per unit:\n_e.g. 50000, 150K, 10M_",
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
