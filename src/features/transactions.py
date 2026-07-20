# src/features/transactions.py
"""Transaction recording — the ONLY natural language feature.

Handles:
- Free-text transaction parsing (AI-powered)
- Confirmation flow (yes/edit/cancel buttons)
- Guided recording flow (button-driven step-by-step)
- Credit/debt detection
- CRM prompt trigger (after save)
- Edit/delete existing transactions
"""

import logging
import re
import traceback
from datetime import datetime

from core import states
from utils.parser import parse_amount, detect_transaction_type, extract_vendor_name
from utils.whatsapp_ui import (
    text_response, button_response, list_response, confirm_buttons, format_amount
)

logger = logging.getLogger(__name__)

# Credit signals anywhere in text
CREDIT_SIGNALS = ['on credit', 'credit', 'owe', 'owes', 'debt', 'balance', 'not paid', 'unpaid']
# Payment signals
PAYMENT_SIGNALS = ['paid', 'pay', 'received', 'settled', 'cleared', 'payment']
# Buyer direction signals
BUYER_SIGNALS = ['i bought', 'i purchased', 'bought from', 'i owe']


class TransactionHandler:
    """Handles all transaction recording flows."""

    def __init__(self, session_mgr, database, categorizer, get_industry_fn):
        self.session = session_mgr
        self.db = database
        self.categorizer = categorizer
        self._get_industry = get_industry_fn  # function(phone_number) → industry handler

    # ═══════════════════════════════════════════════════════════
    # RECORD — The NLP path (free text → AI parse → confirm)
    # ═══════════════════════════════════════════════════════════

    def record(self, phone_number: str, text: str, session: dict) -> list:
        """Parse free text as a transaction and show confirmation."""
        try:
            # Check for payment/debt signals first
            text_lower = text.lower()
            if self._is_payment(text_lower):
                return self._handle_payment_text(phone_number, text)

            # Parse amount
            amount = parse_amount(text)
            if not amount:
                # No financial signal — not a transaction
                return [text_response(
                    "💬 Just type what you bought or sold and I'll record it!\n\n"
                    "Example: _sold shoes 50K to Sandra_\n\n"
                    "Or tap ☰ Menu below for other options."
                )]

            # AI categorization — try rich parse first
            # Get user's industry for better categorization
            user = self.db.get_user(phone_number)
            industry_class = user.get("industry_class", "trading") if user else "trading"

            ai_result = self.categorizer._call_openai_rich(
                text,
                business_type=industry_class,
                phone_number=phone_number,
                industry_class=industry_class
            )

            if ai_result:
                # Rich parse returns: transaction_type, item_name, brand, quantity,
                # vendor_or_customer, category, sub_category, confidence
                tx_type = ai_result.get("transaction_type", detect_transaction_type(text))
                description = ai_result.get("item_name", text)
                category = ai_result.get("category", "Uncategorized")
                vendor = ai_result.get("vendor_or_customer", "") or extract_vendor_name(text) or ""
                quantity = ai_result.get("quantity", "")
                brand = ai_result.get("brand", "")

                # USE AI's total_amount when available — it handles "each"/"per unit" math
                ai_amount = ai_result.get("total_amount")
                if ai_amount and ai_amount > 0:
                    amount = float(ai_amount)
                # Also grab unit_cost for display
                unit_cost = ai_result.get("unit_cost")
            else:
                # Fallback to simple categorize + parser
                simple_result = self.categorizer.categorize(text, phone_number) or {}
                tx_type = detect_transaction_type(text)
                description = text
                category = simple_result.get("category", "Uncategorized")
                vendor = extract_vendor_name(text) or ""
                quantity = ""
                brand = ""
                unit_cost = None

            # Clean bad vendor names (transaction verbs that get mistaken for names)
            bad_vendors = {"sold", "bought", "paid", "received", "sale", "purchase",
                          "expense", "income", "cash", "transfer", "sell", "buy"}
            if vendor.lower().strip() in bad_vendors:
                vendor = ""

            # Check for credit signals
            has_credit = any(sig in text_lower for sig in CREDIT_SIGNALS)

            # Normalize tx_type naming
            if tx_type in ("income", "sale", "sales"):
                tx_type = "sale"
            elif tx_type in ("expense", "cost"):
                tx_type = "expense"
            elif tx_type in ("purchase", "buy"):
                tx_type = "purchase"

            # Smart correction: if category is stock/COGS but type is "expense", it's a purchase
            COGS_CATS = {"Goods & Stock", "Production & Manufacturing", "Service Costs"}
            if tx_type == "expense" and category in COGS_CATS:
                tx_type = "purchase"

            # Reverse: if category is operating expense but type is "purchase", it's expense
            if tx_type == "purchase" and category not in COGS_CATS and category != "Uncategorized":
                tx_type = "expense"

            tx_data = {
                "amount": float(amount),
                "type": tx_type,
                "description": description,
                "category": category,
                "vendor": vendor,
                "quantity": str(quantity) if quantity else "",
                "brand": brand,
                "unit_cost": unit_cost,
                "raw_text": text,
                "has_credit": has_credit,
            }

            # ── Auto-match to catalog product ──
            catalog_match = self._match_to_catalog(phone_number, description, brand, text_lower)
            if catalog_match:
                tx_data["catalog_product"] = catalog_match.get("product_key", "")
                tx_data["catalog_product_name"] = catalog_match.get("product_name", "")

            # Save state
            self.session.save(phone_number, states.AWAITING_CONFIRMATION, {
                "pending_transaction": tx_data,
            })

            # Build confirmation message
            return self._build_confirmation(tx_data, has_credit)

        except Exception as e:
            logger.error(f"Transaction record error: {e}\n{traceback.format_exc()}")
            return [text_response(
                "😅 I had trouble understanding that. Try something like:\n\n"
                "_sold shoes 50K_\n_bought rice from Alhaji 30000_\n_transport 5K_"
            )]

    def _build_confirmation(self, tx_data: dict, has_credit: bool) -> list:
        """Build a polished transaction confirmation card."""
        tx_type = tx_data["type"]
        amount = tx_data["amount"]
        description = tx_data.get("description", "Transaction")
        category = tx_data.get("category", "Uncategorized")
        vendor = tx_data.get("vendor", "")
        quantity = tx_data.get("quantity", "")
        brand = tx_data.get("brand", "")
        unit_cost = tx_data.get("unit_cost")

        # Type styling
        type_config = {
            "sale":     {"emoji": "💰", "label": "SALE",     "accent": "received"},
            "purchase": {"emoji": "📦", "label": "PURCHASE", "accent": "spent"},
            "expense":  {"emoji": "💸", "label": "EXPENSE",  "accent": "spent"},
        }
        config = type_config.get(tx_type, {"emoji": "📝", "label": "TRANSACTION", "accent": "recorded"})

        # ── Build card ──
        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"{config['emoji']}  *{config['label']}*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"*{format_amount(amount)}*",
        ]

        # Item name
        lines.append(f"  📦 {description}")

        # Brand (separate line)
        if brand:
            lines.append(f"  🏷️ {brand}")

        # Quantity + unit cost
        if quantity and unit_cost:
            lines.append(f"  📐 {quantity} × {format_amount(unit_cost)} each")
        elif quantity:
            lines.append(f"  📐 Qty: {quantity}")

        # Details (colors, patterns, etc from guided flow)
        details = tx_data.get("details", "")
        if details and details.lower() not in ("skip", "no", ""):
            lines.append(f"  🎨 {details}")

        lines.append("")

        # Details section
        if vendor:
            lines.append(f"👤  {vendor}")
        lines.append(f"📁  {category}")

        if has_credit:
            lines.append(f"💳  _On credit (not yet paid)_")

        lines.append("")
        lines.append(f"━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"_Is this correct?_")

        body = "\n".join(lines)

        return [button_response(body, confirm_buttons())]

    # ═══════════════════════════════════════════════════════════
    # CONFIRMATION — User taps Yes/Edit/Cancel
    # ═══════════════════════════════════════════════════════════

    def handle_confirmation(self, phone_number: str, text: str, session: dict) -> list:
        """Handle response to the confirmation card."""
        text_lower = text.lower().strip()
        context = session.get("context", {})
        tx_data = context.get("pending_transaction", {})

        # ── YES — Ask payment method before saving ──
        if text_lower in ("yes", "y", "correct", "confirm_yes", "✅ yes"):
            return self._ask_payment_method(phone_number, tx_data)

        # ── EDIT — Show edit options ──
        if text_lower in ("edit", "change", "fix", "confirm_edit", "✏️ edit"):
            return self._show_edit_options(phone_number, tx_data)

        # ── CANCEL ──
        if text_lower in ("no", "n", "cancel", "wrong", "confirm_cancel", "❌ cancel"):
            self.session.reset(phone_number)
            return [text_response("❌ Cancelled. Send another transaction or tap the menu.")]

        # ── ESCAPE: User sent a new transaction (has an amount) ──
        amount = parse_amount(text)
        if amount:
            self.session.reset(phone_number)
            # Re-process as new transaction
            new_session = self.session.get(phone_number)
            return self.record(phone_number, text, new_session)

        # ── ESCAPE: Credit/payment pattern ──
        if any(sig in text_lower for sig in CREDIT_SIGNALS + PAYMENT_SIGNALS):
            self.session.reset(phone_number)
            new_session = self.session.get(phone_number)
            return self.record(phone_number, text, new_session)

        # ── Didn't understand — nudge with buttons ──
        return [button_response(
            "👆 Tap a button above, or type *yes*, *edit*, or *cancel*.",
            confirm_buttons()
        )]

    # ═══════════════════════════════════════════════════════════
    # PAYMENT METHOD — Cash / Transfer / Credit
    # ═══════════════════════════════════════════════════════════

    def _ask_payment_method(self, phone_number: str, tx_data: dict) -> list:
        """Ask how the transaction was paid — Cash, Transfer, or Credit."""
        tx_type = tx_data.get("type", "sale")
        amount  = tx_data.get("amount", 0)

        if tx_type == "sale":
            prompt = f"💳 *How did they pay?*\n\n_{format_amount(amount)} sale_"
        elif tx_type == "purchase":
            prompt = f"💳 *How did you pay?*\n\n_{format_amount(amount)} purchase_"
        else:
            prompt = f"💳 *Payment method?*\n\n_{format_amount(amount)} expense_"

        self.session.save(phone_number, states.PAYMENT_METHOD, {
            "pending_transaction": tx_data,
        })

        return [button_response(
            prompt,
            [
                {"id": "pm_cash",     "title": "💵 Cash"},
                {"id": "pm_transfer", "title": "🏦 Transfer/POS"},
                {"id": "pm_credit",   "title": "📝 On Credit"},
            ]
        )]

    def handle_payment_method(self, phone_number: str, text: str, session: dict) -> list:
        """Handle payment method selection."""
        context  = session.get("context", {})
        tx_data  = context.get("pending_transaction", {})
        text_low = text.lower().strip()

        # Map responses
        if text_low in ("pm_cash", "cash", "1"):
            tx_data["payment_method"] = "cash"
            tx_data["has_credit"] = False
        elif text_low in ("pm_transfer", "transfer", "pos", "2"):
            tx_data["payment_method"] = "transfer"
            tx_data["has_credit"] = False
        elif text_low in ("pm_credit", "credit", "on credit", "3"):
            tx_data["payment_method"] = "credit"
            tx_data["has_credit"] = True
        elif text_low in ("skip", "cancel"):
            tx_data["payment_method"] = "cash"
            tx_data["has_credit"] = False
        else:
            # Didn't understand — nudge
            return [button_response(
                "💳 Please select a payment method:",
                [
                    {"id": "pm_cash",     "title": "💵 Cash"},
                    {"id": "pm_transfer", "title": "🏦 Transfer/POS"},
                    {"id": "pm_credit",   "title": "📝 On Credit"},
                ]
            )]

        # Now save the transaction with payment method set
        return self._save_transaction(phone_number, tx_data)

    def _save_transaction(self, phone_number: str, tx_data: dict) -> list:
        """Save confirmed transaction to DynamoDB."""
        try:
            has_credit = tx_data.get("has_credit", False)
            vendor = tx_data.get("vendor", "")

            # Determine credit direction
            if has_credit and vendor:
                return self._save_credit_transaction(phone_number, tx_data)

            # Normal save
            # Build extra_details including catalog linkage
            extra = {}
            if tx_data.get("details"):
                extra["details"] = tx_data["details"]
            if tx_data.get("catalog_product"):
                extra["catalog_product"] = tx_data["catalog_product"]
            if tx_data.get("catalog_path"):
                extra["catalog_path"] = tx_data["catalog_path"]
            if tx_data.get("catalog_selections"):
                extra["catalog_selections"] = tx_data["catalog_selections"]
            if tx_data.get("catalog_product_name"):
                extra["catalog_product_name"] = tx_data["catalog_product_name"]
            if tx_data.get("landing_cost"):
                extra["landing_cost"] = int(tx_data["landing_cost"])

            result = self.db.save_transaction(
                phone_number,
                int(tx_data["amount"]),
                tx_data["type"],
                tx_data["description"],
                tx_data["category"],
                vendor=vendor,
                quantity=tx_data.get("quantity"),
                brand=tx_data.get("brand"),
                item_name=tx_data.get("description"),
                unit_cost=tx_data.get("unit_cost"),
                payment_method=tx_data.get("payment_method"),
                extra_details=extra if extra else None,
            )
            tx_id = result.get("transaction_id", "") if isinstance(result, dict) else ""

            # ── Update CRM contact totals ──
            if vendor:
                try:
                    self.db.update_contact_totals(
                        phone_number, vendor,
                        int(tx_data["amount"]), tx_data["type"]
                    )
                except Exception as e:
                    logger.warning(f"CRM update failed: {e}")

            # ── For SALES: ask landing cost (trading/manufacturing) or materials used (services) ──
            if tx_data["type"] == "sale":
                # Check user's industry
                user = self.db.get_user(phone_number)
                industry = user.get("industry_class", "trading") if user else "trading"

                if industry == "services":
                    # Services: ask about supplies used (optional)
                    return self._ask_supplies_used(phone_number, tx_id, tx_data)
                else:
                    # Trading/Manufacturing: check if product has variants → ask which one
                    return self._check_variants_then_landing_cost(phone_number, tx_id, tx_data)

            # ── For PURCHASES: update inventory (add stock + save cost) ──
            if tx_data["type"] == "purchase":
                qty = self._parse_qty(tx_data.get("quantity", "1"))
                unit_cost = int(tx_data.get("unit_cost") or 0)
                desc = tx_data.get("description", "")
                brand = tx_data.get("brand", "")
                search_name = f"{brand} {desc}".strip() if brand else desc

                # Detect variant from description/details
                variant = self._detect_variant(phone_number, search_name)

                from features.catalog import CatalogHandler
                cat = CatalogHandler(self.session, self.db)
                qty_str = tx_data.get("quantity", "")
                stock_result = cat.update_stock(phone_number, search_name, qty, unit_cost, qty_str, variant=variant)

                # For manufacturing: also update recipe costs where this material is used
                if unit_cost > 0:
                    self._update_recipe_costs(phone_number, search_name, unit_cost)

            self.session.reset(phone_number)

            # Trigger CRM hint for large transactions without vendor
            if not vendor and tx_data["amount"] >= 10000 and tx_data["type"] in ("sale", "purchase"):
                return self._trigger_crm_hint(phone_number, tx_id, tx_data)

            return [
                text_response(
                    f"✅ *Saved!*\n\n"
                    f"{format_amount(tx_data['amount'])} {tx_data['type']} recorded."
                ),
                button_response(
                    "What's next?",
                    [
                        {"id": "record_sale", "title": "💰 Record Sale"},
                        {"id": "record_purchase", "title": "📦 Record Purchase"},
                        {"id": "record_expense", "title": "💸 Record Expense"},
                    ]
                )
            ]

        except Exception as e:
            logger.error(f"Save transaction error: {e}\n{traceback.format_exc()}")
            self.session.reset(phone_number)
            return [text_response(f"❌ Error saving: {str(e)[:100]}. Please try again.")]

    def _save_credit_transaction(self, phone_number: str, tx_data: dict) -> list:
        """Save a transaction + record as debt."""
        try:
            vendor = tx_data["vendor"]
            amount = tx_data["amount"]
            tx_type = tx_data["type"]
            description = tx_data["description"]
            raw_text = tx_data.get("raw_text", "").lower()

            # Save the transaction
            result = self.db.save_transaction(
                phone_number,
                int(amount),
                tx_type,
                description,
                tx_data["category"],
                vendor=vendor,
                item_name=description,
                payment_method="credit",
            )
            tx_id = result.get("transaction_id", "") if isinstance(result, dict) else ""

            # Determine direction: who owes whom?
            is_buyer = any(sig in raw_text for sig in BUYER_SIGNALS) or tx_type == "purchase"

            if is_buyer:
                # I owe them
                self.db.record_debt(phone_number, vendor, amount, 'i_owe', f"Credit purchase: {description}")
                self.session.reset(phone_number)
                return [
                    text_response(
                        f"✅ Saved! {format_amount(amount)} purchase on credit.\n"
                        f"📝 You owe *{vendor}* {format_amount(amount)}."
                    ),
                    button_response("What's next?", [
                        {"id": "record_purchase", "title": "📦 Buy More"},
                        {"id": "menu_debts", "title": "💳 View Debts"},
                        {"id": "menu_home", "title": "☰ Menu"},
                    ])
                ]
            else:
                # They owe me — this is a credit sale, offer invoice
                self.db.record_debt(phone_number, vendor, amount, 'owed_to_me', f"Credit sale: {description}")
                self.session.reset(phone_number)
                return [
                    text_response(
                        f"✅ Saved! {format_amount(amount)} sale on credit.\n"
                        f"📝 *{vendor}* owes you {format_amount(amount)}."
                    ),
                    button_response("Generate a document?", [
                        {"id": f"gen_invoice_{tx_id}", "title": "🧾 Invoice"},
                        {"id": f"gen_receipt_{tx_id}", "title": "🧾 Receipt"},
                        {"id": "menu_home", "title": "☰ Menu"},
                    ])
                ]

        except Exception as e:
            logger.error(f"Credit save error: {e}\n{traceback.format_exc()}")
            self.session.reset(phone_number)
            return [text_response(f"❌ Error: {str(e)[:100]}. Please try again.")]

    def _trigger_crm_hint(self, phone_number: str, tx_id: str, tx_data: dict) -> list:
        """Ask who the transaction was with (optional, skippable)."""
        self.session.save(phone_number, states.CRM_HINT, {
            "transaction_id": tx_id,
            "tx_type": tx_data["type"],
            "amount": tx_data["amount"],
            "crm_step": "ask_name",
        })

        label = "sell to" if tx_data["type"] == "sale" else "buy from"
        return [text_response(
            f"✅ *Saved!*\n\n"
            f"{format_amount(tx_data['amount'])} {tx_data['type']} recorded.\n\n"
            f"💡 Who did you {label}?\n\n"
            f"_Type their name, or just send your next transaction._"
        )]

    # ═══════════════════════════════════════════════════════════
    # VARIANT SELECTION — Ask which variant before landing cost
    # ═══════════════════════════════════════════════════════════

    def _check_variants_then_landing_cost(self, phone_number: str, tx_id: str, tx_data: dict) -> list:
        """
        Check if the sold product has variant_stock defined.
        If yes → ask which variant.
        If no → go straight to landing cost.
        """
        description = tx_data.get("description", "")
        brand = tx_data.get("brand", "")
        search_name = f"{brand} {description}".strip() if brand else description

        # Look up the product in catalog
        from features.catalog import CatalogHandler
        cat = CatalogHandler(self.session, self.db)
        products = cat._get_products(phone_number)
        matched_key = cat._find_product_key(products, search_name)

        if matched_key:
            product = products[matched_key]
            variant_stock = product.get("variant_stock", {})

            if variant_stock:
                # Product has variants — check if variant is already known from text
                auto_variant = self._detect_variant(phone_number, search_name)
                if auto_variant and auto_variant in variant_stock:
                    # Variant auto-detected from description — skip asking
                    tx_data["selected_variant"] = auto_variant
                    return self._ask_landing_cost(phone_number, tx_id, tx_data)

                # Ask which variant
                return self._ask_variant(phone_number, tx_id, tx_data, product, matched_key)

        # No variants — go straight to landing cost
        return self._ask_landing_cost(phone_number, tx_id, tx_data)

    def _ask_variant(self, phone_number: str, tx_id: str, tx_data: dict, product: dict, product_key: str) -> list:
        """Show variant selection list to user."""
        variant_stock = product.get("variant_stock", {})
        variant_costs = product.get("variant_costs", {})
        product_name = product.get("name", product_key)
        amount = tx_data.get("amount", 0)

        # Build list rows from variants with stock
        rows = []
        for v_name, v_stock in variant_stock.items():
            v_cost = variant_costs.get(v_name, 0)
            v_stock_int = int(v_stock)
            stock_str = f"Stock: {v_stock_int}"
            cost_str = f" · {format_amount(v_cost)}" if v_cost else ""
            rows.append({
                "id": f"var_{v_name}",
                "title": v_name[:24],
                "description": f"{stock_str}{cost_str}"[:72],
            })

        # Limit to 10 (WhatsApp max)
        rows = rows[:10]

        self.session.save(phone_number, states.VARIANT_SELECTION, {
            "var_tx_id": tx_id,
            "var_tx_data": tx_data,
            "var_product_key": product_key,
        })

        return [list_response(
            header=f"🏷️ Which variant?",
            body=f"💰 {format_amount(amount)} sale — *{product_name}*\n\nSelect the variant sold:",
            button_text="Select Variant",
            sections=[{"title": "Variants", "rows": rows}]
        )]

    def handle_variant_selection(self, phone_number: str, text: str, session: dict) -> list:
        """Handle variant selection — user tapped a variant or typed one."""
        context = session.get("context", {})
        tx_id = context.get("var_tx_id", "")
        tx_data = context.get("var_tx_data", {})
        product_key = context.get("var_product_key", "")
        text_s = text.strip()

        # Extract variant name from button ID
        if text_s.startswith("var_"):
            variant = text_s[4:]  # After "var_"
        else:
            variant = text_s

        # Save variant to tx_data
        tx_data["selected_variant"] = variant

        # Update the transaction with variant info
        if tx_id:
            self.db.update_transaction(phone_number, tx_id, {
                "variant": variant,
            })

        # Proceed to landing cost
        return self._ask_landing_cost(phone_number, tx_id, tx_data)

    def _detect_variant(self, phone_number: str, search_name: str) -> str:
        """
        Try to auto-detect a variant from the product name/description.
        e.g. "Honda Accord 2016 Black" → matches variant "2016 Black"
        Returns variant name or empty string.
        """
        from features.catalog import CatalogHandler
        cat = CatalogHandler(self.session, self.db)
        products = cat._get_products(phone_number)
        matched_key = cat._find_product_key(products, search_name)

        if not matched_key:
            return ""

        product = products[matched_key]
        variant_stock = product.get("variant_stock", {})
        if not variant_stock:
            return ""

        # Check if search_name contains a variant name
        search_lower = search_name.lower()
        product_name_lower = product.get("name", "").lower()

        # Remove product name from search to isolate potential variant text
        remainder = search_lower.replace(product_name_lower, "").strip()

        # Try exact match first
        for v_name in variant_stock:
            if v_name.lower() == remainder:
                return v_name
            if v_name.lower() in search_lower:
                return v_name

        return ""

    # ═══════════════════════════════════════════════════════════
    # LANDING COST — Ask cost after sale, pre-fill from catalog
    # ═══════════════════════════════════════════════════════════

    def _ask_landing_cost(self, phone_number: str, tx_id: str, tx_data: dict) -> list:
        """After saving a sale, ask for the landing cost. Check catalog for pre-fill."""
        description = tx_data.get("description", "")
        brand       = tx_data.get("brand", "")
        catalog_key = tx_data.get("catalog_product", "")
        amount      = tx_data.get("amount", 0)
        quantity    = tx_data.get("quantity", "1")
        selected_variant = tx_data.get("selected_variant", "")

        # Try variant-specific cost first, then fall back to general lookup
        saved_cost = 0
        if selected_variant:
            saved_cost = self._get_variant_landing_cost(phone_number, description, brand, selected_variant)

        if not saved_cost:
            saved_cost = self._get_catalog_landing_cost(phone_number, catalog_key, description, brand)

        # Ensure quantity defaults to "1" if empty (so stock always decrements)
        if not quantity or quantity.strip() == "":
            quantity = "1"

        # Build display name (include variant if selected)
        display_name = description
        if selected_variant:
            display_name = f"{description} ({selected_variant})"

        self.session.save(phone_number, states.LANDING_COST, {
            "lc_tx_id": tx_id,
            "lc_amount": amount,
            "lc_description": description,
            "lc_catalog_key": catalog_key,
            "lc_saved_cost": saved_cost,
            "lc_catalog_path": tx_data.get("catalog_path", []),
            "lc_quantity": quantity,
            "lc_variant": tx_data.get("selected_variant", ""),
        })

        if saved_cost:
            # Catalog has a saved cost — offer as suggestion
            return [button_response(
                f"✅ *Sale saved!* {format_amount(amount)}\n\n"
                f"🏷️ *Landing cost for {display_name}?*\n\n"
                f"Last recorded cost: *{format_amount(saved_cost)}*\n\n"
                f"_Use this or type a different amount._",
                [
                    {"id": "lc_use_saved", "title": f"✅ Use {format_amount(saved_cost)}"},
                    {"id": "lc_skip",      "title": "⏭️ Skip"},
                ]
            )]
        else:
            # No saved cost — ask directly
            return [button_response(
                f"✅ *Sale saved!* {format_amount(amount)}\n\n"
                f"🏷️ *Landing cost for {display_name}?*\n"
                f"_(How much did you buy/source this item for?)_\n\n"
                f"Type the cost amount, or tap Skip.",
                [
                    {"id": "lc_skip", "title": "⏭️ Skip"},
                ]
            )]

    def handle_landing_cost(self, phone_number: str, text: str, session: dict) -> list:
        """Handle landing cost input — text or button. Also handles supplies mode for services."""
        context    = session.get("context", {})
        tx_id      = context.get("lc_tx_id", "")
        amount     = context.get("lc_amount", 0)
        desc       = context.get("lc_description", "")
        catalog_key = context.get("lc_catalog_key", "")
        saved_cost = context.get("lc_saved_cost", 0)
        text_low   = text.lower().strip()
        lc_mode    = context.get("lc_mode", "cost")  # "cost" or "supplies"

        # ── Services mode: handle supplies deduction ──
        if lc_mode == "supplies":
            if text_low in ("skip", "no", "nah", "lc_skip", "none"):
                self.session.reset(phone_number)
                return [
                    text_response("👍 No supplies deducted."),
                    button_response("What's next?", [
                        {"id": f"gen_invoice_{tx_id}", "title": "🧾 Invoice"},
                        {"id": "record_sale", "title": "💼 Next Job"},
                        {"id": "menu_home", "title": "☰ Menu"},
                    ])
                ]
            return self._handle_supplies_deduction(phone_number, text, context)

        # ── Normal landing cost mode ──
        qty = self._parse_qty(context.get("lc_quantity", "1"))

        # ── Skip ──
        if text_low in ("skip", "no", "nah", "lc_skip"):
            # Still decrement inventory even when skipping landing cost
            self._decrement_stock_on_sale(phone_number, desc, qty, context)

            self.session.reset(phone_number)
            return [
                text_response(
                    f"👍 No cost recorded.\n\n"
                    f"_Send your next transaction or tap ☰ Menu._"
                ),
                button_response(
                    "What's next?",
                    [
                        {"id": f"gen_invoice_{tx_id}", "title": "🧾 Invoice"},
                        {"id": f"gen_receipt_{tx_id}", "title": "🧾 Receipt"},
                        {"id": "menu_home", "title": "☰ Menu"},
                    ]
                )
            ]

        # ── Use saved cost from catalog ──
        if text_low == "lc_use_saved" and saved_cost:
            landing_cost = int(saved_cost)
        else:
            # Parse typed amount
            landing_cost_parsed = parse_amount(text)
            if not landing_cost_parsed:
                return [text_response(
                    "💰 Enter the landing cost (e.g. 50000, 150K, 10M):\n\n"
                    "_Or type *skip* to continue without it._"
                )]
            landing_cost = int(landing_cost_parsed)

        # Save landing cost (per unit) to the transaction, plus total cost
        total_cost = landing_cost * qty
        if tx_id:
            self.db.update_transaction(phone_number, tx_id, {
                "landing_cost": total_cost,
                "landing_cost_per_unit": landing_cost,
            })

        # Also update catalog with this cost for future auto-fill
        if catalog_key:
            self._update_catalog_cost(phone_number, catalog_key, landing_cost)

        # Decrement stock on sale
        self._decrement_stock_on_sale(phone_number, desc, qty, context)

        # Calculate and show margin (landing_cost is per unit, multiply by qty)
        margin = int(amount) - total_cost
        margin_pct = int(margin / int(amount) * 100) if int(amount) > 0 else 0

        self.session.reset(phone_number)

        # Show per-unit cost breakdown if qty > 1
        cost_line = f"🏷️ Cost: {format_amount(total_cost)}"
        if qty > 1:
            cost_line += f" ({qty} × {format_amount(landing_cost)})"

        return [
            text_response(
                f"✅ *Cost recorded!*\n\n"
                f"💰 Sold for: {format_amount(amount)}\n"
                f"{cost_line}\n"
                f"📈 Margin: {format_amount(margin)} ({margin_pct}%)"
            ),
            button_response(
                "What's next?",
                [
                    {"id": f"gen_invoice_{tx_id}", "title": "🧾 Invoice"},
                    {"id": f"gen_receipt_{tx_id}", "title": "🧾 Receipt"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ]
            )
        ]

    def _decrement_stock_on_sale(self, phone_number: str, description: str, qty: int, context: dict):
        """Decrement stock when a sale is recorded. Searches catalog by description."""
        try:
            from features.catalog import CatalogHandler
            cat = CatalogHandler(self.session, self.db)
            qty_str = context.get("lc_quantity", "")
            variant = context.get("lc_variant", "")
            result = cat.update_stock(phone_number, description, -qty, quantity_str=qty_str, variant=variant)
            if result.get("matched"):
                logger.info(f"Stock decremented: {description} (variant={variant}) by {qty}, new stock: {result.get('new_stock')}")
            else:
                logger.info(f"No catalog match for sale item: {description}")
        except Exception as e:
            logger.error(f"Error decrementing stock on sale: {e}")

    # ═══════════════════════════════════════════════════════════
    # SUPPLIES USED — Services: deduct consumables after a job
    # ═══════════════════════════════════════════════════════════

    def _ask_supplies_used(self, phone_number: str, tx_id: str, tx_data: dict) -> list:
        """After saving a service job, ask if supplies were used (optional)."""
        amount = tx_data.get("amount", 0)
        description = tx_data.get("description", "Job")

        # Check if user has consumables in catalog
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        consumables = {k: v for k, v in products.items()
                       if v.get("item_type") == "consumable" or int(v.get("stock", 0)) > 0}

        if not consumables:
            # No consumables tracked — just show success
            self.session.reset(phone_number)
            return [
                text_response(f"✅ *Job saved!* {format_amount(amount)}\n\n_{description}_"),
                button_response("What's next?", [
                    {"id": f"gen_invoice_{tx_id}", "title": "🧾 Invoice"},
                    {"id": "record_sale", "title": "💼 Next Job"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]

        # Has consumables — offer to deduct
        self.session.save(phone_number, states.LANDING_COST, {
            "lc_tx_id": tx_id,
            "lc_amount": amount,
            "lc_description": description,
            "lc_mode": "supplies",  # Flag to handle differently
        })

        # Build supply list
        supply_names = [v.get("name", k) for k, v in list(consumables.items())[:5]]
        supply_str = ", ".join(supply_names)

        return [button_response(
            f"✅ *Job saved!* {format_amount(amount)}\n\n"
            f"📦 Did you use any supplies?\n"
            f"_({supply_str})_\n\n"
            f"Type supplies used, e.g.:\n"
            f"_2 blades, 1 oil_\n\n"
            f"Or tap Skip if none used.",
            [
                {"id": "lc_skip", "title": "⏭️ No Supplies"},
                {"id": f"gen_invoice_{tx_id}", "title": "🧾 Invoice"},
            ]
        )]

    def _handle_supplies_deduction(self, phone_number: str, text: str, context: dict) -> list:
        """Parse supplies text like '2 blades, 1 oil' and deduct from catalog."""
        tx_id = context.get("lc_tx_id", "")
        amount = context.get("lc_amount", 0)

        # Parse comma-separated items: "2 blades, 1 oil, 3 gloves"
        items = [item.strip() for item in text.split(",") if item.strip()]
        deducted = []
        total_supply_cost = 0

        from features.catalog import CatalogHandler
        cat = CatalogHandler(self.session, self.db)

        for item in items:
            # Parse "2 blades" or "1 oil" or just "blade"
            match = re.match(r'^(\d+)\s*(.+)', item.strip())
            if match:
                qty = int(match.group(1))
                name = match.group(2).strip()
            else:
                qty = 1
                name = item.strip()

            # Deduct from catalog
            result = cat.update_stock(phone_number, name, -qty)
            if result.get("matched"):
                cost = result.get("landing_cost", 0) * qty
                total_supply_cost += cost
                deducted.append(f"  • {qty} × {result['product']} (-{qty})")

        self.session.reset(phone_number)

        if deducted:
            deduct_str = "\n".join(deducted)
            cost_str = f"\n💰 Supply cost: {format_amount(total_supply_cost)}" if total_supply_cost > 0 else ""

            # Save supply cost to transaction
            if tx_id and total_supply_cost > 0:
                self.db.update_transaction(phone_number, tx_id, {
                    "supply_cost": total_supply_cost,
                })

            return [
                text_response(
                    f"📦 *Supplies deducted:*\n{deduct_str}{cost_str}"
                ),
                button_response("What's next?", [
                    {"id": f"gen_invoice_{tx_id}", "title": "🧾 Invoice"},
                    {"id": "record_sale", "title": "💼 Next Job"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]
        else:
            return [
                text_response("❓ Couldn't match those supplies. No stock deducted."),
                button_response("What's next?", [
                    {"id": "record_sale", "title": "💼 Next Job"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]

    def _get_catalog_landing_cost(self, phone_number: str, catalog_key: str,
                                   description: str, brand: str) -> int:
        """Look up landing cost from the flat product catalog."""
        try:
            from features.catalog import CatalogHandler
            cat = CatalogHandler(self.session, self.db)
            search_name = f"{brand} {description}".strip() if brand else description
            return cat.get_landing_cost(phone_number, search_name)
        except Exception:
            return 0

    def _get_variant_landing_cost(self, phone_number: str, description: str, brand: str, variant: str) -> int:
        """Look up variant-specific landing cost directly from variant_costs."""
        try:
            from features.catalog import CatalogHandler
            cat = CatalogHandler(self.session, self.db)
            search_name = f"{brand} {description}".strip() if brand else description
            products = cat._get_products(phone_number)
            matched_key = cat._find_product_key(products, search_name)
            if not matched_key:
                return 0
            product = products[matched_key]
            variant_costs = product.get("variant_costs", {})
            return int(variant_costs.get(variant, 0))
        except Exception:
            return 0

    def _find_deepest_cost(self, node: dict, description: str, brand: str) -> int:
        """Recursively search tree for the deepest __cost__ that matches."""
        if not isinstance(node, dict):
            return 0

        desc_lower = (description or "").lower()
        brand_lower = (brand or "").lower()
        best_cost = 0

        # Check __cost__ at this level
        if "__cost__" in node:
            best_cost = int(node["__cost__"])

        # Try to match children by name against description/brand
        for key, value in node.items():
            if key.startswith("__"):
                continue
            key_lower = key.lower()
            if key_lower in desc_lower or key_lower == brand_lower:
                if isinstance(value, dict):
                    deeper = self._find_deepest_cost(value, description, brand)
                    if deeper:
                        best_cost = deeper  # Deeper match overrides

        return best_cost

    def _update_catalog_cost(self, phone_number: str, catalog_key: str, cost: int):
        """Update the landing_cost on a catalog product."""
        try:
            user = self.db.get_user(phone_number)
            if not user:
                return
            catalog = user.get("product_catalog", {})
            products = catalog.get("products", {})

            if catalog_key in products:
                products[catalog_key]["landing_cost"] = cost
                from services.database import Database
                db = self.db
                db.update_user_field(phone_number, "product_catalog", catalog)
        except Exception as e:
            logger.error(f"Error updating catalog cost: {e}")

    def _save_cost_to_catalog(self, phone_number: str, tx_data: dict):
        """Auto-save unit_cost from a purchase to the matching catalog product."""
        try:
            unit_cost   = tx_data.get("unit_cost")
            catalog_key = tx_data.get("catalog_product", "")
            description = tx_data.get("description", "")
            brand       = tx_data.get("brand", "")

            if not unit_cost:
                return

            user = self.db.get_user(phone_number)
            if not user:
                return
            catalog = user.get("product_catalog", {})
            products = catalog.get("products", {})

            # Find the matching product
            target_key = None
            if catalog_key and catalog_key in products:
                target_key = catalog_key
            else:
                # Try matching by name
                desc_lower = description.lower()
                for key, data in products.items():
                    name = data.get("name", "").lower()
                    if name and (name in desc_lower or desc_lower in name):
                        target_key = key
                        break

            if target_key:
                products[target_key]["landing_cost"] = int(unit_cost)
                self.db.update_user_field(phone_number, "product_catalog", catalog)
                logger.info(f"Auto-saved landing cost {unit_cost} to catalog product {target_key}")
        except Exception as e:
            logger.error(f"Error auto-saving cost to catalog: {e}")

    # ═══════════════════════════════════════════════════════════
    # RECIPE COST UPDATE — Manufacturing: update recipe costs when materials are purchased
    # ═══════════════════════════════════════════════════════════

    def _update_recipe_costs(self, phone_number: str, material_name: str, unit_cost: int):
        """
        When a raw material is purchased, update cost_per_unit in all recipes
        that use this material. This keeps production cost calculations accurate.
        """
        try:
            user = self.db.get_user(phone_number)
            if not user:
                return
            catalog = user.get("product_catalog", {})
            products = catalog.get("products", {})
            mat_lower = material_name.lower()

            updated = False
            for key, product in products.items():
                recipe = product.get("recipe", [])
                for mat in recipe:
                    mat_name = mat.get("material", "").lower()
                    if mat_name in mat_lower or mat_lower in mat_name:
                        mat["cost_per_unit"] = float(unit_cost)
                        updated = True

            if updated:
                self.db.update_user_field(phone_number, "product_catalog", catalog)
                logger.info(f"Updated recipe costs for material: {material_name} @ {unit_cost}")
        except Exception as e:
            logger.error(f"Error updating recipe costs: {e}")

    # ═══════════════════════════════════════════════════════════
    # INVENTORY — Update stock counts from transactions
    # ═══════════════════════════════════════════════════════════

    def _update_inventory(self, phone_number: str, tx_data: dict, direction: str):
        """
        Update inventory counts in the catalog tree.
        direction: "add" (purchase) or "subtract" (sale)
        """
        try:
            catalog_key  = tx_data.get("catalog_product", "")
            catalog_path = tx_data.get("catalog_path", [])
            quantity_str = tx_data.get("quantity", "")

            if not catalog_key:
                return  # No catalog link — can't update inventory

            # Parse numeric quantity
            qty = self._parse_qty(quantity_str)
            if qty <= 0:
                qty = 1  # Default to 1 if no quantity specified

            user = self.db.get_user(phone_number)
            if not user:
                return
            catalog = user.get("product_catalog", {})
            products = catalog.get("products", {})

            if catalog_key not in products:
                return

            product = products[catalog_key]
            tree = product.setdefault("tree", {})

            if catalog_path:
                # Navigate to the leaf and update quantity
                node = tree
                for step in catalog_path[:-1]:
                    if step not in node:
                        node[step] = {}
                    node = node[step]

                leaf_key = catalog_path[-1]
                current = node.get(leaf_key, 0)
                if not isinstance(current, (int, float)):
                    current = 0

                if direction == "add":
                    node[leaf_key] = current + qty
                else:  # subtract
                    node[leaf_key] = max(0, current - qty)

                # Check for low stock alert
                new_qty = node[leaf_key]
                if direction == "subtract" and new_qty <= 3 and new_qty >= 0:
                    self._low_stock_alert(phone_number, product, catalog_path, new_qty)
            else:
                # No tree path — update product-level stock count
                current = product.get("stock", 0)
                if direction == "add":
                    product["stock"] = current + qty
                else:
                    product["stock"] = max(0, current - qty)

                new_qty = product["stock"]
                if direction == "subtract" and new_qty <= 3:
                    self._low_stock_alert(phone_number, product, [], new_qty)

            self.db.update_user_field(phone_number, "product_catalog", catalog)
            logger.info(f"Inventory {direction}: {catalog_key} qty={qty} path={catalog_path}")

        except Exception as e:
            logger.error(f"Inventory update error: {e}")

    def _parse_qty(self, quantity_str: str) -> int:
        """Extract numeric quantity from strings like '5', '10 pairs', '3 cartons'.
        Returns at least 1 (every transaction involves at least 1 unit).
        """
        if not quantity_str:
            return 1
        import re
        match = re.match(r'^(\d+)', str(quantity_str))
        return int(match.group(1)) if match else 1

    def _low_stock_alert(self, phone_number: str, product: dict, path: list, qty: int):
        """Flag a low stock item — stored for the next session greeting or report."""
        try:
            product_name = product.get("name", "Item")
            path_str = " → ".join(path) if path else product_name
            alert = {
                "product": product_name,
                "path": path_str,
                "quantity": qty,
            }
            # Store in user profile for pickup by greeting/report
            user = self.db.get_user(phone_number)
            if user:
                alerts = user.get("low_stock_alerts", [])
                # Avoid duplicates
                existing = [a for a in alerts if a.get("path") == path_str]
                if not existing:
                    alerts.append(alert)
                    # Keep only latest 10
                    self.db.update_user_field(phone_number, "low_stock_alerts", alerts[-10:])
        except Exception:
            pass  # Non-critical

    def _show_edit_options(self, phone_number: str, tx_data: dict) -> list:
        """Show what can be edited."""
        from utils.whatsapp_ui import list_response
        self.session.save(phone_number, states.AWAITING_CORRECTION, {
            "pending_transaction": tx_data,
            "edit_step": "choose_field",
        })

        rows = [
            {"id": "edit_amount", "title": "💰 Amount", "description": f"Currently: {format_amount(tx_data['amount'])}"},
            {"id": "edit_type", "title": "📝 Type", "description": f"Currently: {tx_data['type']}"},
            {"id": "edit_description", "title": "📦 Description", "description": f"Currently: {tx_data['description'][:50]}"},
            {"id": "edit_category", "title": "📁 Category", "description": f"Currently: {tx_data['category']}"},
        ]
        if tx_data.get("vendor"):
            rows.append({"id": "edit_vendor", "title": "👤 Name", "description": f"Currently: {tx_data['vendor']}"})

        return [list_response(
            header="✏️ Edit Transaction",
            body="What would you like to change?",
            button_text="Select Field",
            sections=[{"title": "Fields", "rows": rows}]
        )]

    # ═══════════════════════════════════════════════════════════
    # CORRECTION — After user taps "Edit"
    # ═══════════════════════════════════════════════════════════

    def handle_correction(self, phone_number: str, text: str, session: dict) -> list:
        """Handle the edit flow — user picks a field, types new value."""
        context = session.get("context", {})
        tx_data = context.get("pending_transaction", {})
        edit_step = context.get("edit_step", "choose_field")
        edit_field = context.get("edit_field", "")

        text_lower = text.lower().strip()

        # Field selection (from list button)
        if edit_step == "choose_field" or text_lower.startswith("edit_"):
            field_map = {
                "edit_amount": "amount",
                "edit_type": "type",
                "edit_description": "description",
                "edit_category": "category",
                "edit_vendor": "vendor",
            }
            field = field_map.get(text_lower, "")
            if field:
                self.session.save(phone_number, states.AWAITING_CORRECTION, {
                    "pending_transaction": tx_data,
                    "edit_step": "enter_value",
                    "edit_field": field,
                })
                prompts = {
                    "amount": "💰 Enter the correct amount:",
                    "type": "📝 Enter type: *sale*, *purchase*, or *expense*",
                    "description": "📦 Enter the correct description:",
                    "category": "📁 Enter the correct category:",
                    "vendor": "👤 Enter the correct name:",
                }
                return [text_response(prompts.get(field, "Enter new value:"))]

            # Unrecognized — show options again
            return self._show_edit_options(phone_number, tx_data)

        # Value entry
        if edit_step == "enter_value" and edit_field:
            if edit_field == "amount":
                amount = parse_amount(text)
                if not amount:
                    return [text_response("Please enter a valid amount (e.g. 50000, 150K):")]
                tx_data["amount"] = float(amount)
                # Recalculate unit_cost if quantity exists
                qty_str = tx_data.get("quantity", "")
                if qty_str:
                    qty_match = re.match(r'^(\d+)', str(qty_str))
                    if qty_match:
                        qty_num = int(qty_match.group(1))
                        if qty_num > 0:
                            tx_data["unit_cost"] = float(amount) / qty_num
            elif edit_field == "type":
                if text_lower in ("sale", "purchase", "expense"):
                    tx_data["type"] = text_lower
                else:
                    return [text_response("Please enter: *sale*, *purchase*, or *expense*")]
            else:
                tx_data[edit_field] = text.strip()

            # Show updated confirmation
            self.session.save(phone_number, states.AWAITING_CONFIRMATION, {
                "pending_transaction": tx_data,
            })
            return self._build_confirmation(tx_data, tx_data.get("has_credit", False))

        # Fallback
        self.session.reset(phone_number)
        return [text_response("👍 Let's start over. Send your transaction again.")]

    # ═══════════════════════════════════════════════════════════
    # GUIDED RECORDING — Button-driven step-by-step
    # ═══════════════════════════════════════════════════════════

    def handle_guided_step(self, phone_number: str, text: str, session: dict) -> list:
        """Handle guided recording flow (button-initiated)."""
        context = session.get("context", {})
        step = context.get("guided_step", "item")
        guided_type = context.get("guided_type", "sale")
        guided_data = context.get("guided_data", {})
        text_lower = text.lower().strip()

        # Skip
        if text_lower in ("skip", "no", "nah"):
            return self._advance_guided(phone_number, step, guided_type, guided_data, skip=True)

        # Back
        if text_lower == "__back__" or text_lower == "back":
            return self._go_back_guided(phone_number, step, guided_type, guided_data)

        # Process current step
        if step == "item":
            guided_data["item"] = text.strip()
            return self._advance_guided(phone_number, "item", guided_type, guided_data)

        if step == "amount":
            amount = parse_amount(text)
            if not amount:
                return [text_response("💰 Please enter the amount (e.g. 50000, 150K, 1.2M):")]
            guided_data["amount"] = float(amount)
            return self._advance_guided(phone_number, "amount", guided_type, guided_data)

        if step == "vendor":
            guided_data["vendor"] = text.strip()
            return self._advance_guided(phone_number, "vendor", guided_type, guided_data)

        if step == "details":
            guided_data["details"] = text.strip()
            return self._advance_guided(phone_number, "details", guided_type, guided_data)

        # Unknown step — build transaction from what we have
        return self._finalize_guided(phone_number, guided_type, guided_data)

    def _advance_guided(self, phone_number: str, completed_step: str, guided_type: str, data: dict, skip: bool = False) -> list:
        """Move to next guided step."""
        # Step order depends on type:
        # sale/purchase: item → amount → vendor → details → confirm
        # expense: item → amount → vendor → confirm (no details needed)
        if guided_type == "expense":
            steps = ["item", "amount", "vendor"]
        else:
            steps = ["item", "amount", "vendor", "details"]

        current_idx = steps.index(completed_step) if completed_step in steps else -1
        next_idx = current_idx + 1

        if next_idx >= len(steps):
            return self._finalize_guided(phone_number, guided_type, data)

        next_step = steps[next_idx]
        industry = self._get_industry(phone_number)

        # Get prompt for next step
        prompt_keys = {
            "amount": "ask_amount",
            "vendor": f"ask_vendor_{guided_type}" if guided_type in ("sale", "purchase") else "ask_vendor_expense",
            "details": "ask_details",
        }
        prompt_key = prompt_keys.get(next_step, "ask_amount")
        prompt = industry.get_guided_prompt(prompt_key) if industry else f"Enter {next_step}:"

        self.session.save(phone_number, states.GUIDED_RECORDING, {
            "guided_type": guided_type,
            "guided_step": next_step,
            "guided_data": data,
        })

        return [text_response(prompt)]

    def _go_back_guided(self, phone_number: str, current_step: str, guided_type: str, data: dict) -> list:
        """Go back one step in guided flow."""
        steps = ["item", "amount", "vendor", "details"]
        current_idx = steps.index(current_step) if current_step in steps else 0

        if current_idx <= 0:
            # Already at first step — cancel
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled. Send a transaction or tap the menu.")]

        prev_step = steps[current_idx - 1]
        # Remove the data for current step
        data.pop(current_step, None)

        industry = self._get_industry(phone_number)
        prompt_key = f"ask_item_{guided_type}" if prev_step == "item" else f"ask_{prev_step}"
        prompt = industry.get_guided_prompt(prompt_key) if industry else f"Enter {prev_step}:"

        self.session.save(phone_number, states.GUIDED_RECORDING, {
            "guided_type": guided_type,
            "guided_step": prev_step,
            "guided_data": data,
        })

        return [text_response(f"⬅️ Back!\n\n{prompt}")]

    def _finalize_guided(self, phone_number: str, guided_type: str, data: dict) -> list:
        """Build a transaction directly from guided data — no AI re-parse needed."""
        item = data.get("item", "item")
        amount = data.get("amount")
        vendor = data.get("vendor", "")
        details = data.get("details", "")

        if not amount:
            self.session.save(phone_number, states.GUIDED_RECORDING, {
                "guided_type": guided_type,
                "guided_step": "amount",
                "guided_data": data,
            })
            return [text_response("💰 How much was it? (e.g. 50000, 150K)")]

        # Clean vendor — filter bad names
        bad_vendors = {"sold", "bought", "paid", "received", "sale", "purchase",
                      "expense", "income", "cash", "transfer", "sell", "buy", "skip"}
        if vendor.lower().strip() in bad_vendors:
            vendor = ""

        # Determine transaction type
        if guided_type == "sale":
            tx_type = "sale"
            category = "Sales & Income"
        elif guided_type == "purchase":
            tx_type = "purchase"
            category = "Goods & Stock"
        else:
            tx_type = "expense"
            category = "Utilities & Services"

        # Extract quantity from item text if present (e.g. "10 hand bags")
        quantity = ""
        import re
        qty_match = re.match(r'^(\d+)\s*(pairs?|pieces?|pcs|bags?|cartons?|dozen|units?|boxes?)?\s*(.+)',
                             item, re.IGNORECASE)
        if qty_match:
            qty_num  = qty_match.group(1)
            qty_unit = qty_match.group(2) or "pieces"
            item     = qty_match.group(3).strip()
            quantity = f"{qty_num} {qty_unit}"

        # Build tx_data directly — structured, no AI needed
        tx_data = {
            "amount": float(amount),
            "type": tx_type,
            "description": item.title(),
            "category": category,
            "vendor": vendor,
            "quantity": quantity,
            "brand": "",
            "unit_cost": None,
            "details": details,
            "raw_text": f"{guided_type} {item} {amount}",
            "has_credit": False,
        }

        # Save state for confirmation
        self.session.save(phone_number, states.AWAITING_CONFIRMATION, {
            "pending_transaction": tx_data,
        })

        return self._build_confirmation(tx_data, False)

    # ═══════════════════════════════════════════════════════════
    # EDIT/DELETE EXISTING — (from report/list view)
    # ═══════════════════════════════════════════════════════════

    def show_edit_list(self, phone_number: str, tx_type: str = None) -> list:
        """Show recent transactions as a tappable list for editing."""
        txns = self.db.get_recent_transactions(phone_number, limit=20)

        if tx_type:
            txns = [t for t in txns if t.get("type") == tx_type]

        if not txns:
            return [text_response("📝 No transactions to edit.")]

        rows = []
        for t in txns[:10]:
            tx_id   = t.get("transaction_id", "")
            desc    = t.get("description", t.get("item_name", "Transaction"))[:22]
            amt     = format_amount(t.get("amount", 0))
            date_s  = t.get("date", "")[-5:]
            vendor  = t.get("vendor", "")
            vendor_s = f" · {vendor}" if vendor else ""

            rows.append({
                "id": f"txedit_{tx_id}",
                "title": f"{desc} — {amt}"[:24],
                "description": f"{date_s}{vendor_s}"[:72],
            })

        return [list_response(
            header="✏️ Edit Transaction",
            body="Tap a record to edit or delete it:",
            button_text="Select Record",
            sections=[{"title": "Recent Records", "rows": rows}]
        )]

    def handle_edit_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle txedit_* and txact_* buttons."""
        state   = session.get("state", "")
        context = session.get("context", {})

        # ── User tapped a specific transaction from the list ──
        if button_id.startswith("txedit_"):
            tx_id = button_id[7:]  # after "txedit_"
            return self._show_edit_actions(phone_number, tx_id)

        # ── User tapped an edit action ──
        if button_id.startswith("txact_"):
            parts = button_id[6:].split("_", 1)  # txact_[action]_[tx_id]
            if len(parts) == 2:
                action, tx_id = parts[0], parts[1]
            else:
                action, tx_id = parts[0], context.get("edit_tx_id", "")
            return self._execute_edit_action(phone_number, action, tx_id, session)

        # ── Delete confirmation ──
        if button_id == "btn_yes" and state == states.DELETE_CONFIRM:
            tx_id = context.get("edit_tx_id", "")
            if tx_id:
                self.db.delete_transaction(phone_number, tx_id)
                self.session.reset(phone_number)
                return [text_response("🗑️ *Deleted!* Transaction removed.\n\n_Send a new transaction or tap the menu._")]
            self.session.reset(phone_number)
            return [text_response("❌ Could not find that transaction.")]

        return self.show_edit_list(phone_number)

    def _show_edit_actions(self, phone_number: str, tx_id: str) -> list:
        """Show edit/delete options for a specific transaction."""
        tx = self.db.get_transaction(phone_number, tx_id)
        if not tx:
            return [text_response("❓ Transaction not found. It may have been deleted.")]

        desc   = tx.get("description", tx.get("item_name", "Transaction"))
        amount = format_amount(tx.get("amount", 0))
        vendor = tx.get("vendor", "")
        date   = tx.get("date", "")
        tx_type = tx.get("type", "")
        quantity = tx.get("quantity", "")
        landing_cost = tx.get("landing_cost", 0)
        payment_method = tx.get("payment_method", "")
        extra = tx.get("extra_details", {}) or {}
        if not landing_cost:
            landing_cost = extra.get("landing_cost", 0)

        type_emoji = {"sale": "💰", "purchase": "📦", "expense": "💸"}.get(tx_type, "📝")

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"✏️  *Edit Transaction*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"{type_emoji} {tx_type.title()} — {amount}",
            f"📦 {desc}",
        ]
        if vendor:
            lines.append(f"👤 {vendor}")
        lines.append(f"📅 {date}")
        if quantity:
            lines.append(f"📐 Qty: {quantity}")
        if landing_cost:
            lines.append(f"🏷️ Cost: {format_amount(landing_cost)}")
        if payment_method:
            lines.append(f"💳 Payment: {payment_method.title()}")
        lines.append("")
        lines.append("_What would you like to change?_")

        # Save tx_id in session for subsequent action
        self.session.save(phone_number, states.EDITING, {
            "edit_tx_id": tx_id,
        })

        # Build full list of editable fields (always show all)
        rows = [
            {"id": f"txact_amount_{tx_id}", "title": "💰 Edit Amount",
             "description": f"Currently: {amount}"},
            {"id": f"txact_desc_{tx_id}", "title": "📦 Edit Description",
             "description": f"Currently: {desc[:40]}"},
            {"id": f"txact_vendor_{tx_id}", "title": "👤 Edit Customer/Vendor",
             "description": f"Currently: {vendor or 'None'}"},
            {"id": f"txact_date_{tx_id}", "title": "📅 Edit Date",
             "description": f"Currently: {date}"},
            {"id": f"txact_type_{tx_id}", "title": "🔄 Change Type",
             "description": f"Currently: {tx_type.title()}"},
            {"id": f"txact_quantity_{tx_id}", "title": "📐 Edit Quantity",
             "description": f"Currently: {quantity or 'Not set'}"},
            {"id": f"txact_cost_{tx_id}", "title": "🏷️ Edit Landing Cost",
             "description": f"Currently: {format_amount(landing_cost) if landing_cost else 'Not set'}"},
            {"id": f"txact_payment_{tx_id}", "title": "💳 Payment Method",
             "description": f"Currently: {payment_method.title() if payment_method else 'Not set'}"},
            {"id": f"txact_delete_{tx_id}", "title": "🗑️ Delete",
             "description": "Remove this record permanently"},
        ]

        return [
            text_response("\n".join(lines)),
            list_response(
                header="✏️ Actions",
                body="Pick an action:",
                button_text="Select Action",
                sections=[{"title": "Edit Options", "rows": rows}]
            )
        ]

    def _execute_edit_action(self, phone_number: str, action: str, tx_id: str, session: dict) -> list:
        """Execute a specific edit action or prompt for new value."""
        context = session.get("context", {})

        # ── Delete ──
        if action == "delete":
            self.session.save(phone_number, states.DELETE_CONFIRM, {
                "edit_tx_id": tx_id,
            })
            return [button_response(
                "⚠️ *Delete this transaction?*\n\n_This cannot be undone._",
                [
                    {"id": "btn_yes", "title": "🗑️ Yes, Delete"},
                    {"id": "btn_no",  "title": "← Keep It"},
                ]
            )]

        # ── Edit Amount ──
        if action == "amount":
            self.session.save(phone_number, states.EDIT_TRANSACTION, {
                "edit_tx_id": tx_id,
                "edit_field": "amount",
            })
            return [text_response("💰 Enter the correct amount:\n\n_(e.g. 50000, 150K, 1.2M)_")]

        # ── Edit Description ──
        if action == "desc":
            self.session.save(phone_number, states.EDIT_TRANSACTION, {
                "edit_tx_id": tx_id,
                "edit_field": "description",
            })
            return [text_response("📦 Enter the correct description:\n\n_(e.g. Nike Shoes, Red Bags)_")]

        # ── Edit Vendor ──
        if action == "vendor":
            self.session.save(phone_number, states.EDIT_TRANSACTION, {
                "edit_tx_id": tx_id,
                "edit_field": "vendor",
            })
            return [text_response("👤 Enter the correct name:\n\n_(customer or supplier name)_")]

        # ── Edit Date ──
        if action == "date":
            self.session.save(phone_number, states.EDIT_TRANSACTION, {
                "edit_tx_id": tx_id,
                "edit_field": "date",
            })
            return [text_response("📅 Enter the correct date:\n\n_Format: YYYY-MM-DD (e.g. 2026-07-15)_\n_Or: DD/MM/YYYY (e.g. 15/07/2026)_")]

        # ── Change Type ──
        if action == "type":
            self.session.save(phone_number, states.EDIT_TRANSACTION, {
                "edit_tx_id": tx_id,
                "edit_field": "type",
            })
            return [button_response(
                "🔄 What type should this be?",
                [
                    {"id": "txact_settype_sale", "title": "💰 Sale"},
                    {"id": "txact_settype_purchase", "title": "📦 Purchase"},
                    {"id": "txact_settype_expense", "title": "💸 Expense"},
                ]
            )]

        # ── Edit Quantity ──
        if action == "quantity":
            self.session.save(phone_number, states.EDIT_TRANSACTION, {
                "edit_tx_id": tx_id,
                "edit_field": "quantity",
            })
            return [text_response("📐 Enter the quantity:\n\n_(e.g. 5, 10 pairs, 3 cartons)_")]

        # ── Edit Landing Cost ──
        if action == "cost":
            self.session.save(phone_number, states.EDIT_TRANSACTION, {
                "edit_tx_id": tx_id,
                "edit_field": "landing_cost",
            })
            return [text_response("🏷️ Enter the landing cost per unit:\n\n_(e.g. 50000, 150K, 19M)_")]

        # ── Edit Payment Method ──
        if action == "payment":
            self.session.save(phone_number, states.EDIT_TRANSACTION, {
                "edit_tx_id": tx_id,
                "edit_field": "payment_method",
            })
            return [button_response(
                "💳 Select payment method:",
                [
                    {"id": "txact_setpay_cash", "title": "💵 Cash"},
                    {"id": "txact_setpay_transfer", "title": "🏦 Transfer/POS"},
                    {"id": "txact_setpay_credit", "title": "📝 On Credit"},
                ]
            )]

        # ── Set type from button ──
        if action == "settype":
            new_type = tx_id  # in this case tx_id holds "sale"/"purchase"/"expense"
            real_tx_id = context.get("edit_tx_id", "")
            if real_tx_id:
                self.db.update_transaction(phone_number, real_tx_id, {"type": new_type})
                self.session.reset(phone_number)
                return [text_response(f"✅ Type changed to *{new_type.title()}*!")]
            self.session.reset(phone_number)
            return [text_response("❌ Something went wrong.")]

        # ── Set payment method from button ──
        if action == "setpay":
            method_map = {"cash": "cash", "transfer": "transfer", "credit": "credit"}
            new_method = method_map.get(tx_id, tx_id)  # tx_id holds "cash"/"transfer"/"credit"
            real_tx_id = context.get("edit_tx_id", "")
            if real_tx_id:
                self.db.update_transaction(phone_number, real_tx_id, {"payment_method": new_method})
                self.session.reset(phone_number)
                return [text_response(f"✅ Payment method updated to *{new_method.title()}*!")]
            self.session.reset(phone_number)
            return [text_response("❌ Something went wrong.")]

        self.session.reset(phone_number)
        return [text_response("❓ Unknown action.")]

    def handle_edit(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during edit flows (amount, description, vendor)."""
        context = session.get("context", {})
        state   = session.get("state", "")
        tx_id   = context.get("edit_tx_id", "")
        field   = context.get("edit_field", "")

        if text.lower() in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 No changes made.")]

        # ── Delete confirmation (text-based) ──
        if state == states.DELETE_CONFIRM:
            if text.lower() in ("yes", "y", "confirm"):
                if tx_id:
                    self.db.delete_transaction(phone_number, tx_id)
                    self.session.reset(phone_number)
                    return [text_response("🗑️ *Deleted!*")]
            self.session.reset(phone_number)
            return [text_response("👍 Kept it.")]

        # ── Edit field value ──
        if state == states.EDIT_TRANSACTION and tx_id and field:
            if field == "amount":
                new_amount = parse_amount(text)
                if not new_amount:
                    return [text_response("💰 Enter a valid amount (e.g. 50000, 150K):")]
                self.db.update_transaction(phone_number, tx_id, {"amount": int(new_amount)})
                self.session.reset(phone_number)
                return self._edit_success(f"✅ Amount updated to *{format_amount(new_amount)}*!")

            elif field == "description":
                self.db.update_transaction(phone_number, tx_id, {
                    "description": text.strip(),
                    "item_name": text.strip(),
                })
                self.session.reset(phone_number)
                return self._edit_success(f"✅ Description updated to *{text.strip()}*!")

            elif field == "vendor":
                self.db.update_transaction(phone_number, tx_id, {"vendor": text.strip()})
                self.session.reset(phone_number)
                return self._edit_success(f"✅ Vendor updated to *{text.strip()}*!")

            elif field == "date":
                # Parse multiple date formats
                import re
                date_str = text.strip()
                # Try YYYY-MM-DD
                if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
                    parsed_date = date_str
                # Try DD/MM/YYYY or DD-MM-YYYY
                elif re.match(r'^\d{1,2}[/\-]\d{1,2}[/\-]\d{4}$', date_str):
                    parts = re.split(r'[/\-]', date_str)
                    parsed_date = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
                else:
                    return [text_response(
                        "📅 Invalid format. Use:\n\n"
                        "_YYYY-MM-DD (e.g. 2026-07-15)_\n"
                        "_DD/MM/YYYY (e.g. 15/07/2026)_"
                    )]
                self.db.update_transaction(phone_number, tx_id, {"date": parsed_date})
                self.session.reset(phone_number)
                return self._edit_success(f"✅ Date updated to *{parsed_date}*!")

            elif field == "type":
                if text.lower() in ("sale", "purchase", "expense"):
                    self.db.update_transaction(phone_number, tx_id, {"type": text.lower()})
                    self.session.reset(phone_number)
                    return self._edit_success(f"✅ Type changed to *{text.title()}*!")
                return [text_response("Enter: *sale*, *purchase*, or *expense*")]

            elif field == "quantity":
                self.db.update_transaction(phone_number, tx_id, {"quantity": text.strip()})
                self.session.reset(phone_number)
                return self._edit_success(f"✅ Quantity updated to *{text.strip()}*!")

            elif field == "landing_cost":
                cost = parse_amount(text)
                if not cost:
                    return [text_response("🏷️ Enter a valid amount (e.g. 50000, 150K, 19M):")]
                self.db.update_transaction(phone_number, tx_id, {"landing_cost": int(cost)})
                self.session.reset(phone_number)
                return self._edit_success(f"✅ Landing cost updated to *{format_amount(cost)}*!")

            elif field == "payment_method":
                method = text.lower().strip()
                if method in ("cash", "transfer", "pos", "credit", "on credit"):
                    if method in ("on credit",):
                        method = "credit"
                    if method == "pos":
                        method = "transfer"
                    self.db.update_transaction(phone_number, tx_id, {"payment_method": method})
                    self.session.reset(phone_number)
                    return self._edit_success(f"✅ Payment method updated to *{method.title()}*!")
                return [text_response("Enter: *cash*, *transfer*, or *credit*")]

        # Fallback
        self.session.reset(phone_number)
        return [text_response("👍 Send a transaction or tap the menu.")]

    # ═══════════════════════════════════════════════════════════
    # CATALOG-AWARE RECORDING — Walk product tree then record
    # ═══════════════════════════════════════════════════════════

    def handle_catalog_recording(self, phone_number: str, text: str, session: dict) -> list:
        """Handle catalog-based recording — product selection + tree walk + amount."""
        context  = session.get("context", {})
        step     = context.get("cat_rec_step", "pick_product")
        tx_type  = context.get("cat_rec_type", "sale")
        text_s   = text.strip()
        text_low = text_s.lower()

        if text_low in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled. Send a transaction or tap the menu.")]

        # ── Step: pick_product (user tapped a product from the list) ──
        if step == "pick_product":
            return self._catrec_pick_product(phone_number, text_s, context)

        # ── Step: walk_tree (user is navigating format levels) ──
        if step in ("walk_tree", "walk_tree_new"):
            return self._catrec_walk_tree(phone_number, text_s, context)

        # ── Step: ask_quantity ──
        if step == "ask_quantity":
            return self._catrec_quantity(phone_number, text_s, context)

        # ── Step: ask_amount ──
        if step == "ask_amount":
            amount = parse_amount(text_s)
            if not amount:
                return [text_response("💰 Please enter the amount (e.g. 50000, 150K, 1.2M):")]
            context["cat_rec_amount"] = float(amount)
            context["cat_rec_step"] = "ask_vendor"
            self.session.save(phone_number, states.CATALOG_RECORDING, context)
            label = "sell to" if tx_type == "sale" else "buy from"
            return [text_response(f"👤 Who did you {label}?\n\n_Type their name, or *skip*_")]

        # ── Step: ask_vendor ──
        if step == "ask_vendor":
            vendor = "" if text_low in ("skip", "no", "nah") else text_s
            # Clean bad vendor names
            bad = {"sold", "bought", "paid", "received", "skip", "no"}
            if vendor.lower() in bad:
                vendor = ""
            context["cat_rec_vendor"] = vendor
            # Finalize — build confirmation
            return self._catrec_finalize(phone_number, context)

        # Fallback
        self.session.reset(phone_number)
        return [text_response("Something went wrong. Send a transaction or tap the menu.")]

    def _catrec_pick_product(self, phone_number: str, text: str, context: dict) -> list:
        """Handle product selection — flat catalog, go straight to quantity."""
        tx_type = context.get("cat_rec_type", "sale")

        # "Other" option — fall back to free-text
        if text == "catrec___other__" or text.lower() == "other":
            self.session.reset(phone_number)
            industry = self._get_industry(phone_number)
            prompt = "📦 What did you sell/buy?\n\n_Type it manually:_"
            if industry:
                key = "ask_item_sale" if tx_type == "sale" else "ask_item_purchase"
                prompt = industry.get_guided_prompt(key)
            self.session.save(phone_number, states.GUIDED_RECORDING, {
                "guided_type": tx_type,
                "guided_step": "item",
                "guided_data": {},
            })
            return [text_response(prompt)]

        # Extract product key from button ID
        product_key = text.replace("catrec_", "") if text.startswith("catrec_") else text.lower().replace(" ", "_")

        # Get catalog
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})

        if product_key not in products:
            for k, v in products.items():
                if isinstance(v, dict) and v.get("name", "").lower() == text.lower():
                    product_key = k
                    break
            else:
                return [text_response("❓ Product not found. Please pick from the list.")]

        product = products[product_key]
        product_name = product.get("name", product_key)

        context["cat_rec_product_key"] = product_key
        context["cat_rec_product_name"] = product_name

        # Go straight to quantity (no tree walk)
        context["cat_rec_step"] = "ask_quantity"
        self.session.save(phone_number, states.CATALOG_RECORDING, context)

        stock = int(product.get("stock", 0))
        stock_str = f"\n📊 Current stock: {stock}" if stock > 0 else ""

        return [text_response(
            f"📦 *{product_name}*{stock_str}\n\n"
            f"📐 How many?\n\n_e.g. 5, 10, 3. Or type *skip*_"
        )]

    def _catrec_walk_tree(self, phone_number: str, text: str, context: dict) -> list:
        """Handle tree level navigation — user picked a value, show next level."""
        user = self.db.get_user(phone_number)
        catalog = user.get("product_catalog", {}) if user else {}
        products = catalog.get("products", {})
        product_key = context.get("cat_rec_product_key", "")
        product = products.get(product_key, {})
        pattern = product.get("pattern", [])
        tree = product.get("tree", {})
        path = context.get("cat_rec_path", [])
        selections = context.get("cat_rec_selections", {})

        # Extract value from button ID (catrec_val_[value]) or raw text
        if text.startswith("catrec_val_"):
            value = text[11:]  # after "catrec_val_"
        elif text == "catrec_stop_here":
            # User wants to stop here — skip remaining levels, go to quantity
            context["cat_rec_step"] = "ask_quantity"
            self.session.save(phone_number, states.CATALOG_RECORDING, context)
            product_name = context.get("cat_rec_product_name", "item")
            path_str = " → ".join(path) if path else product_name
            return [text_response(
                f"📦 *{product_name}* → {path_str}\n\n"
                f"📐 How many?\n\n_e.g. 5, 10 pairs, 3 cartons. Or type *skip*_"
            )]
        elif text.startswith("catrec_new_"):
            # User wants to add new value — ask them to type it
            context["cat_rec_step"] = "walk_tree_new"
            self.session.save(phone_number, states.CATALOG_RECORDING, context)
            level_name = pattern[len(path)] if len(path) < len(pattern) else "value"
            return [text_response(f"✏️ Type the new *{level_name}*:")]
        elif context.get("cat_rec_step") == "walk_tree_new":
            # They typed a new value — use it (normalize to Title Case)
            value = text.strip().title()
            context["cat_rec_step"] = "walk_tree"
        else:
            value = text.strip().title()

        # Record this selection
        current_level = len(path)
        if current_level < len(pattern):
            level_name = pattern[current_level]
            selections[level_name] = value
            path.append(value)

        context["cat_rec_path"] = path
        context["cat_rec_selections"] = selections

        # Check if we've completed all levels
        if len(path) >= len(pattern):
            # Tree walk complete — ask quantity
            context["cat_rec_step"] = "ask_quantity"
            self.session.save(phone_number, states.CATALOG_RECORDING, context)
            product_name = context.get("cat_rec_product_name", "item")
            path_str = " → ".join(path)
            return [text_response(
                f"📦 *{product_name}* → {path_str}\n\n"
                f"📐 How many?\n\n_e.g. 5, 10 pairs, 3 cartons. Or type *skip*_"
            )]

        # More levels to show — display next level
        self.session.save(phone_number, states.CATALOG_RECORDING, context)
        return self._catrec_show_tree_level(phone_number, product, context)

    def _catrec_show_tree_level(self, phone_number: str, product: dict, context: dict) -> list:
        """Show the current tree level as a WhatsApp list menu."""
        pattern = product.get("pattern", [])
        tree = product.get("tree", {})
        path = context.get("cat_rec_path", [])
        product_name = context.get("cat_rec_product_name", "Product")

        current_level = len(path)
        level_name = pattern[current_level] if current_level < len(pattern) else "Value"

        # Navigate into tree to current position
        node = tree
        for step in path:
            if isinstance(node, dict) and step in node:
                node = node[step]
            else:
                node = {}
                break

        # Build rows from existing values at this level
        rows = []
        if isinstance(node, dict):
            for value in list(node.keys())[:9]:
                if str(value).startswith("__"):
                    continue  # Skip meta keys (__cost__, __stock__)
                rows.append({
                    "id": f"catrec_val_{value}",
                    "title": str(value)[:24],
                    "description": f"Select {level_name}: {value}"[:72],
                })

        # Add "New" option
        rows.append({
            "id": "catrec_new_value",
            "title": f"➕ New {level_name}",
            "description": f"Type a new {level_name.lower()}"[:72],
        })

        # Add "Stop Here" option — skip remaining levels, go to quantity
        rows.append({
            "id": "catrec_stop_here",
            "title": "✅ Stop Here",
            "description": "Skip remaining levels, enter amount",
        })

        # Path display
        if path:
            path_str = f"{product_name} → {' → '.join(path)}"
        else:
            path_str = product_name

        return [list_response(
            header=f"🏷️ {level_name}",
            body=f"📍 {path_str}\n\nSelect {level_name.lower()}:",
            button_text=f"Select {level_name}",
            sections=[{"title": level_name, "rows": rows}]
        )]

    def _catrec_quantity(self, phone_number: str, text: str, context: dict) -> list:
        """Handle quantity input in catalog recording."""
        if text.lower() in ("skip", "no", "nah"):
            context["cat_rec_quantity"] = ""
        else:
            context["cat_rec_quantity"] = text.strip()

        context["cat_rec_step"] = "ask_amount"
        self.session.save(phone_number, states.CATALOG_RECORDING, context)
        return [text_response("💰 How much total?\n\n_(e.g. 50000, 150K, 1.2M)_")]

    def _catrec_finalize(self, phone_number: str, context: dict) -> list:
        """Build tx_data from catalog recording and show confirmation."""
        tx_type      = context.get("cat_rec_type", "sale")
        product_name = context.get("cat_rec_product_name", "Item")
        product_key  = context.get("cat_rec_product_key", "")
        path         = context.get("cat_rec_path", [])
        selections   = context.get("cat_rec_selections", {})
        quantity     = context.get("cat_rec_quantity", "")
        amount       = context.get("cat_rec_amount", 0)
        vendor       = context.get("cat_rec_vendor", "")

        # Build description from selections
        description = product_name
        brand = selections.get("Brand", selections.get("brand", ""))
        model = selections.get("Name", selections.get("Model", selections.get("name", "")))
        colour = selections.get("Colour", selections.get("Color", selections.get("colour", "")))

        # Build a detail string from all non-brand/model selections
        detail_parts = []
        for key, val in selections.items():
            if key.lower() not in ("brand", "name", "model"):
                detail_parts.append(f"{key}: {val}")
        details = ", ".join(detail_parts) if detail_parts else ""

        # Calculate unit cost if quantity is numeric
        unit_cost = None
        if quantity and amount:
            import re
            qty_match = re.match(r'^(\d+)', str(quantity))
            if qty_match:
                qty_num = int(qty_match.group(1))
                if qty_num > 0:
                    unit_cost = amount / qty_num

        # Category based on type
        if tx_type == "sale":
            category = "Sales & Income"
        elif tx_type == "purchase":
            category = "Goods & Stock"
        else:
            category = "Utilities & Services"

        # Build the full description line
        if model and brand:
            full_desc = f"{brand} {model}"
        elif brand:
            full_desc = f"{brand} {description}"
        elif model:
            full_desc = f"{description} {model}"
        else:
            full_desc = description

        tx_data = {
            "amount": float(amount),
            "type": tx_type,
            "description": full_desc,
            "category": category,
            "vendor": vendor,
            "quantity": quantity,
            "brand": brand,
            "unit_cost": unit_cost,
            "details": details,
            "raw_text": f"{tx_type} {full_desc} {amount}",
            "has_credit": False,
            # Catalog linkage data
            "catalog_product": product_key,
            "catalog_path": path,
            "catalog_selections": selections,
        }

        # Save state for confirmation
        self.session.save(phone_number, states.AWAITING_CONFIRMATION, {
            "pending_transaction": tx_data,
        })

        return self._build_confirmation(tx_data, False)

    # ═══════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════

    def _match_to_catalog(self, phone_number: str, description: str, brand: str, text_lower: str) -> dict:
        """
        Try to match a free-text transaction to a catalog product.
        Returns {"product_key": "...", "product_name": "..."} or None.
        """
        try:
            user = self.db.get_user(phone_number)
            if not user:
                return None
            catalog = user.get("product_catalog", {})
            products = catalog.get("products", {})
            if not products:
                return None

            desc_lower = (description or "").lower()
            brand_lower = (brand or "").lower()

            # Strategy 1: exact product name match in description or text
            for key, data in products.items():
                name = data.get("name", key).lower()
                if name in text_lower or name in desc_lower:
                    return {"product_key": key, "product_name": data.get("name", key)}

            # Strategy 2: brand match against subcategories
            if brand_lower:
                for key, data in products.items():
                    subcats = data.get("subcategories", {})
                    for sub_name in subcats:
                        if sub_name.lower() == brand_lower:
                            return {"product_key": key, "product_name": data.get("name", key)}

            # Strategy 3: check tree values for brand/model matches
            if brand_lower:
                for key, data in products.items():
                    tree = data.get("tree", {})
                    if brand_lower in [k.lower() for k in tree.keys()]:
                        return {"product_key": key, "product_name": data.get("name", key)}

            return None
        except Exception:
            return None

    def _edit_success(self, message: str) -> list:
        """Return edit success message with next action buttons."""
        return [
            text_response(message),
            button_response("What's next?", [
                {"id": "record_sale", "title": "💰 Record Sale"},
                {"id": "record_purchase", "title": "📦 Record Purchase"},
                {"id": "menu_report", "title": "📊 Reports"},
            ])
        ]

    def _is_payment(self, text_lower: str) -> bool:
        """Check if text looks like a debt payment rather than a new transaction."""
        # Pattern: "[name] paid [amount]" or "received [amount] from [name]"
        payment_patterns = [
            r'\b\w+\s+paid\s+\d',
            r'\breceived\s+\d',
            r'\b\w+\s+settled\s+\d',
            r'\b\w+\s+cleared\s+\d',
        ]
        return any(re.search(p, text_lower) for p in payment_patterns)

    def _handle_payment_text(self, phone_number: str, text: str) -> list:
        """Route payment text to debt handler. Returns marker for router."""
        # This will be routed to debt.handle_payment by the router
        # For now, save state and return marker
        self.session.save(phone_number, states.DEBT_PAYMENT, {
            "payment_text": text,
        })
        return [{"type": "__ROUTE_TO_DEBT__", "content": text}]
