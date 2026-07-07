# src/industries/hybrid.py
"""Hybrid industry branch — mix of goods + services."""

from industries.base import BaseIndustry


class HybridIndustry(BaseIndustry):
    """Combination of goods + services."""

    INDUSTRY_KEY = "hybrid"
    EMOJI = "🔄"
    LABEL = "Hybrid"

    TERMS = {
        "sale": "Sale/Service",
        "purchase": "Purchase",
        "expense": "Expense",
        "catalog": "Product & Service Catalog",
        "catalog_item": "Product/Service",
    }

    EXAMPLES = {
        "sale": "sold 5 bags cement 75K",
        "purchase": "bought cement from Dangote 200K",
    }

    GUIDED_PROMPTS = {
        "ask_item_sale": "📦 What did you sell or do?\n\n_(goods sold or service provided)_",
        "ask_item_purchase": "📦 What did you buy?\n\n_(stock, materials, supplies)_",
        "ask_amount": "💰 How much?\n\n_(e.g. 50000, 150K, 1.2M)_",
        "ask_vendor_sale": "👤 Who was it for?\n\n_(customer/client name or type *skip*)_",
        "ask_vendor_purchase": "👤 Who did you buy from?\n\n_(supplier name or type *skip*)_",
        "ask_details": "🏷️ Any extra details?\n\nBrand, size, type, description...\n\nType details or *skip*",
    }

    def _get_record_rows(self) -> list:
        return [
            {"id": "record_sale", "title": "💰 Record Sale/Job", "description": "Sold goods or completed service"},
            {"id": "record_purchase", "title": "📦 Record Purchase", "description": "Bought goods or materials"},
            {"id": "record_expense", "title": "💸 Record Expense", "description": "Operating costs"},
        ]
