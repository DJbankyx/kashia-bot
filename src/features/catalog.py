# src/features/catalog.py
"""Product Catalog — ONE unified system.

Menu options:
- 📋 Browse Catalog — shows full product details (tree, brands, sizes, conversions)
- ➕ Add Products — add new products to catalog
- 🧩 Set Pattern — define the attribute structure (e.g. Brand → Size → Color)
- ✏️ Edit Catalog — remove items, rename, add to specific layers
- 🗑️ Reset Catalog — clear everything
"""

import logging
import re
import json
import traceback

from core import states
from utils.whatsapp_ui import text_response, button_response, list_response

logger = logging.getLogger(__name__)


class CatalogHandler:
    """Unified product catalog management."""

    def __init__(self, session_mgr, database, categorizer):
        self.session = session_mgr
        self.db = database
        self.categorizer = categorizer

    def show_menu(self, phone_number: str) -> list:
        """Show catalog action menu."""
        catalog = self._get_catalog(phone_number)
        product_count = len(catalog.get("products", {}))

        body = f"📋 *Your Catalog* ({product_count} product{'s' if product_count != 1 else ''})\n\nWhat would you like to do?"

        return [list_response(
            header="📋 Catalog",
            body=body,
            button_text="Select Action",
            sections=[{
                "title": "Catalog Actions",
                "rows": [
                    {"id": "cat_browse", "title": "📋 Browse Catalog", "description": "View products with full details"},
                    {"id": "cat_add", "title": "➕ Add Products", "description": "Add new products to your catalog"},
                    {"id": "cat_pattern", "title": "🧩 Set Pattern", "description": "Define structure (Brand → Size → Color)"},
                    {"id": "cat_edit", "title": "✏️ Edit Catalog", "description": "Remove, rename, or modify products"},
                    {"id": "cat_reset", "title": "🗑️ Reset Catalog", "description": "Clear everything and start over"},
                ]
            }]
        )]

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle catalog-related states."""
        state = session.get("state", "")
        context = session.get("context", {})

        if state == states.CATALOG_SETUP_PRODUCTS:
            return self._handle_setup_products(phone_number, text, context)

        if state == states.CATALOG_SETUP_DETAILS:
            return self._handle_setup_details(phone_number, text, context)

        if state == states.CATALOG_ORGANIZE:
            return self._handle_organize(phone_number, text, context)

        if state == states.CATALOG_ADD_DATA:
            return self._handle_edit_flow(phone_number, text, context)

        return self.show_menu(phone_number)

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle catalog buttons."""
        if button_id == "cat_browse":
            return self._browse(phone_number)

        if button_id in ("cat_add", "cat_setup_products"):
            return self._start_add_products(phone_number)

        if button_id in ("cat_pattern", "cat_organize"):
            return self._start_pattern(phone_number)

        if button_id == "cat_edit":
            return self._start_edit(phone_number)

        if button_id == "cat_reset":
            return self._confirm_reset(phone_number)

        if button_id == "cat_reset_yes":
            return self._reset_catalog(phone_number)

        if button_id == "cat_reset_no":
            self.session.reset(phone_number)
            return [text_response("👍 Catalog kept as-is.")]

        # Product-specific buttons
        if button_id.startswith("cat_prod_"):
            product_key = button_id[9:]
            return self._show_product_detail(phone_number, product_key)

        # Pattern attribute buttons
        if button_id.startswith("cat_orgattr_"):
            attr = button_id[12:]
            return self._handle_pattern_attr_pick(phone_number, attr)

        # Edit action buttons
        if button_id.startswith("cat_editact_"):
            action = button_id[12:]
            return self._handle_edit_action(phone_number, action)

        # Edit target buttons
        if button_id.startswith("cat_edittgt_"):
            target = button_id[12:]
            return self._handle_edit_target(phone_number, target)

        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # BROWSE — Full details view
    # ─────────────────────────────────────────────────────────

    def _browse(self, phone_number: str) -> list:
        """Show all products with FULL tree details."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response(
                "📋 Your catalog is empty!\n\n"
                "Tap *Add Products* to get started."
            )]

        lines = ["📋 *Your Catalog*\n"]

        for key, data in products.items():
            name = data.get("name", key)
            attrs = data.get("attributes", {})
            conversions = data.get("conversions", {})
            pattern = data.get("pattern", [])

            lines.append(f"━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"📦 *{name}*")

            # Show pattern if set
            if pattern:
                lines.append(f"  🧩 Pattern: {' → '.join(pattern)}")

            # Show all attributes
            for attr_name, values in attrs.items():
                if values:
                    attr_label = attr_name.replace("_", " ").capitalize()
                    values_str = ", ".join(str(v) for v in values[:8])
                    if len(values) > 8:
                        values_str += f" (+{len(values)-8} more)"
                    lines.append(f"  • {attr_label}: {values_str}")

            # Show conversions
            if conversions:
                lines.append(f"  📐 Conversions:")
                for conv_key, conv_val in list(conversions.items())[:5]:
                    lines.append(f"    {conv_key} = {conv_val} pcs")

            if not attrs and not conversions and not pattern:
                lines.append(f"  _No details set yet_")

        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"_{len(products)} product{'s' if len(products) != 1 else ''} total_")

        return [text_response("\n".join(lines))]

    # ─────────────────────────────────────────────────────────
    # ADD PRODUCTS — Add new items to catalog
    # ─────────────────────────────────────────────────────────

    def _start_add_products(self, phone_number: str) -> list:
        """Start adding products."""
        self.session.save(phone_number, states.CATALOG_SETUP_PRODUCTS, {})
        return [text_response(
            "➕ *Add Products*\n\n"
            "List your products (comma or newline separated):\n\n"
            "Example:\n_Airfreshener, Hand Wash, Floor Cleaner, Dish Wash_\n\n"
            "Or type *cancel* to go back."
        )]

    def _handle_setup_products(self, phone_number: str, text: str, context: dict) -> list:
        """Handle product list input."""
        if text.lower() in ("done", "cancel", "exit"):
            self.session.reset(phone_number)
            return [text_response("👍 Back to menu.")]

        # Parse product list
        raw_items = re.split(r'[,\n]+', text)
        raw_items = [item.strip() for item in raw_items if item.strip()]

        if not raw_items:
            return [text_response("Please list your products, separated by commas:\n\n_e.g. Shoes, Bags, Clothes_")]

        # Smart grouping — detect "500ml Airfreshener, 4L Airfreshener" patterns
        size_pattern = re.compile(r'^(\d+(?:\.\d+)?)\s*(ml|l|kg|g|cl|oz)\s+(.+)$', re.IGNORECASE)
        products = {}

        for item in raw_items:
            match = size_pattern.match(item)
            if match:
                size_val = match.group(1)
                size_unit = match.group(2).lower()
                product_name = match.group(3).strip()
                p_key = product_name.lower().replace(" ", "_")

                if p_key not in products:
                    products[p_key] = {"name": product_name, "sizes": []}
                products[p_key]["sizes"].append(f"{size_val}{size_unit}")
            else:
                p_key = item.lower().replace(" ", "_")
                if p_key not in products:
                    products[p_key] = {"name": item, "sizes": []}

        # Save to catalog
        catalog = self._get_catalog(phone_number)
        if "products" not in catalog:
            catalog["products"] = {}

        added = []
        already_exists = []
        for p_key, p_data in products.items():
            if p_key in catalog["products"]:
                already_exists.append(p_data["name"])
                # Still add sizes if new
                if p_data["sizes"]:
                    existing_sizes = catalog["products"][p_key].get("attributes", {}).get("sizes", [])
                    catalog["products"][p_key].setdefault("attributes", {})["sizes"] = list(dict.fromkeys(existing_sizes + p_data["sizes"]))
            else:
                catalog["products"][p_key] = {
                    "name": p_data["name"],
                    "attributes": {"sizes": p_data["sizes"]} if p_data["sizes"] else {},
                    "conversions": {},
                    "pattern": [],
                }
                added.append(p_data["name"])

        self._save_catalog(phone_number, catalog)
        self.session.reset(phone_number)

        # Build response
        lines = []
        if added:
            lines.append(f"✅ Added {len(added)} product{'s' if len(added) != 1 else ''}: {', '.join(added)}")
        if already_exists:
            lines.append(f"ℹ️ Already existed: {', '.join(already_exists)}")

        lines.append("\nWhat's next?")
        lines.append("• *Set Pattern* — define structure (Brand → Size)")
        lines.append("• *Browse* — see your catalog")

        return [text_response("\n".join(lines))]

    # ─────────────────────────────────────────────────────────
    # SET PATTERN — Define attribute structure
    # ─────────────────────────────────────────────────────────

    def _start_pattern(self, phone_number: str) -> list:
        """Show products to set pattern for."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response("📋 No products yet. Add products first!")]

        rows = []
        for key, data in list(products.items())[:10]:
            name = data.get("name", key)
            pattern = data.get("pattern", [])
            desc = f"Pattern: {' → '.join(pattern)}" if pattern else "No pattern set"
            rows.append({"id": f"cat_prod_{key}", "title": name, "description": desc[:72]})

        self.session.save(phone_number, states.CATALOG_ORGANIZE, {"org_step": "pick_product"})

        return [list_response(
            header="🧩 Set Pattern",
            body="Pick a product to define its structure:",
            button_text="Select Product",
            sections=[{"title": "Products", "rows": rows}]
        )]

    def _handle_organize(self, phone_number: str, text: str, context: dict) -> list:
        """Handle pattern setup flow."""
        if text.lower() in ("done", "cancel", "exit"):
            self.session.reset(phone_number)
            return [text_response("✅ Pattern saved!")]

        step = context.get("org_step", "")

        # Product picked (from button or text)
        if step == "pick_product" or text.startswith("cat_prod_"):
            product_key = text.replace("cat_prod_", "") if text.startswith("cat_prod_") else text.lower().replace(" ", "_")
            
            # Verify product exists
            catalog = self._get_catalog(phone_number)
            if product_key not in catalog.get("products", {}):
                # Try partial match
                for k in catalog.get("products", {}):
                    if product_key in k or k in product_key:
                        product_key = k
                        break
                else:
                    return [text_response("Product not found. Pick from the list.")]

            context["org_product"] = product_key
            context["org_step"] = "pick_attribute"
            self.session.save(phone_number, states.CATALOG_ORGANIZE, context)

            product_name = catalog["products"][product_key].get("name", product_key)
            current_pattern = catalog["products"][product_key].get("pattern", [])
            pattern_str = f"\nCurrent pattern: {' → '.join(current_pattern)}" if current_pattern else ""

            return [list_response(
                header=f"🧩 {product_name}",
                body=f"Add attribute levels to the pattern.{pattern_str}\n\nPick an attribute to add:",
                button_text="Select Attribute",
                sections=[{"title": "Attributes", "rows": [
                    {"id": "cat_orgattr_brand", "title": "🏷️ Brand/Type"},
                    {"id": "cat_orgattr_size", "title": "📐 Size/Volume"},
                    {"id": "cat_orgattr_color", "title": "🎨 Color"},
                    {"id": "cat_orgattr_variant", "title": "📦 Variant/Model"},
                    {"id": "cat_orgattr_material", "title": "🧵 Material"},
                    {"id": "cat_orgattr_scent", "title": "🌸 Scent/Flavor"},
                    {"id": "btn_done", "title": "✅ Done"},
                ]}]
            )]

        # Attribute picked
        if step == "pick_attribute" or text.startswith("cat_orgattr_"):
            attr = text.replace("cat_orgattr_", "") if text.startswith("cat_orgattr_") else text.lower()
            product_key = context.get("org_product", "")

            # Add to pattern
            catalog = self._get_catalog(phone_number)
            if product_key in catalog.get("products", {}):
                pattern = catalog["products"][product_key].setdefault("pattern", [])
                attr_label = attr.capitalize()
                if attr_label not in pattern:
                    pattern.append(attr_label)
                self._save_catalog(phone_number, catalog)

            context["org_step"] = "enter_values"
            context["org_attr"] = attr
            self.session.save(phone_number, states.CATALOG_ORGANIZE, context)

            return [text_response(
                f"✅ Added *{attr.capitalize()}* to pattern!\n\n"
                f"Now type the {attr} values (comma separated):\n\n"
                f"_e.g. Nike, Adidas, Puma_\n\n"
                f"Or type *skip* to add values later."
            )]

        # Values entered
        if step == "enter_values":
            attr = context.get("org_attr", "")
            product_key = context.get("org_product", "")

            if text.lower() != "skip":
                values = [v.strip() for v in text.split(",") if v.strip()]
                if values and product_key:
                    catalog = self._get_catalog(phone_number)
                    if product_key in catalog.get("products", {}):
                        attrs = catalog["products"][product_key].setdefault("attributes", {})
                        attr_key = f"{attr}s" if not attr.endswith("s") else attr
                        existing = attrs.get(attr_key, [])
                        attrs[attr_key] = list(dict.fromkeys(existing + values))
                        self._save_catalog(phone_number, catalog)

            # Back to pick next attribute
            context["org_step"] = "pick_attribute"
            self.session.save(phone_number, states.CATALOG_ORGANIZE, context)

            catalog = self._get_catalog(phone_number)
            current_pattern = catalog.get("products", {}).get(product_key, {}).get("pattern", [])

            return [button_response(
                f"✅ Done! Pattern so far: *{' → '.join(current_pattern)}*\n\n"
                f"Add another attribute level or tap Done.",
                [
                    {"id": "cat_orgattr_size", "title": "➕ Add More"},
                    {"id": "btn_done", "title": "✅ Done"},
                ]
            )]

        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    def _handle_pattern_attr_pick(self, phone_number: str, attr: str) -> list:
        """Handle pattern attribute button picks."""
        session = self.session.get(phone_number)
        context = session.get("context", {})
        context["org_step"] = "pick_attribute"
        # Route back to organize handler
        return self._handle_organize(phone_number, f"cat_orgattr_{attr}", context)

    # ─────────────────────────────────────────────────────────
    # EDIT CATALOG — Remove, rename, add to layers
    # ─────────────────────────────────────────────────────────

    def _start_edit(self, phone_number: str) -> list:
        """Show edit options."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response("📋 No products to edit. Add products first!")]

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {"edit_step": "pick_action"})

        return [list_response(
            header="✏️ Edit Catalog",
            body="What would you like to do?",
            button_text="Select Action",
            sections=[{
                "title": "Edit Actions",
                "rows": [
                    {"id": "cat_editact_remove_product", "title": "🗑️ Remove a Product", "description": "Delete a product entirely"},
                    {"id": "cat_editact_rename_product", "title": "✏️ Rename a Product", "description": "Change product name"},
                    {"id": "cat_editact_add_values", "title": "➕ Add to a Layer", "description": "Add brands, sizes, colors to a product"},
                    {"id": "cat_editact_remove_value", "title": "➖ Remove from a Layer", "description": "Remove a brand, size, or color"},
                    {"id": "cat_editact_add_conversion", "title": "📐 Add Conversion", "description": "e.g. 1 carton = 12 pieces"},
                ]
            }]
        )]

    def _handle_edit_action(self, phone_number: str, action: str) -> list:
        """Handle edit action selection."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        # Show product picker
        rows = []
        for key, data in list(products.items())[:10]:
            name = data.get("name", key)
            rows.append({"id": f"cat_edittgt_{key}", "title": name})

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "edit_step": "pick_target",
            "edit_action": action,
        })

        action_labels = {
            "remove_product": "Which product to remove?",
            "rename_product": "Which product to rename?",
            "add_values": "Which product to add values to?",
            "remove_value": "Which product to remove values from?",
            "add_conversion": "Which product to add conversion to?",
        }

        return [list_response(
            header="✏️ Edit",
            body=action_labels.get(action, "Pick a product:"),
            button_text="Select Product",
            sections=[{"title": "Products", "rows": rows}]
        )]

    def _handle_edit_target(self, phone_number: str, product_key: str) -> list:
        """Handle product picked for editing."""
        session = self.session.get(phone_number)
        context = session.get("context", {})
        action = context.get("edit_action", "")
        catalog = self._get_catalog(phone_number)

        if product_key not in catalog.get("products", {}):
            return [text_response("Product not found.")]

        product = catalog["products"][product_key]
        product_name = product.get("name", product_key)

        # REMOVE PRODUCT
        if action == "remove_product":
            del catalog["products"][product_key]
            self._save_catalog(phone_number, catalog)
            self.session.reset(phone_number)
            return [text_response(f"🗑️ *{product_name}* removed from catalog.")]

        # RENAME PRODUCT
        if action == "rename_product":
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "edit_step": "enter_new_name",
                "edit_action": action,
                "edit_product": product_key,
            })
            return [text_response(f"✏️ Enter new name for *{product_name}*:")]

        # ADD VALUES TO A LAYER
        if action == "add_values":
            attrs = product.get("attributes", {})
            if not attrs:
                self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                    "edit_step": "enter_layer_name",
                    "edit_action": action,
                    "edit_product": product_key,
                })
                return [text_response(
                    f"📦 *{product_name}* has no attributes yet.\n\n"
                    f"What layer do you want to add? (e.g. brands, sizes, colors)"
                )]

            # Show existing layers
            rows = []
            for attr_name, values in attrs.items():
                label = attr_name.replace("_", " ").capitalize()
                desc = ", ".join(str(v) for v in values[:3])
                if len(values) > 3:
                    desc += f" (+{len(values)-3})"
                rows.append({"id": f"cat_editlayer_{attr_name}", "title": label, "description": desc[:72]})

            # Add option for new layer
            rows.append({"id": "cat_editlayer___new__", "title": "➕ New Layer", "description": "Add a new attribute type"})

            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "edit_step": "pick_layer",
                "edit_action": action,
                "edit_product": product_key,
            })

            return [list_response(
                header=f"➕ {product_name}",
                body="Which layer to add values to?",
                button_text="Select Layer",
                sections=[{"title": "Layers", "rows": rows}]
            )]

        # REMOVE VALUE FROM LAYER
        if action == "remove_value":
            attrs = product.get("attributes", {})
            if not attrs:
                self.session.reset(phone_number)
                return [text_response(f"*{product_name}* has no attributes to remove from.")]

            rows = []
            for attr_name, values in attrs.items():
                label = attr_name.replace("_", " ").capitalize()
                desc = ", ".join(str(v) for v in values[:3])
                rows.append({"id": f"cat_editlayer_{attr_name}", "title": label, "description": desc[:72]})

            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "edit_step": "pick_layer_remove",
                "edit_action": action,
                "edit_product": product_key,
            })

            return [list_response(
                header=f"➖ {product_name}",
                body="Which layer to remove values from?",
                button_text="Select Layer",
                sections=[{"title": "Layers", "rows": rows}]
            )]

        # ADD CONVERSION
        if action == "add_conversion":
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "edit_step": "enter_conversion",
                "edit_action": action,
                "edit_product": product_key,
            })
            return [text_response(
                f"📐 *{product_name}* — Add conversion\n\n"
                f"Type the conversion:\n"
                f"_e.g. 1 carton = 12 pieces_\n"
                f"_e.g. 1 carton (500ml) = 24 pieces_\n\n"
                f"Or type *cancel*."
            )]

        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    def _handle_edit_flow(self, phone_number: str, text: str, context: dict) -> list:
        """Handle edit flow text input steps."""
        step = context.get("edit_step", "")
        action = context.get("edit_action", "")
        product_key = context.get("edit_product", "")

        if text.lower() in ("cancel", "exit", "done"):
            self.session.reset(phone_number)
            return [text_response("👍 Done editing.")]

        catalog = self._get_catalog(phone_number)

        # RENAME — enter new name
        if step == "enter_new_name":
            new_name = text.strip()
            if product_key in catalog.get("products", {}):
                catalog["products"][product_key]["name"] = new_name
                self._save_catalog(phone_number, catalog)
            self.session.reset(phone_number)
            return [text_response(f"✅ Renamed to *{new_name}*!")]

        # ADD VALUES — pick layer (from button)
        if step == "pick_layer" and text.startswith("cat_editlayer_"):
            layer = text.replace("cat_editlayer_", "")
            if layer == "__new__":
                context["edit_step"] = "enter_layer_name"
                self.session.save(phone_number, states.CATALOG_ADD_DATA, context)
                return [text_response("What's the new layer called? (e.g. brands, sizes, colors, scents)")]
            context["edit_layer"] = layer
            context["edit_step"] = "enter_values"
            self.session.save(phone_number, states.CATALOG_ADD_DATA, context)
            current = catalog.get("products", {}).get(product_key, {}).get("attributes", {}).get(layer, [])
            current_str = f"\nCurrent: {', '.join(str(v) for v in current)}" if current else ""
            return [text_response(f"Type values to add (comma separated):{current_str}\n\n_e.g. Nike, Adidas, Puma_")]

        # ADD VALUES — enter layer name (for new layer)
        if step == "enter_layer_name":
            layer = text.strip().lower().replace(" ", "_")
            context["edit_layer"] = layer
            context["edit_step"] = "enter_values"
            self.session.save(phone_number, states.CATALOG_ADD_DATA, context)
            return [text_response(f"Type the {text.strip()} values (comma separated):\n\n_e.g. Nike, Adidas, Puma_")]

        # ADD VALUES — enter actual values
        if step == "enter_values":
            layer = context.get("edit_layer", "")
            values = [v.strip() for v in text.split(",") if v.strip()]
            if values and product_key and layer:
                if product_key in catalog.get("products", {}):
                    attrs = catalog["products"][product_key].setdefault("attributes", {})
                    existing = attrs.get(layer, [])
                    attrs[layer] = list(dict.fromkeys(existing + values))
                    self._save_catalog(phone_number, catalog)
            self.session.reset(phone_number)
            return [text_response(f"✅ Added {len(values)} values to *{layer}*!")]

        # REMOVE VALUE — pick layer
        if step == "pick_layer_remove" and text.startswith("cat_editlayer_"):
            layer = text.replace("cat_editlayer_", "")
            context["edit_layer"] = layer
            context["edit_step"] = "enter_remove_value"
            self.session.save(phone_number, states.CATALOG_ADD_DATA, context)
            current = catalog.get("products", {}).get(product_key, {}).get("attributes", {}).get(layer, [])
            return [text_response(
                f"Current values: {', '.join(str(v) for v in current)}\n\n"
                f"Type which value(s) to remove (comma separated):"
            )]

        # REMOVE VALUE — enter values to remove
        if step == "enter_remove_value":
            layer = context.get("edit_layer", "")
            to_remove = [v.strip().lower() for v in text.split(",") if v.strip()]
            if to_remove and product_key and layer:
                if product_key in catalog.get("products", {}):
                    attrs = catalog["products"][product_key].get("attributes", {})
                    current = attrs.get(layer, [])
                    attrs[layer] = [v for v in current if v.lower() not in to_remove]
                    self._save_catalog(phone_number, catalog)
            self.session.reset(phone_number)
            return [text_response(f"✅ Removed from *{layer}*!")]

        # ADD CONVERSION
        if step == "enter_conversion":
            # Parse "1 carton = 12 pieces" or "1 carton (500ml) = 24"
            match = re.match(r'(\d+)\s*(.+?)\s*=\s*(\d+)\s*(.*)', text)
            if match:
                qty_from = int(match.group(1))
                unit_from = match.group(2).strip()
                qty_to = int(match.group(3))

                conv_key = f"{qty_from} {unit_from}"
                if product_key in catalog.get("products", {}):
                    catalog["products"][product_key].setdefault("conversions", {})[conv_key] = qty_to
                    self._save_catalog(phone_number, catalog)
                self.session.reset(phone_number)
                return [text_response(f"✅ Saved: {conv_key} = {qty_to} pieces")]
            else:
                return [text_response("Format: _1 carton = 12 pieces_\nTry again or type *cancel*:")]

        # Fallback
        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # AI-Powered Descriptions (from Add/Setup flow)
    # ─────────────────────────────────────────────────────────

    def _handle_setup_details(self, phone_number: str, text: str, context: dict) -> list:
        """Handle free-form descriptions — AI parses and applies changes."""
        if text.lower().strip() == "done":
            self.session.reset(phone_number)
            return self._browse(phone_number)

        if text.lower().strip() in ("cancel", "exit"):
            self.session.reset(phone_number)
            return [text_response("👍 Done! Your catalog is saved.")]

        # Split multi-line messages
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        results = []

        for line in lines:
            result = self._parse_and_apply(phone_number, line, context)
            results.append(result)

        self.session.save(phone_number, states.CATALOG_SETUP_DETAILS, context)
        combined = "\n".join(results)
        return [text_response(f"{combined}\n\n_Send more or type *done*._")]

    def _parse_and_apply(self, phone_number: str, text: str, context: dict) -> str:
        """Parse a single description line and apply to catalog."""
        try:
            catalog = self._get_catalog(phone_number)
            product_names = [p.get("name", k) for k, p in catalog.get("products", {}).items()]
            last_added = context.get("last_added_product", "")

            parsed = self._parse_catalog_description(text, product_names, last_added)

            if not parsed or parsed.get("action") == "unknown":
                return f"🤔 Didn't understand: _{text[:40]}_"

            action = parsed.get("action", "")
            targets = parsed.get("targets", [])
            data = parsed.get("data", {})

            applied = self._apply_update(phone_number, catalog, action, targets, data)
            self._save_catalog(phone_number, catalog)
            return f"✅ {applied}"

        except Exception as e:
            logger.error(f"Catalog parse error: {e}\n{traceback.format_exc()}")
            return f"⚠️ Couldn't process: _{text[:40]}_"

    def _parse_catalog_description(self, text: str, product_names: list, last_added: str) -> dict:
        """Use AI to parse a catalog description into structured data."""
        try:
            prompt = f"""Parse this catalog description into a JSON action.

Products in catalog: {json.dumps(product_names)}
{"User JUST added '" + last_added + "'. If they don't name a product, they probably mean this one." if last_added else ""}

Text: "{text}"

Return JSON with:
- action: "add_sizes" | "add_brands" | "add_attributes" | "add_conversions" | "add_products" | "unknown"
- targets: list of product names this applies to (use EXACT names from catalog). Only use "all" if user explicitly says "all products" or "all of them".
- data: the values to add

RULES:
- ONLY target products that are actually mentioned or clearly implied
- Include size context in conversion keys like "1 carton (500ml)" NOT just "1 carton"
- If user says "all" explicitly → targets: ["all"]
- If user doesn't specify a product, target the last added product

JSON only:"""

            result = self.categorizer.raw_completion(prompt, max_tokens=300)
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return {"action": "unknown"}

        except Exception as e:
            logger.error(f"AI catalog parse error: {e}")
            return {"action": "unknown"}

    def _apply_update(self, phone_number: str, catalog: dict, action: str, targets: list, data: dict) -> str:
        """Apply a parsed update to the catalog."""
        products = catalog.get("products", {})
        target_keys = self._resolve_targets(targets, products)

        if not target_keys:
            return "No matching products found."

        target_names = [products[k].get("name", k) for k in target_keys]

        if action == "add_sizes":
            sizes = data.get("sizes", [])
            for key in target_keys:
                attrs = products[key].setdefault("attributes", {})
                existing = attrs.get("sizes", [])
                attrs["sizes"] = list(dict.fromkeys(existing + sizes))
            return f"Added sizes to {', '.join(target_names)}"

        if action == "add_brands":
            brands = data.get("brands", [])
            for key in target_keys:
                attrs = products[key].setdefault("attributes", {})
                existing = attrs.get("brands", [])
                attrs["brands"] = list(dict.fromkeys(existing + brands))
            return f"Added brands to {', '.join(target_names)}"

        if action == "add_attributes":
            attr_name = data.get("attr_name", "")
            values = data.get("values", [])
            if attr_name and values:
                for key in target_keys:
                    attrs = products[key].setdefault("attributes", {})
                    existing = attrs.get(attr_name, [])
                    attrs[attr_name] = list(dict.fromkeys(existing + values))
                return f"Added {attr_name}: {values} to {', '.join(target_names)}"

        if action == "add_conversions":
            conversions = data.get("conversions", {})
            for key in target_keys:
                for conv_key, conv_val in conversions.items():
                    products[key].setdefault("conversions", {})[conv_key] = conv_val
            return f"Added conversions to {', '.join(target_names)}"

        if action == "add_products":
            new_products = data.get("products", [])
            for name in new_products:
                p_key = name.lower().replace(" ", "_")
                if p_key not in products:
                    products[p_key] = {"name": name, "attributes": {}, "conversions": {}, "pattern": []}
            return f"Added: {', '.join(new_products)}"

        return "Update applied."

    def _resolve_targets(self, targets: list, products: dict) -> list:
        """Resolve target names to product keys."""
        if not targets:
            return list(products.keys())
        if "all" in [t.lower() for t in targets]:
            return list(products.keys())

        resolved = []
        for target in targets:
            target_lower = target.lower()
            for key in products:
                if key == target_lower.replace(" ", "_"):
                    if key not in resolved:
                        resolved.append(key)
                    break
                if products[key].get("name", "").lower() == target_lower:
                    if key not in resolved:
                        resolved.append(key)
                    break
            else:
                for key in products:
                    name = products[key].get("name", key).lower()
                    if target_lower in name or name in target_lower:
                        if key not in resolved:
                            resolved.append(key)
                        break
        return resolved

    # ─────────────────────────────────────────────────────────
    # RESET
    # ─────────────────────────────────────────────────────────

    def _confirm_reset(self, phone_number: str) -> list:
        """Confirm before wiping catalog."""
        from utils.whatsapp_ui import button_response
        return [button_response(
            "⚠️ This will delete ALL products in your catalog.\n\nAre you sure?",
            [
                {"id": "cat_reset_yes", "title": "🗑️ Yes, Reset"},
                {"id": "cat_reset_no", "title": "❌ Keep It"},
            ]
        )]

    def _reset_catalog(self, phone_number: str) -> list:
        """Clear entire catalog."""
        self._save_catalog(phone_number, {"products": {}})
        self.session.reset(phone_number)
        return [text_response("🗑️ Catalog cleared! Tap *Catalog* in the menu to start fresh.")]

    # ─────────────────────────────────────────────────────────
    # Product detail view
    # ─────────────────────────────────────────────────────────

    def _show_product_detail(self, phone_number: str, product_key: str) -> list:
        """Show detailed view of a single product."""
        catalog = self._get_catalog(phone_number)
        product = catalog.get("products", {}).get(product_key)

        if not product:
            return [text_response("Product not found.")]

        name = product.get("name", product_key)
        attrs = product.get("attributes", {})
        conversions = product.get("conversions", {})
        pattern = product.get("pattern", [])

        lines = [f"📦 *{name}*\n"]

        if pattern:
            lines.append(f"🧩 Pattern: {' → '.join(pattern)}\n")

        for attr_name, values in attrs.items():
            if values:
                label = attr_name.replace("_", " ").capitalize()
                lines.append(f"• {label}: {', '.join(str(v) for v in values)}")

        if conversions:
            lines.append("\n📐 *Conversions:*")
            for key, val in conversions.items():
                lines.append(f"  {key} = {val} pieces")

        if not attrs and not conversions:
            lines.append("_No details set yet. Use Set Pattern or Edit to add._")

        return [text_response("\n".join(lines))]

    # ─────────────────────────────────────────────────────────
    # Database helpers
    # ─────────────────────────────────────────────────────────

    def _get_catalog(self, phone_number: str) -> dict:
        """Get the product catalog from user data."""
        user = self.db.get_user(phone_number)
        if not user:
            return {"products": {}}
        return user.get("product_catalog", {"products": {}})

    def _save_catalog(self, phone_number: str, catalog: dict):
        """Save catalog to user record."""
        self.db.update_user_field(phone_number, "product_catalog", catalog)
