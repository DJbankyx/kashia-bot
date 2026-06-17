# src/services/categorizer.py
"""AI Categorization Engine - categorizes transactions using OpenAI + Nigerian context"""

import json
import logging
from openai import OpenAI

from src.utils.config import get_openai_key
from src.utils.parser import extract_vendor_name, normalize_name

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
    f"{i+1}. {cat} — {', '.join(info['sub_categories'])}"
    for i, (cat, info) in enumerate(CATEGORIES.items())
])


# ==========================================
# SYSTEM PROMPT
# ==========================================

SYSTEM_PROMPT = """You are Kashia, a Nigerian bookkeeping AI assistant. You help small business owners categorize their financial transactions.

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
# FEW-SHOT EXAMPLES
# ==========================================

FEW_SHOT_EXAMPLES = [
    {"role": "user", "content": "I buy rice 3 bags from Iddo market 95,000"},
    {"role": "assistant", "content": '{"category": "Goods & Stock", "sub_category": "Buying goods to sell", "confidence": 95, "reason": "Bulk purchase of goods for resale from market"}'},

    {"role": "user", "content": "pay my sales girl 40,000 for this month"},
    {"role": "assistant", "content": '{"category": "People & Labour", "sub_category": "Staff salaries", "confidence": 95, "reason": "Monthly salary payment to staff"}'},

    {"role": "user", "content": "diesel for generator 15K"},
    {"role": "assistant", "content": '{"category": "Utilities & Services", "sub_category": "Electricity/Generator fuel", "confidence": 92, "reason": "Generator fuel is a utility expense"}'},

    {"role": "user", "content": "Alhaji Musa pay me 350,000 for cement"},
    {"role": "assistant", "content": '{"category": "Sales & Income", "sub_category": "Cash sales", "confidence": 93, "reason": "Customer payment for goods sold"}'},

    {"role": "user", "content": "pay house rent 500K"},
    {"role": "assistant", "content": '{"category": "Personal", "sub_category": "Personal expenses", "confidence": 88, "reason": "House rent is personal, not business (shop rent would be business)"}'},

    {"role": "user", "content": "buy data 2000"},
    {"role": "assistant", "content": '{"category": "Utilities & Services", "sub_category": "Internet/Data", "confidence": 85, "reason": "Data subscription for business use"}'},

    {"role": "user", "content": "uber to customer 3500"},
    {"role": "assistant", "content": '{"category": "Transport & Logistics", "sub_category": "Personal transport", "confidence": 88, "reason": "Transport to visit customer for business"}'},

    {"role": "user", "content": "bank charge me 500"},
    {"role": "assistant", "content": '{"category": "Money Matters", "sub_category": "Bank charges", "confidence": 96, "reason": "Bank service charge"}'},
]


# ==========================================
# CATEGORIZER CLASS
# ==========================================

class TransactionCategorizer:
    """Categorizes transactions using merchant memory + keyword fallback + OpenAI"""

    def __init__(self, database=None):
        """
        Args:
            database: Database instance (for merchant memory + feedback)
        """
        self.db = database
        self.client = None  # Lazy-load OpenAI client

    def categorize(self, description, phone_number=None):
        """
        Categorize a transaction description.

        Strategy:
        1. Check merchant memory (instant, no API call)
        2. Try keyword fallback (fast, no API call)
        3. Call OpenAI (accurate, costs money)

        Returns:
            dict: {"category", "sub_category", "confidence", "reason"}
        """
        # Step 1: Check merchant memory
        if phone_number and self.db:
            vendor = extract_vendor_name(description)
            if vendor:
                memory = self.db.get_merchant(phone_number, vendor)
                if memory:
                    logger.info(f"Merchant memory hit: {vendor} → {memory['category']}")
                    return {
                        "category": memory['category'],
                        "sub_category": memory.get('sub_category', ''),
                        "confidence": 95,
                        "reason": f"Known vendor: {vendor}"
                    }

        # Step 2: Try keyword fallback (high-confidence matches only)
        keyword_result = self._keyword_fallback(description)
        if keyword_result and keyword_result['confidence'] >= 85:
            logger.info(f"Keyword match: {description} → {keyword_result['category']}")
            return keyword_result

        # Step 3: Call OpenAI
        ai_result = self._call_openai(description, phone_number)
        if ai_result:
            return ai_result

        # Step 4: If all else fails, use keyword result (even low confidence)
        if keyword_result:
            return keyword_result

        # Last resort
        return {
            "category": "Uncategorized",
            "sub_category": "",
            "confidence": 0,
            "reason": "Could not determine category"
        }

    def _keyword_fallback(self, description):
        """
        Simple keyword matching — no API call needed.
        Returns result only if confident enough.
        """
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
            # Multiple keyword matches = high confidence
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

    def _call_openai(self, description, phone_number=None):
        """
        Call OpenAI GPT-4o-mini for categorization.
        Includes user's past corrections as few-shot examples.
        """
        try:
            # Lazy-load client
            if not self.client:
                self.client = OpenAI(api_key=get_openai_key())

            # Build messages
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]

            # Add standard few-shot examples
            messages.extend(FEW_SHOT_EXAMPLES)

            # Add user's past corrections (personalized learning)
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

            # Add the actual transaction to categorize
            messages.append({"role": "user", "content": description})

            # Call API
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.1,  # Low temperature = more consistent
                max_tokens=150,
                timeout=10
            )

            # Parse response
            content = response.choices[0].message.content.strip()
            result = json.loads(content)

            # Validate the category exists
            if result.get('category') not in CATEGORIES and result.get('category') != 'Personal':
                logger.warning(f"AI returned unknown category: {result.get('category')}")
                result['confidence'] = max(0, result.get('confidence', 50) - 20)

            logger.info(f"AI categorized: '{description}' → {result['category']} ({result['confidence']}%)")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"AI response not valid JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"OpenAI API error: {str(e)}")
            return None

    def record_correction(self, phone_number, description, wrong_category, correct_category, vendor=None):
        """
        Record when user corrects AI's suggestion.
        Updates both feedback table and merchant memory.
        """
        if not self.db:
            return

        # Save correction for future AI prompts
        self.db.save_feedback(phone_number, description, wrong_category, correct_category)

        # Update merchant memory if vendor identified
        if vendor:
            self.db.save_merchant(phone_number, vendor, correct_category)
            logger.info(f"Merchant memory updated: {vendor} → {correct_category}")
