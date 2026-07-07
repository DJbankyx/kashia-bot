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

    def handle_message(self, phone_number: str, text: str, message_type: str = "text"):
        """
        Main entry point — processes a message and sends response(s).
        """
        try:
            logger.info(f"KashiaBot: {phone_number} | {text[:50]} | {message_type}")

            # Check tier limit (only for potential transactions in IDLE state)
            session = self.router.session.get(phone_number)
            state = session.get("state", "")

            if (state not in EXEMPT_STATES
                    and message_type == "text"
                    and not text.lower().strip().startswith("menu_")):
                
                allowed, warning_msg = self.tier_manager.check_can_record(phone_number)
                if not allowed:
                    self.whatsapp.send_text(phone_number, warning_msg)
                    return

            # Route through the main router
            responses = self.router.process(phone_number, text, message_type)

            # Handle special internal markers
            responses = self._resolve_markers(phone_number, responses)

            # Send all responses
            for response in responses:
                self._send_response(phone_number, response)

        except Exception as e:
            import traceback
            logger.error(f"Error: {phone_number}: {e}\n{traceback.format_exc()}")
            self.whatsapp.send_text(
                phone_number,
                f"Sorry, something went wrong. Please try again.\n\n_Debug: {type(e).__name__}: {str(e)[:150]}_"
            )

    def _resolve_markers(self, phone_number: str, responses: list) -> list:
        """Resolve internal markers (e.g. __SHOW_HOME_MENU__, __ROUTE_TO_DEBT__)."""
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

            resolved.append(resp)

        return resolved

    def _send_response(self, phone_number: str, response: dict):
        """Send a single response via WhatsApp based on its type."""
        resp_type = response.get("type", "text")
        content = response.get("content", "")

        if resp_type == "text":
            self.whatsapp.send_text(phone_number, content)

        elif resp_type == "buttons":
            body_text = content.get("body", "")
            buttons = content.get("buttons", [])
            self.whatsapp.send_buttons(phone_number, body_text, buttons)

        elif resp_type == "list":
            header = content.get("header", "")
            body = content.get("body", "")
            button_text = content.get("button_text", "Select")
            sections = content.get("sections", [])
            self.whatsapp.send_list(phone_number, header, body, button_text, sections)

        elif resp_type == "document":
            link = content.get("link", "")
            filename = content.get("filename", "")
            caption = content.get("caption", "")
            self.whatsapp.send_document(phone_number, link, filename, caption)

        else:
            # Unknown type — try sending as text
            if isinstance(content, str) and content:
                self.whatsapp.send_text(phone_number, content)
            else:
                logger.warning(f"Unknown response type: {resp_type}")
