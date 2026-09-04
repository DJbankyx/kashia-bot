# src/main.py
"""Main Router — wires all services and feature modules together."""
"""Entry point: get_bot() returns the singleton KashiaBot instance."""

import logging

from core.router import Router
from services.database import Database
from services.categorizer import TransactionCategorizer
from services.whatsapp_client import WhatsAppClient
from services.tier_manager import TierManager
from services.export_service import ExportService
from services.pdf_generator import PDFGenerator

from industries.trading import TradingIndustry
from industries.manufacturing import ManufacturingIndustry
from industries.services_industry import ServicesIndustry
from industries.hybrid import HybridIndustry

from features.transactions import TransactionHandler
from features.reports import ReportsHandler
from features.debt import DebtHandler
from features.catalog import CatalogHandler
from features.contacts import ContactsHandler
from features.export import ExportHandler
from features.invoices import InvoiceHandler
from features.profile import ProfileHandler
from features.personal_info import PersonalInfoHandler
from features.settings import SettingsHandler
from features.production import ProductionHandler
from features.recurring import RecurringHandler
from features.quotes import QuotesHandler

from core.states import EXEMPT_STATES

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ─── Singleton ───
_bot_instance = None


def get_bot():
    """Get or create the KashiaBot singleton (used by webhook handler)."""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = KashiaBot()
    return _bot_instance


class KashiaBot:
    """Main bot class — wires all services together."""

    def __init__(self):
        # Core services
        self.db = Database()
        self.categorizer = TransactionCategorizer()
        self.whatsapp = WhatsAppClient()
        # Messaging clients keyed by platform. WhatsApp is always available;
        # Telegram (and future platforms) register here as they come online.
        # The engine stays platform-agnostic — handle_message() selects the
        # right client per incoming message.
        self.clients = {
            "whatsapp": self.whatsapp,
        }
        # Telegram is optional: if the token isn't configured yet (e.g. the bot
        # hasn't been created), we simply don't register it. This must never
        # break the WhatsApp cold start, so failures are swallowed and logged.
        try:
            from services.telegram_client import TelegramClient
            self.telegram = TelegramClient()
            self.clients["telegram"] = self.telegram
            logger.info("Telegram client registered.")
        except Exception as e:
            self.telegram = None
            logger.warning(f"Telegram client not registered (token missing?): {e}")
        self.tier_manager = TierManager(database=self.db)
        self.export_service = ExportService(database=self.db)
        self.pdf_generator = PDFGenerator(database=self.db)

        # Router
        self.router = Router(self.db, self.categorizer)

        # Industry handlers
        self.router.industries = {
            "trading": TradingIndustry(),
            "manufacturing": ManufacturingIndustry(),
            "services": ServicesIndustry(),
            "hybrid": HybridIndustry(),
        }

        # Feature handlers
        self.router.transactions = TransactionHandler(
            self.router.session, self.db, self.categorizer,
            self.router._get_industry_handler
        )
        self.router.reports = ReportsHandler(self.router.session, self.db)
        self.router.debt = DebtHandler(self.router.session, self.db)
        self.router.catalog = CatalogHandler(self.router.session, self.db, self.categorizer)
        self.router.contacts = ContactsHandler(self.router.session, self.db)
        self.router.export = ExportHandler(
            self.router.session, self.db, self.export_service, self.pdf_generator
        )
        self.router.invoices = InvoiceHandler(self.router.session, self.db, self.pdf_generator)
        self.router.profile = ProfileHandler(
            self.router.session, self.db, self.router._get_industry_handler
        )
        self.router.personal_info = PersonalInfoHandler(self.router.session, self.db)
        self.router.settings = SettingsHandler(
            self.router.session, self.db, self.tier_manager
        )
        self.router.production = ProductionHandler(self.router.session, self.db)
        self.router.recurring = RecurringHandler(self.router.session, self.db)
        self.router.quotes = QuotesHandler(self.router.session, self.db)

        # Telegram fast-entry (app-like tappable sale/purchase). Telegram-only;
        # holds a router ref for engine access (catalog builders, confirm/save).
        from features.tg_fastentry import TGFastEntry
        self.router.tg_fastentry = TGFastEntry(self.router)

    def get_client(self, platform: str = "whatsapp"):
        """
        Return the MessagingClient for a platform.

        Falls back to the WhatsApp client if an unknown platform is passed,
        so existing WhatsApp behavior is never broken by a bad/missing value.
        """
        return self.clients.get(platform) or self.whatsapp

    def handle_message(self, phone_number: str, text: str,
                       message_type: str = "text", platform: str = "whatsapp"):
        """
        Main entry point — processes a message and sends response(s).

        Args:
            phone_number: the user id for this platform (phone for WhatsApp,
                          chat_id for Telegram). Kept named phone_number for
                          backward compatibility with existing callers.
            text: message text or button/list id.
            message_type: "text", "interactive", "reaction", etc.
            platform: which messaging platform this message came from. Selects
                      the outbound client. Defaults to "whatsapp".
        """
        client = self.get_client(platform)
        try:
            logger.info(f"KashiaBot[{platform}]: {phone_number} | {text[:50]} | {message_type}")

            # Check tier limit (only for potential transactions in IDLE state)
            session = self.router.session.get(phone_number)
            state = session.get("state", "")

            if (state not in EXEMPT_STATES
                    and message_type == "text"
                    and not text.lower().strip().startswith("menu_")):
                
                allowed, warning_msg = self.tier_manager.check_can_record(phone_number)
                if not allowed:
                    client.send_text(phone_number, warning_msg)
                    return

            # Route through the main router
            responses = self.router.process(phone_number, text, message_type)

            # Resolve markers, add navigation, and send.
            self._deliver_engine_responses(phone_number, responses, platform=platform)

        except Exception as e:
            import traceback
            logger.error(f"Error: {phone_number}: {e}\n{traceback.format_exc()}")
            client.send_text(
                phone_number,
                f"Sorry, something went wrong. Please try again.\n\n_Debug: {type(e).__name__}: {str(e)[:150]}_"
            )

    def _deliver_engine_responses(self, phone_number: str, responses, platform: str = "whatsapp"):
        """Run the standard output pipeline on a list of engine response dicts.

        Resolves internal markers, adds the navigation footer, and sends each
        response via the platform's client. Shared by handle_message and by
        the Telegram fast-entry hand-off so both behave identically.
        """
        client = self.get_client(platform)
        responses = self._resolve_markers(phone_number, responses, client)

        # Navigation footer depends on the (possibly changed) current state.
        current_session = self.router.session.get(phone_number)
        current_state = current_session.get("state", "")
        responses = self._ensure_navigation(responses, current_state)

        for response in responses:
            self._send_response(phone_number, response, client)

    def _resolve_markers(self, phone_number: str, responses: list, client=None) -> list:
        """Resolve internal markers (e.g. __SHOW_HOME_MENU__, __ROUTE_TO_DEBT__, __EXPORT_REPORT__).

        `client` is the active platform's MessagingClient, used for markers that
        send messages directly (e.g. debtor reminders). Defaults to WhatsApp
        for backward compatibility.
        """
        if client is None:
            client = self.whatsapp
        resolved = []
        for resp in responses:
            if resp.get("type") == "__SHOW_HOME_MENU__":
                industry_key = resp.get("industry", "trading")
                industry = self.router.industries.get(industry_key)
                if industry:
                    resolved.extend(industry.show_home_menu(phone_number))
                continue

            if resp.get("type") == "__ROUTE_TO_DEBT__":
                text = resp.get("content", "")
                session = self.router.session.get(phone_number)
                debt_responses = self.router.debt._handle_payment(phone_number, text, session.get("context", {}))
                resolved.extend(debt_responses)
                continue

            if resp.get("type") == "__EXPORT_REPORT__":
                # Triggered from a report page — export that period as Excel
                content = resp.get("content", {})
                period  = content.get("period", "month")
                export_responses = self.export_service.handle_export_request(
                    phone_number, period
                )
                resolved.extend(export_responses)
                continue

            if resp.get("type") == "__EXPORT_PDF_STATEMENT__":
                # Generate and send PDF financial statement
                pdf_responses = self.pdf_generator.handle_statement_request(phone_number)
                resolved.extend(pdf_responses)
                continue

            if resp.get("type") == "__EDIT_RECORDS__":
                # Triggered from a tab report — show edit list for that type
                content = resp.get("content", {})
                tx_type = content.get("tx_type")
                edit_responses = self.router.transactions.show_edit_list(phone_number, tx_type)
                resolved.extend(edit_responses)
                continue

            if resp.get("type") == "__SEND_REMINDER__":
                # Send a reminder to a debtor's PHONE NUMBER. This is inherently
                # a WhatsApp action (the target is a phone number, not the
                # sender's platform), so it always uses the WhatsApp client
                # regardless of which platform the request came from.
                # (Telegram-native debtor reminders are a future enhancement.)
                content = resp.get("content", {})
                debtor_phone = content.get("debtor_phone", "")
                reminder_text = content.get("reminder_text", "")
                if debtor_phone and reminder_text:
                    self.whatsapp.send_text(debtor_phone, reminder_text)
                continue

            if resp.get("type") == "__GEN_INVOICE__":
                # Generate invoice for a specific transaction
                content = resp.get("content", {})
                tx_id = content.get("tx_id", "")
                if tx_id:
                    inv_responses = self.pdf_generator.handle_multi_invoice_request(
                        phone_number, [tx_id]
                    )
                    resolved.extend(inv_responses)
                continue

            if resp.get("type") == "__GEN_RECEIPT__":
                # Generate receipt for a specific transaction
                content = resp.get("content", {})
                tx_id = content.get("tx_id", "")
                if tx_id:
                    rcpt_responses = self.pdf_generator.handle_multi_receipt_request(
                        phone_number, [tx_id]
                    )
                    resolved.extend(rcpt_responses)
                continue

            if resp.get("type") == "__START_RECIPE_SETUP__":
                # Delegate recipe setup to production handler
                recipe_responses = self.router.production._start_recipe_setup(phone_number)
                resolved.extend(recipe_responses)
                continue

            if resp.get("type") == "__PIN_VERIFIED__":
                # PIN was verified — re-execute the original protected action
                content = resp.get("content", {})
                action_id = content.get("action_id", "")
                pin_action_map = {
                    "export_excel": lambda: self.export_service.handle_export_request(phone_number, "month"),
                    "export_csv": lambda: self.export_service.handle_export_request(phone_number, "csv"),
                    "export_statement": lambda: self.pdf_generator.handle_statement_request(phone_number),
                    "pi_bank": lambda: self.router.personal_info._start_bank_details(phone_number),
                    "set_reset": lambda: self.router.settings._confirm_reset(phone_number),
                    "export": lambda: self.router.export.show_options(phone_number),
                }
                handler = pin_action_map.get(action_id)
                if handler:
                    resolved.extend(handler())
                else:
                    resolved.append({"type": "text", "content": "✅ PIN verified. Please tap the option again."})
                continue

            resolved.append(resp)

        return resolved

    def _ensure_navigation(self, responses: list, current_state: str = "") -> list:
        """
        Ensure the last response in a chain has a navigation option (Menu/Back).
        
        Rules:
        - If user is mid-flow (active state), do NOT append Menu — the flow
          handlers manage their own navigation (Back/Skip/Cancel).
        - If last response is already buttons/list → check if it has a menu/back option, add if not
        - If last response is plain text → append a small menu button after it
        - Skip for confirmation flows (those already have Yes/Edit/Cancel)
        - Skip for document responses or forward prompts
        """
        if not responses:
            return responses

        # States where user is mid-flow — do NOT auto-append Menu
        from core.states import EXEMPT_STATES, IDLE
        MID_FLOW_STATES = EXEMPT_STATES - {IDLE}
        if current_state and current_state in MID_FLOW_STATES:
            return responses

        # Find the last "real" response (not document/forward_prompt)
        last_idx = len(responses) - 1
        while last_idx >= 0 and responses[last_idx].get("type") in ("document", "forward_prompt", "__SHOW_HOME_MENU__"):
            last_idx -= 1

        if last_idx < 0:
            return responses

        last = responses[last_idx]
        resp_type = last.get("type", "text")

        # Skip if it's a confirmation flow (has confirm_yes or confirm_edit)
        if resp_type == "buttons":
            buttons = last.get("content", {}).get("buttons", [])
            btn_ids = [b.get("id", "") for b in buttons]
            # Already has menu/home or is a confirmation → skip
            if any(bid in ("menu_home", "confirm_yes", "confirm_edit", "confirm_cancel") for bid in btn_ids):
                return responses
            # Already has 3 buttons (WhatsApp max) → can't add more
            if len(buttons) >= 3:
                return responses
            # Add menu button
            buttons.append({"id": "menu_home", "title": "☰ Menu"})
            return responses

        if resp_type == "list":
            # Lists already have their own navigation — skip
            return responses

        if resp_type == "text":
            content = last.get("content", "")
            # Skip very short acknowledgments or if it already mentions menu
            if "☰ Menu" in content or "tap the menu" in content.lower():
                return responses
            # Don't add after text that's asking for input (ending with : or ?)
            if content.strip().endswith(":") or content.strip().endswith("_"):
                return responses
            # Append a menu button after the text
            from utils.whatsapp_ui import button_response
            responses.append(button_response(
                "☰ Navigation",
                [{"id": "menu_home", "title": "☰ Menu"}]
            ))

        return responses

    def _maybe_send_paginated_list(self, phone_number, client, header, body, sections) -> bool:
        """Telegram-only: render a long, description-less picker as a paged keyboard.

        Returns True if it sent a paginated message (caller should NOT also call
        send_list); False to fall back to the normal list rendering.

        Guard conditions (all must hold):
          - the client is the Telegram client (has page_keyboard/send_and_get_id),
          - the combined rows have NO descriptions (pure picker, not a rich menu),
          - there are more rows than one page (PAGE_SIZE).

        The full option list is stashed in the session (best-effort, 24h TTL)
        keyed by the sent message_id, so Prev/Next taps can re-render pages
        without touching the engine. If anything fails, we return False and the
        caller falls back to a normal (non-paginated) send.
        """
        # Only the Telegram client supports paging; feature-detect to stay
        # platform-agnostic (WhatsApp client lacks these methods).
        if not (hasattr(client, "page_keyboard") and hasattr(client, "send_and_get_id")):
            return False

        try:
            from services.telegram_client import PAGE_SIZE

            options = []
            has_description = False
            for section in sections or []:
                for row in section.get("rows", []):
                    if row.get("description"):
                        has_description = True
                    options.append({"id": row.get("id", ""), "title": row.get("title", "")})

            # Rich menus (with descriptions) and short lists keep normal rendering.
            if has_description or len(options) <= PAGE_SIZE:
                return False

            # Build the message body text (header + body) for page 0.
            text_lines = []
            if header:
                text_lines.append(f"*{header}*")
            if body:
                text_lines.append(body)
            text = "\n".join(text_lines).strip() or "Choose an option:"

            keyboard = client.page_keyboard(options, 0)
            message_id = client.send_and_get_id(phone_number, text, keyboard=keyboard)
            if not message_id:
                # Send failed — let the caller fall back to a normal list.
                return False

            # Stash the full options + text so Prev/Next can re-page. Best-effort:
            # a dedicated context key, merged without disturbing active flow.
            # We keep only the LATEST paginated message (single entry) so the
            # session doesn't grow unbounded — a user pages the list they just
            # saw; older paginated lists don't need to stay navigable.
            try:
                page_store = {
                    str(message_id): {
                        "options": options,
                        "text": text,
                    }
                }
                self.router.session.update_context(
                    phone_number, {"__tg_page": page_store}
                )
            except Exception as e:
                # If the stash fails, paging just won't work (taps fall through
                # to the engine); the page itself already displayed fine.
                logger.warning(f"Pagination stash failed for {phone_number}: {e}")

            return True
        except Exception as e:
            logger.warning(f"Paginated list render failed, falling back: {e}")
            return False

    def _send_response(self, phone_number: str, response: dict, client=None):
        """Send a single response via the given platform client, based on type.

        `client` is the active platform's MessagingClient. Defaults to the
        WhatsApp client for backward compatibility.
        """
        if client is None:
            client = self.whatsapp

        resp_type = response.get("type", "text")
        content = response.get("content", "")

        if resp_type == "text":
            client.send_text(phone_number, content)

        elif resp_type == "buttons":
            body_text = content.get("body", "")
            buttons = content.get("buttons", [])
            client.send_buttons(phone_number, body_text, buttons)

        elif resp_type == "list":
            header = content.get("header", "")
            body = content.get("body", "")
            button_text = content.get("button_text", "Select")
            sections = content.get("sections", [])
            # Telegram-only: long, description-less pickers render as a paginated
            # inline keyboard (◀ Prev / Next ▶). Everything else — and all of
            # WhatsApp — uses the normal send_list path unchanged.
            if not self._maybe_send_paginated_list(
                    phone_number, client, header, body, sections):
                client.send_list(phone_number, header, body, button_text, sections)

        elif resp_type == "document":
            link = content.get("link", "")
            filename = content.get("filename", "")
            caption = content.get("caption", "")
            client.send_document(phone_number, link, filename, caption)

        elif resp_type == "forward_prompt":
            # Invoice/receipt was generated — offer to forward the link to the customer
            # content = {"customer_name": "...", "s3_url": "...", "filename": "..."}
            customer_name = content.get("customer_name", "") if isinstance(content, dict) else ""
            s3_url = content.get("s3_url", "") if isinstance(content, dict) else ""
            if customer_name and s3_url:
                client.send_text(
                    phone_number,
                    f"📤 *Forward to {customer_name}?*\n\n"
                    f"Share this link with them directly:\n{s3_url}\n\n"
                    f"_Link expires in 24 hours._"
                )

        else:
            # Unknown type — try sending as text
            if isinstance(content, str) and content:
                client.send_text(phone_number, content)
            else:
                logger.warning(f"Unknown response type: {resp_type}")
