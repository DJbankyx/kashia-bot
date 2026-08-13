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

        return [text_response(
            "👋 Welcome to *Kashia*!\n\n"
            "I'm your AI bookkeeper. I'll help you track sales, expenses, "
            "debts, and more — all right here on WhatsApp.\n\n"
            "Let's get you set up in 30 seconds.\n\n"
            "📝 *What's your business name?*"
        )]

    def _save_business_name(self, phone_number: str, text: str) -> list:
        """Save business name, ask for industry."""
        business_name = text.strip()

        if len(business_name) < 2:
            return [text_response("Please enter your business name (at least 2 characters):")]

        if len(business_name) > 100:
            return [text_response("That's too long! Please use a shorter business name:")]

        # Reject button IDs that get accidentally passed as text
        # Common pattern: contains underscore and matches known prefixes
        BUTTON_PREFIXES = (
            "menu_", "record_", "sec_", "pi_", "biz_", "cat_", "crm_",
            "set_", "report_", "export_", "gen_", "txedit_", "txact_",
            "debt_", "lc_", "pm_", "prod_", "rec_", "confirm_", "btn_",
            "catrec_", "industry_", "var_",
        )
        text_lower = business_name.lower()
        if any(text_lower.startswith(p) for p in BUTTON_PREFIXES):
            return [text_response(
                "📝 *What's your business name?*\n\n"
                "_e.g. Sandra's Fashion, Alhaji Motors, ABC Electronics_"
            )]

        # Also reject if it looks like a command/keyword
        COMMANDS = {
            "menu", "help", "hi", "hello", "hey", "cancel", "back",
            "done", "skip", "yes", "no", "ok", "okay",
        }
        if text_lower in COMMANDS:
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
            body="What type of industry are you in?",
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

        # Industry-specific natural question
        prompts = {
            "trading": (
                "🛍️ Great! *What does your business sell?*\n\n"
                "Just describe it naturally:\n\n"
                "_e.g. \"I sell new and second hand Honda cars\"_\n"
                "_e.g. \"We sell shoes, bags and accessories\"_\n"
                "_e.g. \"I sell rice, oil and provisions\"_"
            ),
            "manufacturing": (
                "🏭 Great! *What does your business make?*\n\n"
                "Just describe it naturally:\n\n"
                "_e.g. \"We produce soap, detergent and cleaning products\"_\n"
                "_e.g. \"I make furniture — tables, chairs, cabinets\"_\n"
                "_e.g. \"We bake bread, cakes and pastries\"_"
            ),
            "services": (
                "💼 Great! *What services do you offer?*\n\n"
                "Just describe it naturally:\n\n"
                "_e.g. \"I do hair braiding, nails and makeup\"_\n"
                "_e.g. \"We offer cleaning and fumigation services\"_\n"
                "_e.g. \"I do web design and digital marketing\"_"
            ),
            "hybrid": (
                "🔄 Great! *What do you sell or offer?*\n\n"
                "Just describe it naturally:\n\n"
                "_e.g. \"I sell phones and also do phone repairs\"_\n"
                "_e.g. \"We do catering and also sell food items\"_"
            ),
        }

        return [text_response(prompts.get(industry, prompts["trading"]))]

    def _save_what_you_do(self, phone_number: str, text: str) -> list:
        """Parse natural description. For manufacturing: ask for product list next. Others: seed catalog and complete."""
        import re

        context = self.session.get_context(phone_number)
        business_name = context.get("business_name", "My Business")
        industry = context.get("industry", "trading")
        description = text.strip()

        if len(description) < 3:
            return [text_response("Please describe what your business does (even one sentence is fine):")]

        # For manufacturing: save description but DON'T seed catalog yet
        # Ask them to list their actual products in the next step
        if industry == "manufacturing":
            self.session.save(phone_number, ONBOARDING, {
                "onboarding_step": STEP_LIST_PRODUCTS,
                "business_name": business_name,
                "industry": industry,
                "business_description": description,
            })

            return [text_response(
                f"🏭 Got it! You make *{description}*.\n\n"
                f"Now, *list the specific products* you manufacture.\n\n"
                f"_Separate each product with a comma:_\n\n"
                f"Example: _Liquid Soap 1L, Bar Soap, Dish Wash 500ml, Detergent 1L, Detergent 5L_\n\n"
                f"_These will become your product catalog._"
            )]

        # For trading/services/hybrid: extract products from description and complete
        items = self._extract_products_from_description(description, industry)
        return self._complete_onboarding(phone_number, business_name, industry, description, items)

    def _save_product_list(self, phone_number: str, text: str) -> list:
        """Manufacturing-specific: save the explicit product list to catalog."""
        context = self.session.get_context(phone_number)
        business_name = context.get("business_name", "My Business")
        industry = context.get("industry", "manufacturing")
        description = context.get("business_description", "")

        product_text = text.strip()

        if len(product_text) < 2:
            return [text_response(
                "Please list at least one product you manufacture.\n\n"
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
            for item in items[:15]:
                key = item.lower().replace(" ", "_")
                catalog["products"][key] = {
                    "name": item,
                    "stock": 0,
                    "landing_cost": 0,
                    "category": "",
                    "variants": [],
                    "recipe": [],
                    "conversions": {},
                }
            self.db.update_user_field(phone_number, "product_catalog", catalog)

        # Reset to IDLE
        self.session.reset(phone_number)

        industry_labels = {
            "trading": "🛍️ Trading & Retail",
            "manufacturing": "🏭 Manufacturing",
            "services": "💼 Services",
            "hybrid": "🔄 Hybrid",
        }

        # Build completion message
        lines = [
            f"✅ *All set!*\n",
            f"*{business_name}*",
            f"{industry_labels.get(industry, industry)}",
        ]
        if items:
            lines.append(f"📦 Catalog: {', '.join(items[:5])}")
            if len(items) > 5:
                lines.append(f"   _+{len(items) - 5} more_")
        lines.append("")

        # Manufacturing-specific: guide user to set up their first recipe
        if industry == "manufacturing" and items:
            lines.append("🏭 *Next step:* Set a recipe for your products!")
            lines.append("")
            lines.append("A recipe tells me what raw materials you use to make each product.")
            lines.append("When you record production, I'll auto-calculate costs and deduct materials.")
            lines.append("")
            lines.append("_Tap below to set up your first recipe, or skip for now._")

            from utils.whatsapp_ui import button_response
            return [
                text_response("\n".join(lines)),
                button_response(
                    "Set up production?",
                    [
                        {"id": "cat_recipe", "title": "📋 Set Recipe Now"},
                        {"id": "menu_home", "title": "⏭️ Skip for Now"},
                    ]
                )
            ]

        # Non-manufacturing: standard completion
        lines.append("Here's how I work:\n")
        lines.append(f"💬 *Type what you bought or sold* and I'll record it.")
        lines.append(f"Example: \"{self._get_example(industry)}\"")
        lines.append("\nOr tap the menu below to explore features. 👇")

        return [
            text_response("\n".join(lines)),
            self._trigger_home_menu(phone_number, industry)
        ]

    def _extract_products_from_description(self, description: str, industry: str) -> list:
        """
        Extract product/service names from a natural business description.
        Uses keyword parsing — no AI call needed (keeps onboarding fast).
        """
        import re

        desc = description.lower()

        # Remove common filler words
        fillers = [
            "i sell", "we sell", "i make", "we make", "we produce",
            "i do", "we do", "i offer", "we offer", "i provide", "we provide",
            "new and", "second hand", "brand new", "fairly used",
            "all kinds of", "different types of", "various",
            "like", "such as", "including", "e.g.", "for example",
            "and also", "as well as",
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
            item = item.strip()
            if len(item) >= 2 and len(item) <= 40:
                items.append(item.title())

        # Deduplicate
        seen = set()
        unique = []
        for item in items:
            if item.lower() not in seen:
                seen.add(item.lower())
                unique.append(item)

        return unique

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
