# Telegram Onboarding Redesign — Plan (plain English)

_Created 2026-09-06. For approval before building. Same spirit as the sale-flow
redesign: make onboarding feel like a quick app setup, tap-first, not a wall of
text — and set the user up so the new tidy-box flows work great from day one._

---

## What onboarding does today (current flow)
1. **Welcome** + "What's your business name?" (typed)
2. **Industry** — 4 tappable buttons (Trading / Manufacturing / Services / Hybrid) ✅ already boxed
3. **What you sell/make/offer?** (typed, natural language) — bot auto-extracts product names
4. **Manufacturing only:** "List your products" (typed, comma-separated)
5. **Done** — creates the account, seeds a basic catalog, shows next-step buttons
   (set prices / add recipe / add service, per industry)

It works, but it's WhatsApp-shaped: mostly typing, the product extraction is a
guess, and it doesn't capture the things the new flows lean on — **prices** and
**units** — so the user hits "no price set" and "no unit" later.

## What's wrong / what we're fixing
- **Too much free typing.** "Describe your business" then auto-guessing products
  is hit-or-miss (e.g. "Cars" became the only product). Tap-first is clearer.
- **No prices captured at setup.** So the first sale can't show profit; the user
  is nudged to "Set Prices" as a separate chore afterwards.
- **No units captured.** The whole units/conversions vision needs a base unit per
  item; onboarding is the natural place to ask "how do you count this?"
- **Wall-of-text steps.** Long messages instead of short, tappable cards.
- **Doesn't feel like the rest of the app now** (which is tidy, boxed, tap-first).

---

## KEY DECISION (agreed 2026-09-06): onboarding is LIGHT; the Catalog builds products

The big realization: when a user says "I sell cars, trucks and buses," they are
telling the bot **what line of business they're in** — NOT declaring 3 priced,
stocked products. A "car" isn't one SKU; the real stock units are Camry 2020
black, Accord 2019 silver, etc. Forcing prices/stock/units onto "cars" at
onboarding creates wrong single-SKU products.

So the rule:
- **Onboarding = quick identity setup + a GENERAL description of what they do.**
  NO prices, NO units, NO stock captured here.
- What they list is seeded as **category placeholders** (Cars, Trucks, Buses) —
  not priced products. _(Option B, user-chosen.)_
- **Immediately after onboarding, route them to the CATALOG** to build real
  products with proper depth (variants/models/colors, prices, units, stock).
  That depth is the Stage 2 "Shelf & Counter" Catalog job — not onboarding's.

This respects the user's intent, keeps setup ~30 seconds, and sidesteps the
"Cars = one priced item" trap entirely. (Supersedes the earlier idea of asking
price/unit per item during onboarding — that was premature.)

## The redesigned onboarding (final)

### Step 1 — Business name (typed)
> 👋 Welcome to Kashia! Let's get you set up in under a minute.
> **What's your business name?**
Warmer, shorter copy. Keep typed (a name must be typed).

### Step 2 — Industry (tap) ✅ keep
> **What kind of business?**
> [🛍️ Trading & Retail] [🏭 Manufacturing] [💼 Services] [🔄 Hybrid]

### Step 3 — What do you sell / make / offer? (general, comma-separated)
Per-industry wording (from the industry classes):
- Trading: "What do you sell? _(list the main things, separated by commas)_"
- Manufacturing: "What do you make?"
- Services: "What services do you offer?"
- Hybrid: "What do you sell and/or offer?"

User types comma-separated general terms ("Cars, Trucks, Buses") OR taps
**"➕ Add in Catalog later / Skip"**. We store the free-text description AND seed
each listed term as a **category placeholder** in the catalog (name only —
no price, no stock, no unit). No fragile prose auto-extraction beyond splitting
on commas; we take what they typed at face value as starting categories.

### Step 4 — Done → straight to the Catalog
> ✅ You're set, {Business name}! {industry}
> I've noted: {Cars, Trucks, Buses}
> Now let's set up your products properly — add models, prices, and stock.
> [📋 Set up Catalog] [⏭️ Do it later]

- **Set up Catalog** → opens the Catalog so they build real products/variants/
  prices/units now (Stage 2 territory).
- **Do it later** → home menu; they can record right away and flesh out the
  catalog anytime.

That's it — 3 quick steps, then a clean hand-off. No pricing/unit chore inside
onboarding.

## Industry differences (respected — the 4 are independent)
- Step 3 wording differs per industry (sell / make / offer), sourced from the
  industry classes.
- Seeded placeholders get the right default item_type: trading→product,
  manufacturing→finished_product, services→service, hybrid→product (services
  handled in Catalog). No prices/units either way.
- Manufacturing recipes, service rates, variants: all **Catalog-stage**, not
  onboarding.

## What stays the same / safe
- **WhatsApp untouched.** Telegram-specific tap bits gated on the `tg:` namespace;
  WhatsApp keeps today's onboarding.
- Reuse the existing `_complete_onboarding` save logic (account, industry,
  catalog seed) — we change the *collection UX* and make it seed **placeholders
  only** (no price/stock), then route to Catalog.
- Real product depth (prices, units, variants) is the **Catalog + Units &
  Conversions** stage — onboarding just hands off to it.

## Build order (approved — building now)
1. Simplify onboarding steps to: name → industry → general "what you sell/offer"
   (comma list, seed as placeholders, no price/unit) → done.
2. Replace the completion nudges with a clear **"Set up Catalog"** hand-off
   (+ "Do it later").
3. Ensure seeded items carry NO price/stock/unit (just name + item_type).
4. Verify + user live-tests onboarding on a fresh /reset for each industry.
