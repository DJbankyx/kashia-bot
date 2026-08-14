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

        if step == "recipe_material_qty":
            return self._recipe_material_qty(phone_number, text_s, context)

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
            quantity = context.get("prod_quantity", 0)
            self.session.save(phone_number, states.PRODUCTION_RECORDING, context)
            return [text_response(
                f"🗑️ *Report Waste*\n\n"
                f"Total produced: {quantity}\n\n"
                f"How many units were *wasted/damaged*?\n\n"
                f"_Type a number (e.g. 5, 10, 0.5)_\n"
                f"_Type *0* if no waste_"
            )]

        if button_id == "prod_recipe_done":
            self.session.reset(phone_number)
            return [text_response("✅ Recipe saved! You can now record production.")]

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
                recipe_str += f"  • {mat['quantity']} {mat.get('unit', '')} {mat['material']}\n"

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
        quantity = context.get("prod_quantity", 0)
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
        quantity = context.get("prod_quantity", 0)
        good_qty = context.get("prod_good_qty", quantity)
        waste = context.get("prod_waste", 0)
        product_key = context.get("prod_product_key", "")
        product_name = context.get("prod_product_name", "Product")

        # Generate batch number
        import time
        batch_num = f"B{int(time.time()) % 100000:05d}"
        context["prod_batch"] = batch_num

        # Get recipe to show material usage
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        product = catalog.get("products", {}).get(product_key, {})
        recipe = product.get("recipe", [])

        # Calculate materials needed and production cost
        materials_needed = []
        total_cost = 0
        conversion_warnings = []
        for mat in recipe:
            recipe_qty_per_unit = float(mat.get("quantity", 0))
            recipe_qty_total = recipe_qty_per_unit * quantity
            recipe_unit = mat.get("unit", "units")
            mat_key = mat["material"].lower().replace(" ", "_")

            # Look up the material in catalog for unit conversion
            all_products = catalog.get("products", {})
            mat_product = all_products.get(mat_key, {})

            # Convert recipe units to stock units
            conversion = self._convert_to_stock_unit(recipe_qty_total, recipe_unit, mat_product)
            stock_qty = conversion["stock_qty"]
            display_str = conversion["display"]

            if conversion.get("warning"):
                conversion_warnings.append(conversion["warning"])

            # Cost calculation uses the stock qty (converted)
            mat_cost = float(mat.get("cost_per_unit", 0)) * recipe_qty_total
            total_cost += mat_cost

            materials_needed.append({
                "material": mat["material"],
                "quantity_needed": stock_qty,  # converted to stock units
                "recipe_qty": recipe_qty_total,  # original recipe quantity
                "unit": conversion["stock_unit"] if conversion["converted"] else recipe_unit,
                "recipe_unit": recipe_unit,
                "converted": conversion["converted"],
                "display": display_str,
                "cost": mat_cost,
            })

        cost_per_unit = total_cost / good_qty if good_qty > 0 else 0
        waste_pct = int(waste / quantity * 100) if quantity > 0 and waste > 0 else 0

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

        if materials_needed:
            lines.append(f"")
            lines.append(f"🧱 *Materials to use:*")
            for mat in materials_needed:
                display = mat["display"]
                cost_str = f" (₦{int(mat['cost']):,})" if mat['cost'] > 0 else ""
                converted_note = " ↔" if mat.get("converted") else ""
                lines.append(f"  • {display} {mat['material']}{cost_str}{converted_note}")
            lines.append(f"")
            if total_cost > 0:
                lines.append(f"💰 Total cost: {format_amount(total_cost)} _(auto-calculated from recipe)_")
                lines.append(f"💰 Cost/unit: {format_amount(cost_per_unit)}")
            else:
                lines.append(f"💰 Cost: ₦0 _(set material costs via Buy Raw Materials or Set Landing Cost)_")
            # Show conversion warnings
            if conversion_warnings:
                lines.append(f"")
                for warn in conversion_warnings:
                    lines.append(warn)
        else:
            lines.append(f"\n⚠️ _No recipe set — materials won't be deducted._")
            lines.append(f"_Set a recipe to enable auto-deduction._")

        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")

        context["prod_step"] = "confirm_production"
        context["prod_materials_needed"] = materials_needed
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
        quantity        = context.get("prod_quantity", 0)
        good_qty        = context.get("prod_good_qty", quantity)
        waste           = context.get("prod_waste", 0)
        batch_num       = context.get("prod_batch", "")
        materials_needed = context.get("prod_materials_needed", [])
        total_cost      = context.get("prod_total_cost", 0)
        cost_per_unit   = context.get("prod_cost_per_unit", 0)

        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        # 1. Deduct raw materials from stock (based on TOTAL quantity attempted, not good qty)
        deduction_results = []
        low_material_warnings = []
        for mat in materials_needed:
            mat_name = mat["material"]
            mat_qty = mat["quantity_needed"]
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
            current_stock = products[product_key].get("stock", products[product_key].get("stock_count", 0))
            products[product_key]["stock"] = current_stock + good_qty

            # Update landing cost (production cost per unit — based on good units)
            if cost_per_unit > 0 and good_qty > 0:
                # Actual cost per good unit (accounts for waste)
                actual_cost_per_unit = total_cost / good_qty
                products[product_key]["landing_cost"] = int(actual_cost_per_unit)

        # 3. Save catalog
        catalog["products"] = products
        self.db.update_user_field(phone_number, "product_catalog", catalog)

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
                "production_quantity": quantity,
                "good_quantity": good_qty,
                "waste": waste,
                "waste_percent": int(waste / quantity * 100) if quantity > 0 else 0,
                "product_key": product_key,
                "materials_used": materials_needed,
                "cost_per_unit": total_cost / good_qty if good_qty > 0 else cost_per_unit,
            }
        )

        self.session.reset(phone_number)

        # Build result message
        actual_cost = int(total_cost / good_qty) if good_qty > 0 and total_cost > 0 else 0
        lines = [
            f"✅ *Production Recorded!*  _{batch_num}_",
            f"",
            f"📦 +{good_qty} *{product_name}* added to stock",
        ]
        if waste > 0:
            waste_pct = int(waste / quantity * 100)
            lines.append(f"🗑️ Waste: {waste} units ({waste_pct}%)")
            if actual_cost > 0:
                lines.append(f"💰 Actual cost/unit: {format_amount(actual_cost)} _(adjusted for waste)_")
        if deduction_results:
            lines.append(f"")
            lines.append(f"🧱 *Materials deducted:*")
            lines.extend(deduction_results)
        if total_cost > 0 and waste == 0:
            lines.append(f"")
            lines.append(f"💰 Cost per unit: {format_amount(cost_per_unit)}")
            lines.append(f"💰 Total batch cost: {format_amount(total_cost)}")

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
            conv_from_qty = int(key_match.group(1))
            conv_from_unit = key_match.group(2).strip().rstrip("s").lower()
            conv_to_qty = conv_val.get("qty", 1)
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
            for mat in recipe:
                cost_str = f" @ ₦{int(mat.get('cost_per_unit', 0)):,}" if mat.get('cost_per_unit') else ""
                lines.append(f"  • {mat['quantity']} {mat.get('unit', '')} {mat['material']}{cost_str}")
            lines.append(f"\n_Add another material or type *done* to finish._")
            return [
                text_response("\n".join(lines)),
                button_response(
                    "Add more materials or finish?",
                    [
                        {"id": "prod_recipe_add", "title": "➕ Add Material"},
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
        """User typed a material name — ask for quantity needed per unit."""
        if text.lower() == "done":
            self.session.reset(phone_number)
            return [text_response("✅ Recipe saved! You can now record production.")]

        material_name = text.strip().title()
        if len(material_name) < 2:
            return [text_response("Please type the material name (at least 2 characters):")]

        context["prod_step"] = "recipe_material_qty"
        context["current_material"] = material_name
        self.session.save(phone_number, states.PRODUCTION_RECORDING, context)

        return [text_response(
            f"🧱 *{material_name}*\n\n"
            f"How much *{material_name}* is needed to make *1 unit* of {context.get('recipe_product_name', 'product')}?\n\n"
            f"Type: *quantity* then *unit*\n\n"
            f"Examples:\n"
            f"  _500 ml_\n"
            f"  _2 kg_\n"
            f"  _1 piece_\n"
            f"  _0.5 litres_\n"
            f"  _300 grams_\n\n"
            f"_Type *back* to change the material name_"
        )]

    def _recipe_material_qty(self, phone_number: str, text: str, context: dict) -> list:
        """User typed quantity — ask for cost, then save to recipe."""
        # Parse quantity and unit
        match = re.match(r'^([\d.]+)\s*(.*)', text.strip())
        if not match:
            return [text_response("Please enter quantity + unit (e.g. 500ml, 2kg, 1 bottle) or type *back*:")]

        qty = float(match.group(1))
        unit = match.group(2).strip() or "units"

        material_name = context.get("current_material", "Material")
        product_key = context.get("recipe_product_key", "")
        product_name = context.get("recipe_product_name", "Product")

        # Save material to recipe
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if product_key in products:
            recipe = products[product_key].setdefault("recipe", [])

            # Check if material already exists in recipe — update it
            found = False
            for existing in recipe:
                if existing["material"].lower() == material_name.lower():
                    existing["quantity"] = qty
                    existing["unit"] = unit
                    found = True
                    break

            if not found:
                recipe.append({
                    "material": material_name,
                    "quantity": qty,
                    "unit": unit,
                    "cost_per_unit": 0,  # Will be auto-filled from material purchases
                })

            # Try to auto-fill cost from the material's landing_cost in catalog
            mat_key = material_name.lower().replace(" ", "_")
            if mat_key in products:
                mat_cost = products[mat_key].get("landing_cost", 0)
                if mat_cost:
                    for mat in recipe:
                        if mat["material"].lower() == material_name.lower():
                            mat["cost_per_unit"] = float(mat_cost)
            else:
                # Auto-create this material as a raw_material in catalog
                products[mat_key] = {
                    "name": material_name,
                    "stock": 0,
                    "landing_cost": 0,
                    "item_type": "raw_material",
                    "category": "",
                    "variants": [],
                    "recipe": [],
                    "conversions": {},
                }

            # Also ensure the finished product is tagged
            products[product_key]["item_type"] = "finished_product"

            catalog["products"] = products
            self.db.update_user_field(phone_number, "product_catalog", catalog)

        # Ask for next material
        context["prod_step"] = "recipe_add_material"
        del context["current_material"]
        self.session.save(phone_number, states.PRODUCTION_RECORDING, context)

        return [
            text_response(
                f"✅ Added: *{qty} {unit} {material_name}* per unit of {product_name}\n\n"
                f"Add another material or type *done* to finish."
            ),
            button_response(
                "More materials?",
                [
                    {"id": "prod_recipe_add", "title": "➕ Add More"},
                    {"id": "prod_recipe_done", "title": "✅ Done"},
                ]
            )
        ]
