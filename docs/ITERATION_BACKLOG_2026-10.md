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

## DONE — testing round (commit cc2357f)
- **"All time" period** added to the Dashboard AND Records period chips.
  `_date_range` gains an `all` branch (2000-01-01 → today, label "All time");
  `VALID_PERIODS` accepts `all` so endpoints don't downgrade it to this-month.
- **Site/location tag on ANY sale.** The "Mall/Location (optional)" field used to
  appear only for debt sales (credit/part/pay-link); now shown for every sale,
  sent on submit, and persisted server-side on paid sales too (debt sales still
  stamp it in the open-item branch). Hint reworded to be method-neutral.
- **Dated debt reminder.** The nightly "Quick Debt Reminder" + weekly overview
  are current outstanding snapshots (not period totals) but showed no date →
  added an "as of <date>" label so they read like the dated P&L summary.

## DONE — smart pay-link (Build B / Stage 4) · see docs/SMART_PAYLINK_BUILD_B_PLAN.md
A pay-link is now tied to a customer's owed balance:
- Minted FROM the contact's owed total (amount prefilled; "Owes ₦X now — this link
  is for that balance"). The link carries debt_contact (+ open_item_ids plumbing).
- DUPLICATE BLOCK: a 2nd link for a customer who already has a live pending link
  returns 409 needs_confirm → the app asks "a link for ₦X is already out — create
  another anyway?" (force retry on confirm). No more silent link-spam.
- RECOMPUTE after ANY payment: the webhook now settles through
  OpenItems.settle_open_items (reduces the specific unpaid rows + reconciles the
  lump once) instead of a bare lump settle, and cancels sibling pending links. So
  a part-payment leaves the correct remaining balance, and the NEXT link (prefilled
  from the live owed total) is automatically for just the remaining balance.
- Manual payment already recomputed + cancelled links (earlier work); the webhook
  now has parity.
Future: a per-item picker UI (choose which items a link covers) + a chat
mint-a-link command. Not needed for the core fix.

## (was) NEXT FEATURE: smart pay-link (Build B / Stage 4)
Owner's #1 complaint, confirmed again this round:
- Today a pay-link is a flat amount, NOT tied to a debt/open item. You can mint a
  link for ANY amount for a customer who already has one out, and the link
  doesn't know WHAT it settles.
- When a customer part-pays an issued link IN CASH, the link auto-cancels (good),
  but the bot does NOT recognise the remaining balance or reissue a link for just
  the balance. It should.
- BUILD: create a pay-link FROM a chosen owed balance / open item so it carries
  the exact amount + what it settles; recompute the balance after any payment
  (cash or online) and offer a link for the REMAINING balance; block/duplicate-warn
  if a live link already covers that balance.

## DONE — device-test batch (2026-10-02 screenshots)
- **Pay-link overcharge (money bug).** In debt mode the Product dropdown
  overwrote the amount with a selling price → could charge MORE than owed (Ade).
  Fixed: product picker hidden when the link settles a balance; client caps at
  owed; server clamps amount<=owed when debt_contact set.
- **Contact card: remaining unpaid per credit row** ("₦X left" / "paid"), not just
  the sale total. balance_owed + is_credit now returned per row.
- **Recipe units**: material unit was locked to the primary; now the editor offers
  the material's taught units (base + unit_defs) via a datalist. Engine already
  converts any recipe unit → base, so costs/deduction stay correct.
- **Quote in manufacturing** (produce-to-order), not just services/hybrid.
- **Mall/Location autocomplete**: /api/summary returns known site names; the
  record-form location datalist populates from them.
- **Industry-switch staleness**: switching industry while the app is open now
  re-applies labels (loadSummary detects the change) + the app re-fetches summary
  on focus. Still cleanest to reopen, but no longer stuck stale.
- **"Load failed" hardening**: apiPost maps a transport-level fetch reject to a
  clear "network hiccup — try again" message.

## DONE — owner follow-ups (2026-10-02)
- **Bill ANY sale.** Contact-card Bill/receipt picker now has an "Unpaid only /
  All items" toggle. "All" lists every sale (reprint a receipt for already-paid
  sales), "Unpaid only" keeps the open-credit view. _contact_detail returns
  transaction_id per row; multi-doc builds from any id.
- **Post-sale document prompt.** After recording a sale for a named customer, the
  app immediately offers Invoice / Receipt (Telegram popup) → builds the doc for
  that sale. Uses api/transaction's returned transaction_id.
- **Selling price suggested on a sale.** Picking a product with a saved price
  prefills "Amount received" = price × qty (recomputes on qty change), only while
  the field is empty / still the suggestion — a typed figure is never overwritten.

## DONE — feedback/support loop (2026-10)
- features/feedback.submit_feedback persists + forwards each message to an admin
  Telegram chat in real time (SSM /kashia/admin-chat-id; falls back to the
  submitter until set). Chat "Report a Problem" routes through it (and the latent
  _show_bug_report(phone_number) bug is fixed). Mini-app: POST /app/api/feedback +
  a "💬 Feedback / Contact us" footer button + sheet. New route → deploy required.
  ACTION FOR OWNER: set SSM /kashia/admin-chat-id to your Telegram chat id.
  FUTURE: owner→user reply path (not built; audit tool is read-only + TelegramClient
  .send_text is the primitive to build on).

## LAUNCH-IMPORTANT — not yet built (owner flagged 2026-10)
- **Signature on documents.** pdf_generator currently draws only a static
  signature LINE (no image). Let the owner upload a signature image — INDEPENDENT
  of the business logo (separate upload; a business wants both: logo top,
  signature bottom) — and render it on invoices/receipts/quotes/statements.
  Matters more now docs are prominent (post-sale prompt, bill-any-sale).
  → important before launch.
- **Richer, UNIVERSAL quote.** The current quote is thin (effectively one item +
  amount). A real quote carries MANY line items (desc / qty / rate / line total),
  a subtotal, often tax, a validity date, and notes/terms — and the exact fields
  vary by business, so it must be flexible/universal (owner has a sample quote
  image to design against). Build a multi-line quote builder in the mini-app
  (reuse the multi-line invoice renderer) rather than the single-amount sheet.
  → design against the owner's sample image.
- **Feedback / support loop (upgrade the existing "Report a Problem").** Today
  feedback is only logged via save_feedback + shows support@kashia.app — passive,
  one-way, easy to miss. Upgrade to a real intermediary:
  1. Forward every submission to an ADMIN Telegram chat in real time (reuse the
     Telegram client → fixed admin chat id) so the owner is pinged immediately.
  2. Acknowledge back to the user ("Thanks — we got it") and allow a one-way
     owner→user reply (two-way threading is a later nicety).
  3. Make it discoverable: a "💬 Feedback / Contact us" entry in Settings + the
     mini-app, not buried.
  → important before launch (early users WILL hit issues; this is how we catch
  them before silent churn).

## OPEN / WATCH
- **Friend's testing threw transient errors after an industry switch.** Partly
  addressed by the staleness fix above; if specific errors recur post-deploy,
  capture the exact message/screen so it can be traced.

## Answered
- "If I tap Pay, does it cancel the active link?" → NOW YES (auto-cancel pending
  links on manual payment, commit 6169ffc).
