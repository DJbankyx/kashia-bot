# src/utils/whatsapp_ui.py
"""Helper functions to build WhatsApp interactive message payloads."""


def text_response(content: str) -> dict:
    """Plain text message."""
    return {"type": "text", "content": content}


def button_response(body: str, buttons: list) -> dict:
    """
    Interactive button message (max 3 buttons).
    
    buttons: list of {"id": "btn_id", "title": "Button Text"}
    """
    return {
        "type": "buttons",
        "content": {
            "body": body,
            "buttons": buttons[:3],  # WhatsApp max 3
        }
    }


def list_response(header: str, body: str, button_text: str, sections: list,
                  no_paginate: bool = False, tap_first: bool = False) -> dict:
    """
    Interactive list message.
    
    sections: list of {"title": "Section", "rows": [{"id": "...", "title": "...", "description": "..."}]}

    no_paginate: Telegram-only hint. When True, a long description-less list is
    rendered as a single inline keyboard (all buttons shown) instead of being
    auto-paged. Used for rich action cards (e.g. the product card) where every
    action must be visible at once. Ignored on WhatsApp.

    tap_first: Telegram-only hint. When True, the picker renders as a clean,
    grid-packed inline keyboard (app-like taps) even when rows carry
    descriptions — the descriptions are dropped from the button layout so the
    card doesn't become a stacked-list wall of text. Used for the transaction
    payment-method picker and similar quick choices. Implies no_paginate.
    Ignored on WhatsApp (which keeps its native rich list picker).
    """
    return {
        "type": "list",
        "content": {
            "header": header,
            "body": body,
            "button_text": button_text,
            "sections": sections,
            "no_paginate": no_paginate or tap_first,
            "tap_first": tap_first,
        }
    }


def document_response(link: str, filename: str, caption: str = "") -> dict:
    """Document/file message (PDF, Excel, etc.)."""
    return {
        "type": "document",
        "content": {
            "link": link,
            "filename": filename,
            "caption": caption,
        }
    }


# ─── Common button sets ───

def confirm_buttons():
    """Standard Yes/Edit/Cancel buttons for confirmations."""
    return [
        {"id": "confirm_yes", "title": "✅ Yes"},
        {"id": "confirm_edit", "title": "✏️ Edit"},
        {"id": "confirm_cancel", "title": "❌ Cancel"},
    ]


def yes_no_buttons():
    """Simple yes/no."""
    return [
        {"id": "btn_yes", "title": "✅ Yes"},
        {"id": "btn_no", "title": "❌ No"},
    ]


def back_cancel_buttons():
    """Back + Cancel for multi-step flows."""
    return [
        {"id": "btn_back", "title": "⬅️ Back"},
        {"id": "btn_cancel", "title": "❌ Cancel"},
    ]


def done_cancel_buttons():
    """Done + Cancel for setup flows."""
    return [
        {"id": "btn_done", "title": "✅ Done"},
        {"id": "btn_cancel", "title": "❌ Cancel"},
    ]


# ─── Formatting helpers ───

def format_amount(amount) -> str:
    """Format number as ₦X,XXX. Shows decimals for amounts < 1 or with significant decimals.

    Sign-aware: negatives render as -₦X,XXX (previously any negative fell into the
    sub-naira branch because `num < 1` is true for negatives, producing an
    unformatted string like "₦-170020000"). We format the magnitude and prepend
    the sign so thousands separators always apply.
    """
    try:
        num = float(amount)
        if num == 0:
            return "₦0"
        sign = "-" if num < 0 else ""
        mag = abs(num)
        if mag < 1:
            # Sub-naira amounts: show up to 4 decimal places
            body = f"{mag:.4f}".rstrip('0').rstrip('.')
        elif mag < 100 and mag != int(mag):
            # Small amounts with decimals: show 2dp
            body = f"{mag:,.2f}"
        elif mag == int(mag):
            # Whole number
            body = f"{int(mag):,}"
        else:
            # Large amount with decimals
            body = f"{mag:,.2f}"
        return f"{sign}₦{body}"
    except (ValueError, TypeError):
        return f"₦{amount}"


def truncate(text: str, max_len: int = 72) -> str:
    """Truncate text for WhatsApp row descriptions (max 72 chars)."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"
