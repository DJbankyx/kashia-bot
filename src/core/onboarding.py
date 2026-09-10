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

    def _is_telegram(self, phone_number: str) -> bool:
        try:
            from services.messaging_client import platform_for_user
            return platform_for_user(phone_number) == "telegram"
        except Exception:
            return False

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
            "👋 Welcome to *Kashia* — your AI bookkeeper on "
            f"{platform_name}.\n\n"
            "Let's get you set up in under a minute.\n\n"
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

        rows = [
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

        # Telegram: show all four as a visible tappable grid (no dropdown).
        # WhatsApp: keep the classic "Select Industry" list picker.
        return [list_response(
            header="🏢 " + business_name,
            body="*Step 2 of 3*\nWhat type of business are you in?",
            button_text="Select Industry",
            sections=[{"title": "Choose your industry", "rows": rows}],
            no_paginate=self._is_telegram(phone_number),
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

        # Step 3: a plain-language DESCRIPTION of what the business does — NOT a
        # product list. We store it for reassurance/echo-back and seed nothing.
        # Products are built (or skipped) only at the Done card that follows.
        prompts = {
            "trading": (
                "*Step 3 of 3*\n"
                "🛍️ *Tell me a bit about what you do.*\n\n"
                "Just a sentence in your own words — no need to list every "
                "product.\n\n"
                "_e.g. \"I sell cars and spare parts\"_"
            ),
            "manufacturing": (
                "*Step 3 of 3*\n"
                "🏭 *Tell me a bit about what you make.*\n\n"
                "Just a sentence in your own words.\n\n"
                "_e.g. \"I produce soap and detergent\"_"
            ),
            "services": (
                "*Step 3 of 3*\n"
                "💼 *Tell me a bit about what you do.*\n\n"
                "Just a sentence in your own words.\n\n"
                "_e.g. \"I do catering and event planning\"_"
            ),
            "hybrid": (
                "*Step 3 of 3*\n"
                "🔄 *Tell me a bit about what you do.*\n\n"
                "Just a sentence in your own words.\n\n"
                "_e.g. \"I sell phones and also repair them\"_"
            ),
        }
        prompt = prompts.get(industry, prompts["trading"])

        # Telegram: offer a real tappable Skip. WhatsApp keeps typed *skip*.
        if self._is_telegram(phone_number):
            return [button_response(prompt, [
                {"id": "onboard_skip_desc", "title": "⏭️ Skip"},
            ])]
        return [text_response(prompt + "\n\n_Or type *skip*._")]

    def _save_what_you_do(self, phone_number: str, text: str) -> list:
        """Step 3 (final): capture a plain-language DESCRIPTION of the business
        (what they do), store it for echo-back, then complete and hand off to
        the Catalog where real products get built.

        Design (docs/TG_ONBOARDING_PLAN.md): step 3 describes the LINE OF
        BUSINESS — it is NOT a product list and seeds NOTHING. Products are
        created only at the Done card (Set up Catalog / Skip).
        """
        context = self.session.get_context(phone_number)
        business_name = context.get("business_name", "My Business")
        industry = context.get("industry", "trading")
        description = text.strip()

        # Skip — via the Telegram button or a typed keyword.
        if description.lower() in ("skip", "later", "not now", "onboard_skip_desc"):
            return self._complete_onboarding(phone_number, business_name, industry, "", [])

        # Reject stray button taps / commands saved verbatim (e.g. "menu_home").
        if _looks_like_button_or_command(description):
            return self._reprompt_description(phone_number, industry)

        if len(description) < 2:
            return self._reprompt_description(phone_number, industry)

        # Onboarding only DESCRIBES the business — it must NOT create catalog
        # products. We keep the description (echoed back for reassurance) and
        # hand off to the Catalog step. items=[] → no seeding, ever.
        return self._complete_onboarding(phone_number, business_name, industry, description, [])

    def _reprompt_description(self, phone_number: str, industry: str) -> list:
        """Re-ask step 3 as a description (not a product list), with a Skip on
        Telegram."""
        msg = (
            f"Just tell me in a sentence what you {self._sell_verb(industry)}.\n\n"
            f"_e.g. \"{self._what_you_do_example(industry)}\"_"
        )
        if self._is_telegram(phone_number):
            return [button_response(msg, [
                {"id": "onboard_skip_desc", "title": "⏭️ Skip"},
            ])]
        return [text_response(msg + "\n\n_Or type *skip* to set this up later._")]

    def _sell_verb(self, industry: str) -> str:
        return {
            "trading": "sell", "manufacturing": "make",
            "services": "offer", "hybrid": "sell or offer",
        }.get(industry, "sell")

    def _complete_onboarding(self, phone_number: str, business_name: str, industry: str, description: str, items: list) -> list:
        """Finalize onboarding: create user, seed catalog, show completion."""

        # Create user record
        self.db.create_user(phone_number, industry, business_name)
        self.db.update_user_field(phone_number, "industry_class", industry)
        self.db.update_user_field(phone_number, "business_description", description)

        # Onboarding seeds NOTHING into the catalog by design — `items` is
        # always [] from the current flow. This guarded block is kept only as a
        # defensive no-op; products are built exclusively in the Catalog.
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

        # ── Done card: confirm the business, then a DISTINCT catalog step ──
        # Onboarding only describes the business; it seeds NOTHING into the
        # catalog. We echo what they told us for reassurance, then present a
        # clear, separate choice: build the catalog now, or later. Real products
        # (models/variants, prices, units, stock) are built ONLY in the Catalog.
        setup_word = ("services and rates" if industry == "services"
                      else "products, prices and stock")
        noun = "offer" if industry == "services" else (
            "make" if industry == "manufacturing" else "sell")

        lines = [
            f"✅ *You're all set, {business_name}!*",
            f"{industry_labels.get(industry, industry)}",
        ]
        if description and description.lower() not in ("skip", "later", "not now"):
            note = description.strip()
            if len(note) > 90:
                note = note[:90] + "…"
            lines.append("")
            lines.append(f"📝 Noted what you {noun}: _{note}_")

        # ── Telegram: ONE clean card (profile + catalog fork together) ──
        if self._is_telegram(phone_number):
            lines.append("")
            lines.append(f"📋 *Next:* add your {setup_word} in the Catalog so")
            lines.append("every sale shows profit and stock stays accurate.")
            return [button_response("\n".join(lines), [
                {"id": "menu_catalog", "title": "📋 Set Up Catalog Now"},
                {"id": "menu_home", "title": "⏭️ Skip for now"},
            ])]

        # ── WhatsApp: keep the classic three-part completion ──
        lines.append("")
        lines.append("That's your profile done. 🎉")

        catalog_lines = [
            "📋 *Next: set up your Catalog*",
            "",
            f"Add your {setup_word} so every sale shows profit,",
            "stock updates automatically, and reports stay accurate.",
            "",
            "_You can do this now, or anytime from the menu._",
        ]

        return [
            text_response("\n".join(lines)),
            text_response("\n".join(catalog_lines)),
            button_response("Set up your catalog?", [
                {"id": "menu_catalog", "title": "📋 Set Up Catalog Now"},
                {"id": "menu_home", "title": "⏭️ Skip for now"},
            ]),
        ]

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
