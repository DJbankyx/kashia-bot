# src/services/categorizer.py
"""AI Transaction Parser & Categorizer — extracts maximum detail from natural language"""

import json
import logging
from openai import OpenAI

from utils.config import get_openai_key
from utils.parser import extract_vendor_name, normalize_name

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ==========================================
# NIGERIAN CATEGORY TAXONOMY
# ==========================================

CATEGORIES = {
    "Goods & Stock": {
        "sub_categories": ["Buying goods to sell", "Raw materials", "Packaging"],
        "keywords": ["buy", "bought", "stock", "goods", "bags", "carton", "supply",
                     "restock", "purchase", "wholesale", "market"]
    },
    "Sales & Income": {
        "sub_categories": ["Cash sales", "Transfer/POS sales", "Other income"],
        "keywords": ["sold", "sell", "sales", "income", "received", "collected",
                     "customer paid", "revenue"]
    },
    "Rent & Space": {
        "sub_categories": ["Shop rent", "Warehouse", "Storage"],
        "keywords": ["rent", "shop", "warehouse", "storage", "space", "landlord"]
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
        "sub_categories": ["Staff salaries", "Casual workers", "Artisans"],
        "keywords": ["salary", "wage", "staff", "worker", "boy", "girl", "sales girl",
                     "gateman", "cleaner", "driver", "apprentice", "oga"]
    },
    "Equipment & Tools": {
        "sub_categories": ["Phone/Laptop", "Machinery", "Repairs & Maintenance"],
        "keywords": ["phone", "laptop", "computer", "machine", "equipment", "repair",
                     "maintenance", "fix", "tool", "printer"]
    },
    "Money Matters": {
        "sub_categories": ["Bank charges", "Loan repayment", "Interest paid"],
        "keywords": ["bank", "charge", "loan", "interest", "transfer fee", "sms alert",
                     "maintenance fee", "atm"]
    },
    "Marketing & Customers": {
        "sub_categories": ["Advertising", "Flyers/Signage", "Customer gifts"],
        "keywords": ["advert", "flyer", "banner", "signage", "promo", "gift",
                     "entertainment", "marketing", "social media", "facebook", "instagram"]
    },
    "Government & Compliance": {
        "sub_categories": ["Tax payments", "License/Permits", "Association dues"],
        "keywords": ["tax", "license", "permit", "registration", "levy", "association",
                     "dues", "government", "lga", "cac"]
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
- expiry: Expiry info if mentioned
- vendor_or_customer: Supplier/buyer name
""",

    "services": """BUSINESS TYPE: Services
Extract these fields with maximum detail:
- service_name: What service (haircut, phone repair, graphic design, plumbing)
- service_type: Category (maintenance, installation, consultation, creative)
- client_name: Customer/client name
- duration: Time spent if mentioned (2 hours, 3 days, 1 week)
- materials_used: Any materials/parts used (screen replacement, hair extensions)
- material_cost: Cost of materials separately if mentioned
- labor_cost: Service fee/labor portion if separated
- payment_status: Paid in full, part payment, pending, deposit
- amount_pending: How much is still owed if part payment
- vendor_or_customer: Who paid or who was paid
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

RULES:
1. Extract EVERY detail you can identify from the text
2. Calculate unit_cost from total and quantity when possible
3. If transaction type (income/expense) is ambiguous, use context clues:
   - "I buy/bought/purchased" = expense
   - "sold/customer paid/received" = income
   - "pay/paid [person]" = expense (paying someone)
   - "[person] pay me" = income (receiving payment)
4. Choose the MOST SPECIFIC category and sub_category
5. If something is not mentioned, set it to null (don't guess)
6. For known brands, capitalize properly (Nike not nike, iPhone not iphone)
7. Tags should capture useful searchable attributes

Respond with ONLY valid JSON in this format:
{{
    "transaction_type": "income" or "expense",
    "total_amount": number or null,
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
    {"role": "user", "content": "I bought 2 pairs of Nike Airforce size 43 for 85K from Sneaker Hub Ikeja"},
    {"role": "assistant", "content": json.dumps({
        "transaction_type": "expense",
        "total_amount": 85000,
        "item_name": "Sneakers",
        "brand": "Nike",
        "model": "Air Force 1",
        "size": "43",
        "color": None,
        "quantity": "2 pairs",
        "unit_cost": 42500,
        "vendor_or_customer": "Sneaker Hub Ikeja",
        "category": "Goods & Stock",
        "sub_category": "Buying goods to sell",
        "payment_method": None,
        "payment_status": "paid",
        "extra_details": {"condition": "new", "gender": "unisex"},
        "tags": ["nike", "airforce", "sneakers", "footwear", "size-43"],
        "confidence": 96,
        "reason": "Clear purchase of branded footwear for resale with all details specified"
    })},

    {"role": "user", "content": "sold 5 yards ankara to Mama Titi 15K"},
    {"role": "assistant", "content": json.dumps({
        "transaction_type": "income",
        "total_amount": 15000,
        "item_name": "Ankara fabric",
        "brand": None,
        "model": None,
        "size": None,
        "color": None,
        "quantity": "5 yards",
        "unit_cost": 3000,
        "vendor_or_customer": "Mama Titi",
        "category": "Sales & Income",
        "sub_category": "Cash sales",
        "payment_method": None,
        "payment_status": "paid",
        "extra_details": {"material": "ankara"},
        "tags": ["ankara", "fabric", "textile"],
        "confidence": 94,
        "reason": "Sale of fabric to named customer"
    })},

    {"role": "user", "content": "buy 3 bags rice 50kg Golden Penny from Alaba market 195K"},
    {"role": "assistant", "content": json.dumps({
        "transaction_type": "expense",
        "total_amount": 195000,
        "item_name": "Rice",
        "brand": "Golden Penny",
        "model": None,
        "size": "50kg bag",
        "color": None,
        "quantity": "3 bags",
        "unit_cost": 65000,
        "vendor_or_customer": "Alaba market",
        "category": "Goods & Stock",
        "sub_category": "Buying goods to sell",
        "payment_method": None,
        "payment_status": "paid",
        "extra_details": {"weight_per_bag": "50kg", "total_weight": "150kg"},
        "tags": ["rice", "golden-penny", "50kg", "bulk"],
        "confidence": 95,
        "reason": "Bulk purchase of branded rice bags from market"
    })},

    {"role": "user", "content": "Alhaji pay me 350K for the cement through transfer"},
    {"role": "assistant", "content": json.dumps({
        "transaction_type": "income",
        "total_amount": 350000,
        "item_name": "Cement",
        "brand": None,
        "model": None,
        "size": None,
        "color": None,
        "quantity": None,
        "unit_cost": None,
        "vendor_or_customer": "Alhaji",
        "category": "Sales & Income",
        "sub_category": "Transfer/POS sales",
        "payment_method": "transfer",
        "payment_status": "paid",
        "extra_details": {},
        "tags": ["cement", "building-materials", "transfer"],
        "confidence": 93,
        "reason": "Customer payment received via transfer for goods sold"
    })},

    {"role": "user", "content": "pay my barber boy 25K salary for June"},
    {"role": "assistant", "content": json.dumps({
        "transaction_type": "expense",
        "total_amount": 25000,
        "item_name": "Monthly salary",
        "brand": None,
        "model": None,
        "size": None,
        "color": None,
        "quantity": "1 month (June)",
        "unit_cost": 25000,
        "vendor_or_customer": "barber boy",
        "category": "People & Labour",
        "sub_category": "Staff salaries",
        "payment_method": None,
        "payment_status": "paid",
        "extra_details": {"period": "June", "role": "barber"},
        "tags": ["salary", "staff", "barber", "june"],
        "confidence": 95,
        "reason": "Monthly staff salary payment"
    })},

    {"role": "user", "content": "repair my generator 45K parts and labour"},
    {"role": "assistant", "content": json.dumps({
        "transaction_type": "expense",
        "total_amount": 45000,
        "item_name": "Generator repair",
        "brand": None,
        "model": None,
        "size": None,
        "color": None,
        "quantity": "1 repair job",
        "unit_cost": 45000,
        "vendor_or_customer": None,
        "category": "Equipment & Tools",
        "sub_category": "Repairs & Maintenance",
        "payment_method": None,
        "payment_status": "paid",
        "extra_details": {"includes": "parts and labour"},
        "tags": ["generator", "repair", "maintenance", "parts"],
        "confidence": 92,
        "reason": "Equipment repair including parts and labour costs"
    })},
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

Respond with ONLY valid JSON:
{{"category": "...", "sub_category": "...", "confidence": 0-100, "reason": "brief explanation"}}
""".format(categories=CATEGORY_LIST)


# ==========================================
# CATEGORIZER CLASS
# ==========================================

class TransactionCategorizer:
    """Parses and categorizes transactions using merchant memory + keyword fallback + OpenAI"""

    def __init__(self, database=None):
        self.db = database
        self.client = None  # Lazy-load OpenAI client

    def parse_transaction(self, description, phone_number=None, business_type="trading"):
        """
        RICH PARSING — extracts maximum detail from a transaction description.
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
        result = self._call_openai_rich(description, business_type, phone_number)

        if result:
            # If we have merchant memory, override category
            if vendor_memory:
                result['category'] = vendor_memory.get('category', result.get('category'))
                result['sub_category'] = vendor_memory.get('sub_category', result.get('sub_category', ''))
                logger.info(f"Merchant memory applied: {vendor} -> {vendor_memory['category']}")
            return result

        # Step 3: Fallback — simple categorize + basic fields
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
        SIMPLE CATEGORIZATION — backward compatible.
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
        """Simple keyword matching — no API call needed."""
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

    def _call_openai_rich(self, description, business_type="trading", phone_number=None):
        """Call OpenAI with the rich parsing prompt tailored to business type."""
        try:
            if not self.client:
                self.client = OpenAI(api_key=get_openai_key())

            # Get business-specific instructions
            biz_instructions = BUSINESS_PARSE_INSTRUCTIONS.get(
                business_type,
                BUSINESS_PARSE_INSTRUCTIONS.get("general")
            )

            # Build system prompt
            system_prompt = RICH_PARSE_PROMPT.format(
                business_instructions=biz_instructions,
                categories=CATEGORY_LIST
            )

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

    def record_correction(self, phone_number, description, wrong_category, correct_category, vendor=None):
        """Record when user corrects AI's suggestion."""
        if not self.db:
            return

        self.db.save_feedback(phone_number, description, wrong_category, correct_category)

        if vendor:
            self.db.save_merchant(phone_number, vendor, correct_category)
            logger.info(f"Merchant memory updated: {vendor} -> {correct_category}")
