# src/services/whatsapp_client.py
"""WhatsApp Client - sends messages to users via Meta Cloud API"""

import json
import logging
import requests

from src.utils.config import get_whatsapp_token, get_phone_number_id

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Meta WhatsApp API base URL
API_VERSION = "v17.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"


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
        Send a request to WhatsApp API.
        Returns True on success, False on failure.
        """
        try:
            response = requests.post(
                self.url,
                headers=self.headers,
                json=payload,
                timeout=10
            )

            if response.status_code == 200:
                logger.info(f"Message sent successfully to {payload.get('to', 'unknown')}")
                return True
            else:
                logger.error(
                    f"WhatsApp API error: {response.status_code} - {response.text}"
                )
                return False

        except requests.Timeout:
            logger.error("WhatsApp API timeout")
            return False
        except Exception as e:
            logger.error(f"Error sending message: {str(e)}")
            return False
