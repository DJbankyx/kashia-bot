# src/industries/trading.py
"""Trading & Retail industry branch."""

from industries.base import BaseIndustry


class TradingIndustry(BaseIndustry):
    """Buy and sell goods — shops, markets, online stores."""

    INDUSTRY_KEY = "trading"
    EMOJI = "🛍️"
    LABEL = "Trading & Retail"

    TERMS = {
        "sale": "Sale",
        "purchase": "Purchase",
        "expense": "Expense",
        "catalog": "Product Catalog",
        "catalog_item": "Product",
    }

    EXAMPLES = {
        "sale": "sold 10 Nike shoes to Sandra for 150K",
        "purchase": "bought 50 pairs socks from Alhaji 200K",
    }

    GUIDED_PROMPTS = {
        "ask_item_sale": "📦 What did you sell?\n\n_(e.g. shoes, bags, clothes, cement)_",
        "ask_item_purchase": "📦 What did you buy?\n\n_(e.g. flour, fabric, stock, goods)_",
        "ask_amount": "💰 How much?\n\n_(e.g. 50000, 150K, 1.2M)_",
        "ask_vendor_sale": "👤 Who did you sell to?\n\n_(customer name or type *skip*)_",
        "ask_vendor_purchase": "👤 Who did you buy from?\n\n_(supplier name or type *skip*)_",
        "ask_details": "🏷️ Any extra details?\n\nBrand, color, size, model...\n\nType details or *skip*",
    }

    def _get_record_rows(self) -> list:
        return [
            {"id": "record_sale", "title": "💰 Record Sale", "description": "Sold goods to customer"},
            {"id": "record_purchase", "title": "📦 Record Purchase", "description": "Bought stock/goods"},
            {"id": "record_expense", "title": "💸 Record Expense", "description": "Rent, transport, bills"},
        ]
