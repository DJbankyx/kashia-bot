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
from utils.parser import BAD_VENDOR_NAMES

logger = logging.getLogger(__name__)

# State for multi-step add contact flow
CRM_ADDING = "CRM_ADDING"

# Filter bad vendor names — shared single source of truth (utils.parser)
BAD_NAMES = BAD_VENDOR_NAMES


class ContactsHandler:
    """Full CRM handler — contacts, profiles, rankings, reminders."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database
        self.router = None  # set by main.py; used for the record-sale shortcut

    def _is_telegram(self, phone_number: str) -> bool:
        try:
            from services.messaging_client import platform_for_user
            return platform_for_user(phone_number) == "telegram"
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────
    # BUTTON ENTRY POINTS
    # ─────────────────────────────────────────────────────────

    def handle_button(self, phone_number: str, button_id: str, session: dict) -> list:
        """Route all crm_* buttons."""

        if button_id == "crm_all":
            # Telegram gets a filtered, paginated, tappable browse; WhatsApp keeps
            # the existing simple list.
            if self._is_telegram(phone_number):
                return self._browse(phone_number, "customer")
            return self._show_all_contacts(phone_number)

        if button_id == "crm_customers":
            return self._browse(phone_number, "customer")

        if button_id == "crm_suppliers":
            return self._browse(phone_number, "supplier")

        if button_id == "crm_search":
            return self._start_search(phone_number)

        if button_id == "crm_add":
            return self._start_add_contact(phone_number)

        if button_id == "crm_top_customers":
            # Consolidated: ranked browse of customers.
            if self._is_telegram(phone_number):
                return self._browse(phone_number, "customer", ranked=True)
            return self._show_top(phone_number, "customer")

        if button_id == "crm_top_suppliers":
            if self._is_telegram(phone_number):
                return self._browse(phone_number, "supplier", ranked=True)
            return self._show_top(phone_number, "supplier")

        if button_id == "crm_reminders":
            return self._show_reminders(phone_number)

        if button_id == "crm_creditors":
            return self._show_creditors(phone_number)

        if button_id == "crm_insights":
            return self._show_insights(phone_number)

        # ── Contact profile tap (crm_view_[contact_id]) ──
        if button_id.startswith("crm_view_"):
            contact_id = button_id[9:]
            return self._show_profile(phone_number, contact_id)

        # ── Contact edit actions (crm_edit_*, crm_del_*) ──
        if button_id.startswith("crm_editname_"):
            return self._edit_field_prompt(phone_number, button_id[13:], "name")
        if button_id.startswith("crm_editphone_"):
            return self._edit_field_prompt(phone_number, button_id[14:], "phone")
        if button_id.startswith("crm_editnote_"):
            return self._edit_field_prompt(phone_number, button_id[13:], "note")
        if button_id.startswith("crm_edittype_"):
            return self._edit_type_menu(phone_number, button_id[13:])
        if button_id.startswith("crm_settype_"):
            # crm_settype_<type>_<contact_id>
            rest = button_id[12:]
            new_type, _, cid = rest.partition("_")
            return self._apply_type(phone_number, cid, new_type)
        if button_id.startswith("crm_edit_"):
            return self._edit_menu(phone_number, button_id[9:])
        if button_id.startswith("crm_del_"):
            return self._confirm_delete(phone_number, button_id[8:])
        if button_id.startswith("crm_delyes_"):
            return self._do_delete(phone_number, button_id[11:])
        if button_id.startswith("crm_stmt_"):
            return self._show_statement(phone_number, button_id[9:])

        # ── Record sale/purchase pre-filled with this contact ──
        if button_id.startswith("crm_recsale_"):
            return self._record_to_contact(phone_number, button_id[12:], "record_sale")
        if button_id.startswith("crm_recbuy_"):
            return self._record_to_contact(phone_number, button_id[11:], "record_purchase")

        # ── Message a contact (tappable wa.me / tel link) ──
        if button_id.startswith("crm_wa_"):
            return self._message_link(phone_number, button_id[7:])

        # ── CRM hint buttons (from transaction flow) ──
        if button_id in ("crm_cash", "crm_transfer", "crm_credit"):
            return None  # Let router handle these

        return [text_response("👆 Pick an option from the CRM menu.")]

    def _record_to_contact(self, phone_number: str, contact_id: str, record_button: str) -> list:
        """Start the tidy-box sale/purchase flow pre-filled with this contact."""
        name = contact_id.replace("_", " ").title()
        # Try to resolve the real stored name/casing.
        contact = self.db.get_contact_by_name(phone_number, name)
        if contact and contact.get("name"):
            name = contact["name"]
        if self.router is not None:
            try:
                return self.router._start_guided_recording(
                    phone_number, record_button, preset_vendor=name)
            except TypeError:
                # Router not yet updated for preset_vendor — fall back gracefully.
                return self.router._start_guided_recording(phone_number, record_button)
        return [text_response("Open the menu and tap Record Sale.")]

    def _message_link(self, phone_number: str, contact_id: str) -> list:
        """Give a tappable link to message/call the contact (wa.me + tel)."""
        name = contact_id.replace("_", " ").title()
        contact = self.db.get_contact_by_name(phone_number, name)
        cphone = (contact or {}).get("contact_phone", "")
        if not cphone:
            return [text_response(f"📱 No phone number saved for *{name}*.")]
        # Normalise to international-ish for wa.me (strip leading 0, assume NG if 10-11 digits).
        digits = re.sub(r'[^\d]', '', cphone)
        wa = digits
        if digits.startswith("0") and len(digits) == 11:
            wa = "234" + digits[1:]
        return [text_response(
            f"💬 *Message {name}*\n\n"
            f"📱 {cphone}\n\n"
            f"WhatsApp: https://wa.me/{wa}\n"
            f"Call: tel:{cphone}"
        )]

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

        if step == "search":
            return self._do_search(phone_number, text_s)

        if step == "edit_value":
            return self._save_edit_value(phone_number, text_s, context)

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

    # ═════════════════════════════════════════════════════════
    # TELEGRAM CRM — filtered/paginated browse, boxed card, edit,
    # search, statement. WhatsApp keeps the legacy methods below.
    # ═════════════════════════════════════════════════════════

    def crm_home(self, phone_number: str) -> list:
        """Telegram CRM home: clean, tap-first. WhatsApp uses the industry menu."""
        cust_label, supp_label = self._type_labels(phone_number)
        return [list_response(
            header="👥 Contacts",
            body="Manage your people — tap to browse:",
            button_text="Select",
            sections=[{"title": "", "rows": [
                {"id": "crm_customers", "title": f"👤 {cust_label}"},
                {"id": "crm_suppliers", "title": "🏪 Suppliers"},
                {"id": "crm_search", "title": "🔍 Search"},
                {"id": "crm_add", "title": "➕ Add Contact"},
                {"id": "crm_reminders", "title": "🔴 Who Owes Me"},
                {"id": "crm_creditors", "title": "📝 Who I Owe"},
                {"id": "crm_insights", "title": "📊 Insights"},
            ]}]
        )]

    def _type_labels(self, phone_number: str):
        """Industry-aware labels: services calls customers 'Clients'."""
        try:
            user = self.db.get_user(phone_number) or {}
            industry = user.get("industry_class", user.get("business_type", "trading"))
        except Exception:
            industry = "trading"
        cust = "Clients" if industry == "services" else "Customers"
        return cust, "Suppliers"

    def _contact_matches_type(self, contact: dict, want: str) -> bool:
        t = (contact.get("type") or "").lower()
        if want == "customer":
            return t in ("customer", "both", "", "client")
        if want == "supplier":
            return t in ("supplier", "both")
        return True

    def _clean_contacts(self, phone_number: str) -> list:
        contacts = self.db.get_contacts(phone_number, limit=100) or []
        return [c for c in contacts
                if c.get("name", "").lower().strip() not in BAD_NAMES
                and len(c.get("name", "")) > 1]

    def _browse(self, phone_number: str, want: str, ranked: bool = False) -> list:
        """Filtered, tappable, auto-paginated browse of customers OR suppliers.

        Rows carry NO description → the engine's Telegram pager auto-paginates
        (Prev/Next) and row taps (crm_view_<id>) open the contact card.
        """
        cust_label, supp_label = self._type_labels(phone_number)
        label = cust_label if want == "customer" else supp_label
        emoji = "👤" if want == "customer" else "🏪"

        contacts = [c for c in self._clean_contacts(phone_number)
                    if self._contact_matches_type(c, want)]

        if not contacts:
            return [text_response(
                f"{emoji} *{label}*\n\n"
                f"No {label.lower()} yet.\n\n"
                f"_They're added automatically when you record a transaction with "
                f"a name — or tap ➕ Add Contact._"
            )]

        # Rank by value (customers by received, suppliers by paid) or recency.
        val_field = "total_received" if want == "customer" else "total_paid"
        if ranked:
            contacts.sort(key=lambda c: int(c.get(val_field, 0) or 0), reverse=True)
        else:
            contacts.sort(key=lambda c: c.get("last_transaction_date", ""), reverse=True)

        rows = []
        for c in contacts:
            name = c.get("name", "Unknown")
            cid = c.get("contact_id", name.lower().replace(" ", "_"))
            debt = int(c.get("debt_owed_to_me", 0) or 0)
            # Short suffix on the label (no description, so the pager engages).
            if debt > 0:
                suffix = f" · owes {format_amount(debt)}"
            else:
                total = int(c.get(val_field, 0) or 0)
                suffix = f" · {format_amount(total)}" if total else ""
            title = f"{emoji} {name}{suffix}"
            rows.append({"id": f"crm_view_{cid}"[:60], "title": title[:60]})

        body = f"👥 *{len(rows)} {label.lower()}*\nTap a name to open their profile:"
        return [list_response(
            header=f"{emoji} {label}",
            body=body,
            button_text="View",
            sections=[{"title": "", "rows": rows}],
        )]

    # ── Search ───────────────────────────────────────────────

    def _start_search(self, phone_number: str) -> list:
        self.session.save(phone_number, CRM_ADDING, {"crm_step": "search"})
        return [text_response(
            "🔍 *Search contacts*\n\nType a name (or part of it):\n\n_Type *cancel* to go back._"
        )]

    def _do_search(self, phone_number: str, query: str) -> list:
        self.session.reset(phone_number)
        q = query.strip().lower()
        if len(q) < 1:
            return [text_response("🔍 Type at least one letter to search.")]
        matches = [c for c in self._clean_contacts(phone_number)
                   if q in c.get("name", "").lower()]
        if not matches:
            return [text_response(
                f"🔍 No contact matches *{query}*.\n\n_Try fewer letters, or ➕ Add Contact._"
            )]
        if len(matches) == 1:
            cid = matches[0].get("contact_id", matches[0].get("name", "").lower().replace(" ", "_"))
            return self._show_profile(phone_number, cid)
        rows = []
        for c in matches:
            name = c.get("name", "Unknown")
            cid = c.get("contact_id", name.lower().replace(" ", "_"))
            t = (c.get("type") or "contact").title()
            rows.append({"id": f"crm_view_{cid}"[:60], "title": f"👤 {name} · {t}"[:60]})
        return [list_response(
            header="🔍 Search results",
            body=f"Found *{len(rows)}* — tap one:",
            button_text="View",
            sections=[{"title": "", "rows": rows}],
        )]

    # ─────────────────────────────────────────────────────────
    # ALL CONTACTS — tappable list (WhatsApp legacy)
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

        # ── Debt-first card body (Telegram): lead with what they owe ──
        if self._is_telegram(phone_number):
            card = [f"{type_emoji} *{name}*"]
            if c_phone:
                card.append(f"📱 {c_phone}")
            card.append(f"🏷️ {c_type.title()}")
            card.append("")
            # Debt first — the thing an owner checks first.
            if debt_owed > 0:
                card.append(f"🔴 *Owes you: {format_amount(debt_owed)}*")
            if debt_mine > 0:
                card.append(f"📝 *You owe them: {format_amount(debt_mine)}*")
            if debt_owed <= 0 and debt_mine <= 0:
                card.append("✅ No outstanding balance")
            card.append("")
            # Then the financial history.
            if total_recv > 0:
                card.append(f"💰 Bought from you: {format_amount(total_recv)}")
            if total_paid > 0:
                card.append(f"📦 You bought from them: {format_amount(total_paid)}")
            if avg_order > 0:
                card.append(f"📐 Avg order: {format_amount(avg_order)}")
            card.append(f"🧾 Transactions: {tx_count}")
            if last_date:
                inactive_str = ""
                if days_inactive is not None:
                    inactive_str = " (today)" if days_inactive == 0 else (
                        " (yesterday)" if days_inactive == 1 else f" ({days_inactive}d ago)")
                card.append(f"📅 Last: {last_date}{inactive_str}")
            if notes:
                card.append(f"\n📝 _{notes}_")

            # Grided actions (no 3-button cap on Telegram).
            buttons = []
            if debt_owed > 0:
                buttons.append({"id": f"debt_remind_{contact_id}", "title": "⏰ Remind"})
            buttons.append({"id": f"crm_stmt_{contact_id}", "title": "📄 Statement"})
            # Record sale/purchase to this contact (pre-fills them).
            verb = "💰 Record sale" if c_type != "supplier" else "📦 Record purchase"
            rec_id = "crm_recsale_" if c_type != "supplier" else "crm_recbuy_"
            buttons.append({"id": f"{rec_id}{contact_id}", "title": verb})
            if c_phone:
                buttons.append({"id": f"crm_wa_{contact_id}", "title": "💬 Message"})
            buttons.append({"id": f"crm_edit_{contact_id}", "title": "✏️ Edit"})
            buttons.append({"id": "crm_all", "title": "← Contacts"})

            return [
                text_response("\n".join(card)),
                button_response("Actions:", buttons)
            ]

        # WhatsApp: keep the simple 3-button version.
        buttons = []
        if debt_owed > 0:
            buttons.append({"id": f"debt_remind_{contact_id}", "title": "⏰ Send Reminder"})
        buttons.append({"id": "crm_all", "title": "← All Contacts"})
        buttons = buttons[:3]

        return [
            text_response("\n".join(lines)),
            button_response("What would you like to do?", buttons)
        ]

    # ─────────────────────────────────────────────────────────
    # EDIT CONTACT
    # ─────────────────────────────────────────────────────────

    def _edit_menu(self, phone_number: str, contact_id: str) -> list:
        name = contact_id.replace("_", " ").title()
        return [button_response(
            f"✏️ *Edit {name}*\n\nWhat do you want to change?",
            [
                {"id": f"crm_editname_{contact_id}", "title": "✏️ Name"},
                {"id": f"crm_editphone_{contact_id}", "title": "📱 Phone"},
                {"id": f"crm_edittype_{contact_id}", "title": "🏷️ Type"},
                {"id": f"crm_editnote_{contact_id}", "title": "📝 Note"},
                {"id": f"crm_del_{contact_id}", "title": "🗑️ Delete"},
                {"id": f"crm_view_{contact_id}", "title": "← Back"},
            ]
        )]

    def _edit_field_prompt(self, phone_number: str, contact_id: str, field: str) -> list:
        name = contact_id.replace("_", " ").title()
        prompts = {
            "name": "Type the new *name*:",
            "phone": "Type the new *phone number* (or *skip* to clear):",
            "note": "Type a *note* for this contact:",
        }
        self.session.save(phone_number, CRM_ADDING, {
            "crm_step": "edit_value",
            "edit_contact_id": contact_id,
            "edit_field": field,
            "edit_contact_name": name,
        })
        return [text_response(
            f"✏️ *{name}*\n\n{prompts.get(field, 'Type the new value:')}\n\n_Type *cancel* to go back._"
        )]

    def _save_edit_value(self, phone_number: str, value: str, context: dict) -> list:
        field = context.get("edit_field", "")
        cid = context.get("edit_contact_id", "")
        name = context.get("edit_contact_name", cid.replace("_", " ").title())
        self.session.reset(phone_number)
        value = value.strip()

        if field == "name":
            if len(value) < 2:
                return [text_response("Please enter a valid name (2+ characters).")]
            self.db.rename_contact(phone_number, name, value)
            new_id = value.lower().replace(" ", "_")
            return self._show_profile(phone_number, new_id)
        if field == "phone":
            phone_clean = "" if value.lower() == "skip" else re.sub(r'[^\d]', '', value)
            if phone_clean and (len(phone_clean) < 10 or len(phone_clean) > 14):
                return [text_response("📱 Enter a valid phone (10-14 digits) or *skip*.")]
            self.db.update_contact_profile(phone_number, name, {"contact_phone": phone_clean})
            return self._show_profile(phone_number, cid)
        if field == "note":
            self.db.update_contact_note(phone_number, name, value)
            return self._show_profile(phone_number, cid)
        return self._show_profile(phone_number, cid)

    def _edit_type_menu(self, phone_number: str, contact_id: str) -> list:
        name = contact_id.replace("_", " ").title()
        return [button_response(
            f"🏷️ *{name}* — set type:",
            [
                {"id": f"crm_settype_customer_{contact_id}", "title": "👤 Customer"},
                {"id": f"crm_settype_supplier_{contact_id}", "title": "🏪 Supplier"},
                {"id": f"crm_settype_both_{contact_id}", "title": "🔄 Both"},
            ]
        )]

    def _apply_type(self, phone_number: str, contact_id: str, new_type: str) -> list:
        name = contact_id.replace("_", " ").title()
        if new_type in ("customer", "supplier", "both"):
            self.db.update_contact_profile(phone_number, name, {"type": new_type})
        return self._show_profile(phone_number, contact_id)

    def _confirm_delete(self, phone_number: str, contact_id: str) -> list:
        name = contact_id.replace("_", " ").title()
        return [button_response(
            f"🗑️ *Delete {name}?*\n\nThis removes the contact (their recorded "
            f"transactions stay). This can't be undone.",
            [
                {"id": f"crm_delyes_{contact_id}", "title": "🗑️ Yes, Delete"},
                {"id": f"crm_view_{contact_id}", "title": "← Keep"},
            ]
        )]

    def _do_delete(self, phone_number: str, contact_id: str) -> list:
        name = contact_id.replace("_", " ").title()
        self.db.delete_contact(phone_number, name)
        return [
            text_response(f"🗑️ *{name}* deleted."),
            button_response("Contacts:", [{"id": "crm_all", "title": "👥 View Contacts"},
                                          {"id": "menu_home", "title": "☰ Menu"}])
        ]

    # ─────────────────────────────────────────────────────────
    # PER-CONTACT STATEMENT (in-chat)
    # ─────────────────────────────────────────────────────────

    def _show_statement(self, phone_number: str, contact_id: str) -> list:
        """An in-chat account statement for one contact: their transactions +
        running balance owed. (PDF version is a later Documents-stage add.)"""
        name = contact_id.replace("_", " ").title()
        analytics = self.db.get_contact_analytics(phone_number, name)
        if not analytics:
            return [text_response(f"❓ Contact *{name}* not found.")]
        txns = self.db.get_contact_transactions(phone_number, name, limit=20) or []

        lines = [
            "━━━━━━━━━━━━━━━━━━━━",
            f"📄 *Statement — {analytics['name']}*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]
        if not txns:
            lines.append("_No transactions recorded with this contact yet._")
        else:
            for t in txns:
                date = (t.get("date", "") or "")[-5:]
                amt = int(t.get("amount", 0) or 0)
                desc = t.get("description", t.get("item_name", "")) or t.get("type", "entry")
                ttype = t.get("type", "")
                sign = "＋" if ttype in ("sale", "income") else "－"
                lines.append(f"{date}  {sign}{format_amount(amt)}  _{desc}_")
            lines.append("")

        debt_owed = int(analytics.get("debt_owed_to_me", 0) or 0)
        debt_mine = int(analytics.get("debt_i_owe", 0) or 0)
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        if debt_owed > 0:
            lines.append(f"🔴 *Balance owed to you: {format_amount(debt_owed)}*")
        elif debt_mine > 0:
            lines.append(f"📝 *You owe: {format_amount(debt_mine)}*")
        else:
            lines.append("✅ *Settled — no balance*")

        return [
            text_response("\n".join(lines)),
            button_response("Actions:", [
                {"id": f"crm_view_{contact_id}", "title": "← Back to contact"},
                {"id": "crm_all", "title": "👥 Contacts"},
            ])
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
        lines.append("_Tap a name below to open them and send a reminder._")

        # Telegram: make each debtor tappable (opens their card → Remind).
        if self._is_telegram(phone_number):
            rows = []
            for d in debtors[:10]:
                name = d.get("name", "Unknown")
                cid = d.get("contact_id") or name.lower().replace(" ", "_")
                rows.append({"id": f"crm_view_{cid}"[:60],
                             "title": f"🔴 {name} · {format_amount(d.get('amount', 0))}"[:60]})
            rows.append({"id": "crm_all", "title": "← Contacts"})
            return [list_response(
                header="🔴 Who Owes Me",
                body="\n".join(lines),
                button_text="Open",
                sections=[{"title": "", "rows": rows}],
            )]

        lines.append("_Or type: remind [name]_")
        return [text_response("\n".join(lines))]

    def _show_creditors(self, phone_number: str) -> list:
        """Show everyone the USER owes money to (payables / 'Who I Owe')."""
        creditors = self.db.get_all_creditors(phone_number) or []

        if not creditors:
            return [text_response(
                "📝 *Who I Owe*\n\n"
                "✅ You're all settled — you don't owe anyone right now.\n\n"
                "_When you buy on credit or part-pay a supplier, they'll appear here._"
            )]

        total = sum(c["amount"] for c in creditors)

        lines = [
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📝  *Who I Owe*",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"",
            f"*Total you owe: {format_amount(total)}*",
            f"",
        ]
        for i, c in enumerate(creditors[:10], 1):
            name   = c.get("name", "Unknown")
            amount = c.get("amount", 0)
            date   = c.get("last_date", "")
            lines.append(f"{i}. *{name}* — {format_amount(amount)}")
            if date:
                lines.append(f"   Since: {date}")
        if len(creditors) > 10:
            lines.append(f"\n_...and {len(creditors) - 10} more_")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")

        if self._is_telegram(phone_number):
            lines.append("_Tap a supplier to open them (record a payment / statement)._")
            rows = []
            for c in creditors[:10]:
                name = c.get("name", "Unknown")
                cid = c.get("contact_id") or name.lower().replace(" ", "_")
                rows.append({"id": f"crm_view_{cid}"[:60],
                             "title": f"📝 {name} · {format_amount(c.get('amount', 0))}"[:60]})
            rows.append({"id": "crm_all", "title": "← Contacts"})
            return [list_response(
                header="📝 Who I Owe",
                body="\n".join(lines),
                button_text="Open",
                sections=[{"title": "", "rows": rows}],
            )]

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
