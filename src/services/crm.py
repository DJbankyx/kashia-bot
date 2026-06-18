# src/services/crm.py
"""CRM Service - automatically tracks customers and suppliers from transactions"""

import logging
from datetime import datetime

from utils.parser import normalize_name, extract_vendor_name
from services.database import Database

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ContactService:
    """Manages contacts (customers/suppliers) automatically from transactions"""

    def __init__(self, database=None):
        self.db = database or Database()

    def process_transaction(self, phone_number, transaction):
        """
        After a transaction is saved, auto-detect and link a contact.

        Args:
            transaction: dict with keys: description, vendor, type, amount, category
        """
        vendor = transaction.get('vendor', '')

        if not vendor:
            # Try to extract from description
            vendor = extract_vendor_name(transaction.get('description', ''))

        if not vendor:
            return  # No vendor to track

        # Determine contact type based on transaction type
        tx_type = transaction.get('type', 'expense')
        if tx_type == 'expense':
            contact_type = 'supplier'  # You pay them = they supply you
        else:
            contact_type = 'customer'  # They pay you = they buy from you

        # Check if contact already exists
        existing = self.db.get_contact_by_name(phone_number, vendor)

        if existing:
            # Update existing contact
            # If they were supplier but now paying you, upgrade to "both"
            if existing.get('type') != contact_type and existing.get('type') != 'both':
                self.db.update_contact_totals(phone_number, vendor,
                                             transaction.get('amount', 0), tx_type)
                # TODO: Update type to "both" if needed
            else:
                self.db.update_contact_totals(phone_number, vendor,
                                             transaction.get('amount', 0), tx_type)
        else:
            # Create new contact
            self.db.save_contact(phone_number, vendor, contact_type)
            # Update with first transaction amount
            self.db.update_contact_totals(phone_number, vendor,
                                         transaction.get('amount', 0), tx_type)

        logger.info(f"CRM: {vendor} linked as {contact_type} for {phone_number}")

    def get_top_customers(self, phone_number, limit=5):
        """
        Get top customers (people who pay the user).

        Returns:
            Formatted WhatsApp text
        """
        contacts = self.db.get_contacts(phone_number)
        customers = [c for c in contacts if c.get('type') in ['customer', 'both']]

        if not customers:
            return "📋 No customers found yet.\nThey'll appear as you record sales!"

        # Sort by total received (highest first)
        customers.sort(key=lambda x: int(x.get('total_received', 0)), reverse=True)

        result = "📋 *Your Top Customers:*\n\n"
        for i, contact in enumerate(customers[:limit], 1):
            name = contact.get('name', 'Unknown')
            total = int(contact.get('total_received', 0))
            count = int(contact.get('transaction_count', 0))
            result += f"{i}. *{name}*\n"
            result += f"   💰 ₦{total:,} received ({count} transactions)\n\n"

        return result

    def get_top_suppliers(self, phone_number, limit=5):
        """
        Get top suppliers (people the user pays).

        Returns:
            Formatted WhatsApp text
        """
        contacts = self.db.get_contacts(phone_number)
        suppliers = [c for c in contacts if c.get('type') in ['supplier', 'both']]

        if not suppliers:
            return "📋 No suppliers found yet.\nThey'll appear as you record purchases!"

        # Sort by total paid (highest first)
        suppliers.sort(key=lambda x: int(x.get('total_paid', 0)), reverse=True)

        result = "📋 *Your Top Suppliers:*\n\n"
        for i, contact in enumerate(suppliers[:limit], 1):
            name = contact.get('name', 'Unknown')
            total = int(contact.get('total_paid', 0))
            count = int(contact.get('transaction_count', 0))
            result += f"{i}. *{name}*\n"
            result += f"   💸 ₦{total:,} paid ({count} transactions)\n\n"

        return result

    def get_contact_detail(self, phone_number, contact_name):
        """
        Get full history with a specific contact.

        Returns:
            Formatted WhatsApp text
        """
        contact = self.db.get_contact_by_name(phone_number, contact_name)

        if not contact:
            return f"❓ I don't have a contact named '{contact_name}'."

        name = contact.get('name', 'Unknown')
        contact_type = contact.get('type', 'unknown')
        total_paid = int(contact.get('total_paid', 0))
        total_received = int(contact.get('total_received', 0))
        tx_count = int(contact.get('transaction_count', 0))
        last_date = contact.get('last_transaction_date', 'N/A')

        type_emoji = "🛒" if contact_type == "supplier" else "💰" if contact_type == "customer" else "🔄"

        result = f"{type_emoji} *{name}*\n"
        result += f"Type: {contact_type.title()}\n\n"

        if total_paid > 0:
            result += f"💸 Total paid to them: ₦{total_paid:,}\n"
        if total_received > 0:
            result += f"💰 Total received from them: ₦{total_received:,}\n"

        result += f"📝 Transactions: {tx_count}\n"
        result += f"📅 Last transaction: {last_date}\n"

        return result

    def search_contacts(self, phone_number, search_term):
        """
        Search contacts by name.

        Returns:
            list of matching contacts
        """
        contacts = self.db.get_contacts(phone_number)
        search_lower = search_term.lower()

        matches = [
            c for c in contacts
            if search_lower in c.get('name', '').lower()
        ]

        return matches

    def generate_insights(self, phone_number):
        """
        Generate smart CRM insights for the user.

        Returns:
            list of insight strings (or empty if nothing notable)
        """
        insights = []
        contacts = self.db.get_contacts(phone_number)

        if not contacts:
            return insights

        # Insight 1: Top supplier concentration
        suppliers = [c for c in contacts if c.get('type') in ['supplier', 'both']]
        if suppliers:
            suppliers.sort(key=lambda x: int(x.get('total_paid', 0)), reverse=True)
            top = suppliers[0]
            total_all = sum(int(s.get('total_paid', 0)) for s in suppliers)
            top_amount = int(top.get('total_paid', 0))

            if total_all > 0 and (top_amount / total_all) > 0.5:
                pct = int((top_amount / total_all) * 100)
                insights.append(
                    f"⚠️ *Supplier alert:* {pct}% of your spending goes to "
                    f"*{top.get('name')}*. Consider diversifying."
                )

        # Insight 2: Inactive customer
        customers = [c for c in contacts if c.get('type') in ['customer', 'both']]
        today = datetime.now().strftime('%Y-%m-%d')

        for customer in customers:
            last_date = customer.get('last_transaction_date', '')
            if last_date:
                try:
                    last = datetime.strptime(last_date, '%Y-%m-%d')
                    days_ago = (datetime.now() - last).days
                    if days_ago > 14:
                        insights.append(
                            f"📢 *{customer.get('name')}* hasn't bought from you "
                            f"in {days_ago} days. Time to follow up?"
                        )
                except ValueError:
                    pass

        return insights[:3]  # Max 3 insights at a time
