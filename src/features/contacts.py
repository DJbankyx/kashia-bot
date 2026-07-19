# src/features/contacts.py
"""CRM — full contact management for customers and suppliers.

Features:
  crm_all            — browse all contacts (tappable list)
  crm_add            — multi-step add contact form
  crm_top_customers  — top 10 by spending
  crm_top_suppliers  — top 10 by purchases
  crm_reminders      — debtors with reminder action
  crm_insights       — customer analytics (frequency, avg, last seen)
  Contact profile    — tap any contact for full detail
"""

import logging
import re

from core import states
from utils.whatsapp_ui import (
    text_response, button_response, list_response, format_amount
)

logger = logging.getLogger(__name__)

# State for multi-step add contact flow
CRM_ADDING = "CRM_ADDING"

# Filter bad vendor names
BAD_NAMES = {"sold", "bought", "paid", "received", "sale", "purchase",
             "expense", "income", "cash", "transfer", "unknown", "customer"}


class ContactsHandler:
    """Full CRM handler — contacts, profiles, rankings, reminders."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    # ─────────────────────────────────────────────────────────
    # BUTTON ENTRY POINTS
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Route all crm_* buttons."""

        if button_id == "crm_all":
            return self._show_all_contacts(phone_number)

        if button_id == "crm_add":
            return self._start_add_contact(phone_number)

        if button_id == "crm_top_customers":
            return self._show_top(phone_number, "customer")

        if button_id == "crm_top_suppliers":
            return self._show_top(phone_number, "supplier")

        if button_id == "crm_reminders":
            return self._show_reminders(phone_number)

        if button_id == "crm_insights":
            return self._show_insights(phone_number)

        # ── Contact profile tap (crm_view_[contact_id]) ──
        if button_id.startswith("crm_view_"):
            contact_id = button_id[9:]
            return self._show_profile(phone_number, contact_id)

        # ── CRM hint buttons (from transaction flow) ──
        if button_id in ("crm_cash", "crm_transfer", "crm_credit"):
            return None  # Let router handle these

        return [text_response("👆 Pick an option from the CRM menu.")]

    # ─────────────────────────────────────────────────────────
    # STATE HANDLER — for multi-step add contact
    # ─────────────────────────────────────────────────────────

    def handle(self, phone_number: str, text: str, session: dict) -> list:
        """Handle text input during add contact flow."""
        context = session.get("context", {})
        step    = context.get("crm_step", "")
        text_s  = text.strip()

        if text_s.lower() in ("cancel", "exit", "back"):
            self.session.reset(phone_number)
            return [text_response("👍 Cancelled.")]

        if step == "ask_name":
            return self._add_step_name(phone_number, text_s, context)

        if step == "ask_phone":
            return self._add_step_phone(phone_number, text_s, context)

        if step == "ask_type":
            return self._add_step_type(phone_number, text_s, context)

        self.session.reset(phone_number)
        return [text_response("Something went wrong. Try again from the CRM menu.")]

    # ─────────────────────────────────────────────────────────
    # SHOW — legacy text shortcut
    # ─────────────────────────────────────────────────────────

    def show(self, phone_number: str) -> list:
        """Text shortcut — show contacts summary."""
        return self._show_all_contacts(phone_number)

    def save_contact_from_text(self, phone_number: str, text: str) -> list:
        """Parse 'save number [name] [phone]' and save contact."""
        cleaned = re.sub(r'^save\s+(?:number|contact)\s+', '', text, flags=re.IGNORECASE).strip()
        phone_match = re.search(r'(\d[\d\s]{9,15}\d)', cleaned)

        if not phone_match:
            return [text_response(
                "📱 Include a valid phone number:\n\n"
                "_Save number Sandra 08060475064_"
            )]

        contact_phone = phone_match.group(1).replace(" ", "")
        if len(contact_phone) < 10 or len(contact_phone) > 14:
            return [text_response("📱 That doesn't look like a valid phone number.")]

        name = cleaned[:phone_match.start()].strip()
        if not name:
            name = cleaned[phone_match.end():].strip()
        if not name:
            return [text_response("👤 Include a name:\n\n_Save number Sandra 08060475064_")]

        self.db.save_contact(phone_number, name, "customer", contact_phone)
        return [text_response(f"✅ *{name}* saved — {contact_phone}")]

    # ─────────────────────────────────────────────────────────
    # ALL CONTACTS — tappable list
    # ─────────────────────────────────────────────────────────

    def _show_all_contacts(self, phone_number: str) -> list:
        """Show all contacts as a tappable list menu."""
        contacts = self.db.get_contacts(phone_number, limit=100) or []

        # Filter out junk names
        contacts = [c for c in contacts
                    if c.get("name", "").lower().strip() not in BAD_NAMES
                    and len(c.get("name", "")) > 1]

        if not contacts:
            return [text_response(
                "📇 *Contacts*\n\n"
                "No contacts saved yet.\n\n"
                "Contacts are created automatically when you record transactions "
                "with a name.\n\n"
                "Or tap ➕ *Add Contact* from the CRM menu."
            )]

        # Sort by most recent activity
        contacts.sort(
            key=lambda c: c.get("last_transaction_date", ""),
            reverse=True
        )

        # Build list menu (max 10 rows per section)
        rows = []
        for c in contacts[:10]:
            name    = c.get("name", "Unknown")
            c_type  = c.get("type", "contact")
            c_id    = c.get("contact_id", name.lower().replace(" ", "_"))
            total   = int(c.get("total_received", 0)) + int(c.get("total_paid", 0))
            debt    = int(c.get("debt_owed_to_me", 0))

            # Build description
            emoji   = "👤" if c_type == "customer" else "🏪" if c_type == "supplier" else "📇"
            desc    = f"{c_type.title()} · {format_amount(total)} total"
            if debt > 0:
                desc = f"Owes {format_amount(debt)} · {format_amount(total)} total"

            rows.append({
                "id": f"crm_view_{c_id}",
                "title": f"{emoji} {name}"[:24],
                "description": desc[:72],
            })

        total_count = len(contacts)
        body = f"📇 *{total_count} contact{'s' if total_count != 1 else ''}*\n\nTap a name to see their full profile:"

        result = [list_response(
            header="📇 Contacts",
            body=body,
            button_text="View Contact",
            sections=[{"title": "Recent Contacts", "rows": rows}]
        )]

        if total_count > 10:
            result.append(text_response(
                f"_Showing 10 of {total_count}. "
                f"Type a name to search, e.g. \"profile Sandra\"_"
            ))

        return result

    # ─────────────────────────────────────────────────────────
    # ADD CONTACT — multi-step form
    # ─────────────────────────────────────────────────────────

    def _start_add_contact(self, phone_number: str) -> list:
        """Start add contact flow."""
        self.session.save(phone_number, CRM_ADDING, {
            "crm_step": "ask_name",
        })
        return [text_response(
            "➕ *Add Contact*\n\n"
            "What is their name?\n\n"
            "_Type *cancel* to go back._"
        )]

    def _add_step_name(self, phone_number: str, name: str, context: dict) -> list:
        """Step 1 — save name, ask for phone."""
        if len(name) < 2:
            return [text_response("Please enter a valid name (at least 2 characters):")]

        context["contact_name"] = name
        context["crm_step"] = "ask_phone"
        self.session.save(phone_number, CRM_ADDING, context)

        return [text_response(
            f"👤 *{name}*\n\n"
            f"📱 What is their phone number?\n\n"
            f"_Type *skip* if you don't have it._"
        )]

    def _add_step_phone(self, phone_number: str, text: str, context: dict) -> list:
        """Step 2 — save phone, ask for type."""
        if text.lower() == "skip":
            context["contact_phone"] = ""
        else:
            phone_clean = re.sub(r'[^\d]', '', text)
            if len(phone_clean) < 10 or len(phone_clean) > 14:
                return [text_response(
                    "📱 Enter a valid phone number (10-14 digits) or type *skip*:"
                )]
            context["contact_phone"] = phone_clean

        context["crm_step"] = "ask_type"
        self.session.save(phone_number, CRM_ADDING, context)

        name = context.get("contact_name", "")
        return [button_response(
            f"👤 *{name}*\n\nIs this person a customer or supplier?",
            [
                {"id": "crm_type_customer", "title": "👤 Customer"},
                {"id": "crm_type_supplier", "title": "🏪 Supplier"},
                {"id": "crm_type_both",     "title": "🔄 Both"},
            ]
        )]

    def _add_step_type(self, phone_number: str, text: str, context: dict) -> list:
        """Step 3 — save type, complete."""
        text_low = text.lower().strip()

        type_map = {
            "crm_type_customer": "customer",
            "crm_type_supplier": "supplier",
            "crm_type_both": "both",
            "customer": "customer",
            "supplier": "supplier",
            "both": "both",
            "1": "customer",
            "2": "supplier",
            "3": "both",
        }
        contact_type = type_map.get(text_low, "customer")

        name  = context.get("contact_name", "Contact")
        phone = context.get("contact_phone", "")

        self.db.save_contact(phone_number, name, contact_type, phone)
        self.session.reset(phone_number)

        phone_line = f"\n📱 {phone}" if phone else ""
        type_emoji = {"customer": "👤", "supplier": "🏪", "both": "🔄"}.get(contact_type, "📇")

        return [text_response(
            f"✅ *Contact Saved!*\n\n"
            f"{type_emoji} {name}{phone_line}\n"
            f"🏷️ {contact_type.title()}\n\n"
            f"_Their profile will build up as you record transactions with them._"
        )]

    # ─────────────────────────────────────────────────────────
    # TOP CUSTOMERS / SUPPLIERS
    # ─────────────────────────────────────────────────────────

    def _show_top(self, phone_number: str, contact_type: str) -> list:
        """Show top contacts ranked by total value."""
        top = self.db.get_top_contacts(phone_number, contact_type, limit=10)

        label = "Customers" if contact_type == "customer" else "Suppliers"
        emoji = "💰" if contact_type == "customer" else "🏪"
        field = "total_received" if contact_type == "customer" else "total_paid"

        if not top:
            return [text_response(
                f"{emoji} *Top {label}*\n\n"
                f"No {label.lower()} yet. They'll appear after you record transactions."
            )]

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"{emoji}  *Top {label}*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
        ]

        for i, c in enumerate(top, 1):
            name    = c.get("name", "Unknown")
            total   = int(c.get(field, 0))
            tx_cnt  = int(c.get("transaction_count", 0))
            debt    = int(c.get("debt_owed_to_me", 0)) if contact_type == "customer" else 0

            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f" {i}.")
            lines.append(f"{medal} *{name}*")
            lines.append(f"     {format_amount(total)} · {tx_cnt} orders")
            if debt > 0:
                lines.append(f"     ⚠️ Owes {format_amount(debt)}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"_Tap CRM → All Contacts to see profiles_")

        return [text_response("\n".join(lines))]

    # ─────────────────────────────────────────────────────────
    # CONTACT PROFILE — detailed view
    # ─────────────────────────────────────────────────────────

    def _show_profile(self, phone_number: str, contact_id: str) -> list:
        """Show full profile for one contact using analytics."""
        # Try to get analytics by contact_id (which is name-based)
        contact_name = contact_id.replace("_", " ").title()
        analytics = self.db.get_contact_analytics(phone_number, contact_name)

        if not analytics:
            # Try exact contact_id lookup
            contact = self.db.get_contact_by_name(phone_number, contact_name)
            if contact:
                analytics = self.db.get_contact_analytics(
                    phone_number, contact.get("name", contact_name)
                )

        if not analytics:
            return [text_response(f"❓ Contact *{contact_name}* not found.")]

        name         = analytics["name"]
        c_type       = analytics["type"]
        c_phone      = analytics.get("contact_phone", "")
        total_recv   = analytics["total_received"]
        total_paid   = analytics["total_paid"]
        tx_count     = analytics["transaction_count"]
        avg_order    = analytics["avg_order_value"]
        avg_days     = analytics["avg_days_between"]
        first_date   = analytics["first_purchase_date"]
        last_date    = analytics["last_transaction_date"]
        days_inactive = analytics["days_inactive"]
        rel_days     = analytics["relationship_days"]
        debt_owed    = analytics["debt_owed_to_me"]
        debt_mine    = analytics["debt_i_owe"]
        notes        = analytics.get("notes", "")

        type_emoji = {"customer": "👤", "supplier": "🏪", "both": "🔄"}.get(c_type, "📇")

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"{type_emoji}  *{name}*",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        if c_phone:
            lines.append(f"📱 {c_phone}")
        lines.append(f"🏷️ {c_type.title()}")
        lines.append("")

        # ── Financials ──
        lines.append("💰 *Financials*")
        if total_recv > 0:
            lines.append(f"  Bought from you: {format_amount(total_recv)}")
        if total_paid > 0:
            lines.append(f"  You bought from them: {format_amount(total_paid)}")
        if avg_order > 0:
            lines.append(f"  Avg order: {format_amount(avg_order)}")
        lines.append(f"  Transactions: {tx_count}")
        lines.append("")

        # ── Timeline ──
        if first_date or last_date:
            lines.append("📅 *Timeline*")
            if first_date:
                lines.append(f"  First: {first_date}")
            if last_date:
                inactive_str = ""
                if days_inactive is not None:
                    if days_inactive == 0:
                        inactive_str = " (today)"
                    elif days_inactive == 1:
                        inactive_str = " (yesterday)"
                    else:
                        inactive_str = f" ({days_inactive} days ago)"
                lines.append(f"  Last: {last_date}{inactive_str}")
            if rel_days and rel_days > 0:
                lines.append(f"  Relationship: {rel_days} days")
            if avg_days > 0:
                lines.append(f"  Buys every ~{avg_days} days")
            lines.append("")

        # ── Debts ──
        if debt_owed > 0 or debt_mine > 0:
            lines.append("💳 *Debts*")
            if debt_owed > 0:
                lines.append(f"  ⚠️ Owes you: {format_amount(debt_owed)}")
            if debt_mine > 0:
                lines.append(f"  📝 You owe them: {format_amount(debt_mine)}")
            lines.append("")

        # ── Notes ──
        if notes:
            lines.append(f"📝 _{notes}_")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")

        # Action buttons
        buttons = []
        if debt_owed > 0:
            buttons.append({"id": f"debt_remind_{contact_id}", "title": "⏰ Send Reminder"})
        buttons.append({"id": "crm_all", "title": "← All Contacts"})
        # WhatsApp max 3 buttons
        buttons = buttons[:3]

        return [
            text_response("\n".join(lines)),
            button_response("What would you like to do?", buttons)
        ]

    # ─────────────────────────────────────────────────────────
    # DEBT REMINDERS
    # ─────────────────────────────────────────────────────────

    def _show_reminders(self, phone_number: str) -> list:
        """Show all debtors with outstanding balances."""
        debtors = self.db.get_all_debtors(phone_number) or []

        if not debtors:
            return [text_response(
                "⏰ *Debt Reminders*\n\n"
                "✅ No outstanding debts! Nobody owes you money right now.\n\n"
                "_When someone buys on credit, they'll appear here._"
            )]

        total = sum(d["amount"] for d in debtors)

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"⏰  *Debt Reminders*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"*Total owed to you: {format_amount(total)}*",
            f"",
        ]

        for i, d in enumerate(debtors[:10], 1):
            name   = d.get("name", "Unknown")
            amount = d.get("amount", 0)
            date   = d.get("last_date", "")

            lines.append(f"{i}. *{name}* — {format_amount(amount)}")
            if date:
                lines.append(f"   Since: {date}")

        if len(debtors) > 10:
            lines.append(f"\n_...and {len(debtors) - 10} more_")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("_To send a reminder, tap a contact from All Contacts._")
        lines.append("_Or type: remind [name]_")

        return [text_response("\n".join(lines))]

    # ─────────────────────────────────────────────────────────
    # CUSTOMER INSIGHTS
    # ─────────────────────────────────────────────────────────

    def _show_insights(self, phone_number: str) -> list:
        """Show customer behaviour insights."""
        contacts = self.db.get_contacts(phone_number, limit=100) or []

        # Filter to real contacts
        contacts = [c for c in contacts
                    if c.get("name", "").lower() not in BAD_NAMES
                    and int(c.get("transaction_count", 0)) > 0]

        if not contacts:
            return [text_response(
                "📊 *Customer Insights*\n\n"
                "Not enough data yet. Record more transactions to see patterns."
            )]

        # Calculate insights
        customers = [c for c in contacts if c.get("type") == "customer"]
        total_customers = len(customers)
        total_revenue = sum(int(c.get("total_received", 0)) for c in customers)
        avg_customer_value = total_revenue // total_customers if total_customers > 0 else 0

        # Find most valuable customer
        if customers:
            customers.sort(key=lambda x: int(x.get("total_received", 0)), reverse=True)
            top_customer = customers[0]
        else:
            top_customer = None

        # Find most frequent buyer
        freq_sorted = sorted(
            [c for c in customers if int(c.get("transaction_count", 0)) > 1],
            key=lambda x: int(x.get("transaction_count", 0)),
            reverse=True
        )
        most_frequent = freq_sorted[0] if freq_sorted else None

        # Inactive customers (no activity in 14+ days)
        inactive = [c for c in customers
                    if c.get("last_transaction_date", "") and
                    _days_since(c.get("last_transaction_date", "")) and
                    _days_since(c.get("last_transaction_date", "")) > 14]

        # Build insights
        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📊  *Customer Insights*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"👥 Total customers: *{total_customers}*",
            f"💰 Total revenue: *{format_amount(total_revenue)}*",
            f"📐 Avg customer value: *{format_amount(avg_customer_value)}*",
            f"",
        ]

        if top_customer:
            lines.append(f"🥇 *Best Customer:*")
            lines.append(f"   {top_customer.get('name', '?')} — {format_amount(top_customer.get('total_received', 0))}")
            lines.append("")

        if most_frequent:
            tx_count = int(most_frequent.get("transaction_count", 0))
            lines.append(f"🔁 *Most Frequent:*")
            lines.append(f"   {most_frequent.get('name', '?')} — {tx_count} orders")
            lines.append("")

        if inactive:
            lines.append(f"⚠️ *Inactive ({len(inactive)} customers):*")
            for c in inactive[:3]:
                name = c.get("name", "?")
                days = _days_since(c.get("last_transaction_date", ""))
                lines.append(f"   {name} — {days} days ago")
            if len(inactive) > 3:
                lines.append(f"   _...and {len(inactive) - 3} more_")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("_Record more transactions to improve these insights._")

        return [text_response("\n".join(lines))]


# ─────────────────────────────────────────────────────────
# MODULE HELPERS
# ─────────────────────────────────────────────────────────

def _days_since(date_str: str):
    """Calculate days since a date string (YYYY-MM-DD)."""
    if not date_str:
        return None
    try:
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return (datetime.now() - dt).days
    except Exception:
        return None
