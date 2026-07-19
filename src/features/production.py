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
                "Go to: *Business → Products & Materials → ➕ Add Product*\n"
                "Add your finished products and raw materials."
            )]

        # Show finished products (those with recipes or that aren't raw materials)
        rows = []
        for key, data in list(products.items())[:9]:
            name = data.get("name", key)
            recipe = data.get("recipe", [])
            stock = data.get("stock_count", 0)
            tree = data.get("tree", {})
            if tree:
                from features.catalog import CatalogHandler
                stock = 0
                # Simple inline count
                def _sum(node):
                    if isinstance(node, (int, float)):
                        return max(0, int(node))
                    if isinstance(node, dict):
                        return sum(_sum(v) for v in node.values() if not str(v).startswith("__"))
                    return 0
                stock = _sum(tree)

            recipe_str = f"Recipe: {len(recipe)} materials" if recipe else "No recipe set"
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
            sections=[{"title": "Your Products", "rows": rows}]
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

        if text_low in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        if step == "pick_product":
            return self._handle_pick_product(phone_number, text_s, context)

        if step == "enter_quantity":
            return self._handle_quantity(phone_number, text_s, context)

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

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle production-related buttons."""
        if button_id.startswith("prod_item_"):
            product_key = button_id[10:]
            return self._select_product(phone_number, product_key)

        if button_id == "prod_set_recipe":
            return self._start_recipe_setup(phone_number)

        if button_id == "prod_confirm_yes":
            session = self.session.get(phone_number)
            return self._execute_production(phone_number, session.get("context", {}))

        if button_id == "prod_confirm_no":
            self.session.reset(phone_number)
            return [text_response("👍 Production not recorded.")]

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
            f"_Type a number (e.g. 200, 50, 1000)_"
        )]

    def _handle_quantity(self, phone_number: str, text: str, context: dict) -> list:
        """Handle quantity input — show confirmation with material usage."""
        qty_match = re.match(r'^(\d+)', text)
        if not qty_match:
            return [text_response("Please enter a number (e.g. 200):")]

        quantity = int(qty_match.group(1))
        if quantity <= 0:
            return [text_response("Please enter a quantity greater than 0:")]

        product_key = context.get("prod_product_key", "")
        product_name = context.get("prod_product_name", "Product")

        # Get recipe to show material usage
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        product = catalog.get("products", {}).get(product_key, {})
        recipe = product.get("recipe", [])

        # Calculate materials needed and production cost
        materials_needed = []
        total_cost = 0
        for mat in recipe:
            mat_qty = float(mat.get("quantity", 0)) * quantity
            mat_cost = float(mat.get("cost_per_unit", 0)) * mat_qty
            total_cost += mat_cost
            materials_needed.append({
                "material": mat["material"],
                "quantity_needed": mat_qty,
                "unit": mat.get("unit", ""),
                "cost": mat_cost,
            })

        cost_per_unit = total_cost / quantity if quantity > 0 else 0

        # Build confirmation
        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🏭  *PRODUCTION*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"📦 *{product_name}* × {quantity}",
        ]

        if materials_needed:
            lines.append(f"")
            lines.append(f"🧱 *Materials to use:*")
            for mat in materials_needed:
                qty_str = f"{mat['quantity_needed']:.0f}" if mat['quantity_needed'] == int(mat['quantity_needed']) else f"{mat['quantity_needed']:.1f}"
                cost_str = f" (₦{int(mat['cost']):,})" if mat['cost'] > 0 else ""
                lines.append(f"  • {qty_str} {mat['unit']} {mat['material']}{cost_str}")
            lines.append(f"")
            if total_cost > 0:
                lines.append(f"💰 Total production cost: {format_amount(total_cost)}")
                lines.append(f"💰 Cost per unit: {format_amount(cost_per_unit)}")
        else:
            lines.append(f"\n⚠️ _No recipe set — materials won't be deducted._")
            lines.append(f"_Set a recipe from the menu to enable auto-deduction._")

        lines.append(f"")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"_Record this production?_")

        context["prod_step"] = "confirm_production"
        context["prod_quantity"] = quantity
        context["prod_materials_needed"] = materials_needed
        context["prod_total_cost"] = total_cost
        context["prod_cost_per_unit"] = cost_per_unit
        self.session.save(phone_number, states.PRODUCTION_RECORDING, context)

        return [
            text_response("\n".join(lines)),
            button_response(
                "Confirm production?",
                [
                    {"id": "prod_confirm_yes", "title": "✅ Yes, Record"},
                    {"id": "prod_confirm_no", "title": "❌ Cancel"},
                ]
            )
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
        materials_needed = context.get("prod_materials_needed", [])
        total_cost      = context.get("prod_total_cost", 0)
        cost_per_unit   = context.get("prod_cost_per_unit", 0)

        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        # 1. Deduct raw materials from stock
        deduction_results = []
        for mat in materials_needed:
            mat_name = mat["material"]
            mat_qty = mat["quantity_needed"]
            # Find material in catalog
            mat_key = mat_name.lower().replace(" ", "_")
            if mat_key in products:
                current_stock = products[mat_key].get("stock_count", 0)
                new_stock = max(0, current_stock - mat_qty)
                products[mat_key]["stock_count"] = new_stock
                deduction_results.append(f"  • {mat_name}: -{mat_qty:.0f} (remaining: {new_stock:.0f})")

        # 2. Add finished goods to stock
        if product_key in products:
            current_stock = products[product_key].get("stock_count", 0)
            products[product_key]["stock_count"] = current_stock + quantity

            # Update landing cost (production cost per unit)
            if cost_per_unit > 0:
                products[product_key]["landing_cost"] = int(cost_per_unit)

        # 3. Save catalog
        catalog["products"] = products
        self.db.update_user_field(phone_number, "product_catalog", catalog)

        # 4. Save production as a transaction record (type: "production")
        self.db.save_transaction(
            phone_number,
            int(total_cost) if total_cost > 0 else 0,
            "production",
            f"Produced {quantity} × {product_name}",
            "Production & Manufacturing",
            sub_category="Production Run",
            quantity=str(quantity),
            item_name=product_name,
            unit_cost=int(cost_per_unit) if cost_per_unit > 0 else None,
            extra_details={
                "production_quantity": quantity,
                "product_key": product_key,
                "materials_used": materials_needed,
                "cost_per_unit": cost_per_unit,
            }
        )

        self.session.reset(phone_number)

        # Build result message
        lines = [
            f"✅ *Production Recorded!*",
            f"",
            f"📦 +{quantity} *{product_name}* added to stock",
        ]
        if deduction_results:
            lines.append(f"")
            lines.append(f"🧱 *Materials deducted:*")
            lines.extend(deduction_results)
        if cost_per_unit > 0:
            lines.append(f"")
            lines.append(f"💰 Cost per unit: {format_amount(cost_per_unit)}")
            lines.append(f"💰 Total batch cost: {format_amount(total_cost)}")

        lines.append(f"\n_Send next transaction or tap ☰ Menu._")

        return [text_response("\n".join(lines))]

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
                f"What's the first raw material needed?\n\n"
                f"_Type the material name (e.g. Sulphonic Acid, Flour, Bottles)_\n\n"
                f"_Type *done* when finished._"
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
            f"How much is needed to produce *1 unit* of {context.get('recipe_product_name', 'product')}?\n\n"
            f"_Type: quantity unit (e.g. 500ml, 2kg, 1 bottle, 0.5 litres)_"
        )]

    def _recipe_material_qty(self, phone_number: str, text: str, context: dict) -> list:
        """User typed quantity — ask for cost, then save to recipe."""
        # Parse quantity and unit
        match = re.match(r'^([\d.]+)\s*(.*)', text.strip())
        if not match:
            return [text_response("Please enter quantity + unit (e.g. 500ml, 2kg, 1 bottle):")]

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
