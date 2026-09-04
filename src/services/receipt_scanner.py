# src/services/receipt_scanner.py
"""
Receipt / invoice / quote scanner (Phase A: extraction only).

Given an image URL (a receipt, invoice, or quote photo), uses an OpenAI
vision model to extract structured data — WITHOUT recording anything. The
caller (webhook) turns the result into a confirmation card so the user can
verify before it ever touches the ledger.

Design notes:
- Uses the same OpenAI SDK + model family already in the project (gpt-4o-mini
  is vision-capable), so no new dependencies.
- Money accuracy matters, so this NEVER auto-records. It returns structured
  fields + a confidence score; the human confirms.
- Returns a plain dict (never raises to the caller); on any failure it returns
  {"ok": False, "error": ...} so the webhook can fall back gracefully.
"""

import json
import logging

from utils.config import get_openai_key

logger = logging.getLogger(__name__)

# Document types we recognize. "quote" is deliberately NOT a ledger entry —
# it's a potential future sale, so downstream routing (Phase C) must treat it
# differently. We surface it here so the confirm card can say so.
DOC_TYPES = ("receipt", "invoice", "quote", "unknown")

_VISION_SYSTEM_PROMPT = """You read photos of business documents (receipts, \
invoices, quotes) for a Nigerian small-business bookkeeping assistant and \
return STRICT JSON only — no prose, no markdown.

Extract these fields:
{
  "doc_type": "receipt" | "invoice" | "quote" | "unknown",
  "direction": "purchase" | "sale" | "unknown",   // purchase = money the user PAID/OWES; sale = money the user RECEIVED/IS OWED
  "vendor": string | null,        // the other party (shop/supplier/customer name)
  "date": "YYYY-MM-DD" | null,
  "currency": string | null,      // e.g. "NGN"
  "total": number | null,         // the grand total as a number, no symbols/commas
  "tax": number | null,
  "line_items": [ { "name": string, "qty": number | null, "amount": number | null } ],
  "confidence": 0-100,            // your confidence the total + doc_type are correct
  "notes": string | null          // anything ambiguous or unreadable
}

Rules:
- A QUOTE/estimate/proforma is NOT a completed transaction — set doc_type "quote".
- A RECEIPT means money already moved. An INVOICE is a bill (may be unpaid).
- Amounts are Nigerian Naira unless clearly otherwise. Return numbers only.
- If the image is unreadable or not a financial document, set doc_type \
"unknown", total null, confidence low, and explain in notes.
- Never invent a total you cannot see; use null and lower confidence instead."""


class ReceiptScanner:
    """Vision extraction for receipts/invoices/quotes. Extraction only (no record)."""

    MODEL = "gpt-4o-mini"  # vision-capable; same family as the categorizer

    def __init__(self):
        self._client = None  # lazy

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=get_openai_key())
        return self._client

    def scan(self, image_url: str, business_name: str = "", industry: str = "") -> dict:
        """Extract structured data from a document image.

        Args:
            image_url: publicly fetchable URL to the image (e.g. S3).
            business_name: the user's business name — helps decide sale vs
                purchase (an invoice bearing the user's own name is a sale).
            industry: the user's industry, as a hint.

        Returns:
            {"ok": True, "data": {...extracted fields...}} on success, or
            {"ok": False, "error": "..."} on failure. Never raises.
        """
        if not image_url:
            return {"ok": False, "error": "no image url"}

        try:
            client = self._get_client()

            context_hint = ""
            if business_name:
                context_hint += (
                    f"\nThe user's own business is called \"{business_name}\". "
                    f"If the document is issued BY this business, it's a sale; "
                    f"if issued TO them by someone else, it's a purchase."
                )
            if industry:
                context_hint += f"\nThe user's industry is: {industry}."

            response = client.chat.completions.create(
                model=self.MODEL,
                messages=[
                    {"role": "system", "content": _VISION_SYSTEM_PROMPT + context_hint},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Extract the JSON for this document."},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    },
                ],
                temperature=0.1,
                max_tokens=700,
                timeout=30,
            )

            content = (response.choices[0].message.content or "").strip()

            # Strip markdown fences if the model added them.
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()
                if content.lower().startswith("json"):
                    content = content[4:].strip()

            data = json.loads(content)
            data = self._normalize(data)
            logger.info(
                f"Receipt scan: type={data.get('doc_type')} dir={data.get('direction')} "
                f"total={data.get('total')} conf={data.get('confidence')}"
            )
            return {"ok": True, "data": data}

        except json.JSONDecodeError as e:
            logger.error(f"Receipt scan JSON parse failed: {e}")
            return {"ok": False, "error": "could not read the document"}
        except Exception as e:
            logger.error(f"Receipt scan error: {e}")
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _normalize(data: dict) -> dict:
        """Coerce the model output into safe, predictable shapes."""
        if not isinstance(data, dict):
            return {"doc_type": "unknown", "confidence": 0, "notes": "bad model output"}

        doc_type = str(data.get("doc_type", "unknown")).lower()
        if doc_type not in DOC_TYPES:
            doc_type = "unknown"
        data["doc_type"] = doc_type

        direction = str(data.get("direction", "unknown")).lower()
        if direction not in ("purchase", "sale", "unknown"):
            direction = "unknown"
        data["direction"] = direction

        # Numbers: coerce total/tax to float or None.
        for k in ("total", "tax"):
            v = data.get(k)
            if isinstance(v, str):
                v = v.replace(",", "").replace("₦", "").replace("N", "").strip()
                try:
                    v = float(v)
                except (ValueError, TypeError):
                    v = None
            elif not isinstance(v, (int, float)):
                v = None
            data[k] = v

        try:
            data["confidence"] = int(data.get("confidence", 0) or 0)
        except (ValueError, TypeError):
            data["confidence"] = 0

        if not isinstance(data.get("line_items"), list):
            data["line_items"] = []

        return data
