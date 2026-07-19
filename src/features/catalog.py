# src/features/catalog.py
"""Product Catalog — ONE unified system.

Menu options:
1. Add Product — just add product names (the base dataset)
2. Set Product Format — define tree structure per product (Type → Brand → Colour)
3. Input Product Data — fill values into the tree structure
4. View A Product — interactive drill-down for one product
5. View All Catalog — shorthand overview of everything
6. Edit Catalog — remove, rename, modify
7. Reset Catalog — clear everything
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

        body = f"📋 *Your Catalog* — {product_count} product{'s' if product_count != 1 else ''}\n\nWhat would you like to do?"

        return [list_response(
            header="📋 Catalog",
            body=body,
            button_text="Select Action",
            sections=[{
                "title": "Catalog Actions",
                "rows": [
                    {"id": "cat_add", "title": "➕ Add Product", "description": "Add new product names"},
                    {"id": "cat_format", "title": "🧩 Set Product Format", "description": "Define tree (Type → Brand → Colour)"},
                    {"id": "cat_input", "title": "📥 Input Product Data", "description": "Fill values into product tree"},
                    {"id": "cat_view_one", "title": "🔍 View A Product", "description": "Drill-down one product"},
                    {"id": "cat_view_all", "title": "📋 View All Catalog", "description": "Overview of everything"},
                    {"id": "cat_edit", "title": "✏️ Edit Catalog", "description": "Remove, rename, modify"},
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
        if button_id == "cat_add":
            return self._start_add_products(phone_number)

        if button_id == "cat_format":
            return self._start_set_format(phone_number)

        if button_id == "cat_input":
            return self._start_input_data(phone_number)

        if button_id == "cat_view_one":
            return self._view_one_product(phone_number)

        if button_id in ("cat_view_all", "cat_browse"):
            return self._browse(phone_number)

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

        # Drill-down navigation buttons (cat_drill_[product_key]_[path_parts])
        if button_id.startswith("cat_drill_"):
            return self._handle_drill_button(phone_number, button_id)

        # Format product pick buttons (cat_fmt_[product_key])
        if button_id.startswith("cat_fmt_"):
            product_key = button_id[8:]
            return self._handle_format_pick_product(phone_number, product_key)

        # Format level pick buttons (cat_lvl_[level])
        if button_id.startswith("cat_lvl_"):
            session = self.session.get(phone_number)
            context = session.get("context", {})
            return self._handle_format_pick_level(phone_number, button_id, context)

        # Input product pick buttons (cat_inp_[product_key])
        if button_id.startswith("cat_inp_"):
            suffix = button_id[8:]  # after "cat_inp_"
            session = self.session.get(phone_number)
            context = session.get("context", {})

            # Navigation into tree
            if suffix.startswith("nav_"):
                value = suffix[4:]  # after "nav_"
                return self._handle_input_navigate(phone_number, value, context)

            # Add new value
            if suffix == "addnew":
                return self._handle_input_add_new(phone_number, context)

            # Quantity buttons
            if suffix == "addqty":
                self.session.save(phone_number, states.CATALOG_ADD_DATA, {**context, "inp_step": "typing_add_qty"})
                return [text_response("➕ How many to add?\n\n_Type a number:_")]

            if suffix == "setqty":
                self.session.save(phone_number, states.CATALOG_ADD_DATA, {**context, "inp_step": "typing_set_qty"})
                return [text_response("✏️ Set quantity to what?\n\n_Type a number:_")]

            if suffix == "done":
                self.session.reset(phone_number)
                return [text_response("✅ Done! Your catalog data is saved.")]

            if suffix == "back":
                # Go back one level
                path = context.get("inp_path", [])
                product_key_ctx = context.get("inp_product", "")
                if path:
                    path.pop()
                self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                    "inp_product": product_key_ctx,
                    "inp_path": path,
                    "inp_step": "browse_level",
                })
                return self._show_tree_level(phone_number, product_key_ctx, path)

            # Product pick (cat_inp_[product_key] — no prefix like nav_/addnew)
            return self._handle_input_pick_product(phone_number, suffix)

        # Pattern attribute buttons
        if button_id.startswith("cat_orgattr_"):
            attr = button_id[12:]
            return self._handle_pattern_attr_pick(phone_number, attr)

        # Edit action buttons
        if button_id.startswith("cat_editact_"):
            action = button_id[12:]
            return self._handle_edit_action(phone_number, action)

        # Cost level drill buttons
        if button_id.startswith("cat_costlvl_"):
            session = self.session.get(phone_number)
            context = session.get("context", {})
            # Ensure we're in the right state
            if context.get("edit_step") != "cost_drill" and button_id != "cat_costlvl___here__":
                # State may have been lost — check if we have a product key
                if not context.get("edit_product"):
                    return [text_response("❓ Session expired. Please start again from Catalog → Edit → Set Landing Cost.")]
            return self._handle_edit_flow(phone_number, button_id, context)

        # Edit target buttons
        if button_id.startswith("cat_edittgt_"):
            target = button_id[12:]
            return self._handle_edit_target(phone_number, target)

        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # BROWSE — Full details view
    # ─────────────────────────────────────────────────────────

    def _browse(self, phone_number: str) -> list:
        """Show all products with FULL tree details + stock levels."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response(
                "📋 Your catalog is empty!\n\n"
                "Tap *Add Products* to get started."
            )]

        lines = ["📋 *Your Catalog*\n"]
        total_stock = 0

        for key, data in products.items():
            name = data.get("name", key)
            pattern = data.get("pattern", [])
            tree = data.get("tree", {})
            landing_cost = data.get("landing_cost", 0)
            stock_count = data.get("stock_count", 0)

            lines.append(f"━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"📦 *{name}*")

            # Show landing cost if set
            if landing_cost:
                lines.append(f"  🏷️ Cost: ₦{int(landing_cost):,}")

            # Show pattern if set
            if pattern:
                lines.append(f"  🧩 {' → '.join(pattern)}")

            # Show tree data with stock
            if tree:
                product_stock = self._count_tree_stock(tree)
                total_stock += product_stock
                if product_stock > 0:
                    lines.append(f"  📊 Stock: *{product_stock} units*")
                self._render_tree_lines(tree, lines, indent=1)
            elif stock_count > 0:
                total_stock += stock_count
                lines.append(f"  📊 Stock: *{stock_count} units*")
            elif not pattern:
                lines.append(f"  _No details set yet_")
            else:
                lines.append(f"  _No data entered yet_")

        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"_{len(products)} product{'s' if len(products) != 1 else ''}_")
        if total_stock > 0:
            lines.append(f"_📊 Total stock: {total_stock} units_")

        return [text_response("\n".join(lines))]

    def _render_tree_lines(self, node, lines: list, indent: int = 1):
        """Recursively render tree data as indented text lines with stock indicators."""
        prefix = "  " * indent
        if not isinstance(node, dict):
            return

        for key, value in node.items():
            if str(key).startswith("__"):
                continue  # Skip __cost__, __stock__ meta keys
            if isinstance(value, dict) and value:
                # Branch with children — show count and recurse
                branch_stock = self._count_tree_stock(value)
                stock_str = f" _({branch_stock})_" if branch_stock > 0 else ""
                lines.append(f"{prefix}📂 {key}{stock_str}")
                self._render_tree_lines(value, lines, indent + 1)
            elif isinstance(value, (int, float)):
                # Leaf with quantity — show stock level with indicator
                qty = int(value)
                if qty <= 0:
                    indicator = "🔴"
                elif qty <= 3:
                    indicator = "🟡"
                else:
                    indicator = "🟢"
                lines.append(f"{prefix}{indicator} {key}: *{qty}*")
            else:
                # Empty branch
                lines.append(f"{prefix}📂 {key} _(empty)_")

    def _count_tree_stock(self, node) -> int:
        """Recursively count total stock across all leaves in a tree."""
        if isinstance(node, (int, float)):
            return max(0, int(node))
        if isinstance(node, dict):
            return sum(self._count_tree_stock(v) for v in node.values())
        return 0

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
    # SET PRODUCT FORMAT (Click 2)
    # ─────────────────────────────────────────────────────────

    def _start_set_format(self, phone_number: str) -> list:
        """Set product data format — pick a product, then define its tree levels."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response(
                "🧩 *Set Product Format*\n\n"
                "No products yet! Add products first.\n\n"
                "Go to: Catalog → ➕ Add Product"
            )]

        # Show product list to pick
        rows = []
        for key, data in list(products.items())[:10]:
            name = data.get("name", key)
            pattern = data.get("pattern", [])
            desc = f"Current: {' → '.join(pattern)}" if pattern else "No format set yet"
            rows.append({"id": f"cat_fmt_{key}", "title": name, "description": desc[:72]})

        return [list_response(
            header="🧩 Set Format",
            body="Pick a product to define its tree structure:",
            button_text="Select Product",
            sections=[{"title": "Products", "rows": rows}]
        )]

    def _handle_format_pick_product(self, phone_number: str, product_key: str) -> list:
        """User picked a product — now show attribute level options."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if product_key not in products:
            return [text_response("Product not found. Try again.")]

        product_name = products[product_key].get("name", product_key)
        current_pattern = products[product_key].get("pattern", [])

        # Save state
        self.session.save(phone_number, states.CATALOG_ORGANIZE, {
            "fmt_product": product_key,
            "fmt_step": "pick_level",
        })

        pattern_str = f"\nCurrent format: *{' → '.join(current_pattern)}*" if current_pattern else ""

        return [list_response(
            header=f"🧩 {product_name}",
            body=f"Pick Level {len(current_pattern) + 1} for the tree.{pattern_str}\n\nWhat attribute comes next?",
            button_text="Select Level",
            sections=[{"title": "Attribute Levels", "rows": [
                {"id": "cat_lvl_type", "title": "📦 Type", "description": "e.g. SUV, Sedan, Coupe"},
                {"id": "cat_lvl_brand", "title": "🏷️ Brand", "description": "e.g. Nike, Gucci, Adidas"},
                {"id": "cat_lvl_name", "title": "📝 Name/Model", "description": "e.g. Pilot, CRV, Camry"},
                {"id": "cat_lvl_size", "title": "📐 Size", "description": "e.g. 500ml, XL, 4L"},
                {"id": "cat_lvl_colour", "title": "🎨 Colour", "description": "e.g. Red, Blue, Black"},
                {"id": "cat_lvl_material", "title": "🧵 Material", "description": "e.g. Leather, Cotton"},
                {"id": "cat_lvl_condition", "title": "✨ Condition", "description": "e.g. Brand New, Second Hand"},
                {"id": "cat_lvl_custom", "title": "✏️ Custom", "description": "Type your own level name"},
                {"id": "cat_lvl_done", "title": "✅ Done", "description": "Finish setting format"},
            ]}]
        )]

    def _handle_format_pick_level(self, phone_number: str, level_id: str, context: dict) -> list:
        """User picked a level — add to pattern and show next."""
        product_key = context.get("fmt_product", "")
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if product_key not in products:
            self.session.reset(phone_number)
            return [text_response("Error: product not found. Try again from Catalog menu.")]

        # Map button IDs to level names
        level_map = {
            "cat_lvl_type": "Type",
            "cat_lvl_brand": "Brand",
            "cat_lvl_name": "Name",
            "cat_lvl_size": "Size",
            "cat_lvl_colour": "Colour",
            "cat_lvl_material": "Material",
            "cat_lvl_condition": "Condition",
        }

        # Done
        if level_id == "cat_lvl_done":
            self.session.reset(phone_number)
            pattern = products[product_key].get("pattern", [])
            product_name = products[product_key].get("name", product_key)
            if pattern:
                return [text_response(
                    f"✅ Format saved for *{product_name}*!\n\n"
                    f"🧩 {' → '.join(pattern)}\n\n"
                    f"Now go to *Input Product Data* to fill in values."
                )]
            else:
                return [text_response(f"👍 No changes made to *{product_name}*.")]

        # Custom — ask user to type
        if level_id == "cat_lvl_custom":
            self.session.save(phone_number, states.CATALOG_ORGANIZE, {
                "fmt_product": product_key,
                "fmt_step": "enter_custom_level",
            })
            return [text_response("✏️ Type the name for this level:\n\n_e.g. Scent, Flavor, Grade, Mileage_")]

        # Standard level
        level_name = level_map.get(level_id, "")
        if not level_name:
            return [text_response("Please pick from the list above.")]

        # Add to pattern
        pattern = products[product_key].setdefault("pattern", [])
        if level_name not in pattern:
            pattern.append(level_name)
        self._save_catalog(phone_number, catalog)

        # Show next level picker
        return self._handle_format_pick_product(phone_number, product_key)

    def _handle_format_custom_level(self, phone_number: str, text: str, context: dict) -> list:
        """User typed a custom level name."""
        product_key = context.get("fmt_product", "")
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if product_key not in products:
            self.session.reset(phone_number)
            return [text_response("Error: product not found.")]

        level_name = text.strip().capitalize()
        if len(level_name) < 2:
            return [text_response("Please type a level name (at least 2 characters):")]

        # Add to pattern
        pattern = products[product_key].setdefault("pattern", [])
        if level_name not in pattern:
            pattern.append(level_name)
        self._save_catalog(phone_number, catalog)

        # Show next level picker
        return self._handle_format_pick_product(phone_number, product_key)

    # ─────────────────────────────────────────────────────────
    # INPUT PRODUCT DATA (Click 3) — Phase B3
    # ─────────────────────────────────────────────────────────

    def _start_input_data(self, phone_number: str) -> list:
        """Input data — pick a product, then walk its tree format."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response(
                "📥 *Input Product Data*\n\n"
                "No products yet! Add products first.\n\n"
                "Go to: Catalog → ➕ Add Product"
            )]

        # Only show products that have a format set
        rows = []
        for key, data in list(products.items())[:10]:
            name = data.get("name", key)
            pattern = data.get("pattern", [])
            if pattern:
                desc = f"Format: {' → '.join(pattern)}"
            else:
                desc = "⚠️ Set format first"
            rows.append({"id": f"cat_inp_{key}", "title": name, "description": desc[:72]})

        return [list_response(
            header="📥 Input Data",
            body="Pick a product to add data to:",
            button_text="Select Product",
            sections=[{"title": "Products", "rows": rows}]
        )]

    def _handle_input_pick_product(self, phone_number: str, product_key: str) -> list:
        """User picked a product — show level 1 of its tree."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if product_key not in products:
            return [text_response("Product not found.")]

        product = products[product_key]
        pattern = product.get("pattern", [])

        if not pattern:
            return [text_response(
                f"⚠️ *{product.get('name', product_key)}* has no format set!\n\n"
                f"Go to: Catalog → 🧩 Set Product Format"
            )]

        # Start at level 0 of the tree
        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "inp_product": product_key,
            "inp_path": [],  # current path through the tree
            "inp_step": "browse_level",
        })

        return self._show_tree_level(phone_number, product_key, [])

    def _show_tree_level(self, phone_number: str, product_key: str, path: list) -> list:
        """Show the current level of the tree — existing values + Add New."""
        catalog = self._get_catalog(phone_number)
        product = catalog.get("products", {}).get(product_key, {})
        pattern = product.get("pattern", [])
        tree = product.get("tree", {})
        product_name = product.get("name", product_key)

        current_level = len(path)

        # Navigate to current position in tree
        node = tree
        for step in path:
            if isinstance(node, dict):
                node = node.get(step, {})
            else:
                node = {}

        # Check if we're at the last level (leaf)
        if current_level >= len(pattern):
            # We're at the leaf — show quantity
            qty = node if isinstance(node, (int, float)) else 0
            path_str = " → ".join(path)
            return [button_response(
                f"📦 *{product_name}*\n"
                f"📍 {path_str}\n\n"
                f"📊 Quantity: *{qty}*",
                [
                    {"id": "cat_inp_addqty", "title": "➕ Add Stock"},
                    {"id": "cat_inp_setqty", "title": "✏️ Set Quantity"},
                    {"id": "cat_inp_back", "title": "⬅️ Back"},
                ]
            )]

        # We're at a branch — show existing values + "Add New"
        level_name = pattern[current_level]
        rows = []

        # Show existing values at this level
        if isinstance(node, dict):
            for value in list(node.keys())[:9]:  # Max 9 + 1 for "Add New"
                if str(value).startswith("__"):
                    continue  # Skip __cost__, __stock__ meta keys
                # Count items below this value
                sub = node[value]
                if isinstance(sub, dict):
                    count = len([k for k in sub if not str(k).startswith("__")])
                    desc = f"{count} item{'s' if count != 1 else ''} inside"
                else:
                    desc = f"Qty: {sub}"
                rows.append({"id": f"cat_inp_nav_{value}", "title": value, "description": desc[:72]})

        # Add "Add New" option
        rows.append({"id": "cat_inp_addnew", "title": f"➕ Add New {level_name}", "description": f"Type a new {level_name.lower()}"})

        # Add "Done" option — stop filling here
        rows.append({"id": "cat_inp_done", "title": "✅ Done", "description": "Stop here, don't fill more levels"})
        # Build header showing path
        if path:
            path_str = f"{product_name} → {' → '.join(path)}"
        else:
            path_str = product_name

        return [list_response(
            header=f"📥 {level_name}",
            body=f"📍 {path_str}\n\nPick a {level_name.lower()} or add new:",
            button_text=f"Select {level_name}",
            sections=[{"title": level_name, "rows": rows}]
        )]

    def _handle_input_navigate(self, phone_number: str, value: str, context: dict) -> list:
        """User picked an existing value — go deeper in tree."""
        product_key = context.get("inp_product", "")
        path = context.get("inp_path", [])

        path.append(value)
        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "inp_product": product_key,
            "inp_path": path,
            "inp_step": "browse_level",
        })

        return self._show_tree_level(phone_number, product_key, path)

    def _handle_input_add_new(self, phone_number: str, context: dict) -> list:
        """User wants to add a new value at current level."""
        product_key = context.get("inp_product", "")
        path = context.get("inp_path", [])
        catalog = self._get_catalog(phone_number)
        product = catalog.get("products", {}).get(product_key, {})
        pattern = product.get("pattern", [])
        current_level = len(path)
        level_name = pattern[current_level] if current_level < len(pattern) else "Value"

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "inp_product": product_key,
            "inp_path": path,
            "inp_step": "typing_new_value",
        })

        return [text_response(f"✏️ Type the new *{level_name}* value:\n\n_e.g. SUV, Pilot, Red_")]

    def _handle_input_typed_value(self, phone_number: str, text: str, context: dict) -> list:
        """User typed a new value — save to tree and show next level.
        At leaf level: supports comma-separated values (e.g. 'Black, White, Red').
        """
        product_key = context.get("inp_product", "")
        path = context.get("inp_path", [])

        if len(text.strip()) < 1:
            return [text_response("Please type a value (at least 1 character):")]

        # Save to tree
        catalog = self._get_catalog(phone_number)
        product = catalog.get("products", {}).get(product_key, {})
        pattern = product.get("pattern", [])
        tree = product.setdefault("tree", {})

        # Navigate to current position
        node = tree
        for step in path:
            if step not in node:
                node[step] = {}
            node = node[step]

        # Determine if this is the leaf level
        current_level = len(path)
        is_leaf = (current_level + 1) >= len(pattern)

        if is_leaf and "," in text:
            # MULTIPLE VALUES — split by comma, create all at leaf level
            values = [v.strip().title() for v in text.split(",") if v.strip()]
            for val in values:
                if val and val not in node:
                    node[val] = 0
            self._save_catalog(phone_number, catalog)

            # Stay at this level so user can see what was added
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "inp_product": product_key,
                "inp_path": path,
                "inp_step": "browse_level",
            })
            return [text_response(
                f"✅ Added {len(values)} entries: {', '.join(values)}\n\n"
                f"_Add more or tap from the list to continue._"
            )]
        else:
            # SINGLE VALUE — original behavior
            value = text.strip().title()

            if is_leaf:
                node[value] = 0
            else:
                if value not in node:
                    node[value] = {}

            self._save_catalog(phone_number, catalog)

            # Navigate into the new value
            path.append(value)
            self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                "inp_product": product_key,
                "inp_path": path,
                "inp_step": "browse_level",
            })

            return self._show_tree_level(phone_number, product_key, path)

    def _handle_input_quantity(self, phone_number: str, text: str, context: dict, mode: str) -> list:
        """Handle quantity input (add or set)."""
        product_key = context.get("inp_product", "")
        path = context.get("inp_path", [])

        # Parse number
        try:
            qty = int(re.sub(r'[^\d]', '', text))
        except (ValueError, TypeError):
            return [text_response("Please enter a number:")]

        # Save to tree
        catalog = self._get_catalog(phone_number)
        product = catalog.get("products", {}).get(product_key, {})
        tree = product.setdefault("tree", {})

        # Navigate to leaf
        node = tree
        for step in path[:-1]:
            node = node.setdefault(step, {})

        leaf_key = path[-1] if path else ""
        if leaf_key:
            current = node.get(leaf_key, 0) if isinstance(node.get(leaf_key), (int, float)) else 0
            if mode == "add":
                node[leaf_key] = current + qty
            else:
                node[leaf_key] = qty

        self._save_catalog(phone_number, catalog)

        new_qty = node.get(leaf_key, 0)
        path_str = " → ".join(path)

        product_name = product.get("name", product_key)

        self.session.save(phone_number, states.CATALOG_ADD_DATA, {
            "inp_product": product_key,
            "inp_path": path,
            "inp_step": "browse_level",
        })

        return [button_response(
            f"✅ *{product_name}*\n📍 {path_str}\n📊 Quantity: *{new_qty}*",
            [
                {"id": "cat_inp_addnew", "title": "➕ Add More Here"},
                {"id": "cat_inp_back", "title": "⬅️ Back"},
                {"id": "cat_inp_done", "title": "✅ Done"},
            ]
        )]

    # ─────────────────────────────────────────────────────────
    # VIEW A PRODUCT (Click 4) — Phase B4
    # ─────────────────────────────────────────────────────────

    def _view_one_product(self, phone_number: str) -> list:
        """Interactive drill-down for one product."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response("🔍 No products in catalog yet. Add products first!")]

        # Show product list for user to pick one
        rows = []
        for key, data in list(products.items())[:10]:
            name = data.get("name", key)
            pattern = data.get("pattern", [])
            desc = f"Format: {' → '.join(pattern)}" if pattern else "No format set"
            rows.append({"id": f"cat_prod_{key}", "title": name, "description": desc[:72]})

        return [list_response(
            header="🔍 View Product",
            body="Pick a product to explore:",
            button_text="Select",
            sections=[{"title": "Products", "rows": rows}]
        )]

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
        # ── Format flow (Set Product Format) ──
        fmt_step = context.get("fmt_step", "")
        if fmt_step == "enter_custom_level":
            return self._handle_format_custom_level(phone_number, text, context)
        if fmt_step == "pick_level":
            # Text input in pick_level state — might be typing "done" or "cancel"
            if text.lower() in ("done", "cancel", "exit"):
                self.session.reset(phone_number)
                product_key = context.get("fmt_product", "")
                catalog = self._get_catalog(phone_number)
                pattern = catalog.get("products", {}).get(product_key, {}).get("pattern", [])
                name = catalog.get("products", {}).get(product_key, {}).get("name", product_key)
                if pattern:
                    return [text_response(f"✅ Format saved for *{name}*!\n\n🧩 {' → '.join(pattern)}")]
                return [text_response("👍 No changes.")]
            return [text_response("👆 Pick an attribute from the list above, or type *done* to finish.")]

        # ── Legacy organize flow ──
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
                    {"id": "cat_editact_set_cost", "title": "🏷️ Set Landing Cost", "description": "Set/update product cost price"},
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

        # SET LANDING COST
        if action == "set_cost":
            current_cost = product.get("landing_cost", 0)
            cost_str = f"\nProduct-level cost: *₦{int(current_cost):,}*" if current_cost else ""

            # If product has a tree, let user drill into a specific level
            if product.get("tree"):
                self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                    "edit_step": "cost_drill",
                    "edit_action": action,
                    "edit_product": product_key,
                    "cost_path": [],
                })
                return self._show_cost_level(phone_number, product_key, [])
            else:
                # No tree — just set at product level
                self.session.save(phone_number, states.CATALOG_ADD_DATA, {
                    "edit_step": "enter_landing_cost",
                    "edit_action": action,
                    "edit_product": product_key,
                    "cost_path": [],
                })
                return [text_response(
                    f"🏷️ *{product_name}* — Set Landing Cost\n"
                    f"{cost_str}\n\n"
                    f"Enter the cost price:\n_e.g. 50000, 150K, 10M_\n\n"
                    f"Or type *cancel*."
                )]

        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    def _handle_edit_flow(self, phone_number: str, text: str, context: dict) -> list:
        """Handle edit flow text input steps."""
        # ── Input Product Data flow (shares CATALOG_ADD_DATA state) ──
        inp_step = context.get("inp_step", "")
        if inp_step == "typing_new_value":
            return self._handle_input_typed_value(phone_number, text, context)
        if inp_step == "typing_add_qty":
            return self._handle_input_quantity(phone_number, text, context, "add")
        if inp_step == "typing_set_qty":
            return self._handle_input_quantity(phone_number, text, context, "set")
        if inp_step == "browse_level":
            # User typed text while in browse mode — might be "cancel" or confused
            if text.lower() in ("cancel", "exit", "done", "back"):
                self.session.reset(phone_number)
                return [text_response("👍 Done! Your data is saved.")]
            return [text_response("👆 Pick from the list above, or type *done* to finish.")]

        # ── Edit Catalog flow ──
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

        # SET LANDING COST
        if step == "enter_landing_cost":
            from utils.parser import parse_amount
            cost = parse_amount(text)
            if not cost:
                return [text_response("💰 Enter a valid amount (e.g. 50000, 150K, 10M):")]

            cost_path = context.get("cost_path", [])

            if product_key in catalog.get("products", {}):
                product_data = catalog["products"][product_key]

                if cost_path:
                    # Set cost at a specific tree node using __cost__ key
                    tree = product_data.setdefault("tree", {})
                    node = tree
                    for step_name in cost_path:
                        if step_name not in node:
                            node[step_name] = {}
                        if isinstance(node[step_name], (int, float)):
                            # It's a leaf (stock count) — convert to dict with stock
                            node[step_name] = {"__stock__": int(node[step_name])}
                        node = node[step_name]
                    node["__cost__"] = int(cost)
                else:
                    # Set at product level
                    product_data["landing_cost"] = int(cost)

                self._save_catalog(phone_number, catalog)

            product_name_display = catalog.get("products", {}).get(product_key, {}).get("name", product_key)
            path_display = f" → {' → '.join(cost_path)}" if cost_path else ""
            self.session.reset(phone_number)
            return [text_response(
                f"✅ *Landing cost saved!*\n\n"
                f"📦 {product_name_display}{path_display}\n"
                f"🏷️ Cost: *₦{int(cost):,}*\n\n"
                f"_This will auto-fill when you sell this item._"
            )]

        # COST DRILL — navigating tree to pick level for cost
        if step == "cost_drill":
            cost_path = context.get("cost_path", [])

            if text.startswith("cat_costlvl_"):
                # User picked a level to drill into
                value = text[12:]  # after "cat_costlvl_"
                if value == "__here__":
                    # Set cost at current level
                    context["edit_step"] = "enter_landing_cost"
                    self.session.save(phone_number, states.CATALOG_ADD_DATA, context)
                    path_display = " → ".join(cost_path) if cost_path else product_key
                    # Show current cost at this level if exists
                    current = self._get_cost_at_path(catalog, product_key, cost_path)
                    cost_str = f"\nCurrent: *₦{int(current):,}*" if current else ""
                    return [text_response(
                        f"🏷️ *Set cost for: {path_display}*{cost_str}\n\n"
                        f"Enter the landing cost:\n_e.g. 50000, 150K, 10M_"
                    )]
                else:
                    # Drill deeper
                    cost_path.append(value)
                    context["cost_path"] = cost_path
                    self.session.save(phone_number, states.CATALOG_ADD_DATA, context)
                    return self._show_cost_level(phone_number, product_key, cost_path)
            else:
                return [text_response("👆 Pick from the list above, or type *cancel*.")]

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

    def _show_product_detail(self, phone_number: str, product_key: str, path: list = None) -> list:
        """Interactive drill-down view of a single product's tree — one level at a time."""
        catalog = self._get_catalog(phone_number)
        product = catalog.get("products", {}).get(product_key)

        if not product:
            return [text_response("Product not found.")]

        name         = product.get("name", product_key)
        pattern      = product.get("pattern", [])
        tree         = product.get("tree", {})
        landing_cost = product.get("landing_cost", 0)

        if path is None:
            path = []

        # Navigate to current position in tree
        node = tree
        for step in path:
            if isinstance(node, dict) and step in node:
                node = node[step]
            else:
                node = {}
                break

        # Build path display
        if path:
            path_str = f"{name} → {' → '.join(path)}"
        else:
            path_str = name

        current_level = len(path)

        # ── At a leaf (quantity) or no tree ──
        if not isinstance(node, dict) or not node:
            # Show summary for this leaf/product
            qty = int(node) if isinstance(node, (int, float)) else 0
            lines = [
                f"━━━━━━━━━━━━━━━━━━━━",
                f"📦 *{path_str}*",
                f"━━━━━━━━━━━━━━━━━━━━",
            ]
            if qty > 0:
                indicator = "🟢" if qty > 3 else "🟡"
                lines.append(f"{indicator} Stock: *{qty} units*")
            elif isinstance(node, (int, float)):
                lines.append(f"🔴 Stock: *0* (out of stock)")
            if landing_cost:
                lines.append(f"🏷️ Landing Cost: ₦{int(landing_cost):,}")
            if not tree:
                lines.append(f"\n_No tree data. Use Input Product Data to add._")
            lines.append(f"\n━━━━━━━━━━━━━━━━━━━━")

            buttons = [{"id": f"cat_prod_{product_key}", "title": "⬅️ Back to Top"}]
            if path:
                parent_path = "_".join(path[:-1]) if len(path) > 1 else ""
                buttons.insert(0, {"id": f"cat_drill_{product_key}_{parent_path}", "title": "⬅️ Back"})

            return [text_response("\n".join(lines)),
                    button_response("What next?", buttons[:3])]

        # ── At a branch — show children as tappable list ──
        level_name = pattern[current_level] if current_level < len(pattern) else "Items"

        rows = []
        for key, value in list(node.items())[:10]:
            if str(key).startswith("__"):
                continue  # Skip __cost__, __stock__ meta keys
            if isinstance(value, dict):
                child_stock = self._count_tree_stock(value)
                desc = f"{child_stock} units in stock" if child_stock > 0 else "Tap to explore"
            elif isinstance(value, (int, float)):
                qty = int(value)
                indicator = "🟢" if qty > 3 else ("🟡" if qty > 0 else "🔴")
                desc = f"{indicator} Stock: {qty}"
            else:
                desc = "Tap to view"

            # Build drill path for this child
            child_path = "_".join(path + [key])
            rows.append({
                "id": f"cat_drill_{product_key}_{child_path}",
                "title": str(key)[:24],
                "description": desc[:72],
            })

        # Total stock at this level
        level_stock = self._count_tree_stock(node)
        stock_str = f" — *{level_stock} total*" if level_stock > 0 else ""

        body = f"📍 *{path_str}*{stock_str}\n\nTap to drill deeper:"
        if landing_cost and not path:
            body = f"📍 *{path_str}*{stock_str}\n🏷️ Cost: ₦{int(landing_cost):,}\n\nTap to drill deeper:"

        return [list_response(
            header=f"🔍 {level_name}",
            body=body,
            button_text=f"Select {level_name}",
            sections=[{"title": level_name, "rows": rows}]
        )]

    def _handle_drill_button(self, phone_number: str, button_id: str) -> list:
        """Handle cat_drill_[product_key]_[path_part1]_[path_part2]... buttons."""
        # Format: cat_drill_honda_Suv_Pilot_Red
        suffix = button_id[10:]  # after "cat_drill_"

        # The first segment is the product key, rest is the path
        # Product keys can have underscores, so we need to match against catalog
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        # Try to find the product key by matching from the start
        product_key = None
        path = []
        for key in products:
            if suffix == key:
                product_key = key
                path = []
                break
            if suffix.startswith(key + "_"):
                product_key = key
                path_str = suffix[len(key) + 1:]
                path = path_str.split("_") if path_str else []
                break

        if not product_key:
            # Fallback — treat first segment as product key
            parts = suffix.split("_", 1)
            product_key = parts[0]
            path = parts[1].split("_") if len(parts) > 1 else []

        return self._show_product_detail(phone_number, product_key, path)

    # ─────────────────────────────────────────────────────────
    # LANDING COST — tree-level cost setting helpers
    # ─────────────────────────────────────────────────────────

    def _show_cost_level(self, phone_number: str, product_key: str, cost_path: list) -> list:
        """Show tree level for picking where to set cost — with 'Set Here' option."""
        catalog = self._get_catalog(phone_number)
        product = catalog.get("products", {}).get(product_key, {})
        tree = product.get("tree", {})
        name = product.get("name", product_key)

        # Navigate to current position
        node = tree
        for step in cost_path:
            if isinstance(node, dict) and step in node:
                child = node[step]
                if isinstance(child, (int, float)):
                    # Leaf — can't drill deeper
                    node = {}
                    break
                node = child
            else:
                node = {}
                break

        # Build path display
        path_display = f"{name} → {' → '.join(cost_path)}" if cost_path else name

        # Check current cost at this level
        current_cost = self._get_cost_at_path(catalog, product_key, cost_path)
        cost_str = f"\n🏷️ Current cost here: *₦{int(current_cost):,}*" if current_cost else ""

        rows = []
        # "Set cost HERE" option — always first
        rows.append({
            "id": "cat_costlvl___here__",
            "title": f"🏷️ Set Cost Here",
            "description": f"Set cost for: {path_display}"[:72],
        })

        # Show children to drill deeper
        if isinstance(node, dict):
            for key, value in list(node.items())[:9]:
                if key.startswith("__"):
                    continue  # Skip __cost__, __stock__ meta keys
                if isinstance(value, dict):
                    child_cost = self._get_cost_at_path(catalog, product_key, cost_path + [key])
                    desc = f"Cost: ₦{int(child_cost):,}" if child_cost else "Drill deeper"
                    rows.append({
                        "id": f"cat_costlvl_{key}",
                        "title": str(key)[:24],
                        "description": desc[:72],
                    })
                # Skip leaf integers — can't drill into them, cost is set at this level

        return [list_response(
            header="🏷️ Set Landing Cost",
            body=f"📍 *{path_display}*{cost_str}\n\nSet cost here or drill deeper:",
            button_text="Select Level",
            sections=[{"title": "Options", "rows": rows}]
        )]

    def _get_cost_at_path(self, catalog: dict, product_key: str, path: list) -> int:
        """Get the landing cost stored at a specific tree path (using __cost__ keys)."""
        product = catalog.get("products", {}).get(product_key, {})

        # Check path-level cost (stored as __cost__ in tree nodes)
        if path:
            tree = product.get("tree", {})
            node = tree
            for step in path:
                if isinstance(node, dict) and step in node:
                    child = node[step]
                    if isinstance(child, (int, float)):
                        break
                    node = child
                else:
                    break
            # Check __cost__ at this node
            if isinstance(node, dict) and "__cost__" in node:
                return int(node["__cost__"])

        # Fallback to product-level cost
        return int(product.get("landing_cost", 0))

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
