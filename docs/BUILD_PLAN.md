# Kashia Bot — Build Plan

_Last updated: 2026-08-27_

This is the working execution plan for getting Kashia to a soft launch and
beyond. It consolidates the roadmap, the code-review findings ("lapses"), and
the tech-debt backlog into one prioritized list with clear acceptance criteria.

Source of truth for high-level status stays in `.kiro/steering/roadmap.md`.
This document is the detailed, task-level plan Kiro works from.

---

## Guiding Priorities

1. **Stabilize** what testers touch first (Sprint 1).
2. **Unblock launch** (real WhatsApp number, templates, Paystack, legal).
3. **Harden** security and reliability (tech debt from code review).
4. **Grow** with post-launch features.

Correctness and reliability beat new features until launch is unblocked.

---

## Sprint 1 — Stabilize

Fix the rough edges testers hit first.

| # | Task | Status | Acceptance Criteria |
|---|------|--------|---------------------|
| 1 | Sale picker for manufacturing — finished products only | ✅ Done (2026-08-27) | Raw materials, overhead, and consumables never appear in the Sell Output picker; only finished/sellable items show. |
| 2 | Unit conversion display | ✅ Done (2026-08-27) | Quantity prompt shows the tracked unit; picker rows show unit; confirmation shows "3 bags = 150 kg". |
| 3 | Edit recipe flow | ✅ Done (2026-08-27) | User can edit qty/cost of an existing recipe material via Edit Quantity / Edit Cost. |
| 4 | Hybrid industry sync | ✅ Done (2026-08-27) | Quotes wired into hybrid menu (deposits + supplies already worked via generic paths). Also fixed dashboard product/service split with a persisted `sale_kind` marker. |
| 5 | Edit recurring services | ✅ Done (2026-08-27) | Edit/Manage entry lists all services; edit client/service/amount/frequency; frequency change recomputes next_due. |
| 6 | Sale picker for trading — "Other/Not Listed" always shows | ✅ Done (2026-08-27) | Picker rows hard-capped at 9 before appending "Other", so it always shows and never exceeds WhatsApp's 10-row limit. |

### Notes on completed items
- **#1 (done):** `catalog.get_product_list_for_recording` now calls
  `ensure_item_types()` for manufacturing/hybrid before filtering and uses an
  allowlist of sellable types (`finished_product`, `product`, `service`, `""`).
  This closes the gap where recipe-only raw materials with an unset `item_type`
  leaked into the sale picker.

---

## Launch Prep — Blocking a Real Launch

| Task | Status | Notes |
|------|--------|-------|
| Real WhatsApp Business number + Meta verification | ⬜ Blocked | Adding +234 901 640 3500 fails with "Unexpected null value for wabaID" — Meta platform issue. On test number for now. Revisit / open Meta support ticket. |
| WhatsApp message templates | ⬜ To do | Meta requires approval for outbound reports/reminders. Draft + submit templates. |
| Paystack end-to-end test | ⬜ To do | user pays → webhook → tier upgrade. Verify full path in a staging run. |
| Monthly counter reset | ⬜ To do | Scheduled Lambda calling `reset_monthly_counters`. |
| Privacy policy + Terms of Service | ⬜ To do | Required by Meta for Business API. |
| Landing page | ⬜ To do | One-pager with WhatsApp link. |
| Onboarding final UX pass | ⬜ To do | Review flow end-to-end for new users. |

---

## Security & Reliability Hardening (from code review)

These are the "lapses" surfaced in review. Ordered by risk.

| # | Issue | Risk | Fix | Status |
|---|-------|------|-----|--------|
| S1 | PIN stored as unsalted SHA-256 | High | ✅ Done (2026-08-27) — salted PBKDF2 (200k iters) in `utils/pin_security.py`; legacy SHA-256 verified + auto-upgraded on next successful entry (no lockout). | ✅ Done |
| S2 | No retry/backoff on WhatsApp API sends | Med | ✅ Done (2026-08-27) — `_send` retries 429/5xx/timeouts/connection errors up to 3x with exponential backoff (honors Retry-After); non-transient 4xx not retried. | ✅ Done |
| S3 | Session race condition on rapid concurrent messages | Med | Use DynamoDB conditional writes / optimistic locking on session updates. | ⬜ To do |
| S4 | `check_can_generate_pdf` has `TODO: re-enable tier check after beta` | Low | Re-enable the tier gate when beta ends. | ⬜ Parked (beta) |
| S5 | Router is 900+ lines | Low | Split `_route_button` into a `ButtonDispatcher`. | ⬜ To do |
| S6 | No CloudWatch alarms / DLQ on webhook Lambda | Med | Add a DLQ and basic alarms (errors, throttles, duration) in `template.yaml`. | ⬜ To do |

---

## Repo Hygiene

| Task | Status | Notes |
|------|--------|-------|
| Remove dead monolith snapshots | ✅ Done (2026-08-27) | Deleted `conversation_engine_current.py`, `conversation_engine_latest.py`, `database_latest.py`. Confirmed unreferenced. |
| Decide fate of root maintenance scripts | ⬜ Pending decision | `check_syntax.py`, `cleanup_contacts.py`, `fix_conversion_match.py`, `fix_qty_display.py`, `quick_reset.py`, `reset_all_testers.py`, `reset_both_users.py`, `reset_user.py`. Suggest moving to a `scripts/` folder rather than deleting, since some are active. |

---

## Industry Quality Findings (from services/hybrid review)

Findings from a critical review of the Services and Hybrid industries. Two were
fixed on 2026-08-27; the rest are tracked here.

| # | Finding | Status | Notes |
|---|---------|--------|-------|
| Q1 | `expense_class` relied on a manual tap; unclassified expenses dumped into overhead, making gross profit misleading | ✅ Done (2026-08-27) | Expenses now get a default `expense_class` derived from category (COGS category → direct, else indirect) written at save time; the classify prompt still overrides. |
| Q2 | Vendor/client blocklist duplicated & inconsistent across 6+ places | ✅ Done (2026-08-27) | Centralized into `utils/parser.py` (`BAD_VENDOR_NAMES`, `is_bad_vendor`, `clean_vendor`); all copies replaced. |
| Q3 | Recurring "due soon" shows in-app but reminders don't actually send | ⬜ To do | Needs the scheduled Lambda + approved WhatsApp templates (see Launch Prep). In-app display works; no outbound notification yet. |
| Q4 | No service duration / job completion tracking | ⬜ To do (post-launch) | A job is a one-shot sale. "In progress vs completed" + time tracking would make services first-class. Product gap, not a bug. |
| Q5 | Hybrid duplicates trading + services logic instead of composing them | ⬜ To do (pre-launch refactor) | Hybrid re-implements dashboards/menus, so it lags the specialized industries and is the buggiest. Recommend a focused pass where hybrid delegates service-side behavior to the services logic. The `sale_kind` fix patched a symptom; the architecture remains duplicative. |

---

## Post-Launch Queue (parked)

- Multi-currency (Phase 1 — manual exchange rate)
- Returns / refunds
- Bulk stock import ("stock: shoes 50, bags 30")
- Smart reports (profit per product, margin ranking)
- Proactive low-stock alerts (scheduled Lambda)
- Customer loyalty insights
- i18n infrastructure (English, Pidgin, French)
- Account deletion flow ("Delete my data" — compliance)
- Voice note transcription (WhatsApp audio → text)

---

## Suggested Order of Attack

1. Finish Sprint 1 (#2 → #6) — quick wins testers feel immediately.
2. Ship S1 (PIN hashing) and S2 (WhatsApp retry) — cheap, high-value hardening.
3. Work Launch Prep in parallel: draft templates + legal pages while chasing
   the Meta `wabaID` blocker.
4. Add S6 (DLQ + alarms) before onboarding real users.
5. Tackle S3/S5 refactors once launch is stable.
