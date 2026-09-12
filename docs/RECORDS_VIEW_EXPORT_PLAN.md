# Records Viewing & Export Plan — see and print your records by period/date

_Created 2026-09-12. For approval before building. Reuses the existing engine
(`_date_range`/`_resolve_range`, `get_transactions_by_period`,
`export_service.handle_filtered_export`); no forked business logic. Telegram-first
+ Mini App; WhatsApp keeps its current paths. Money figures stay sourced from the
shared accounting engine._

Answers the owner's ask:
> "How does a user check their records — my sales for this period, my purchases
> on this day, who owes me on this day? And it should be printable (Excel/PDF)."

---

## Current reality (verified against code)

- **Chat record tabs** (`biz_sales`/`biz_purchases`/`biz_expenses` →
  `reports._tab_report`) are HARD-LOCKED to the current calendar month, newest
  **15** only. No period toggle, no single-date filter.
- **Period selection** exists only on the P&L/dashboard (`_pnl_report` /
  `_date_range`: today/week/month/last_month/quarter/year) — but that renders
  **summary totals, not a transaction list**.
- **Export reachable today:** Excel (THIS-MONTH tx list), CSV (ALL history),
  Contacts xlsx, receipt/statement PDFs. A range-aware, per-type
  `export_service.handle_filtered_export(phone, filter_type, start, end,
  period_label, fmt='excel'|'pdf')` — which emits an Excel **or** PDF of a
  filtered transaction LIST + total — **already exists but is wired to NO button.**
- **Statement PDF** is summary-only and its period picker is BUGGY:
  `generate_financial_statement` only branches `month` vs everything-else→**YEAR**,
  so today/week/last_month silently produce year-to-date.
- **Mini App:** no transaction-list view, no export/download at all.
- **Debt** is current-balance-only (mutate-in-place). "Who owed me on date X" is
  NOT derivable without transaction replay → OUT OF SCOPE v1 (see below).

The good news: the hard parts (range date math, the filtered Excel/PDF exporter)
already exist. This is mostly **wiring + a proper list UI**, not new engine work.

---

## Design principles (locked)

1. **One engine.** Reuse `_date_range`/`_resolve_range` for boundaries,
   `get_transactions_by_period` for the fetch, and `handle_filtered_export` for
   Excel/PDF. No new totals math; money still comes from `services/accounting`.
2. **Same period vocabulary everywhere** (chat, Mini App, export): today / week /
   month / last_month / quarter / year / **single day** / **custom range**.
3. **View then print.** Every record list has an "⬇️ Export" action that exports
   THAT exact filter (type + range) to Excel or PDF.
4. **Balances are "as of now."** A record LIST is period-scoped; debt/inventory
   balances remain current-snapshot (already labelled in the Mini App). No fake
   historical balances.
5. **Telegram-first, WhatsApp safe.** New tap-first list/period UI is
   Telegram-gated; WhatsApp keeps its current tabs + export menu. Shared-engine
   fixes (statement period bug) benefit both.
6. **Paginated, not truncated.** Lists page (reuse the `__tgpg__` pagination)
   instead of the current silent "recent 15".

---

## Sequencing (each verified before the next)

### V1 — Chat: period-scoped record list (the core view)
- Rework `reports._tab_report(phone, tab_type)` → `_tab_report(phone, tab_type,
  period="month", start=None, end=None)`. Resolve the range via a shared helper
  (lift the Mini App's `_resolve_range` logic into `reports` so both share it),
  fetch with `get_transactions_by_period`, filter by type, render a header
  (Total / Count / Average for the range) + a **paginated** tx list.
- Add a period toggle row to the view: Today · Week · Month · Last month ·
  Quarter · Year · 📅 Pick a date. Button ids `rec_<type>_<period>` re-render in
  place. (The docstring already promised a switcher; this delivers it.)
- **Single date / custom range in chat:** "📅 Pick a date" → prompt "Type a date
  (YYYY-MM-DD) or a range (YYYY-MM-DD to YYYY-MM-DD)"; parse → `start/end`.
  (Telegram has no native date input in a message; a typed date is the pragmatic
  path. Mini App gets the real date picker — see V3.)
- Keep `report_edit_<type>` (edit records) working.

### V2 — Chat: export THIS filtered view (wire the dead exporter)
- Add "⬇️ Export Excel" / "🧾 Export PDF" to the record-list view. Route to
  `export_service.handle_filtered_export(phone, filter_type=my_<type>, start, end,
  period_label, fmt)` via a marker (mirror the existing `__EXPORT_REPORT__`
  pattern in `main._resolve_markers`) so it stays engine-side.
- PIN-gate + paywall consistent with current exports (`requires_pin`,
  `check_can_generate_pdf` for the PDF).
- Result: "my purchases from 1–15 Sep" → Excel/PDF of exactly those rows + total.

### V3 — Mini App: record list + export
- New read endpoint `GET /app/api/records?type=sale|purchase|expense&period=…`
  (or `from`/`to`) → returns the transaction LIST for the range (reuse
  `get_transactions_by_period` + the same `_resolve_range`). Paginated
  (limit/offset) for large histories.
- UI: a **Records** view (either a new tab or a drill from Dashboard) with a
  type switch (Sales/Purchases/Expenses), the SAME period chips + 📅 date picker
  already built, a scrollable list, and a running total.
- **Export from the app:** an "⬇️ Export" button → `GET /app/api/export?type=…&
  period=…&fmt=excel|pdf` that runs `handle_filtered_export`, uploads to S3, and
  returns the presigned URL (open/download in the browser). Reuses `deliver_file`
  upload path (URL only, no chat send).

### V4 — Fix the statement period bug (shared correctness)
- `pdf_generator.generate_financial_statement`: replace the month-vs-year branch
  with `_date_range(period)` so today/week/month/last_month/quarter/year all
  produce the CORRECT window (this benefits chat + WhatsApp too). Optionally
  accept explicit start/end for a custom range.

### V5 — Verify
- Dry-run: list for each type × each period + a single day + a custom range
  returns the right rows/total; export produces a file whose rows == the view;
  statement period now matches the chosen window; WhatsApp tabs/export intact;
  Mini App `_PAGE_HTML` still utf-8-safe; everything compiles.

---

## Decisions / guardrails
- **Reuse `handle_filtered_export`** (Excel + PDF, already built) — do NOT write a
  new exporter. Just wire it + feed it the resolved range.
- **Lift `_resolve_range` to a shared spot** so chat, Mini App, and export use ONE
  range resolver (single source of truth; already handles custom from/to + single
  day).
- **Pagination** via the existing `__tgpg__` mechanism (no silent 15-cap).
- **Money-safe:** totals on the list come from the same rows the export uses.
- **WhatsApp untouched** for the tap-first list; it keeps its current month tabs +
  export menu (the statement fix + any shared-engine change still applies).

## Out of scope (v1)
- **Historical "who owed me on date X".** Debt is mutate-in-place; reconstructing
  a past-date balance needs a transaction-replay engine (or a daily balance
  snapshot store). Separate, larger effort — flag, don't build here. (v1 debt view
  stays "current balances", already labelled.)
- Scheduled/emailed exports.
- Multi-format bundle (zip) — one file per export is fine.
