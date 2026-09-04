# src/services/messaging_client.py
"""
MessagingClient — the platform-agnostic messaging contract.

Kashia's engine (router, features, industries) never talks to a specific
messaging platform. It returns neutral response dicts:

    {"type": "text",     "content": "..."}
    {"type": "buttons",  "content": {"body": ..., "buttons": [...]}}
    {"type": "list",     "content": {"header": ..., "body": ..., "button_text": ..., "sections": [...]}}
    {"type": "document", "content": {"link": ..., "filename": ..., "caption": ...}}

Each platform (WhatsApp, Telegram, ...) provides a concrete MessagingClient
that knows how to turn those intents into real API calls, respecting that
platform's own limits and strengths.

To add a new platform: subclass MessagingClient and implement the four
send_* methods. Everything else in the engine stays untouched.
"""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# User-id namespacing: Telegram users are keyed as "tg:<chat_id>" so they never
# collide with WhatsApp users (bare phone numbers). This prefix is the single
# source of truth for detecting a user's platform from their id.
TELEGRAM_PREFIX = "tg:"


def platform_for_user(user_id: str) -> str:
    """Infer the messaging platform from a namespaced user id."""
    if isinstance(user_id, str) and user_id.startswith(TELEGRAM_PREFIX):
        return "telegram"
    return "whatsapp"


def bare_recipient_id(user_id: str) -> str:
    """Strip the platform namespace to get the id the platform API expects.

    WhatsApp: bare phone number (unchanged).
    Telegram: numeric chat_id (drops the "tg:" prefix).
    """
    if isinstance(user_id, str) and user_id.startswith(TELEGRAM_PREFIX):
        return user_id[len(TELEGRAM_PREFIX):]
    return user_id


def resolve_client(user_id: str, whatsapp_fallback=None):
    """Resolve (client, bare_recipient_id) for delivering to a user.

    The platform is inferred from the user id namespace, then the matching
    MessagingClient is fetched from the running bot's client registry. If the
    inferred client isn't registered (e.g. Telegram token not configured yet),
    we fall back to the provided WhatsApp client so existing behavior is never
    broken.

    This lets platform-neutral services (exports, PDFs) deliver documents to
    whichever platform the user is on, without threading `platform` through
    every call site.
    """
    platform = platform_for_user(user_id)
    recipient = bare_recipient_id(user_id)

    client = None
    try:
        from main import get_bot
        client = get_bot().get_client(platform)
    except Exception as e:
        logger.warning(f"resolve_client: could not fetch bot client for {platform}: {e}")

    if client is None:
        client = whatsapp_fallback

    return client, recipient


class MessagingClient(ABC):
    """Abstract outbound-messaging contract shared by all platforms."""

    #: Short platform identifier, e.g. "whatsapp" or "telegram".
    #: Subclasses should override this.
    platform: str = "unknown"

    @abstractmethod
    def send_text(self, to, text) -> bool:
        """
        Send a plain text message.

        Args:
            to: platform-specific recipient id (phone number for WhatsApp,
                chat_id for Telegram).
            text: message body.

        Returns:
            True on success, False on failure.
        """
        raise NotImplementedError

    @abstractmethod
    def send_buttons(self, to, body_text, buttons) -> bool:
        """
        Send a message with tappable reply buttons.

        Args:
            to: recipient id.
            body_text: main message text.
            buttons: list of {"id": "btn_id", "title": "Label"}.
                     Concrete clients enforce their own platform limits
                     (e.g. WhatsApp caps at 3; Telegram allows many).

        Returns:
            True on success, False on failure.
        """
        raise NotImplementedError

    @abstractmethod
    def send_list(self, to, header, body_text, button_text, sections) -> bool:
        """
        Send a selectable list / menu message.

        Args:
            to: recipient id.
            header: header text.
            body_text: main message text.
            button_text: label on the trigger button (WhatsApp) — clients
                         that don't need it may ignore it.
            sections: list of {"title": "Section",
                               "rows": [{"id": ..., "title": ..., "description": ...}]}.

        Returns:
            True on success, False on failure.
        """
        raise NotImplementedError

    @abstractmethod
    def send_document(self, to, document_link, filename, caption="") -> bool:
        """
        Send a document/file (PDF, Excel, CSV, ...).

        Args:
            to: recipient id.
            document_link: public URL to the file (e.g. S3 presigned URL).
            filename: display name shown to the user.
            caption: optional text shown with the document.

        Returns:
            True on success, False on failure.
        """
        raise NotImplementedError
