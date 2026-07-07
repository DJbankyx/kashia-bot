# src/services/categorizer.py
"""AI Transaction Parser & Categorizer â€” extracts maximum detail from natural language"""

import json
import logging
import re
from openai import OpenAI
from utils.config import get_openai_key
from utils.parser import extract_vendor_name, normalize_name

logger = logging.getLogger(__name__)


# ==========================================
# ACCOUNTING CATEGORIES
# ==========================================

CATEGORIES = {
    "Goods & Stock": {
        "sub_categories": ["Buying goods to sell", "Raw materials", "Packaging"],
        "keywords": ["buy", "bought", "stock", "goods", "bags", "carton", "supply",
                     "restock", "purchase", "wholesale", "market"]
    },
    "Sales & Income": {
        "sub_categories": ["Cash sales", "Transfer/POS sales", "Credit sales", "Other income"],
        "keywords": ["sold", "sell", "sales", "income", "received", "collected",
                     "customer paid", "revenue"]
    },
    "Service Revenue": {
        "sub_categories": ["Consulting fees", "Professional fees", "Contract work", "Retainer fees", "Commission"],
        "keywords": ["service", "consultation", "contract", "retainer", "commission",
                     "professional fee", "advisory", "engagement", "project fee"]
    },
    "Service Costs": {
        "sub_categories": ["Subcontractors", "Freelancers", "Software/Tools", "Professional development"],
        "keywords": ["subcontract", "freelance", "outsource", "software", "subscription",
                     "tools", "license", "training", "certification"]
    },
    "Production & Manufacturing": {
        "sub_categories": ["Raw materials", "Direct labour", "Factory overhead", "Work in progress"],
        "keywords": ["production", "manufacture", "factory", "assembly", "fabricate",
                     "welding", "molding", "processing", "raw material"]
    },
    "Rent & Space": {
        "sub_categories": ["Shop rent", "Warehouse", "Storage", "Office space"],
        "keywords": ["rent", "shop", "warehouse", "storage", "space", "landlord", "office"]
    },
    "Utilities & Services": {
        "sub_categories": ["Electricity/Generator fuel", "Internet/Data", "Water", "Phone"],
        "keywords": ["light", "nepa", "phcn", "diesel", "fuel", "generator", "data",
                     "airtime", "internet", "water", "dstv", "gotv", "subscription"]
    },
    "Transport & Logistics": {
        "sub_categories": ["Delivery costs", "Personal transport", "Shipping"],
        "keywords": ["transport", "delivery", "shipping", "uber", "bolt", "bus",
                     "fuel", "petrol", "logistics", "dispatch"]
    },
    "People & Labour": {
        "sub_categories": ["Staff salaries", "Casual workers", "Artisans", "PAYE/Pension"],
        "keywords": ["salary", "wage", "staff", "worker", "boy", "girl", "sales girl",
                     "gateman", "cleaner", "driver", "apprentice", "oga"]
    },
    "Equipment & Tools": {
        "sub_categories": ["Phone/Laptop", "Machinery", "Repairs & Maintenance"],
        "keywords": ["phone", "laptop", "computer", "machine", "equipment", "repair",
                     "maintenance", "fix", "tool", "printer"]
    },
    "Money Matters": {
        "sub_categories": ["Bank charges", "Loan repayment", "Interest paid", "Insurance"],
        "keywords": ["bank", "charge", "loan", "interest", "transfer fee", "sms alert",
                     "maintenance fee", "atm", "insurance"]
    },
    "Marketing & Customers": {
        "sub_categories": ["Advertising", "Flyers/Signage", "Customer gifts"],
        "keywords": ["advert", "flyer", "banner", "signage", "promo", "gift",
                     "entertainment", "marketing", "social media", "facebook", "instagram"]
    },
    "Government & Compliance": {
        "sub_categories": ["VAT payment", "WHT payment", "License/Permits", "Association dues", "Tax filing"],
        "keywords": ["tax", "vat", "wht", "license", "permit", "registration", "levy", "association",
                     "dues", "government", "lga", "cac", "firs", "withholding"]
    },
    "Personal": {
        "sub_categories": ["Owner withdrawal", "Personal expenses"],
        "keywords": ["personal", "myself", "family", "house rent", "school fees",
                     "feeding", "my own", "withdraw", "drawing"]
    }
}

# Category list for prompts
CATEGORY_LIST = "\n".join([
    f"{i+1}. {cat} -- {', '.join(info['sub_categories'])}"
    for i, (cat, info) in enumerate(CATEGORIES.items())
])


# ==========================================
# INDUSTRY-SPECIFIC CATEGORY CONFIGURATION
# ==========================================

INDUSTRY_CATEGORIES = {
    "trading": {
        "income": ["Sales & Income"],
        "cogs": ["Goods & Stock"],
        "opex": ["Rent & Space", "Utilities & Services", "Transport & Logistics", 
                 "People & Labour", "Equipment & Tools", "Money Matters",
                 "Marketing & Customers", "Government & Compliance", "Personal"],
        "excluded": ["Service Revenue", "Service Costs", "Production & Manufacturing"],
        "key_metrics": ["markup_percent", "stock_turnover", "top_products", "top_customers"],
    },
    "manufacturing": {
        "income": ["Sales & Income"],
        "cogs": ["Production & Manufacturing"],
        "opex": ["Rent & Space", "Utilities & Services", "Transport & Logistics",
                 "People & Labour", "Equipment & Tools", "Money Matters",
                 "Marketing & Customers", "Government & Compliance", "Personal"],
        "excluded": ["Service Revenue", "Service Costs", "Goods & Stock"],
        "key_metrics": ["cost_per_unit", "yield_rate", "material_costs", "labour_costs"],
    },
    "services": {
        "income": ["Service Revenue"],
        "cogs": ["Service Costs"],
        "opex": ["Rent & Space", "Utilities & Services", "Transport & Logistics",
                 "People & Labour", "Equipment & Tools", "Money Matters",
                 "Marketing & Customers", "Government & Compliance", "Personal"],
        "excluded": ["Goods & Stock", "Production & Manufacturing", "Sales & Income"],
        "key_metrics": ["revenue_per_client", "service_margin", "billable_hours"],
    },
    "hybrid": {
        "income": ["Sales & Income", "Service Revenue"],
        "cogs": ["Goods & Stock", "Service Costs"],
        "opex": ["Rent & Space", "Utilities & Services", "Transport & Logistics",
                 "People & Labour", "Equipment & Tools", "Money Matters",
                 "Marketing & Customers", "Government & Compliance", "Personal"],
        "excluded": [],  # Hybrid sees everything
        "key_metrics": ["product_margin", "service_margin", "combined_profit"],
    },
}

# P&L SECTION LABELS per industry
PNL_LABELS = {
    "trading": {
        "revenue_title": "REVENUE",
        "revenue_subtitle": "Sales & Trading Income",
        "cogs_title": "COST OF GOODS SOLD",
        "cogs_subtitle": "Purchases made for resale",
        "gross_title": "GROSS PROFIT",
        "opex_title": "OPERATING EXPENSES",
        "net_title": "NET PROFIT / (LOSS)",
    },
    "manufacturing": {
        "revenue_title": "REVENUE",
        "revenue_subtitle": "Finished Goods Sales",
        "cogs_title": "COST OF PRODUCTION",
        "cogs_subtitle": "Raw Materials + Direct Labour + Factory Overhead",
        "gross_title": "GROSS PROFIT (Manufacturing Margin)",
        "opex_title": "OPERATING & ADMIN EXPENSES",
        "net_title": "NET PROFIT / (LOSS)",
    },
    "services": {
        "revenue_title": "SERVICE REVENUE",
        "revenue_subtitle": "Fees, Contracts & Commissions",
        "cogs_title": "DIRECT SERVICE COSTS",
        "cogs_subtitle": "Subcontractors, Tools & Materials used",
        "gross_title": "GROSS MARGIN",
        "opex_title": "OPERATING EXPENSES",
        "net_title": "NET PROFIT / (LOSS)",
    },
    "hybrid": {
        "revenue_title": "REVENUE",
        "revenue_subtitle": "Products + Services Combined",
        "cogs_title": "DIRECT COSTS",
        "cogs_subtitle": "COGS (Products) + Service Delivery Costs",
        "gross_title": "GROSS PROFIT",
        "opex_title": "OPERATING EXPENSES",
        "net_title": "NET PROFIT / (LOSS)",
    },
}

def get_categories_for_industry(industry_class):
    """Get the filtered category list for a specific industry"""
    config = INDUSTRY_CATEGORIES.get(industry_class, INDUSTRY_CATEGORIES['hybrid'])
    excluded = set(config.get('excluded', []))
    
    filtered = {k: v for k, v in CATEGORIES.items() if k not in excluded}
    
    category_list = "\n".join([
        f"{i+1}. {cat} -- {', '.join(info['sub_categories'])}"
        for i, (cat, info) in enumerate(filtered.items())
    ])
    return category_list, filtered




# ==========================================
# BUSINESS-SPECIFIC PARSING INSTRUCTIONS
# ==========================================

BUSINESS_PARSE_INSTRUCTIONS = {
    "trading": """BUSINESS TYPE: Trading (Buy & Sell)
Extract these fields with maximum detail:
- item_name: What was bought/sold (be specific: "Nike Airforce 1" not just "shoes")
- brand: Brand name if mentioned (Nike, Gucci, Samsung, Dangote, etc.)
- model: Model/variant if mentioned (Airforce 1, iPhone 15, Galaxy S24)
- size: Size if mentioned (43, XL, UK 9, 50kg bag)
- color: Color if mentioned
- quantity: How many (include unit: "3 bags", "2 pairs", "1 carton", "5 pieces")
- unit_cost: Cost per single item (calculate from total if needed)
- condition: New, UK-used/thrift, refurbished (if mentioned)
- vendor_or_customer: Who they bought from or sold to
- discount: If a discount was given/received (amount or percentage)
- tax: If VAT, WHT, or any tax was mentioned (amount or percentage)
""",

    "fashion": """BUSINESS TYPE: Fashion/Clothing
Extract these fields with maximum detail:
- item_name: Specific item (shirt, trouser, gown, sneakers, bag)
- brand: Brand name (Nike, Gucci, Zara, Louis Vuitton, Balenciaga, local brands)
- model: Model/style (Airforce, Jordan 4, City Boy, Cargo pants)
- size: Size (S/M/L/XL/XXL, 38-46, UK 6-12, US 7-13)
- color: Color/pattern (black, ash, multi-color, ankara pattern)
- material: Fabric/material if mentioned (leather, cotton, denim, silk)
- quantity: How many pieces/pairs/yards
- unit_cost: Cost per item
- gender: Men/Women/Unisex if clear
- condition: New, fairly-used, thrift/UK-used, vintage
- vendor_or_customer: Source/buyer name
- discount: If a discount was given/received
- tax: If VAT or tax was mentioned
""",

    "food": """BUSINESS TYPE: Food & Drinks
Extract these fields with maximum detail:
- item_name: Specific food/drink item (rice, palm oil, chicken, soft drinks)
- brand: Brand if relevant (Dangote sugar, Golden Penny flour, Peak milk, Indomie)
- variant: Type/variant (basmati rice, palm kernel oil, broiler chicken)
- unit: Measurement unit (kg, bags of 50kg, litres, crates, cartons, mudu, paint)
- weight_volume: Weight or volume (50kg, 25L, 5kg)
- quantity: How many units (3 bags, 2 crates, 10 cartons)
- unit_cost: Cost per unit
- pack_size: Items per pack/carton if bulk (Indomie = 40 per carton)
- vendor_or_customer: Supplier/buyer name
- discount: If a discount was given/received
- tax: If VAT or tax was mentioned
""",

    "services": """BUSINESS TYPE: Services (Consulting, IT, Legal, Medical, Creative)
Extract these fields with maximum detail:
- service_name: What service (consulting, web development, legal advice, surgery, design)
- service_type: Category (consultation, implementation, maintenance, creative, professional)
- client_name: Customer/client name
- duration: Time spent if mentioned (2 hours, 3 days, 1 week, 1 month retainer)
- materials_used: Any materials/parts used
- material_cost: Cost of materials separately if mentioned
- labor_cost: Service fee/labor portion if separated
- payment_status: Paid in full, part payment, pending, deposit
- vendor_or_customer: Who paid or who was paid
- discount: If a discount was given to client
- tax: If VAT, WHT, or withholding tax was mentioned/deducted

CATEGORY GUIDANCE for services:
- Income from services rendered -> "Service Revenue"
- Costs to deliver services (subcontractors, tools) -> "Service Costs"
- General expenses (rent, utilities) -> appropriate category
- DO NOT use "Goods & Stock" for service businesses unless they buy/sell physical goods
""",

    "manufacturing": """BUSINESS TYPE: Manufacturing/Production
Extract these fields with maximum detail:
- item_name: What product/material (steel rods, fabric, chemicals, components)
- material_type: Raw material, semi-finished, finished goods
- brand: Brand if relevant
- quantity: Amount with unit (500kg, 200 pieces, 50 litres)
- unit_cost: Cost per unit
- stage: Raw material purchase, Production cost, Finished goods sale
- vendor_or_customer: Supplier or buyer
- discount: If discount was given/received
- tax: If VAT, WHT, or tax was mentioned

CATEGORY GUIDANCE for manufacturing:
- Buying raw materials -> "Production & Manufacturing" -> "Raw materials"
- Factory labour -> "Production & Manufacturing" -> "Direct labour"
- Factory overhead (power, maintenance) -> "Production & Manufacturing" -> "Factory overhead"
- Selling finished products -> "Sales & Income"
- DO NOT put production costs under "Goods & Stock"
""",

    "general": """BUSINESS TYPE: General
Extract these fields with maximum detail:
- item_name: What was bought/sold/paid for
- brand: Brand name if mentioned
- model: Model/type if mentioned
- quantity: How many (with unit)
- unit_cost: Cost per item
- vendor_or_customer: Who the transaction was with
- payment_method: Cash, transfer, POS if mentioned
- discount: If a discount was given/received
- tax: If VAT, WHT, or any tax was mentioned
"""
}


# ==========================================
# SYSTEM PROMPT FOR RICH PARSING
# ==========================================

RICH_PARSE_PROMPT = """You are Kashia, a Nigerian AI bookkeeping assistant that extracts MAXIMUM DETAIL from transaction descriptions.

You MUST understand:
- Nigerian Pidgin English ("I buy", "dem pay me", "wetin", "na so", "abeg")
- Nigerian business language ("restock", "supply", "customer collect")
- Nigerian vendor names (Dangote, Mama Ngozi, Alhaji, etc.)
- Nigerian context (POS, transfer, NEPA, generator, pure water, suya)
- Naira amounts: K = thousand, M = million, "95K" = 95000, "1.5M" = 1500000

{business_instructions}

ACCOUNTING CATEGORIES:
{categories}

DISCOUNT & TAX RULES:
- If user mentions "discount", "less", "off", "gave discount", "got discount" -> extract discount
- discount_type: "given" (you gave customer discount) or "allowed" (supplier gave you discount)
- discount can be a percentage ("10% discount") or fixed amount ("less 5000", "discount 10K")
- If user mentions "VAT", "tax", "+ VAT", "including VAT", "plus tax" -> extract tax
- If user mentions "WHT", "withholding", "WHT deducted" -> extract tax as WHT
- tax can be a percentage ("7.5% VAT") or amount ("VAT 15000")
- subtotal = price BEFORE discount and tax adjustments
- For sales: total_amount = subtotal - discount + tax (customer pays more with tax)
- For WHT deductions: total_amount = subtotal - WHT (you receive less)
- If ONLY the final amount is given (no breakdown), set total_amount to that and leave discount/tax null

RULES:
1. Extract EVERY detail you can identify from the text
2. Calculate unit_cost from total and quantity when possible
3. IMPORTANT: If user says 'each', 'per unit', 'per one', 'per carton', 'per bag' etc., the stated amount is the UNIT COST, not the total. Calculate total_amount = unit_cost x quantity.
4. For item_name vs brand: If a product IS the brand (like Coca-Cola, Pepsi, Indomie), set item_name to the product type (e.g. 'Soft Drinks', 'Instant Noodles') and brand to the brand name.
5. If transaction type (income/expense) is ambiguous, use context clues:
   - "I buy/bought/purchased" = expense
   - "sold/customer paid/received" = income
   - "pay/paid [person]" = expense (paying someone)
   - "[person] pay me" = income (receiving payment)
6. Choose the MOST SPECIFIC category and sub_category
7. If something is not mentioned, set it to null (don't guess)
8. For known brands, capitalize properly (Nike not nike, iPhone not iphone)
9. Tags should capture useful searchable attributes
10. For SERVICE businesses: use "Service Revenue" for income, not "Sales & Income" (unless selling physical goods)
11. For MANUFACTURING: use "Production & Manufacturing" for production costs, not "Goods & Stock"

Respond with ONLY valid JSON in this format:
{{
    "transaction_type": "income" or "expense",
    "total_amount": number or null,
    "subtotal": number or null,
    "discount_amount": number or null,
    "discount_percent": number or null,
    "discount_type": "given" or "allowed" or null,
    "tax_amount": number or null,
    "tax_percent": number or null,
    "tax_type": "VAT" or "WHT" or "custom" or null,
    "item_name": "specific item name" or null,
    "brand": "brand name" or null,
    "model": "model/variant" or null,
    "size": "size" or null,
    "color": "color" or null,
    "quantity": "number with unit" or null,
    "unit_cost": number or null,
    "vendor_or_customer": "name" or null,
    "category": "main category from list",
    "sub_category": "sub category from list",
    "payment_method": "cash/transfer/POS" or null,
    "payment_status": "paid/pending/part-payment" or null,
    "extra_details": {{any other relevant fields extracted}},
    "tags": ["searchable", "tag", "words"],
    "confidence": 0-100,
    "reason": "brief explanation"
}}
"""


# ==========================================
# FEW-SHOT EXAMPLES (RICH)
# ==========================================

RICH_FEW_SHOT = [
    {
        "role": "user",
        "content": "I bought 2 pairs of Nike Airforce size 43 for 85K from Sneaker Hub Ikeja"
    },
    {
        "role": "assistant",
        "content": "{\"transaction_type\": \"expense\", \"total_amount\": 85000, \"subtotal\": 85000, \"discount_amount\": null, \"discount_percent\": null, \"discount_type\": null, \"tax_amount\": null, \"tax_percent\": null, \"tax_type\": null, \"item_name\": \"Sneakers\", \"brand\": \"Nike\", \"model\": \"Air Force 1\", \"size\": \"43\", \"color\": null, \"quantity\": \"2 pairs\", \"unit_cost\": 42500, \"vendor_or_customer\": \"Sneaker Hub Ikeja\", \"category\": \"Goods & Stock\", \"sub_category\": \"Buying goods to sell\", \"payment_method\": null, \"payment_status\": \"paid\", \"extra_details\": {\"condition\": \"new\", \"gender\": \"unisex\"}, \"tags\": [\"nike\", \"airforce\", \"sneakers\", \"footwear\", \"size-43\"], \"confidence\": 96, \"reason\": \"Clear purchase of branded footwear for resale with all details specified\"}"
    },
    {
        "role": "user",
        "content": "sold 5 yards ankara to Mama Titi 15K"
    },
    {
        "role": "assistant",
        "content": "{\"transaction_type\": \"income\", \"total_amount\": 15000, \"subtotal\": 15000, \"discount_amount\": null, \"discount_percent\": null, \"discount_type\": null, \"tax_amount\": null, \"tax_percent\": null, \"tax_type\": null, \"item_name\": \"Ankara fabric\", \"brand\": null, \"model\": null, \"size\": null, \"color\": null, \"quantity\": \"5 yards\", \"unit_cost\": 3000, \"vendor_or_customer\": \"Mama Titi\", \"category\": \"Sales & Income\", \"sub_category\": \"Cash sales\", \"payment_method\": null, \"payment_status\": \"paid\", \"extra_details\": {\"material\": \"ankara\"}, \"tags\": [\"ankara\", \"fabric\", \"textile\"], \"confidence\": 94, \"reason\": \"Sale of fabric to named customer\"}"
    },
    {
        "role": "user",
        "content": "Sold 50 pieces of hand wash to Mama Tolu for 75K, gave her 5K discount"
    },
    {
        "role": "assistant",
        "content": "{\"transaction_type\": \"income\", \"total_amount\": 70000, \"subtotal\": 75000, \"discount_amount\": 5000, \"discount_percent\": null, \"discount_type\": \"given\", \"tax_amount\": null, \"tax_percent\": null, \"tax_type\": null, \"item_name\": \"Hand Wash\", \"brand\": null, \"model\": null, \"size\": null, \"color\": null, \"quantity\": \"50 pieces\", \"unit_cost\": 1400, \"vendor_or_customer\": \"Mama Tolu\", \"category\": \"Sales & Income\", \"sub_category\": \"Cash sales\", \"payment_method\": null, \"payment_status\": \"paid\", \"extra_details\": {}, \"tags\": [\"hand-wash\", \"discount\", \"bulk\"], \"confidence\": 95, \"reason\": \"Sale with discount given to customer. Total = 75K - 5K discount = 70K\"}"
    },
    {
        "role": "user",
        "content": "invoice FG Ministry 2.5M for IT consulting + 7.5% VAT"
    },
    {
        "role": "assistant",
        "content": "{\"transaction_type\": \"income\", \"total_amount\": 2687500, \"subtotal\": 2500000, \"discount_amount\": null, \"discount_percent\": null, \"discount_type\": null, \"tax_amount\": 187500, \"tax_percent\": 7.5, \"tax_type\": \"VAT\", \"item_name\": \"IT Consulting\", \"brand\": null, \"model\": null, \"size\": null, \"color\": null, \"quantity\": null, \"unit_cost\": null, \"vendor_or_customer\": \"FG Ministry\", \"category\": \"Service Revenue\", \"sub_category\": \"Consulting fees\", \"payment_method\": null, \"payment_status\": null, \"extra_details\": {}, \"tags\": [\"consulting\", \"IT\", \"government\", \"VAT\"], \"confidence\": 94, \"reason\": \"Service income with VAT. Total = 2.5M + 7.5% VAT (187,500) = 2,687,500\"}"
    },
    {
        "role": "user",
        "content": "received 450K from Zenith Bank for consultancy, they deducted 10% WHT"
    },
    {
        "role": "assistant",
        "content": "{\"transaction_type\": \"income\", \"total_amount\": 450000, \"subtotal\": 500000, \"discount_amount\": null, \"discount_percent\": null, \"discount_type\": null, \"tax_amount\": 50000, \"tax_percent\": 10, \"tax_type\": \"WHT\", \"item_name\": \"Consultancy\", \"brand\": null, \"model\": null, \"size\": null, \"color\": null, \"quantity\": null, \"unit_cost\": null, \"vendor_or_customer\": \"Zenith Bank\", \"category\": \"Service Revenue\", \"sub_category\": \"Consulting fees\", \"payment_method\": \"transfer\", \"payment_status\": \"paid\", \"extra_details\": {\"wht_deducted_at_source\": true}, \"tags\": [\"consultancy\", \"WHT\", \"withholding-tax\"], \"confidence\": 93, \"reason\": \"Consulting income with WHT deducted. Gross = 500K, WHT 10% = 50K, Net received = 450K\"}"
    },
    {
        "role": "user",
        "content": "bought 100 cartons of noodles from Dufil for 850K, got 5% discount"
    },
    {
        "role": "assistant",
        "content": "{\"transaction_type\": \"expense\", \"total_amount\": 807500, \"subtotal\": 850000, \"discount_amount\": 42500, \"discount_percent\": 5, \"discount_type\": \"allowed\", \"tax_amount\": null, \"tax_percent\": null, \"tax_type\": null, \"item_name\": \"Noodles\", \"brand\": \"Dufil\", \"model\": null, \"size\": null, \"color\": null, \"quantity\": \"100 cartons\", \"unit_cost\": 8075, \"vendor_or_customer\": \"Dufil\", \"category\": \"Goods & Stock\", \"sub_category\": \"Buying goods to sell\", \"payment_method\": null, \"payment_status\": \"paid\", \"extra_details\": {}, \"tags\": [\"noodles\", \"bulk\", \"discount-allowed\"], \"confidence\": 95, \"reason\": \"Bulk purchase with trade discount. 850K - 5% (42,500) = 807,500\"}"
    },
    {
        "role": "user",
        "content": "pay my barber boy 25K salary for June"
    },
    {
        "role": "assistant",
        "content": "{\"transaction_type\": \"expense\", \"total_amount\": 25000, \"subtotal\": 25000, \"discount_amount\": null, \"discount_percent\": null, \"discount_type\": null, \"tax_amount\": null, \"tax_percent\": null, \"tax_type\": null, \"item_name\": \"Monthly salary\", \"brand\": null, \"model\": null, \"size\": null, \"color\": null, \"quantity\": \"1 month (June)\", \"unit_cost\": 25000, \"vendor_or_customer\": \"barber boy\", \"category\": \"People & Labour\", \"sub_category\": \"Staff salaries\", \"payment_method\": null, \"payment_status\": \"paid\", \"extra_details\": {\"period\": \"June\", \"role\": \"barber\"}, \"tags\": [\"salary\", \"staff\", \"barber\", \"june\"], \"confidence\": 95, \"reason\": \"Monthly staff salary payment\"}"
    }
]

# ==========================================
# SIMPLE CATEGORIZE PROMPT (backward compat)
# ==========================================

SIMPLE_SYSTEM_PROMPT = """You are Kashia, a Nigerian bookkeeping AI assistant. You help small business owners categorize their financial transactions.

You MUST understand:
- Nigerian Pidgin English ("I buy", "dem pay me", "wetin")
- Nigerian vendor names (Dangote, Mama Ngozi, Alhaji)
- Nigerian context (POS, transfer, NEPA, generator, pure water)
- Naira amounts (K = thousand, M = million)

CATEGORIES:
{categories}

RULES:
1. Choose the MOST SPECIFIC category that fits
2. If the transaction is clearly personal (not business), choose "Personal"
3. If someone is being paid for work, choose "People & Labour"
4. If goods are being purchased to RESELL, choose "Goods & Stock"
5. Generator fuel/diesel = "Utilities & Services" (not Transport)
6. Bank charges = "Money Matters" (not Utilities)
7. For SERVICE businesses: use "Service Revenue" for income from services
8. For MANUFACTURING: use "Production & Manufacturing" for production costs

Respond with ONLY valid JSON:
{{"category": "...", "sub_category": "...", "confidence": 0-100, "reason": "brief explanation"}}
""".format(categories=CATEGORY_LIST)



class TransactionCategorizer:
    """Parses and categorizes transactions using merchant memory + keyword fallback + OpenAI"""

    def __init__(self, database=None):
        self.db = database
        self.client = None  # Lazy-load OpenAI client


    def _build_catalog_context(self, phone_number):
        """Build AI prompt context from user's hierarchical product catalog"""
        if not phone_number:
            return ""
        try:
            from services.database import Database
            db = Database()
            catalog_text = db.get_catalog_for_ai(phone_number)
            if not catalog_text:
                return ""
            
            context = "\n\n--- USER'S PRODUCT CATALOG ---\n"
            context += catalog_text
            context += "\nIMPORTANT: Match transactions to these products/brands/models."
            context += "\nExtract relevant attributes listed for each product."
            context += "\nUse conversions to calculate amounts when units are mentioned."
            context += "\n--- END CATALOG ---"
            return context
        except Exception as e:
            logging.warning(f"Could not load product catalog: {e}")
            return ""

    def parse_transaction(self, description, phone_number=None, business_type="trading", industry_class=None):
        """
        RICH PARSING â€” extracts maximum detail from a transaction description.
        This is the main method for Phase 2+.

        Args:
            description: Natural language transaction text
            phone_number: User's phone number (for merchant memory)
            business_type: User's business category (tailors the AI prompt)

        Returns:
            dict with all extracted fields (see RICH_PARSE_PROMPT for schema)
        """
        # Step 1: Check merchant memory for known vendors
        vendor_memory = None
        if phone_number and self.db:
            vendor = extract_vendor_name(description)
            if vendor:
                vendor_memory = self.db.get_merchant(phone_number, vendor)

        # Step 2: Call OpenAI with rich parsing prompt
        result = self._call_openai_rich(description, business_type, phone_number, industry_class)

        if result:
            # If we have merchant memory, override category
            if vendor_memory:
                result['category'] = vendor_memory.get('category', result.get('category'))
                result['sub_category'] = vendor_memory.get('sub_category', result.get('sub_category', ''))
                logger.info(f"Merchant memory applied: {vendor} -> {vendor_memory['category']}")
            return result

        # Step 3: Fallback â€” simple categorize + basic fields
        simple = self.categorize(description, phone_number)
        return {
            "transaction_type": "expense",
            "total_amount": None,
            "item_name": None,
            "brand": None,
            "model": None,
            "size": None,
            "color": None,
            "quantity": None,
            "unit_cost": None,
            "vendor_or_customer": extract_vendor_name(description),
            "category": simple.get('category', 'Uncategorized'),
            "sub_category": simple.get('sub_category', ''),
            "payment_method": None,
            "payment_status": None,
            "extra_details": {},
            "tags": [],
            "confidence": simple.get('confidence', 0),
            "reason": simple.get('reason', 'Fallback parsing')
        }

    def categorize(self, description, phone_number=None):
        """
        SIMPLE CATEGORIZATION â€” backward compatible.
        Returns: {"category", "sub_category", "confidence", "reason"}
        """
        # Step 1: Check merchant memory
        if phone_number and self.db:
            vendor = extract_vendor_name(description)
            if vendor:
                memory = self.db.get_merchant(phone_number, vendor)
                if memory:
                    logger.info(f"Merchant memory hit: {vendor} -> {memory['category']}")
                    return {
                        "category": memory['category'],
                        "sub_category": memory.get('sub_category', ''),
                        "confidence": 95,
                        "reason": f"Known vendor: {vendor}"
                    }

        # Step 2: Try keyword fallback
        keyword_result = self._keyword_fallback(description)
        if keyword_result and keyword_result['confidence'] >= 85:
            return keyword_result

        # Step 3: Call OpenAI (simple mode)
        ai_result = self._call_openai_simple(description, phone_number)
        if ai_result:
            return ai_result

        # Step 4: Use keyword result even if low confidence
        if keyword_result:
            return keyword_result

        return {
            "category": "Uncategorized",
            "sub_category": "",
            "confidence": 0,
            "reason": "Could not determine category"
        }

    def _keyword_fallback(self, description):
        """Simple keyword matching â€” no API call needed."""
        text_lower = description.lower()
        best_match = None
        best_score = 0

        for category, info in CATEGORIES.items():
            score = 0
            for keyword in info['keywords']:
                if keyword in text_lower:
                    score += 1

            if score > best_score:
                best_score = score
                best_match = category

        if best_match and best_score >= 2:
            confidence = min(90, 70 + (best_score * 10))
            return {
                "category": best_match,
                "sub_category": CATEGORIES[best_match]['sub_categories'][0],
                "confidence": confidence,
                "reason": f"Keyword match ({best_score} keywords)"
            }
        elif best_match and best_score == 1:
            return {
                "category": best_match,
                "sub_category": CATEGORIES[best_match]['sub_categories'][0],
                "confidence": 65,
                "reason": "Single keyword match"
            }

        return None

    def _call_openai_rich(self, description, business_type="trading", phone_number=None, industry_class=None):
        """Call OpenAI with the rich parsing prompt tailored to business type and industry."""
        try:
            if not self.client:
                self.client = OpenAI(api_key=get_openai_key())

            # Get business-specific instructions
            biz_instructions = BUSINESS_PARSE_INSTRUCTIONS.get(
                business_type,
                BUSINESS_PARSE_INSTRUCTIONS.get("general")
            )

            # Use industry_class for category filtering (preferred over business_type)
            if industry_class and industry_class in INDUSTRY_CATEGORIES:
                config = INDUSTRY_CATEGORIES[industry_class]
                excluded = set(config.get('excluded', []))
                filtered_categories = "\n".join([
                    f"{i+1}. {cat} -- {', '.join(info['sub_categories'])}"
                    for i, (cat, info) in enumerate(CATEGORIES.items())
                    if cat not in excluded
                ])
            else:
                # Legacy fallback using business_type
                exclude_cats = set()
                if business_type == "services":
                    exclude_cats = {"Goods & Stock", "Production & Manufacturing"}
                elif business_type == "manufacturing":
                    exclude_cats = {"Service Revenue", "Service Costs"}
                elif business_type in ("trading", "fashion", "food"):
                    exclude_cats = {"Service Revenue", "Service Costs", "Production & Manufacturing"}

                filtered_categories = "\n".join([
                    f"{i+1}. {cat} -- {', '.join(info['sub_categories'])}"
                    for i, (cat, info) in enumerate(CATEGORIES.items())
                    if cat not in exclude_cats
                ])

            # Build system prompt
            system_prompt = RICH_PARSE_PROMPT.format(
                business_instructions=biz_instructions,
                categories=filtered_categories
            )
            
            # Add product catalog context if user has one
            system_prompt += self._build_catalog_context(phone_number)

            messages = [{"role": "system", "content": system_prompt}]

            # Add rich few-shot examples
            messages.extend(RICH_FEW_SHOT)

            # Add user's past corrections
            if phone_number and self.db:
                corrections = self.db.get_recent_feedback(phone_number, limit=3)
                for correction in corrections:
                    messages.append({
                        "role": "user",
                        "content": correction.get('description', '')
                    })
                    messages.append({
                        "role": "assistant",
                        "content": json.dumps({
                            "transaction_type": "expense",
                            "total_amount": None,
                            "item_name": None,
                            "brand": None,
                            "model": None,
                            "size": None,
                            "color": None,
                            "quantity": None,
                            "unit_cost": None,
                            "vendor_or_customer": None,
                            "category": correction.get('correct_category', ''),
                            "sub_category": "",
                            "payment_method": None,
                            "payment_status": None,
                            "extra_details": {},
                            "tags": [],
                            "confidence": 95,
                            "reason": f"Corrected by user (was: {correction.get('wrong_category', '')})"
                        })
                    })

            # Add the actual transaction
            messages.append({"role": "user", "content": description})

            # Call API
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.1,
                max_tokens=400,
                timeout=15
            )

            content = response.choices[0].message.content.strip()

            # Handle markdown code blocks in response
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            result = json.loads(content)

            # Validate category
            if result.get('category') not in CATEGORIES and result.get('category') != 'Personal':
                logger.warning(f"AI returned unknown category: {result.get('category')}")
                result['confidence'] = max(0, result.get('confidence', 50) - 20)

            logger.info(f"Rich parse: '{description}' -> {result.get('item_name')} | {result['category']} ({result.get('confidence')}%)")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Rich parse JSON error: {e}")
            return None
        except Exception as e:
            logger.error(f"Rich parse OpenAI error: {str(e)}")
            return None

    def _call_openai_simple(self, description, phone_number=None):
        """Call OpenAI for simple categorization (backward compat)."""
        try:
            if not self.client:
                self.client = OpenAI(api_key=get_openai_key())

            messages = [{"role": "system", "content": SIMPLE_SYSTEM_PROMPT}]

            # Add user's past corrections
            if phone_number and self.db:
                corrections = self.db.get_recent_feedback(phone_number, limit=3)
                for correction in corrections:
                    messages.append({
                        "role": "user",
                        "content": correction.get('description', '')
                    })
                    messages.append({
                        "role": "assistant",
                        "content": json.dumps({
                            "category": correction.get('correct_category', ''),
                            "sub_category": "",
                            "confidence": 95,
                            "reason": f"Corrected by user (was: {correction.get('wrong_category', '')})"
                        })
                    })

            messages.append({"role": "user", "content": description})

            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.1,
                max_tokens=150,
                timeout=10
            )

            content = response.choices[0].message.content.strip()
            result = json.loads(content)

            if result.get('category') not in CATEGORIES and result.get('category') != 'Personal':
                result['confidence'] = max(0, result.get('confidence', 50) - 20)

            return result

        except json.JSONDecodeError as e:
            logger.error(f"Simple categorize JSON error: {e}")
            return None
        except Exception as e:
            logger.error(f"Simple categorize OpenAI error: {str(e)}")
            return None

    def parse_multi_transaction(self, description, phone_number=None, business_type="trading", industry_class=None):
        """
        Parse a message containing MULTIPLE transactions.
        Returns a list of transaction dicts.
        """
        try:
            if not self.client:
                self.client = OpenAI(api_key=get_openai_key())

            biz_instructions = BUSINESS_PARSE_INSTRUCTIONS.get(
                business_type,
                BUSINESS_PARSE_INSTRUCTIONS.get("general")
            )

            system_prompt = f"""You are Kashia, a Nigerian AI bookkeeping assistant.
            system_prompt += self._build_catalog_context(phone_number)
The user has sent a message containing MULTIPLE transactions/items in one message.

You MUST understand:
- Nigerian Pidgin English ("I buy", "dem pay me", "each", "per one")
- Nigerian context (POS, transfer, K = thousand, M = million)
- "at X each" means unit_cost = X, total = X * quantity

{biz_instructions}

CATEGORIES:
{CATEGORY_LIST}

IMPORTANT RULES:
1. Parse ALL items/transactions mentioned in the message
2. If the message starts with "Sold" or "received", ALL items are INCOME
3. If the message starts with "Bought", "Paid", "Buy", ALL items are EXPENSE
4. Calculate total_amount = unit_cost * quantity when "each" or "per" is mentioned
5. vendor_or_customer applies to ALL items if mentioned once at the end
6. Return a JSON ARRAY of transaction objects

Respond with ONLY a valid JSON array:
[
    {{"transaction_type": "income/expense", "total_amount": number, "item_name": "...", "brand": "..." or null, "model": null, "size": "..." or null, "color": null, "quantity": "...", "unit_cost": number or null, "vendor_or_customer": "..." or null, "category": "...", "sub_category": "...", "payment_method": null, "payment_status": null, "extra_details": {{}}, "tags": [], "confidence": 0-100, "description": "short description of this item"}},
    ...
]"""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Sold 1L toilet wash 3 pieces at 3500 each, 1L dish wash 3 pieces at 2500 each, 2L laundry wash 3 pieces at 4000 each To Mrs Ibiyemi"},
                {"role": "assistant", "content": json.dumps([
                    {"transaction_type": "income", "total_amount": 10500, "item_name": "Toilet Wash", "brand": None, "model": None, "size": "1L", "color": None, "quantity": "3 pieces", "unit_cost": 3500, "vendor_or_customer": "Mrs Ibiyemi", "category": "Sales & Income", "sub_category": "Cash sales", "payment_method": None, "payment_status": "paid", "extra_details": {}, "tags": ["cleaning", "toilet-wash", "1L"], "confidence": 95, "description": "Sold 3 pieces 1L toilet wash at 3500 each"},
                    {"transaction_type": "income", "total_amount": 7500, "item_name": "Dish Wash", "brand": None, "model": None, "size": "1L", "color": None, "quantity": "3 pieces", "unit_cost": 2500, "vendor_or_customer": "Mrs Ibiyemi", "category": "Sales & Income", "sub_category": "Cash sales", "payment_method": None, "payment_status": "paid", "extra_details": {}, "tags": ["cleaning", "dish-wash", "1L"], "confidence": 95, "description": "Sold 3 pieces 1L dish wash at 2500 each"},
                    {"transaction_type": "income", "total_amount": 12000, "item_name": "Laundry Wash", "brand": None, "model": None, "size": "2L", "color": None, "quantity": "3 pieces", "unit_cost": 4000, "vendor_or_customer": "Mrs Ibiyemi", "category": "Sales & Income", "sub_category": "Cash sales", "payment_method": None, "payment_status": "paid", "extra_details": {}, "tags": ["cleaning", "laundry-wash", "2L"], "confidence": 95, "description": "Sold 3 pieces 2L laundry wash at 4000 each"},
                ])},
                {"role": "user", "content": "Paid for 20 unit office light and 50 liters of water. 2000 for water and 10000 for light"},
                {"role": "assistant", "content": json.dumps([
                    {"transaction_type": "expense", "total_amount": 10000, "item_name": "Office Light", "brand": None, "model": None, "size": None, "color": None, "quantity": "20 units", "unit_cost": 500, "vendor_or_customer": None, "category": "Equipment & Tools", "sub_category": "Repairs & Maintenance", "payment_method": None, "payment_status": "paid", "extra_details": {}, "tags": ["office", "light", "electrical"], "confidence": 90, "description": "20 units office light for 10000"},
                    {"transaction_type": "expense", "total_amount": 2000, "item_name": "Water", "brand": None, "model": None, "size": "50 liters", "color": None, "quantity": "50 liters", "unit_cost": 40, "vendor_or_customer": None, "category": "Utilities & Services", "sub_category": "Water", "payment_method": None, "payment_status": "paid", "extra_details": {}, "tags": ["water", "utility"], "confidence": 90, "description": "50 liters of water for 2000"},
                ])},
                {"role": "user", "content": description},
            ]

            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.1,
                max_tokens=1000,
                timeout=20
            )

            content = response.choices[0].message.content.strip()

            # Handle markdown code blocks
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            result = json.loads(content)

            if not isinstance(result, list):
                logger.warning("Multi-parse: AI didn't return an array")
                return None

            logger.info(f"Multi-parse: found {len(result)} transactions from: '{description[:50]}...'")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Multi-parse JSON error: {e}")
            return None
        except Exception as e:
            logger.error(f"Multi-parse OpenAI error: {str(e)}")
            return None

    def record_correction(self, phone_number, description, wrong_category, correct_category, vendor=None):
        """Record when user corrects AI's suggestion."""
        if not self.db:
            return

        self.db.save_feedback(phone_number, description, wrong_category, correct_category)

        if vendor:
            self.db.save_merchant(phone_number, vendor, correct_category)
            logger.info(f"Merchant memory updated: {vendor} -> {correct_category}")

    # ─────────────────────────────────────────────────────────
    # Generic completion (for catalog AI parsing, etc.)
    # ─────────────────────────────────────────────────────────

    def raw_completion(self, prompt: str, max_tokens: int = 300) -> str:
        """
        Send a raw prompt to GPT-4o-mini and return the text response.
        Used by catalog and other features that need custom AI parsing.
        """
        try:
            if not self.client:
                self.client = OpenAI(api_key=get_openai_key())

            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.1,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"raw_completion error: {e}")
            return ""
