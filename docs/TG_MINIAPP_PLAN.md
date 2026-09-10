# Kashia — Telegram Mini App (N6 / Stage 7) Plan

_Created 2026-09-10. The plan for the in-chat web view: a full inventory grid +
interactive dashboard/charts, reusing the same engine + data. This is the
roadmap's ceiling feature, deliberately last, on a now-settled data model._

## Guiding principles (unchanged from TELEGRAM_MASTER_PLAN)
1. **One engine, two platforms — now three surfaces.** The Mini App is a THIRD
   presentation surface (chat = Telegram + WhatsApp; web = Mini App). It NEVER
   forks business logic. Every number it shows comes from the existing
   `services/accounting.py` (P&L, cash flow, margins, position, profit trend) and
   the existing catalog/normalizers. No new accounting math.
2. **WhatsApp stays working.** The Mini App is Telegram-only by nature (it's a
   Telegram WebApp). Nothing here touches the WhatsApp path.
3. **Incremental, always shippable.** Built in stages (M0–M6 below); each ships
   and is verifiable before the next.
4. **Read-only first.** v1 is READ-ONLY (view inventory + dashboard). Editing /
   recording from the web view is a later stage, once auth + data plumbing are
   proven. Money-mutating actions get the most care and come last.
5. **Money-safe & auth-safe.** The web view authenticates via Telegram's signed
   `initData` (HMAC with the bot token), validated server-side on EVERY request.
   No user data is served without a valid, unexpired signature.
6. **Backward-compatible.** Purely additive: a new API route + a static page.
   Existing Lambdas/routes/data untouched.

---

## What a Telegram Mini App actually is (grounding)
- A **web page** (HTML/CSS/JS) opened INSIDE Telegram via a button
  (`web_app` inline button, or the bot Menu Button). Telegram renders it in an
  in-app browser and injects `window.Telegram.WebApp`.
- On open, Telegram provides **`initData`** — a signed query string containing
  the user (id, name), auth date, and a hash. The server validates the hash with
  the bot token (HMAC-SHA256) to trust "this really is tg:<chat_id>".
- The page then calls our **backend API** (same SAM stack) with `initData` in an
  `Authorization`-style header; the backend validates it, derives the user id
  (`tg:<chat_id>`), and returns that user's data as JSON.
- The page renders the data (inventory grid, dashboard, charts). No secrets ever
  reach the browser — only the user's own already-owned data.

References (verify at build time; APIs are stable but confirm current):
- Telegram WebApp: https://core.telegram.org/bots/webapps
- initData validation: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

---

## Architecture (fits the existing stack)

```
Telegram chat
  └─ "📊 Open Dashboard" web_app button  (added in telegram_webhook / a menu)
        └─ opens https://<api>/app  (static HTML shell + JS)
              └─ JS reads Telegram.WebApp.initData
              └─ fetch https://<api>/app/api/<resource>  (initData in header)
                    └─ MiniAppFunction (new Lambda)
                          - validate initData HMAC (bot token from SSM)
                          - derive tg:<chat_id>
                          - call services/accounting + catalog (READ ONLY)
                          - return JSON
```

- **New Lambda: `MiniAppFunction`** on the existing `WebhookApi`, serving:
  - `GET /app` → the static HTML shell (single page; JS/CSS inlined or from S3).
  - `GET /app/api/summary` → dashboard numbers (period P&L, cash, debt, position).
  - `GET /app/api/inventory` → the normalized product/catalog grid (incl. tree
    roll-ups, stock, cost, low-stock flags).
  - `GET /app/api/charts/*` → series data for charts (profit trend, top products)
    — OR reuse N2's server-rendered chart images if we prefer images over JS
    charts in v1.
- **Reuses:** `services/accounting.AccountingEngine` (period_pnl, period_cashflow,
  position, product_margins, profit_trend), `features/catalog` normalizers,
  `services/database`. No new business logic.
- **Auth module: `services/miniapp_auth.py`** — pure `validate_init_data(init_data,
  bot_token) -> {ok, user_id}`; HMAC-SHA256 per Telegram's spec; rejects stale
  auth_date (configurable max age, e.g. 1 hour) and bad hashes. Unit-testable
  with no network.
- **SSM:** reuses `/kashia/telegram-bot-token` (already present) for validation.
- **Menu Button / entry:** register the bot's Menu Button to open the web app
  (Bot API `setChatMenuButton`), and/or add a `web_app` inline button on the
  Telegram dashboard card. A new `set_telegram_menu_button.sh` (one-time) or fold
  into the existing setup scripts.

### Why one Lambda serving both page + API
- One deploy (`./deploy.sh dev`), one origin (no CORS headache — page and API
  same host), initData validated in one place. Simple and shippable. We can split
  the static page to S3+CloudFront later if we want a CDN; not needed for v1.

---

## Data contracts (v1, read-only)

`GET /app/api/summary?period=today|week|month|last`
```json
{
  "business": "Kashia Motors",
  "period": "month",
  "pnl": { "revenue": 0, "cogs": 0, "gross": 0, "opex": 0, "net": 0, "gross_margin_pct": 0 },
  "cash": { "in": 0, "out": 0, "net": 0 },
  "debt": { "owed_to_me": 0, "i_owe": 0, "net": 0 },
  "position": { "inventory_value": 0, "receivables": 0, "payables": 0, "net_position": 0 },
  "uncosted_sales": 0
}
```

`GET /app/api/inventory`
```json
{
  "count": 0,
  "products": [
    { "key": "honda", "name": "Honda", "stock": 41, "unit": "unit",
      "cost": 0, "sale_price": 0, "low_stock": false, "has_variants": true,
      "stock_value": 0, "category": "" }
  ]
}
```
All figures come from the accounting engine / normalizers — identical to what the
chat dashboard shows, so the web numbers and chat numbers always agree.

---

## Staged build order (each verified + shipped before the next)

- **M0 — Plan + decisions (this doc).** Confirm scope, hosting, auth, v1 =
  read-only. ← we are here.
- **M1 — Auth core (no UI).** `services/miniapp_auth.validate_init_data` +
  unit tests (valid hash passes, tampered/expired fails). Pure, offline. Ship.
- **M2 — Backend endpoints (JSON only).** `MiniAppFunction` + `handlers/
  miniapp.py` serving `/app/api/summary` and `/app/api/inventory`, guarded by
  M1 auth, reusing the accounting engine. template.yaml route + SSM read. Test
  with a locally-generated signed initData. Ship.
- **M3 — Static shell + entry button.** `GET /app` returns a minimal responsive
  HTML page; add the Telegram Menu Button / a `web_app` button on the dashboard
  card. Prove the page opens in-chat, reads initData, and round-trips one number.
  Ship.
- **M4 — Inventory grid (read-only).** Render the full product grid (search,
  low-stock highlight, tree roll-ups, stock value). The thing WhatsApp/chat can't
  do well. Ship.
- **M5 — Dashboard + charts.** Period toggles (Today/Week/Month/Last), P&L / cash
  / debt / position cards, and charts (JS charts from the series endpoints, or
  reuse N2 chart images). Ship.
- **M6 — (LATER, separate go-ahead) Interactivity.** Editing stock / recording a
  sale/purchase from the web view — routed through the SAME engine save path with
  the SAME confirmations. Money-mutating, so it comes last and gets its own
  review. NOT in v1.

Rollback safety: every stage is additive (new Lambda + route + static page). No
existing route, Lambda, or data shape changes. Removing the Menu Button + route
fully disables it.

---

## Open decisions (confirm before M1)
1. **v1 = read-only?** (Recommended yes; M6 interactivity later.)
2. **Hosting = one MiniAppFunction on the existing WebhookApi serving page+API?**
   (Recommended yes; S3+CloudFront later only if we want a CDN.)
3. **Charts: JS charts (interactive) or reuse N2 server-rendered images (simpler)
   for v1?** (Lean: images in M5 for speed, JS charts as a polish pass.)
4. **initData max age** for accepting a session (e.g. 1 hour) — affects how often
   Telegram re-signs. Default 3600s.
5. **Entry point:** bot Menu Button (persistent, bottom-left in chat), a
   `web_app` button on the dashboard card, or both? (Recommended: both.)

---

## Risks / notes
- **Auth is the critical path.** A wrong HMAC validation = either locked-out users
  or (worse) data leakage. M1 is isolated and unit-tested precisely for this.
- **API Gateway + binary/HTML responses:** serving HTML from Lambda via API
  Gateway needs correct content-type handling; confirm the `WebhookApi`
  integration passes it through (may need a `text/html` response + base64 setting).
- **Mini App is a separate runtime surface** — most effort of any stage; that's
  why it's staged small and read-only first.
- **No new data model.** If a future need appears, follow the existing lazy-
  migration + normalizer discipline; don't special-case for the web view.
