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

## TODO — naming / wording
- **Rename "Catalog" → "Inventory Management"** (or similar). Users find "Catalog"
  ambiguous. Touches: bottom-tab label, chat "Catalog — Products & Materials" menu,
  the catalog-nudge copy ("Open catalog" / "Finish setting up your catalog"), any
  "catalog" in prompts. Keep it consistent across mini-app + chat.
- **Pay-link sheet / message wording still says "debt".** The webhook book_line is
  already sale-vs-repayment aware; audit the pay-link SHEET subtitles + any
  remaining "debt"-only copy so a fresh-sale link doesn't read as debt.

## TODO — UX
- **Persistent "Navigation" label before Menu (chat).** The "≡ Navigation" row is
  too sticky/repetitive after actions — trim or stop re-sending it every message.
- **Services: "Write a quote" on the customer card.** Add a quote action (services
  industry) — generate_invoice already supports kind="quote"; wire a button.

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
