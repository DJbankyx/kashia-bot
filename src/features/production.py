# src/features/production.py
"""Production Recording — Manufacturing-specific feature.

Handles:
- Record Production (finished goods produced from raw materials)
- Recipe/BOM management (define what materials make each product)
- Auto-deduction of raw materials from stock
- Production cost calculation per unit
"""

import logging
import re
from datetime import datetime

from core import states
from utils.whatsapp_ui import (
    text_response, button_response, list_response, format_amount
)
from utils.parser import parse_amount

logger = logging.getLogger(__name__)


class ProductionHandler:
    """Handles production recording and recipe/BOM management."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    # ─────────────────────────────────────────────────────────
    # RECORD PRODUCTION — Guided flow
    # ─────────────────────────────────────────────────────────

    def start_production(self, phone_number: str) -> list:
        """Start the Record Production flow — pick finished product from catalog."""
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if not products:
            return [text_response(
                "🏭 *Record Production*\n\n"
                "You need products in your catalog first.\n\n"
                "Go to: *Products & Materials → ➕ Add Product*\n"
                "Add your finished products and raw materials,\n"
                "then set a recipe for what you produce."
            )]

        # Identify raw materials (products that appear in other products' recipes)
        raw_material_keys = set()
        for key, data in products.items():
            recipe = data.get("recipe", [])
            for mat in recipe:
                mat_key = mat.get("material", "").lower().replace(" ", "_")
                raw_material_keys.add(mat_key)

        # Filter: show only finished products (have a recipe OR not a raw material)
        # Priority: products WITH recipes first (they're ready to produce)
        finished_products = {}
        for key, data in products.items():
            has_recipe = bool(data.get("recipe", []))
            is_raw_material = key in raw_material_keys

            # Show if: has a recipe (definitely a finished product)
            # OR: not identified as a raw material (could be a finished product without recipe yet)
            if has_recipe:
                finished_products[key] = data
            elif not is_raw_material:
                finished_products[key] = data

        if not finished_products:
            return [text_response(
                "🏭 *Record Production*\n\n"
                "No finished products found.\n\n"
                "You have raw materials but no products with recipes set.\n\n"
                "Set a recipe first:\n"
                "→ *Products & Materials → 📋 Set Recipe*\n\n"
                "_A recipe tells the bot what materials are needed to make each product._"
            )]

        # Build product list — show recipe-ready products first
        rows = []
        with_recipe = [(k, d) for k, d in finished_products.items() if d.get("recipe")]
        without_recipe = [(k, d) for k, d in finished_products.items() if not d.get("recipe")]

        for key, data in (with_recipe + without_recipe)[:9]:
            name = data.get("name", key)
            recipe = data.get("recipe", [])
            stock = int(data.get("stock", data.get("stock_count", 0)))

            if recipe:
                recipe_str = f"✅ Recipe: {len(recipe)} materials"
            else:
                recipe_str = "⚠️ No recipe — set one first"
            desc = f"Stock: {stock} · {recipe_str}"

            rows.append({
                "id": f"prod_item_{key}",
                "title": f"📦 {name}"[:24],
                "description": desc[:72],
            })

        rows.append({
            "id": "prod_set_recipe",
            "title": "📋 Set/Edit Recipe",
            "description": "Define materials needed per product",
        })

        self.session.save(phone_number, states.PRODUCTION_RECORDING, {
            "prod_step": "pick_product",
        })

        return [list_response(
            header="🏭 Record Production",
            body="What did you produce?",
            button_text="Select Product",
            sections=[{"title": "Finished Products", "rows": rows}]
        )]

    # ─────────────────────────────────────────────────────────
    # STATE HANDLER
    # ─────────────────────────────────────────────────────────

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle production recording states."""
        context  = session.get("context", {})
        step     = context.get("prod_step", "pick_product")
        text_s   = text.strip()
        text_low = text_s.lower()

        if text_low in ("cancel", "exit"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        # ── Back handling — go to previous step ──
        if text_low == "back":
            return self._handle_back(phone_number, step, context)

        if step == "pick_product":
            return self._handle_pick_product(phone_number, text_s, context)

        if step == "enter_quantity":
            return self._handle_quantity(phone_number, text_s, context)

        if step == "yield_check":
            return self._handle_yield(phone_number, text_s, context)

        if step == "confirm_production":
            return self._handle_confirm(phone_number, text_s, context)

        # Recipe steps
        if step == "recipe_pick_product":
            return self._recipe_pick_product(phone_number, text_s, context)

        if step == "recipe_add_material":
            return self._recipe_add_material(phone_number, text_s, context)

        if step == "recipe_ask_type":
            # User typed instead of tapping button — try to interpret
            if text_low in ("material", "raw material", "raw", "1"):
                context["current_mat_type"] = "material"
                context["prod_step"] = "recipe_material_qty"
                self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
                mat_name = context.get("current_material", "Material")
                return [text_response(
                    f"🧱 *{mat_name}* (raw material)\n\n"
                    f"How much is needed to make *1 unit*?\n\n"
                    f"Type: *quantity* then *unit*\n_e.g. 0.5 kg, 50 CL, 2 pieces_"
                )]
            elif text_low in ("overhead", "rate", "electricity", "labour", "machine", "2"):
                context["current_mat_type"] = "overhead"
                context["prod_step"] = "recipe_material_qty"
                self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
                mat_name = context.get("current_material", "Material")
                return [text_response(
                    f"⚡ *{mat_name}* (overhead rate)\n\n"
                    f"How much is used to make *1 unit*?\n\n"
                    f"Type: *quantity* then *unit*\n_e.g. 5 wh, 30 seconds, 0.5 hours_"
                )]
            return [text_response("Please tap *🧱 Raw Material* or *⚡ Overhead Rate*")]

        if step == "recipe_material_qty":
            return self._recipe_material_qty(phone_number, text_s, context)

        if step == "recipe_material_cost":
            return self._recipe_material_cost(phone_number, text_s, context)

        self.session.reset(phone_number)
        return [text_response("Something went wrong. Try again from the menu.")]

    def _handle_back(self, phone_number: str, step: str, context: dict) -> list:
        """Handle 'back' — go to previous production step or exit."""

        if step == "enter_quantity":
            # Back to product list
            return self.start_production(phone_number)

        if step == "yield_check":
            # Back to quantity input (yield is optional, came from Report Waste button)
            product_name = context.get("prod_product_name", "Product")
            context["prod_step"] = "enter_quantity"
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            return [text_response(
                f"🏭 *Producing: {product_name}*\n\n"
                f"📐 How many units did you produce?\n\n"
                f"_Type a number (e.g. 200, 50, 0.5, 2.5)_\n\n"
                f"_Type *back* to pick a different product_"
            )]

        if step == "confirm_production":
            # Back to quantity input
            product_name = context.get("prod_product_name", "Product")
            context["prod_step"] = "enter_quantity"
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            return [text_response(
                f"🏭 *Producing: {product_name}*\n\n"
                f"📐 How many units did you produce?\n\n"
                f"_Type a number (e.g. 200, 50, 0.5, 2.5)_\n\n"
                f"_Type *back* to pick a different product_"
            )]

        # Recipe steps
        if step == "recipe_add_material":
            # Back to recipe product selection
            return self._start_recipe_setup(phone_number)

        if step == "recipe_material_qty":
            # Back to material name input
            context["prod_step"] = "recipe_add_material"
            if "current_material" in context:
                del context["current_material"]
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            return [text_response(
                "🧱 *Add material to recipe*\n\n"
                "What raw material is needed?\n\n"
                "_Type the material name or *back* to go back_"
            )]

        if step == "recipe_material_cost":
            # Back to quantity input
            material_name = context.get("current_material", "Material")
            context["prod_step"] = "recipe_material_qty"
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            return [text_response(
                f"🧱 *{material_name}*\n\n"
                f"How much *{material_name}* is needed to make *1 unit*?\n\n"
                f"Type: *quantity* then *unit*\n\n"
                f"_e.g. 500 ml, 2 kg, 1 piece, 0.5 hours_"
            )]

        # Default: exit to menu
        self.session.reset(phone_number)
        return [text_response("👍 Cancelled.")]

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle production-related buttons."""
        if button_id.startswith("prod_item_"):
            product_key = button_id[10:]
            return self._select_product(phone_number, product_key)

        if button_id == "prod_set_recipe":
            return self._start_recipe_setup(phone_number)

        if button_id.startswith("prod_recipe_") and button_id not in ("prod_recipe_done", "prod_recipe_add"):
            # User selected a product for recipe setup (e.g. prod_recipe_detergent)
            product_key = button_id[12:]
            session = self.session.get(phone_number)
            context = session.get("context", {})
            return self._recipe_pick_product(phone_number, button_id, context)

        if button_id == "prod_confirm_yes":
            session = self.session.get(phone_number)
            return self._execute_production(phone_number, session.get("context", {}))

        if button_id == "prod_confirm_no":
            self.session.reset(phone_number)
            return [text_response("👍 Production not recorded.")]

        if button_id == "prod_report_waste":
            # Switch to yield_check step so user can type waste amount
            session = self.session.get(phone_number)
            context = session.get("context", {})
            context["prod_step"] = "yield_check"
            quantity = float(context.get("prod_quantity", 0))
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            return [text_response(
                f"🗑️ *Report Waste*\n\n"
                f"Total produced: {int(quantity) if quantity == int(quantity) else quantity}\n\n"
                f"How many units were *wasted/damaged*?\n\n"
                f"_Type a number (e.g. 5, 10, 0.5)_\n"
                f"_Type *0* if no waste_"
            )]

        if button_id == "prod_recipe_done":
            self.session.reset(phone_number)
            return [text_response("✅ Recipe saved! You can now record production.")]

        if button_id == "prod_recalc_costs":
            return self._recalculate_all_costs(phone_number)

        if button_id == "prod_recipe_remove":
            session = self.session.get(phone_number)
            context = session.get("context", {})
            return self._recipe_show_remove_list(phone_number, context)

        if button_id.startswith("prod_rmmat_"):
            # Remove a specific material by index
            session = self.session.get(phone_number)
            context = session.get("context", {})
            mat_idx = button_id[11:]  # after "prod_rmmat_"
            return self._recipe_remove_material(phone_number, mat_idx, context)

        if button_id == "prod_mattype_material":
            session = self.session.get(phone_number)
            context = session.get("context", {})
            context["current_mat_type"] = "material"
            context["prod_step"] = "recipe_material_qty"
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            mat_name = context.get("current_material", "Material")
            return [text_response(
                f"🧱 *{mat_name}* (raw material)\n\n"
                f"How much *{mat_name}* is needed to make *1 unit*?\n\n"
                f"Type: *quantity* then *unit*\n\n"
                f"_e.g. 0.5 kg, 50 CL, 2 pieces, 500 ml_\n\n"
                f"_Type *back* to go back_"
            )]

        if button_id == "prod_mattype_overhead":
            session = self.session.get(phone_number)
            context = session.get("context", {})
            context["current_mat_type"] = "overhead"
            context["prod_step"] = "recipe_material_qty"
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            mat_name = context.get("current_material", "Material")
            return [text_response(
                f"⚡ *{mat_name}* (overhead rate)\n\n"
                f"How much *{mat_name}* is used per *1 unit* produced?\n\n"
                f"Type: *quantity* then *unit*\n\n"
                f"_e.g. 5 wh, 30 seconds, 0.5 hours, 2 minutes_\n\n"
                f"_Type *back* to go back_"
            )]

        if button_id == "prod_recipe_add":
            session = self.session.get(phone_number)
            context = session.get("context", {})
            context["prod_step"] = "recipe_add_material"
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            return [text_response(
                "🧱 *Add material to recipe*\n\n"
                "What raw material is needed?\n\n"
                "_Type the material name (e.g. Sulphonic Acid, Flour, Bottles)_"
            )]

        return self.start_production(phone_number)

    # ─────────────────────────────────────────────────────────
    # PRODUCTION FLOW STEPS
    # ─────────────────────────────────────────────────────────

    def _handle_pick_product(self, phone_number: str, text: str, context: dict) -> list:
        """Handle product selection from button or text."""
        if text.startswith("prod_item_"):
            return self._select_product(phone_number, text[10:])
        if text == "prod_set_recipe":
            return self._start_recipe_setup(phone_number)
        # Try to match text to a product
        return [text_response("👆 Please pick a product from the list above.")]

    def _select_product(self, phone_number: str, product_key: str) -> list:
        """Product selected — ask quantity to produce."""
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if product_key not in products:
            return [text_response("❓ Product not found.")]

        product = products[product_key]
        product_name = product.get("name", product_key)
        recipe = product.get("recipe", [])

        self.session.save(phone_number, states.PRODUCTION_RECORDING, {
            "prod_step": "enter_quantity",
            "prod_product_key": product_key,
            "prod_product_name": product_name,
        })

        recipe_str = ""
        if recipe:
            recipe_str = "\n\n📋 *Recipe per unit:*\n"
            for mat in recipe:
                mat_qty = float(mat.get('quantity', 0))
                mat_unit = mat.get('unit', '')
                mat_name = mat['material']
                # Show stock unit if different
                mat_key = mat_name.lower().replace(" ", "_")
                mat_product = products.get(mat_key, {})
                stock_unit = mat_product.get("primary_unit", "")
                unit_hint = ""
                if stock_unit and stock_unit.lower() != mat_unit.lower().rstrip("s"):
                    unit_hint = f" _(stock: {stock_unit})_"
                qty_display = int(mat_qty) if mat_qty == int(mat_qty) else mat_qty
                recipe_str += f"  • {qty_display} {mat_unit} {mat_name}{unit_hint}\n"

        return [text_response(
            f"🏭 *Producing: {product_name}*{recipe_str}\n\n"
            f"📐 How many units did you produce?\n\n"
            f"_Type a number (e.g. 200, 50, 0.5, 2.5)_\n\n"
            f"_Type *back* to pick a different product_"
        )]

    def _handle_quantity(self, phone_number: str, text: str, context: dict) -> list:
        """Handle quantity input — go straight to confirmation (no waste by default)."""
        qty_match = re.match(r'^([\d.]+)', text)
        if not qty_match:
            return [text_response("Please enter a number (e.g. 200, 0.5, 2.5) or type *back*:")]

        quantity = float(qty_match.group(1))
        if quantity <= 0:
            return [text_response("Please enter a quantity greater than 0:")]

        context["prod_quantity"] = quantity
        context["prod_good_qty"] = quantity  # Default: all good, no waste
        context["prod_waste"] = 0

        # Skip yield step — go straight to confirmation
        return self._show_production_confirmation(phone_number, context)

    def _handle_yield(self, phone_number: str, text: str, context: dict) -> list:
        """Handle yield/waste input (optional — only reached via 'Report Waste' button)."""
        quantity = float(context.get("prod_quantity", 0))
        text_low = text.lower().strip()

        if text_low in ("all", "same", "no waste", "none", "0"):
            context["prod_good_qty"] = quantity
            context["prod_waste"] = 0
            return self._show_production_confirmation(phone_number, context)

        qty_match = re.match(r'^([\d.]+)', text)
        if not qty_match:
            return [text_response(f"How many units were wasted/damaged? (max {quantity})\n\n_Type a number or *0* for no waste:_")]

        waste_qty = float(qty_match.group(1))
        if waste_qty >= quantity:
            return [text_response(f"Waste can't be more than total produced ({quantity}).\n\nType waste amount:")]
        if waste_qty < 0:
            waste_qty = 0

        good_qty = quantity - waste_qty
        context["prod_good_qty"] = good_qty
        context["prod_waste"] = waste_qty

        return self._show_production_confirmation(phone_number, context)

    def _show_production_confirmation(self, phone_number: str, context: dict) -> list:
        """Show production confirmation card with all details."""
        quantity = float(context.get("prod_quantity", 0))
        good_qty = float(context.get("prod_good_qty", quantity))
        waste = float(context.get("prod_waste", 0))
        product_key = context.get("prod_product_key", "")
        product_name = context.get("prod_product_name", "Product")

        # Generate sequential batch number (per user)
        user = self.db.get_user(phone_number)
        last_batch = int(user.get("last_batch_number", 0)) if user else 0
        new_batch = last_batch + 1
        batch_num = f"B{new_batch:04d}"
        context["prod_batch"] = batch_num

        # Get recipe to show material usage
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        product = catalog.get("products", {}).get(product_key, {})
        recipe = product.get("recipe", [])

        # Calculate materials needed and production cost
        materials_needed = []  # Physical materials (stock-tracked)
        overhead_costs = []    # Rate-based costs (no stock)
        total_material_cost = 0
        total_overhead_cost = 0
        conversion_warnings = []
        all_products = catalog.get("products", {})

        for mat in recipe:
            recipe_qty_per_unit = float(mat.get("quantity", 0))
            recipe_qty_total = recipe_qty_per_unit * quantity
            recipe_unit = mat.get("unit", "units")
            mat_key = mat["material"].lower().replace(" ", "_")
            mat_type = mat.get("type", "material")  # default to material for legacy recipes

            if mat_type == "overhead":
                # Overhead: simple rate × usage, no stock conversion
                rate = float(mat.get("rate", mat.get("cost_per_unit", 0)))
                mat_cost = rate * recipe_qty_total
                total_overhead_cost += mat_cost

                qty_display = f"{recipe_qty_total:.1f}" if recipe_qty_total != int(recipe_qty_total) else f"{int(recipe_qty_total)}"
                overhead_costs.append({
                    "material": mat["material"],
                    "quantity_total": recipe_qty_total,
                    "unit": recipe_unit,
                    "rate": rate,
                    "cost": mat_cost,
                    "display": f"{qty_display} {recipe_unit}",
                })
            else:
                # Raw material: convert to stock units, track for deduction
                mat_product = all_products.get(mat_key, {})
                conversion = self._convert_to_stock_unit(recipe_qty_total, recipe_unit, mat_product)
                stock_qty = conversion["stock_qty"]
                display_str = conversion["display"]

                if conversion.get("warning"):
                    conversion_warnings.append(conversion["warning"])

                # Cost: use cost_per_unit (per recipe unit) × recipe_qty_total
                cost_per = float(mat.get("cost_per_unit", 0))
                mat_cost = cost_per * recipe_qty_total
                total_material_cost += mat_cost

                materials_needed.append({
                    "material": mat["material"],
                    "quantity_needed": stock_qty,
                    "recipe_qty": recipe_qty_total,
                    "unit": conversion["stock_unit"] if conversion["converted"] else recipe_unit,
                    "recipe_unit": recipe_unit,
                    "converted": conversion["converted"],
                    "display": display_str,
                    "cost": mat_cost,
                    "cost_per_unit": cost_per,
                })

        total_cost = total_material_cost + total_overhead_cost
        cost_per_unit = total_cost / good_qty if good_qty > 0 else 0
        waste_pct = int(waste / quantity * 100) if quantity > 0 and waste > 0 else 0

        # Check stock sufficiency for materials only (not overhead)
        stock_warnings = []
        for mat in materials_needed:
            mat_key = mat["material"].lower().replace(" ", "_")
            mat_product = all_products.get(mat_key, {})
            current_stock = float(mat_product.get("stock", 0))
            needed = float(mat["quantity_needed"])
            if needed > current_stock:
                shortfall = needed - current_stock
                unit = mat.get("unit", "")
                stock_warnings.append(
                    f"  ⚠️ *{mat['material']}*: need {needed:.0f} {unit}, only {current_stock:.0f} in stock (short {shortfall:.0f})"
                )

        # Build confirmation
        # Format quantity display (show as int if whole number)
        qty_display = int(quantity) if quantity == int(quantity) else quantity

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🏭  *PRODUCTION*  _{batch_num}_",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"📦 *{product_name}* × {qty_display} produced",
        ]

        # Yield/waste info
        if waste > 0:
            lines.append(f"✅ Good: {good_qty}  |  🗑️ Waste: {waste} ({waste_pct}%)")
        else:
            lines.append(f"✅ All {qty_display} good")

        # ── Raw Materials section ──
        if materials_needed:
            lines.append(f"")
            lines.append(f"🧱 *Raw Materials:*")
            for mat in materials_needed:
                display = mat["display"]
                mat_cost = mat['cost']
                cost_str = ""
                if mat_cost > 0:
                    pct = int(mat_cost / total_cost * 100) if total_cost > 0 else 0
                    cost_str = f" (₦{mat_cost:,.2f} · {pct}%)"
                converted_note = " ↔" if mat.get("converted") else ""
                lines.append(f"  • {display} {mat['material']}{cost_str}{converted_note}")

        # ── Overhead Costs section ──
        if overhead_costs:
            lines.append(f"")
            lines.append(f"⚡ *Overhead Costs:*")
            for ov in overhead_costs:
                ov_cost = ov['cost']
                pct = int(ov_cost / total_cost * 100) if total_cost > 0 else 0
                rate_str = f"₦{ov['rate']:,.2f}/{ov['unit']}"
                lines.append(f"  • {ov['display']} {ov['material']} @ {rate_str} (₦{ov_cost:,.2f} · {pct}%)")

        # ── Totals ──
        if materials_needed or overhead_costs:
            lines.append(f"")
            if total_cost > 0:
                lines.append(f"💰 *Total cost: ₦{total_cost:,.2f}*")
                lines.append(f"💰 Cost/unit: ₦{cost_per_unit:,.2f}")
            else:
                lines.append(f"💰 Cost: ₦0 _(set costs via recipe or purchases)_")
            # Show conversion warnings
            if conversion_warnings:
                lines.append(f"")
                for warn in conversion_warnings:
                    lines.append(warn)
        else:
            lines.append(f"\n⚠️ _No recipe set — materials won't be deducted._")
            lines.append(f"_Set a recipe to enable auto-deduction._")

        # Show stock insufficiency warnings (materials only)
        if stock_warnings:
            lines.append(f"")
            lines.append(f"🚨 *Insufficient Stock:*")
            lines.extend(stock_warnings)
            lines.append(f"_Production will zero out these materials._")

        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")

        context["prod_step"] = "confirm_production"
        context["prod_materials_needed"] = materials_needed
        context["prod_overhead_costs"] = overhead_costs
        context["prod_total_cost"] = total_cost
        context["prod_cost_per_unit"] = cost_per_unit
        self.session.save(phone_number, states.PRODUCTION_RECORDING, context)

        # Buttons: Confirm, Report Waste (if no waste reported yet), Cancel
        buttons = [{"id": "prod_confirm_yes", "title": "✅ Confirm"}]
        if waste == 0:
            buttons.append({"id": "prod_report_waste", "title": "🗑️ Report Waste"})
        buttons.append({"id": "prod_confirm_no", "title": "❌ Cancel"})

        return [
            text_response("\n".join(lines)),
            button_response("Record this production?", buttons)
        ]

    def _handle_confirm(self, phone_number: str, text: str, context: dict) -> list:
        """Handle text confirmation."""
        if text.lower() in ("yes", "y", "confirm"):
            return self._execute_production(phone_number, context)
        self.session.reset(phone_number)
        return [text_response("👍 Production not recorded.")]

    def _execute_production(self, phone_number: str, context: dict) -> list:
        """Execute the production — deduct materials, add finished goods, save record."""
        product_key     = context.get("prod_product_key", "")
        product_name    = context.get("prod_product_name", "Product")
        quantity        = float(context.get("prod_quantity", 0))
        good_qty        = float(context.get("prod_good_qty", quantity))
        waste           = float(context.get("prod_waste", 0))
        batch_num       = context.get("prod_batch", "")
        materials_needed = context.get("prod_materials_needed", [])
        total_cost      = float(context.get("prod_total_cost", 0))
        cost_per_unit   = float(context.get("prod_cost_per_unit", 0))

        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        # 1. Deduct raw materials from stock (based on TOTAL quantity attempted, not good qty)
        deduction_results = []
        low_material_warnings = []
        for mat in materials_needed:
            mat_name = mat["material"]
            mat_qty = float(mat["quantity_needed"])
            mat_unit = mat.get("unit", "")
            mat_key = mat_name.lower().replace(" ", "_")
            if mat_key in products:
                current_stock = products[mat_key].get("stock", products[mat_key].get("stock_count", 0))
                new_stock = max(0, float(current_stock) - mat_qty)
                products[mat_key]["stock"] = new_stock
                qty_display = f"{mat_qty:.0f}" if mat_qty == int(mat_qty) else f"{mat_qty:.2f}"
                stock_display = f"{new_stock:.0f}" if new_stock == int(new_stock) else f"{new_stock:.1f}"
                unit_str = f" {mat_unit}" if mat_unit else ""
                deduction_results.append(f"  • {mat_name}: -{qty_display}{unit_str} (remaining: {stock_display})")
                # Check for low material
                if new_stock <= 5:
                    low_material_warnings.append(f"⚠️ *{mat_name}* is LOW — only {int(new_stock)} left!")

        # 2. Add GOOD finished goods to stock (waste not added)
        if product_key in products:
            current_stock = float(products[product_key].get("stock", products[product_key].get("stock_count", 0)))
            products[product_key]["stock"] = current_stock + good_qty

            # Update landing cost (production cost per unit — based on good units)
            if cost_per_unit > 0 and good_qty > 0:
                # Actual cost per good unit (accounts for waste)
                actual_cost_per_unit = total_cost / good_qty
                products[product_key]["landing_cost"] = int(actual_cost_per_unit)

        # 3. Save catalog
        catalog["products"] = products
        self.db.update_user_field(phone_number, "product_catalog", catalog)

        # 3b. Persist batch number counter
        # Extract numeric part from batch_num (e.g. "B0005" → 5)
        batch_number_int = int(batch_num[1:]) if batch_num.startswith("B") else 0
        self.db.update_user_field(phone_number, "last_batch_number", batch_number_int)

        # 4. Save production as a transaction record (type: "production")
        self.db.save_transaction(
            phone_number,
            int(total_cost) if total_cost > 0 else 0,
            "production",
            f"Batch {batch_num}: {quantity} × {product_name}" + (f" ({waste} waste)" if waste else ""),
            "Production & Manufacturing",
            sub_category="Production Run",
            quantity=str(good_qty),
            item_name=product_name,
            unit_cost=int(total_cost / good_qty) if good_qty > 0 and total_cost > 0 else None,
            extra_details={
                "batch_number": batch_num,
                "production_quantity": int(quantity),
                "good_quantity": int(good_qty),
                "waste": int(waste),
                "waste_percent": int(waste / quantity * 100) if quantity > 0 else 0,
                "product_key": product_key,
                "materials_used": materials_needed,
                "cost_per_unit": float(total_cost / good_qty) if good_qty > 0 else float(cost_per_unit),
            }
        )

        self.session.reset(phone_number)

        # Build result message
        actual_cost = total_cost / good_qty if good_qty > 0 and total_cost > 0 else 0
        good_display = int(good_qty) if good_qty == int(good_qty) else good_qty
        lines = [
            f"✅ *Production Recorded!*  _{batch_num}_",
            f"",
            f"📦 +{good_display} *{product_name}* added to stock",
        ]
        if waste > 0:
            waste_pct = int(waste / quantity * 100)
            lines.append(f"🗑️ Waste: {int(waste)} units ({waste_pct}%)")
            if actual_cost > 0:
                lines.append(f"💰 Actual cost/unit: ₦{actual_cost:,.2f} _(adjusted for waste)_")
        if deduction_results:
            lines.append(f"")
            lines.append(f"🧱 *Materials deducted:*")
            lines.extend(deduction_results)

        # Show overhead costs in summary
        overhead_costs = context.get("prod_overhead_costs", [])
        if overhead_costs:
            lines.append(f"")
            lines.append(f"⚡ *Overhead applied:*")
            for ov in overhead_costs:
                lines.append(f"  • {ov['material']}: ₦{ov['cost']:,.2f}")

        if total_cost > 0 and waste == 0:
            lines.append(f"")
            lines.append(f"💰 Cost per unit: ₦{cost_per_unit:,.2f}")
            lines.append(f"💰 Total batch cost: ₦{total_cost:,.2f}")

        lines.append("")

        # Add low material warnings
        if low_material_warnings:
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            lines.append("🚨 *Low Material Alert:*")
            lines.extend(low_material_warnings)
            lines.append("_Restock soon!_")

        return [
            text_response("\n".join(lines)),
            button_response(
                "What's next?",
                [
                    {"id": "record_production", "title": "🏭 Produce More"},
                    {"id": "record_purchase", "title": "🧱 Buy Materials"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ]
            )
        ]

    # ─────────────────────────────────────────────────────────
    # UNIT CONVERSION HELPERS
    # ─────────────────────────────────────────────────────────

    # Standard metric conversions (bidirectional)
    STANDARD_CONVERSIONS = {
        # Volume
        ("ml", "l"): 0.001, ("ml", "litre"): 0.001, ("ml", "litres"): 0.001, ("ml", "liter"): 0.001,
        ("l", "ml"): 1000, ("litre", "ml"): 1000, ("litres", "ml"): 1000, ("liter", "ml"): 1000,
        ("cl", "ml"): 10, ("ml", "cl"): 0.1,
        ("cl", "l"): 0.01, ("l", "cl"): 100, ("cl", "litre"): 0.01, ("litre", "cl"): 100,
        ("cl", "litres"): 0.01, ("litres", "cl"): 100,
        # Weight
        ("g", "kg"): 0.001, ("kg", "g"): 1000,
        ("mg", "g"): 0.001, ("g", "mg"): 1000,
        ("gram", "kg"): 0.001, ("kg", "gram"): 1000,
        ("grams", "kg"): 0.001, ("kg", "grams"): 1000,
        ("tonne", "kg"): 1000, ("kg", "tonne"): 0.001,
        ("tonnes", "kg"): 1000, ("kg", "tonnes"): 0.001,
        # Time
        ("min", "hour"): 1/60, ("hour", "min"): 60,
        ("minute", "hour"): 1/60, ("hour", "minute"): 60,
        ("minutes", "hours"): 1/60, ("hours", "minutes"): 60,
        ("hr", "min"): 60, ("min", "hr"): 1/60,
        ("hour", "day"): 1/24, ("day", "hour"): 24,
        # Quantity synonyms (treat as same)
        ("piece", "pieces"): 1, ("pieces", "piece"): 1,
        ("unit", "units"): 1, ("units", "unit"): 1,
        ("piece", "units"): 1, ("units", "piece"): 1,
        ("pieces", "units"): 1, ("units", "pieces"): 1,
        ("pc", "pieces"): 1, ("pieces", "pc"): 1,
        ("pcs", "pieces"): 1, ("pieces", "pcs"): 1,
        # Volume larger
        ("gallon", "litre"): 3.785, ("litre", "gallon"): 0.264,
        ("gallon", "litres"): 3.785, ("litres", "gallon"): 0.264,
        ("drum", "litre"): 200, ("litre", "drum"): 0.005,
        ("drum", "litres"): 200, ("litres", "drum"): 0.005,
        ("drum", "l"): 200, ("l", "drum"): 0.005,
        # Energy
        ("kwh", "whr"): 1, ("whr", "kwh"): 1,
        ("kw", "kwh"): 1, ("kwh", "kw"): 1,
        ("watt", "kw"): 0.001, ("kw", "watt"): 1000,
    }

    def _convert_to_stock_unit(self, recipe_qty: float, recipe_unit: str,
                                material_product: dict) -> dict:
        """
        Convert a recipe quantity to the material's stock unit.
        
        Args:
            recipe_qty: quantity needed in recipe units (e.g. 500)
            recipe_unit: unit from recipe (e.g. "ml")
            material_product: the material's catalog product dict
            
        Returns:
            {
                "stock_qty": float,      # quantity to deduct from stock
                "stock_unit": str,       # unit of stock
                "converted": bool,       # whether conversion was applied
                "display": str,          # human-readable (e.g. "0.5 litres")
                "warning": str or None,  # warning if units don't match and no conversion
            }
        """
        stock_unit = material_product.get("primary_unit", "").lower().strip()
        recipe_unit_lower = recipe_unit.lower().strip().rstrip("s")  # normalize plural
        stock_unit_normalized = stock_unit.rstrip("s")

        # If no primary_unit set on material, or units match — no conversion needed
        if not stock_unit or recipe_unit_lower == stock_unit_normalized:
            return {
                "stock_qty": recipe_qty,
                "stock_unit": recipe_unit,
                "converted": False,
                "display": f"{recipe_qty:.1f} {recipe_unit}" if recipe_qty != int(recipe_qty) else f"{int(recipe_qty)} {recipe_unit}",
                "warning": None,
            }

        # Try standard metric conversion
        for (from_u, to_u), factor in self.STANDARD_CONVERSIONS.items():
            if recipe_unit_lower == from_u.rstrip("s") and stock_unit_normalized == to_u.rstrip("s"):
                converted_qty = recipe_qty * factor
                display = f"{converted_qty:.2f} {stock_unit}" if converted_qty != int(converted_qty) else f"{int(converted_qty)} {stock_unit}"
                return {
                    "stock_qty": converted_qty,
                    "stock_unit": stock_unit,
                    "converted": True,
                    "display": display,
                    "warning": None,
                }

        # Try user-defined conversions on the material
        conversions = material_product.get("conversions", {})
        for conv_key, conv_val in conversions.items():
            # conv_key = "1 carton", conv_val = {"qty": 24, "unit": "pieces"}
            import re as _re
            key_match = _re.match(r'^(\d+)\s+(.+)', conv_key)
            if not key_match:
                continue
            conv_from_qty = float(key_match.group(1))
            conv_from_unit = key_match.group(2).strip().rstrip("s").lower()
            conv_to_qty = float(conv_val.get("qty", 1))
            conv_to_unit = conv_val.get("unit", "").rstrip("s").lower()

            # Check if recipe unit matches the "from" side
            if recipe_unit_lower == conv_from_unit:
                # Convert: recipe_qty [from_unit] → stock [to_unit]
                converted_qty = recipe_qty * (conv_to_qty / conv_from_qty)
                display = f"{converted_qty:.1f} {conv_val.get('unit', stock_unit)}" if converted_qty != int(converted_qty) else f"{int(converted_qty)} {conv_val.get('unit', stock_unit)}"
                return {
                    "stock_qty": converted_qty,
                    "stock_unit": conv_val.get("unit", stock_unit),
                    "converted": True,
                    "display": display,
                    "warning": None,
                }

            # Check reverse: recipe unit matches the "to" side
            if recipe_unit_lower == conv_to_unit:
                # Convert: recipe_qty [to_unit] → stock [from_unit]
                converted_qty = recipe_qty * (conv_from_qty / conv_to_qty)
                display = f"{converted_qty:.2f} {conv_key.split(' ', 1)[1]}" if converted_qty != int(converted_qty) else f"{int(converted_qty)} {conv_key.split(' ', 1)[1]}"
                return {
                    "stock_qty": converted_qty,
                    "stock_unit": conv_key.split(' ', 1)[1] if ' ' in conv_key else stock_unit,
                    "converted": True,
                    "display": display,
                    "warning": None,
                }

        # No conversion found — units don't match
        return {
            "stock_qty": recipe_qty,  # deduct as-is (best effort)
            "stock_unit": recipe_unit,
            "converted": False,
            "display": f"{recipe_qty:.1f} {recipe_unit}" if recipe_qty != int(recipe_qty) else f"{int(recipe_qty)} {recipe_unit}",
            "warning": f"⚠️ Unit mismatch: recipe uses *{recipe_unit}* but stock is in *{stock_unit}*. Set a conversion in Catalog → Set Conversion.",
        }

    # ─────────────────────────────────────────────────────────
    # RECIPE / BOM MANAGEMENT
    # ─────────────────────────────────────────────────────────

    def _start_recipe_setup(self, phone_number: str) -> list:
        """Start recipe/BOM setup — pick which product to set recipe for."""
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if not products:
            return [text_response(
                "📋 Add products to your catalog first, then set recipes."
            )]

        rows = []
        for key, data in list(products.items())[:10]:
            name = data.get("name", key)
            recipe = data.get("recipe", [])
            desc = f"Recipe: {len(recipe)} materials" if recipe else "No recipe yet"
            rows.append({
                "id": f"prod_recipe_{key}",
                "title": f"📦 {name}"[:24],
                "description": desc[:72],
            })

        self.session.save(phone_number, states.PRODUCTION_RECORDING, {
            "prod_step": "recipe_pick_product",
        })

        return [list_response(
            header="📋 Set Recipe",
            body="Which product do you want to set a recipe for?",
            button_text="Select Product",
            sections=[{"title": "Products", "rows": rows}]
        )]

    def _recipe_pick_product(self, phone_number: str, text: str, context: dict) -> list:
        """Handle product selection for recipe setup."""
        product_key = text.replace("prod_recipe_", "") if text.startswith("prod_recipe_") else text.lower().replace(" ", "_")

        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if product_key not in products:
            return [text_response("❓ Product not found. Pick from the list.")]

        product = products[product_key]
        product_name = product.get("name", product_key)
        recipe = product.get("recipe", [])

        context["prod_step"] = "recipe_add_material"
        context["recipe_product_key"] = product_key
        context["recipe_product_name"] = product_name
        self.session.save(phone_number, states.PRODUCTION_RECORDING, context)

        # Show current recipe if exists
        if recipe:
            lines = [
                f"📋 *Current recipe for {product_name}:*\n",
            ]
            for i, mat in enumerate(recipe):
                cost_str = f" @ ₦{int(mat.get('cost_per_unit', 0)):,}" if mat.get('cost_per_unit') else ""
                lines.append(f"  {i+1}. {mat['quantity']} {mat.get('unit', '')} {mat['material']}{cost_str}")
            lines.append(f"\n_Add, remove, or finish._")
            return [
                text_response("\n".join(lines)),
                button_response(
                    "What next?",
                    [
                        {"id": "prod_recipe_add", "title": "➕ Add Material"},
                        {"id": "prod_recipe_remove", "title": "🗑️ Remove"},
                        {"id": "prod_recipe_done", "title": "✅ Done"},
                    ]
                )
            ]
        else:
            return [text_response(
                f"📋 *Set recipe for: {product_name}*\n\n"
                f"What raw material is needed to make this?\n\n"
                f"Type *only the material name* (one at a time):\n\n"
                f"Examples:\n"
                f"  _Sulphonic Acid_\n"
                f"  _Flour_\n"
                f"  _Bottles_\n"
                f"  _HCL_\n\n"
                f"_I'll ask for the quantity next._\n"
                f"_Type *done* when you've added all materials._"
            )]

    def _recipe_add_material(self, phone_number: str, text: str, context: dict) -> list:
        """User typed a material name — ask if it's a raw material or overhead rate."""
        if text.lower() == "done":
            self.session.reset(phone_number)
            return [text_response("✅ Recipe saved! You can now record production.")]

        material_name = text.strip().title()
        if len(material_name) < 2:
            return [text_response("Please type the material/cost name (at least 2 characters):")]

        context["current_material"] = material_name

        # Check if this material already exists in catalog — auto-detect type
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})
        mat_key = material_name.lower().replace(" ", "_")
        existing_product = products.get(mat_key, {})

        # If already tagged as overhead, skip the type question
        if existing_product.get("item_type") == "overhead":
            context["current_mat_type"] = "overhead"
            context["prod_step"] = "recipe_material_qty"
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            return [text_response(
                f"⚡ *{material_name}* (overhead rate)\n\n"
                f"How much *{material_name}* is used to make *1 unit* of {context.get('recipe_product_name', 'product')}?\n\n"
                f"Type: *quantity* then *unit*\n\n"
                f"_e.g. 5 wh, 30 seconds, 0.5 hours, 2 minutes_"
            )]

        # If already tagged as raw_material, skip the type question
        if existing_product.get("item_type") == "raw_material":
            context["current_mat_type"] = "material"
            context["prod_step"] = "recipe_material_qty"
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            punit = existing_product.get("primary_unit", "")
            unit_hint = f" _({punit})_" if punit else ""
            return [text_response(
                f"🧱 *{material_name}* (raw material)\n\n"
                f"How much *{material_name}* is needed to make *1 unit*?{unit_hint}\n\n"
                f"Type: *quantity* then *unit*\n\n"
                f"_e.g. 0.5 kg, 50 CL, 2 pieces, 500 ml_\n\n"
                f"_Type *back* to change the name_"
            )]

        # Unknown — ask user to classify
        context["prod_step"] = "recipe_ask_type"
        self.session.save(phone_number, states.PRODUCTION_RECORDING, context)

        return [button_response(
            f"🧱 *{material_name}*\n\n"
            f"Is this a *raw material* you physically buy,\n"
            f"or an *overhead cost* (electricity, labour, machine time)?",
            [
                {"id": "prod_mattype_material", "title": "🧱 Raw Material"},
                {"id": "prod_mattype_overhead", "title": "⚡ Overhead Rate"},
            ]
        )]

    def _recipe_material_qty(self, phone_number: str, text: str, context: dict) -> list:
        """User typed quantity — now ask for cost/rate."""
        # Parse quantity and unit
        match = re.match(r'^([\d.]+)\s*(.*)', text.strip())
        if not match:
            return [text_response("Please enter quantity + unit (e.g. 500 ml, 2 kg, 5 wh, 30 seconds) or type *back*:")]

        qty = float(match.group(1))
        unit = match.group(2).strip() or "units"
        mat_type = context.get("current_mat_type", "material")
        material_name = context.get("current_material", "Material")

        # Save qty and unit to context, ask for cost/rate
        context["current_qty"] = qty
        context["current_unit"] = unit
        context["prod_step"] = "recipe_material_cost"
        self.session.save(phone_number, states.PRODUCTION_RECORDING, context)

        if mat_type == "overhead":
            return [text_response(
                f"⚡ *{qty} {unit} {material_name}* per unit\n\n"
                f"💰 What is the *rate* for {material_name}?\n"
                f"(Cost per 1 {unit})\n\n"
                f"_e.g. 60 (₦60 per {unit}), 0.5, 1500_\n"
                f"_Decimals allowed: 0.06, 1.5, 0.001_\n\n"
                f"_Type *back* to change quantity_"
            )]
        else:
            return [text_response(
                f"🧱 *{qty} {unit} {material_name}* per unit\n\n"
                f"💰 What does *1 {unit}* of {material_name} cost?\n\n"
                f"_e.g. 500, 2K, 10000, 0.5_\n\n"
                f"_Type *skip* if you don't know (will use purchase price)_\n"
                f"_Type *back* to change the quantity_"
            )]

    def _recipe_material_cost(self, phone_number: str, text: str, context: dict) -> list:
        """User typed cost/rate (or skip) — save to recipe."""
        material_name = context.get("current_material", "Material")
        qty = context.get("current_qty", 1)
        unit = context.get("current_unit", "units")
        mat_type = context.get("current_mat_type", "material")
        product_key = context.get("recipe_product_key", "")
        product_name = context.get("recipe_product_name", "Product")

        # Parse cost/rate — support decimals
        cost_value = 0.0
        if text.lower().strip() not in ("skip", "no", "0", "none"):
            # Try decimal parsing first
            decimal_match = re.match(r'^[\d.]+$', text.strip())
            if decimal_match:
                cost_value = float(text.strip())
            else:
                from utils.parser import parse_amount
                cost = parse_amount(text)
                if cost:
                    cost_value = float(cost)
                else:
                    label = "rate" if mat_type == "overhead" else "cost"
                    return [text_response(
                        f"💰 Enter {label} per {unit} (e.g. 500, 0.06, 2K) or type *skip*:"
                    )]

        # Now save to recipe
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if product_key in products:
            recipe = products[product_key].setdefault("recipe", [])

            # Build recipe entry based on type
            recipe_entry = {
                "material": material_name,
                "quantity": qty,
                "unit": unit,
                "type": mat_type,  # "material" or "overhead"
            }
            if mat_type == "overhead":
                recipe_entry["rate"] = cost_value
            else:
                recipe_entry["cost_per_unit"] = cost_value

            # Check if material already exists in recipe — update it
            found = False
            for i, existing in enumerate(recipe):
                if existing["material"].lower() == material_name.lower():
                    recipe[i] = recipe_entry
                    found = True
                    break

            if not found:
                recipe.append(recipe_entry)

            # For raw materials: auto-fill cost from landing_cost if skipped
            mat_key = material_name.lower().replace(" ", "_")
            if mat_type == "material" and cost_value == 0 and mat_key in products:
                mat_cost = float(products[mat_key].get("landing_cost", 0))
                if mat_cost:
                    for mat in recipe:
                        if mat["material"].lower() == material_name.lower():
                            mat["cost_per_unit"] = mat_cost
                            cost_value = mat_cost

            # Auto-create in catalog if not exists
            if mat_key not in products:
                if mat_type == "overhead":
                    products[mat_key] = {
                        "name": material_name,
                        "stock": 0,
                        "landing_cost": cost_value,
                        "item_type": "overhead",
                        "category": "",
                        "variants": [],
                        "recipe": [],
                        "conversions": {},
                    }
                else:
                    products[mat_key] = {
                        "name": material_name,
                        "stock": 0,
                        "landing_cost": cost_value,
                        "item_type": "raw_material",
                        "category": "",
                        "variants": [],
                        "recipe": [],
                        "conversions": {},
                    }
            elif mat_type == "overhead":
                # Tag existing item as overhead
                products[mat_key]["item_type"] = "overhead"
                if cost_value > 0:
                    products[mat_key]["landing_cost"] = cost_value
            elif cost_value > 0 and not products[mat_key].get("landing_cost"):
                products[mat_key]["landing_cost"] = cost_value

            # Tag the finished product
            products[product_key]["item_type"] = "finished_product"

            catalog["products"] = products
            self.db.update_user_field(phone_number, "product_catalog", catalog)

        # Build success message
        if mat_type == "overhead":
            cost_str = f" @ ₦{cost_value:,.2f}/{unit}" if cost_value else ""
            emoji = "⚡"
        else:
            cost_str = f" @ ₦{cost_value:,.2f}/{unit}" if cost_value else ""
            emoji = "🧱"

        # Ask for next material
        context["prod_step"] = "recipe_add_material"
        for key in ("current_material", "current_qty", "current_unit", "current_mat_type"):
            context.pop(key, None)
        self.session.save(phone_number, states.PRODUCTION_RECORDING, context)

        return [
            text_response(
                f"✅ Added: {emoji} *{qty} {unit} {material_name}*{cost_str} per unit of {product_name}\n\n"
                f"Add another material/cost or type *done* to finish."
            ),
            button_response(
                "More?",
                [
                    {"id": "prod_recipe_add", "title": "➕ Add More"},
                    {"id": "prod_recipe_done", "title": "✅ Done"},
                ]
            )
        ]

    # ─────────────────────────────────────────────────────────
    # RECIPE MATERIAL REMOVAL
    # ─────────────────────────────────────────────────────────

    def _recipe_show_remove_list(self, phone_number: str, context: dict) -> list:
        """Show list of recipe materials to remove."""
        product_key = context.get("recipe_product_key", "")
        product_name = context.get("recipe_product_name", "Product")

        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})
        product = products.get(product_key, {})
        recipe = product.get("recipe", [])

        if not recipe:
            return [text_response("📋 Recipe is already empty.")]

        rows = []
        for i, mat in enumerate(recipe[:9]):
            name = mat.get("material", "Material")
            qty = mat.get("quantity", 0)
            unit = mat.get("unit", "")
            rows.append({
                "id": f"prod_rmmat_{i}",
                "title": f"🗑️ {name}"[:24],
                "description": f"{qty} {unit} per unit"[:72],
            })

        return [list_response(
            header="🗑️ Remove Material",
            body=f"Which material to remove from *{product_name}*?",
            button_text="Select",
            sections=[{"title": "Recipe Materials", "rows": rows}]
        )]

    def _recipe_remove_material(self, phone_number: str, mat_idx_str: str, context: dict) -> list:
        """Remove a material from the recipe by index."""
        product_key = context.get("recipe_product_key", "")
        product_name = context.get("recipe_product_name", "Product")

        try:
            mat_idx = int(mat_idx_str)
        except ValueError:
            return [text_response("❌ Invalid selection.")]

        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if product_key not in products:
            return [text_response("❓ Product not found.")]

        product = products[product_key]
        recipe = product.get("recipe", [])

        if mat_idx < 0 or mat_idx >= len(recipe):
            return [text_response("❌ Invalid material index.")]

        removed = recipe.pop(mat_idx)
        removed_name = removed.get("material", "Material")
        product["recipe"] = recipe
        self.db.update_user_field(phone_number, "product_catalog", catalog)

        # Show updated recipe
        if recipe:
            lines = [f"✅ Removed *{removed_name}* from recipe.\n"]
            lines.append(f"📋 *Updated recipe for {product_name}:*\n")
            for i, mat in enumerate(recipe):
                cost_str = f" @ ₦{int(mat.get('cost_per_unit', 0)):,}" if mat.get('cost_per_unit') else ""
                lines.append(f"  {i+1}. {mat['quantity']} {mat.get('unit', '')} {mat['material']}{cost_str}")

            return [
                text_response("\n".join(lines)),
                button_response(
                    "What next?",
                    [
                        {"id": "prod_recipe_add", "title": "➕ Add Material"},
                        {"id": "prod_recipe_remove", "title": "🗑️ Remove More"},
                        {"id": "prod_recipe_done", "title": "✅ Done"},
                    ]
                )
            ]
        else:
            return [
                text_response(f"✅ Removed *{removed_name}*. Recipe is now empty."),
                button_response(
                    "Add materials?",
                    [
                        {"id": "prod_recipe_add", "title": "➕ Add Material"},
                        {"id": "prod_recipe_done", "title": "✅ Done"},
                    ]
                )
            ]

    # ─────────────────────────────────────────────────────────
    # RECALCULATE ALL RECIPE COSTS
    # ─────────────────────────────────────────────────────────

    def _recalculate_all_costs(self, phone_number: str) -> list:
        """
        Re-sync all recipe cost_per_unit values from current material landing_costs.
        Useful after bulk purchases or manual cost edits.
        """
        user = self.db.get_user(phone_number)
        if not user:
            return [text_response("❌ User not found.")]

        catalog = user.get("product_catalog", {})
        products = catalog.get("products", {})

        if not products:
            return [text_response("📋 No products in catalog yet.")]

        updated_count = 0
        updated_materials = []

        for key, product in products.items():
            recipe = product.get("recipe", [])
            if not recipe:
                continue

            for mat in recipe:
                mat_name = mat.get("material", "")
                mat_key = mat_name.lower().replace(" ", "_")
                recipe_unit = mat.get("unit", "").lower().strip()

                # Look up current landing_cost from the material's catalog entry
                mat_product = products.get(mat_key, {})
                landing_cost = float(mat_product.get("landing_cost", 0))

                if landing_cost <= 0:
                    continue  # No cost data — skip

                primary_unit = mat_product.get("primary_unit", "").lower().strip()

                # Convert landing_cost (per primary_unit) to cost per recipe_unit
                new_cost = landing_cost
                if primary_unit and recipe_unit and recipe_unit.rstrip("s") != primary_unit.rstrip("s"):
                    # Need conversion: landing_cost is per primary_unit, recipe needs per recipe_unit
                    factor = None
                    for (from_u, to_u), f in self.STANDARD_CONVERSIONS.items():
                        if primary_unit.rstrip("s") == from_u.rstrip("s") and recipe_unit.rstrip("s") == to_u.rstrip("s"):
                            factor = f
                            break
                    if factor and factor > 0:
                        # e.g. landing_cost=₦100/litre, recipe uses CL, factor=100
                        # cost per CL = ₦100 / 100 = ₦1
                        new_cost = landing_cost / factor

                old_cost = float(mat.get("cost_per_unit", 0))
                if new_cost != old_cost and new_cost > 0:
                    mat["cost_per_unit"] = new_cost
                    updated_count += 1
                    if mat_name not in updated_materials:
                        updated_materials.append(mat_name)

        if updated_count == 0:
            return [text_response(
                "✅ *All recipe costs are up to date!*\n\n"
                "_No changes needed. Costs already match current material prices._"
            )]

        # Save updated catalog
        self.db.update_user_field(phone_number, "product_catalog", catalog)

        # Build response
        lines = [
            "✅ *Recipe costs recalculated!*",
            "",
            f"📊 Updated *{updated_count}* cost entries:",
        ]
        for mat_name in updated_materials[:8]:
            mat_key = mat_name.lower().replace(" ", "_")
            mat_product = products.get(mat_key, {})
            cost = int(mat_product.get("landing_cost", 0))
            unit = mat_product.get("primary_unit", "unit")
            lines.append(f"  • {mat_name}: ₦{cost:,}/{unit}")

        if len(updated_materials) > 8:
            lines.append(f"  _+{len(updated_materials) - 8} more..._")

        lines.append("")
        lines.append("_Production costs will now use these updated prices._")

        return [
            text_response("\n".join(lines)),
            button_response(
                "What's next?",
                [
                    {"id": "record_production", "title": "🏭 Produce"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ]
            )
        ]
