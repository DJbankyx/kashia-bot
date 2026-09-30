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

## TODO — remaining
- **Main dashboard** could still be richer (owner said "make that dashboard
  generally better") — scope TBD (charts, trends, quick actions).

## \u26a0 DEPLOY OUTSTANDING — a large stack is pushed but undeployed
New API routes across recent batches: /app/api/delete-payment-request, /open-items,
/settle-open, /site-label, /quote. Plus engine/JS: pay-link double-fix, sub-accounts
(open items + site) engine + UI, rename Catalog→Inventory, nav trim, link marker,
richer inventory dashboard. Owner: `cd ~/projects/kashia-bot && ./deploy.sh dev`
then reopen the app from the ☰ menu.

## TODO — Products & materials dashboard (make it richer + clearer)
- The three stats (Products / Total stock / Stock value) are unclear. Wanted:
  - Split **Product value** vs **Raw-materials value** vs a **Total inventory value**.
  - Clicking a product opens a **quick-view** with details (stock, cost, price,
    margin, unit, recipe cost if any). (Row tap already opens the edit sheet —
    make it a richer read-first view.)
  - Generally make the Products dashboard better/clearer.
- Dashboard (main) could also be richer — owner said "make that dashboard generally
  better" (scope TBD).

## Answered
- "If I tap Pay, does it cancel the active link?" → NOW YES (auto-cancel pending
  links on manual payment, commit 6169ffc).
