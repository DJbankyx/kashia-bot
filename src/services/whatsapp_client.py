# src/services/whatsapp_client.py
"""WhatsApp Client - sends messages to users via Meta Cloud API"""

import json
import logging
import time
import requests

from utils.config import get_whatsapp_token, get_phone_number_id

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Meta WhatsApp API base URL
API_VERSION = "v21.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"

# Retry policy for transient send failures (timeouts, connection errors,
# HTTP 429 rate limits, and 5xx server errors). Non-transient 4xx errors
# (bad payload, auth) are NOT retried. Kept small to stay within Lambda time.
MAX_SEND_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5   # 0.5s, 1s, ... (exponential)
MAX_BACKOFF_SECONDS = 4.0


class WhatsAppClient:
    """Handles all outgoing WhatsApp messages"""

    def __init__(self):
        self.token = get_whatsapp_token()
        self.phone_number_id = get_phone_number_id()
        self.url = f"{BASE_URL}/{self.phone_number_id}/messages"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    def send_text(self, to, text):
        """
        Send a simple text message.

        Args:
            to: recipient phone number (e.g., "2348012345678")
            text: message content
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }
        return self._send(payload)

    def send_buttons(self, to, body_text, buttons):
        """
        Send a message with up to 3 buttons.

        Args:
            to: recipient phone number
            body_text: main message text
            buttons: list of dicts [{"id": "btn_1", "title": "Yes"}] (max 3)
        """
        # WhatsApp allows max 3 buttons, title max 20 chars
        button_list = []
        for btn in buttons[:3]:
            button_list.append({
                "type": "reply",
                "reply": {
                    "id": btn["id"],
                    "title": btn["title"][:20]
                }
            })

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {
                    "buttons": button_list
                }
            }
        }
        return self._send(payload)

    def send_list(self, to, header, body_text, button_text, sections):
        """
        Send a list message (up to 10 items per section).

        Args:
            to: recipient phone number
            header: header text
            body_text: main message text
            button_text: text on the list button (e.g., "Choose option")
            sections: list of dicts [{"title": "Section", "rows": [{"id": "1", "title": "Option 1"}]}]
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {"type": "text", "text": header},
                "body": {"text": body_text},
                "action": {
                    "button": button_text[:20],
                    "sections": sections
                }
            }
        }
        return self._send(payload)

    def send_document(self, to, document_link, filename, caption=""):
        """
        Send a document (PDF, Excel, CSV) as attachment.

        Args:
            to: recipient phone number
            document_link: public URL to the file (e.g., S3 presigned URL)
            filename: display name (e.g., "Kashia_Report_June.pdf")
            caption: optional text below the document
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "document",
            "document": {
                "link": document_link,
                "filename": filename,
                "caption": caption
            }
        }
        return self._send(payload)

    def mark_read(self, message_id):
        """
        Mark a message as read (blue ticks).

        Args:
            message_id: the wamid of the message to mark
        """
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id
        }
        return self._send(payload)

    def _send(self, payload):
        """
        Send a request to WhatsApp API, retrying transient failures with
        exponential backoff. Returns True on success, False on failure.

        Retries: timeouts, connection errors, HTTP 429, and 5xx.
        Does NOT retry: non-transient 4xx (e.g. 400 bad payload, 401 auth).
        """
        to = payload.get("to", "unknown")

        for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
            retry_after = None
            try:
                response = requests.post(
                    self.url,
                    headers=self.headers,
                    json=payload,
                    timeout=10,
                )

                if response.status_code == 200:
                    if attempt > 1:
                        logger.info(f"Message sent to {to} on attempt {attempt}")
                    else:
                        logger.info(f"Message sent successfully to {to}")
                    return True

                # Non-transient client errors (except 429) — don't retry
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    logger.error(
                        f"WhatsApp API non-retryable error: "
                        f"{response.status_code} - {response.text}"
                    )
                    return False

                # Transient: 429 or 5xx — log and fall through to backoff/retry
                logger.warning(
                    f"WhatsApp API transient error (attempt {attempt}/"
                    f"{MAX_SEND_ATTEMPTS}): {response.status_code} - {response.text}"
                )
                if response.status_code == 429:
                    # Honor Retry-After header when present (seconds), capped.
                    hdr = response.headers.get("Retry-After")
                    if hdr:
                        try:
                            retry_after = min(float(hdr), MAX_BACKOFF_SECONDS)
                        except (ValueError, TypeError):
                            retry_after = None

            except (requests.Timeout, requests.ConnectionError) as e:
                logger.warning(
                    f"WhatsApp API network error (attempt {attempt}/"
                    f"{MAX_SEND_ATTEMPTS}): {type(e).__name__}"
                )
            except Exception as e:
                # Unexpected error — don't retry blindly
                logger.error(f"Error sending message: {str(e)}")
                return False

            # If there are attempts left, wait (exponential backoff) then retry.
            if attempt < MAX_SEND_ATTEMPTS:
                delay = retry_after if retry_after is not None else min(
                    BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS
                )
                time.sleep(delay)

        logger.error(f"WhatsApp send failed after {MAX_SEND_ATTEMPTS} attempts to {to}")
        return False
