# src/features/export.py
"""Export — Excel/CSV/PDF export of transactions."""

import logging
from core import states
from utils.whatsapp_ui import text_response, list_response

logger = logging.getLogger(__name__)


class ExportHandler:
    """Handle data export requests."""

    def __init__(self, session_mgr, database, export_service, pdf_generator):
        self.session = session_mgr
        self.db = database
        self.export_service = export_service
        self.pdf_generator = pdf_generator

    def show_options(self, phone_number: str) -> list:
        """Show export options menu."""
        return [list_response(
            header="📁 Export & Documents",
            body="What would you like to export or generate?",
            button_text="Select Option",
            sections=[{
                "title": "Export Options",
                "rows": [
                    {"id": "export_excel", "title": "📊 Excel Report", "description": "This month's transactions"},
                    {"id": "export_csv", "title": "📄 CSV File", "description": "Raw data for spreadsheets"},
                    {"id": "export_contacts", "title": "📇 Contacts Export", "description": "Customer & supplier list"},
                    {"id": "export_invoice", "title": "🧾 Generate Invoice", "description": "Professional invoice PDF"},
                    {"id": "export_receipt", "title": "🧾 Generate Receipt", "description": "Last transaction receipt"},
                    {"id": "export_statement", "title": "📑 Financial Statement", "description": "Monthly statement PDF"},
                ]
            }]
        )]

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle export state."""
        self.session.reset(phone_number)
        return self.show_options(phone_number)

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Handle export buttons."""
        if button_id == "export_excel":
            return self.export_service.handle_export_request(phone_number, "month")

        if button_id == "export_csv":
            return self.export_service.handle_export_request(phone_number, "csv")

        if button_id == "export_contacts":
            return self.export_service.handle_export_request(phone_number, "contacts")

        if button_id == "export_invoice":
            return self._start_invoice(phone_number)

        if button_id == "export_receipt":
            return self.pdf_generator.handle_receipt_request(phone_number)

        if button_id == "export_statement":
            return self.pdf_generator.handle_statement_request(phone_number)

        return self.show_options(phone_number)

    def _start_invoice(self, phone_number: str) -> list:
        """Start invoice generation flow."""
        self.session.save(phone_number, states.INVOICING, {
            "invoice_step": "ask_details",
        })
        return [text_response(
            "🧾 *Generate Invoice*\n\n"
            "Type the invoice details:\n\n"
            "_[Customer Name] [Amount] for [Description]_\n\n"
            "Example: _Sandra 150000 for 10 pairs Nike shoes_\n\n"
            "Or type *cancel* to go back."
        )]
