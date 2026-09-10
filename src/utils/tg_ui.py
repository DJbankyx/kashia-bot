# src/utils/tg_ui.py
"""
Telegram "flex" UI builders — inline keyboards for app-like, tap-first flows.

These build Telegram inline_keyboard structures (lists of button rows, each
button {"text","callback_data"}) that TelegramClient already knows how to send
and edit. They are Telegram-only and never touch the WhatsApp path.

Callback namespacing:
  All fast-entry taps use the reserved prefix TGFX_PREFIX ("__tgfx__") so the
  Telegram webhook can route them to the fast-entry flow handler instead of the
  shared engine — exactly like pagination's "__tgpg__". Format:
      "__tgfx__:<action>:<value>"
  e.g. "__tgfx__:amt:5000", "__tgfx__:pay:cash", "__tgfx__:custom", "__tgfx__:back".

Keep callback_data <= 64 bytes (Telegram limit); these are all short.
"""

TGFX_PREFIX = "__tgfx__"


def _cb(action: str, value: str = "") -> str:
    """Build a namespaced fast-entry callback string."""
    return f"{TGFX_PREFIX}:{action}:{value}" if value != "" else f"{TGFX_PREFIX}:{action}"


# NGN-tuned amount presets (kobo-free, common small-business values).
DEFAULT_AMOUNT_PRESETS = [500, 1000, 2000, 5000, 10000, 20000, 50000, 100000]


def amount_keyboard(presets=None, include_custom=True, include_back=True,
                    include_total=False) -> list:
    """Grid of amount presets + Custom + Back.

    Presets are laid out 3 per row (short chips). "Custom" lets the user type an
    exact amount; "Back" returns to the previous step.

    `include_total=True` (shown on the price-EACH step when qty > 1) adds an
    "Enter total instead" escape: when the units sold at different/mixed prices,
    the user types the full total for all units rather than one price each.
    """
    presets = presets or DEFAULT_AMOUNT_PRESETS
    rows, row = [], []
    for amt in presets:
        # Compact label: 1000 -> "₦1k", 20000 -> "₦20k", 100000 -> "₦100k".
        row.append({"text": _fmt_preset(amt), "callback_data": _cb("amt", str(amt))})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if include_total:
        rows.append([{"text": "🧾 Enter total instead", "callback_data": _cb("tot")}])

    tail = []
    if include_custom:
        tail.append({"text": "✏️ Custom", "callback_data": _cb("custom")})
    if include_back:
        tail.append({"text": "⬅️ Back", "callback_data": _cb("back")})
    if tail:
        rows.append(tail)
    return rows


def payment_keyboard(credit_label="💳 Credit (owes me)") -> list:
    """Cash / Transfer / Part payment / Credit choice (+ back)."""
    return [
        [
            {"text": "💵 Cash", "callback_data": _cb("pay", "cash")},
            {"text": "🏦 Transfer", "callback_data": _cb("pay", "transfer")},
        ],
        [
            {"text": "📝 Part payment", "callback_data": _cb("pay", "part")},
            {"text": credit_label, "callback_data": _cb("pay", "credit")},
        ],
        [
            {"text": "⬅️ Back", "callback_data": _cb("back")},
        ],
    ]


def quantity_keyboard(presets=None, include_more=True, include_back=True,
                      include_no_qty=False) -> list:
    """Quick quantity grid + "type number" + Back.

    `presets` is a list of suggested quantities (learned from the item's recent
    sales / catalog). Falls back to a spread that isn't capped at 10, so items
    sold in large counts (e.g. 500, 1000, 2000 sachets) are one tap away.

    `include_no_qty=True` adds a "Just a total (no quantity)" escape — used for
    EXPENSES, where some items are countable (fuel, cartons) and some are lump
    costs (rent, bills) with no quantity.
    """
    presets = presets or DEFAULT_QTY_PRESETS
    # De-dupe, keep order, drop non-positive.
    seen, clean = set(), []
    for n in presets:
        n = int(n)
        if n > 0 and n not in seen:
            seen.add(n)
            clean.append(n)
    rows, row = [], []
    for n in clean:
        row.append({"text": _fmt_qty(n), "callback_data": _cb("qty", str(n))})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if include_more:
        rows.append([{"text": "🔢 Type number", "callback_data": _cb("qtymore")}])
    if include_no_qty:
        rows.append([{"text": "💵 Just a total (no quantity)", "callback_data": _cb("noqty")}])
    if include_back:
        rows.append([{"text": "⬅️ Back", "callback_data": _cb("back")}])
    return rows


# Fallback quantity spread (not capped at 10) for items with no sales history.
DEFAULT_QTY_PRESETS = [1, 2, 5, 10, 20, 50, 100, 500, 1000]


def _fmt_qty(n: int) -> str:
    """Compact quantity label: 1000 -> '1,000'."""
    return f"{n:,}"


def unit_keyboard(units, include_back=True) -> list:
    """Unit toggle for the quantity step (2F). `units` = list of unit words
    (e.g. ['bag', 'piece']). Each becomes a tap: __tgfx__:unit:<unit>."""
    rows, row = [], []
    for u in (units or []):
        row.append({"text": u, "callback_data": _cb("unit", u)})
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if include_back:
        rows.append([{"text": "⬅️ Back", "callback_data": _cb("back")}])
    return rows


def customer_keyboard(recent=None, include_back=True) -> list:
    """'Who?' step: recent/known customers as buttons + type-a-name + walk-in.

    `recent` is a list of (contact_id, name) tuples (most recent/known first).
    Always offers a "type a new name" option and a "Walk-in / Skip" escape so the
    user is never forced to name a random buyer.
    """
    rows = []
    for cid, name in (recent or [])[:6]:
        label = f"👤 {name}"[:40]
        rows.append([{"text": label, "callback_data": _cb("cust", str(cid))}])
    rows.append([{"text": "✍️ Type a name", "callback_data": _cb("custtype")}])
    rows.append([{"text": "🚶 Walk-in / Skip", "callback_data": _cb("walkin")}])
    if include_back:
        rows.append([{"text": "⬅️ Back", "callback_data": _cb("back")}])
    return rows


def confirm_keyboard() -> list:
    """Save / Edit / Cancel row for a fast-entry confirmation."""
    return [[
        {"text": "✅ Save", "callback_data": _cb("save")},
        {"text": "✏️ Edit", "callback_data": _cb("edit")},
        {"text": "❌ Cancel", "callback_data": _cb("cancel")},
    ]]


def edit_menu_keyboard(fields) -> list:
    """Field-picker shown when the user taps ✏️ Edit on the confirm card.

    `fields` is an ordered list of (field_key, label) tuples — only the fields
    that actually apply to this transaction (e.g. no Quantity for a lump
    expense). Each becomes `__tgfx__:editf:<field_key>` which jumps straight to
    that step. A Back button returns to the confirm card unchanged.
    """
    rows, row = [], []
    for key, label in fields:
        row.append({"text": label, "callback_data": _cb("editf", key)})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ Back", "callback_data": _cb("editback")}])
    return rows


def product_grid(rows, page: int = 0, page_size: int = 8, other_label="📝 Other") -> list:
    """Inline grid of catalog products (paged) + an 'Other' escape + Back.

    `rows` is the catalog list-builder output ({"id","title","description"}).
    Product taps carry the fast-entry 'prod' action with the product id as value;
    an 'Other / not listed' option falls back to free-text entry.

    Pagination here uses the fast-entry namespace (not __tgpg__) because these
    taps must reach the fast-entry handler, which re-renders with product context.
    """
    rows = rows or []
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    slice_ = rows[start:start + page_size]

    keyboard = []
    # One product per row (titles are usually descriptive/long).
    for r in slice_:
        title = (r.get("title", "") or "item")[:40]
        pid = r.get("id", "")
        keyboard.append([{"text": title, "callback_data": _cb("prod", pid)}])

    # Prev/Next nav for products (fast-entry namespaced).
    nav = []
    if page > 0:
        nav.append({"text": "◀ Prev", "callback_data": _cb("ppage", str(page - 1))})
    if pages > 1:
        nav.append({"text": f"· {page + 1}/{pages} ·", "callback_data": _cb("noop")})
    if page < pages - 1:
        nav.append({"text": "Next ▶", "callback_data": _cb("ppage", str(page + 1))})
    if nav:
        keyboard.append(nav)

    keyboard.append([{"text": other_label, "callback_data": _cb("other")}])
    # Back/Cancel — lets the user leave the item picker (the first step) instead
    # of being stuck scrolling. At the product step this cancels the flow.
    keyboard.append([{"text": "⬅️ Back", "callback_data": _cb("back")}])
    return keyboard


def _fmt_preset(amount: int) -> str:
    """Compact NGN label for a preset amount."""
    if amount >= 1000 and amount % 1000 == 0:
        return f"₦{amount // 1000}k"
    return f"₦{amount:,}"


# ═══════════════════════════════════════════════════════════════════════
# INVOICE BUILDER (Stage 4C) — own reserved namespace so its taps route to the
# invoice builder handler, never the sale/purchase fast-entry flow.
#   Format: "__tginv__:<action>:<value>"
# ═══════════════════════════════════════════════════════════════════════

TGINV_PREFIX = "__tginv__"


def _icb(action: str, value: str = "") -> str:
    """Build a namespaced invoice-builder callback string."""
    return f"{TGINV_PREFIX}:{action}:{value}" if value != "" else f"{TGINV_PREFIX}:{action}"


def inv_customer_keyboard(recent=None) -> list:
    """Invoice 'Who is this for?' step: recent customers + type-a-name."""
    rows = []
    for cid, name in (recent or [])[:6]:
        rows.append([{"text": f"👤 {name}"[:40], "callback_data": _icb("cust", str(cid))}])
    rows.append([{"text": "✍️ Type a name", "callback_data": _icb("custtype")}])
    rows.append([{"text": "❌ Cancel", "callback_data": _icb("cancel")}])
    return rows


def inv_builder_keyboard(has_items: bool) -> list:
    """The main invoice-builder card: add items (3 ways), then generate."""
    rows = [
        [{"text": "➕ Type an item", "callback_data": _icb("add_type")}],
        [{"text": "🗂️ From catalog", "callback_data": _icb("add_cat")},
         {"text": "🧾 From a past sale", "callback_data": _icb("add_sale")}],
    ]
    if has_items:
        rows.append([{"text": "💵 Discount", "callback_data": _icb("discount")},
                     {"text": "🧾 Tax", "callback_data": _icb("tax")}])
        rows.append([{"text": "📝 Note", "callback_data": _icb("note")},
                     {"text": "📅 Due date", "callback_data": _icb("due")}])
        rows.append([{"text": "✅ Generate & Send", "callback_data": _icb("generate")}])
        rows.append([{"text": "🗑️ Remove last item", "callback_data": _icb("rmlast")}])
    rows.append([{"text": "❌ Cancel", "callback_data": _icb("cancel")}])
    return rows


def inv_due_keyboard() -> list:
    """Quick due-date choices for an invoice."""
    return [
        [{"text": "On receipt", "callback_data": _icb("dueset", "0")},
         {"text": "7 days", "callback_data": _icb("dueset", "7")}],
        [{"text": "14 days", "callback_data": _icb("dueset", "14")},
         {"text": "30 days", "callback_data": _icb("dueset", "30")}],
        [{"text": "⬅️ Back", "callback_data": _icb("back")}],
    ]


def inv_catalog_grid(rows, page: int = 0, page_size: int = 8) -> list:
    """Catalog products for adding an invoice line (own namespace)."""
    rows = rows or []
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    slice_ = rows[start:start + page_size]
    kb = []
    for r in slice_:
        kb.append([{"text": (r.get("title", "") or "item")[:40],
                    "callback_data": _icb("cat", r.get("id", ""))}])
    nav = []
    if page > 0:
        nav.append({"text": "◀ Prev", "callback_data": _icb("catpage", str(page - 1))})
    if page < pages - 1:
        nav.append({"text": "Next ▶", "callback_data": _icb("catpage", str(page + 1))})
    if nav:
        kb.append(nav)
    kb.append([{"text": "⬅️ Back", "callback_data": _icb("back")}])
    return kb


def inv_sales_keyboard(sales) -> list:
    """Recent sales to pull into the invoice as a line. `sales` = list of
    (tx_id, label) tuples."""
    kb = []
    for tx_id, label in (sales or [])[:8]:
        kb.append([{"text": label[:40], "callback_data": _icb("sale", str(tx_id))}])
    kb.append([{"text": "⬅️ Back", "callback_data": _icb("back")}])
    return kb
