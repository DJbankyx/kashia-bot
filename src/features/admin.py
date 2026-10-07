"""Admin engine — broadcast to all users, reply to one user, support inbox.

Platform-neutral and shared by BOTH the chat command path
(handlers/telegram_webhook.py: /broadcast, /reply) and the mini-app admin
endpoints (handlers/miniapp.py: /app/api/admin/*). Gating is a single is_admin()
so there is one source of truth for "who may use these".

Admin ids come from SSM (/kashia/admin-chat-ids, comma-separated; folds in the
single /kashia/admin-chat-id). Ids are matched in both bare ("1072412276") and
namespaced ("tg:1072412276") form so it doesn't matter which the owner stored.
"""

import logging
import time

logger = logging.getLogger(__name__)

# Telegram allows ~30 messages/second to different chats. Pace well under that so
# a broadcast never trips the rate limit (and so one slow send can't stampede).
_BROADCAST_SLEEP = 0.05   # ~20 msg/sec


def _bare(uid: str) -> str:
    uid = str(uid or "")
    return uid[len("tg:"):] if uid.startswith("tg:") else uid


def is_admin(user_id: str) -> bool:
    """True if this user id is a configured admin. Matches bare + namespaced."""
    from utils.config import get_admin_chat_ids
    uid = str(user_id or "")
    bare = _bare(uid)
    if not bare:
        return False
    try:
        admins = get_admin_chat_ids()
    except Exception:
        admins = set()
    return uid in admins or bare in admins or ("tg:" + bare) in admins


def _send_to(user_id: str, text: str) -> bool:
    """Send a plain message to one user on THEIR platform (telegram or whatsapp).
    Returns True on success. Never raises."""
    try:
        from services.messaging_client import resolve_client
        client, recipient = resolve_client(user_id)
        if client is None:
            return False
        return bool(client.send_text(recipient, text))
    except Exception as e:
        logger.warning(f"admin send to {user_id} failed: {e}")
        return False


def reply_to_user(target_user_id: str, message: str) -> dict:
    """Send one message to one user (support reply / targeted notice).
    Returns {ok, error?}."""
    target = str(target_user_id or "").strip()
    msg = (message or "").strip()
    if not target:
        return {"ok": False, "error": "no target user"}
    if not msg:
        return {"ok": False, "error": "empty message"}
    # A reply from the team — label it so the user knows it's a human response,
    # not the bot's automation.
    body = "\U0001F4AC *Message from Kashia support*\n\n" + msg
    ok = _send_to(target, body)
    return {"ok": ok} if ok else {"ok": False, "error": "could not deliver (user may have blocked the bot)"}


def broadcast(db, message: str, onboarded_only: bool = True) -> dict:
    """Send one message to EVERY user (announcement). Paced to respect platform
    rate limits. Returns {ok, sent, failed, total}. Never raises."""
    msg = (message or "").strip()
    if not msg:
        return {"ok": False, "error": "empty message", "sent": 0, "failed": 0, "total": 0}
    # Announcements read better with a small banner so they're clearly official.
    body = "\U0001F4E2 *Kashia announcement*\n\n" + msg
    sent = failed = total = 0
    try:
        for uid in db.iter_user_ids(onboarded_only=onboarded_only):
            total += 1
            if _send_to(uid, body):
                sent += 1
            else:
                failed += 1
            time.sleep(_BROADCAST_SLEEP)
    except Exception as e:
        logger.error(f"broadcast loop failed after {total}: {e}")
    return {"ok": True, "sent": sent, "failed": failed, "total": total}


def recent_support(db, limit: int = 50) -> list:
    """List recent support messages across all users for the admin inbox.
    Each item: {phone_number, feedback_id, who, message, source, timestamp,
    replied, reply_text}. Never raises."""
    import re
    out = []
    try:
        from features.feedback import _who
        for row in db.scan_recent_feedback(limit=limit):
            desc = str(row.get("description") or "")
            # Rows are tagged "[SUPPORT:<source>] <message>".
            m = re.match(r"^\[SUPPORT:([^\]]*)\]\s*(.*)$", desc, re.S)
            source = m.group(1) if m else ""
            message = m.group(2) if m else desc
            uid = row.get("phone_number", "")
            out.append({
                "phone_number": uid,
                "feedback_id": row.get("feedback_id", ""),
                "who": _who(db, uid),
                "message": message,
                "source": source,
                "timestamp": row.get("timestamp", ""),
                "replied": bool(row.get("replied")),
                "reply_text": row.get("reply_text", ""),
            })
    except Exception as e:
        logger.error(f"recent_support failed: {e}")
    return out
