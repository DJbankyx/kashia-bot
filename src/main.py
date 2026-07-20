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
        self.router.personal_info = PersonalInfoHandler(self.router.session, self.db)
        self.router.settings = SettingsHandler(
            self.router.session, self.db, self.tier_manager
        )
        self.router.production = ProductionHandler(self.router.session, self.db)
        self.router.recurring = RecurringHandler(self.router.session, self.db)

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

            # Add navigation footer (Menu/Back) if missing from last response
            responses = self._ensure_navigation(responses)

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
        """Resolve internal markers (e.g. __SHOW_HOME_MENU__, __ROUTE_TO_DEBT__, __EXPORT_REPORT__)."""
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
                # Send a WhatsApp message to a debtor's phone number
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

    def _ensure_navigation(self, responses: list) -> list:
        """
        Ensure the last response in a chain has a navigation option (Menu/Back).
        
        Rules:
        - If last response is already buttons/list → check if it has a menu/back option, add if not
        - If last response is plain text → append a small menu button after it
        - Skip for confirmation flows (those already have Yes/Edit/Cancel)
        - Skip for document responses or forward prompts
        """
        if not responses:
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

        elif resp_type == "forward_prompt":
            # Invoice/receipt was generated — offer to forward the link to the customer
            # content = {"customer_name": "...", "s3_url": "...", "filename": "..."}
            customer_name = content.get("customer_name", "") if isinstance(content, dict) else ""
            s3_url = content.get("s3_url", "") if isinstance(content, dict) else ""
            if customer_name and s3_url:
                self.whatsapp.send_text(
                    phone_number,
                    f"📤 *Forward to {customer_name}?*\n\n"
                    f"Share this link with them directly:\n{s3_url}\n\n"
                    f"_Link expires in 24 hours._"
                )

        else:
            # Unknown type — try sending as text
            if isinstance(content, str) and content:
                self.whatsapp.send_text(phone_number, content)
            else:
                logger.warning(f"Unknown response type: {resp_type}")
