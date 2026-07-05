# src/main.py
"""Main Router - connects all services together"""

import logging

from services.conversation_engine import ConversationEngine
from services.whatsapp_client import WhatsAppClient
from services.tier_manager import TierManager
from services.export_service import ExportService
from services.pdf_generator import PDFGenerator
from services.crm import ContactService

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Commands and states that are ALWAYS allowed regardless of tier/transaction limit
EXEMPT_COMMANDS = {
    'greeting', 'help', 'report', 'today', 'week', 'month',
    'customers', 'suppliers', 'contacts',
    'setup_catalog', 'show_catalog', 'add_product', 'remove_product',
    'add_subcategory', 'add_series', 'remove_subcategory', 'remove_series',
    'set_unit', 'edit_catalog', 'edit_sales', 'edit_expenses',
    'edit_transaction', 'delete_entry', 'edit',
    'upgrade', 'change_category', 'undo',
    'compliment', 'sad', 'excited', 'pidgin_chat',
}

# States where transaction limit should NOT apply
EXEMPT_STATES = {
    'reg_products', 'reg_subcategories', 'reg_series',
    'reg_attributes', 'reg_attr_values', 'reg_conversions',
    'editing', 'edit_transaction', 'delete_confirm',
    'VIEWING_REPORT', 'EXPORTING', 'INVOICING',
    'ONBOARDING', 'NEW_USER', 'CHANGING_CATEGORY',
    'AWAITING_CORRECTION', 'AWAITING_CONFIRMATION',
}


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
        """
        try:
            logger.info(f"Processing: {phone_number} | {text} | {message_type}")

            command = self.engine._detect_command(text.lower().strip())

            # Check current session state
            session = self.engine.db.get_session(phone_number)
            current_state = session.get('state', '') if session else ''

            # Only check transaction limit if:
            # - No command detected (likely a transaction)
            # - Not in an exempt state (catalog setup, editing, reports etc)
            # - Not an exempt command
            should_check_limit = (
                not command
                and current_state not in EXEMPT_STATES
                and command not in EXEMPT_COMMANDS
                and not text.lower().strip().startswith('menu_')
            )

            if should_check_limit:
                allowed, warning_msg = self.tier_manager.check_can_record(phone_number)

                if not allowed:
                    # User hit their limit — send upgrade message
                    self.whatsapp.send_text(phone_number, warning_msg)
                    return

                if warning_msg:
                    # User is close to limit — send warning after processing
                    pass

            # Process through conversation engine
            responses = self.engine.process_message(phone_number, text, message_type)

            # Handle special responses that need other services
            responses = self._handle_special_responses(phone_number, text, responses)

            # Send all responses via WhatsApp
            for response in responses:
                self._send_response(phone_number, response)

        except Exception as e:
            import traceback
            logger.error(f"Error handling message from {phone_number}: {e}\n{traceback.format_exc()}")
            self.whatsapp.send_text(
                phone_number,
                f"Sorry, something went wrong. Please try again.\n\n_Debug: {type(e).__name__}: {str(e)[:150]}_"
            )

    def _handle_special_responses(self, phone_number, text, responses):
        """
        Handle responses that need extra services (export, PDF, upgrade).
        """
        text_lower = text.lower().strip()

        # Filtered export (after viewing a report)
        if text_lower in ['export_filtered_excel', 'export_filtered_pdf'] and self._is_in_state(phone_number, 'EXPORTING'):
            session = self.engine.db.get_session(phone_number)
            ctx = session.get('context', {}) if session else {}
            if ctx.get('filtered_export'):
                filter_type = ctx.get('filter_type', '')
                start_date = ctx.get('filter_start', '')
                end_date = ctx.get('filter_end', '')
                period = ctx.get('filter_period', 'Report')
                fmt = 'pdf' if 'pdf' in text_lower else 'excel'
                return self.export_service.handle_filtered_export(
                    phone_number, filter_type, start_date, end_date, period, fmt)

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

        # Handle PDF generation markers from conversation engine
        if responses and len(responses) == 1:
            content = responses[0].get('content', '')
            resp_type = responses[0].get('type', 'text')

            # Handle invoice generation (structured response)
            if resp_type == 'invoice_generate' and isinstance(content, dict):
                customer_name = content.get('customer_name', 'Customer')
                amount = content.get('amount', 0)
                description = content.get('description', 'Goods/Services')
                tax = content.get('tax')  # dict: {amount, percent, type} or None
                discount = content.get('discount')  # dict: {amount, percent, type} or None
                return self.pdf_generator.handle_invoice_request(
                    phone_number, customer_name, amount, description, discount=discount, tax=tax)

            # Handle multi-transaction invoice
            if resp_type == 'invoice_from_transactions' and isinstance(content, dict):
                tx_ids = content.get('transaction_ids', [])
                return self.pdf_generator.handle_multi_invoice_request(phone_number, tx_ids)

            # Handle receipt generation
            if resp_type == 'receipt_generate' and isinstance(content, dict):
                mode = content.get('mode', 'last')
                if mode == 'last':
                    return self.pdf_generator.handle_receipt_request(phone_number)
                elif mode == 'specific':
                    tx_ids = content.get('transaction_ids', [])
                    return self.pdf_generator.handle_multi_receipt_request(phone_number, tx_ids)

            if content == '__STATEMENT_REQUEST__':
                allowed, msg = self.tier_manager.check_can_generate_pdf(phone_number)
                if not allowed:
                    return [{"type": "text", "content": msg}]
                return self.pdf_generator.handle_statement_request(phone_number)

            if content == '__RECEIPT_REQUEST__':
                return self.pdf_generator.handle_receipt_request(phone_number)

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

        elif resp_type == 'forward_prompt':
            # After invoice/receipt delivery — offer to send to customer
            customer_name = content.get('customer_name', '')
            s3_url = content.get('s3_url', '')
            filename = content.get('filename', '')
            if customer_name and s3_url:
                # Look up customer's phone from contacts
                contact = self.engine.db.get_contact_by_name(phone_number, customer_name)
                contact_phone = contact.get('contact_phone', '') if contact else ''
                # Save state for confirmation
                self.engine.db.save_session(phone_number, 'CONFIRM_FORWARD', {
                    'customer_name': customer_name,
                    'contact_phone': contact_phone,
                    's3_url': s3_url,
                    'filename': filename
                })
                if contact_phone:
                    self.whatsapp.send_buttons(phone_number,
                        f"📤 Send this to *{customer_name}* ({contact_phone})?",
                        [{"id": "forward_yes", "title": "✅ Yes, Send"},
                         {"id": "forward_no", "title": "❌ No"}])
                else:
                    self.whatsapp.send_text(phone_number,
                        f"📤 Want to send this to *{customer_name}*?\n\n"
                        f"Send their WhatsApp number and I'll deliver it.\n"
                        f"Or type *skip* to skip.")

        elif resp_type == 'forward_send':
            # Actually send the document to the customer
            to_phone = content.get('to_phone', '')
            customer_name = content.get('customer_name', '')
            s3_url = content.get('s3_url', '')
            filename = content.get('filename', '')
            if to_phone and s3_url:
                # Normalize Nigerian phone numbers
                to_phone = to_phone.replace(' ', '').replace('-', '')
                if to_phone.startswith('0') and len(to_phone) == 11:
                    to_phone = '234' + to_phone[1:]
                success = self.whatsapp.send_document(
                    to_phone, s3_url, filename,
                    caption=f"From {self.engine.db.get_user(phone_number).get('business_name', 'Your vendor')}")
                if success:
                    self.whatsapp.send_text(phone_number, f"✅ Sent to *{customer_name}*!")
                else:
                    # Delivery failed — restore state so user can retry with different number
                    self.engine.db.save_session(phone_number, 'CONFIRM_FORWARD', {
                        'customer_name': customer_name,
                        'contact_phone': '',
                        's3_url': s3_url,
                        'filename': filename
                    })
                    self.whatsapp.send_text(phone_number,
                        f"⚠️ Couldn't deliver to {customer_name} on that number.\n\n"
                        f"Try a different WhatsApp number, or type *skip* to cancel.")

        else:
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

bot = None


def get_bot():
    """Get or create the bot singleton"""
    global bot
    if bot is None:
        bot = KashiaBot()
    return bot
