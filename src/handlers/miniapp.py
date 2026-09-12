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

        # ── Writes (M6a: catalog) ──
        if method == "POST" and path.endswith("/app/api/product"):
            return _product_write(event, user_id)
        # ── Writes (M6b: record a transaction) ──
        if method == "POST" and path.endswith("/app/api/transaction"):
            return _transaction_write(event, user_id)

        # ── Reads ──
        if method == "GET" and path.endswith("/app/api/summary"):
            return _summary(event, user_id)
        if method == "GET" and path.endswith("/app/api/inventory"):
            return _inventory(event, user_id)
        if method == "GET" and path.endswith("/app/api/charts"):
            return _charts(event, user_id)
        if method == "GET" and path.endswith("/app/api/tree"):
            return _tree(event, user_id)

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

    # Split payables (what you owe) into real suppliers (goods) vs expense
    # payees (rent, utilities…). Expense_payee is an explicit contact type;
    # legacy/unknown contacts fall back to the supplier side (no faked split).
    owed_suppliers = 0
    owed_expenses = 0
    try:
        for c in (db.get_all_creditors(user_id) or []):
            amt = int(c.get("amount", 0) or 0)
            if amt <= 0:
                continue
            if (c.get("type") or "").lower().strip() == "expense_payee":
                owed_expenses += amt
            else:
                owed_suppliers += amt
    except Exception:
        owed_suppliers = pos["payables"]
        owed_expenses = 0

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
            # Breakdown of what you owe, for CRM clarity in the app.
            "i_owe_suppliers": owed_suppliers,
            "i_owe_expenses": owed_expenses,
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


def _row_from_product(p: dict, cat=None) -> dict:
    """Map a normalized product into the grid-row shape the page expects.
    Shared by the inventory list and the write echo, so the UI can patch a row
    in place after a write with an identical shape.

    For a variant-TREE product, cost + stock value live on the leaves (product
    landing_cost is 0), so we roll them up via cat.tree_rollup — otherwise the
    app would show 'no price/cost set' even when leaves are costed."""
    cost = int(p.get("landing_cost") or 0)
    stock_value = int(p.get("_stock_value") or 0)
    if p.get("_has_tree") and cat is not None:
        try:
            roll = cat.tree_rollup(p)
            if roll.get("avg_cost"):
                cost = int(roll["avg_cost"])
            if roll.get("value"):
                stock_value = int(roll["value"])
        except Exception:
            pass
    return {
        "key": p.get("_key"),
        "name": p.get("name"),
        "category": p.get("category") or "",
        "unit": p.get("primary_unit") or "",
        "stock": int(p.get("stock") or 0),
        "cost": cost,
        "sale_price": int(p.get("sale_price") or 0),
        "reorder_level": int(p.get("reorder_level") or 0),
        "low_stock": bool(p.get("_is_low_stock")),
        "has_variants": bool(p.get("_has_tree") or p.get("_has_variants")),
        "stock_value": stock_value,
        "item_type": p.get("item_type") or "",
    }


def _inventory(event, user_id: str):
    """The normalized product grid — the same data the catalog shelf shows."""
    from services.database import Database
    from features.catalog import CatalogHandler

    db = Database()
    cat = CatalogHandler(None, db)
    products = cat._normalized_products(user_id) or []
    rows = [_row_from_product(p, cat) for p in products]

    # Stable, useful ordering: low-stock first, then by name.
    rows.sort(key=lambda r: (not r["low_stock"], (r["name"] or "").lower()))

    return _json(200, {"count": len(rows), "products": rows})


def _tree(event, user_id: str):
    """Variant-tree drill for the record-form product picker. Given a product
    key + a path (comma-separated ancestor values), returns the children at that
    node: {axis, children:[{value, stock, cost, is_leaf}], product, path}."""
    from services.database import Database
    from features.catalog import CatalogHandler

    qs = event.get("queryStringParameters") or {}
    key = (qs.get("key") or "").strip()
    path_str = (qs.get("path") or "").strip()
    if not key:
        return _json(400, {"error": "key required"})

    db = Database()
    cat = CatalogHandler(None, db)
    products = cat._get_products(user_id) or {}
    prod = products.get(key)
    if not isinstance(prod, dict):
        return _json(404, {"error": "product not found"})

    tree = prod.get("variant_tree") or {}
    if not tree.get("children"):
        return _json(200, {"product": key, "is_leaf": True, "axis": "", "children": [], "path": []})

    path = [p.strip() for p in path_str.split(",") if p.strip()] if path_str else []
    node = cat._vt_get_node(tree, path) if path else tree
    if node is None:
        return _json(404, {"error": "path not found"})

    children_dict = node.get("children") or {}
    if not children_dict:
        # Leaf
        return _json(200, {
            "product": key, "is_leaf": True, "path": path,
            "axis": "", "children": [],
            "stock": cat._as_int(node.get("stock"), 0),
            "cost": cat._as_int(node.get("cost"), 0),
        })

    axis = node.get("child_axis") or "Variant"
    kids = []
    for val, child in children_dict.items():
        is_child_leaf = not (child.get("children") or {})
        kids.append({
            "value": val,
            "stock": cat._vt_node_total(child) if not is_child_leaf else cat._as_int(child.get("stock"), 0),
            "cost": cat._as_int(child.get("cost"), 0) if is_child_leaf else 0,
            "is_leaf": is_child_leaf,
        })

    return _json(200, {"product": key, "is_leaf": False, "axis": axis,
                        "children": kids, "path": path})


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
    return _json(200, {"ok": True, "product": _row_from_product(updated, cat)})


def _transaction_write(event, user_id: str):
    """M6b — record a full sale/purchase/expense from the web (stateless).

    Body: {submit_id, type, amount, description, category?, quantity?, brand?,
           unit_cost?, landing_cost?, payment_method?, vendor?, variant?,
           catalog_product?, catalog_product_name?, is_service_job?, has_credit?,
           deposit_amount?, balance_owed?}
    Idempotent via submit_id (a retried POST returns the original tx, no double
    record). Reuses the shared engine (TransactionHandler.record_transaction_web).
    """
    from services.database import Database
    from features.transactions import TransactionHandler

    data = _parse_body(event)
    submit_id = str(data.get("submit_id") or "").strip()
    if not submit_id:
        return _json(400, {"error": "submit_id required"})
    tx_type = str(data.get("type") or "").strip()
    if tx_type not in ("sale", "purchase", "expense"):
        return _json(400, {"error": "invalid type"})
    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        return _json(400, {"error": "amount must be a number"})
    if amount <= 0:
        return _json(400, {"error": "amount must be greater than 0"})

    db = Database()

    # Idempotency: claim the submit_id BEFORE any side effect. A retry/double
    # POST short-circuits here and returns the original result.
    claimed, prior_tx = db.claim_web_submit(user_id, submit_id)
    if not claimed:
        return _json(200, {"ok": True, "transaction_id": prior_tx or "",
                           "duplicate": True})

    # Build tx_data for the shared engine (only pass through known fields).
    tx_data = {
        "type": tx_type,
        "amount": amount,
        "description": (data.get("description") or "Item").strip(),
        "category": data.get("category") or "Uncategorized",
        "quantity": data.get("quantity"),
        "brand": data.get("brand"),
        "unit_cost": data.get("unit_cost"),
        "landing_cost": data.get("landing_cost"),
        "payment_method": data.get("payment_method") or "cash",
        "vendor": (data.get("vendor") or "").strip(),
        "variant": data.get("variant"),
        "catalog_product": data.get("catalog_product"),
        "catalog_product_name": data.get("catalog_product_name"),
        "is_service_job": bool(data.get("is_service_job")),
        "has_credit": bool(data.get("has_credit")),
        "deposit_amount": data.get("deposit_amount"),
        "balance_owed": data.get("balance_owed"),
        "_name_handled": True,
    }

    # Stateless save: no session/categorizer/industry-fn needed by
    # record_transaction_web or the helpers it composes.
    tx = TransactionHandler(None, db, None, None)
    result = tx.record_transaction_web(user_id, tx_data)

    if result.get("ok"):
        db.mark_web_submit_recorded(user_id, submit_id, result.get("transaction_id", ""))
        return _json(200, result)
    # Save failed — status 400 so the client can show the error.
    return _json(400, result)


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
  <button class="btn save" id="recordBtn" style="width:100%;margin-bottom:12px" onclick="openRecord()">➕ Record a transaction</button>

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
      <div class="card"><div class="k">You owe</div><div class="v neg" id="iowe">—</div><div class="sub" id="iowebreak"></div></div>
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

  <!-- Record-transaction sheet (M6b) -->
  <div id="recOverlay" class="overlay hidden">
    <div class="sheet">
      <h2>Record a transaction</h2>
      <div class="sub2">Saved straight to your books.</div>
      <div class="chips" id="rec-type">
        <div class="chip active" data-t="sale" onclick="recType('sale')">💰 Sale</div>
        <div class="chip" data-t="purchase" onclick="recType('purchase')">📦 Purchase</div>
        <div class="chip" data-t="expense" onclick="recType('expense')">💸 Expense</div>
      </div>
      <!-- Product picker (sale/purchase) — tap to choose from your catalog. -->
      <div class="field" id="rec-prod-wrap">
        <label id="rec-desc-label">What did you sell?</label>
        <div class="step" id="rec-prod-btn" style="text-align:left;padding:11px 12px" onclick="openPicker()">
          <span id="rec-prod-text" style="color:var(--hint)">Tap to choose a product</span>
        </div>
        <input id="rec-desc" class="hidden" placeholder="e.g. Hilux">
      </div>
      <div class="field">
        <label id="rec-amount-label">Amount received (NGN)</label>
        <input id="rec-amount" type="number" inputmode="numeric" min="0" oninput="recBalanceHint()">
      </div>
      <div class="field" id="rec-qty-wrap">
        <label id="rec-qty-label">Quantity</label>
        <input id="rec-qty" type="number" inputmode="numeric" min="1" value="1">
        <div class="sub2">In the product's unit (set the unit in Catalog).</div>
      </div>
      <div class="field" id="rec-cost-wrap">
        <label id="rec-cost-label">Cost of goods (total, NGN) — optional</label>
        <input id="rec-cost" type="number" inputmode="numeric" min="0" placeholder="for accurate profit">
      </div>
      <div class="field">
        <label id="rec-who-label">Customer (optional)</label>
        <input id="rec-who" placeholder="name">
      </div>
      <div class="field">
        <label>Payment</label>
        <div class="chips" id="rec-pay">
          <div class="chip active" data-p="cash" onclick="recPay('cash')">💵 Cash</div>
          <div class="chip" data-p="transfer" onclick="recPay('transfer')">🏦 Transfer</div>
          <div class="chip" data-p="credit" onclick="recPay('credit')">📝 Credit</div>
          <div class="chip" data-p="part" onclick="recPay('part')">💳 Part</div>
        </div>
      </div>
      <div class="field hidden" id="rec-deposit-wrap">
        <label id="rec-deposit-label">Deposit paid now (NGN)</label>
        <input id="rec-deposit" type="number" inputmode="numeric" min="0" placeholder="amount paid so far" oninput="recBalanceHint()">
        <div class="sub2" id="rec-balance-hint"></div>
      </div>
      <div class="sheeterr" id="rec-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeRecord()">Cancel</button>
        <button class="btn save" id="rec-save" onclick="saveRecord()">Record</button>
      </div>
    </div>
  </div>

  <!-- Product / variant picker sheet -->
  <div id="pickOverlay" class="overlay hidden">
    <div class="sheet">
      <h2 id="pick-title">Choose a product</h2>
      <div class="sub2" id="pick-crumb"></div>
      <input class="search" id="pick-search" placeholder="Search..." oninput="pickFilter()">
      <div id="pick-list" style="max-height:50vh;overflow-y:auto"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closePicker()">Close</button>
      </div>
    </div>
  </div>

  <!-- Read-only variant detail viewer (tap a variant product in Inventory) -->
  <div id="varOverlay" class="overlay hidden">
    <div class="sheet">
      <h2 id="var-title">Product</h2>
      <div class="sub2" id="var-crumb"></div>
      <div id="var-list" style="max-height:55vh;overflow-y:auto"></div>
      <div class="sheeterr" id="var-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeVarView()">Close</button>
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
        var ib = document.getElementById("iowebreak");
        if (ib) {
          var sup = d.debt.i_owe_suppliers || 0, exp = d.debt.i_owe_expenses || 0;
          // Only show the split when both sides are present — a single-source
          // payable reads cleaner without the breakdown.
          if (sup > 0 && exp > 0) {
            ib.textContent = "Suppliers " + naira(sup) + " - Expenses " + naira(exp);
          } else {
            ib.textContent = "";
          }
        }
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
      // Every product is now tappable. Non-variant products open the edit sheet
      // (stock/price/cost). Variant (tree) products open a READ-ONLY variant
      // viewer that drills the tree (per-leaf stock/cost) — leaf editing still
      // lives in chat, so the web view avoids ambiguous which-leaf writes.
      div.className = "item tappable";
      div.innerHTML = '<div><div class="name">' + escapeHtml(p.name || "?") + badges +
        '</div><div class="meta">' + (sub.join(" / ") || "no price/cost set") + '</div></div>' +
        '<div class="right"><div class="stock">' + Number(p.stock||0).toLocaleString() +
        ' ' + escapeHtml(p.unit || "") +
        (p.has_variants ? ' ›' : '') + '</div><div class="meta">' +
        (p.stock_value ? naira(p.stock_value) : "") + '</div></div>';
      div.onclick = p.has_variants
        ? (function (prod) { return function () { openVarView(prod); }; })(p)
        : (function (prod) { return function () { openSheet(prod); }; })(p);
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

  // ── Read-only variant viewer (tap a variant product in Inventory) ──
  var varProd = null;      // the product being viewed
  var varPath = [];        // current drill path (ancestor values)
  window.openVarView = function (p) {
    varProd = p; varPath = [];
    document.getElementById("var-title").textContent = p.name || "Product";
    document.getElementById("var-err").textContent = "";
    document.getElementById("varOverlay").classList.remove("hidden");
    drillVarView();
  };
  window.closeVarView = function () {
    document.getElementById("varOverlay").classList.add("hidden");
    varProd = null; varPath = [];
  };
  function drillVarView() {
    var list = document.getElementById("var-list");
    var crumb = document.getElementById("var-crumb");
    var err = document.getElementById("var-err");
    err.textContent = "";
    crumb.textContent = (varProd.name || "") + (varPath.length ? " › " + varPath.join(" › ") : "");
    list.innerHTML = '<div class="muted">Loading…</div>';
    api("api/tree?key=" + encodeURIComponent(varProd.key) +
        "&path=" + encodeURIComponent(varPath.join(",")))
      .then(function (d) {
        list.innerHTML = "";
        // Up-one-level row when drilled in.
        if (varPath.length) {
          var up = document.createElement("div");
          up.className = "item tappable";
          up.innerHTML = '<div class="name">⬆️ Up one level</div>';
          up.onclick = function () { varPath.pop(); drillVarView(); };
          list.appendChild(up);
        }
        var kids = d.children || [];
        if (!kids.length) {
          // A leaf reached directly — show its stock/cost.
          var leaf = document.createElement("div");
          leaf.className = "item";
          leaf.innerHTML = '<div class="name">Leaf</div><div class="meta">' +
            Number(d.stock||0).toLocaleString() + ' in stock' +
            (d.cost ? ' · cost ' + naira(d.cost) : '') + '</div>';
          list.appendChild(leaf);
        }
        if (d.axis) {
          var ax = document.createElement("div");
          ax.className = "sub2"; ax.style.margin = "4px 0";
          ax.textContent = d.axis;
          list.appendChild(ax);
        }
        kids.forEach(function (c) {
          var row = document.createElement("div");
          row.className = "item" + (c.is_leaf ? "" : " tappable");
          var meta = c.is_leaf
            ? (Number(c.stock||0).toLocaleString() + " in stock"
               + (c.cost ? " · cost " + naira(c.cost) : ""))
            : (Number(c.stock||0).toLocaleString() + " total →");
          row.innerHTML = '<div><div class="name">' + escapeHtml(c.value) +
            '</div><div class="meta">' + meta + '</div></div>';
          if (!c.is_leaf) {
            row.onclick = (function (val) {
              return function () { varPath.push(val); drillVarView(); };
            })(c.value);
          }
          list.appendChild(row);
        });
      })
      .catch(function (e) {
        list.innerHTML = "";
        err.textContent = e.message || "Could not load variants";
      });
  }
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

  // ── Record a transaction (M6b) ──
  var recTypeVal = "sale", recPayVal = "cash", recSubmitId = null;
  // Picked catalog product for the record form.
  var pick = { key: null, name: null, variant: null };
  var pickPath = [];   // current drill path while picking a variant
  var pickMode = "products";  // "products" | "tree"

  window.openPicker = function () {
    // Expenses don't use the catalog picker.
    if (recTypeVal === "expense") return;
    pickMode = "products"; pickPath = [];
    document.getElementById("pick-title").textContent = "Choose a product";
    document.getElementById("pick-crumb").textContent = "";
    document.getElementById("pick-search").value = "";
    document.getElementById("pick-search").style.display = "";
    document.getElementById("pickOverlay").classList.remove("hidden");
    if (!invData) { loadInventory(); setTimeout(renderPickerProducts, 400); }
    else renderPickerProducts();
  };
  window.closePicker = function () {
    document.getElementById("pickOverlay").classList.add("hidden");
  };
  window.pickFilter = function () { if (pickMode === "products") renderPickerProducts(); };

  function renderPickerProducts() {
    var q = (document.getElementById("pick-search").value || "").toLowerCase().trim();
    var list = document.getElementById("pick-list");
    var rows = (invData || []).filter(function (p) {
      return !q || (p.name || "").toLowerCase().indexOf(q) >= 0;
    });
    list.innerHTML = "";
    if (!rows.length) { list.innerHTML = '<div class="muted">No products. Add one in chat first.</div>'; return; }
    rows.forEach(function (p) {
      var d = document.createElement("div");
      d.className = "item tappable";
      d.innerHTML = '<div><div class="name">' + escapeHtml(p.name) +
        (p.has_variants ? ' <span class="badge var">variants</span>' : '') +
        '</div><div class="meta">' + Number(p.stock||0).toLocaleString() + ' ' + escapeHtml(p.unit||'') + ' in stock</div></div>';
      d.onclick = function () { pickProduct(p); };
      list.appendChild(d);
    });
  }

  function pickProduct(p) {
    if (!p.has_variants) {
      // Simple product — done.
      pick = { key: p.key, name: p.name, variant: null };
      applyPick();
      closePicker();
      return;
    }
    // Variant product — drill the tree.
    pick = { key: p.key, name: p.name, variant: null };
    pickMode = "tree"; pickPath = [];
    document.getElementById("pick-search").style.display = "none";
    drillTree();
  }

  function drillTree() {
    document.getElementById("pick-title").textContent = pick.name;
    document.getElementById("pick-crumb").textContent =
      pickPath.length ? pickPath.join(" / ") : "Choose a variant";
    var list = document.getElementById("pick-list");
    list.innerHTML = '<div class="muted">Loading...</div>';
    var url = "api/tree?key=" + encodeURIComponent(pick.key) +
      (pickPath.length ? "&path=" + encodeURIComponent(pickPath.join(",")) : "");
    api(url).then(function (d) {
      list.innerHTML = "";
      // Back-up-one row when drilled in.
      if (pickPath.length) {
        var up = document.createElement("div");
        up.className = "item tappable";
        up.innerHTML = '<div class="name">⬆️ Up one level</div>';
        up.onclick = function () { pickPath.pop(); drillTree(); };
        list.appendChild(up);
      }
      (d.children || []).forEach(function (c) {
        var d2 = document.createElement("div");
        d2.className = "item tappable";
        var meta = c.is_leaf ? (Number(c.stock||0).toLocaleString() + " in stock"
                                + (c.cost ? " · cost " + naira(c.cost) : ""))
                             : (Number(c.stock||0).toLocaleString() + " total →");
        d2.innerHTML = '<div><div class="name">' + escapeHtml(c.value) + '</div>' +
          '<div class="meta">' + meta + '</div></div>';
        d2.onclick = function () {
          pickPath.push(c.value);
          if (c.is_leaf) { pick.variant = pickPath.join(" / "); applyPick(); closePicker(); }
          else drillTree();
        };
        list.appendChild(d2);
      });
      if (!(d.children || []).length) {
        // Reached a leaf node directly — treat current path as the variant.
        pick.variant = pickPath.join(" / "); applyPick(); closePicker();
      }
    }).catch(function (e) {
      list.innerHTML = '<div class="err">' + (e.message || "Could not load") + '</div>';
    });
  }

  function applyPick() {
    var label = pick.name + (pick.variant ? " — " + pick.variant : "");
    var el = document.getElementById("rec-prod-text");
    el.textContent = label;
    el.style.color = "var(--text)";
  }

  function uuid() {
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0, v = c === "x" ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }
  function recSyncLabels() {
    var t = recTypeVal;
    document.getElementById("rec-desc-label").textContent =
      t === "sale" ? "What did you sell?" : (t === "purchase" ? "What did you buy?" : "What was it for?");
    document.getElementById("rec-amount-label").textContent =
      t === "sale" ? "Amount received (NGN)" : (t === "purchase" ? "Amount paid (NGN)" : "Amount (NGN)");
    // Sale/purchase pick from the catalog; expense is free text.
    var isExpense = (t === "expense");
    document.getElementById("rec-prod-btn").classList.toggle("hidden", isExpense);
    var descInput = document.getElementById("rec-desc");
    descInput.classList.toggle("hidden", !isExpense);
    if (isExpense) descInput.placeholder = "e.g. Fuel, Rent";
    // Cost + quantity + who only make sense for sale/purchase.
    document.getElementById("rec-qty-wrap").style.display = isExpense ? "none" : "";
    document.getElementById("rec-cost-wrap").style.display = (t === "sale") ? "" : "none";
    document.getElementById("rec-who-label").textContent =
      t === "purchase" ? "Supplier (optional)" : (t === "sale" ? "Customer (optional)" : "Paid to (optional)");
    // Part payment (deposit + balance) doesn't apply to expenses — hide that chip
    // and fall back to cash if it was selected.
    var partChip = document.querySelector('#rec-pay .chip[data-p="part"]');
    if (partChip) partChip.classList.toggle("hidden", isExpense);
    if (isExpense && recPayVal === "part") recPay("cash");
  }
  window.recType = function (t) {
    recTypeVal = t;
    // Reset the picked product when switching type.
    pick = { key: null, name: null, variant: null };
    document.getElementById("rec-prod-text").textContent = "Tap to choose a product";
    document.getElementById("rec-prod-text").style.color = "var(--hint)";
    var chips = document.querySelectorAll("#rec-type .chip");
    chips.forEach(function (c) { c.classList.toggle("active", c.getAttribute("data-t") === t); });
    recSyncLabels();
  };
  window.recPay = function (p) {
    recPayVal = p;
    var chips = document.querySelectorAll("#rec-pay .chip");
    chips.forEach(function (c) { c.classList.toggle("active", c.getAttribute("data-p") === p); });
    // Show the deposit field only for a Part payment (deposit now + balance owed).
    var isPart = (p === "part");
    document.getElementById("rec-deposit-wrap").classList.toggle("hidden", !isPart);
    if (isPart) recBalanceHint();
  };
  // Live "balance owed" preview under the deposit field.
  window.recBalanceHint = function () {
    var amount = parseInt(document.getElementById("rec-amount").value, 10) || 0;
    var dep = parseInt(document.getElementById("rec-deposit").value, 10) || 0;
    var hint = document.getElementById("rec-balance-hint");
    if (!amount) { hint.textContent = ""; return; }
    var bal = Math.max(0, amount - dep);
    hint.textContent = dep >= amount
      ? "Fully paid — this will record as paid, not part."
      : ("Balance owed: NGN " + bal.toLocaleString("en-NG"));
  };
  window.openRecord = function () {
    recTypeVal = "sale"; recPayVal = "cash"; recSubmitId = uuid();
    pick = { key: null, name: null, variant: null };
    document.getElementById("rec-prod-text").textContent = "Tap to choose a product";
    document.getElementById("rec-prod-text").style.color = "var(--hint)";
    recType("sale"); recPay("cash");
    document.getElementById("rec-desc").value = "";
    document.getElementById("rec-amount").value = "";
    document.getElementById("rec-qty").value = "1";
    document.getElementById("rec-cost").value = "";
    document.getElementById("rec-who").value = "";
    document.getElementById("rec-deposit").value = "";
    document.getElementById("rec-deposit-wrap").classList.add("hidden");
    document.getElementById("rec-balance-hint").textContent = "";
    document.getElementById("rec-err").textContent = "";
    document.getElementById("rec-save").disabled = false;
    document.getElementById("recOverlay").classList.remove("hidden");
  };
  window.closeRecord = function () {
    document.getElementById("recOverlay").classList.add("hidden");
  };
  window.saveRecord = function () {
    var isExpense = (recTypeVal === "expense");
    // Description: expense = free text; sale/purchase = picked product name.
    var desc = isExpense
      ? (document.getElementById("rec-desc").value || "").trim()
      : (pick.name || "");
    var amount = parseInt(document.getElementById("rec-amount").value, 10) || 0;
    var qty = Math.max(1, parseInt(document.getElementById("rec-qty").value, 10) || 1);
    var cost = parseInt(document.getElementById("rec-cost").value, 10) || 0;
    var who = (document.getElementById("rec-who").value || "").trim();
    var err = document.getElementById("rec-err");
    err.textContent = "";
    if (!isExpense && !pick.key) { err.textContent = "Please choose a product."; return; }
    if (isExpense && !desc) { err.textContent = "Please enter what it was for."; return; }
    if (amount <= 0) { err.textContent = "Please enter an amount."; return; }

    // Part payment = deposit now + balance owed. If the deposit covers the full
    // amount, treat it as a normal (paid) transfer — mirrors the chat flow.
    var isPart = recPayVal === "part";
    var deposit = isPart ? (parseInt(document.getElementById("rec-deposit").value, 10) || 0) : 0;
    if (isPart && deposit >= amount) { isPart = false; recPayVal = "transfer"; }
    var isCredit = (recPayVal === "credit") || isPart;  // both create a debt

    if (isCredit && !who) {
      err.textContent = recTypeVal === "purchase"
        ? "A credit/part purchase needs a supplier name."
        : "A credit/part sale needs a customer name.";
      return;
    }
    if (isPart && deposit <= 0) {
      err.textContent = "Enter the deposit paid now (or choose Credit for nothing paid).";
      return;
    }

    var body = {
      submit_id: recSubmitId, type: recTypeVal, amount: amount,
      description: desc,
      payment_method: isPart ? "deposit" : recPayVal,
      vendor: who, has_credit: isCredit,
    };
    if (isPart) {
      body.deposit_amount = deposit;
      body.balance_owed = amount - deposit;
    }
    if (!isExpense) {
      body.quantity = String(qty);
      if (pick.key) { body.catalog_product = pick.key; body.catalog_product_name = pick.name; }
      if (pick.variant) body.variant = pick.variant;
    }
    if (recTypeVal === "sale" && cost > 0) body.landing_cost = cost;
    var btn = document.getElementById("rec-save");
    btn.disabled = true;
    apiPost("api/transaction", body)
      .then(function () {
        closeRecord();
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        // Refresh dashboard + inventory so the new numbers show.
        loadSummary();
        invLoaded = false;
        if (!document.getElementById("view-inv").classList.contains("hidden")) loadInventory();
      })
      .catch(function (e) {
        btn.disabled = false;
        err.textContent = e.message || "Could not record";
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
