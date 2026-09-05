# Units & Conversions — "Master Unit" Plan (plain English)

_Created 2026-09-05. Captured from user's real water-business example. This is a
BIG structural feature. NOT being built yet — it's entangled with the Catalog
redesign, the Recipe flow, and Onboarding (where units get registered). Written
down now so we build it right when we get to it._

## The problem (in the user's words, paraphrased)
The bot isn't fully "conversant" with units. A business buys, produces, and sells
the SAME item in DIFFERENT units, and the bot should convert between them
seamlessly at every level, always showing a chosen **master unit**.

## Real example (water business)
- Buys raw material (power) in **KWh**; the recipe records it in **Wh**;
  production should use **KWh (the master unit)** → bot converts Wh ↔ KWh.
- Produces water in **units**. Registered conversions:
  - 20 units = 1 bag
  - 400 bags = 1 truck
- The user wants to enter production OR a sale in **any registered unit**, and the
  bot recognizes it at every level. e.g. "sold 20 bags" = 20 × 20 = **400 pieces**,
  and stock is deducted by 400 pieces.
- There is ONE **master unit** per item; the bot stores and displays everything in
  that master unit, regardless of which unit was typed.

## What this means (the shape of the feature)
- Each catalog item has a **master (base) unit** + a set of **conversion factors**
  linking other units to the base (a small "unit graph"): e.g. base=piece,
  1 bag = 20 pieces, 1 truck = 400 bags = 8,000 pieces.
- The SAME conversions apply consistently at **all levels**:
  1. **Buying inputs** — record a purchase in any unit; store in base.
  2. **Production / recipe** — inputs may be recorded in one unit, produced in
     another; convert to base for stock + cost math.
  3. **Selling output** — sell in any unit; convert to base to deduct stock and
     compute cost/profit per base unit.
- Reports, stock levels, and cost-per-unit are shown in the **master unit**
  (with the entered unit visible too, e.g. "20 bags = 400 pieces").

## Current state (what exists in code today)
- Catalog already stores `primary_unit` and `conversions`, and the DB has
  `convert_to_base()` / `convert_from_base()` helpers. So the plumbing partly
  exists — but it is NOT wired through every flow, and NOT into the new Telegram
  tidy-box flow (which currently asks a plain number with no unit choice).

## Known design notes / decisions from the user
- **Master unit** is the display/output unit. (Confirmed.)
- **All levels matter and must interconnect** — buy, produce, sell — with the bot
  converting interchangeably. (Confirmed.)
- The current design is **WhatsApp-shaped**. On Telegram we'll likely change:
  - how the unit is entered (tap a registered unit vs type it),
  - how the recipe-registration flow asks for units (user wants this changed),
  - how the menu pops up.
- On the SELL step: whether to also let the user pick the UNIT (pieces/bags/truck)
  and have the bot convert — user asked "what's smartest?" → PROPOSAL below.

## Proposal for "what's smartest" on the sell/enter-quantity step (for later)
When an item has registered units, the quantity step shows a small unit toggle,
e.g. after "How many?" offer the registered units as chips: [pieces] [bags]
[truck]. User taps a unit, types/taps the count, bot converts to base. If the
item has only a base unit (no conversions), skip the toggle entirely (keep it
simple). This keeps easy items easy and only shows complexity where it exists.
_This is a proposal to refine when we build; not final._

## Sequencing (agreed)
- NOT now. Build order stays: finish SALE → PURCHASE → EXPENSE → CRM.
- Conversions is its own stage, built together with (or right around) the Catalog
  redesign + Onboarding redesign, since that's where units are registered.
- When built: reuse the existing convert_to_base/from_base logic; wire it through
  buy/produce/sell; keep WhatsApp working; rethink the Telegram entry UX.
