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

        # Save to context (will write to users table at the end)
        self.session.save(phone_number, ONBOARDING, {
            "onboarding_step": STEP_INDUSTRY,
            "business_name": business_name,
        })

        return [list_response(
            header="🏢 " + business_name,
            body="What type of business do you run?",
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
        """Save industry, complete onboarding, create user record."""
        # Map button IDs to industry keys
        industry_map = {
            "industry_trading": "trading",
            "industry_manufacturing": "manufacturing",
            "industry_services": "services",
            "industry_hybrid": "hybrid",
            # Also handle raw text input
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

        # Create user record in DynamoDB
        self.db.create_user(phone_number, {
            "business_name": business_name,
            "industry_class": industry,
            "tier": "free",
            "onboarding_complete": True,
        })

        # Reset to IDLE — ready to use
        self.session.reset(phone_number)

        industry_labels = {
            "trading": "🛍️ Trading & Retail",
            "manufacturing": "🏭 Manufacturing",
            "services": "💼 Services",
            "hybrid": "🔄 Hybrid",
        }

        return [text_response(
            f"✅ All set!\n\n"
            f"*{business_name}*\n"
            f"{industry_labels[industry]}\n\n"
            f"You're ready to go! Here's how I work:\n\n"
            f"💬 *Type what you bought or sold* and I'll record it.\n"
            f"Example: \"{self._get_example(industry)}\"\n\n"
            f"Or tap the menu below to explore features. 👇"
        ), self._trigger_home_menu(phone_number, industry)]

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
