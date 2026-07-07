# src/features/invoices.py
"""Invoice/Receipt/Statement generation handler."""

import logging
import re
from core import states
from utils.parser import parse_amount
from utils.whatsapp_ui import text_response, format_amount

logger = logging.getLogger(__name__)


class InvoiceHandler:
    """Handle invoice, receipt, and statement generation flows."""

    def __init__(self, session_mgr, database, pdf_generator):
        self.session = session_mgr
        self.db = database
        self.pdf_generator = pdf_generator

    def start(self, phone_number: str) -> list:
        """Start invoice creation flow."""
        self.session.save(phone_number, states.INVOICING, {
            "invoice_step": "ask_details",
        })
        return [text_response(
            "🧾 *Generate Invoice*\n\n"
            "Type the details:\n_[Customer] [Amount] for [Description]_\n\n"
            "Example: _Sandra 150000 for 10 pairs Nike shoes_\n\n"
            "Or type *cancel*."
        )]

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle invoice flow states."""
        context = session.get("context", {})
        step = context.get("invoice_step", "ask_details")

        if text.lower() in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        if step == "ask_details":
            return self._parse_invoice_details(phone_number, text)

        self.session.reset(phone_number)
        return self.start(phone_number)

    def _parse_invoice_details(self, phone_number: str, text: str) -> list:
        """Parse invoice details from text and generate."""
        # Try to extract: [name] [amount] for [description]
        amount = parse_amount(text)
        if not amount:
            return [text_response("💰 Please include an amount.\n\nExample: _Sandra 150000 for Nike shoes_")]

        # Extract name (before the amount) and description (after "for")
        text_lower = text.lower()
        description = "Goods/Services"
        customer_name = "Customer"

        # Look for "for" to split description
        for_match = re.search(r'\bfor\b\s+(.+)', text, re.IGNORECASE)
        if for_match:
            description = for_match.group(1).strip()
            before_for = text[:for_match.start()].strip()
        else:
            before_for = text

        # Name is text before the amount (excluding the amount itself)
        amount_str = str(int(float(amount)))
        # Find where amount appears in before_for
        name_part = re.sub(r'\d[\d,KkMm.]*', '', before_for).strip()
        if name_part:
            customer_name = name_part

        self.session.reset(phone_number)

        # Generate invoice via pdf_generator
        try:
            result = self.pdf_generator.handle_invoice_request(
                phone_number, customer_name, float(amount), description
            )
            return result
        except Exception as e:
            logger.error(f"Invoice generation error: {e}")
            return [text_response(f"❌ Error generating invoice. Please try again.")]
