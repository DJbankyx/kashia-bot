# src/core/states.py
"""All conversation states in one place."""

# ─── Core States ───
NEW_USER = "NEW_USER"
ONBOARDING = "ONBOARDING"
IDLE = "IDLE"

# ─── Transaction Recording ───
RECORDING = "RECORDING"
AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
AWAITING_CORRECTION = "AWAITING_CORRECTION"

# ─── Debt / Credit ───
DEBT_RECORDING = "DEBT_RECORDING"
DEBT_CONFIRMING = "DEBT_CONFIRMING"
DEBT_PAYMENT = "DEBT_PAYMENT"

# ─── Catalog Setup ───
CATALOG_MENU = "CATALOG_MENU"
CATALOG_SETUP_PRODUCTS = "CATALOG_SETUP_PRODUCTS"
CATALOG_SETUP_DETAILS = "CATALOG_SETUP_DETAILS"
CATALOG_ORGANIZE = "CATALOG_ORGANIZE"
CATALOG_ADD_DATA = "CATALOG_ADD_DATA"

# ─── CRM Prompt ───
CRM_HINT = "CRM_HINT"

# ─── Post-purchase cost choice (Use new / Keep old / Weighted avg) ───
COST_CHOICE = "COST_CHOICE"

# ─── Records: typed date / range for the period-scoped record list ───
RECORDS_DATE = "RECORDS_DATE"

# ─── Export / Documents ───
EXPORTING = "EXPORTING"
INVOICING = "INVOICING"
INVOICE_BUILDER = "INVOICE_BUILDER"   # Telegram tap-first invoice builder (4C)

# ─── Edit / Delete ───
EDITING = "EDITING"
EDIT_TRANSACTION = "EDIT_TRANSACTION"
DELETE_CONFIRM = "DELETE_CONFIRM"

# ─── Guided Recording (button-driven) ───
GUIDED_RECORDING = "GUIDED_RECORDING"
CATALOG_RECORDING = "CATALOG_RECORDING"

# ─── Landing Cost (after sale) ───
LANDING_COST = "LANDING_COST"

# ─── Variant Selection (after sale, before landing cost) ───
VARIANT_SELECTION = "VARIANT_SELECTION"

# ─── Payment Method (Cash/Credit after confirmation) ───
PAYMENT_METHOD = "PAYMENT_METHOD"

# ─── Production Recording (manufacturing only) ───
PRODUCTION_RECORDING = "PRODUCTION_RECORDING"

# ─── Recurring Services (services only) ───
RECURRING_SERVICES = "RECURRING_SERVICES"

# ─── Telegram fast-entry (app-like tappable sale/purchase flow; Telegram only) ───
TG_FASTENTRY = "TG_FASTENTRY"

# ─── Document Scan confirm (Telegram only; N4.5). Holds a pending scanned
#     document awaiting the user's one-tap confirm-to-record. ───
SCAN_CONFIRM = "SCAN_CONFIRM"

# ─── Returns / Refunds (build #4). Telegram tap-first: pick an original sale/
#     purchase, then confirm the return quantity (full or partial). ───
RETURN_RECORDING = "RETURN_RECORDING"

# ─── Bill a customer (Option A): Telegram tap-first multi-item single document.
#     Pick a customer, toggle several of their sales, generate ONE invoice/
#     receipt from the selected set. Holds the selection set in context. ───
BILLDOC_SELECT = "BILLDOC_SELECT"

# ─── Personal Info ───
PERSONAL_INFO = "PERSONAL_INFO"

# ─── Settings ───
SETTINGS_FLOW = "SETTINGS_FLOW"

# ─── CRM Add Contact ───
CRM_ADDING = "CRM_ADDING"

# ─── PIN Verification ───
PIN_VERIFYING = "PIN_VERIFYING"

# ─── Groups ───
# States where tier/limit check should NOT apply
EXEMPT_STATES = {
    ONBOARDING, NEW_USER,
    AWAITING_CONFIRMATION, AWAITING_CORRECTION,
    CATALOG_MENU, CATALOG_SETUP_PRODUCTS, CATALOG_SETUP_DETAILS,
    CATALOG_ORGANIZE, CATALOG_ADD_DATA,
    EDITING, EDIT_TRANSACTION, DELETE_CONFIRM,
    EXPORTING, INVOICING,
    DEBT_RECORDING, DEBT_CONFIRMING, DEBT_PAYMENT,
    CRM_HINT, COST_CHOICE, GUIDED_RECORDING,
    PERSONAL_INFO, SETTINGS_FLOW, CRM_ADDING, CATALOG_RECORDING, PIN_VERIFYING,
    LANDING_COST, PAYMENT_METHOD, PRODUCTION_RECORDING, RECURRING_SERVICES,
    VARIANT_SELECTION, TG_FASTENTRY, INVOICE_BUILDER,
    SCAN_CONFIRM, RETURN_RECORDING, BILLDOC_SELECT,
    RECORDS_DATE,
}
