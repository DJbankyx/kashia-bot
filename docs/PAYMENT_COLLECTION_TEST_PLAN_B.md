# Payment-collection test plan — the runs that decide Build B

_Purpose: gather the real behaviour we need to choose HOW to build B (multi-line
pay-link + part-payment allocation) — or whether B-lite is even needed yet. Use
Paystack TEST mode + a test card. Nothing here changes code; it's a testing script._

Live app: https://9od53gtzih.execute-api.eu-west-1.amazonaws.com/dev/app
Bot: @KashiaFinance_Bot · region eu-west-1.
Paystack TEST card (standard): 4084 0840 8408 4081, any future expiry, CVV 408,
OTP 123456 (confirm current values in your Paystack test dashboard).

IMPORTANT baseline (already confirmed in code): a customer's debt is ONE number
(`debt_owed_to_me`), reduced as a lump sum by any payment. There is no per-product
or per-invoice balance today. Every test below is really asking: "does the single-
balance behaviour hurt in practice, and if so, which allocation rule fixes it?"

────────────────────────────────────────────────────────────────────────
PART 0 — Deploy + smoke (do first)
────────────────────────────────────────────────────────────────────────
0.1 Owner runs `./deploy.sh dev`. Reopen the app from the ☰ menu (cache-bust
    `?v=<build>` should load the new build).
0.2 Confirm the new bits render: Catalog tab shows "🧱 Add raw material"
    (mfg/hybrid); recipe material dropdown shows overheads as "⚡ … /sec (rate)"
    grouped under "Overheads"; the pay-link sheet shows the Product dropdown.
Record: did each appear? (If not → stale deploy / cache; note it.)

────────────────────────────────────────────────────────────────────────
PART 1 — Single-product pay-link (Option A, the common case) — SANITY
────────────────────────────────────────────────────────────────────────
Goal: confirm A works end-to-end and the honest-amount fix is correct.
1.1 Customer card → "💳 Get pay-link" → pick a product from the dropdown.
    → Does it prefill the amount (selling price) + description? (Y/N)
1.2 Create the link, pay it with the test card for the FULL amount.
1.3 Check your books:
    - The recorded SALE = the amount you REQUESTED (not the bigger Paystack
      total). Write down: requested ₦____, Paystack charged ₦____, recorded ₦____.
    - The "Payment received" message: does it mention the Paystack fee line?
1.4 Repeat 1.1 but choose "Something else" and type a free amount. Works? (Y/N)
DECISION THIS FEEDS: confirms A is solid so B is an ADD-ON, not a rewrite.

────────────────────────────────────────────────────────────────────────
PART 2 — The core B question: ONE customer, MULTIPLE unpaid products
────────────────────────────────────────────────────────────────────────
Set up a realistic debt: sell the SAME customer 3 different products ON CREDIT
(don't collect yet), e.g. Rice ₦5,000 + Oil ₦3,000 + Sugar ₦2,000 = ₦10,000 owed.
2.1 Confirm the customer card now shows they owe ₦10,000 (one number). (Y/N)
2.2 Create a pay-link for a PARTIAL amount, say ₦4,000, and pay it.
2.3 Observe + record:
    - New balance owed? (expect ₦6,000)
    - Could you tell the bot WHICH product(s) the ₦4,000 was for? (expect: no)
    - Did you WANT to? In this real scenario, would you (or your customer) care
      that it went "against rice" vs just "against the total"? (Y/N + why)
DECISION THIS FEEDS: **is B even needed?** If "just reduce the total" is fine for
your customers, B-lite is low priority. If you genuinely need to say which product
got paid, we need B (and possibly C).

────────────────────────────────────────────────────────────────────────
PART 3 — Which allocation RULE feels right (proportional vs oldest-first)
────────────────────────────────────────────────────────────────────────
Only matters if Part 2 said "yes, I care which product." Think through (or act out
with a real customer) how YOU would want a ₦4,000 part-payment on Rice/Oil/Sugar
split:
  (a) PROPORTIONAL — ₦4,000 spreads by size: 50% rice, 30% oil, 20% sugar
      → rice −₦2,000, oil −₦1,200, sugar −₦800.
  (b) OLDEST-FIRST (FIFO) — clear the earliest sale first: rice fully paid
      (−₦5,000? no, only ₦4,000) → rice −₦4,000, oil/sugar untouched.
  (c) CUSTOMER/OWNER PICKS — you choose "this ₦4,000 is for the oil + sugar."
3.1 Which of (a)/(b)/(c) matches how your merchants actually think? Note it.
3.2 Do different merchants want different ones? (If yes, B may need to ASK.)
DECISION THIS FEEDS: (a) or (b) = B-lite (buildable now, a fixed rule). (c) = needs
C (per-invoice ledger, the big build). This is THE decision that sets B's size.

────────────────────────────────────────────────────────────────────────
PART 4 — Multi-product on ONE link (do you bill a basket?)
────────────────────────────────────────────────────────────────────────
4.1 In real life, do you ever want to send ONE link that itemises several products
    (Rice + Oil + Sugar on one bill), or do you send separate links / one lump?
4.2 If one link: should the customer be able to pay only PART of that basket? Or is
    it always pay-the-whole-basket?
DECISION THIS FEEDS: whether B needs multi-LINE links at all, or just the
allocation logic on a single amount. (If baskets are always paid in full, B is much
smaller — no partial-allocation needed.)

────────────────────────────────────────────────────────────────────────
PART 5 — Refund path (since it's now live) — quick check
────────────────────────────────────────────────────────────────────────
5.1 Take a Paystack-paid sale from Part 1 and process a RETURN on it.
5.2 Is the "refund online" offer shown? Tap it. Does Paystack (test mode) accept
    the refund? (Needs refund permission on the account.) Record success/failure.
DECISION THIS FEEDS: not B directly, but confirms the returns↔refund loop before
B adds more money paths.

────────────────────────────────────────────────────────────────────────
WHAT TO SEND BACK (so we can decide B)
────────────────────────────────────────────────────────────────────────
1. Part 2.3: do you actually care which product a part-payment covers? (the
   go/no-go for B)
2. Part 3.1/3.2: which allocation rule — proportional, oldest-first, or "I pick"?
   (sets B-lite vs C)
3. Part 4: do you bill multi-product baskets on one link, and can they be
   part-paid? (sets whether B needs multi-line links)
4. Any surprises/bugs from Parts 0/1/5.

With those four answers I can size B precisely: either B-lite with a fixed rule
(buildable now, no data-model change) or "we need C first" — no guessing.
