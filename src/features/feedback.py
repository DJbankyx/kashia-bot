"""Feedback / support loop — shared forwarder.

A single place both the chat ("Report a Problem") and the mini-app feedback
entry points use to (1) persist the message and (2) forward it to an ADMIN
Telegram chat in real time so the owner is pinged the moment a user reports
something. Falls back to the submitter's own chat when no admin chat is set
(SSM /kashia/admin-chat-id). Never raises — a forward failure must never block
the user's acknowledgement.
"""

import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)


def _strip_markup(text: str) -> str:
    """Remove characters Telegram's Markdown parser chokes on. User feedback is
    arbitrary text; unbalanced * _ [ ` would 400 ('can't parse entities'). We
    forward it inside a plain block, so neutralise those markers."""
    return re.sub(r"[*_`\[\]]", "", str(text or ""))


def _who(db, user_id: str) -> str:
    """A human label for the submitter (business/name + id) for the admin ping."""
    try:
        u = db.get_user(user_id) or {}
        name = (u.get("business_name") or u.get("tg_name")
                or u.get("tg_username") or "").strip()
        return f"{name} ({user_id})" if name else str(user_id)
    except Exception:
        return str(user_id)


def submit_feedback(db, user_id: str, message: str, source: str = "chat") -> bool:
    """Persist + forward one feedback/support message.

    - Saves to the feedback log (reuses db.save_feedback with a [SUPPORT] tag).
    - Forwards to the admin Telegram chat (SSM /kashia/admin-chat-id); if unset,
      forwards to the submitter's OWN chat so it's never silently dropped during
      early launch.
    Returns True if it was at least saved. Never raises.
    """
    msg = (message or "").strip()
    if not msg:
        return False

    # 1. Persist (lightweight support log — reuse the feedback table).
    try:
        db.save_feedback(user_id, f"[SUPPORT:{source}] {msg}", "", "")
    except Exception as e:
        logger.warning(f"submit_feedback save failed: {e}")

    # 2. Forward to the admin chat (or the submitter as a fallback).
    try:
        from utils.config import get_admin_chat_id
        from services.telegram_client import TelegramClient
        admin = get_admin_chat_id() or user_id   # send_text strips any tg: prefix
        when = datetime.now().strftime("%d %b %Y %H:%M")
        body = (
            "\U0001F4E3 New feedback\n\n"
            f"From: {_strip_markup(_who(db, user_id))}\n"
            f"Via: {source} \u00b7 {when}\n\n"
            f"{_strip_markup(msg)}"
        )
        TelegramClient().send_text(admin, body)
    except Exception as e:
        # A forward failure must never block the user's acknowledgement.
        logger.warning(f"submit_feedback forward failed: {e}")

    return True
