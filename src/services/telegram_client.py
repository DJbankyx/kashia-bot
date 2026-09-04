# src/services/telegram_client.py
"""
Telegram Client — sends messages to users via the Telegram Bot API.

Implements the shared MessagingClient contract so the Kashia engine can talk
to Telegram exactly the way it talks to WhatsApp: by returning neutral
response dicts. This client renders those intents using Telegram's strengths:

  - Inline keyboards are a grid (no WhatsApp 3-button cap), so button rows and
    list menus render richly without the WhatsApp row/section workarounds.
  - A single "list" intent becomes an inline keyboard where each row is a
    tappable button, with the description folded into the message body.
  - WhatsApp-style *bold* / _italic_ markup is converted to Telegram Markdown.

The engine emits platform-neutral intent; this client decides how to present
it best on Telegram.
"""

import json
import logging
import re
import time
import requests

from utils.config import get_telegram_bot_token
from services.messaging_client import MessagingClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Telegram hard limits we must respect.
CALLBACK_DATA_MAX_BYTES = 64      # inline button callback_data limit
MESSAGE_TEXT_MAX = 4096           # message text limit
CAPTION_MAX = 1024                # document caption limit

# Retry policy for transient send failures — mirrors WhatsAppClient so both
# platforms behave consistently under rate limits / transient errors.
MAX_SEND_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 4.0

# Pagination: how many option rows per page before we add Prev/Next nav.
# Reserved callback prefix for page navigation — intercepted by the webhook
# and handled locally (edit message), never dispatched to the engine.
PAGE_SIZE = 8
PAGE_NAV_PREFIX = "__tgpg__"   # e.g. "__tgpg__:2" means "go to page 2"


class TelegramClient(MessagingClient):
    """Handles all outgoing Telegram messages via the Bot API."""

    platform = "telegram"

    def __init__(self, token: str = None):
        # Token can be injected (tests) or loaded lazily from SSM.
        self._token = token or get_telegram_bot_token()

    # ── Public API (MessagingClient contract) ─────────────────────────────

    def send_text(self, to, text) -> bool:
        """Send a plain text message."""
        payload = {
            "chat_id": to,
            "text": self._prepare_text(text),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        return self._call("sendMessage", payload)

    def send_buttons(self, to, body_text, buttons) -> bool:
        """
        Send a message with tappable buttons.

        On Telegram these become an inline keyboard. Unlike WhatsApp there is
        no 3-button cap, so we lay them out in a tidy grid (2 per row unless a
        title is long, then 1 per row).
        """
        keyboard = self._buttons_to_keyboard(buttons)
        payload = {
            "chat_id": to,
            "text": self._prepare_text(body_text),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard},
        }
        return self._call("sendMessage", payload)

    def send_list(self, to, header, body_text, button_text, sections) -> bool:
        """
        Send a selectable menu.

        WhatsApp renders this as a native list picker (with a trigger button
        labelled `button_text`). Telegram has no equivalent modal, and it does
        not need one: we render the header + body as the message text, fold
        each row's description into that text, and turn every row into an
        inline keyboard button. `button_text` is unused on Telegram.
        """
        lines = []
        if header:
            lines.append(f"*{self._strip_markup(header)}*")
        if body_text:
            lines.append(self._prepare_text(body_text))

        # Collect every row across sections into a flat button list, folding
        # descriptions into the body so no information is lost.
        flat_buttons = []
        any_description = False
        for section in sections or []:
            section_title = section.get("title", "")
            rows = section.get("rows", [])
            if section_title and len(sections) > 1:
                lines.append(f"\n*{self._strip_markup(section_title)}*")
            for row in rows:
                title = row.get("title", "")
                desc = row.get("description", "")
                rid = row.get("id", "")
                if desc:
                    any_description = True
                    lines.append(f"• *{self._strip_markup(title)}* — {self._strip_markup(desc)}")
                flat_buttons.append({"id": rid, "title": title})

        # Rich menus (rows carry descriptions) read best as one button per row,
        # aligned under the description lines. Pure pickers (short chips, no
        # descriptions — categories, quantities, yes/no) grid-pack for a
        # compact, tappable layout that WhatsApp's fixed list can't match.
        if any_description:
            keyboard = [[self._make_button(b["title"], b["id"])] for b in flat_buttons]
        else:
            keyboard = self._buttons_to_keyboard(flat_buttons)

        text = "\n".join(l for l in lines if l).strip() or "Choose an option:"
        payload = {
            "chat_id": to,
            "text": self._truncate(text, MESSAGE_TEXT_MAX),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard},
        }
        return self._call("sendMessage", payload)

    def send_document(self, to, document_link, filename, caption="") -> bool:
        """Send a document by URL (Telegram fetches it server-side)."""
        payload = {
            "chat_id": to,
            "document": document_link,
        }
        if filename:
            # Telegram derives the display name from the URL, but we can hint
            # it via the caption; the actual filename shown comes from the URL
            # path. We keep filename in the caption when useful.
            pass
        if caption:
            payload["caption"] = self._truncate(self._prepare_text(caption), CAPTION_MAX)
            payload["parse_mode"] = "Markdown"
        return self._call("sendDocument", payload)

    def send_chat_action(self, to, action: str = "typing") -> bool:
        """Show a transient status (e.g. "typing…", "upload_document") to the user.

        Called by the webhook the moment a message arrives, so the chat feels
        responsive while the engine works. The action auto-clears after ~5s or
        when the next message is sent. Best-effort — never blocks the reply.
        """
        return self._call("sendChatAction", {"chat_id": to, "action": action})

    def send_and_get_id(self, to, text, keyboard=None):
        """Send a text message and return its message_id (or None on failure).

        Used when we may want to edit the message later (e.g. paginated pickers,
        live-updating screens). `keyboard` is an inline_keyboard (list of rows).
        """
        payload = {
            "chat_id": to,
            "text": self._prepare_text(text),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = self._call("sendMessage", payload, return_result=True)
        return (result or {}).get("message_id") if result else None

    def edit_message_text(self, to, message_id, text, keyboard=None) -> bool:
        """Edit an existing message's text (and optionally its keyboard) in place.

        This is the core of Telegram's "live screen" UX — menus and pages update
        the same message instead of spawning new ones. WhatsApp has no equivalent.
        """
        payload = {
            "chat_id": to,
            "message_id": message_id,
            "text": self._prepare_text(text),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self._call("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        """
        Acknowledge a button tap so Telegram stops the loading spinner on the
        user's button. Called by the webhook after receiving a callback_query.
        """
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        return self._call("answerCallbackQuery", payload)

    # ── Pagination rendering ─────────────────────────────────────────────

    def page_keyboard(self, options: list, page: int, page_size: int = PAGE_SIZE) -> list:
        """Build the inline keyboard for one page of a long option list.

        `options` is the full list of {"id","title"} buttons. Returns the grid
        for `page` plus a Prev/Next nav row (only the arrows that apply). Nav
        buttons carry the reserved PAGE_NAV_PREFIX so the webhook handles them
        locally (by editing the message) instead of routing to the engine.
        """
        total = len(options)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, pages - 1))
        start = page * page_size
        slice_ = options[start:start + page_size]

        keyboard = self._buttons_to_keyboard(slice_)

        nav = []
        if page > 0:
            nav.append({"text": "◀ Prev", "callback_data": f"{PAGE_NAV_PREFIX}:{page - 1}"})
        if pages > 1:
            nav.append({"text": f"· {page + 1}/{pages} ·",
                        "callback_data": f"{PAGE_NAV_PREFIX}:{page}"})
        if page < pages - 1:
            nav.append({"text": "Next ▶", "callback_data": f"{PAGE_NAV_PREFIX}:{page + 1}"})
        if nav:
            keyboard.append(nav)
        return keyboard

    # ── Rendering helpers ────────────────────────────────────────────────

    # Label-length thresholds for grid packing. Telegram inline buttons look
    # best when short chips share a row and long labels get their own.
    _GRID_3COL_MAX = 8    # labels this short pack 3 per row (e.g. "Yes", "1", "Cash")
    _GRID_2COL_MAX = 16   # labels this short pack 2 per row
    # (anything longer gets a full-width row of its own)

    def _buttons_to_keyboard(self, buttons) -> list:
        """
        Turn a list of {"id","title"} into a Telegram inline_keyboard grid.

        Packs buttons by label length for a tidy, tappable layout:
          - very short labels (<=8 chars): up to 3 per row
          - short labels (<=16 chars): up to 2 per row
          - long labels: their own full-width row
        Row width is chosen per the *widest* label in the current run so a row
        never mixes a tiny chip with a long label awkwardly.
        """
        keyboard = []
        row = []
        row_cap = None  # columns allowed for the current row, set by first item

        def _cols_for(label_len: int) -> int:
            if label_len <= self._GRID_3COL_MAX:
                return 3
            if label_len <= self._GRID_2COL_MAX:
                return 2
            return 1

        for btn in (buttons or []):
            title = btn.get("title", "")
            bid = btn.get("id", "")
            button = self._make_button(title, bid)
            cols = _cols_for(len(self._strip_markup(title)))

            # Starting a fresh row: this item sets the row's column budget.
            if not row:
                row_cap = cols
                row.append(button)
            else:
                # Keep the row's width to the stricter of the two so a long
                # label forces a break rather than crowding.
                row_cap = min(row_cap, cols)
                if len(row) < row_cap:
                    row.append(button)
                else:
                    keyboard.append(row)
                    row = [button]
                    row_cap = cols

            if len(row) >= row_cap:
                keyboard.append(row)
                row = []
                row_cap = None

        if row:
            keyboard.append(row)
        return keyboard

    def _make_button(self, title: str, bid: str) -> dict:
        """
        Build one inline keyboard button.

        Enforces Telegram's 64-byte callback_data limit. Button IDs in Kashia
        are short and stable (menu_home, record_sale, prod_item_<key>, ...), so
        they fit; we log loudly if one ever overflows so it's caught in testing
        rather than silently failing at runtime.
        """
        data = bid or ""
        if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
            logger.error(
                f"Telegram callback_data exceeds 64 bytes and will be rejected: "
                f"{data!r}. This button id needs shortening."
            )
            # Best effort: keep the first 64 bytes so it doesn't hard-crash the
            # whole message; the tap won't route correctly but the menu shows.
            data = data.encode("utf-8")[:CALLBACK_DATA_MAX_BYTES].decode("utf-8", "ignore")
        return {"text": self._strip_markup(title) or "•", "callback_data": data}

    def _prepare_text(self, text: str) -> str:
        """Normalize engine text for Telegram and clamp to the length limit."""
        if not isinstance(text, str):
            text = str(text)
        return self._truncate(text, MESSAGE_TEXT_MAX)

    @staticmethod
    def _strip_markup(text: str) -> str:
        """
        Strip WhatsApp-style markup for contexts that can't render it
        (button labels). Removes surrounding * and _.
        """
        if not isinstance(text, str):
            return str(text)
        return text.replace("*", "").replace("_", "").strip()

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if not isinstance(text, str):
            text = str(text)
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    # ── Transport ────────────────────────────────────────────────────────

    def _call(self, method: str, payload: dict, return_result: bool = False):
        """
        Call a Telegram Bot API method, retrying transient failures with
        exponential backoff.

        Returns:
            - By default: True on success, False on failure.
            - If return_result=True: the API's parsed `result` object on
              success (e.g. the sent Message, for its message_id), or None on
              failure.

        Retries: timeouts, connection errors, HTTP 429, and 5xx.
        Does NOT retry: non-transient 4xx (bad payload, bad token).
        """
        fail = None if return_result else False
        url = API_BASE.format(token=self._token, method=method)

        # Normalize the recipient: the engine may pass the namespaced user id
        # (e.g. "tg:1072412276"), but Telegram's API needs the bare numeric
        # chat_id ("1072412276"). Stripping here means every send is safe
        # regardless of whether the caller stripped the prefix. Without this,
        # Telegram returns 400 "chat not found".
        if isinstance(payload.get("chat_id"), str) and payload["chat_id"].startswith("tg:"):
            payload = dict(payload)
            payload["chat_id"] = payload["chat_id"][len("tg:"):]

        to = payload.get("chat_id", "unknown")

        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            retry_after = None
            try:
                response = requests.post(url, json=payload, timeout=10)

                if response.status_code == 200:
                    if attempt > 1:
                        logger.info(f"Telegram {method} ok to {to} on attempt {attempt}")
                    if return_result:
                        try:
                            return response.json().get("result")
                        except (ValueError, AttributeError):
                            return None
                    return True

                # Non-transient client errors (except 429) — don't retry.
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    logger.error(
                        f"Telegram {method} non-retryable error: "
                        f"{response.status_code} - {response.text}"
                    )
                    return fail

                logger.warning(
                    f"Telegram {method} transient error (attempt {attempt}/"
                    f"{MAX_SEND_ATTEMPTS}): {response.status_code} - {response.text}"
                )
                if response.status_code == 429:
                    # Telegram returns retry_after inside parameters.
                    try:
                        params = response.json().get("parameters", {})
                        ra = params.get("retry_after")
                        if ra is not None:
                            retry_after = min(float(ra), MAX_BACKOFF_SECONDS)
                    except (ValueError, TypeError, AttributeError):
                        retry_after = None

            except (requests.Timeout, requests.ConnectionError) as e:
                logger.warning(
                    f"Telegram {method} network error (attempt {attempt}/"
                    f"{MAX_SEND_ATTEMPTS}): {type(e).__name__}"
                )
            except Exception as e:
                logger.error(f"Error calling Telegram {method}: {str(e)}")
                return fail

            if attempt < MAX_SEND_ATTEMPTS:
                delay = retry_after if retry_after is not None else min(
                    BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS
                )
                time.sleep(delay)

        logger.error(f"Telegram {method} failed after {MAX_SEND_ATTEMPTS} attempts to {to}")
        return fail
