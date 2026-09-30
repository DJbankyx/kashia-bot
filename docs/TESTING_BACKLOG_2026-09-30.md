# Testing backlog — owner round (2026-09-30, with screenshots)

Consolidated from the owner's device testing (today + Mon/yesterday notes). Sorted
by type. NOTHING here is built yet — this is the punch list to work through.

## A. BUGS (fix)
1. **Debt-settling payment DISPLAYS like a sale.** (Muyideen) The MATH is correct —
   his debt went ₦14,416 → ₦9,416 after a ₦5,000 pay-link (settle worked), and the
   receipt correctly reads "Amount Paid / Balance Remaining". BUT in the customer
   card transaction list the ₦5,000 shows with the 💰 sale icon and reads like a
   fresh sale. FIX = LABEL/ICON only: show a debt-settling collection as a
   "Payment received"/repayment row (distinct icon + label), not blended with sales.
   Do NOT change the settle math. (Also verify it isn't ALSO adding to revenue —
   the webhook still calls save_transaction type='sale' with settled_receivable>0;
   confirm accounting excludes it via _is_debt_settlement, else it double-counts P&L.)
2. **"Days" missing in the report.** Regression — the debtor/report day count isn't
   showing at all now (worse than the earlier "1 days" plural). Find in
   scheduled_reports / debt / reports.
3. **Pay-link amount didn't prefill** (description did). Likely the picked product
   had no sale_price set (prefill reads sale_price, which was 0). NOT a code bug if
   so — but improve: if no price, hint it; consider prefilling from last sale price.

## B. UX / polish (owner-flagged)
4. **Keyboard too persistent (Mini App).** Tapping empty screen (no input) should
   dismiss the keyboard. Add blur-on-tap-outside so it leaves when not needed.
5. **Add raw material — no unit TRAINING + no "this is the primary unit" clarity.**
   The add-raw sheet has a bare "Stock unit" text box; it doesn't teach conversions
   and doesn't say it's the PRIMARY/base unit. Need: primary-unit labelling + a way
   to add trained/custom units (conversions), and a dropdown of primary + trained
   units in the raw-material's relevant places. UPDATE THE EDIT SIDE too.
6. **Add raw material — no material/overhead split.** Like the recipe editor splits
   raw material vs overhead, the "Add raw material" flow (chat + mini app) should let
   you pick material vs overhead up front.
7. **Time as well as date.** In several places date alone isn't enough — time matters
   too (Records list, payment link). Add time where it aids ordering/clarity.

## C. FEATURES (design/build)
8. **Generate receipt/invoice from the Mini App.** Today there's NO way to make a
   receipt/invoice in the mini app — not on a sale, not from Records. Owner had to use
   chat "Bill a Customer". Add: from a Records row (and/or a sale), generate a
   receipt or invoice for those item(s). (Chat "Bill a Customer" already does the
   multi-item version — mirror it in the app.)
9. **Mini-app debtors/creditors are dead-ends.** "Owed to you / You owe" cards show a
   cold number with no link. Tapping should open an actual list of WHO owes what /
   who you owe (chat has this). Make them tappable into a debtor/creditor list.
10. **"Pay online via a link" as a payment OPTION at record time.** Owner's idea (a
    good one): when recording a transaction, alongside cash / credit / part-pay, add
    "request payment via link" — and possibly on the invoice too. This makes the
    system KNOW whether an incoming pay-link payment is a fresh SALE or a DEBT
    SETTLEMENT (because the owner chose up front), removing the gu#1 guessing. Ties
    into the open-items/sub-accounts model.
11. **Payment-link lifecycle.** Owner asked: does a pay-link live forever? expire?
    can the owner manually delete/hide a link from the Records/links view once
    they're done with it? DECIDE: add an expiry and/or a manual dismiss/delete on the
    Payment-links view. (Cancel exists for pending; need "remove from view" for
    settled ones + maybe TTL.)

## Priority (proposed)
Quick wins first: #1 (label), #2 (days), #4 (keyboard) — small, high annoyance.
Then #8 + #9 (mini-app receipts + tappable debtor list) — real everyday value.
Then #5/#6/#7 (raw-material units + splits + time) — clarity of the mfg model.
Then #10 + #11 — fold into the open-items/sub-accounts build (they belong together).

## Notes
- Walk-in/skip customer registry = NON-ISSUE (owner confirmed it shows in Customers).
- Refund test = SKIPPED (owner unclear; it's only for online-paid returns; low
  urgency, needs Paystack refund permission). Leave as-is.
- Set Recipe = behaves well now. ✅
- The chat "Bill a Customer" multi-item invoice/receipt works well (screenshots) —
  it's the MODEL to mirror into the mini app for #8.
