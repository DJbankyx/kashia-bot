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


def list_response(header: str, body: str, button_text: str, sections: list) -> dict:
    """
    Interactive list message.
    
    sections: list of {"title": "Section", "rows": [{"id": "...", "title": "...", "description": "..."}]}
    """
    return {
        "type": "list",
        "content": {
            "header": header,
            "body": body,
            "button_text": button_text,
            "sections": sections,
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
    """Format number as ₦X,XXX."""
    try:
        num = float(amount)
        if num >= 1_000_000:
            return f"₦{num:,.0f}"
        elif num >= 1_000:
            return f"₦{num:,.0f}"
        else:
            return f"₦{num:.0f}"
    except (ValueError, TypeError):
        return f"₦{amount}"


def truncate(text: str, max_len: int = 72) -> str:
    """Truncate text for WhatsApp row descriptions (max 72 chars)."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "…"
