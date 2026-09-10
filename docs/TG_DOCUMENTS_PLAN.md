# Stage 4 — Documents (Telegram, tap-first)

_Plan doc. Status: NEXT / scoping. Owner: Kashia Telegram Elevation._

## Goal (plain English)

Make it effortless to produce a clean **invoice, receipt, quote, or statement**
that the owner can send to a customer — built tap-first on Telegram, not through
one free-text line. The output should look professional (logo, business details,
line items, totals, bank details, terms) and be easy to forward.

Reuse the **existing PDF engine and delivery pipeline** — we change how documents
are *assembled and triggered* (a tap-first builder), not how they're rendered or
delivered. **WhatsApp stays working**: Telegram-specific building is gated by
`_is_telegram`; the existing free-text/menu paths remain for WhatsApp.

## What already exists (reuse, don't rebuild)

- **`services/pdf_generator.py` — the engine.** `generate_invoice(phone, customer,
  amount, description, items=None, discount=None, tax=None)`, `generate_receipt(
  phone, transaction_id)`, `generate_financial_statement(phone, period,
  industry)`. Already reads logo (`logo_s3_key` from S3), bank details
  (`bank_name/account_number/account_name`), `terms_conditions`, `invoice_prefix`,
  and auto-increments `inv_counter`/`rcp_counter`. Orchestrators:
  `handle_invoice_request`, `handle_multi_invoice_request(tx_ids)`,
  `handle_receipt_request` (picker), `handle_multi_receipt_request`,
  `handle_statement_request`. `deliver_pdf` → `export_service.deliver_file`.
- **`services/export_service.py` — delivery.** `upload_to_s3` (24hr presigned)
  + `deliver_file` which resolves platform (`tg:` → Telegram, bare → WhatsApp)
  and calls `send_document`. Already platform-aware.
- **Triggers/markers.** `button_dispatcher.py` maps `gen_invoice_<tx>` /
  `gen_receipt_<tx>` → markers `__GEN_INVOICE__` / `__GEN_RECEIPT__`, resolved in
  `main.py` (also `__EXPORT_PDF_STATEMENT__`). Post-sale, `transactions.py`
  already offers "Generate a document?" (Invoice / Receipt). `export.py` has the
  "📁 Export & Docs" menu (invoice/receipt/statement, PIN-gated).
- **Data.** `db.get_transaction(phone, tx_id)` exists (exact fetch); a sale stores
  one line item (item_name/qty/unit_cost) + vendor(customer) + discount/tax.

## Known gaps this stage closes

1. **Invoice creation is a single free-text prompt** (`invoices.py` / `export.py`
   → one `ask_details` step). No tap-first item builder, no discount/tax/notes
   prompts. → Build a boxed invoice builder.
2. **Multi-line invoices are faked** by selecting multiple past transactions.
   → Allow adding ad-hoc line items in the builder (not only past txns).
3. **Quotes are text-only** (`quotes.py`) — no quote PDF. → Add a quote PDF
   (reuse the invoice layout; title QUOTE, "valid until", no "paid").
4. **Delivery leaks a raw presigned URL as text** + Telegram ignores the
   filename. → Clean the delivery UX (document + short caption; drop the raw URL
   dump; keep an explicit "Forward to customer" action).
5. **Statement delivery tuple-truthiness quirk** + `get_transaction` unused by
   doc paths (list+filter). → Minor correctness cleanups.
6. Address/TIN not rendered. → Optional: render `business_address` + a new
   `tin` profile field when set (deferred unless you want it).

## The design — one boxed "Document" flow

Mirror the tidy-box / dashboard language: a single card that assembles the
document, then a clean delivery. Telegram-gated. Reserved callback prefix
`__tgdoc__` (or plain `doc_*` button ids routed via the dispatcher, like the
dashboard's `dash_*`).

### Entry
- Main menu / "📁 Documents" (Telegram) → a document type picker:
  **🧾 Invoice · 🧾 Receipt · 📄 Quote · 📊 Statement**.
- Also keep the existing post-sale "Invoice / Receipt" buttons (already wired to
  the tx) and the report-page PDF.

### Invoice / Quote builder (boxed, tap-first)
```
🧾 New Invoice
────────────────
👤 Customer: (tap a recent contact / type)
📦 Items:
   • Cement ×20 @ ₦4,000 = ₦80,000
   • Delivery      = ₦10,000
   [+ Add item]  [+ From catalog]  [+ From a sale]
💰 Subtotal ₦90,000  · Discount — · Tax —
────────────────
[💵 Discount] [🧾 Tax] [📝 Note] [📅 Due/Valid date]
[✅ Generate & Send]  [❌ Cancel]
```
- **Add item** three ways: type "name qty price", pick from the **catalog**
  (reuse `catalog.get_product_list_for_recording` + price/cost on file), or pull
  a **past sale** (existing multi-tx path).
- Customer via the CRM contact picker (reuse `_recent_contacts` pattern).
- Builds an `items[]` + optional discount/tax and calls the EXISTING
  `pdf_generator.generate_invoice(..., items=items, discount=..., tax=...)`.
- Quote = same builder; PDF titled QUOTE with a "valid until" date and no
  payment/bank block (new small template branch reusing invoice layout).

### Receipt
- Reuse `handle_receipt_request` picker (recent sales → `gen_receipt_<tx>`), plus
  post-sale receipt button. No builder needed — a receipt is for a real payment.

### Statement
- Reuse `handle_statement_request`; make it **period-aware** from the dashboard
  (pass the selected period), fixing the tuple-truthiness check.

### Delivery (clean up)
- Send the PDF as a document with a short caption ("🧾 Invoice INV-00012 for
  {customer} — ₦X"). **Stop dumping the raw presigned URL** in chat.
- Keep a distinct **"📤 Forward to {customer}"** action (the existing
  `forward_prompt`) that hands the user a shareable link when a real customer is
  named. Set a proper filename for WhatsApp; Telegram derives from the URL.

## Build order (safest-first, reuse-first, verify + push each)

- **4A — Documents home + type picker (Telegram).** `menu_export` / a new
  "Documents" entry opens the tap-first picker; WhatsApp keeps `export.show_options`.
  Wire `doc_invoice / doc_receipt / doc_quote / doc_statement`.
- **4B — Receipt + Statement (thin, reuse).** Receipt picker + statement, both
  already exist — just surface them tap-first and make statement period-aware.
  Fix the delivery UX (no raw URL) here since it's low-risk.
- **4C — Invoice builder (boxed).** Customer step + item list (type / from catalog
  / from sale) + subtotal; then Generate via `generate_invoice(items=...)`.
- **4D — Discount / Tax / Note / Due date** on the builder (all optional; feed
  the existing generate_invoice discount/tax params).
- **4E — Quote PDF.** New quote branch in the PDF engine reusing invoice layout
  (QUOTE title, valid-until, no bank/paid); quote builder = the invoice builder.
- **4F — Delivery polish + correctness.** Clean caption, drop raw URL, filename;
  switch doc paths to `db.get_transaction` for exact fetch; fix statement
  tuple-truthiness.
- **4G — Verify + live-test.** Dry-run each doc type with mock data (items,
  totals, discount/tax math, doc numbering); confirm WhatsApp export/doc paths
  unchanged; user live-tests real PDFs.

## Decisions / guardrails
- **No new PDF rendering engine** — reuse `pdf_generator` templates; only add a
  quote branch and pass richer `items[]` from the builder.
- **No new money math** — totals (subtotal, discount, tax) computed in the builder
  but rendered by the existing generator; must match the tidy-box/report numbers.
- **Delivery stays platform-aware** via `export_service.deliver_file`; documents
  are sent to the user's own chat + optional forward-to-customer.
- **WhatsApp untouched** — Telegram builder gated by `_is_telegram`; existing
  free-text invoice + export menu remain for WhatsApp.
- **PIN gate** preserved on export/statement where it exists.
- Verify with mock-data dry-runs before deploy; user live-tests real documents.

## Out of scope (later)
- Emailing/auto-sending invoices to customers (only manual forward for now).
- Recurring/scheduled invoices.
- Payment links on invoices (Paystack) — possible later tie-in.
- TIN/address rendering unless you ask for it (small add if wanted).
