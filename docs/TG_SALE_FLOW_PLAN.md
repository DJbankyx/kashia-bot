# Telegram Sale Flow — The "One Tidy Box" Plan (plain English)

_Created 2026-09-05. Written for humans, not coders. This is the agreed design
for how recording a SALE should feel on Telegram, across all 4 business types.
Once we're happy with SALE, the same pattern gets copied to Purchase and Expense._

---

## The big idea (in one sentence)
Instead of the bot sending a new message for every step, it shows **ONE box that
updates itself** as the user taps — from the first question all the way to
"Saved" — so it feels like filling a quick form in an app, not chatting with a bot.

## The two rules we agreed on
1. **Quantity ("how many?") is only asked for counted stock items** (bottles,
   bags, shoes). It is **skipped** for services / one-off jobs (a cleaning, a
   repair, a consultation) where "how many" makes no sense.
2. **One quick safety check before saving (Option B).** After the user says how
   they were paid, the same box shows a short summary with a **Save** button.
   One glance, one tap. After saving, an **Undo** button is right there in case
   something was wrong.

## What we are fixing (the current problems)
- Every step spawns a **new message** → the chat piles up and feels cluttered.
- It **never asks how many** — jumps straight to the money amount.
- It shows **paragraphs of text next to buttons** that say the same thing (noise).
- It uses **generic wording** ("Sale", "Sales & Income") even for a water factory,
  instead of each business's own language.

---

## The new SALE flow — step by step

The user starts a sale (taps "Record Sale" / "Sell Output" / "Record Job", or
types /sale). From here, **it is all ONE box that keeps changing**:

### Step 1 — What?
> 🧾 **Record sale**
> What did you sell?
> [ 50Cl Water ]  [ Orange Drink ]  [ 📝 Other ]

- Shows their catalog items as tappable buttons (paged if there are many).
- "Other" lets them type something not in the list (for those rare cases).

### Step 2 — How many?  (ONLY for counted stock items)
> 🧾 **50Cl Water**
> How many?
> [ 1 ] [ 2 ] [ 3 ] [ 5 ] [ 10 ] [ 🔢 Other number ]

- Quick number buttons + a "type another number" option.
- **Skipped entirely** if the item isn't counted stock (e.g. a service/job).

### Step 3 — Price (the CONSISTENCY rule)
**One rule, no mixing:**
- **If we asked "how many" (counted stock) → ask the PRICE EACH.** The bot
  multiplies for the total. Example: 2 × ₦20,000 each = ₦40,000.
- **If we did NOT ask "how many" (service / one-off job) → ask the TOTAL.**
  Example: ₦20,000.

> 🧾 **50Cl Water ×2**
> Price each?
> [ ₦500 ] [ ₦1k ] [ ₦2k ] [ ₦5k ] … [ ✏️ Type amount ]

- Quick amount buttons + "type the exact amount."
- The top line always shows the running summary so far (item + quantity).
- Why this rule: it's fully consistent (quantity ⇒ per-unit, no quantity ⇒
  total), the bot never has to guess a pricing style, and it lines up with how
  the app already tracks cost-per-unit for stock. It also covers "hours × rate"
  naturally — e.g. 4 hours × ₦5,000 each — without naming it or forcing it on
  services that don't work that way.

### Step 4 — How were you paid?
> 🧾 **50Cl Water ×2 · ₦40,000**
> How were you paid?
> [ 💵 Cash ]  [ 🏦 Transfer ]
> [ 💳 On Credit ]  [ 📝 Part payment ]

- Simple tappable choices. No paragraph explaining them — the buttons are clear.

### Step 5 — Quick check, then Save  (our Option B safety step)
> 🧾 **Confirm sale**
> 50Cl Water ×2
> ₦40,000 · Cash
> [ ✅ Save ]   [ ✏️ Edit ]   [ ❌ Cancel ]

- One glance to confirm. "Edit" lets them jump back and change something.
- Still the SAME box — nothing new piled up.

### Step 6 — Saved (the box becomes the receipt)
> ✅ **Sale saved**
> 50Cl Water ×2 · ₦40,000 · Cash
> Profit +₦14,000 · Stock left: 1,998
> [ ↩️ Undo ]   [ ➕ New sale ]   [ ☰ Menu ]

- The box turns into a clean receipt.
- **Undo** reverses it if it was a mistake.
- **New sale** starts a fresh one instantly (great for busy sellers).

That's the whole thing: **6 changes to ONE box**, not 6 separate messages.

---

## How it speaks each business's language (all 4 types)

Same tidy box, different words. The wording already exists in each business type;
we just make the box use it.

### 1. Trading & Retail (shops, markets)
- Button/opening: "Record sale" · "What did you sell?"
- Counted stock → **asks how many**.
- Person: "customer".

### 2. Manufacturing (e.g. the water factory)
- Opening: "Sell output" · "What finished product did you sell?"
- Sale list shows **finished products only** (never raw materials).
- Counted stock → **asks how many**.
- Person: "buyer".
- Note: "Record Production" stays its own separate flow (making goods is not a
  sale) — we are not changing that here.

### 3. Services (cleaners, repairers, consultants)
- Opening: "Record job/service" · "What service did you provide?"
- A service is **not counted stock → skips the "how many" step**.
- Amount can be a plain figure (we keep it simple for now).
- Person: "client".

### 4. Hybrid (sells goods AND does services)
- First asks a quick split: **[ 🛍️ Sold a product ]  [ 💼 Did a service ]**
  - Product path → behaves like Trading (asks how many).
  - Service path → behaves like Services (skips how many).
- We quietly tag which kind it was, so the dashboard's "product vs service"
  split keeps working correctly.
- Person: "customer/client".

---

## What stays exactly the same (important for safety)
- All the real money math — saving the sale, updating stock, profit/cost,
  credit/part-payment handling, customer records — **is untouched**. We only
  change how the questions are asked and shown. The saving engine is reused as-is.
- **WhatsApp is not touched at all.** This new box is Telegram-only. WhatsApp
  users keep the exact flow they have today.

---

## Live-test log (manufacturing, 50Cl Water)
_2026-09-05 — screenshots reviewed._ Flow works end-to-end on the OLD deployed
build: "Record Output Sale" wording ✓, per-unit math (200 × ₦40 = ₦8,000) ✓,
Cash/Transfer/Part payment/Credit payment options ✓, in-place confirm (Save/Edit/Cancel)
✓, "Sale saved! Profit ₦1,400, cost auto-from recipe" ✓.

Still to validate live (fixed in code, NOT yet deployed at time of screenshots):
- [ ] Quantity buttons must be LEARNED (e.g. 200) / wide fallback (1..1,000),
      NOT the old fixed 1–10 grid.
- [ ] Price buttons must be LEARNED (e.g. ₦40 each) / item's set price first,
      NOT the old generic ₦500..₦100k.
- Note: typed values (200, 8000, 40) show as the user's own chat bubbles — that
      is Telegram echoing user input and can't be hidden; the fix is better
      buttons so there's less need to type.
- Watch: a cancelled run can leave a half-state; confirm cancel fully resets.

## Payment methods — how each behaves (confirmed 2026-09-05)
- **Cash** — paid in full; tagged "cash"; no debt; no "who?" needed.
- **Transfer** — SAME behaviour as cash (paid in full); only the saved label
  differs (transfer/POS) so income source is visible later. Not functionally
  different from cash.
- **Credit** — nothing paid; full amount owed; asks who owes.
- **Part payment** — some paid now, balance owed; asks deposit amount, then who
  owes the balance. (Balance-owed bug fixed 2026-09-05: only the UNPAID balance
  is recorded as the debt, not the full amount.)

## CRM / "walk-in customer" notes (for the CRM stage — NOT now)
User sells water to random people daily; there is often NO specific customer.
BUT the CRM still needs names to be useful (top customers, history, insights),
so the rule is "offer always, force only for debt":
- **Always OFFER** a "who was this for?" step on every sale: tap a known/recent
  customer, type a new name, or tap **"🚶 Walk-in / Skip."**
- A **named** sale — even paid-in-full cash — FEEDS the CRM. This is how the CRM
  gets its data. Do NOT skip the offer just because it's a cash sale.
- A name is **REQUIRED** only for **credit / part payment** (a debt must be tied
  to someone). Everywhere else it's optional (Walk-in/Skip is always available).
- Consider a standing "Walk-in customer" bucket so unnamed sales still roll up
  somewhere useful without cluttering the real contact list.
- (Corrects an earlier note that said "don't ask on cash sales" — that would
  starve the CRM of data.)

## The order we'll build it
1. **Sale flow** (this plan) → I build it → you test it live on Telegram.
2. Once sale feels right → copy the same tidy-box pattern to **Purchase**
   (purchases also need "how many" and "who you bought from").
3. Then copy it to **Expense** (simplest — amount + what it was for).

## Decisions (agreed with user, 2026-09-05)
0. **Pricing rule (consistency, no mixing):** if the flow asked "how many"
   (counted stock) → ask **price each** and multiply for the total; if it did
   NOT ask "how many" (service/one-off job) → ask the **total**. This is the
   same rule for sales and purchases. Covers "hours × rate" naturally (qty=hours,
   price each=rate) without forcing it on services that don't price that way.
1. **Services amount = single total** (because services skip quantity, per the
   rule above). No separate "hours × rate" mode.
2. **Undo = only right after saving.** The Undo button lives on the receipt and
   works until the next action. We don't keep a general "delete last sale" here.
3. **Steps confirmed correct.** The 6-step flow matches how sellers work; we just
   make sure the wording fits each industry (sourced from each business type's
   existing terms — no generic "Sale"/"Sales & Income" leaking through).
