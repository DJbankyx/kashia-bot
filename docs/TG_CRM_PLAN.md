# Telegram CRM Redesign — Plan (plain English)

_Created 2026-09-06. For approval before building. Same pattern as sale/onboarding._

CRM = your customers and suppliers: who they are, what they've bought, who owes
you, and nudging debtors to pay. Today it works but it's WhatsApp-shaped (long
`━━━` text blobs, a 10-contact cap with no paging, non-tappable reminders) and it
doesn't yet reflect the walk-in / customer-capture decisions we agreed.

---

## What CRM does today
- **All Contacts** — tappable list, but capped at 10 (no pagination, no search).
- **Add Contact** — 3-step typed form (name → phone → type).
- **Top Customers / Top Suppliers** — text-blob rankings.
- **Debt Reminders** — a text list of debtors; not tappable per person here (you
  have to go to All Contacts or the Debt board to actually send one).
- **Customer Insights** — text-blob analytics (best customer, most frequent,
  inactive).
- **Contact Profile** — tap a contact → full text-blob (financials, timeline,
  debts) + a couple of action buttons.

## What's wrong / what we're fixing
1. **Mixed list, no filtering by type.** Customers and suppliers are lumped
   together. For a purchase you want suppliers; for a sale, customers.
2. **10-contact cap, no pagination or search.** Businesses with many contacts
   can't reach the rest except by typing a name.
3. **Reminders aren't tappable where you'd expect.** The "Debt Reminders" screen
   is a text list; you can't tap a debtor to remind them from there.
4. **Everything is a text blob**, not the clean boxed/tappable style the rest of
   the bot now uses.
5. **Walk-in / customer-capture rule not applied** (see below).
6. **Telegram reminder reality:** the bot can send a reminder to a saved *phone*
   via WhatsApp, but from Telegram it can't message an arbitrary phone. So on
   Telegram, "Send Reminder" mostly produces a clean copy-paste message the user
   forwards. We should make that copy-paste path tidy and obvious (not treat it
   as a failure).

## Decisions already agreed (carry into CRM)
- **Customer capture rule:** every sale OFFERS a "who?" step (tap known/recent +
  type + Walk-in/Skip). A named sale — even cash — FEEDS the CRM. A name is
  REQUIRED only for credit/part payment (a debt needs an owner). _(Already built
  into the tidy-box sale/purchase flow.)_
- **Walk-in bucket:** unnamed credit/part debts attach to a "Walk-in" contact so
  the debt still has an owner without forcing a fake name.
- **Who? list should favour the right type:** suppliers first on a purchase,
  customers first on a sale. _(This is the "mixed list" fix — do it here.)_

---

## The redesigned CRM (proposed, Telegram tap-first)

### CRM home (tidy menu)
> 👥 **Customers & Suppliers**
> [👤 Customers] [🏪 Suppliers]
> [➕ Add Contact] [⏰ Who Owes Me]
> [📊 Insights] [☰ Menu]

Clean grid, plain words (no "CRM" jargon in the body).

### Browse Customers / Suppliers (paginated, filtered)
- Tap "Customers" → a **paginated** list of just customers (reuse the Prev/Next
  pager we already use elsewhere). Same for Suppliers.
- Each row: name + a short stat (total, or "owes ₦X"). Tap → profile card.
- Kills the 10-cap and the mixed list.

### Contact card (boxed, edited in place)
Tap a contact → a rich card (one message):
- Name, type, phone (if any)
- Financials (bought from you / you bought from them, avg, count)
- Debt (owes you / you owe) — highlighted
- Last seen / frequency
- **Action buttons:** [⏰ Remind] (if they owe) · [💰 Record sale] · [✏️ Edit] ·
  [📄 Statement] · [← Back]
- No 3-button cap on Telegram (grid the actions).

### Who Owes Me (actionable debt board)
- A **tappable** list of debtors (paginated), each row "Name — owes ₦X".
- Tap a debtor → remind directly (send to saved phone via WhatsApp if present,
  else a clean copy-paste message on Telegram). No dead-end text list.

### Add Contact
- Keep the simple flow, but make type selection tappable (already is).
- Optionally: reachable from a contact "Edit" too.

### Edit Contact (NEW — small engine addition)
- Let the user fix a name/phone/type or add a note. Today there's no edit path.
- Keep it light: tap a field → type the new value.

### Insights
- Keep the analytics, but present as a tidy card (trim the `━━━` walls).

---

## Industry wording (respect the 4 industries)
- Trading/Manufacturing/Hybrid: "Customers" + "Suppliers".
- Services: "Clients" + "Suppliers" (services already says Clients).
- Manufacturing: buyers are "Customers" (fine) — keep supplier/customer split.
Wording pulled from the industry layer where it already differs.

## What stays safe
- **WhatsApp untouched** — Telegram-specific tap/paginate/grid gated on `tg:`;
  WhatsApp keeps its native list rendering.
- Reuse existing DB: `get_contacts`, `get_top_contacts`, `get_all_debtors`,
  `get_contact_analytics`, `record_debt`, `save_contact`, the `__SEND_REMINDER__`
  path. We change presentation + add a contact-edit path + search/filter; we do
  NOT fork the debt/analytics math.

## Decisions (agreed 2026-09-06)
1. **Search: YES.** In-chat "type a name to find them" — faster navigation.
2. **Edit contact: YES.** Contacts editable (name / phone / type / note).
3. **Reminders: copy-paste on Telegram is accepted** as the default (bot can't
   auto-message an arbitrary phone from Telegram; auto-send only for WhatsApp
   numbers). Make the copy-paste message clean and forward-ready.
4. **Statement per contact: LATER (Documents stage).** It's a mini account history
   for one contact (their buys/payments/running balance) — document-flavored, so
   it belongs with Documents, not CRM.

## Recommended extras (my analysis — pending user scope decision)
- **ADD — Call / WhatsApp link on a contact card** (when a phone is saved): a
  tappable link to call or open a WhatsApp chat. Handy for chasing debtors.
- **ADD — "Record sale to this contact"** from the card: pre-fills them into the
  tidy-box sale flow (ties CRM back into recording).
- **IMPROVE/CONSOLIDATE — collapse overlapping screens:** today Top Customers,
  Top Suppliers, and Insights are three near-duplicate text-blob screens. Fold
  into: browse Customers (rankable), browse Suppliers (rankable), one clean
  Insights card. Fewer menu items, less redundancy.
- **IMPROVE — lead the contact card with DEBT** (owes me / I owe) at the top, then
  financial history below — that's what a business owner checks first.
- **NOT NOW — loyalty/segmentation/tags** (VIP etc.): on the post-launch queue;
  would bloat this stage. Keep CRM focused.

## Build order (once approved)
1. Filtered + paginated Customers / Suppliers browse (kills 10-cap & mixing).
2. Boxed contact card with grided actions.
3. Actionable "Who Owes Me" (tap-to-remind), tidy copy-paste on Telegram.
4. (If approved) contact search + edit-contact path.
5. Verify + you live-test per industry.
