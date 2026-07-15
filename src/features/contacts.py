# src/features/contacts.py
"""Contacts — save and view customer/supplier contacts."""

import logging
import re
from utils.whatsapp_ui import text_response, list_response

logger = logging.getLogger(__name__)


class ContactsHandler:
    """Manage customer and supplier contacts."""

    def __init__(self, session_mgr, database):
        self.session = session_mgr
        self.db = database

    def show(self, phone_number: str) -> list:
        """Show contacts list with action options."""
        contacts = self.db.get_contacts(phone_number) or []

        if not contacts:
            return [text_response(
                "📇 *Contacts*\n\n"
                "No contacts saved yet.\n\n"
                "Contacts are saved automatically when you record transactions "
                "with a name, or you can type:\n\n"
                "_Save number [Name] [Phone]_\n"
                "e.g. _Save number Sandra 08060475064_"
            )]

        # Group by type
        customers = [c for c in contacts if c.get("type") == "customer"]
        suppliers = [c for c in contacts if c.get("type") == "supplier"]
        other = [c for c in contacts if c.get("type") not in ("customer", "supplier")]

        lines = ["📇 *Your Contacts*\n"]

        if customers:
            lines.append(f"*Customers ({len(customers)}):*")
            for c in customers[:10]:
                name = c.get("name", "Unknown")
                phone = c.get("phone", "")
                phone_str = f" — {phone}" if phone else ""
                lines.append(f"  👤 {name}{phone_str}")

        if suppliers:
            lines.append(f"\n*Suppliers ({len(suppliers)}):*")
            for c in suppliers[:10]:
                name = c.get("name", "Unknown")
                phone = c.get("phone", "")
                phone_str = f" — {phone}" if phone else ""
                lines.append(f"  🏪 {name}{phone_str}")

        if other:
            lines.append(f"\n*Other ({len(other)}):*")
            for c in other[:5]:
                name = c.get("name", "Unknown")
                lines.append(f"  • {name}")

        lines.append(f"\n_{len(contacts)} total contacts_")
        lines.append("\nTo add: _Save number [Name] [Phone]_")

        return [text_response("\n".join(lines))]

    def save_contact_from_text(self, phone_number: str, text: str) -> list:
        """Parse 'save number [name] [phone]' and save contact."""
        # Remove "save number" prefix
        cleaned = re.sub(r'^save\s+number\s+', '', text, flags=re.IGNORECASE).strip()

        # Extract phone number from text (keep original spacing for regex)
        phone_match = re.search(r'(\d[\d\s]{9,15}\d)', cleaned)

        if not phone_match:
            return [text_response(
                "📱 Please include a valid phone number:\n\n"
                "_Save number Sandra 08060475064_"
            )]

        contact_phone = phone_match.group(1).replace(" ", "")
        # Validate length
        if len(contact_phone) < 10 or len(contact_phone) > 14:
            return [text_response("📱 That doesn't look like a valid phone number. Please try again.")]

        # Name is everything before the phone number
        name = cleaned[:phone_match.start()].strip()
        if not name:
            # Try after
            name = cleaned[phone_match.end():].strip()
        if not name:
            return [text_response("👤 Please include a name:\n\n_Save number Sandra 08060475064_")]

        # Save — matches database.save_contact(phone_number, name, contact_type, contact_phone)
        self.db.save_contact(phone_number, name, "customer", contact_phone)

        return [text_response(f"✅ Saved! *{name}* — {contact_phone}")]
