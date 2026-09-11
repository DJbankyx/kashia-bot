# Subscription / Payment Lifecycle — Plan Doc

_Drafted: 2026-09-11. Status: FOR OWNER REVIEW (no code yet)._

Kashia's revenue engine. Today upgrades WORK but never end — this plan adds the
missing lifecycle: a subscription that starts, warns before it lapses, expires,
downgrades, and can be renewed, plus multi-period pricing and a nudge to
subscribe.

---

## Where we are today (grounded in the code)
- **3 tiers** in `tier_manager.TIERS`: Free (₦0), Basic (₦3,000/mo), Pro
  (₦6,000/mo), each with limits (transactions/exports/invoices, PDF, CRM
  insights). `tier` is stored on the user record.
- **Payment = one-time Paystack charge.** `paystack.initialize_transaction`
  (`/transaction/initialize`) → checkout link → `charge.success` webhook →
  `tier_manager.upgrade_user(plan)` sets `tier` + `tier_upgraded_at` and zeroes
  usage counters. It is NOT a Paystack recurring subscription; no card is stored
  for auto-recharge; only `charge.success` is handled.
- **A paid tier is PERMANENT.** There is NO `valid_until` / `subscription_ends`
  / `next_charge` field anywhere. `tier_upgraded_at` is written but never read.
  Once Basic/Pro is set, it stays forever. **This is the core gap.**
- **Limits read `tier` live** (`check_can_record` enforced in main.py;
  `check_can_export` / `check_can_invoice` defined). So the moment `tier` becomes
  'free' again, free limits AUTO-reapply — no per-feature rework needed.
- **Solid infrastructure to reuse:**
  - Scheduled-Lambda pattern (`MonthlyResetFunction`: cron + shared
    `ScheduledReportsDLQ` + CloudWatch alarm).
  - Cross-platform send (`resolve_client` / `platform_for_user` / `send_text`) —
    the same path scheduled reports and the Paystack webhook already use. Alerts
    need NO new plumbing.
  - `MessagingClient` supports buttons, so a "Renew now" tap is easy.
- **Monthly is the ONLY period** across PLANS/TIERS/UI.

---

## Guiding principles (consistent with the rest of Kashia)
1. **Reuse the shared engine + patterns** — one scheduled alert Lambda modeled on
   MonthlyResetFunction; deliver via resolve_client/send_text; no forked logic.
2. **Backward-compatible data** — existing paid users have no expiry date. On
   first touch, treat a missing `subscription_ends` as "grandfathered / no
   expiry" and set one on their NEXT renewal, OR seed one from `tier_upgraded_at`
   + period (owner decides — see Decisions). Never accidentally expire a paying
   user.
3. **Never lose money data on downgrade** — downgrading to Free re-applies limits
   but NEVER deletes transactions/catalog. Free just caps NEW activity.
4. **Money-safe & idempotent** — a payment must upgrade exactly once (Paystack
   reference already unique); the expiry job must be safe to run repeatedly (only
   acts on users whose date has actually passed).
5. **Telegram-first, WhatsApp intact** — alerts go to both via the existing
   client resolver; no template-wall issue on Telegram.

---

## The lifecycle (what we're adding)

```
 SUBSCRIBE            RENEWAL WINDOW                 LAPSE
 ┌────────┐   active   ┌───────────────┐   expired   ┌──────┐
 │ pay →  │──────────► │ T-7 / T-3 / T-1│──────────►  │ free │
 │ Basic/ │            │  "renew soon"  │  grace?     │ (cap)│
 │ Pro    │            │  reminders     │             │      │
 └────────┘            └───────────────┘             └──────┘
      ▲                                                   │
      └──────────────── "reminder to subscribe/renew" ◄───┘
```

### 1. Subscription START (extend the existing upgrade)
`tier_manager.upgrade_user(plan, period="monthly")` also computes and stores:
- `subscription_period` = "monthly" | "quarterly" | "yearly"
- `subscription_started` = now (rename/keep `tier_upgraded_at`)
- `subscription_ends` = now + period length (30 / 90 / 365 days, or calendar-
  month math)
- `subscription_source` = "paystack"
Paystack `reference` already carries the plan; extend the checkout to carry the
**period** too (`kashia_{plan}_{period}_{id}_{ts}`) so the webhook knows how long
to extend. Renewal = same flow; if still active, EXTEND from `subscription_ends`
(don't lose remaining days); if lapsed, extend from now.

### 2. Renewal-window ALERTS (new scheduled Lambda)
New `SubscriptionReminderFunction` (copy MonthlyResetFunction's SAM shape; daily
cron e.g. `cron(0 8 * * ? *)`). Each run scans users with a paid `tier` + a
`subscription_ends`, computes days-left, and sends via resolve_client/send_text:
- **T-7 (almost expired):** "Your {Plan} renews in 7 days — ₦{price}. Tap to
  renew." + Renew button.
- **T-3 / T-1:** escalating nudges.
- **T-0 / past (expired):** "Your {Plan} has expired — you're back on Free
  (limits apply). Renew anytime." + Renew button.
Send-once guards: stamp `last_renewal_nudge` (date + which bucket) so the same
bucket isn't re-sent daily. Respect `notifications_enabled` (as scheduled reports
do).

### 3. EXPIRY + auto-downgrade (same Lambda)
When `subscription_ends` < today (optionally + a grace period):
- Add `tier_manager.downgrade_user(phone)` (the missing tier-setter) → sets
  `tier="free"`, records `downgraded_at`, keeps all data. Existing `check_can_*`
  limits then re-apply automatically.
- Optional **grace period** (e.g. 3 days) before the hard flip, with a "payment
  overdue" message — softer, reduces accidental lockout. Owner decides length.

### 4. Multi-period subscriptions
Add periods to pricing + picker:
- `PLANS`/`TIERS` gain per-period amounts (e.g. yearly = 10× monthly = 2 months
  free, owner sets the discount).
- Plan picker offers Monthly / Quarterly / Yearly; the chosen period flows into
  the Paystack reference + `subscription_ends` math.
- Alerts and the "renews in N days" copy read `subscription_period`.

### 5. Reminder to SUBSCRIBE (free users) + RENEW (lapsed)
- **Free users:** a gentle, rate-limited nudge (e.g. when they hit ~80% of the
  free transaction cap — that warning already exists in `check_can_record`; make
  it a real "Subscribe" CTA with the picker) and/or a periodic value nudge. Not
  spammy — cap frequency.
- **Lapsed users:** the expiry alert already carries a Renew button; optionally a
  follow-up a few days later ("come back to {Plan}").

---

## Auto-renewal? (honest scope call)
True auto-renewal needs a **Paystack recurring subscription/plan** (store a card
authorization, handle `subscription.*` / `invoice.payment_failed` webhooks) —
that's a bigger, separate integration. **Recommendation for v1: manual renewal
via reminders + one-tap checkout link** (what we have, plus the alerts). It
delivers the revenue-protecting behavior (users get warned and renew) without the
card-on-file complexity. Auto-renewal can be a **phase 2** once v1 proves the
funnel. (Flagged so we don't over-build.)

---

## Build order (incremental, each shippable)
- **S1 — Expiry data + downgrade path.** Extend `upgrade_user` to set
  `subscription_ends`/`period`/`started`; add `downgrade_user`. Grandfather
  existing paid users. (No behavior change yet; foundation.)
- **S2 — Expiry + alert Lambda.** New `SubscriptionReminderFunction` (T-7/T-3/T-1
  + expired) that also performs the auto-downgrade past expiry (+ optional
  grace). Reuse resolve_client/send_text + DLQ. Verify with dry-runs
  (days-left buckets, send-once guard, downgrade flips tier, data preserved).
- **S3 — Multi-period pricing + picker.** Monthly/Quarterly/Yearly in
  PLANS/TIERS + the plan picker + period in the Paystack reference + expiry math.
- **S4 — Subscribe/renew nudges.** Turn the free-cap warning into a real
  Subscribe CTA; lapsed follow-up. Rate-limited.
- **S5 (later / phase 2) — Paystack recurring** (card-on-file auto-renew,
  subscription webhooks). Only if v1 funnel justifies it.

Also fix alongside: re-enable `check_can_generate_pdf` (parked S4 tech-debt) and
confirm `check_can_export`/`check_can_invoice` call sites so downgrade truly
re-applies every limit.

---

## Decisions needed from the owner
1. **Grandfathering:** existing paid users with no expiry date — give them "no
   expiry until next renewal" (safest), or seed `subscription_ends` from
   `tier_upgraded_at` + one period? (Recommend: no-expiry-until-next-renewal, so
   nobody who already paid gets surprise-downgraded.)
2. **Grace period** after expiry before downgrade: 0 / 3 / 7 days? (Recommend 3.)
3. **Multi-period pricing:** confirm quarterly/yearly amounts + discount (e.g.
   yearly = pay for 10 months). Or launch monthly-only and add periods later.
4. **Auto-renewal:** v1 = manual renewal via reminders (recommended) vs. build
   Paystack recurring now.
5. **Alert cadence:** T-7 / T-3 / T-1 / expired — good, or different?
6. **Free-user subscribe nudge:** only at the 80% cap warning, or also a periodic
   value nudge? How often at most?

---

## ✅ OWNER DECISIONS (2026-09-11) — locked
1. **Grandfathering:** existing paid users get **no expiry until their next
   renewal** (never surprise-downgrade someone who already paid). A missing
   `subscription_ends` = active/grandfathered.
2. **Grace period:** **3 days** after `subscription_ends` before the hard
   downgrade to Free (a "payment overdue" nudge during grace).
3. **Multi-period:** BUILD Monthly / Quarterly / Yearly (S3).
4. **Auto-renewal:** **v1 = manual renewal** via reminders + one-tap checkout.
   Paystack recurring (card-on-file) is **phase 2 / later** (S5).
5. **Alert cadence:** T-7 / T-3 / T-1 / expired.
6. **Free-user subscribe nudge:** at the ~80% free-cap warning, rate-limited.

Build order confirmed: **S1 → S2 → S3 → S4**, S5 later. Starting with S1.
