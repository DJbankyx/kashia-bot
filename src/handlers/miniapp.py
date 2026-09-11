# src/handlers/miniapp.py
"""Telegram Mini App backend (M2) — read-only JSON endpoints.

Serves the Mini App's data over the existing REST API (WebhookApi). Every
request is authenticated with the Telegram WebApp `initData` signature (M1,
services/miniapp_auth), so a caller can only ever read THEIR OWN data
(tg:<chat_id>). All figures are computed by the SHARED accounting engine
(services/accounting) + catalog normalizers — no new business logic, so the web
numbers always agree with the chat dashboard.

Routes (all GET, read-only in v1):
  /app/api/summary?period=today|week|month|last_month  → P&L, cash, debt, position
  /app/api/inventory                                    → normalized product grid

Auth: the web page sends the initData string in the `X-Telegram-Init-Data`
header (or ?_auth= query fallback). Missing/invalid/expired → 401.

M3 will add GET /app (the HTML shell) to this same function.
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _json(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            # The page and API share an origin (same API Gateway), so CORS isn't
            # strictly needed, but be explicit and safe.
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def _get_init_data(event) -> str:
    """Pull the Telegram initData from the request (header preferred)."""
    headers = event.get("headers") or {}
    # Header names can arrive in any case via API Gateway.
    for k, v in headers.items():
        if k.lower() == "x-telegram-init-data":
            return v or ""
    # Fallback: query string (?_auth=...), useful for the initial page fetch.
    qs = event.get("queryStringParameters") or {}
    return qs.get("_auth", "") or ""


def _authenticate(event):
    """Validate initData → return (user_id, None) on success, or (None, resp)
    with a 401 JSON response on failure."""
    from services.miniapp_auth import validate_init_data
    from utils.config import get_telegram_bot_token

    init_data = _get_init_data(event)
    if not init_data:
        return None, _json(401, {"error": "missing auth"})
    try:
        token = get_telegram_bot_token()
    except Exception as e:
        logger.error(f"miniapp: could not read bot token: {e}")
        return None, _json(500, {"error": "server auth config"})

    result = validate_init_data(init_data, token)
    if not result.get("ok"):
        logger.warning(f"miniapp auth rejected: {result.get('error')}")
        return None, _json(401, {"error": "unauthorized"})
    return result["user_id"], None


def _html(body_html: str):
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
        },
        "body": body_html,
        "isBase64Encoded": False,
    }


def lambda_handler(event, context):
    """Route the Mini App request (page + API)."""
    try:
        path = (event.get("path") or event.get("rawPath") or "").rstrip("/")
        method = (event.get("httpMethod")
                  or (event.get("requestContext", {}) or {}).get("http", {}).get("method")
                  or "GET").upper()

        if method not in ("GET", "POST"):
            return _json(405, {"error": "method not allowed"})

        # The page shell is public HTML (no data in it — the JS fetches data with
        # initData afterward). Data routes below require a valid signature.
        if method == "GET" and path.endswith("/app"):
            return _html(_PAGE_HTML)

        # Every data + write route requires a valid Telegram signature.
        user_id, err = _authenticate(event)
        if err is not None:
            return err

        # ── Writes (M6a) ──
        if method == "POST" and path.endswith("/app/api/product"):
            return _product_write(event, user_id)

        # ── Reads ──
        if method == "GET" and path.endswith("/app/api/summary"):
            return _summary(event, user_id)
        if method == "GET" and path.endswith("/app/api/inventory"):
            return _inventory(event, user_id)
        if method == "GET" and path.endswith("/app/api/charts"):
            return _charts(event, user_id)

        return _json(404, {"error": "not found", "path": path})

    except Exception as e:
        logger.error(f"miniapp handler error: {e}")
        return _json(500, {"error": "server error"})


# ── Endpoints ───────────────────────────────────────────────────────────────

def _summary(event, user_id: str):
    """Dashboard numbers for a period. Reuses the accounting engine + the SAME
    period boundaries as the chat dashboard (reports._date_range)."""
    from services.database import Database
    from services.accounting import Accounting
    from features.reports import _date_range

    qs = event.get("queryStringParameters") or {}
    period = (qs.get("period") or "month").lower()
    if period not in ("today", "week", "month", "last_month"):
        period = "month"

    db = Database()
    acct = Accounting(db)
    start, end, label = _date_range(period)

    pnl = acct.period_pnl(user_id, start, end, label)
    cf = acct.period_cashflow(user_id, start, end, label)
    pos = acct.position(user_id)

    user = db.get_user(user_id) or {}
    business = user.get("business_name") or "Your business"

    return _json(200, {
        "business": business,
        "period": period,
        "period_label": label,
        "pnl": {
            "revenue": pnl["revenue"],
            "cogs": pnl["cogs"],
            "gross_profit": pnl["gross_profit"],
            "gross_margin_pct": pnl["gross_margin_pct"],
            "opex": pnl["opex"],
            "net_profit": pnl["net_profit"],
            "net_margin_pct": pnl["net_margin_pct"],
        },
        "cash": {
            "in": cf["cash_in"],
            "out": cf["cash_out"],
            "net": cf["net_cash"],
        },
        "debt": {
            "owed_to_me": pos["receivables"],
            "i_owe": pos["payables"],
            "net": pos["receivables"] - pos["payables"],
        },
        "position": {
            "inventory_value": pos["inventory_value"],
            "inventory_units": pos["inventory_units"],
            "receivables": pos["receivables"],
            "payables": pos["payables"],
            "net_position": pos["net_worth_proxy"],
        },
        "uncosted_sales": pnl["uncosted_count"],
    })


def _row_from_product(p: dict) -> dict:
    """Map a normalized product into the grid-row shape the page expects.
    Shared by the inventory list and the write echo, so the UI can patch a row
    in place after a write with an identical shape."""
    return {
        "key": p.get("_key"),
        "name": p.get("name"),
        "category": p.get("category") or "",
        "unit": p.get("primary_unit") or "",
        "stock": int(p.get("stock") or 0),
        "cost": int(p.get("landing_cost") or 0),
        "sale_price": int(p.get("sale_price") or 0),
        "reorder_level": int(p.get("reorder_level") or 0),
        "low_stock": bool(p.get("_is_low_stock")),
        "has_variants": bool(p.get("_has_tree") or p.get("_has_variants")),
        "stock_value": int(p.get("_stock_value") or 0),
        "item_type": p.get("item_type") or "",
    }


def _inventory(event, user_id: str):
    """The normalized product grid — the same data the catalog shelf shows."""
    from services.database import Database
    from features.catalog import CatalogHandler

    db = Database()
    cat = CatalogHandler(None, db)
    products = cat._normalized_products(user_id) or []
    rows = [_row_from_product(p) for p in products]

    # Stable, useful ordering: low-stock first, then by name.
    rows.sort(key=lambda r: (not r["low_stock"], (r["name"] or "").lower()))

    return _json(200, {"count": len(rows), "products": rows})


def _parse_body(event) -> dict:
    """Parse a JSON request body (API Gateway may base64-encode it)."""
    import base64
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:
            return {}
    try:
        return json.loads(body) if body else {}
    except Exception:
        return {}


def _product_write(event, user_id: str):
    """M6a — apply a catalog write for the AUTH'D user only. Body:
        {action, key, variant?, value}
    Actions: set_price, set_cost, set_stock (exact), set_stock_delta (+/-).
    All reuse existing engine methods; money-safe (value-sets, per-user, tree
    leaf aware). Echoes the recomputed product row so the UI reflects truth."""
    from services.database import Database
    from features.catalog import CatalogHandler

    data = _parse_body(event)
    action = str(data.get("action", "")).strip()
    key = str(data.get("key", "")).strip()
    variant = str(data.get("variant", "") or "").strip()
    raw_value = data.get("value")

    if not key or action not in ("set_price", "set_cost", "set_stock", "set_stock_delta"):
        return _json(400, {"error": "bad request"})

    db = Database()
    cat = CatalogHandler(None, db)

    # Resolve the product on the AUTH'D user's catalog only.
    products = cat._get_products(user_id) or {}
    prod = products.get(key)
    if not isinstance(prod, dict):
        return _json(404, {"error": "product not found"})
    name = prod.get("name") or key

    # Validate the value per action (server-side; never trust the client).
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return _json(400, {"error": "value must be a number"})

    if action in ("set_price", "set_cost", "set_stock") and value < 0:
        return _json(400, {"error": "value must be 0 or more"})

    ok = True
    if action == "set_price":
        ok = cat.set_sale_price(user_id, key, value)
    elif action == "set_cost":
        ok = cat.set_cost_direct(user_id, name, value, variant)
    elif action == "set_stock":
        res = cat.set_stock_exact(user_id, name, value, variant)
        ok = bool(res.get("matched", True))
    elif action == "set_stock_delta":
        res = cat.update_stock(user_id, name, value, variant=variant, cost_mode="keep")
        ok = bool(res.get("matched", True))

    if not ok:
        return _json(500, {"error": "write failed"})

    # Echo the recomputed row (true stored state, not the client's optimistic value).
    updated = cat.get_normalized_product(user_id, key)
    return _json(200, {"ok": True, "product": _row_from_product(updated)})


def _png_data_uri(path: str):
    """Read a PNG file and return a base64 data URI, or None. Returned inside a
    JSON body (data URI) so we avoid API Gateway binary-media-type config — the
    page just sets it as an <img> src."""
    import base64
    try:
        if not path:
            return None
        with open(path, "rb") as f:
            b = f.read()
        return "data:image/png;base64," + base64.b64encode(b).decode("ascii")
    except Exception as e:
        logger.warning(f"miniapp chart read failed: {e}")
        return None


def _charts(event, user_id: str):
    """Chart images for the dashboard tab (M5.1). Reuses the SAME aggregation as
    the chat visual dashboard (top products from period sales + profit_trend) and
    the shared chart_renderer. Returns base64 PNG data URIs in JSON."""
    from services.database import Database
    from services.accounting import Accounting
    from features.reports import _date_range

    qs = event.get("queryStringParameters") or {}
    period = (qs.get("period") or "month").lower()
    if period not in ("today", "week", "month", "last_month"):
        period = "month"

    out = {"top_products": None, "profit_trend": None}
    try:
        from services.chart_renderer import bar_chart, trend_chart
    except Exception:
        # Charts optional — return nulls; the page hides the section.
        return _json(200, out)

    db = Database()
    acct = Accounting(db)
    start, end, label = _date_range(period)

    # Top products for the period (revenue by product name).
    try:
        pnl = acct.period_pnl(user_id, start, end, label)
        agg = {}
        for t in pnl.get("sales", []):
            name = (t.get("item_name") or t.get("description") or "Item").strip()
            agg[name] = agg.get(name, 0) + int(t.get("amount", 0) or 0)
        top = sorted(agg.items(), key=lambda x: x[1], reverse=True)[:6]
        if top:
            p = bar_chart(f"Top products - {label}",
                          [n for n, _ in top], [v for _, v in top],
                          filename=f"ma_top_{user_id[-6:]}.png")
            out["top_products"] = _png_data_uri(p)
    except Exception as e:
        logger.warning(f"miniapp top-products chart failed: {e}")

    # Net-profit trend, last 6 months.
    try:
        series = acct.profit_trend(user_id, months=6)
        if any(v for _, v in series):
            p = trend_chart("Net profit - last 6 months", series,
                            filename=f"ma_trend_{user_id[-6:]}.png")
            out["profit_trend"] = _png_data_uri(p)
    except Exception as e:
        logger.warning(f"miniapp trend chart failed: {e}")

    return _json(200, out)


# ── The Mini App page (M3 shell + M4 inventory + M5 dashboard) ───────────────
# One self-contained page (no external assets besides Telegram's WebApp SDK) so
# a single Lambda serves everything. Two tabs: Dashboard (period toggles, P&L /
# cash / debt / position) and Inventory (searchable product grid, low-stock
# highlight, margin, stock value). All data comes from the auth'd /app/api/*
# endpoints, which reuse the shared accounting engine — so the web numbers match
# the chat dashboard exactly. Read-only (v1).
_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Kashia</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: var(--tg-theme-bg-color, #0f1115);
    --card: var(--tg-theme-secondary-bg-color, #1a1d24);
    --text: var(--tg-theme-text-color, #f2f4f8);
    --hint: var(--tg-theme-hint-color, #8a93a3);
    --accent: var(--tg-theme-button-color, #2ea6ff);
    --btntext: var(--tg-theme-button-text-color, #ffffff);
    --pos: #35c26a; --neg: #ff5c5c;
    --line: rgba(255,255,255,.08);
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, system-ui, "Segoe UI", Roboto, sans-serif;
    padding: 14px 14px 40px; -webkit-font-smoothing: antialiased; }
  h1 { font-size: 18px; margin: 2px 0; }
  .sub { color: var(--hint); font-size: 13px; margin-bottom: 12px; }
  .card { background: var(--card); border-radius: 14px; padding: 14px; margin-bottom: 10px; }
  .k { color: var(--hint); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  .v { font-size: 24px; font-weight: 700; margin-top: 3px; }
  .row { display: flex; gap: 10px; }
  .row .card { flex: 1; }
  .pos { color: var(--pos); } .neg { color: var(--neg); }
  .err { color: var(--neg); font-size: 14px; }
  .muted { color: var(--hint); font-size: 12px; margin: 14px 0; text-align:center; }
  .tabs { display: flex; gap: 8px; margin-bottom: 12px; }
  .tab { flex: 1; text-align: center; padding: 9px; border-radius: 10px;
    background: var(--card); color: var(--hint); font-weight: 600; font-size: 14px; cursor: pointer; }
  .tab.active { background: var(--accent); color: var(--btntext); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 12px; }
  .chip { padding: 6px 12px; border-radius: 20px; background: var(--card);
    color: var(--text); font-size: 13px; cursor: pointer; border: 1px solid var(--line); }
  .chip.active { background: var(--accent); color: var(--btntext); border-color: var(--accent); }
  .search { width: 100%; padding: 11px 12px; border-radius: 10px; border: 1px solid var(--line);
    background: var(--card); color: var(--text); font-size: 15px; margin-bottom: 10px; }
  .item { display: flex; justify-content: space-between; align-items: center;
    padding: 11px 0; border-bottom: 1px solid var(--line); }
  .item:last-child { border-bottom: none; }
  .item .name { font-weight: 600; font-size: 15px; }
  .item .meta { color: var(--hint); font-size: 12px; margin-top: 2px; }
  .item .right { text-align: right; white-space: nowrap; }
  .item .stock { font-weight: 700; font-size: 15px; }
  .badge { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 6px;
    margin-left: 6px; vertical-align: middle; }
  .badge.low { background: rgba(255,92,92,.18); color: var(--neg); }
  .badge.var { background: rgba(46,166,255,.16); color: var(--accent); }
  .chart { width: 100%; border-radius: 8px; margin-top: 8px; display: block; }
  .item.tappable { cursor: pointer; }
  .item.tappable:active { opacity: .6; }
  /* Edit sheet (bottom sheet modal) */
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.55);
    display: flex; align-items: flex-end; z-index: 50; }
  .sheet { width: 100%; background: var(--bg); border-radius: 16px 16px 0 0;
    padding: 18px 16px 26px; box-shadow: 0 -4px 24px rgba(0,0,0,.4); }
  .sheet h2 { font-size: 16px; margin: 0 0 2px; }
  .sheet .sub2 { color: var(--hint); font-size: 12px; margin-bottom: 14px; }
  .field { margin-bottom: 14px; }
  .field label { display:block; color: var(--hint); font-size: 12px; margin-bottom: 5px; }
  .field input { width: 100%; padding: 11px 12px; border-radius: 10px;
    border: 1px solid var(--line); background: var(--card); color: var(--text); font-size: 16px; }
  .steppers { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .step { flex: 1; min-width: 52px; text-align:center; padding: 9px 0; border-radius: 10px;
    background: var(--card); border: 1px solid var(--line); color: var(--text); font-weight:600; cursor:pointer; }
  .sheet .actions { display: flex; gap: 10px; margin-top: 6px; }
  .btn { flex: 1; padding: 12px; border-radius: 10px; border: none; font-size: 15px;
    font-weight: 700; cursor: pointer; }
  .btn.save { background: var(--accent); color: var(--btntext); }
  .btn.cancel { background: var(--card); color: var(--text); }
  .btn:disabled { opacity: .5; cursor: default; }
  .sheeterr { color: var(--neg); font-size: 13px; margin-top: 6px; min-height: 16px; }
  .hidden { display: none; }
</style>
</head>
<body>
  <h1 id="biz">Kashia</h1>
  <div class="sub" id="period">Loading…</div>

  <div class="tabs">
    <div class="tab active" id="tab-dash" onclick="showTab('dash')">📊 Dashboard</div>
    <div class="tab" id="tab-inv" onclick="showTab('inv')">📦 Inventory</div>
  </div>

  <div id="view-dash">
    <div class="chips" id="chips"></div>
    <div class="card"><div class="k">Net profit</div><div class="v" id="net">—</div></div>
    <div class="row">
      <div class="card"><div class="k">Revenue</div><div class="v" id="rev">—</div></div>
      <div class="card"><div class="k">Cost of sales</div><div class="v" id="cogs">—</div></div>
    </div>
    <div class="row">
      <div class="card"><div class="k">Gross margin</div><div class="v" id="gm">—</div></div>
      <div class="card"><div class="k">Expenses</div><div class="v" id="opex">—</div></div>
    </div>
    <div class="card"><div class="k">Cash in - out</div><div class="v" id="cash">—</div></div>
    <div class="row">
      <div class="card"><div class="k">Owed to you</div><div class="v pos" id="owed">—</div></div>
      <div class="card"><div class="k">You owe</div><div class="v neg" id="iowe">—</div></div>
    </div>
    <div class="card">
      <div class="k">Inventory value / Net position</div>
      <div class="v"><span id="invval">—</span> <span class="k">/</span> <span id="netpos">—</span></div>
    </div>
    <div class="card hidden" id="chartTop">
      <div class="k">Top products</div><img class="chart" id="imgTop" alt="">
    </div>
    <div class="card hidden" id="chartTrend">
      <div class="k">Net profit - last 6 months</div><img class="chart" id="imgTrend" alt="">
    </div>
    <div id="dashmsg" class="muted"></div>
  </div>

  <div id="view-inv" class="hidden">
    <input class="search" id="search" placeholder="Search products..." oninput="renderInv()">
    <div class="card" id="invlist"><div class="muted">Loading...</div></div>
    <div id="invmsg" class="muted"></div>
  </div>

  <!-- Edit sheet (bottom modal) -->
  <div id="overlay" class="overlay hidden">
    <div class="sheet">
      <h2 id="sh-name">Product</h2>
      <div class="sub2" id="sh-sub"></div>
      <div class="field">
        <label>Stock</label>
        <input id="sh-stock" type="number" inputmode="numeric" min="0">
        <div class="steppers">
          <div class="step" onclick="bump(-5)">-5</div>
          <div class="step" onclick="bump(-1)">-1</div>
          <div class="step" onclick="bump(1)">+1</div>
          <div class="step" onclick="bump(5)">+5</div>
          <div class="step" onclick="bump(10)">+10</div>
        </div>
      </div>
      <div class="field">
        <label>Selling price (NGN)</label>
        <input id="sh-price" type="number" inputmode="numeric" min="0">
      </div>
      <div class="field">
        <label>Cost per unit (NGN)</label>
        <input id="sh-cost" type="number" inputmode="numeric" min="0">
      </div>
      <div class="sheeterr" id="sh-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeSheet()">Cancel</button>
        <button class="btn save" id="sh-save" onclick="saveSheet()">Save changes</button>
      </div>
    </div>
  </div>

<script>
(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  var initData = (tg && tg.initData) || "";
  var PERIODS = [["today","Today"],["week","Week"],["month","Month"],["last_month","Last month"]];
  var curPeriod = "month";
  var invData = null;
  var invLoaded = false;

  function naira(n) { return "NGN " + Number(n||0).toLocaleString("en-NG"); }
  function setSigned(id, n) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = naira(n);
    var base = el.className.replace(/\\b(pos|neg)\\b/g, "").trim();
    el.className = base + (Number(n) < 0 ? " neg" : (Number(n) > 0 ? " pos" : ""));
  }
  // The page is served at .../<stage>/app (no trailing slash). A RELATIVE
  // fetch of "api/summary" would resolve against ".../<stage>/" (dropping
  // "app") and 403. So build an ABSOLUTE path from the page's own path:
  // "<...>/app" + "/api/<x>".
  var BASE = window.location.pathname.replace(/\\/+$/, "");
  function api(sub) {
    return fetch(BASE + "/" + sub, { headers: { "X-Telegram-Init-Data": initData } })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status === 401 ? "Not authorized" : ("Error " + r.status));
        return r.json();
      });
  }

  window.showTab = function (which) {
    document.getElementById("tab-dash").classList.toggle("active", which === "dash");
    document.getElementById("tab-inv").classList.toggle("active", which === "inv");
    document.getElementById("view-dash").classList.toggle("hidden", which !== "dash");
    document.getElementById("view-inv").classList.toggle("hidden", which !== "inv");
    if (which === "inv" && !invLoaded) loadInventory();
  };

  function renderChips() {
    var c = document.getElementById("chips");
    c.innerHTML = "";
    PERIODS.forEach(function (p) {
      var el = document.createElement("div");
      el.className = "chip" + (p[0] === curPeriod ? " active" : "");
      el.textContent = p[1];
      el.onclick = function () { curPeriod = p[0]; renderChips(); loadSummary(); };
      c.appendChild(el);
    });
  }
  function loadSummary() {
    var msg = document.getElementById("dashmsg");
    msg.textContent = "";
    api("api/summary?period=" + curPeriod)
      .then(function (d) {
        document.getElementById("biz").textContent = d.business || "Kashia";
        document.getElementById("period").textContent = "P&L - " + (d.period_label || "");
        setSigned("net", d.pnl.net_profit);
        document.getElementById("rev").textContent = naira(d.pnl.revenue);
        document.getElementById("cogs").textContent = naira(d.pnl.cogs);
        document.getElementById("gm").textContent = (d.pnl.gross_margin_pct || 0) + "%";
        document.getElementById("opex").textContent = naira(d.pnl.opex);
        setSigned("cash", d.cash.net);
        document.getElementById("owed").textContent = naira(d.debt.owed_to_me);
        document.getElementById("iowe").textContent = naira(d.debt.i_owe);
        document.getElementById("invval").textContent = naira(d.position.inventory_value);
        setSigned("netpos", d.position.net_position);
        if (d.uncosted_sales > 0) {
          msg.textContent = d.uncosted_sales + " sale(s) have no cost set - profit may be overstated.";
        }
      })
      .catch(function (e) {
        document.getElementById("period").textContent = "";
        msg.innerHTML = '<span class="err">' + (e.message || "Could not load") + '</span>';
      });
    loadCharts();
  }

  function loadCharts() {
    // Charts are best-effort: hide the cards, then show each only if the
    // endpoint returns an image. A chart failure never breaks the dashboard.
    var cTop = document.getElementById("chartTop"), cTrend = document.getElementById("chartTrend");
    cTop.classList.add("hidden"); cTrend.classList.add("hidden");
    api("api/charts?period=" + curPeriod)
      .then(function (d) {
        if (d.top_products) {
          document.getElementById("imgTop").src = d.top_products;
          cTop.classList.remove("hidden");
        }
        if (d.profit_trend) {
          document.getElementById("imgTrend").src = d.profit_trend;
          cTrend.classList.remove("hidden");
        }
      })
      .catch(function () { /* charts optional — ignore */ });
  }

  function loadInventory() {
    invLoaded = true;
    api("api/inventory")
      .then(function (d) { invData = d.products || []; renderInv(); })
      .catch(function (e) {
        document.getElementById("invmsg").innerHTML =
          '<span class="err">' + (e.message || "Could not load") + '</span>';
      });
  }
  window.renderInv = function () {
    if (!invData) return;
    var q = (document.getElementById("search").value || "").toLowerCase().trim();
    var list = document.getElementById("invlist");
    var rows = invData.filter(function (p) {
      return !q || (p.name || "").toLowerCase().indexOf(q) >= 0
                || (p.category || "").toLowerCase().indexOf(q) >= 0;
    });
    if (!rows.length) { list.innerHTML = '<div class="muted">No products.</div>'; return; }
    list.innerHTML = "";
    rows.forEach(function (p) {
      var margin = (p.sale_price && p.cost) ? (p.sale_price - p.cost) : 0;
      var badges = "";
      if (p.low_stock) badges += '<span class="badge low">low</span>';
      if (p.has_variants) badges += '<span class="badge var">variants</span>';
      var sub = [];
      if (p.cost) sub.push("cost " + naira(p.cost));
      if (p.sale_price) sub.push("price " + naira(p.sale_price));
      if (margin) sub.push("margin " + naira(margin));
      var div = document.createElement("div");
      // Variant (tree) products are edited per-leaf in chat; the web edit sheet
      // targets product-level fields, so only tap-to-edit non-variant products
      // for now (avoids ambiguous which-leaf writes).
      div.className = "item" + (p.has_variants ? "" : " tappable");
      div.innerHTML = '<div><div class="name">' + escapeHtml(p.name || "?") + badges +
        '</div><div class="meta">' + (sub.join(" / ") || "no price/cost set") + '</div></div>' +
        '<div class="right"><div class="stock">' + Number(p.stock||0).toLocaleString() +
        ' ' + escapeHtml(p.unit || "") + '</div><div class="meta">' +
        (p.stock_value ? naira(p.stock_value) : "") + '</div></div>';
      if (!p.has_variants) div.onclick = function () { openSheet(p); };
      list.appendChild(div);
    });
    document.getElementById("invmsg").textContent = rows.length + " product(s)";
  };
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // ── Edit sheet (M6a write) ──
  var editing = null;   // the product being edited
  function apiPost(sub, body) {
    return fetch(BASE + "/" + sub, {
      method: "POST",
      headers: { "X-Telegram-Init-Data": initData, "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || !j.ok) throw new Error((j && j.error) || ("Error " + r.status));
        return j;
      });
    });
  }
  window.openSheet = function (p) {
    editing = p;
    document.getElementById("sh-name").textContent = p.name || "Product";
    document.getElementById("sh-sub").textContent =
      "Stock in " + (p.unit || "units") + " · edits save to your catalog";
    document.getElementById("sh-stock").value = Number(p.stock || 0);
    document.getElementById("sh-price").value = p.sale_price ? Number(p.sale_price) : "";
    document.getElementById("sh-cost").value = p.cost ? Number(p.cost) : "";
    document.getElementById("sh-err").textContent = "";
    document.getElementById("sh-save").disabled = false;
    document.getElementById("overlay").classList.remove("hidden");
  };
  window.closeSheet = function () {
    document.getElementById("overlay").classList.add("hidden");
    editing = null;
  };
  window.bump = function (n) {
    var el = document.getElementById("sh-stock");
    el.value = Math.max(0, (parseInt(el.value, 10) || 0) + n);
  };
  window.saveSheet = function () {
    if (!editing) return;
    var key = editing.key;
    var newStock = Math.max(0, parseInt(document.getElementById("sh-stock").value, 10) || 0);
    var priceRaw = document.getElementById("sh-price").value;
    var costRaw = document.getElementById("sh-cost").value;
    var newPrice = priceRaw === "" ? null : Math.max(0, parseInt(priceRaw, 10) || 0);
    var newCost = costRaw === "" ? null : Math.max(0, parseInt(costRaw, 10) || 0);

    // Only send the fields that actually changed.
    var ops = [];
    if (newStock !== Number(editing.stock || 0))
      ops.push({ action: "set_stock", key: key, value: newStock });
    if (newPrice !== null && newPrice !== Number(editing.sale_price || 0))
      ops.push({ action: "set_price", key: key, value: newPrice });
    if (newCost !== null && newCost !== Number(editing.cost || 0))
      ops.push({ action: "set_cost", key: key, value: newCost });

    if (!ops.length) { closeSheet(); return; }

    var saveBtn = document.getElementById("sh-save");
    saveBtn.disabled = true;                    // double-tap guard
    document.getElementById("sh-err").textContent = "";

    // Apply sequentially; the last response carries the fully-updated row.
    var latest = null;
    ops.reduce(function (chain, op) {
      return chain.then(function () {
        return apiPost("api/product", op).then(function (j) { latest = j.product; });
      });
    }, Promise.resolve())
    .then(function () {
      // Patch the row in place from the server's truth, re-render, close.
      if (latest) {
        for (var i = 0; i < invData.length; i++) {
          if (invData[i].key === latest.key) { invData[i] = latest; break; }
        }
        renderInv();
      }
      closeSheet();
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    })
    .catch(function (e) {
      saveBtn.disabled = false;
      document.getElementById("sh-err").textContent = e.message || "Save failed";
    });
  };

  if (!initData) {
    document.getElementById("period").textContent = "";
    document.getElementById("dashmsg").innerHTML =
      '<span class="err">Open this from inside Telegram.</span>';
    return;
  }
  renderChips();
  loadSummary();
})();
</script>
</body>
</html>"""
