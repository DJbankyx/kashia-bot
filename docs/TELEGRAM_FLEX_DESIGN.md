# Telegram "Full Flex" — Design Note

_Created 2026-09-03. Pilot: sale-recording fast-entry._

## Goal
Make Telegram flows feel like an app: fewer steps, tappable amount presets,
inline toggles, one message that edits itself — instead of the WhatsApp-shaped
"type the amount / pick from a 10-row list / one thing per message" flow.

## Non-negotiable constraint
Business logic (validation, catalog linkage, saving, stock deduction, CRM,
landing-cost/margin) lives in the **shared engine** and must stay there. WhatsApp
must be **completely unchanged**. Only *presentation and step-collapsing* are
Telegram-specific.

## The seam (where Telegram flows intercept)
`Router._start_guided_recording()` (core/router.py) is the entry point for the
`record_sale` / `record_purchase` button. It already branches (catalog vs
free-text). We add ONE more branch at the top:

- **If the user is on Telegram** (detected by the `tg:` prefix on their user id —
  the same namespace `resolve_client` uses, so NO new signature threading needed)
  **and** `tx_type` is sale/purchase → hand off to the new Telegram fast-entry
  flow.
- **Otherwise** → existing behavior, byte-for-byte. WhatsApp never sees the new
  path.

Detection helper: reuse `platform_for_user(user_id)` from `services.messaging_client`.

## Reuse vs replace
REUSE (unchanged — the whole point):
- `TransactionHandler._build_confirmation(tx_data, has_credit)` — we converge here
  so the confirm → payment-method → `_save_transaction` chain (with ALL side
  effects: stock, CRM, landing cost, credit/deposit) runs exactly as today.
- `parse_amount`, `clean_vendor`, catalog list builders
  (`get_product_list_for_recording` etc.), `CatalogHandler`.

REPLACE (Telegram presentation only):
- The product picker → a paginated inline grid (Phase 4 `page_keyboard`).
- "Type the amount" → an **amount-preset grid** (₦500/1k/2k/5k/10k/20k + Custom).
- Payment method → an inline **cash/credit toggle** (can be folded into the
  same screen).
- Multi-message steps → **one message edited in place** (Phase 4
  `edit_message_text` + `send_and_get_id`).

## Flow (sale fast-entry) — target: 2–3 taps
1. `record_sale` → screen: "What did you sell?" + product grid (paged) + "Other".
2. Tap product → screen edits to: amount presets + "Custom" (+ back).
   - "Custom" → prompt for typed amount (one text step).
3. Tap/enter amount → assemble `tx_data` and call `_build_confirmation()`.
   From there the existing engine confirm/payment/save runs unchanged.

(We keep the confirmation card for now — money accuracy. A later "instant"
variant could skip straight to `_save_transaction` for trusted quick entries.)

## State & data
- New session state (e.g. `TG_FASTREC`) OR reuse the existing `cat_rec_*` context
  shape so `_catrec_finalize`/`_build_confirmation` can consume it. Decision:
  assemble a `tx_data` dict with the known keys (amount, type, description,
  category="Sales & Income" default, vendor, quantity, unit_cost, catalog_* if a
  catalog product was picked) and call `_build_confirmation()` directly — least
  coupling to the catalog step machine.
- Fast-entry screens live on a single message id (stashed like `__tg_page`) so we
  edit in place. Reuse the Phase-4 pattern.

## Rollout
Pilot = SALE only. Prove end-to-end live, then apply the identical pattern to
purchase and expense. Everything guarded by the `tg:` check; WhatsApp untouched.
