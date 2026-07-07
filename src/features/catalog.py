# src/features/catalog.py
"""Product Catalog — ONE unified system. AI-powered setup + browse."""

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

        body = f"📋 *Your Catalog* ({product_count} products)\n\nWhat would you like to do?"

        return [list_response(
            header="📋 Catalog",
            body=body,
            button_text="Select Action",
            sections=[{
                "title": "Catalog Actions",
                "rows": [
                    {"id": "cat_browse", "title": "📋 Browse Products", "description": f"{product_count} products in catalog"},
                    {"id": "cat_setup", "title": "➕ Add/Setup Products", "description": "AI-powered: describe your products"},
                    {"id": "cat_organize", "title": "⚙️ Organize", "description": "Build product tree (brands, sizes)"},
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
            return self._handle_add_data(phone_number, text, context)

        return self.show_menu(phone_number)

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle catalog buttons."""
        if button_id == "cat_browse":
            return self._browse(phone_number)

        if button_id == "cat_setup":
            return self._start_setup(phone_number)

        if button_id == "cat_organize":
            return self._start_organize(phone_number)

        if button_id == "cat_reset":
            return self._reset_catalog(phone_number)

        # Product-specific buttons (cat_prod_[product_key])
        if button_id.startswith("cat_prod_"):
            product_key = button_id[9:]  # after "cat_prod_"
            return self._show_product_detail(phone_number, product_key)

        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # Browse
    # ─────────────────────────────────────────────────────────

    def _browse(self, phone_number: str) -> list:
        """Show all products in catalog."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response(
                "📋 Your catalog is empty!\n\n"
                "Tap *Add/Setup Products* to get started."
            )]

        lines = ["📋 *Your Products*\n"]
        for key, data in products.items():
            name = data.get("name", key)
            attrs = data.get("attributes", {})
            sizes = attrs.get("sizes", [])
            brands = attrs.get("brands", attrs.get("subcategories", []))

            detail_parts = []
            if sizes:
                detail_parts.append(f"Sizes: {', '.join(sizes[:4])}")
            if brands:
                detail_parts.append(f"Types: {', '.join(brands[:4])}")

            detail = f"\n  _{', '.join(detail_parts)}_" if detail_parts else ""
            lines.append(f"• *{name}*{detail}")

        lines.append(f"\n_{len(products)} total products_")
        return [text_response("\n".join(lines))]

    # ─────────────────────────────────────────────────────────
    # Setup (AI-powered)
    # ─────────────────────────────────────────────────────────

    def _start_setup(self, phone_number: str) -> list:
        """Start catalog setup — ask for product list."""
        catalog = self._get_catalog(phone_number)

        if catalog.get("products"):
            # Products exist — go to details mode
            self.session.save(phone_number, states.CATALOG_SETUP_DETAILS, {
                "last_added_product": "",
            })
            product_list = ", ".join(p.get("name", k) for k, p in catalog["products"].items())
            return [text_response(
                f"📋 Current products: {product_list}\n\n"
                f"Describe what to add or change — sizes, brands, conversions, new products.\n\n"
                f"Examples:\n"
                f"• _\"Airfreshener comes in 500ml, 1L, 4L\"_\n"
                f"• _\"Brands: Charming, Alluring\"_\n"
                f"• _\"1 carton = 12 pieces for 500ml\"_\n"
                f"• _\"Add Toilet Wash, Dish Wash\"_\n\n"
                f"Type *done* when finished."
            )]
        else:
            # Empty — ask for product list
            self.session.save(phone_number, states.CATALOG_SETUP_PRODUCTS, {})
            return [text_response(
                "📋 Let's set up your catalog!\n\n"
                "List your main products (comma or newline separated):\n\n"
                "Example:\n_Airfreshener, Hand Wash, Floor Cleaner, Dish Wash_"
            )]

    def _handle_setup_products(self, phone_number: str, text: str, context: dict) -> list:
        """Handle product list input during setup."""
        if text.lower() in ("done", "cancel", "exit"):
            self.session.reset(phone_number)
            return [text_response("👍 Catalog setup cancelled.")]

        # Parse product list (comma or newline separated)
        raw_items = re.split(r'[,\n]+', text)
        raw_items = [item.strip() for item in raw_items if item.strip()]

        if not raw_items:
            return [text_response("Please list your products, separated by commas:\n\n_e.g. Shoes, Bags, Clothes_")]

        # Smart grouping — detect "500ml Airfreshener, 4L Airfreshener" patterns
        size_pattern = re.compile(r'^(\d+(?:\.\d+)?)\s*(ml|l|kg|g|cl|oz)\s+(.+)$', re.IGNORECASE)
        products = {}  # product_key → {name, sizes: []}

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

        for p_key, p_data in products.items():
            catalog["products"][p_key] = {
                "name": p_data["name"],
                "attributes": {"sizes": p_data["sizes"]} if p_data["sizes"] else {},
                "conversions": {},
            }

        self._save_catalog(phone_number, catalog)

        # Move to details mode
        self.session.save(phone_number, states.CATALOG_SETUP_DETAILS, {
            "last_added_product": list(products.keys())[-1] if products else "",
        })

        product_names = [p["name"] for p in products.values()]
        return [text_response(
            f"✅ Added {len(products)} products: {', '.join(product_names)}\n\n"
            f"Now describe details — sizes, brands, conversions:\n\n"
            f"• _\"All come in 500ml, 1L, 4L\"_\n"
            f"• _\"Airfreshener brands: Charming, Alluring\"_\n"
            f"• _\"1 carton = 12 pieces for 500ml\"_\n\n"
            f"Type *done* when finished."
        )]

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

        # Update last_added_product in context
        self.session.save(phone_number, states.CATALOG_SETUP_DETAILS, context)

        combined = "\n".join(results)
        return [text_response(f"{combined}\n\n_Send more descriptions or type *done*._")]

    def _parse_and_apply(self, phone_number: str, text: str, context: dict) -> str:
        """Parse a single description line and apply to catalog."""
        try:
            catalog = self._get_catalog(phone_number)
            product_names = [p.get("name", k) for k, p in catalog.get("products", {}).items()]
            last_added = context.get("last_added_product", "")

            # AI parse
            parsed = self._parse_catalog_description(text, product_names, last_added)

            if not parsed or parsed.get("action") == "unknown":
                return f"🤔 Didn't understand: _{text[:40]}_"

            # Apply
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
  - For add_sizes: {{"sizes": ["500ml", "1L"]}}
  - For add_brands: {{"brands": ["Nike", "Gucci"]}}
  - For add_attributes: {{"attr_name": "color", "values": ["red", "blue"]}}
  - For add_conversions: {{"conversions": {{"1 carton (500ml)": 12, "1 carton (1L)": 6}}}}
  - For add_products: {{"products": ["New Product 1"]}}

RULES:
- ONLY target products that are actually mentioned or clearly implied
- Include size context in conversion keys like "1 carton (500ml)" NOT just "1 carton"
- If user says "all" explicitly → targets: ["all"]
- If user doesn't specify a product, target the last added product

JSON only, no explanation:"""

            result = self.categorizer.raw_completion(prompt, max_tokens=300)
            # Parse JSON from response
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return {"action": "unknown"}

        except Exception as e:
            logger.error(f"AI catalog parse error: {e}")
            return self._parse_catalog_simple(text)

    def _parse_catalog_simple(self, text: str) -> dict:
        """Rule-based fallback parser."""
        text_lower = text.lower()

        # Size patterns
        sizes = re.findall(r'(\d+(?:\.\d+)?\s*(?:ml|l|kg|g|cl|oz))', text_lower)
        if sizes:
            return {"action": "add_sizes", "targets": ["all"], "data": {"sizes": sizes}}

        # Brand patterns (after "brand:" or "brands:")
        brand_match = re.search(r'brands?:\s*(.+)', text, re.IGNORECASE)
        if brand_match:
            brands = [b.strip() for b in brand_match.group(1).split(",") if b.strip()]
            return {"action": "add_brands", "targets": ["all"], "data": {"brands": brands}}

        return {"action": "unknown"}

    def _apply_update(self, phone_number: str, catalog: dict, action: str, targets: list, data: dict) -> str:
        """Apply a parsed update to the catalog."""
        products = catalog.get("products", {})

        # Resolve targets
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
            return f"Added sizes {sizes} to {', '.join(target_names)}"

        if action == "add_brands":
            brands = data.get("brands", [])
            for key in target_keys:
                attrs = products[key].setdefault("attributes", {})
                existing = attrs.get("brands", [])
                attrs["brands"] = list(dict.fromkeys(existing + brands))
            return f"Added brands {brands} to {', '.join(target_names)}"

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
                # Only apply if product has matching sizes
                product_sizes = products[key].get("attributes", {}).get("sizes", [])
                for conv_key, conv_val in conversions.items():
                    # Check if conversion mentions a size this product doesn't have
                    size_in_key = re.search(r'\((\d+(?:\.\d+)?\s*\w+)\)', conv_key)
                    if size_in_key and product_sizes:
                        conv_size = size_in_key.group(1).lower().replace(" ", "")
                        product_sizes_normalized = [s.lower().replace(" ", "") for s in product_sizes]
                        if conv_size not in product_sizes_normalized:
                            continue  # Skip — this conversion doesn't apply to this product
                    products[key].setdefault("conversions", {})[conv_key] = conv_val
            return f"Added conversions to {', '.join(target_names)}"

        if action == "add_products":
            new_products = data.get("products", [])
            for name in new_products:
                p_key = name.lower().replace(" ", "_")
                if p_key not in products:
                    products[p_key] = {"name": name, "attributes": {}, "conversions": {}}
            return f"Added products: {', '.join(new_products)}"

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
            # Exact match
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
                # Partial match (substring)
                for key in products:
                    name = products[key].get("name", key).lower()
                    if target_lower in name or name in target_lower:
                        if key not in resolved:
                            resolved.append(key)
                        break

        return resolved

    # ─────────────────────────────────────────────────────────
    # Organize (tree building)
    # ─────────────────────────────────────────────────────────

    def _start_organize(self, phone_number: str) -> list:
        """Show products to organize."""
        catalog = self._get_catalog(phone_number)
        products = catalog.get("products", {})

        if not products:
            return [text_response("📋 No products to organize. Add products first!")]

        rows = []
        for key, data in list(products.items())[:10]:
            name = data.get("name", key)
            rows.append({"id": f"cat_org_{key}", "title": name})

        self.session.save(phone_number, states.CATALOG_ORGANIZE, {"org_step": "pick_product"})

        return [list_response(
            header="⚙️ Organize Product",
            body="Pick a product to build its category tree:",
            button_text="Select Product",
            sections=[{"title": "Products", "rows": rows}]
        )]

    def _handle_organize(self, phone_number: str, text: str, context: dict) -> list:
        """Handle organize flow steps."""
        if text.lower() == "done":
            self.session.reset(phone_number)
            return [text_response("✅ Organization saved!")]

        # For now, simple attribute adding
        step = context.get("org_step", "")

        if step == "pick_product" or text.startswith("cat_org_"):
            product_key = text.replace("cat_org_", "") if text.startswith("cat_org_") else text.lower().replace(" ", "_")
            context["org_product"] = product_key
            context["org_step"] = "pick_attribute"
            self.session.save(phone_number, states.CATALOG_ORGANIZE, context)

            return [list_response(
                header="⚙️ Add Attribute Level",
                body="What attribute do you want to organize by?",
                button_text="Select Attribute",
                sections=[{"title": "Attributes", "rows": [
                    {"id": "cat_orgattr_brand", "title": "🏷️ Brand"},
                    {"id": "cat_orgattr_size", "title": "📐 Size"},
                    {"id": "cat_orgattr_color", "title": "🎨 Color"},
                    {"id": "cat_orgattr_type", "title": "📦 Type/Variant"},
                    {"id": "cat_orgattr_material", "title": "🧵 Material"},
                    {"id": "btn_done", "title": "✅ Done"},
                ]}]
            )]

        if step == "pick_attribute" or text.startswith("cat_orgattr_"):
            attr = text.replace("cat_orgattr_", "") if text.startswith("cat_orgattr_") else text.lower()
            context["org_attr"] = attr
            context["org_step"] = "enter_values"
            self.session.save(phone_number, states.CATALOG_ORGANIZE, context)
            return [text_response(f"Type the {attr} values (comma separated):\n\n_e.g. Nike, Adidas, Puma_")]

        if step == "enter_values":
            values = [v.strip() for v in text.split(",") if v.strip()]
            if values:
                product_key = context.get("org_product", "")
                attr = context.get("org_attr", "")
                catalog = self._get_catalog(phone_number)
                if product_key in catalog.get("products", {}):
                    attrs = catalog["products"][product_key].setdefault("attributes", {})
                    existing = attrs.get(f"{attr}s", [])  # pluralize: brand → brands
                    attrs[f"{attr}s"] = list(dict.fromkeys(existing + values))
                    self._save_catalog(phone_number, catalog)

            context["org_step"] = "pick_attribute"
            self.session.save(phone_number, states.CATALOG_ORGANIZE, context)
            return [text_response(
                f"✅ Added! Pick another attribute or tap *Done*."
            )]

        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    def _handle_add_data(self, phone_number: str, text: str, context: dict) -> list:
        """Handle add data flow — placeholder for now."""
        self.session.reset(phone_number)
        return self.show_menu(phone_number)

    # ─────────────────────────────────────────────────────────
    # Reset
    # ─────────────────────────────────────────────────────────

    def _reset_catalog(self, phone_number: str) -> list:
        """Clear entire catalog."""
        self._save_catalog(phone_number, {"products": {}})
        self.session.reset(phone_number)
        return [text_response("🗑️ Catalog cleared! Tap *Catalog* in the menu to set up fresh.")]

    # ─────────────────────────────────────────────────────────
    # Product detail
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

        lines = [f"📦 *{name}*\n"]

        for attr_name, values in attrs.items():
            if values:
                lines.append(f"• {attr_name.capitalize()}: {', '.join(str(v) for v in values)}")

        if conversions:
            lines.append("\n📐 *Conversions:*")
            for key, val in conversions.items():
                lines.append(f"  {key} = {val} pieces")

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
