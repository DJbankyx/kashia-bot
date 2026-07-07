# src/industries/services_industry.py
"""Services industry branch."""

from industries.base import BaseIndustry


class ServicesIndustry(BaseIndustry):
    """Provide services — cleaning, consulting, repair, logistics."""

    INDUSTRY_KEY = "services"
    EMOJI = "💼"
    LABEL = "Services"

    TERMS = {
        "sale": "Service/Job",
        "purchase": "Supply Purchase",
        "expense": "Operating Expense",
        "catalog": "Service Catalog",
        "catalog_item": "Service",
    }

    EXAMPLES = {
        "sale": "cleaned Alhaji's office 25K",
        "purchase": "bought cleaning supplies 8K",
    }

    GUIDED_PROMPTS = {
        "ask_item_sale": "💼 What service did you provide?\n\n_(e.g. cleaning, repair, consultation, delivery)_",
        "ask_item_purchase": "📦 What supplies did you buy?\n\n_(e.g. cleaning chemicals, tools, fuel)_",
        "ask_amount": "💰 How much?\n\n_(e.g. 50000, 150K, 1.2M)_",
        "ask_vendor_sale": "👤 Who was the client?\n\n_(client name or type *skip*)_",
        "ask_vendor_purchase": "👤 Where did you buy from?\n\n_(supplier name or type *skip*)_",
        "ask_details": "🏷️ Any extra details?\n\nLocation, duration, description...\n\nType details or *skip*",
    }

    def _get_record_rows(self) -> list:
        return [
            {"id": "record_sale", "title": "💰 Record Job/Service", "description": "Completed a service or job"},
            {"id": "record_expense", "title": "💸 Record Expense", "description": "Tools, transport, materials"},
        ]

    def _build_menu_sections(self) -> list:
        """Services don't have a 'purchase' button by default — fewer rows."""
        return [
            {
                "title": "📝 Record",
                "rows": self._get_record_rows() + [
                    {"id": "record_purchase", "title": "📦 Buy Supplies", "description": "Tools, materials, stock"},
                ],
            },
            {
                "title": "💼 Business",
                "rows": [
                    {"id": "menu_profile", "title": "👤 My Dashboard", "description": "Revenue overview & profile"},
                    {"id": "menu_report", "title": "📊 Reports", "description": "Today, this week, this month"},
                    {"id": "menu_debts", "title": "💳 Debts & Credits", "description": "Outstanding payments"},
                    {"id": "menu_contacts", "title": "📇 Clients", "description": "Client contacts"},
                    {"id": "menu_catalog", "title": f"📋 {self.TERMS['catalog']}", "description": "Services & pricing"},
                    {"id": "menu_export", "title": "📁 Export & Docs", "description": "Invoices, receipts, Excel"},
                ]
            }
        ]
