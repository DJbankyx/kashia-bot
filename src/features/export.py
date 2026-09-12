# src/features/export.py
"""Export — Excel/CSV/PDF export of transactions."""

import logging
from core import states
from utils.whatsapp_ui import text_response, list_response, button_response

logger = logging.getLogger(__name__)


class ExportHandler:
    """Handle data export requests."""

    def __init__(self, session_mgr, database, export_service, pdf_generator):
        self.session = session_mgr
        self.db = database
        self.export_service = export_service
        self.pdf_generator = pdf_generator

    def _is_telegram(self, phone_number: str) -> bool:
        """True when this user is on Telegram (tg: namespaced id). Telegram gets
        the tap-first Documents flow; WhatsApp keeps the classic export menu."""
        try:
            from services.messaging_client import platform_for_user
            return platform_for_user(phone_number) == "telegram"
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────
    # STAGE 4 — Documents home (Telegram, tap-first)
    # ─────────────────────────────────────────────────────────

    def documents_home(self, phone_number: str) -> list:
        """Tap-first Documents type picker (Telegram). Mirrors the dashboard's
        boxed language. Routes to the existing receipt/statement flows and the
        invoice/quote builders (4C–4E)."""
        lines = [
            "🗂️ *Documents*",
            "────────────────────",
            "Create something you can send to a customer.",
            "",
            "🧾 *Invoice* — a bill for goods/services",
            "🧾 *Receipt* — proof of a payment received",
            "📄 *Quote* — a price estimate (not yet paid)",
            "📊 *Statement* — your financial summary PDF",
        ]
        rows = [
            {"id": "doc_invoice", "title": "🧾 Invoice"},
            {"id": "doc_receipt", "title": "🧾 Receipt"},
            {"id": "doc_quote", "title": "📄 Quote"},
            {"id": "doc_statement", "title": "📊 Statement"},
            {"id": "menu_home", "title": "☰ Menu"},
        ]
        return [list_response(
            header="🗂️ Documents",
            body="\n".join(lines),
            button_text="Create",
            sections=[{"title": "Document Type", "rows": rows}],
            no_paginate=True,
        )]

    def show_options(self, phone_number: str) -> list:
        """Show export/documents menu. Telegram gets the tap-first Documents
        picker; WhatsApp keeps the classic export list."""
        if self._is_telegram(phone_number):
            return self.documents_home(phone_number)
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
        """Handle export buttons — PIN-protected for data exports."""
        from core.pin_guard import requires_pin

        # ── Stage 4: Documents (Telegram tap-first) ──────────────────────────
        # Receipt + Statement reuse the existing generators. Invoice + Quote
        # open the boxed builder (4C–4E); until those land they route to the
        # existing invoice flow / a clear placeholder so no tap dead-ends.
        if button_id == "doc_statement":
            # Period-aware (Telegram): let the user pick the range first. WhatsApp
            # reaches the statement via export_statement (defaults to month).
            if self._is_telegram(phone_number):
                return self._doc_statement_period_picker(phone_number)
            pin_check = requires_pin(self.db, self.session, phone_number, button_id)
            if pin_check:
                return pin_check
            return self.pdf_generator.handle_statement_request(phone_number)

        if button_id.startswith("doc_statement_"):
            # doc_statement_<period> — generate for the chosen range (PIN-gated).
            period = button_id[len("doc_statement_"):] or "month"
            if period not in ("today", "week", "month", "last_month", "quarter", "year"):
                period = "month"
            pin_check = requires_pin(self.db, self.session, phone_number, "doc_statement")
            if pin_check:
                return pin_check
            return self.pdf_generator.handle_statement_request(phone_number, period=period)

        if button_id == "doc_receipt":
            return self.pdf_generator.handle_receipt_request(phone_number)

        if button_id == "doc_invoice":
            # Telegram: tap-first boxed invoice builder (4C). WhatsApp keeps the
            # existing free-text invoice flow.
            if self._is_telegram(phone_number):
                from main import get_bot
                builder = getattr(get_bot().router, "tg_invoice", None)
                if builder is not None:
                    return builder.start(phone_number)
            return self._start_invoice(phone_number)

        if button_id == "doc_quote":
            # Telegram: the tap-first builder in QUOTE mode. WhatsApp keeps the
            # existing text quote tracker / falls back to the invoice placeholder.
            if self._is_telegram(phone_number):
                from main import get_bot
                builder = getattr(get_bot().router, "tg_invoice", None)
                if builder is not None:
                    return builder.start(phone_number, kind="quote")
            return self._doc_quote_placeholder(phone_number)

        # PIN-protected actions
        if button_id in ("export_excel", "export_csv", "export_statement"):
            pin_check = requires_pin(self.db, self.session, phone_number, button_id)
            if pin_check:
                return pin_check

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

    def _doc_statement_period_picker(self, phone_number: str) -> list:
        """Tap-first period picker for the financial statement (Telegram). Each
        choice generates the statement PDF for that range."""
        rows = [
            {"id": "doc_statement_today", "title": "📅 Today"},
            {"id": "doc_statement_week", "title": "📆 This Week"},
            {"id": "doc_statement_month", "title": "🗓️ This Month"},
            {"id": "doc_statement_last_month", "title": "📅 Last Month"},
            {"id": "doc_statement_quarter", "title": "📊 This Quarter"},
            {"id": "doc_statement_year", "title": "📈 This Year"},
            {"id": "menu_export", "title": "← Documents"},
        ]
        return [list_response(
            header="📊 Financial Statement",
            body=("📊 *Financial Statement*\n"
                  "────────────────────\n"
                  "Pick the period to cover. I'll build a clean P&L PDF you can "
                  "send to your accountant."),
            button_text="Choose Period",
            sections=[{"title": "Period", "rows": rows}],
            no_paginate=True,
        )]

    def _doc_quote_placeholder(self, phone_number: str) -> list:
        """Interim Quote entry (4A). The full tap-first quote builder + quote
        PDF land in 4E; until then this is an honest placeholder that doesn't
        dead-end the tap."""
        return [button_response(
            "📄 *Quote*\n\n"
            "The tap-first quote builder is coming next. For now you can build a "
            "full *Invoice* the same way and send it as your estimate.",
            [
                {"id": "doc_invoice", "title": "🧾 Build Invoice"},
                {"id": "menu_export", "title": "← Documents"},
            ]
        )]

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
