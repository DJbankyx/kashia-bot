# Iteration backlog — owner device testing (2026-10)

From live use of the deployed build. Sorted; status tracked.

## DONE (commit 6169ffc)
- **Double pay-link (money-critical).** Create-link button stayed active after
  creating a link → a 2nd tap minted a 2nd Paystack link (2 refs, 2 "Payment
  received" msgs, 2 registry rows). FIXED: button locks + hides after creation;
  re-enabled only on reopen; record-flow path hides it too.
- **Manual payment vs stale link.** If the buyer pays another way (owner taps Pay /
  records a debt payment / settles a site), the customer's PENDING links are now
  auto-cancelled (db.cancel_pending_requests_for) so they can't be double-paid.

## DONE — naming / wording (commit 6544a12)
- **Renamed "Catalog" → "Inventory"** across mini-app (tab, trading TERM, nudge,
  search placeholder) + chat (home menu, mfg section, onboarding). Button ids
  unchanged.
- **Pay-link wording** audited — the sheet is already neutral ("...can pay
  online"); the webhook message is already sale-vs-repayment aware. No user-facing
  "debt-only" copy remained.

## DONE — UX (commit 6544a12)
- **Removed the "≡ Navigation" chat row.** Text replies now get "Anything else?"
  with ☰ Menu + 📊 Dashboard instead of the throwaway Navigation label.

## DONE — link marker (commit 81b11a1)
- **Debts with a live pay-link now warn the owner** ("🔗 A pay-link is out …") in
  BOTH the mini-app contact card and the chat debt person-card, so they don't
  double-collect. db.has_pending_link / pending_link_names. Recording payment
  auto-cancels the link (from the earlier money-safety fix).

## TODO — UX
- **Services: "Write a quote" on the customer card.** Add a quote action (services
  industry) — generate_invoice already supports kind="quote"; wire a button.

## DONE — richer Inventory dashboard (commit df41c65)
- Clearer header ('Products for sale' + N items total, 'Items in stock', 'Total
  inventory value'); VALUE SPLIT into Product value vs Materials value; product
  QUICK-VIEW on tap (type, category, stock, cost/rate, recipe-cost note, price,
  margin %, stock value, reorder) with an Edit button. Split row hides for trading.

## DONE — services quote (commit 0ca55fa)
- "📝 Write a quote" on the customer card (services/hybrid + billable contact) →
  POST /app/api/quote → quote PDF via generate_invoice(kind='quote'), delivered to
  chat. New route → deploy required.

## DONE — Inventory dashboard redesign v2 (commit 4f3c048)
- Owner said v1 was STILL ambiguous ("what does items-in-stock represent? the
  pointer links back to the same page"). Rebuilt so the stat cards are TAPPABLE
  and drill into a FILTERED list:
  - 'Products for sale ›' + 'Raw materials ›' count cards, and 'Product value ›'
    / 'Materials value ›' cards, each `catShow('product'|'raw')` → `catFilter`
    gates `renderCatalog` via `FILTER_MAP` (product=product; raw=raw+supply) with
    a 'show all ✕' clear chip. The '›' arrows now DRILL instead of looping.
  - Removed the ambiguous 'Items in stock' card.
  - Each row carries a stock tag (out / low / in stock), margin %, and a 'value'
    suffix; each type section shows its item count + total value at the top.
  - Raw-materials count card + value split only shown when the business keeps
    materials (mfg/hybrid, or any catalog that actually has some).

## TODO — remaining
- **Main dashboard** could still be richer (owner said "make that dashboard
  generally better") — scope TBD (charts, trends, quick actions).
- **Pay-link tied to a specific debt/open item (Build B / Stage 4).** Owner's #1
  complaint: can mint a link for ANY amount for a customer who already has a link
  out, and the link doesn't know WHAT it settles. Build: create a pay-link FROM a
  chosen owed balance / open item so it carries the amount + what it settles, and
  block / warn on duplicates. Highest-value next feature.
- **Location-name affordance "too casual."** The site/location name under
  Customers reads as throwaway; needs a clearer label + a one-line explainer so
  owners know what it does.

## \u26a0 DEPLOY OUTSTANDING — a large stack is pushed but undeployed
New API routes across recent batches: /app/api/delete-payment-request, /open-items,
/settle-open, /site-label, /quote, /multi-doc. Plus engine/JS: pay-link double-fix,
sub-accounts (open items + site) engine + UI, rename Catalog→Inventory, nav trim,
link marker, richer inventory dashboard v1 + v2 (tappable filtered list), services
quote, Stage-3 multi-item docs, Ade name-picker fix. Owner:
`cd ~/projects/kashia-bot && ./deploy.sh dev` then reopen the app from the ☰ menu.
(The Ade name-picker fix + this inventory redesign only take effect after deploy +
reopening the app from the ☰ menu button.)

## Answered
- "If I tap Pay, does it cancel the active link?" → NOW YES (auto-cancel pending
  links on manual payment, commit 6169ffc).
