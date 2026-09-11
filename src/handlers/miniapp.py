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


def lambda_handler(event, context):
    """Route the Mini App API request."""
    try:
        path = (event.get("path") or event.get("rawPath") or "").rstrip("/")
        method = (event.get("httpMethod")
                  or (event.get("requestContext", {}) or {}).get("http", {}).get("method")
                  or "GET").upper()

        if method != "GET":
            return _json(405, {"error": "method not allowed"})

        # Every data route requires a valid Telegram signature.
        user_id, err = _authenticate(event)
        if err is not None:
            return err

        if path.endswith("/app/api/summary"):
            return _summary(event, user_id)
        if path.endswith("/app/api/inventory"):
            return _inventory(event, user_id)

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


def _inventory(event, user_id: str):
    """The normalized product grid — the same data the catalog shelf shows."""
    from services.database import Database
    from features.catalog import CatalogHandler

    db = Database()
    cat = CatalogHandler(None, db)
    products = cat._normalized_products(user_id) or []

    rows = []
    for p in products:
        rows.append({
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
        })

    # Stable, useful ordering: low-stock first, then by name.
    rows.sort(key=lambda r: (not r["low_stock"], (r["name"] or "").lower()))

    return _json(200, {"count": len(rows), "products": rows})
