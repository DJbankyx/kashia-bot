# src/features/quotes.py
"""Quotes & Estimates — Services industry feature.

Data stored on user profile as:
  quotes: [
    {
      "id": "Q0001",
      "client": "Mrs Ade",
      "description": "Office deep cleaning + fumigation",
      "amount": 75000,
      "status": "pending",  # pending, accepted, converted, expired
      "created_at": "2026-08-15",
      "valid_until": "2026-08-22",
      "converted_tx_id": None,  # set when converted to invoice/sale
    }
  ]

Flow:
  Create Quote → Send to client (text summary) → Mark Accepted → Convert to Sale+Invoice
"""

import logging
import time
from datetime import datetime, timedelta

from core import states
from utils.whatsapp_ui import text_response, button_response, list_response, format_amount
from utils.parser import parse_amount

logger = logging.getLogger(__name__)


class QuotesHandler:
    """Manage quotes/estimates for service businesses."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    # ─────────────────────────────────────────────────────────
    # SHOW QUOTES
    # ─────────────────────────────────────────────────────────

    def show(self, phone_number: str) -> list:
        """Show all pending/active quotes."""
        user = self.db.get_user(phone_number) or {}
        quotes = user.get("quotes", [])
        pending = [q for q in quotes if q.get("status") == "pending"]
        accepted = [q for q in quotes if q.get("status") == "accepted"]

        if not pending and not accepted:
            return [
                text_response(
                    "📝 *Quotes & Estimates*\n\n"
                    "No active quotes.\n\n"
                    "Create a quote to send to a client before doing the work.\n"
                    "When they accept, convert it to a sale + invoice."
                ),
                button_response("Actions:", [
                    {"id": "quote_create", "title": "➕ Create Quote"},
                    {"id": "menu_home", "title": "☰ Menu"},
                ])
            ]

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            "📝  *Quotes & Estimates*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        if accepted:
            lines.append("✅ *Accepted (ready to convert):*")
            for q in accepted[:5]:
                lines.append(f"  • {q['client']} — {format_amount(q['amount'])}")
                lines.append(f"    _{q.get('description', '')[:40]}_")
            lines.append("")

        if pending:
            lines.append("⏳ *Pending:*")
            for q in pending[:5]:
                lines.append(f"  • {q['client']} — {format_amount(q['amount'])}")
                lines.append(f"    _{q.get('description', '')[:40]}_")
                valid = q.get("valid_until", "")
                if valid and valid < datetime.now().strftime("%Y-%m-%d"):
                    lines.append(f"    🔴 Expired")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")

        # Build action rows
        rows = [{"id": "quote_create", "title": "➕ Create New Quote",
                 "description": "Send estimate to a client"}]

        for q in (accepted + pending)[:7]:
            qid = q.get("id", "")
            status_emoji = "✅" if q.get("status") == "accepted" else "⏳"
            rows.append({
                "id": f"quote_view_{qid}",
                "title": f"{status_emoji} {q['client']}"[:24],
                "description": f"{format_amount(q['amount'])} — {q.get('description', '')[:30]}"[:72],
            })

        return [
            text_response("\n".join(lines)),
            list_response(
                header="📝 Quote Actions",
                body="Create new or manage existing:",
                button_text="Select",
                sections=[{"title": "Quotes", "rows": rows}]
            )
        ]

    # ─────────────────────────────────────────────────────────
    # BUTTON HANDLER
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str) -> list:
        """Handle quote_* buttons."""
        if button_id == "quote_create":
            return self._start_create(phone_number)

        if button_id.startswith("quote_view_"):
            quote_id = button_id[11:]
            return self._view_quote(phone_number, quote_id)

        if button_id.startswith("quote_accept_"):
            quote_id = button_id[13:]
            return self._accept_quote(phone_number, quote_id)

        if button_id.startswith("quote_convert_"):
            quote_id = button_id[14:]
            return self._convert_to_sale(phone_number, quote_id)

        if button_id.startswith("quote_delete_"):
            quote_id = button_id[13:]
            return self._delete_quote(phone_number, quote_id)

        return self.show(phone_number)

    # ─────────────────────────────────────────────────────────
    # TEXT STATE HANDLER
    # ─────────────────────────────────────────────────────────

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during quote creation flow."""
        context = session.get("context", {})
        step = context.get("quote_step", "")
        text_s = text.strip()

        if text_s.lower() in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        if step == "ask_client":
            return self._step_client(phone_number, text_s, context)

        if step == "ask_description":
            return self._step_description(phone_number, text_s, context)

        if step == "ask_amount":
            return self._step_amount(phone_number, text_s, context)

        return self.show(phone_number)

    # ─────────────────────────────────────────────────────────
    # CREATE QUOTE FLOW
    # ─────────────────────────────────────────────────────────

    def _start_create(self, phone_number: str) -> list:
        """Start quote creation — ask for client name."""
        self.session.save(phone_number, states.INVOICING, {
            "quote_step": "ask_client",
            "quote_data": {},
        })

        return [text_response(
            "📝 *Create Quote*\n\n"
            "Step 1 of 3\n\n"
            "👤 *Who is this quote for?*\n\n"
            "_e.g. Mrs Ade, Zenith Bank, Dangote Office_\n\n"
            "_Type *cancel* to exit_"
        )]

    def _step_client(self, phone_number: str, text: str, context: dict) -> list:
        """Step 1: Save client, ask for description."""
        if len(text) < 2:
            return [text_response("Please enter the client name (at least 2 characters):")]

        quote_data = context.get("quote_data", {})
        quote_data["client"] = text.title()
        context["quote_data"] = quote_data
        context["quote_step"] = "ask_description"
        self.session.save(phone_number, states.INVOICING, context)

        return [text_response(
            f"👤 Client: *{text.title()}*\n\n"
            f"Step 2 of 3\n\n"
            f"📋 *What are you quoting for?*\n\n"
            f"_Describe the work/service:_\n"
            f"_e.g. Office deep cleaning + fumigation_\n"
            f"_e.g. Full website redesign (5 pages)_\n"
            f"_e.g. Hair braiding + coloring_"
        )]

    def _step_description(self, phone_number: str, text: str, context: dict) -> list:
        """Step 2: Save description, ask for amount."""
        if len(text) < 3:
            return [text_response("Please describe the work (at least 3 characters):")]

        quote_data = context.get("quote_data", {})
        quote_data["description"] = text
        context["quote_data"] = quote_data
        context["quote_step"] = "ask_amount"
        self.session.save(phone_number, states.INVOICING, context)

        return [text_response(
            f"📋 Work: _{text}_\n\n"
            f"Step 3 of 3\n\n"
            f"💰 *How much are you quoting?*\n\n"
            f"_e.g. 50000, 75K, 150K, 1.2M_"
        )]

    def _step_amount(self, phone_number: str, text: str, context: dict) -> list:
        """Step 3: Save amount and create the quote."""
        amount = parse_amount(text)
        if not amount:
            return [text_response("💰 Please enter a valid amount (e.g. 50000, 75K, 150K):")]

        quote_data = context.get("quote_data", {})
        client = quote_data.get("client", "Client")
        description = quote_data.get("description", "Service")

        # Generate quote ID
        user = self.db.get_user(phone_number) or {}
        quotes = user.get("quotes", [])
        last_num = len(quotes)
        quote_id = f"Q{last_num + 1:04d}"

        # Save quote
        now = datetime.now()
        valid_until = (now + timedelta(days=7)).strftime("%Y-%m-%d")

        new_quote = {
            "id": quote_id,
            "client": client,
            "description": description,
            "amount": int(amount),
            "status": "pending",
            "created_at": now.strftime("%Y-%m-%d"),
            "valid_until": valid_until,
            "converted_tx_id": None,
        }
        quotes.append(new_quote)
        self.db.update_user_field(phone_number, "quotes", quotes)

        self.session.reset(phone_number)

        # Build shareable quote text
        business_name = user.get("business_name", "My Business")
        quote_text = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📝  *QUOTE / ESTIMATE*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"From: *{business_name}*\n"
            f"To: *{client}*\n"
            f"Date: {now.strftime('%d/%m/%Y')}\n"
            f"Ref: {quote_id}\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 *{description}*\n"
            f"\n"
            f"💰 *Total: {format_amount(int(amount))}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"Valid until: {valid_until}\n"
            f"\n"
            f"_To accept, please confirm with {business_name}._\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

        return [
            text_response(f"✅ *Quote created!*\n\n{quote_text}"),
            text_response(
                "💡 *Tip:* Copy the quote above and send it to your client directly.\n\n"
                "When they accept, come back here and tap *Mark Accepted* → then *Convert to Sale*."
            ),
            button_response("What's next?", [
                {"id": "quote_create", "title": "➕ Another Quote"},
                {"id": "biz_quotes", "title": "📝 View Quotes"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    # ─────────────────────────────────────────────────────────
    # VIEW / MANAGE QUOTES
    # ─────────────────────────────────────────────────────────

    def _view_quote(self, phone_number: str, quote_id: str) -> list:
        """View a specific quote with action buttons."""
        user = self.db.get_user(phone_number) or {}
        quotes = user.get("quotes", [])

        quote = None
        for q in quotes:
            if q.get("id") == quote_id:
                quote = q
                break

        if not quote:
            return [text_response("❓ Quote not found.")]

        status = quote.get("status", "pending")
        client = quote.get("client", "")
        description = quote.get("description", "")
        amount = int(quote.get("amount", 0))
        created = quote.get("created_at", "")
        valid_until = quote.get("valid_until", "")

        status_display = {"pending": "⏳ Pending", "accepted": "✅ Accepted",
                          "converted": "💰 Converted", "expired": "🔴 Expired"}.get(status, status)

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📝  *Quote {quote_id}*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"👤 Client: *{client}*",
            f"📋 {description}",
            f"💰 Amount: *{format_amount(amount)}*",
            f"📅 Created: {created}",
            f"⏰ Valid until: {valid_until}",
            f"📊 Status: {status_display}",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        buttons = []
        if status == "pending":
            buttons.append({"id": f"quote_accept_{quote_id}", "title": "✅ Mark Accepted"})
            buttons.append({"id": f"quote_delete_{quote_id}", "title": "🗑️ Delete"})
        elif status == "accepted":
            buttons.append({"id": f"quote_convert_{quote_id}", "title": "💰 Convert to Sale"})
            buttons.append({"id": f"quote_delete_{quote_id}", "title": "🗑️ Delete"})

        if not buttons:
            buttons.append({"id": "biz_quotes", "title": "← Back to Quotes"})

        return [
            text_response("\n".join(lines)),
            button_response("Actions:", buttons)
        ]

    def _accept_quote(self, phone_number: str, quote_id: str) -> list:
        """Mark a quote as accepted by the client."""
        user = self.db.get_user(phone_number) or {}
        quotes = user.get("quotes", [])

        for q in quotes:
            if q.get("id") == quote_id:
                q["status"] = "accepted"
                break

        self.db.update_user_field(phone_number, "quotes", quotes)

        return [
            text_response(f"✅ Quote *{quote_id}* marked as accepted!"),
            button_response("Convert to sale now?", [
                {"id": f"quote_convert_{quote_id}", "title": "💰 Convert to Sale"},
                {"id": "biz_quotes", "title": "📝 View Quotes"},
            ])
        ]

    def _convert_to_sale(self, phone_number: str, quote_id: str) -> list:
        """Convert an accepted quote into a sale transaction + optionally generate invoice."""
        user = self.db.get_user(phone_number) or {}
        quotes = user.get("quotes", [])

        quote = None
        for q in quotes:
            if q.get("id") == quote_id:
                quote = q
                break

        if not quote:
            return [text_response("❓ Quote not found.")]

        client = quote.get("client", "Client")
        description = quote.get("description", "Service")
        amount = int(quote.get("amount", 0))

        # Record as sale transaction
        result = self.db.save_transaction(
            phone_number,
            amount,
            "sale",
            f"{description} (Quote {quote_id})",
            "Service Revenue",
            vendor=client,
            sub_category="Quoted Service",
        )
        tx_id = result.get("transaction_id", "") if result else ""

        # Update quote status
        quote["status"] = "converted"
        quote["converted_tx_id"] = tx_id
        self.db.update_user_field(phone_number, "quotes", quotes)

        # Update CRM
        try:
            self.db.update_contact_totals(phone_number, client, amount, "sale")
        except Exception:
            pass

        return [
            text_response(
                f"💰 *Quote converted to sale!*\n\n"
                f"📝 {quote_id} → ✅ Sale recorded\n"
                f"👤 Client: {client}\n"
                f"💰 Amount: {format_amount(amount)}\n"
                f"📋 {description}"
            ),
            button_response("What's next?", [
                {"id": f"gen_invoice_{tx_id}", "title": "🧾 Generate Invoice"},
                {"id": "record_sale", "title": "💼 Next Job"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]

    def _delete_quote(self, phone_number: str, quote_id: str) -> list:
        """Delete a quote."""
        user = self.db.get_user(phone_number) or {}
        quotes = user.get("quotes", [])

        new_quotes = [q for q in quotes if q.get("id") != quote_id]
        self.db.update_user_field(phone_number, "quotes", new_quotes)

        return [
            text_response(f"🗑️ Quote *{quote_id}* deleted."),
            button_response("What's next?", [
                {"id": "quote_create", "title": "➕ New Quote"},
                {"id": "biz_quotes", "title": "📝 View Quotes"},
                {"id": "menu_home", "title": "☰ Menu"},
            ])
        ]
