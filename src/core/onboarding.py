# src/core/onboarding.py
"""Onboarding flow — new user registration + industry selection."""

import logging
from core.states import NEW_USER, ONBOARDING, IDLE
from utils.whatsapp_ui import text_response, button_response, list_response

logger = logging.getLogger(__name__)

# Onboarding steps
STEP_WELCOME = "welcome"
STEP_BUSINESS_NAME = "business_name"
STEP_INDUSTRY = "industry"
STEP_WHAT_YOU_DO = "what_you_do"
STEP_LIST_PRODUCTS = "list_products"
STEP_COMPLETE = "complete"

# Button IDs that can leak in as text if a user taps a stale button mid-onboarding.
_BUTTON_PREFIXES = (
    "menu_", "record_", "sec_", "pi_", "biz_", "cat_", "crm_",
    "set_", "report_", "export_", "gen_", "txedit_", "txact_",
    "debt_", "lc_", "pm_", "prod_", "rec_", "confirm_", "btn_",
    "catrec_", "industry_", "var_", "quote_", "expclass_",
)

# Commands/keywords that are never a valid free-text answer.
_COMMAND_WORDS = {
    "menu", "help", "hi", "hello", "hey", "cancel", "back",
    "done", "skip", "yes", "no", "ok", "okay",
}


def _looks_like_button_or_command(text: str) -> bool:
    """True if the input is a stray button ID or a command word, not a real answer."""
    t = (text or "").lower().strip()
    if not t:
        return True
    if any(t.startswith(p) for p in _BUTTON_PREFIXES):
        return True
    return t in _COMMAND_WORDS


class OnboardingHandler:
    """Handles new user registration flow."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Route to correct onboarding step."""
        state = session.get("state", NEW_USER)
        context = session.get("context", {})
        step = context.get("onboarding_step", STEP_WELCOME)

        if state == NEW_USER or step == STEP_WELCOME:
            return self._welcome(phone_number)

        if step == STEP_BUSINESS_NAME:
            return self._save_business_name(phone_number, text)

        if step == STEP_INDUSTRY:
            return self._save_industry(phone_number, text)

        if step == STEP_WHAT_YOU_DO:
            return self._save_what_you_do(phone_number, text)

        if step == STEP_LIST_PRODUCTS:
            return self._save_product_list(phone_number, text)

        # Fallback — restart onboarding
        return self._welcome(phone_number)

    def _welcome(self, phone_number: str) -> list:
        """Show welcome message + ask for business name."""
        self.session.save(phone_number, ONBOARDING, {
            "onboarding_step": STEP_BUSINESS_NAME
        })

        # Name the platform the user is actually on (WhatsApp or Telegram).
        from services.messaging_client import platform_for_user
        platform_name = "Telegram" if platform_for_user(phone_number) == "telegram" else "WhatsApp"

        return [text_response(
            "👋 Welcome to *Kashia*!\n\n"
            "I'm your AI bookkeeper. I'll help you track sales, expenses, "
            f"debts, and more — all right here on {platform_name}.\n\n"
            "Let's get you set up in 30 seconds.\n\n"
            "*Step 1 of 3*\n"
            "📝 *What's your business name?*"
        )]

    def _save_business_name(self, phone_number: str, text: str) -> list:
        """Save business name, ask for industry."""
        business_name = text.strip()

        if len(business_name) < 2:
            return [text_response("Please enter your business name (at least 2 characters):")]

        if len(business_name) > 100:
            return [text_response("That's too long! Please use a shorter business name:")]

        # Reject stray button IDs / command words that aren't a real name.
        if _looks_like_button_or_command(business_name):
            return [text_response(
                "📝 *What's your business name?*\n\n"
                "_e.g. Sandra's Fashion, Alhaji Motors, ABC Electronics_"
            )]

        # Save to context (will write to users table at the end)
        self.session.save(phone_number, ONBOARDING, {
            "onboarding_step": STEP_INDUSTRY,
            "business_name": business_name,
        })

        return [list_response(
            header="🏢 " + business_name,
            body="*Step 2 of 3*\nWhat type of industry are you in?",
            button_text="Select Industry",
            sections=[{
                "title": "Choose your industry",
                "rows": [
                    {
                        "id": "industry_trading",
                        "title": "🛍️ Trading & Retail",
                        "description": "Buy and sell goods (shop, market, online store)"
                    },
                    {
                        "id": "industry_manufacturing",
                        "title": "🏭 Manufacturing",
                        "description": "Produce/make goods (factory, workshop, food)"
                    },
                    {
                        "id": "industry_services",
                        "title": "💼 Services",
                        "description": "Provide services (cleaning, consulting, repair)"
                    },
                    {
                        "id": "industry_hybrid",
                        "title": "🔄 Hybrid / Mixed",
                        "description": "Combination of goods + services"
                    },
                ]
            }]
        )]

    def _save_industry(self, phone_number: str, text: str) -> list:
        """Save industry, then ask what they do in natural language."""
        # Map button IDs to industry keys
        industry_map = {
            "industry_trading": "trading",
            "industry_manufacturing": "manufacturing",
            "industry_services": "services",
            "industry_hybrid": "hybrid",
            "trading": "trading",
            "manufacturing": "manufacturing",
            "services": "services",
            "hybrid": "hybrid",
            "1": "trading",
            "2": "manufacturing",
            "3": "services",
            "4": "hybrid",
        }

        industry = industry_map.get(text.lower().strip())

        if not industry:
            return [text_response(
                "Please select an industry from the list above, "
                "or type: trading, manufacturing, services, or hybrid"
            )]

        # Get business name from context
        context = self.session.get_context(phone_number)
        business_name = context.get("business_name", "My Business")

        # Save industry to context, ask what they do naturally
        self.session.save(phone_number, ONBOARDING, {
            "onboarding_step": STEP_WHAT_YOU_DO,
            "business_name": business_name,
            "industry": industry,
        })

        # Step 3: general list of what they sell/make/offer, comma-separated.
        # These become STARTING CATEGORIES to expand in the Catalog — not priced
        # products. So we ask for the broad things, not specific SKUs.
        _skip_hint = "\n\n_Separate with commas. Or type *skip* — you can set up your products in the Catalog anytime._"
        prompts = {
            "trading": (
                "*Step 3 of 3*\n"
                "🛍️ Great! *What do you sell?*\n\n"
                "List the main things (we'll set up details like models, prices "
                "and stock next):\n\n"
                "_e.g. Cars, Trucks, Buses_\n"
                "_e.g. Shoes, Bags, Accessories_\n"
                "_e.g. Rice, Oil, Provisions_" + _skip_hint
            ),
            "manufacturing": (
                "*Step 3 of 3*\n"
                "🏭 Great! *What do you make?*\n\n"
                "List your main products (details come next):\n\n"
                "_e.g. Soap, Detergent, Bleach_\n"
                "_e.g. Bread, Cakes, Pastries_\n"
                "_e.g. Tables, Chairs, Cabinets_" + _skip_hint
            ),
            "services": (
                "*Step 3 of 3*\n"
                "💼 Great! *What services do you offer?*\n\n"
                "List them (you'll set rates next):\n\n"
                "_e.g. Braiding, Nails, Makeup_\n"
                "_e.g. Cleaning, Fumigation_\n"
                "_e.g. Web Design, Marketing_" + _skip_hint
            ),
            "hybrid": (
                "*Step 3 of 3*\n"
                "🔄 Great! *What do you sell and/or offer?*\n\n"
                "List the main things (details come next):\n\n"
                "_e.g. Phones, Phone Repairs_\n"
                "_e.g. Food Items, Catering_" + _skip_hint
            ),
        }

        return [text_response(prompts.get(industry, prompts["trading"]))]

    def _save_what_you_do(self, phone_number: str, text: str) -> list:
        """Step 3 (final): take the general 'what do you sell/make/offer' as a
        comma-separated list of GENERAL terms, seed them as category placeholders
        (name + item_type only — NO price/stock/unit), then complete and hand off
        to the Catalog where real products get built.

        Design (docs/TG_ONBOARDING_PLAN.md): "I sell cars, trucks, buses" means
        the LINE OF BUSINESS, not 3 priced SKUs. We store them as starting
        categories to expand in the Catalog — never as priced products here.
        """
        context = self.session.get_context(phone_number)
        business_name = context.get("business_name", "My Business")
        industry = context.get("industry", "trading")
        description = text.strip()

        # Let users skip if they'd rather build the catalog later.
        if description.lower() in ("skip", "later", "not now"):
            return self._complete_onboarding(phone_number, business_name, industry, "", [])

        # Reject stray button taps / commands saved verbatim (e.g. "menu_home").
        if _looks_like_button_or_command(description):
            return [text_response(
                f"Just list what you {self._sell_verb(industry)} (separate with commas).\n\n"
                f"_e.g. \"{self._what_you_do_example(industry)}\"_\n\n"
                f"_Or type *skip* to set this up in the Catalog later._"
            )]

        if len(description) < 2:
            return [text_response(
                f"Please list what you {self._sell_verb(industry)} (even one is fine),\n"
                "_or type *skip* to set it up in the Catalog later._"
            )]

        # Take the comma list at face value as starting CATEGORIES (no prose
        # auto-guessing beyond a light clean). These are placeholders only.
        items = self._parse_category_list(description)
        return self._complete_onboarding(phone_number, business_name, industry, description, items)

    def _parse_category_list(self, text: str) -> list:
        """Split a comma/'and' list into clean starting-category names.

        Deliberately simple: no prose extraction, no location stripping games —
        the user is listing general terms. Just split, tidy, de-dupe.
        """
        import re
        parts = re.split(r'[,&]|\band\b', text)
        seen, items = set(), []
        for part in parts:
            item = part.strip().strip('.').strip()
            # Drop obvious lead-ins if the user still typed "I sell ..."
            item = re.sub(r'^\s*(i|we)\s+(sell|make|produce|offer|do|provide)\s+', '',
                          item, flags=re.IGNORECASE).strip()
            if len(item) < 2 or len(item) > 40:
                continue
            if item.lower() in seen:
                continue
            seen.add(item.lower())
            items.append(item.title())
        return items

    def _sell_verb(self, industry: str) -> str:
        return {
            "trading": "sell", "manufacturing": "make",
            "services": "offer", "hybrid": "sell or offer",
        }.get(industry, "sell")

    def _save_product_list(self, phone_number: str, text: str) -> list:
        """Manufacturing-specific: save the explicit product list to catalog."""
        context = self.session.get_context(phone_number)
        business_name = context.get("business_name", "My Business")
        industry = context.get("industry", "manufacturing")
        description = context.get("business_description", "")

        product_text = text.strip()

        # Let the user skip listing products now — finish with an empty catalog.
        if product_text.lower() in ("skip", "later", "not now"):
            return self._complete_onboarding(phone_number, business_name, industry, description, [])

        if len(product_text) < 2:
            return [text_response(
                "Please list at least one product you manufacture,\n"
                "_or type *skip* to add them later._\n\n"
                "_Separate with commas: e.g. Liquid Soap, Bar Soap, Detergent_"
            )]

        # Parse comma-separated product names
        items = [item.strip().title() for item in product_text.split(",") if item.strip() and len(item.strip()) >= 2]

        if not items:
            return [text_response(
                "I couldn't find product names. Please list them separated by commas:\n\n"
                "_e.g. Liquid Soap 1L, Bar Soap, Dish Wash, Detergent 5L_"
            )]

        # Deduplicate
        seen = set()
        unique_items = []
        for item in items:
            if item.lower() not in seen:
                seen.add(item.lower())
                unique_items.append(item)

        return self._complete_onboarding(phone_number, business_name, industry, description, unique_items)

    def _complete_onboarding(self, phone_number: str, business_name: str, industry: str, description: str, items: list) -> list:
        """Finalize onboarding: create user, seed catalog, show completion."""

        # Create user record
        self.db.create_user(phone_number, industry, business_name)
        self.db.update_user_field(phone_number, "industry_class", industry)
        self.db.update_user_field(phone_number, "business_description", description)

        # Seed catalog if products were extracted
        if items:
            catalog = {"products": {}}
            # Set item_type based on industry
            if industry == "services":
                default_type = "service"
            elif industry == "manufacturing":
                default_type = "finished_product"
            else:
                default_type = "product"

            for item in items[:15]:
                key = item.lower().replace(" ", "_")
                catalog["products"][key] = {
                    "name": item,
                    "stock": 0,
                    "landing_cost": 0,
                    "item_type": default_type,
                    "category": "",
                    "variants": [],
                    "recipe": [],
                    "conversions": {},
                }
            self.db.update_user_field(phone_number, "product_catalog", catalog)

        # Reset to IDLE
        self.session.reset(phone_number)

        from utils.whatsapp_ui import button_response

        industry_labels = {
            "trading": "🛍️ Trading & Retail",
            "manufacturing": "🏭 Manufacturing",
            "services": "💼 Services",
            "hybrid": "🔄 Hybrid",
        }

        # ── Done card: tidy summary + hand off to the CATALOG ──
        # Onboarding stays light. Real products (models/variants, prices, units,
        # stock) are built in the Catalog — so we route there, not into a pile of
        # per-industry price/recipe nudges. Consistent for all 4 industries.
        lines = [
            f"✅ *You're all set, {business_name}!*",
            f"{industry_labels.get(industry, industry)}",
        ]

        if items:
            noun = "services" if industry == "services" else "products"
            shown = ", ".join(items[:6])
            more = f" _+{len(items) - 6} more_" if len(items) > 6 else ""
            lines.append("")
            lines.append(f"📝 I've noted your {noun}: *{shown}*{more}")
            lines.append("")
            lines.append(
                "Now let's set them up properly — add "
                + ("rates" if industry == "services"
                   else "models, prices and stock")
                + " in your Catalog. 👇"
            )
            buttons = [
                {"id": "menu_catalog", "title": "📋 Set Up Catalog"},
                {"id": "menu_home", "title": "⏭️ Do It Later"},
            ]
        else:
            lines.append("")
            lines.append("Whenever you're ready, set up your products in the Catalog —")
            lines.append("add prices and stock so every sale shows your profit. 👇")
            buttons = [
                {"id": "menu_catalog", "title": "📋 Set Up Catalog"},
                {"id": "menu_home", "title": "⏭️ Do It Later"},
            ]

        return [
            text_response("\n".join(lines)),
            button_response("What first?", buttons),
        ]

    def _extract_products_from_description(self, description: str, industry: str) -> list:
        """
        Extract product/service names from a natural business description.
        Uses keyword parsing — no AI call needed (keeps onboarding fast).
        """
        import re

        desc = description.lower()

        # Strip trailing location / audience phrases so they don't leak into the
        # catalog, e.g. "beans in Lagos" → "beans", "shoes for customers" → "shoes".
        # Applied to the whole description before splitting.
        desc = re.sub(
            r'\b(in|at|around|for|to)\b[\s\w]*$',
            '',
            desc,
        ) if re.search(r'\b(in|at|around)\b\s+\w+\s*$', desc) else desc

        # Remove common filler words / lead-ins
        fillers = [
            "i sell", "we sell", "i make", "we make", "we produce", "i produce",
            "i do", "we do", "i offer", "we offer", "i provide", "we provide",
            "i deal in", "we deal in", "i run", "we run", "my business",
            "new and", "second hand", "brand new", "fairly used", "quality",
            "all kinds of", "all sorts of", "different types of", "various",
            "like", "such as", "including", "e.g.", "for example",
            "and also", "as well as", "mainly", "mostly",
        ]
        cleaned = desc
        for filler in fillers:
            cleaned = cleaned.replace(filler, " ")

        # Split on common separators: comma, "and", "&"
        parts = re.split(r'[,&]|\band\b', cleaned)

        # Clean each part
        items = []
        for part in parts:
            item = part.strip().strip('.')
            # Remove trailing "etc", "products", "items", "services"
            item = re.sub(r'\s*(etc|products?|items?|services?|goods?)\s*$', '', item)
            # Drop a trailing location phrase on this specific part too
            # ("beans in lagos" → "beans")
            item = re.sub(r'\b(in|at|around)\b\s+.*$', '', item)
            item = item.strip()
            # Reject junk: empty, too long, or too many words to be a product name
            if len(item) < 2 or len(item) > 40:
                continue
            if len(item.split()) > 4:
                continue
            items.append(item.title())

        # Deduplicate
        seen = set()
        unique = []
        for item in items:
            if item.lower() not in seen:
                seen.add(item.lower())
                unique.append(item)

        return unique

    def _what_you_do_example(self, industry: str) -> str:
        """Short industry-specific example for the 'what you do' prompt/guard."""
        examples = {
            "trading": "I sell shoes, bags and accessories",
            "manufacturing": "We produce soap and detergent",
            "services": "I do hair braiding, nails and makeup",
            "hybrid": "I sell phones and also do phone repairs",
        }
        return examples.get(industry, examples["trading"])

    def _get_example(self, industry: str) -> str:
        """Industry-specific example transaction."""
        examples = {
            "trading": "sold 10 bags cement to Alhaji 150K",
            "manufacturing": "sold 200 bottles detergent to Shoprite 80K",
            "services": "cleaned Alhaji's office 25K",
            "hybrid": "sold 5 bags cement 75K",
        }
        return examples.get(industry, "sold goods 50K")

    def _trigger_home_menu(self, phone_number: str, industry: str) -> dict:
        """Return a special marker that router resolves to industry home menu."""
        return {"type": "__SHOW_HOME_MENU__", "industry": industry}
