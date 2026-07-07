# src/industries/manufacturing.py
"""Manufacturing industry branch."""

from industries.base import BaseIndustry


class ManufacturingIndustry(BaseIndustry):
    """Produce/make goods — factories, workshops, food production."""

    INDUSTRY_KEY = "manufacturing"
    EMOJI = "🏭"
    LABEL = "Manufacturing"

    TERMS = {
        "sale": "Output Sale",
        "purchase": "Raw Material Purchase",
        "expense": "Production Cost",
        "catalog": "Product & Recipe Book",
        "catalog_item": "Product/Material",
    }

    EXAMPLES = {
        "sale": "sold 200 bottles detergent to Shoprite 80K",
        "purchase": "bought 5 drums sulphonic acid 1.3M",
    }

    GUIDED_PROMPTS = {
        "ask_item_sale": "📦 What finished product did you sell?\n\n_(e.g. detergent, bread, furniture)_",
        "ask_item_purchase": "🧱 What raw material did you buy?\n\n_(e.g. sulphonic acid, flour, wood)_",
        "ask_amount": "💰 How much?\n\n_(e.g. 50000, 150K, 1.2M)_",
        "ask_vendor_sale": "👤 Who did you sell to?\n\n_(customer/retailer name or type *skip*)_",
        "ask_vendor_purchase": "👤 Who did you buy from (supplier)?\n\n_(supplier name or type *skip*)_",
        "ask_details": "🏷️ Any extra details?\n\nBatch, quantity, size, grade...\n\nType details or *skip*",
    }

    def _get_record_rows(self) -> list:
        return [
            {"id": "record_sale", "title": "💰 Sell Output", "description": "Sold finished goods"},
            {"id": "record_purchase", "title": "🧱 Buy Raw Materials", "description": "Purchased inputs/supplies"},
            {"id": "record_expense", "title": "💸 Production Cost", "description": "Labour, overhead, utilities"},
        ]
