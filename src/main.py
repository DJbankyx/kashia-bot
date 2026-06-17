# src/main.py
"""Main Router - connects all services together"""

import logging

from src.services.conversation_engine import ConversationEngine
from src.services.whatsapp_client import WhatsAppClient
from src.services.tier_manager import TierManager
from src.services.export_service import ExportService
from src.services.pdf_generator import PDFGenerator
from src.services.crm import ContactService

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class KashiaBot:
    """Main bot class - wires all services together"""

    def __init__(self):
        self.engine = ConversationEngine()
        self.whatsapp = WhatsAppClient()
        self.tier_manager = TierManager(database=self.engine.db)
        self.export_service = ExportService(database=self.engine.db)
        self.pdf_generator = PDFGenerator(database=self.engine.db)
        self.crm = ContactService(database=self.engine.db)

    def handle_message(self, phone_number, text, message_type="text"):
        """
        Main entry point - processes a message and sends response(s) via WhatsApp.

        Args:
            phone_number: sender's phone number
            text: message text
            message_type: "text", "button_reply", "list_reply"
        """
        try:
            logger.info(f"Processing: {phone_number} | {text} | {message_type}")

            # Check tier limits before recording (only for transaction-like messages)
            # Commands like "report", "help", "export" bypass this check
            command = self.engine._detect_command(text.lower().strip())

            if not command:
                # This might be a transaction — check limits
                allowed, warning_msg = self.tier_manager.check_can_record(phone_number)

                if not allowed:
                    # User hit their limit — send upgrade message
                    self.whatsapp.send_text(phone_number, warning_msg)
                    return

                if warning_msg:
                    # User is close to limit — send warning after processing
                    pass  # We'll send it after the main response

            # Process through conversation engine
            responses = self.engine.process_message(phone_number, text, message_type)

            # Handle special responses that need other services
            responses = self._handle_special_responses(phone_number, text, responses)

            # Send all responses via WhatsApp
            for response in responses:
                self._send_response(phone_number, response)

        except Exception as e:
            logger.error(f"Error handling message from {phone_number}: {e}")
            # Always reply — don't leave the user hanging
            self.whatsapp.send_text(
                phone_number,
                "Sorry, something went wrong. Please try again."
            )

    def _handle_special_responses(self, phone_number, text, responses):
        """
        Handle responses that need extra services (export, PDF, upgrade).
        """
        text_lower = text.lower().strip()

        # Handle export commands
        if text_lower in ['export_month', '1'] and self._is_in_state(phone_number, 'EXPORTING'):
            allowed, msg = self.tier_manager.check_can_export(phone_number)
            if not allowed:
                return [{"type": "text", "content": msg}]
            return self.export_service.handle_export_request(phone_number, "month")

        if text_lower in ['export_csv', '2'] and self._is_in_state(phone_number, 'EXPORTING'):
            allowed, msg = self.tier_manager.check_can_export(phone_number)
            if not allowed:
                return [{"type": "text", "content": msg}]
            return self.export_service.handle_export_request(phone_number, "csv")

        if text_lower in ['export_contacts', '3'] and self._is_in_state(phone_number, 'EXPORTING'):
            return self.export_service.handle_export_request(phone_number, "contacts")

        # Handle invoice command
        if text_lower == 'statement':
            allowed, msg = self.tier_manager.check_can_generate_pdf(phone_number)
            if not allowed:
                return [{"type": "text", "content": msg}]
            return self.pdf_generator.handle_statement_request(phone_number)

        if text_lower == 'receipt':
            return self.pdf_generator.handle_receipt_request(phone_number)

        # Handle upgrade commands
        if text_lower in ['basic', 'basic plan']:
            return self.tier_manager.handle_upgrade_request(phone_number, 'basic')

        if text_lower in ['pro', 'pro plan']:
            return self.tier_manager.handle_upgrade_request(phone_number, 'pro')

        if text_lower == 'usage':
            summary = self.tier_manager.get_usage_summary(phone_number)
            return [{"type": "text", "content": summary}]

        return responses

    def _send_response(self, phone_number, response):
        """Send a single response via WhatsApp based on its type"""
        resp_type = response.get('type', 'text')
        content = response.get('content', '')

        if resp_type == 'text':
            self.whatsapp.send_text(phone_number, content)

        elif resp_type == 'buttons':
            body_text = content.get('body', '')
            buttons = content.get('buttons', [])
            self.whatsapp.send_buttons(phone_number, body_text, buttons)

        elif resp_type == 'list':
            header = content.get('header', '')
            body = content.get('body', '')
            button_text = content.get('button_text', 'Select')
            sections = content.get('sections', [])
            self.whatsapp.send_list(phone_number, header, body, button_text, sections)

        elif resp_type == 'document':
            link = content.get('link', '')
            filename = content.get('filename', '')
            caption = content.get('caption', '')
            self.whatsapp.send_document(phone_number, link, filename, caption)

        else:
            # Unknown type — send as text
            self.whatsapp.send_text(phone_number, str(content))

    def _is_in_state(self, phone_number, state):
        """Check if user is currently in a specific state"""
        session = self.engine.db.get_session(phone_number)
        if session:
            return session.get('state', '') == state
        return False


# ==========================================
# SINGLETON INSTANCE
# ==========================================

# Create one instance to be reused across Lambda invocations
bot = None


def get_bot():
    """Get or create the bot singleton"""
    global bot
    if bot is None:
        bot = KashiaBot()
    return bot
