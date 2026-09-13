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

# Period keys the Mini App may request. All are resolved by
# features.reports._date_range (single source of truth for boundaries), so the
# web numbers always match the chat dashboard for the same range.
VALID_PERIODS = ("today", "week", "month", "last_month", "quarter", "year")

_DATE_RE = None  # compiled lazily


def _resolve_range(qs: dict, period: str):
    """Resolve (start, end, label) for a request. If the query supplies a valid
    custom `from` (and optional `to`) date (YYYY-MM-DD), use that EXACT range —
    a single day when from==to (or to omitted), else a span. Otherwise fall back
    to the named period via reports._date_range. Dates pass straight to the
    accounting engine (which already takes start/end dates), so custom ranges are
    as accurate as the presets — no new math."""
    import re
    from datetime import datetime as _dt
    from features.reports import _date_range

    global _DATE_RE
    if _DATE_RE is None:
        _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    frm = (qs.get("from") or "").strip()
    to = (qs.get("to") or "").strip() or frm  # single day when 'to' omitted

    def _valid(d):
        if not _DATE_RE.match(d):
            return False
        try:
            _dt.strptime(d, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    if _valid(frm) and _valid(to):
        # Normalise ordering so a swapped range still works.
        if to < frm:
            frm, to = to, frm

        def _pretty(d):
            return _dt.strptime(d, "%Y-%m-%d").strftime("%-d %b %Y")

        try:
            label = _pretty(frm) if frm == to else (_pretty(frm) + " - " + _pretty(to))
        except ValueError:
            label = frm if frm == to else (frm + " - " + to)
        return frm, to, label

    return _date_range(period)


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
        # ── Writes (CRM: record a debt payment / collection) ──
        if method == "POST" and path.endswith("/app/api/debt-payment"):
            return _debt_payment_write(event, user_id)

        # ── Reads ──
        if method == "GET" and path.endswith("/app/api/summary"):
            return _summary(event, user_id)
        if method == "GET" and path.endswith("/app/api/inventory"):
            return _inventory(event, user_id)
        if method == "GET" and path.endswith("/app/api/contacts"):
            return _contacts(event, user_id)
        if method == "GET" and path.endswith("/app/api/records"):
            return _records(event, user_id)
        if method == "GET" and path.endswith("/app/api/export"):
            return _export(event, user_id)
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
    if period not in VALID_PERIODS:
        period = "month"

    db = Database()
    acct = Accounting(db)
    start, end, label = _resolve_range(qs, period)

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


def _contacts(event, user_id: str):
    """CRM data for the app: who owes me (debtors) + who I owe (creditors),
    split payables into suppliers vs expense payees, and top customers/suppliers.
    Reuses the SAME engine accessors as the chat debt board + CRM, so the numbers
    match. Read-only."""
    from services.database import Database
    db = Database()

    debtors = db.get_all_debtors(user_id) or []
    creditors = db.get_all_creditors(user_id) or []

    def _clean(lst):
        out = []
        for c in lst:
            out.append({
                "name": c.get("name", "Unknown"),
                "amount": int(c.get("amount", 0) or 0),
                "type": (c.get("type") or "").lower().strip(),
                "last_date": c.get("last_date", ""),
                "due_date": c.get("due_date", ""),
            })
        return out

    debtors = _clean(debtors)
    creditors = _clean(creditors)
    owed_to_me = sum(c["amount"] for c in debtors)
    i_owe = sum(c["amount"] for c in creditors)
    owe_suppliers = sum(c["amount"] for c in creditors if c["type"] != "expense_payee")
    owe_expenses = sum(c["amount"] for c in creditors if c["type"] == "expense_payee")

    # Full contact directory (customers + suppliers + both) with details, so
    # the app can list everyone and show a detail card — not just debtors.
    try:
        contacts = db.get_contacts(user_id, limit=100) or []
    except Exception:
        contacts = []

    def _person(c):
        ctype = (c.get("type") or "").lower().strip()
        received = int(c.get("total_received", 0) or 0)  # money IN (customer)
        paid = int(c.get("total_paid", 0) or 0)          # money OUT (supplier)
        return {
            "name": c.get("name", c.get("contact_id", "Unknown")),
            "type": ctype or "contact",
            "phone": c.get("contact_phone", "") or "",
            "total_received": received,
            "total_paid": paid,
            "spend": received,   # what a CUSTOMER has spent with you
            "transactions": int(c.get("transaction_count", 0) or 0),
            "last_date": c.get("last_transaction_date", "") or "",
            "owes_me": int(c.get("debt_owed_to_me", 0) or 0),
            "i_owe": int(c.get("debt_i_owe", 0) or 0),
        }

    people = [_person(c) for c in contacts]
    # Customers = anyone who has bought (customer/both, or has received-total).
    customers = [p for p in people
                 if p["type"] in ("customer", "both") or p["total_received"] > 0]
    suppliers = [p for p in people
                 if p["type"] in ("supplier", "expense_payee", "both") or p["total_paid"] > 0]
    customers.sort(key=lambda p: p["total_received"], reverse=True)
    suppliers.sort(key=lambda p: p["total_paid"], reverse=True)

    return _json(200, {
        "owed_to_me": owed_to_me,
        "i_owe": i_owe,
        "i_owe_suppliers": owe_suppliers,
        "i_owe_expenses": owe_expenses,
        "debtors": debtors,       # people who owe ME (collect)
        "creditors": creditors,   # people I owe (repay)
        "customers": customers,   # full customer directory (with details)
        "suppliers": suppliers,   # full supplier directory
    })


def _records(event, user_id: str):
    """Period/date-scoped transaction LIST for the Records view. Same window
    resolver as the dashboard (_resolve_range: named period OR custom from/to,
    single day when to==from), filtered by type. Paginated (limit/offset).
    Returns rows the UI renders + a running total for the whole (unpaged) set."""
    from services.database import Database
    from utils.parser import is_bad_vendor

    qs = event.get("queryStringParameters") or {}
    tx_type = (qs.get("type") or "sale").lower()
    if tx_type not in ("sale", "purchase", "expense"):
        tx_type = "sale"
    period = (qs.get("period") or "month").lower()
    if period not in VALID_PERIODS:
        period = "month"
    try:
        limit = max(1, min(200, int(qs.get("limit") or 50)))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(qs.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0

    start, end, label = _resolve_range(qs, period)

    db = Database()
    all_txns = db.get_transactions_by_period(user_id, start, end) or []
    rows = [t for t in all_txns if t.get("type") == tx_type]
    # Newest first.
    rows.sort(key=lambda t: t.get("created_at", t.get("date", "")), reverse=True)

    total = sum(int(t.get("amount", 0) or 0) for t in rows)
    count = len(rows)
    page = rows[offset:offset + limit]

    def _desc(t):
        item = (t.get("item_name") or "").strip()
        brand = (t.get("brand") or "").strip()
        if item and brand and not item.lower().startswith(brand.lower()):
            return (brand + " " + item)[:40]
        if item:
            return item[:40]
        return (t.get("description") or t.get("raw_text") or "Transaction")[:40]

    out = []
    for t in page:
        vendor = t.get("vendor", "") or ""
        if is_bad_vendor(vendor):
            vendor = ""
        out.append({
            "desc": _desc(t),
            "amount": int(t.get("amount", 0) or 0),
            "vendor": vendor,
            "date": t.get("date", ""),
            "qty": t.get("quantity", ""),
        })

    return _json(200, {
        "type": tx_type, "period": period, "period_label": label,
        "total": total, "count": count,
        "offset": offset, "limit": limit,
        "has_more": (offset + limit) < count,
        "records": out,
    })


def _export(event, user_id: str):
    """Build a period/date-scoped, per-type transaction-LIST export (Excel or
    PDF) and return a presigned download URL. Reuses the SAME exporter as chat
    (export_service.handle_filtered_export) + the SAME window resolver, so the
    file matches the Records view. Returns {ok, url, filename} — the browser
    opens/downloads the URL."""
    from services.database import Database
    from services.export_service import ExportService

    qs = event.get("queryStringParameters") or {}
    tx_type = (qs.get("type") or "sale").lower()
    if tx_type not in ("sale", "purchase", "expense"):
        tx_type = "sale"
    period = (qs.get("period") or "month").lower()
    if period not in VALID_PERIODS:
        period = "month"
    fmt = (qs.get("fmt") or "excel").lower()
    if fmt not in ("excel", "pdf"):
        fmt = "excel"

    # PDF is a paid feature (mirror chat). Excel stays open.
    if fmt == "pdf":
        try:
            from services.tier_manager import TierManager
            allowed, msg = TierManager(database=Database()).check_can_generate_pdf(user_id)
            if not allowed:
                return _json(403, {"error": "pdf_paywalled",
                                   "message": msg or "PDF export is a Basic/Pro feature."})
        except Exception:
            pass

    start, end, label = _resolve_range(qs, period)
    filter_map = {"sale": "my_sales", "purchase": "my_purchases",
                  "expense": "my_expenses"}

    db = Database()
    svc = ExportService(database=db)
    try:
        # Reuse the SAME exporter as chat. It BUILDS the file and DELIVERS it to
        # the user's Telegram chat (the file lands where they can save/forward
        # it) — cleaner + more reliable than handing a raw presigned S3 URL to
        # an in-app browser. The app just confirms it was sent.
        resp = svc.handle_filtered_export(
            user_id, filter_map.get(tx_type, "my_sales"),
            start, end, label, fmt)
        # handle_filtered_export returns a chat response list; surface a concise
        # status to the app.
        txt = ""
        if isinstance(resp, list) and resp:
            txt = (resp[0].get("content") or "")
        empty = "No " in txt and "to export" in txt
        return _json(200, {
            "ok": not empty,
            "delivered_to_chat": not empty,
            "empty": empty,
            "message": txt or ("Your %s export was sent to your chat." % tx_type),
        })
    except Exception as e:
        logger.error(f"miniapp export failed: {e}")
        return _json(500, {"error": "export failed"})


def _debt_payment_write(event, user_id: str):
    """Record a debt payment from the app — a COLLECTION (a customer repays me)
    or a REPAYMENT (I pay a supplier). Mirrors the chat debt board EXACTLY:
    settle_debt (reduce the balance) + save_transaction (log the cash move), so
    cash flow + the balance both stay correct. Idempotent via submit_id.

    Body: {submit_id, name, amount, direction: 'in'|'out'}
      in  = a debtor pays me   -> settle owed_to_me + log income (sale)
      out = I repay a creditor -> settle i_owe      + log expense
    """
    from services.database import Database

    data = _parse_body(event)
    submit_id = str(data.get("submit_id") or "").strip()
    if not submit_id:
        return _json(400, {"error": "submit_id required"})
    name = (data.get("name") or "").strip()
    direction = (data.get("direction") or "").strip().lower()
    try:
        amount = int(data.get("amount") or 0)
    except (TypeError, ValueError):
        return _json(400, {"error": "amount must be a number"})
    if not name:
        return _json(400, {"error": "a contact name is required"})
    if direction not in ("in", "out"):
        return _json(400, {"error": "direction must be 'in' or 'out'"})
    if amount <= 0:
        return _json(400, {"error": "amount must be greater than 0"})

    db = Database()

    # Idempotency: claim BEFORE any side effect (mirrors _transaction_write).
    claimed, _prior = db.claim_web_submit(user_id, submit_id)
    if not claimed:
        return _json(200, {"ok": True, "duplicate": True})

    try:
        if direction == "in":
            remaining = db.settle_debt(user_id, name, float(amount), "owed_to_me")
            db.save_transaction(
                user_id, int(amount), "sale",
                f"Debt repayment from {name}", "Sales & Income",
                vendor=name, payment_method="cash",
                extra_details={"source": "miniapp", "debt_payment": True})
        else:
            remaining = db.settle_debt(user_id, name, float(amount), "i_owe")
            db.save_transaction(
                user_id, int(amount), "expense",
                f"Debt repayment to {name}", "Debt Repayment",
                vendor=name, payment_method="cash",
                extra_details={"source": "miniapp", "debt_payment": True})
        return _json(200, {
            "ok": True, "name": name, "direction": direction,
            "amount": amount, "remaining": int(remaining or 0),
        })
    except Exception as e:
        logger.error(f"miniapp debt payment failed: {e}")
        return _json(500, {"error": "could not record the payment"})


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
    if period not in VALID_PERIODS:
        period = "month"

    out = {"top_products": None, "profit_trend": None}
    try:
        from services.chart_renderer import bar_chart, trend_chart
    except Exception:
        # Charts optional — return nulls; the page hides the section.
        return _json(200, out)

    db = Database()
    acct = Accounting(db)
    start, end, label = _resolve_range(qs, period)

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
  h1 { font-size: 19px; font-weight: 700; margin: 2px 0 1px; }
  .sub { color: var(--hint); font-size: 13px; margin-bottom: 14px; }
  .card { background: var(--card); border-radius: 14px; padding: 14px; margin-bottom: 10px; }
  .k { color: var(--hint); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  /* Values can be long (₦80,000,000). Keep them on one line and let the
     browser shrink very long numbers rather than wrapping into a scatter. */
  .v { font-size: 22px; font-weight: 700; margin-top: 4px; white-space: nowrap;
       overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
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
  .seclabel { color: var(--hint); font-size: 11px; text-transform: uppercase;
    letter-spacing: .04em; margin: 16px 2px 6px; font-weight: 700; }
  .datebox { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 12px; }
  .datebox .df { flex: 1; min-width: 120px; }
  .datebox label { display:block; color: var(--hint); font-size: 11px; margin-bottom: 4px; }
  .datebox input { width: 100%; padding: 9px 10px; border-radius: 9px;
    border: 1px solid var(--line); background: var(--card); color: var(--text); font-size: 15px; }
  .datebox .apply { padding: 9px 14px; border-radius: 9px; border: none;
    background: var(--accent); color: var(--btntext); font-weight: 700; cursor: pointer; }
  .hidden { display: none; }
</style>
</head>
<body>
  <h1 id="biz">Kashia</h1>
  <div class="sub" id="period">Loading…</div>

  <div class="tabs">
    <div class="tab active" id="tab-dash" onclick="showTab('dash')">📊 Dashboard</div>
    <div class="tab" id="tab-cat" onclick="showTab('cat')">📦 Catalog</div>
    <div class="tab" id="tab-crm" onclick="showTab('crm')">👥 Customers</div>
    <div class="tab" id="tab-rec" onclick="showTab('rec')">📋 Records</div>
  </div>
  <button class="btn save" id="recordBtn" style="width:100%;margin-bottom:12px" onclick="openRecord()">➕ Record a transaction</button>

  <div id="view-dash">
    <div class="chips" id="chips"></div>
    <div class="datebox hidden" id="datebox">
      <div class="df"><label>From</label><input type="date" id="date-from"></div>
      <div class="df"><label>To (blank = single day)</label><input type="date" id="date-to"></div>
      <button class="apply" onclick="applyDateRange()">Apply</button>
      <div class="sheeterr" id="date-err" style="flex-basis:100%"></div>
    </div>

    <div class="seclabel" id="periodlabel">This period</div>
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

    <div class="seclabel">Current balances · as of today</div>
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

  <div id="view-cat" class="hidden">
    <div class="row">
      <div class="card"><div class="k">Products</div><div class="v" id="cat-count">—</div></div>
      <div class="card"><div class="k">Total stock</div><div class="v" id="cat-units">—</div></div>
    </div>
    <div class="card"><div class="k">Stock value (at cost)</div><div class="v" id="cat-value">—</div></div>
    <div class="card hidden" id="cat-lowcard"><div class="k">Low stock</div><div class="v neg" id="cat-low">—</div></div>
    <input class="search" id="catsearch" placeholder="Search catalog..." oninput="renderCatalog()">
    <div id="catgroups"><div class="muted">Loading...</div></div>
    <div id="catmsg" class="muted"></div>
  </div>

  <div id="view-crm" class="hidden">
    <div class="row">
      <div class="card"><div class="k">Owed to you</div><div class="v pos" id="crm-owed">—</div></div>
      <div class="card"><div class="k">You owe</div><div class="v neg" id="crm-iowe">—</div><div class="sub" id="crm-iowebreak"></div></div>
    </div>
    <div class="chips" id="crm-dir-tabs">
      <div class="chip active" data-cd="customers" onclick="crmSetDir('customers')">👤 Customers</div>
      <div class="chip" data-cd="suppliers" onclick="crmSetDir('suppliers')">🏭 Suppliers</div>
    </div>
    <input class="search" id="crmsearch" placeholder="Search people..." oninput="renderCrm()">
    <div id="crmlists"><div class="muted">Loading...</div></div>
    <div id="crmmsg" class="muted"></div>
  </div>

  <!-- Contact detail sheet -->
  <div id="cdOverlay" class="overlay hidden">
    <div class="sheet">
      <h2 id="cd-name">Contact</h2>
      <div class="sub2" id="cd-type"></div>
      <div class="row">
        <div class="card"><div class="k" id="cd-spend-k">Total spent</div><div class="v" id="cd-spend">—</div></div>
        <div class="card"><div class="k">Transactions</div><div class="v" id="cd-txns">—</div></div>
      </div>
      <div class="card"><div class="k">Last transaction</div><div class="v" id="cd-last" style="font-size:16px">—</div></div>
      <div class="card hidden" id="cd-debtcard">
        <div class="k" id="cd-debt-k">Balance</div><div class="v" id="cd-debt">—</div>
      </div>
      <div class="sub2 hidden" id="cd-phone"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeContact()">Close</button>
        <button class="btn save hidden" id="cd-pay" onclick="cdRecordPayment()">💵 Record payment</button>
      </div>
    </div>
  </div>

  <div id="view-rec" class="hidden">
    <div class="chips" id="rec-type-tabs">
      <div class="chip active" data-rt="sale" onclick="recSetType('sale')">💰 Sales</div>
      <div class="chip" data-rt="purchase" onclick="recSetType('purchase')">📦 Purchases</div>
      <div class="chip" data-rt="expense" onclick="recSetType('expense')">💸 Expenses</div>
    </div>
    <div class="chips" id="rec-chips"></div>
    <div class="datebox hidden" id="rec-datebox">
      <div class="df"><label>From</label><input type="date" id="rec-date-from"></div>
      <div class="df"><label>To (blank = single day)</label><input type="date" id="rec-date-to"></div>
      <button class="apply" onclick="recApplyDate()">Apply</button>
      <div class="sheeterr" id="rec-date-err" style="flex-basis:100%"></div>
    </div>
    <div class="card"><div class="k" id="rec-total-k">Total</div><div class="v" id="rec-total">—</div></div>
    <div class="row">
      <button class="btn save" style="flex:1" onclick="recExport('excel')">⬇️ Excel</button>
      <button class="btn cancel" style="flex:1" onclick="recExport('pdf')">🧾 PDF</button>
    </div>
    <div id="rec-list"><div class="muted">Loading...</div></div>
    <div id="rec-msg" class="muted"></div>
  </div>

  <!-- Debt-payment sheet (CRM write) -->
  <div id="payOverlay" class="overlay hidden">
    <div class="sheet">
      <h2 id="pay-title">Record a payment</h2>
      <div class="sub2" id="pay-sub"></div>
      <div class="field">
        <label id="pay-amount-label">Amount (\u20a6)</label>
        <input id="pay-amount" type="number" inputmode="numeric" min="0" oninput="payHint()">
        <div class="sub2" id="pay-hint"></div>
      </div>
      <div class="sheeterr" id="pay-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closePay()">Cancel</button>
        <button class="btn save" id="pay-save" onclick="savePay()">Record payment</button>
      </div>
    </div>
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
        <label>Selling price (\u20a6)</label>
        <input id="sh-price" type="number" inputmode="numeric" min="0">
      </div>
      <div class="field">
        <label>Cost per unit (\u20a6)</label>
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
        <label id="rec-amount-label">Amount received (\u20a6)</label>
        <input id="rec-amount" type="number" inputmode="numeric" min="0" oninput="recBalanceHint()">
      </div>
      <div class="field" id="rec-qty-wrap">
        <label id="rec-qty-label">Quantity</label>
        <input id="rec-qty" type="number" inputmode="numeric" min="1" value="1">
        <div class="sub2">In the product's unit (set the unit in Catalog).</div>
      </div>
      <div class="field" id="rec-cost-wrap">
        <label id="rec-cost-label">Cost of goods (total, \u20a6) — optional</label>
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
        <label id="rec-deposit-label">Deposit paid now (\u20a6)</label>
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
  var PERIODS = [["today","Today"],["week","Week"],["month","Month"],["last_month","Last month"],["quarter","Quarter"],["year","Year"]];
  var curPeriod = "month";
  var invData = null;
  var invLoaded = false;
  var crmData = null;
  var crmLoaded = false;
  // Custom date range (single day or range). When set, overrides curPeriod.
  var curFrom = "";
  var curTo = "";
  // Records tab state (independent period + range + type).
  var recType = "sale";
  var recPeriod = "month";
  var recFrom = "";
  var recTo = "";

  // Web page (not a PDF) so the ₦ glyph is safe and reads cleaner than "NGN".
  function naira(n) { return "\u20a6" + Number(n||0).toLocaleString("en-NG"); }
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
  // Build the date query for summary/charts: a custom from/to range when set,
  // else the named period. A single day = from==to.
  function periodQuery() {
    if (curFrom) {
      return "from=" + encodeURIComponent(curFrom) +
             "&to=" + encodeURIComponent(curTo || curFrom);
    }
    return "period=" + curPeriod;
  }

  window.showTab = function (which) {
    document.getElementById("tab-dash").classList.toggle("active", which === "dash");
    document.getElementById("tab-cat").classList.toggle("active", which === "cat");
    document.getElementById("tab-crm").classList.toggle("active", which === "crm");
    document.getElementById("tab-rec").classList.toggle("active", which === "rec");
    document.getElementById("view-dash").classList.toggle("hidden", which !== "dash");
    document.getElementById("view-cat").classList.toggle("hidden", which !== "cat");
    document.getElementById("view-crm").classList.toggle("hidden", which !== "crm");
    document.getElementById("view-rec").classList.toggle("hidden", which !== "rec");
    // "Record a transaction" belongs on the Dashboard only — it's noise on the
    // Catalog / Customers / Records views.
    var rb = document.getElementById("recordBtn");
    if (rb) rb.classList.toggle("hidden", which !== "dash");
    // Catalog is the single product tab (Inventory merged in). Load products the
    // first time it's opened, then render.
    if (which === "cat") {
      if (!invLoaded) { loadInventory(); } else { renderCatalog(); }
    } else if (which === "crm") {
      if (!crmLoaded) { loadCrm(); } else { renderCrm(); }
    } else if (which === "rec") {
      recRenderChips(); loadRecords();
    }
  };

  function renderChips() {
    var c = document.getElementById("chips");
    c.innerHTML = "";
    PERIODS.forEach(function (p) {
      var el = document.createElement("div");
      // A preset is active only when no custom range is set.
      el.className = "chip" + (!curFrom && p[0] === curPeriod ? " active" : "");
      el.textContent = p[1];
      el.onclick = function () {
        curFrom = ""; curTo = "";           // clear any custom range
        curPeriod = p[0];
        toggleDatePicker(false);
        renderChips(); loadSummary();
      };
      c.appendChild(el);
    });
    // Custom single-day / range picker chip.
    var pick = document.createElement("div");
    pick.className = "chip" + (curFrom ? " active" : "");
    pick.textContent = "📅 Pick date";
    pick.onclick = function () { toggleDatePicker(); };
    c.appendChild(pick);
  }
  function toggleDatePicker(show) {
    var box = document.getElementById("datebox");
    if (!box) return;
    var willShow = (show === undefined) ? box.classList.contains("hidden") : show;
    box.classList.toggle("hidden", !willShow);
  }
  window.applyDateRange = function () {
    var f = document.getElementById("date-from").value;
    var t = document.getElementById("date-to").value;
    var err = document.getElementById("date-err");
    if (!f) { err.textContent = "Pick at least a start date."; return; }
    err.textContent = "";
    curFrom = f;
    curTo = t || f;         // single day when 'to' left blank
    renderChips();
    loadSummary();
  };
  function loadSummary() {
    var msg = document.getElementById("dashmsg");
    msg.textContent = "";
    api("api/summary?" + periodQuery())
      .then(function (d) {
        document.getElementById("biz").textContent = d.business || "Kashia";
        document.getElementById("period").textContent = "\\ud83d\\udcc5 " + (d.period_label || "");
        var pl = document.getElementById("periodlabel");
        if (pl) pl.textContent = (d.period_label ? (d.period_label + " · this period") : "This period");
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
    api("api/charts?" + periodQuery())
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
      .then(function (d) { invData = d.products || []; renderCatalog(); })
      .catch(function (e) {
        document.getElementById("catmsg").innerHTML =
          '<span class="err">' + (e.message || "Could not load") + '</span>';
      });
  }

  // ── Catalog tab (Inventory merged in): totals up top, then products grouped
  // by category, searchable. Every product is tappable — non-variant opens the
  // edit sheet (stock/price/cost), variant opens the read-only tree viewer.
  window.renderCatalog = function () {
    if (!invData) return;
    var q = (document.getElementById("catsearch").value || "").toLowerCase().trim();
    var rows = invData.filter(function (p) {
      return !q || (p.name || "").toLowerCase().indexOf(q) >= 0
                || (p.category || "").toLowerCase().indexOf(q) >= 0;
    });

    // Totals (across the FULL catalog, not just the filtered view).
    var totUnits = 0, totValue = 0, lowCount = 0;
    invData.forEach(function (p) {
      totUnits += Number(p.stock || 0);
      totValue += Number(p.stock_value || 0);
      if (p.low_stock) lowCount += 1;
    });
    document.getElementById("cat-count").textContent = invData.length.toLocaleString();
    document.getElementById("cat-units").textContent = totUnits.toLocaleString();
    document.getElementById("cat-value").textContent = naira(totValue);
    var lowCard = document.getElementById("cat-lowcard");
    if (lowCount > 0) {
      document.getElementById("cat-low").textContent = lowCount + " item(s)";
      lowCard.classList.remove("hidden");
    } else {
      lowCard.classList.add("hidden");
    }

    var wrap = document.getElementById("catgroups");
    if (!rows.length) { wrap.innerHTML = '<div class="muted">No products.</div>'; return; }

    // Group by category (blank category → "Uncategorized").
    var groups = {};
    rows.forEach(function (p) {
      var key = (p.category || "").trim() || "Uncategorized";
      (groups[key] = groups[key] || []).push(p);
    });
    var names = Object.keys(groups).sort(function (a, b) {
      if (a === "Uncategorized") return 1;
      if (b === "Uncategorized") return -1;
      return a.toLowerCase() < b.toLowerCase() ? -1 : 1;
    });

    wrap.innerHTML = "";
    names.forEach(function (cat) {
      var items = groups[cat];
      var gUnits = 0, gValue = 0;
      items.forEach(function (p) { gUnits += Number(p.stock || 0); gValue += Number(p.stock_value || 0); });
      var head = document.createElement("div");
      head.className = "k";
      head.style.margin = "14px 2px 6px";
      head.textContent = cat + " · " + items.length + " item(s) · " + naira(gValue);
      wrap.appendChild(head);

      var card = document.createElement("div");
      card.className = "card";
      card.style.padding = "4px 0";
      items.forEach(function (p) {
        var badges = "";
        if (p.low_stock) badges += '<span class="badge low">low</span>';
        if (p.has_variants) badges += '<span class="badge var">variants</span>';
        var sub = [];
        if (p.cost) sub.push("cost " + naira(p.cost));
        if (p.sale_price) sub.push("price " + naira(p.sale_price));
        var div = document.createElement("div");
        div.className = "item tappable";
        div.innerHTML = '<div><div class="name">' + escapeHtml(p.name || "?") + badges +
          '</div><div class="meta">' + (sub.join(" / ") || "no price/cost set") + '</div></div>' +
          '<div class="right"><div class="stock">' + Number(p.stock||0).toLocaleString() +
          ' ' + escapeHtml(p.unit || "") + (p.has_variants ? ' ›' : '') + '</div><div class="meta">' +
          (p.stock_value ? naira(p.stock_value) : "") + '</div></div>';
        div.onclick = p.has_variants
          ? (function (prod) { return function () { openVarView(prod); }; })(p)
          : (function (prod) { return function () { openSheet(prod); }; })(p);
        card.appendChild(div);
      });
      wrap.appendChild(card);
    });
    document.getElementById("catmsg").textContent = rows.length + " product(s)";
  };
  // Inventory was merged into Catalog; keep renderInv as an alias so post-write
  // refreshes (edit sheet save) re-render the single Catalog view.
  window.renderInv = function () { renderCatalog(); };

  // ── CRM tab: who owes me (collect) + who I owe (repay). Tap a person to
  // record a payment (reuses the shared debt engine via api/debt-payment).
  function loadCrm() {
    crmLoaded = true;
    api("api/contacts")
      .then(function (d) { crmData = d; renderCrm(); })
      .catch(function (e) {
        document.getElementById("crmmsg").innerHTML =
          '<span class="err">' + (e.message || "Could not load") + '</span>';
      });
  }
  var crmDir = "customers";   // which directory the CRM tab shows
  window.crmSetDir = function (d) {
    crmDir = d;
    var tabs = document.getElementById("crm-dir-tabs").children;
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("active", tabs[i].getAttribute("data-cd") === d);
    }
    renderCrm();
  };
  window.renderCrm = function () {
    if (!crmData) return;
    var q = (document.getElementById("crmsearch").value || "").toLowerCase().trim();
    document.getElementById("crm-owed").textContent = naira(crmData.owed_to_me || 0);
    document.getElementById("crm-iowe").textContent = naira(crmData.i_owe || 0);
    var ib = document.getElementById("crm-iowebreak");
    var sup = crmData.i_owe_suppliers || 0, exp = crmData.i_owe_expenses || 0;
    ib.textContent = (sup > 0 && exp > 0)
      ? ("Suppliers " + naira(sup) + " - Expenses " + naira(exp)) : "";

    var list = (crmDir === "suppliers" ? crmData.suppliers : crmData.customers) || [];
    list = list.filter(function (c) {
      return !q || (c.name || "").toLowerCase().indexOf(q) >= 0;
    });

    var wrap = document.getElementById("crmlists");
    wrap.innerHTML = "";
    if (!list.length) {
      wrap.innerHTML = '<div class="muted">No ' + crmDir +
        ' yet. Record a sale (with a name) or a purchase to build your list.</div>';
      document.getElementById("crmmsg").textContent = "";
      return;
    }

    var card = document.createElement("div");
    card.className = "card"; card.style.padding = "4px 0";
    list.forEach(function (c) {
      var isCust = (crmDir === "customers");
      var val = isCust ? c.total_received : c.total_paid;
      var sub = c.transactions + " txn(s)";
      if (c.last_date) sub += " · last " + c.last_date;
      // Debt flag on the row.
      if (c.owes_me > 0) sub += " · owes you " + naira(c.owes_me);
      else if (c.i_owe > 0) sub += " · you owe " + naira(c.i_owe);
      var div = document.createElement("div");
      div.className = "item tappable";
      div.innerHTML = '<div><div class="name">' + escapeHtml(c.name || "?") +
        '</div><div class="meta">' + escapeHtml(sub) + '</div></div>' +
        '<div class="right"><div class="stock">' + naira(val || 0) + '</div>' +
        '<div class="meta">' + (isCust ? "spent ›" : "paid ›") + '</div></div>';
      div.onclick = (function (person) {
        return function () { openContact(person); };
      })(c);
      card.appendChild(div);
    });
    wrap.appendChild(card);
    document.getElementById("crmmsg").textContent = list.length + " " + crmDir;
  };

  // ── Contact detail sheet — tap a person to see their details + record a
  // payment if they carry a debt. ──
  var cdCtx = null;
  window.openContact = function (c) {
    cdCtx = c;
    var isCust = (crmDir === "customers");
    document.getElementById("cd-name").textContent = c.name || "Contact";
    document.getElementById("cd-type").textContent =
      (c.type ? c.type.charAt(0).toUpperCase() + c.type.slice(1) : "Contact");
    document.getElementById("cd-spend-k").textContent = isCust ? "Total spent" : "Total paid";
    document.getElementById("cd-spend").textContent =
      naira(isCust ? c.total_received : c.total_paid);
    document.getElementById("cd-txns").textContent = (c.transactions || 0);
    document.getElementById("cd-last").textContent = c.last_date || "—";

    var debtCard = document.getElementById("cd-debtcard");
    var payBtn = document.getElementById("cd-pay");
    if (c.owes_me > 0) {
      document.getElementById("cd-debt-k").textContent = "Owes you";
      document.getElementById("cd-debt").textContent = naira(c.owes_me);
      debtCard.classList.remove("hidden");
      payBtn.classList.remove("hidden");
    } else if (c.i_owe > 0) {
      document.getElementById("cd-debt-k").textContent = "You owe";
      document.getElementById("cd-debt").textContent = naira(c.i_owe);
      debtCard.classList.remove("hidden");
      payBtn.classList.remove("hidden");
    } else {
      debtCard.classList.add("hidden");
      payBtn.classList.add("hidden");
    }

    var phoneEl = document.getElementById("cd-phone");
    if (c.phone) { phoneEl.textContent = "📞 " + c.phone; phoneEl.classList.remove("hidden"); }
    else { phoneEl.classList.add("hidden"); }

    document.getElementById("cdOverlay").classList.remove("hidden");
  };
  window.closeContact = function () {
    document.getElementById("cdOverlay").classList.add("hidden");
    cdCtx = null;
  };
  window.cdRecordPayment = function () {
    if (!cdCtx) return;
    var c = cdCtx;
    closeContact();
    // "in" = they owe me (collect); "out" = I owe them (repay).
    if (c.owes_me > 0) openPay(c.name, c.owes_me, "in");
    else if (c.i_owe > 0) openPay(c.name, c.i_owe, "out");
  };

  // ── Debt-payment sheet (CRM write) ──
  var payCtx = null;   // {name, amount, direction}
  window.openPay = function (name, amount, direction) {
    payCtx = { name: name, amount: amount, direction: direction };
    document.getElementById("pay-title").textContent =
      direction === "in" ? ("Record payment from " + name) : ("Record payment to " + name);
    document.getElementById("pay-sub").textContent =
      direction === "in"
        ? (name + " owes you " + naira(amount))
        : ("You owe " + name + " " + naira(amount));
    var amt = document.getElementById("pay-amount");
    amt.value = amount ? Number(amount) : "";
    amt.max = amount || undefined;
    document.getElementById("pay-err").textContent = "";
    document.getElementById("pay-hint").textContent = "";
    document.getElementById("pay-save").disabled = false;
    document.getElementById("payOverlay").classList.remove("hidden");
    payHint();
  };
  window.closePay = function () {
    document.getElementById("payOverlay").classList.add("hidden");
    payCtx = null;
  };
  window.payHint = function () {
    if (!payCtx) return;
    var v = parseInt(document.getElementById("pay-amount").value, 10) || 0;
    var rem = Math.max(0, (payCtx.amount || 0) - v);
    document.getElementById("pay-hint").textContent =
      v > 0 ? ("Remaining after this: " + naira(rem)) : "";
  };
  window.savePay = function () {
    if (!payCtx) return;
    var v = parseInt(document.getElementById("pay-amount").value, 10) || 0;
    var err = document.getElementById("pay-err");
    if (v <= 0) { err.textContent = "Enter an amount greater than 0."; return; }
    var btn = document.getElementById("pay-save");
    btn.disabled = true;
    var body = {
      submit_id: "pay_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8),
      name: payCtx.name, amount: v, direction: payCtx.direction
    };
    apiPost("api/debt-payment", body)
      .then(function (r) {
        closePay();
        // Refresh CRM + dashboard (a payment moves cash + the balance).
        crmLoaded = false; loadCrm();
        loadSummary();
      })
      .catch(function (e) {
        btn.disabled = false;
        err.textContent = e.message || "Could not record the payment.";
      });
  };

  // ── Records tab: period/date-scoped transaction list + export ──
  var REC_PERIODS = [["today","Today"],["week","Week"],["month","Month"],
                     ["last_month","Last month"],["quarter","Quarter"],["year","Year"]];
  window.recSetType = function (t) {
    recType = t;
    var tabs = document.getElementById("rec-type-tabs").children;
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("active", tabs[i].getAttribute("data-rt") === t);
    }
    var k = document.getElementById("rec-total-k");
    k.textContent = "Total " + (t === "sale" ? "sales" : (t === "purchase" ? "purchases" : "expenses"));
    loadRecords();
  };
  function recRenderChips() {
    var c = document.getElementById("rec-chips");
    c.innerHTML = "";
    REC_PERIODS.forEach(function (p) {
      var el = document.createElement("div");
      el.className = "chip" + (!recFrom && p[0] === recPeriod ? " active" : "");
      el.textContent = p[1];
      el.onclick = function () {
        recFrom = ""; recTo = ""; recPeriod = p[0];
        document.getElementById("rec-datebox").classList.add("hidden");
        recRenderChips(); loadRecords();
      };
      c.appendChild(el);
    });
    var pick = document.createElement("div");
    pick.className = "chip" + (recFrom ? " active" : "");
    pick.textContent = "📅 Pick date";
    pick.onclick = function () {
      document.getElementById("rec-datebox").classList.toggle("hidden");
    };
    c.appendChild(pick);
  }
  window.recApplyDate = function () {
    var f = document.getElementById("rec-date-from").value;
    var t = document.getElementById("rec-date-to").value;
    var err = document.getElementById("rec-date-err");
    if (!f) { err.textContent = "Pick at least a start date."; return; }
    err.textContent = "";
    recFrom = f; recTo = t || f;
    recRenderChips(); loadRecords();
  };
  function recQuery() {
    var q = "type=" + recType;
    if (recFrom) {
      q += "&from=" + encodeURIComponent(recFrom) + "&to=" + encodeURIComponent(recTo || recFrom);
    } else {
      q += "&period=" + recPeriod;
    }
    return q;
  }
  function loadRecords() {
    document.getElementById("rec-msg").textContent = "";
    document.getElementById("rec-list").innerHTML = '<div class="muted">Loading...</div>';
    api("api/records?" + recQuery())
      .then(function (d) { renderRecords(d); })
      .catch(function (e) {
        document.getElementById("rec-list").innerHTML =
          '<span class="err">' + (e.message || "Could not load") + '</span>';
      });
  }
  function renderRecords(d) {
    document.getElementById("rec-total").textContent = naira(d.total || 0);
    var list = document.getElementById("rec-list");
    var rows = d.records || [];
    if (!rows.length) {
      list.innerHTML = '<div class="muted">No records in ' + escapeHtml(d.period_label || "this period") + '.</div>';
      document.getElementById("rec-msg").textContent = "";
      return;
    }
    var card = document.createElement("div");
    card.className = "card"; card.style.padding = "4px 0";
    rows.forEach(function (t) {
      var meta = [t.date || ""];
      if (t.vendor) meta.push(t.vendor);
      var div = document.createElement("div");
      div.className = "item";
      div.innerHTML = '<div><div class="name">' + escapeHtml(t.desc || "?") +
        '</div><div class="meta">' + escapeHtml(meta.join(" · ")) + '</div></div>' +
        '<div class="right"><div class="stock">' + naira(t.amount || 0) + '</div></div>';
      card.appendChild(div);
    });
    list.innerHTML = "";
    list.appendChild(card);
    var note = d.count + " record(s) · " + (d.period_label || "");
    if (d.has_more) note += " · showing " + rows.length + " of " + d.count + " (narrow the date or export for all)";
    document.getElementById("rec-msg").textContent = note;
  }
  window.recExport = function (fmt) {
    var msg = document.getElementById("rec-msg");
    msg.textContent = "Preparing " + fmt.toUpperCase() + " export...";
    api("api/export?" + recQuery() + "&fmt=" + fmt)
      .then(function (d) {
        msg.textContent = d.message ||
          (d.ok ? "Export sent to your chat." : "Nothing to export.");
      })
      .catch(function (e) {
        msg.textContent = e.message || "Export failed.";
      });
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
      t === "sale" ? "Amount received (\u20a6)" : (t === "purchase" ? "Amount paid (\u20a6)" : "Amount (\u20a6)");
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
      : ("Balance owed: " + naira(bal));
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
