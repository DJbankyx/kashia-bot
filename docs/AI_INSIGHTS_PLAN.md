# AI Smart Insights (Option A) — Plan Doc

_Drafted: 2026-09-11. Status: FOR BUILD (owner approved: Option A now, Pro-gated;
Option B conversational Q&A later)._

A "🧠 Smart Insights" feature that turns the business's REAL numbers into a few
plain-English observations + suggestions, written by AI. The AI only NARRATES
figures the accounting/CRM engine already computed — it never computes or invents
a number. Pro-gated. Telegram-first; WhatsApp gets the same text (it's just a
message).

---

## Non-negotiable safety property (money app)
**Numbers come from the engine; the AI only phrases them.** The prompt is fed
EXACT, pre-computed figures (P&L, margins, cash, debts, trend, top/slow products,
inactive customers) and is instructed: "explain and advise on THESE numbers; do
NOT invent, recompute, or state any figure not given." So it can never report a
wrong balance. If the AI call fails, we fall back to a deterministic
rule-based insight summary (no blank screen).

## What it reuses (all already built)
- `reports._period_totals(phone, period)` + `_date_range(period)` — headline P&L
  (revenue, COGS, gross/net, margin, debts).
- `accounting.product_margins` (best/worst margin products), `period_cashflow`
  (cash in/out, refunds), `position` (inventory value, receivables/payables),
  `profit_trend` (last 6 months net).
- `crm.generate_insights` — the currently-UNUSED supplier-concentration +
  inactive-customer logic (finally put to work as deterministic signals).
- OpenAI client pattern from categorizer.py: `OpenAI(api_key=get_openai_key())`
  → `chat.completions.create(model="gpt-4o-mini", messages=[...])`.
- Tier gate pattern from the PDF paywall: `tier_manager` + the `crm_insights`
  flag (Pro-only).
- Cross-platform delivery: normal engine response dicts (text/buttons).

## The build — a new service + a tap entry
### 1. services/ai_insights.py (new)
`AIInsights(db, session)`:
- `gather(phone, period)` → a compact dict of REAL numbers: pnl, margins (top +
  worst), cashflow, position, trend, plus CRM signals from
  crm.generate_insights and the inactive/best-customer stats. Pure data, no AI.
- `narrate(facts)` → calls gpt-4o-mini with a tight system prompt: "You are
  Kashia's business advisor for a Nigerian SME. Given ONLY these figures, write
  3–5 short, specific, friendly observations + one concrete suggestion each. Use
  the exact numbers provided (NGN). Never invent figures. If a figure is missing,
  don't mention it." Returns the text. On any error/timeout → `fallback(facts)`.
- `fallback(facts)` → deterministic bullet list from the same facts (so the
  feature ALWAYS returns something useful even with no AI/network).
- `smart_insights(phone, period)` → the public entry: gather → narrate → format
  a card. Cheap guard: cache the last result on the user for ~6h
  (`ai_insight_cache` = {period, date, text}) so repeated taps don't re-bill
  OpenAI.

### 2. Pro gate
Before generating: `tier_manager` check on the `crm_insights` flag (Pro-only).
Free/Basic get an upgrade prompt (reuse the paywall message style + the S3
Subscribe CTA). Add a `check_can_use_insights(phone)` helper to tier_manager
mirroring check_can_generate_pdf.

### 3. Entry points (Telegram tap-first; WhatsApp gets the text)
- A **"🧠 Smart Insights"** button on the dashboard card (reports.dashboard),
  routed like the other drills: `dash_drill_ai_<period>` → AIInsights. (Keeps it
  next to the numbers it explains.)
- Also reachable from the CRM "📊 Insights" area as a "🧠 Smart (AI)" option
  alongside the existing basic insights, so the basic one stays free and the AI
  one is the Pro upsell.
- Optional later: fold a short AI insight into the weekly scheduled report.

## Cost / latency guards
- gpt-4o-mini (cheap, same model as categorizer), max_tokens capped (~400).
- 6h per-user cache keyed by period → repeated taps don't re-bill.
- On timeout/error → deterministic fallback (never blocks, never blank).

## Build order
- **AI1** — services/ai_insights.py: gather() + fallback() + narrate() +
  smart_insights() with cache. Dry-run gather/fallback with stubs (no network);
  narrate behind a try/except so tests don't need OpenAI.
- **AI2** — Pro gate (tier_manager.check_can_use_insights) + wire the
  dash_drill_ai_<period> tap in reports + the CRM entry. Free/Basic → upgrade CTA.
- **AI3** (later, separate) — Option B conversational "Ask Kashia" Q&A, built on
  the same gather() facts once the pattern is trusted.

## Risk flags
- MONEY: AI must never state a number not in `facts`. Enforced by prompt + the
  fact that we pass exact figures and cap creativity; the fallback is pure data.
- COST: guarded by cache + cheap model + token cap.
- Pro-gate must not break Free users — they get a clean upsell, not an error.
- WhatsApp: it's just a text message via the normal pipeline — unaffected.

## Decisions (proceeding with recommended defaults)
1. Pro-gated — YES (owner approved).
2. Cache window 6h — reasonable default (tune later).
3. Entry: dashboard drill + CRM insights area (recommended) — both, so it's
   discoverable where users already look.
