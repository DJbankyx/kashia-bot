# Telegram Root-Level Upgrade Map

_Created 2026-09-03. Based on a full audit of CRM, Catalog, and Documents._

Each candidate is tagged by change type:
- **[UI]** pure presentation — Telegram layer only, WhatsApp untouched.
- **[EXTEND]** engine gains a new capability/intent (additive; WhatsApp ignores it).
- **[DE-BIAS]** remove a WhatsApp assumption baked into shared logic (careful refactor).

The one repeated root cause: `utils/whatsapp_ui.py` hard-caps buttons to 3
(`buttons[:3]`) and handlers slice rows to `[:9]`/`[:10]`, and every flow returns
"a list of messages" (one dict = one message). The transport is already
platform-agnostic (`resolve_client`); the SHAPING is where WhatsApp is baked in.

---

## 1. CRM / Contacts  — highest UX payoff, most self-contained → DO FIRST

Current constraints (contacts.py):
- All-contacts list capped `[:10]`, no pagination, no in-UI search (just a
  "type a name" hint). `db.get_contacts(limit=100)` already returns the data.
- Contact profile is a `━━━`-framed text blob + at most 3 action buttons
  (`buttons[:3]`, explicit "WhatsApp max 3" comment).
- Top customers/suppliers, reminders, insights are all text blobs; reminders
  are NOT actionable (can't attach a button per debtor).
- No edit-contact flow at all; add-contact is 3 separate messages.

Upgrade:
- **[UI]** Browsable, paginated contact list (reuse Phase-4 pagination).
- **[UI]** Rich contact "card" edited in place: financials, timeline, debt —
  with a full action fan-out (Remind, Record sale, History, Edit, Statement)
  instead of 3 buttons.
- **[UI]** Actionable reminders: one "Remind" button per debtor.
- **[EXTEND]** In-UI search + an edit-contact flow (engine needs a search
  intent / offset param and a contact-update path; data already exists).

---

## 2. Product Catalog  — biggest constraint surface → DO SECOND

Current constraints (catalog.py):
- Recording picker hard-capped to "9 products + Other" (`if len(rows) >= 9`);
  items 10+ are unreachable except by typing. Same `[:9]` in services/materials.
- Management picker splits into "A–M / N–Z" sections to fake >10, effectively
  ~20 max, plus a synthetic "Type Name" escape + fuzzy text matcher.
- Menu capped `rows[:10]`; stock levels + product detail are text blobs.
- Categories are stored but never used to navigate; no product search.

Upgrade:
- **[UI]** Paginated product grid for BOTH recording and management (kills the
  9/10 caps and the A–M/N–Z hack). Fast-entry pilot already proved the pattern.
- **[UI]** Product "card" edited in place: stock, cost, unit, variants — with
  inline +/- stock adjust instead of "pick product → type +5".
- **[EXTEND]** Category browsing + product search (engine needs
  `get_products(category=...)` / `search_products(query)` intents).

---

## 3. Documents (Invoice / Receipt / Quote / Statement) → DO THIRD

Current constraints (invoices.py, quotes.py, pdf_generator.py):
- Invoice = ONE free-text line parsed then generated immediately. No
  interactive line items, no preview, no edit-before-generate.
- Multi-line invoices only assembled from ALREADY-recorded transactions.
- Quotes output a copy-paste TEXT BLOB, not a document ("copy the quote above
  and send it").
- Delivery = document + redundant "check your chat" text + a raw 24hr presigned
  URL string + a `forward_prompt` workaround.
- Receipt picker `[:10]`; quote lists `[:5]/[:7]`.

Upgrade:
- **[UI]** Cleaner delivery (drop the raw-link noise; Telegram renders the file
  inline). Receipt/quote pickers paginated.
- **[EXTEND]** Interactive invoice builder: add item / edit qty / remove line /
  preview / then generate. Engine needs an invoice-DRAFT state + intent (today
  `generate_invoice` accepts an `items` list but nothing collects it in chat).
- **[EXTEND]** Real PDF quote (reuse invoice rendering) instead of a text blob.

---

## Sequencing (incremental, each shipped + live-tested before the next)
0. Finish validating the SALE fast-entry pilot (proves the pattern). ← in progress
1. CRM upgrade (self-contained, high visible payoff).
2. Catalog upgrade (browsable/searchable, kills the 9/10 caps).
3. Documents (interactive invoice builder + PDF quotes).

Principle for every step: keep WhatsApp working (guard on `tg:`), reuse the
engine's business logic, and only add engine capability where the UI genuinely
can't do it alone. Never fork the money/save logic.
