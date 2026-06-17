# src/utils/parser.py
"""Nigerian Amount Parser - extracts financial data from natural text"""

import re
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def parse_amount(text):
    """
    Extract a naira amount from Nigerian-style text.

    Handles:
        "95,000"        → 95000
        "95000"         → 95000
        "95K" or "95k"  → 95000
        "₦95,000"      → 95000
        "N95,000"       → 95000
        "95 thousand"   → 95000
        "1.5M" or "1.5m"→ 1500000
        "1.5 million"   → 1500000
        "2k"            → 2000

    Returns:
        int or None (if no amount found)
    """
    if not text:
        return None

    # Clean the text
    cleaned = text.strip()

    # Remove naira symbols and "naira" word
    cleaned = cleaned.replace('₦', '').replace('NGN', '').replace('ngn', '')
    cleaned = re.sub(r'\bnaira\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bN(\d)', r'\1', cleaned)  # "N95,000" → "95,000"

    # Pattern 1: "1.5M" or "1.5m" or "1.5 million"
    match = re.search(r'(\d+\.?\d*)\s*[mM](?:illion)?', cleaned)
    if match:
        return int(float(match.group(1)) * 1_000_000)

    # Pattern 2: "95K" or "95k" or "95 thousand"
    match = re.search(r'(\d+\.?\d*)\s*[kK](?:thousand)?', cleaned)
    if match:
        return int(float(match.group(1)) * 1_000)

    # Pattern 3: "95 thousand"
    match = re.search(r'(\d+\.?\d*)\s*thousand', cleaned, flags=re.IGNORECASE)
    if match:
        return int(float(match.group(1)) * 1_000)

    # Pattern 4: "1 million"
    match = re.search(r'(\d+\.?\d*)\s*million', cleaned, flags=re.IGNORECASE)
    if match:
        return int(float(match.group(1)) * 1_000_000)

    # Pattern 5: Number with commas "95,000" or "1,500,000"
    match = re.search(r'(\d{1,3}(?:,\d{3})+)', cleaned)
    if match:
        return int(match.group(1).replace(',', ''))

    # Pattern 6: Plain number "95000" (at least 3 digits to avoid matching random numbers)
    match = re.search(r'(\d{3,})', cleaned)
    if match:
        return int(match.group(1))

    # Pattern 7: Small numbers "50" (for things like "50 naira transport")
    match = re.search(r'(\d+)', cleaned)
    if match:
        value = int(match.group(1))
        if value > 0:
            return value

    return None


def detect_transaction_type(text):
    """
    Determine if a transaction is income or expense.

    Returns:
        "income" or "expense"
    """
    if not text:
        return "expense"

    text_lower = text.lower()

    # Income keywords (English + Pidgin)
    income_keywords = [
        'sold', 'sell', 'received', 'collected', 'customer paid',
        'they pay', 'got paid', 'credited', 'income', 'revenue',
        'them pay', 'e pay me', 'pay me', 'i collect',
        'customer give', 'dem pay', 'money enter', 'cash in',
        'i sell', 'i sold'
    ]

    # Expense keywords (English + Pidgin)
    expense_keywords = [
        'bought', 'buy', 'paid', 'spent', 'purchased', 'pay for',
        'i pay', 'i buy', 'i bought', 'i spent',
        'gave', 'give', 'transfer to', 'sent to',
        'money go', 'cash out', 'i give'
    ]

    # Check income first
    for keyword in income_keywords:
        if keyword in text_lower:
            return "income"

    # Check expense
    for keyword in expense_keywords:
        if keyword in text_lower:
            return "expense"

    # Default to expense (most common for Nigerian traders)
    return "expense"


def extract_vendor_name(text):
    """
    Try to extract a vendor/person name from the transaction text.

    Patterns:
        "from Mama Ngozi"  → "Mama Ngozi"
        "to Dangote Depot" → "Dangote Depot"
        "paid Femi"        → "Femi"
        "buy from Iddo market" → "Iddo market"

    Returns:
        str or None
    """
    if not text:
        return None

    # Remove amount patterns first (so they don't interfere)
    cleaned = re.sub(r'₦?\d[\d,]*[kKmM]?', '', text)
    cleaned = re.sub(r'\b\d+\s*(thousand|million|naira)\b', '', cleaned, flags=re.IGNORECASE)

    # Pattern: "from [vendor]"
    match = re.search(r'from\s+([A-Za-z][A-Za-z\s]{1,30})', cleaned, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Pattern: "to [vendor]"
    match = re.search(r'(?:paid?\s+to|sent?\s+to|transfer\s+to|give\s+to)\s+([A-Za-z][A-Za-z\s]{1,30})', cleaned, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Pattern: "paid [person]" or "pay [person]"
    match = re.search(r'(?:paid?|pay)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', cleaned)
    if match:
        name = match.group(1).strip()
        # Filter out common non-name words
        skip_words = ['for', 'the', 'my', 'some', 'this', 'that', 'one']
        if name.lower() not in skip_words:
            return name

    # Pattern: "buy from [vendor]" — already caught above
    # Pattern: "[person] paid" (someone paid you)
    match = re.search(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+paid?', cleaned)
    if match:
        name = match.group(1).strip()
        skip_words = ['I', 'He', 'She', 'They', 'We', 'Customer']
        if name not in skip_words:
            return name

    return None


def normalize_name(name):
    """
    Normalize a contact/vendor name for consistent matching.

    "Mama Ngozi" → "mama ngozi"
    "MAMA NGOZI" → "mama ngozi"
    " mama  ngozi " → "mama ngozi"

    Returns:
        str (lowercase, trimmed, single spaces)
    """
    if not name:
        return ""

    # Lowercase, strip whitespace, collapse multiple spaces
    normalized = name.strip().lower()
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


# ==========================================
# TEST CASES (run this file directly to test)
# ==========================================

if __name__ == "__main__":
    print("=== AMOUNT PARSER TESTS ===\n")

    test_cases = [
        ("I buy rice 95K", 95000),
        ("₦150,000 for cement", 150000),
        ("spent 5000 on data", 5000),
        ("paid 1.5M for rent", 1500000),
        ("bought goods 95,000", 95000),
        ("N50,000 transport", 50000),
        ("salary 40k", 40000),
        ("200 naira pure water", 200),
        ("3.5k for data", 3500),
        ("received 1 million", 1000000),
        ("sold for 85 thousand", 85000),
        ("₦2,500,000 land", 2500000),
        ("buy rice 3 bags 95000", 95000),
        ("fuel 15,000", 15000),
        ("generator 2.5M", 2500000),
    ]

    passed = 0
    for text, expected in test_cases:
        result = parse_amount(text)
        status = "✅" if result == expected else "❌"
        if result == expected:
            passed += 1
        print(f"  {status} parse_amount('{text}') = {result} (expected {expected})")

    print(f"\n  Results: {passed}/{len(test_cases)} passed\n")

    print("=== TRANSACTION TYPE TESTS ===\n")

    type_tests = [
        ("I buy rice from market", "expense"),
        ("sold goods to Alhaji", "income"),
        ("paid Femi salary", "expense"),
        ("customer paid 50K", "income"),
        ("bought cement", "expense"),
        ("received transfer", "income"),
        ("I sell groundnut", "income"),
        ("spent on transport", "expense"),
        ("dem pay me 100K", "income"),
        ("i pay my worker", "expense"),
    ]

    passed = 0
    for text, expected in type_tests:
        result = detect_transaction_type(text)
        status = "✅" if result == expected else "❌"
        if result == expected:
            passed += 1
        print(f"  {status} detect_type('{text}') = {result} (expected {expected})")

    print(f"\n  Results: {passed}/{len(type_tests)} passed\n")

    print("=== VENDOR EXTRACTION TESTS ===\n")

    vendor_tests = [
        ("bought from Mama Ngozi", "Mama Ngozi"),
        ("paid Femi 40K", "Femi"),
        ("buy from Dangote Depot", "Dangote Depot"),
        ("i buy rice 95K", None),
        ("transfer to Chidi", "Chidi"),
    ]

    passed = 0
    for text, expected in vendor_tests:
        result = extract_vendor_name(text)
        status = "✅" if result == expected else "❌"
        if result == expected:
            passed += 1
        print(f"  {status} extract_vendor('{text}') = {result} (expected {expected})")

    print(f"\n  Results: {passed}/{len(vendor_tests)} passed")
