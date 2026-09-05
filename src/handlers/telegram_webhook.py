# src/handlers/telegram_webhook.py
"""
Telegram Webhook Handler — receives and processes incoming Telegram updates.

This is the Telegram counterpart to handlers/webhook.py (WhatsApp). It:
  1. Parses a Telegram `update` (text messages and inline-button taps).
  2. Normalizes it into the engine's shape: (user_id, text, message_type).
  3. Deduplicates by update_id (Telegram re-delivers on non-200).
  4. Acknowledges button taps via answerCallbackQuery.
  5. Calls the shared engine with platform="telegram".

User identity:
  Telegram identifies users by a numeric chat_id. To keep Telegram users
  independent from WhatsApp users (who are keyed by bare phone number in
  DynamoDB) and avoid any key collision, we namespace Telegram users with a
  "tg:" prefix, e.g. chat_id 12345678 -> user_id "tg:12345678". The engine
  treats the user_id as an opaque string key, so this is transparent to it.

Security:
  Telegram doesn't sign requests. The standard protections are (a) using the
  bot token in the webhook URL path so only Telegram knows it, and (b) an
  optional secret token echoed in the X-Telegram-Bot-Api-Secret-Token header
  (set when we register the webhook). We verify that header when configured.
"""

import json
import logging
import os
from collections import OrderedDict

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TG_USER_PREFIX = "tg:"

# Reserved callback prefix for pagination nav (◀ Prev / Next ▶). Handled
# locally by editing the message; never dispatched to the engine.
from services.telegram_client import PAGE_NAV_PREFIX
# Reserved prefix for Telegram fast-entry taps (app-like sale/purchase flow).
from utils.tg_ui import TGFX_PREFIX

# ── Update deduplication ──
# Telegram re-delivers updates if we don't answer 200 quickly. Same in-memory
# LRU approach as the WhatsApp handler (handles retries to the same warm
# Lambda instance, the common case).
_processed_updates = OrderedDict()
MAX_DEDUP_CACHE = 200


def _is_duplicate(update_id) -> bool:
    if update_id is None:
        return False
    if update_id in _processed_updates:
        return True
    _processed_updates[update_id] = True
    if len(_processed_updates) > MAX_DEDUP_CACHE:
        _processed_updates.popitem(last=False)
    return False


def lambda_handler(event, context):
    """
    Entry point for the Telegram webhook Lambda.

    Telegram only sends POST updates (no GET verification handshake — the
    webhook is registered out-of-band via setWebhook). We still guard the
    method for safety.
    """
    http_method = event.get("httpMethod", "")
    if http_method and http_method != "POST":
        return _response(405, {"error": "Method not allowed"})
    return handle_update(event)


def handle_update(event):
    """Process an incoming Telegram update (POST)."""
    try:
        if not _verify_secret(event):
            logger.warning("Telegram webhook secret mismatch — rejecting request")
            return _response(401, {"error": "Invalid secret"})

        body = json.loads(event.get("body", "{}") or "{}")

        update_id = body.get("update_id")
        if _is_duplicate(update_id):
            logger.info(f"Duplicate Telegram update skipped: {update_id}")
            return _response(200, {"status": "ok"})

        # ── Button tap (inline keyboard) ──
        callback = body.get("callback_query")
        if callback:
            _handle_callback_query(callback)
            return _response(200, {"status": "ok"})

        # ── Regular message ──
        message = body.get("message") or body.get("edited_message")
        if message:
            _handle_message(message)
            return _response(200, {"status": "ok"})

        # Other update types (channel posts, etc.) — ignore.
        return _response(200, {"status": "ignored"})

    except Exception as e:
        logger.error(f"Error processing Telegram update: {str(e)}")
        # Return 200 so Telegram doesn't hammer us with retries on our own bug.
        return _response(200, {"status": "error", "message": str(e)})


def _handle_message(message: dict):
    """Normalize a Telegram message and hand it to the engine."""
    chat = message.get("chat", {}) or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    user_id = f"{TG_USER_PREFIX}{chat_id}"

    # Immediately show "typing…" so the chat feels responsive while the engine
    # works (Telegram-only nicety; auto-clears when we reply).
    _show_typing(chat_id)

    # Text message
    text = message.get("text", "")

    if text:
        # Telegram slash-commands map to engine entry points. Some jump straight
        # to a feature by reusing the deterministic button routing (dispatched
        # as "interactive"); others become plain text the engine word-matches.
        msg_type = "text"
        if text.startswith("/"):
            # A slash command is an explicit "start this now" intent. Abandon any
            # half-finished flow first so e.g. /sale never lands mid-expense (or
            # any stale state). Onboarding is left alone — /start & /reset have
            # their own handling and will re-enter it cleanly.
            _reset_stale_flow(user_id)
            text, msg_type = _translate_command(text)
        logger.info(f"Telegram message from {user_id}: {text[:50]} ({msg_type})")
        _dispatch(user_id, text, msg_type)
        return

    # Voice note (or audio) — transcribe and feed into the normal text path so
    # "sold 3 bags of rice for 45k" spoken works exactly like typing it. This is
    # a Telegram standout: WhatsApp Cloud API makes this far clumsier.
    voice = message.get("voice") or message.get("audio")
    if voice:
        file_id = voice.get("file_id", "")
        if file_id:
            _handle_voice_note(user_id, chat_id, file_id)
        return

    # Photo. Two paths, chosen by the caption:
    #   - a receipt/invoice/quote hint  -> scan & extract (confirmation card)
    #   - otherwise                      -> treat as a logo upload (existing)
    photos = message.get("photo")
    if photos:
        file_id = photos[-1].get("file_id", "")
        caption = message.get("caption", "")
        if file_id:
            if _looks_like_document_scan(caption):
                _handle_receipt_scan(user_id, chat_id, file_id)
            else:
                _handle_photo_upload(user_id, file_id, caption)
        return

    # Document / other types — not handled yet (parity item).
    logger.info(f"Telegram unsupported message type from {user_id}; ignoring")


def _handle_callback_query(callback: dict):
    """Handle an inline-button tap."""
    data = callback.get("data", "")
    message = callback.get("message", {}) or {}
    chat = message.get("chat", {}) or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    callback_id = callback.get("id", "")

    # Acknowledge immediately so the button stops spinning.
    if callback_id:
        try:
            _get_telegram_client().answer_callback_query(callback_id)
        except Exception as e:
            logger.warning(f"answerCallbackQuery failed: {e}")

    if chat_id is None or not data:
        return

    user_id = f"{TG_USER_PREFIX}{chat_id}"

    # ── Pagination nav (◀ Prev / Next ▶) is handled locally by editing the
    #    message in place — it must NOT reach the engine. ──
    if data.startswith(PAGE_NAV_PREFIX):
        _handle_page_nav(user_id, chat_id, message_id, data)
        return

    # ── Receipt-scan confirmation (Phase A: extraction only, no record yet).
    #    Handled locally so the placeholder button doesn't confuse the engine. ──
    if data == "scan_ok":
        _send_text(user_id, "👍 Great. For now, please record it the usual way — scan-to-record is coming soon.")
        return

    # ── Telegram fast-entry taps (app-like sale/purchase flow). These are
    #    handled by the fast-entry flow (which edits its own message), and only
    #    the final hand-off (confirmation card) flows back through the engine. ──
    if data.startswith(TGFX_PREFIX):
        _handle_fastentry(user_id, chat_id, data)
        return

    # Show "typing…" while the engine builds the response to the tap.
    _show_typing(chat_id)
    logger.info(f"Telegram button from {user_id}: {data}")
    # A button tap maps to the engine's "interactive" type — the button id is
    # the text, exactly like WhatsApp button_reply ids.
    _dispatch(user_id, data, "interactive")


# Slash-commands surfaced in the Telegram command menu (see COMMAND_MENU below).
# Each maps to (routed_value, message_type):
#   - "interactive" routes reuse the engine's deterministic button dispatcher
#     by sending a known button id (e.g. "menu_report"), so the command lands
#     exactly where the equivalent tap would.
#   - "text" routes become plain text the engine word-matches.
_COMMAND_MAP = {
    "start": ("hi", "text"),          # greeting / onboarding entry
    "menu":  ("menu_home", "interactive"),
    "report": ("menu_report", "interactive"),
    "sale":  ("record_sale", "interactive"),
    "debts": ("menu_debts", "interactive"),
    "help":  ("help", "text"),
    "reset": ("set_hardreset", "interactive"),  # full reset → re-onboard fresh
}


def _translate_command(text: str) -> tuple:
    """
    Map a Telegram slash-command to (routed_value, message_type).

    Unknown commands drop the leading slash and pass through as text, so the
    engine can still word-match them.
    """
    cmd = text.lstrip("/").split()[0].split("@")[0].lower() if text else ""
    return _COMMAND_MAP.get(cmd, (cmd, "text"))


# The command list registered with Telegram via setMyCommands (see
# set_telegram_commands.sh). Descriptions show in the in-app "/" menu.
COMMAND_MENU = [
    {"command": "menu",   "description": "Open the main menu"},
    {"command": "sale",   "description": "Record a sale"},
    {"command": "report", "description": "View your reports"},
    {"command": "debts",  "description": "Who owes you / who you owe"},
    {"command": "help",   "description": "How to use Kashia"},
    {"command": "start",  "description": "Restart / onboarding"},
    {"command": "reset",  "description": "Full reset — delete all data & start over"},
]


def _reset_stale_flow(user_id: str):
    """Abandon any half-finished flow before a slash command runs.

    Prevents stale-state bleed (e.g. tapping /sale while mid-expense). Onboarding
    is preserved — a slash command mid-onboarding shouldn't wipe that progress;
    /start and /reset re-enter onboarding through their own handlers.
    """
    try:
        from main import get_bot
        from core import states
        bot = get_bot()
        session = bot.router.session.get(user_id)
        state = session.get("state", "") if session else ""
        if state in (states.ONBOARDING, states.NEW_USER):
            return  # don't disturb onboarding
        bot.router.session.reset(user_id)
    except Exception as e:
        logger.warning(f"_reset_stale_flow failed for {user_id}: {e}")


def _dispatch(user_id: str, text: str, message_type: str):
    """Route a normalized message into the shared engine."""
    from main import get_bot
    bot = get_bot()
    bot.handle_message(user_id, text, message_type, platform="telegram")


def _handle_photo_upload(user_id: str, file_id: str, caption: str = ""):
    """
    Handle a photo (logo) sent on Telegram: resolve the file, download it,
    upload to S3, and save as the user's logo — mirroring the WhatsApp flow.
    """
    try:
        import requests
        import boto3
        from utils.config import get_telegram_bot_token

        token = get_telegram_bot_token()
        bucket = os.environ.get("GENERATED_FILES_BUCKET", "kashia-generated-files-dev")

        # Step 1: getFile to resolve the file path.
        gf = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        if gf.status_code != 200:
            logger.error(f"Telegram getFile failed: {gf.text}")
            _send_text(user_id, "❌ Could not process the image. Please try again.")
            return
        file_path = gf.json().get("result", {}).get("file_path", "")
        if not file_path:
            _send_text(user_id, "❌ Could not process the image. Please try again.")
            return

        # Step 2: download the file bytes.
        dl = requests.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}",
            timeout=15,
        )
        if dl.status_code != 200:
            logger.error(f"Telegram file download failed: {dl.status_code}")
            _send_text(user_id, "❌ Could not download the image. Please try again.")
            return

        # Step 3: upload to S3.
        content_type = dl.headers.get("Content-Type", "image/jpeg")
        ext = "png" if "png" in content_type else "jpg"
        s3_key = f"logos/{user_id}/logo.{ext}"

        s3 = boto3.client("s3")
        s3.put_object(Bucket=bucket, Key=s3_key, Body=dl.content, ContentType=content_type)
        logo_url = f"https://{bucket}.s3.amazonaws.com/{s3_key}"

        # Step 4: save to profile.
        from services.database import Database
        db = Database()
        db.update_user_field(user_id, "logo_url", logo_url)
        db.update_user_field(user_id, "logo_s3_key", s3_key)
        logger.info(f"Telegram logo saved for {user_id}: {s3_key}")

        _send_text(user_id, (
            "✅ *Logo saved!*\n\n"
            "🖼️ Your business logo has been uploaded.\n"
            "It will appear on all your invoices, receipts, and statements.\n\n"
            "_Send another image anytime to update it._"
        ))
    except Exception as e:
        logger.error(f"Telegram image upload error: {e}")
        _send_text(user_id, "❌ Something went wrong uploading your logo. Please try again.")


_DOC_SCAN_HINTS = (
    "receipt", "invoice", "quote", "record", "expense", "purchase",
    "bought", "bill", "scan",
)


def _looks_like_document_scan(caption: str) -> bool:
    """A photo is treated as a document scan when its caption hints at one."""
    if not caption:
        return False
    c = caption.lower()
    return any(word in c for word in _DOC_SCAN_HINTS)


def _download_telegram_file_to_s3(file_id: str, user_id: str, kind: str = "scans"):
    """Resolve + download a Telegram file and upload it to S3.

    Returns (public_url, content_type) or (None, None) on failure. Shared by the
    receipt scanner (and reusable for other doc types later).
    """
    import os
    import requests
    import boto3
    from utils.config import get_telegram_bot_token

    token = get_telegram_bot_token()
    bucket = os.environ.get("GENERATED_FILES_BUCKET", "kashia-generated-files-dev")

    gf = requests.get(
        f"https://api.telegram.org/bot{token}/getFile",
        params={"file_id": file_id}, timeout=10,
    )
    if gf.status_code != 200:
        logger.error(f"Scan getFile failed: {gf.text}")
        return None, None
    file_path = gf.json().get("result", {}).get("file_path", "")
    if not file_path:
        return None, None

    dl = requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=20)
    if dl.status_code != 200:
        logger.error(f"Scan download failed: {dl.status_code}")
        return None, None

    content_type = dl.headers.get("Content-Type", "image/jpeg")
    ext = "png" if "png" in content_type else "jpg"
    import time as _t
    s3_key = f"documents/{user_id}/{kind}_{int(_t.time())}.{ext}"
    boto3.client("s3").put_object(
        Bucket=bucket, Key=s3_key, Body=dl.content, ContentType=content_type,
    )
    return f"https://{bucket}.s3.amazonaws.com/{s3_key}", content_type


def _handle_receipt_scan(user_id: str, chat_id, file_id: str):
    """Scan a receipt/invoice/quote photo and reply with a confirmation card.

    Phase A: EXTRACTION ONLY — nothing is recorded. The card shows what Kashia
    read and asks the user to verify. (Recording/routing comes in later phases.)
    """
    _show_typing(chat_id)
    try:
        # Look up the user's business context to sharpen sale-vs-purchase.
        business_name, industry = "", ""
        try:
            from main import get_bot
            user = get_bot().db.get_user(user_id) or {}
            business_name = user.get("business_name", "") or ""
            industry = user.get("business_type", user.get("industry_class", "")) or ""
        except Exception:
            pass

        image_url, _ = _download_telegram_file_to_s3(file_id, user_id, kind="scan")
        if not image_url:
            _send_text(user_id, "📄 I couldn't process that document. Please try a clearer photo.")
            return

        from services.receipt_scanner import ReceiptScanner
        result = ReceiptScanner().scan(image_url, business_name=business_name, industry=industry)

        if not result.get("ok"):
            _send_text(user_id, "📄 I couldn't read that document. Please try a clearer photo, or type the details in.")
            return

        card, buttons = _build_scan_card(result["data"])
        client = _get_telegram_client()
        if client is not None:
            client.send_buttons(user_id, card, buttons)
        else:
            _send_text(user_id, card)

    except Exception as e:
        logger.error(f"Receipt scan handling error for {user_id}: {e}")
        _send_text(user_id, "📄 Something went wrong scanning that. Please type the details instead.")


def _build_scan_card(data: dict):
    """Build the confirmation-card text + buttons from extracted data.

    Phase A: buttons are placeholders (scan_confirm/scan_cancel) — recording is
    wired in a later phase. The card is explicit that nothing is saved yet.
    """
    doc_type = data.get("doc_type", "unknown")
    direction = data.get("direction", "unknown")
    vendor = data.get("vendor") or "—"
    total = data.get("total")
    date = data.get("date") or "—"
    confidence = data.get("confidence", 0)
    notes = data.get("notes") or ""

    def _money(v):
        try:
            return f"₦{float(v):,.0f}"
        except (ValueError, TypeError):
            return "—"

    type_label = {
        "receipt": "🧾 Receipt",
        "invoice": "📑 Invoice",
        "quote": "💬 Quote",
        "unknown": "📄 Document",
    }.get(doc_type, "📄 Document")

    lines = [f"{type_label} — here's what I read:", ""]
    if direction in ("purchase", "sale"):
        lines.append(f"↕️ *Type:* {'Purchase (money out)' if direction == 'purchase' else 'Sale (money in)'}")
    lines.append(f"🏷️ *Vendor:* {vendor}")
    lines.append(f"💰 *Total:* {_money(total)}")
    lines.append(f"📅 *Date:* {date}")

    items = data.get("line_items") or []
    if items:
        lines.append("\n*Items:*")
        for it in items[:6]:
            nm = it.get("name", "item")
            amt = it.get("amount")
            lines.append(f"  • {nm} — {_money(amt)}" if amt is not None else f"  • {nm}")

    lines.append(f"\n_Confidence: {confidence}%_")
    if notes:
        lines.append(f"_Note: {notes}_")

    if doc_type == "quote":
        lines.append("\n⚠️ This looks like a *quote*, not a completed transaction — it wouldn't be recorded as income/expense.")

    lines.append("\n📌 _Nothing has been saved yet._ Recording from scans is coming soon — for now, please record it the usual way.")

    buttons = [
        {"id": "scan_ok", "title": "👍 Looks right"},
        {"id": "menu_home", "title": "☰ Menu"},
    ]
    return "\n".join(lines), buttons


def _handle_voice_note(user_id: str, chat_id, file_id: str):
    """Transcribe a Telegram voice note and route the text through the engine.

    Flow: getFile -> download OGG/Opus -> OpenAI Whisper -> dispatch as a normal
    text message so it hits the same transaction parser as typed input. On any
    failure we ask the user to type instead — voice is a convenience, never a
    hard dependency.
    """
    _show_typing(chat_id)
    try:
        import os
        import tempfile
        import requests
        from utils.config import get_telegram_bot_token, get_openai_key

        token = get_telegram_bot_token()

        # 1. Resolve the file path.
        gf = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        if gf.status_code != 200:
            logger.error(f"Voice getFile failed: {gf.text}")
            _send_text(user_id, "🎤 I couldn't process that voice note. Please type it instead.")
            return
        file_path = gf.json().get("result", {}).get("file_path", "")
        if not file_path:
            _send_text(user_id, "🎤 I couldn't process that voice note. Please type it instead.")
            return

        # 2. Download the audio bytes (Telegram voice notes are OGG/Opus).
        dl = requests.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}",
            timeout=20,
        )
        if dl.status_code != 200:
            logger.error(f"Voice download failed: {dl.status_code}")
            _send_text(user_id, "🎤 I couldn't download that voice note. Please type it instead.")
            return

        # 3. Transcribe with OpenAI Whisper. The SDK needs a real file-like
        #    object with a name so it can infer the format, so write to /tmp.
        suffix = os.path.splitext(file_path)[1] or ".oga"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir="/tmp") as tmp:
            tmp.write(dl.content)
            tmp_path = tmp.name

        transcript = ""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=get_openai_key())
            with open(tmp_path, "rb") as audio:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio,
                )
            transcript = (getattr(result, "text", "") or "").strip()
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        if not transcript:
            _send_text(user_id, "🎤 I couldn't make out that voice note. Please type it instead.")
            return

        # 4. Confirm what we heard, then route it exactly like typed text.
        logger.info(f"Voice transcribed for {user_id}: {transcript[:80]}")
        _send_text(user_id, f"🎤 _Heard:_ \"{transcript}\"")
        _dispatch(user_id, transcript, "text")

    except Exception as e:
        logger.error(f"Voice note handling error for {user_id}: {e}")
        _send_text(user_id, "🎤 Something went wrong with that voice note. Please type it instead.")


def _send_text(user_id: str, text: str):
    """Quick Telegram text send for the photo handler."""
    try:
        _get_telegram_client().send_text(user_id.replace(TG_USER_PREFIX, ""), text)
    except Exception as e:
        logger.error(f"Send text error in Telegram image handler: {e}")


def _get_telegram_client():
    """Get a TelegramClient (reuses the engine's registered one if available)."""
    from main import get_bot
    bot = get_bot()
    client = bot.get_client("telegram")
    return client


def _handle_page_nav(user_id: str, chat_id, message_id, data: str):
    """Handle a ◀ Prev / Next ▶ tap by editing the message to the target page.

    Reads the option list stashed in the session (keyed by this message_id),
    re-renders the requested page, and edits the message in place. Never calls
    the engine. If the stash is missing (e.g. session expired/evicted), we do
    nothing — the existing page stays on screen.
    """
    try:
        target_page = int(data.split(":", 1)[1])
    except (ValueError, IndexError):
        return

    try:
        from main import get_bot
        bot = get_bot()
        session = bot.router.session.get(user_id)
        page_store = (session.get("context", {}) or {}).get("__tg_page", {}) or {}
        entry = page_store.get(str(message_id))
        if not entry:
            logger.info(f"Page nav: no stash for msg {message_id} (expired?) — ignoring")
            return

        options = entry.get("options", [])
        text = entry.get("text", "Choose an option:")

        client = bot.get_client("telegram")
        if client is None:
            return
        keyboard = client.page_keyboard(options, target_page)
        client.edit_message_text(chat_id, message_id, text, keyboard=keyboard)
    except Exception as e:
        logger.warning(f"Page nav failed for {user_id} msg {message_id}: {e}")


def _handle_fastentry(user_id: str, chat_id, data: str):
    """Route a fast-entry tap ("__tgfx__:action:value") to the flow handler.

    The flow edits its own single message and returns [] for intermediate steps;
    at hand-off it returns the engine's confirmation-card response dicts, which
    we send through the normal engine path so the confirm→save chain proceeds.
    """
    # Parse "__tgfx__:action:value" (value optional, may itself contain ':').
    rest = data[len(TGFX_PREFIX):].lstrip(":")
    parts = rest.split(":", 1)
    action = parts[0] if parts else ""
    value = parts[1] if len(parts) > 1 else ""

    try:
        from main import get_bot
        bot = get_bot()
        fastentry = getattr(bot.router, "tg_fastentry", None)
        if fastentry is None:
            return
        responses = fastentry.handle_callback(user_id, action, value) or []
        # Intermediate steps return [] (message edited in place). A hand-off
        # returns the confirmation card — send it via the normal engine path so
        # navigation footer + subsequent confirm/save routing behave normally.
        if responses:
            bot._deliver_engine_responses(user_id, responses, platform="telegram")
    except Exception as e:
        logger.error(f"Fast-entry handling error for {user_id}: {e}")


def _show_typing(chat_id):
    """Best-effort 'typing…' indicator. Never blocks the actual reply."""
    try:
        client = _get_telegram_client()
        if client is not None and hasattr(client, "send_chat_action"):
            client.send_chat_action(chat_id, "typing")
    except Exception as e:
        logger.warning(f"send_chat_action (typing) failed: {e}")


def _verify_secret(event) -> bool:
    """
    Verify the optional Telegram secret token header.

    If /kashia/telegram-webhook-secret is configured, Telegram echoes it in
    the X-Telegram-Bot-Api-Secret-Token header (we set it during setWebhook).
    If not configured, we skip (dev) — but fail closed in prod.
    """
    try:
        from utils.config import get_parameter
        secret = get_parameter("/kashia/telegram-webhook-secret")
    except Exception:
        secret = None

    if not secret:
        stage = os.environ.get("STAGE", "dev")
        if stage == "prod":
            logger.error("CRITICAL: No Telegram webhook secret configured in production!")
            return False
        logger.warning("Telegram webhook secret check skipped (none set, dev mode)")
        return True

    headers = event.get("headers", {}) or {}
    got = (headers.get("x-telegram-bot-api-secret-token")
           or headers.get("X-Telegram-Bot-Api-Secret-Token", ""))
    import hmac
    return hmac.compare_digest(str(secret), str(got))


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
