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

    # A SPECIFIC month / quarter / year picked from the dropdown (e.g.
    # period=quarter&y=2026&q=1, period=month&y=2025&m=3, period=year&y=2024).
    # This is what lets the user pick ANY quarter/month/year, not just the
    # current one (the named presets always end 'today').
    specific = _specific_range(period, qs)
    if specific:
        return specific

    return _date_range(period)


def _specific_range(period: str, qs: dict):
    """Resolve an explicitly-chosen month/quarter/year from y/m/q query params.
    Returns (start, end, label) or None if not applicable/invalid. A window in
    the FUTURE past today is capped at today (a partial current period)."""
    import calendar
    from datetime import datetime as _dt
    now = _dt.now()

    def _i(key, default=None):
        try:
            return int(qs.get(key))
        except (TypeError, ValueError):
            return default

    y = _i("y")
    if not y or y < 2000 or y > 2100:
        return None
    today = now.strftime("%Y-%m-%d")

    def _cap(end):
        return end if end <= today else today

    if period == "year":
        start = "%04d-01-01" % y
        end = _cap("%04d-12-31" % y)
        return start, end, str(y)

    if period == "quarter":
        q = _i("q")
        if q not in (1, 2, 3, 4):
            return None
        sm = (q - 1) * 3 + 1          # start month
        em = sm + 2                    # end month
        last_day = calendar.monthrange(y, em)[1]
        start = "%04d-%02d-01" % (y, sm)
        end = _cap("%04d-%02d-%02d" % (y, em, last_day))
        return start, end, "Q%d %d" % (q, y)

    if period == "month":
        mth = _i("m")
        if mth not in range(1, 13):
            return None
        last_day = calendar.monthrange(y, mth)[1]
        start = "%04d-%02d-01" % (y, mth)
        end = _cap("%04d-%02d-%02d" % (y, mth, last_day))
        label = _dt(y, mth, 1).strftime("%B %Y")
        return start, end, label

    return None


def _json_default(o):
    """JSON fallback: DynamoDB returns numbers as Decimal, which json.dumps
    can't serialize. Coerce to int when whole, else float. Anything else →
    str (never crash the response)."""
    try:
        from decimal import Decimal
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
    except Exception:
        pass
    return str(o)


def _json(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            # The page and API share an origin (same API Gateway), so CORS isn't
            # strictly needed, but be explicit and safe.
            "Cache-Control": "no-store",
        },
        # default=_json_default → Decimal (and any stray type) is serialized
        # safely, so a raw DynamoDB value can never 500 the endpoint.
        "body": json.dumps(body, default=_json_default),
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
            # Stamp the live build id (set by deploy.sh into BUILD_STAMP) into the
            # footer so the owner can confirm which build is actually running —
            # settles "did my change deploy / is Telegram caching?" questions.
            import os as _os
            page = _PAGE_HTML.replace("__BUILD_STAMP__",
                                      _os.environ.get("BUILD_STAMP", "dev"))
            return _html(page)

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
        # ── Writes (payment collection: create a Paystack pay-link) ──
        if method == "POST" and path.endswith("/app/api/payment-request"):
            return _payment_request_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/cancel-payment-request"):
            return _cancel_payment_request_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/delete-payment-request"):
            return _delete_payment_request_write(event, user_id)
        if method == "GET" and path.endswith("/app/api/payment-requests"):
            return _payment_requests_read(event, user_id)
        # ── Sub-accounts / open items (Stage 1+2) ──
        if method == "GET" and path.endswith("/app/api/open-items"):
            return _open_items_read(event, user_id)
        if method == "POST" and path.endswith("/app/api/settle-open"):
            return _settle_open_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/site-label"):
            return _site_label_write(event, user_id)
        # ── Writes (generate + deliver a payment receipt PDF, on-demand) ──
        if method == "POST" and path.endswith("/app/api/receipt"):
            return _receipt_send(event, user_id)
        # ── Writes (Stage 2: recipe/BOM add/remove material) ──
        if method == "POST" and path.endswith("/app/api/recipe"):
            return _recipe_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/produce"):
            return _produce_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/cash-adjust"):
            return _cash_adjust_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/opening-cash"):
            return _opening_cash_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/void-transaction"):
            return _void_transaction_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/restore-transaction"):
            return _restore_transaction_write(event, user_id)
        if method == "GET" and path.endswith("/app/api/deleted-transactions"):
            return _deleted_transactions_read(event, user_id)
        if method == "POST" and path.endswith("/app/api/prepaid-expense"):
            return _prepaid_expense_write(event, user_id)
        if method == "POST" and path.endswith("/app/api/edit-transaction"):
            return _edit_transaction_write(event, user_id)

        # ── Reads ──
        if method == "GET" and path.endswith("/app/api/recipe"):
            return _recipe_read(event, user_id)
        if method == "GET" and path.endswith("/app/api/summary"):
            return _summary(event, user_id)
        if method == "GET" and path.endswith("/app/api/inventory"):
            return _inventory(event, user_id)
        if method == "GET" and path.endswith("/app/api/contacts"):
            return _contacts(event, user_id)
        if method == "GET" and path.endswith("/app/api/contact-detail"):
            return _contact_detail(event, user_id)
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

def _catalog_health(user: dict, industry: str) -> dict:
    """Stage 4 — a gentle, catalog-first setup check.

    Scans the user's products and counts setup gaps that hurt accuracy, so the
    app can nudge (never block) the owner to finish setup. Computed server-side
    (has the full catalog + industry); the app only renders the counts.

    Gaps flagged:
      - finished goods (mfg/hybrid) with NO recipe → cost can't be derived
      - raw materials / supplies with NO cost → COGS/margin understated
      - products with NO selling price → margin unknown
      - products with NO unit → quantity/valuation ambiguous
    Trading has no recipe concept, so that check is skipped for it.
    Returns {total, complete, issues:{no_recipe,no_cost,no_price,no_unit}, tips:[...]}.
    """
    catalog = user.get("product_catalog", {}) or {}
    products = catalog.get("products", {}) or {}
    uses_recipes = industry in ("manufacturing", "hybrid")

    no_recipe = no_cost = no_price = no_unit = 0
    total = 0
    for key, p in products.items():
        if not isinstance(p, dict):
            continue
        total += 1
        item_type = p.get("item_type", "")
        has_tree = bool(p.get("variant_tree") or p.get("_has_tree"))
        # Variant-tree products carry cost/stock on leaves — don't false-flag
        # them for a missing product-level cost.
        cost = float(p.get("landing_cost", 0) or 0)
        price = float(p.get("sale_price", 0) or 0)
        unit = str(p.get("primary_unit", "") or "").strip()
        recipe = p.get("recipe") or []

        if uses_recipes and item_type == "finished_product" and not recipe:
            no_recipe += 1
        # Cost gap: raw materials / supplies need a buy-cost. Finished goods get
        # cost from the recipe, so a missing product cost there isn't a gap.
        if item_type in ("raw_material", "supply", "overhead", "") and not has_tree:
            if item_type != "" or not uses_recipes:  # plain products count in trading
                if cost <= 0:
                    no_cost += 1
        if price <= 0 and item_type not in ("raw_material", "supply", "overhead"):
            no_price += 1
        if not unit:
            no_unit += 1

    issues = {
        "no_recipe": no_recipe,
        "no_cost": no_cost,
        "no_price": no_price,
        "no_unit": no_unit,
    }
    issue_total = no_recipe + no_cost + no_price + no_unit
    complete = (total > 0 and issue_total == 0)

    tips = []
    if no_recipe:
        tips.append(f"{no_recipe} finished product(s) have no recipe — set one so cost is calculated.")
    if no_cost:
        tips.append(f"{no_cost} item(s) have no cost — add it for accurate profit.")
    if no_price:
        tips.append(f"{no_price} product(s) have no selling price — set it to see margin.")
    if no_unit:
        tips.append(f"{no_unit} product(s) have no unit — add one (e.g. piece, kg).")

    return {
        "total": total,
        "complete": complete,
        "issue_count": issue_total,
        "issues": issues,
        "tips": tips,
    }


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
    # All-time cash at hand (snapshot, ignores the period filter). Opening cash
    # is an optional stored figure (0 by default for now).
    cashpos = acct.cash_position(user_id, opening=user.get("opening_cash", 0))

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

    # Industry drives per-industry UI in the app (Stage 0). Read from the user,
    # never guessed in JS. Fallback to trading (the baseline).
    industry = (user.get("industry_class")
                or user.get("business_type") or "trading")

    # The owner's chosen name for the sub-account layer (Stage 2), so the UI can
    # label the Site field / cards (Mall / Property / Location …). Default kept in
    # the engine helper.
    try:
        from features.open_items import get_site_label as _gsl
        site_label = _gsl(db, user_id)
    except Exception:
        site_label = "Location"

    return _json(200, {
        "business": business,
        "industry": industry,
        "site_label": site_label,
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
            # All-time running balance (snapshot). Distinct from net (period).
            "at_hand": cashpos["cash_at_hand"],
            # Editable starting balance (the cash held before recording began).
            "opening": cashpos["opening"],
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
            # TRUE net worth now includes cash at hand (cash + inventory +
            # receivables − payables). Falls back to the pre-cash proxy for
            # safety if an older engine build is deployed.
            "net_position": pos.get("net_worth", pos.get("net_worth_proxy", 0)),
        },
        "uncosted_sales": pnl["uncosted_count"],
        # Stage 4: catalog-first setup health (nudge, never blocks).
        "catalog_health": _catalog_health(user, industry),
    })


def _row_from_product(p: dict, cat=None) -> dict:
    """Map a normalized product into the grid-row shape the page expects.
    Shared by the inventory list and the write echo, so the UI can patch a row
    in place after a write with an identical shape.

    For a variant-TREE product, cost + stock value live on the leaves (product
    landing_cost is 0), so we roll them up via cat.tree_rollup — otherwise the
    app would show 'no price/cost set' even when leaves are costed."""
    # MONEY is kobo-precise (int when whole, else 2dp); STOCK may be fractional.
    from utils.money import money_round
    # Cost source order MUST match the accounting engine (product_avg_cost):
    # weighted-average first (what purchases accrue into), then landing_cost as
    # the opening/legacy fallback. Reading landing_cost only made a purchased
    # raw material whose cost lives in avg_cost show "no cost set" in the app.
    cost = money_round(p.get("avg_cost") or p.get("landing_cost") or 0)
    stock_value = money_round(p.get("_stock_value") or 0)
    def _num(v):
        try:
            n = float(v or 0)
        except (TypeError, ValueError):
            return 0
        return int(n) if n == int(n) else n
    stock = _num(p.get("stock"))
    if p.get("_has_tree") and cat is not None:
        try:
            roll = cat.tree_rollup(p)
            if roll.get("avg_cost"):
                cost = money_round(roll["avg_cost"])
            if roll.get("value"):
                stock_value = money_round(roll["value"])
            if roll.get("stock") is not None:
                stock = _num(roll["stock"])
        except Exception:
            pass
    # Units for the web: base unit + custom conversion rules (upgraded on read).
    try:
        from utils import units as _units
        _base_unit, _unit_defs = _units.product_units(p)
    except Exception:
        _base_unit, _unit_defs = (p.get("primary_unit") or ""), {}
    return {
        "key": p.get("_key"),
        "name": p.get("name"),
        "category": p.get("category") or "",
        "unit": _base_unit or (p.get("primary_unit") or ""),
        "base_unit": _base_unit or (p.get("primary_unit") or ""),
        "unit_defs": _unit_defs or {},
        "stock": stock,
        "cost": cost,
        "sale_price": money_round(p.get("sale_price") or 0),
        "reorder_level": int(p.get("reorder_level") or 0),
        "low_stock": bool(p.get("_is_low_stock")),
        "has_variants": bool(p.get("_has_tree") or p.get("_has_variants")),
        "stock_value": stock_value,
        "item_type": p.get("item_type") or "",
        # Stage 1: does this product carry a recipe? Finished/manufactured goods
        # derive their cost from a recipe (Decision A), so the mfg/hybrid UI
        # shows "Cost (from recipe)" instead of a manual cost field.
        "has_recipe": bool(p.get("recipe")),
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
    """Catalog write for the AUTH'D user only. Reuses the CatalogHandler engine
    (same product data model as chat); money-safe, per-user, tree-leaf aware.

    Value actions:  set_price | set_cost | set_stock | set_stock_delta
    CRUD actions:   add | rename | delete | set_unit | set_reorder | set_category
    Variant leaf:   set_leaf_stock | set_leaf_cost  (body carries path:[...])

    Echoes the recomputed row (or {ok} for delete) so the UI reflects truth."""
    from services.database import Database
    from features.catalog import CatalogHandler

    data = _parse_body(event)
    action = str(data.get("action", "")).strip()
    key = str(data.get("key", "")).strip()
    variant = str(data.get("variant", "") or "").strip()

    VALUE_ACTIONS = ("set_price", "set_cost", "set_stock", "set_stock_delta")
    CRUD_ACTIONS = ("add", "rename", "delete", "set_unit", "set_reorder",
                    "set_category", "set_conversion")
    LEAF_ACTIONS = ("set_leaf_stock", "set_leaf_cost")
    if action not in VALUE_ACTIONS + CRUD_ACTIONS + LEAF_ACTIONS:
        return _json(400, {"error": "bad request"})

    db = Database()
    cat = CatalogHandler(None, db)
    products = cat._get_products(user_id) or {}
    user = db.get_user(user_id) or {}   # for industry-aware write guards

    def _echo(k):
        updated = cat.get_normalized_product(user_id, k)
        return _json(200, {"ok": True, "product": _row_from_product(updated, cat)})

    # ── ADD a new product (no existing key required) ──
    if action == "add":
        name = str(data.get("name", "")).strip()
        if not name:
            return _json(400, {"error": "a product name is required"})
        new_key = name.lower().replace(" ", "_")
        if new_key in products:
            return _json(409, {"error": "a product with that name already exists"})
        products[new_key] = {
            "name": name.title(),
            "stock": 0,
            "landing_cost": 0,
            "sale_price": 0,
            "category": str(data.get("category", "") or "").strip(),
            "variants": [],
        }
        # Stage 1: let mfg/hybrid tag the item type on creation (finished_product
        # / raw_material / supply). Only accept known values; trading omits it and
        # ensure_item_types will auto-tag as before.
        req_type = str(data.get("item_type", "") or "").strip()
        if req_type in ("finished_product", "raw_material", "supply", "overhead"):
            products[new_key]["item_type"] = req_type
        # Optional cost + unit on creation. Useful for "Add raw material" (you
        # register stock you already have on hand, with what it cost you and its
        # stock unit) — the plain product add still omits both and they stay 0/"".
        # Money stays kobo-precise; unit goes through the canonical unit engine so
        # base_unit/unit_defs are consistent with the rest of the catalog.
        raw_cost = data.get("landing_cost", data.get("cost"))
        if raw_cost not in (None, ""):
            try:
                from utils.money import money_round as _mr
                products[new_key]["landing_cost"] = _mr(raw_cost)
            except Exception:
                pass
        raw_unit = str(data.get("unit", "") or "").strip()
        if raw_unit:
            try:
                from utils import units as _units
                u = _units.normalize_unit(raw_unit)
                if u:
                    _units.rebase_product(products[new_key], u)
            except Exception:
                pass
        # Optional opening stock (what you already have on hand). Fractional-safe.
        raw_stock = data.get("stock")
        if raw_stock not in (None, ""):
            try:
                from utils.quantity import to_qty as _toq
                products[new_key]["stock"] = _toq(raw_stock)
            except Exception:
                pass
        cat._save_products(user_id, products)
        return _echo(new_key)

    # Everything else needs an existing product.
    if not key:
        return _json(400, {"error": "product key required"})
    prod = products.get(key)
    if not isinstance(prod, dict):
        return _json(404, {"error": "product not found"})
    name = prod.get("name") or key

    # ── CRUD (direct dict mutation → save; matches the chat data shape) ──
    if action == "delete":
        # Financial-safety gate: deletion must be explicitly confirmed. The
        # client uses a two-tap confirm and sends confirm:true; a malformed or
        # replayed partial request without it will NOT delete anything.
        if not data.get("confirm"):
            held = 0
            try:
                held = int(float(prod.get("stock", 0) or 0))
            except (TypeError, ValueError):
                held = 0
            return _json(409, {
                "error": "confirm required",
                "needs_confirm": True,
                "name": name,
                "stock": held,
            })
        del products[key]
        cat._save_products(user_id, products)
        return _json(200, {"ok": True, "deleted": key})
    if action == "rename":
        new_name = str(data.get("name", "")).strip()
        if not new_name:
            return _json(400, {"error": "a new name is required"})
        # Keep the KEY stable (past sales reference catalog_product by key);
        # only the display name changes.
        prod["name"] = new_name.title()
        cat._save_products(user_id, products)
        return _echo(key)
    if action == "set_unit":
        # Set the canonical BASE unit. Keep primary_unit + base_unit in sync AND
        # rebuild unit_defs against the new base from the raw taught rules — a
        # bare base swap would leave every custom factor pointing at the OLD base
        # (e.g. a "1 bag = 20 pieces" product switched to base=bag would keep
        # {"bag":20} = "1 bag = 20 bags" and multiply everything by 20). Rebasing
        # from unit_edges keeps the graph consistent with the new base.
        from utils import units as _units
        u = _units.normalize_unit(str(data.get("unit", "") or ""))
        if not u:
            return _json(400, {"error": "a unit is required"})
        _units.rebase_product(prod, u)
        cat._save_products(user_id, products)
        return _echo(key)
    if action == "set_conversion":
        # Teach a custom unit rule from the web, e.g. "1 bag = 20 pieces".
        # Same shared engine as chat: parse, rebuild the multi-hop graph to the
        # product's base, reject contradictions, echo the resolved factor.
        from utils import units as _units
        text = str(data.get("rule", "") or "").strip()
        # Deterministic parse first; LLM fallback only on failure (validated below).
        rule = _units.parse_rule(text) or _units.parse_rule_llm(text)
        if not rule:
            return _json(400, {"error": "Use a format like '1 bag = 20 pieces'"})
        _units.upgrade_product_units(prod)
        qa, ua, qb, ub = rule
        base_unit = _units.normalize_unit(prod.get("base_unit", "")) \
            or _units.normalize_unit(prod.get("primary_unit", "")) \
            or _units.normalize_unit(ub)
        prior_edges = list(prod.get("unit_edges", []) or [])
        _, conflicts = _units.build_unit_defs(base_unit, prior_edges + [[qa, ua, qb, ub]])
        if conflicts:
            return _json(400, {"error": "That conflicts with an existing rule: "
                                        + "; ".join(conflicts)})
        raw_edges = [e for e in prior_edges
                     if not (len(e) == 4
                             and _units.normalize_unit(e[1]) == _units.normalize_unit(ua)
                             and _units.normalize_unit(e[3]) == _units.normalize_unit(ub))]
        raw_edges.append([qa, ua, qb, ub])
        defs, _ = _units.build_unit_defs(base_unit, raw_edges)
        prod["base_unit"] = base_unit
        prod["primary_unit"] = base_unit
        prod["unit_edges"] = raw_edges
        prod["unit_defs"] = defs
        cat._save_products(user_id, products)
        return _echo(key)
    if action == "set_category":
        prod["category"] = str(data.get("category", "") or "").strip()
        cat._save_products(user_id, products)
        return _echo(key)
    if action == "set_reorder":
        try:
            prod["reorder_level"] = max(0, int(data.get("value")))
        except (TypeError, ValueError):
            return _json(400, {"error": "reorder must be a number"})
        cat._save_products(user_id, products)
        return _echo(key)

    # ── Variant-leaf writes (drill path → engine, tree-leaf aware) ──
    if action in LEAF_ACTIONS:
        path = data.get("path") or []
        if isinstance(path, str):
            path = [p.strip() for p in path.split(",") if p.strip()]
        path = [str(p).strip() for p in path if str(p).strip()]
        if not path:
            return _json(400, {"error": "a variant path is required"})
        leaf = " / ".join(path)   # _COMBO_SEP
        # Leaf STOCK may be fractional (0.5 kg); leaf COST is money (kobo-precise).
        from utils.money import money_round
        try:
            if action == "set_leaf_stock":
                value = float(data.get("value"))
                if value == int(value):
                    value = int(value)
            else:  # set_leaf_cost — money
                value = money_round(data.get("value"))
        except (TypeError, ValueError):
            return _json(400, {"error": "value must be a number"})
        if value < 0:
            return _json(400, {"error": "value must be 0 or more"})
        if action == "set_leaf_stock":
            res = cat.set_stock_exact(user_id, name, value, leaf)
            if not bool(res.get("matched", True)):
                return _json(500, {"error": "write failed"})
        else:  # set_leaf_cost
            if not cat.set_cost_direct(user_id, name, value, leaf):
                return _json(500, {"error": "write failed"})
        return _echo(key)

    # ── Value actions (existing): price / cost / stock exact / stock delta ──
    # STOCK may be fractional (0.5 kg) → parse as a number; MONEY (price/cost) +
    # reorder stay integer. set_stock_delta may be negative (a deduction).
    from utils.money import money_round as _mr
    _stock_actions = ("set_stock", "set_stock_delta")
    try:
        if action in _stock_actions:
            value = float(data.get("value"))
            if value == int(value):
                value = int(value)   # keep whole values whole for clean display
        else:
            value = _mr(data.get("value"))   # set_price / set_cost — money (kobo)
    except (TypeError, ValueError):
        return _json(400, {"error": "value must be a number"})
    if action in ("set_price", "set_cost", "set_stock") and value < 0:
        return _json(400, {"error": "value must be 0 or more"})

    ok = True
    if action == "set_price":
        ok = cat.set_sale_price(user_id, key, value)
    elif action == "set_cost":
        # Decision A: finished/manufactured goods derive cost from their recipe.
        # A manual cost write on such a product would create a second source of
        # truth (the exact confusion this stage removes), so reject it. The JS
        # already hides the field, but this guards direct/replayed POSTs too.
        # Raw materials/supplies keep a real manual buy-cost, and Trading is
        # unaffected (its products aren't tagged finished_product).
        industry = (user.get("industry_class")
                    or user.get("business_type") or "trading")
        if industry in ("manufacturing", "hybrid") and \
                prod.get("item_type") == "finished_product":
            return _json(409, {
                "error": "This is a manufactured item — its cost comes from "
                         "its recipe. Open it and tap \"Set / edit recipe\" to "
                         "change the cost.",
                "recipe_driven": True,
            })
        ok = cat.set_cost_direct(user_id, name, value, variant)
    elif action == "set_stock":
        res = cat.set_stock_exact(user_id, name, value, variant)
        ok = bool(res.get("matched", True))
    elif action == "set_stock_delta":
        res = cat.update_stock(user_id, name, value, variant=variant, cost_mode="keep")
        ok = bool(res.get("matched", True))
    if not ok:
        return _json(500, {"error": "write failed"})
    return _echo(key)


def _recipe_read(event, user_id: str):
    """Stage 2 — return a finished product's recipe + rolled-up per-unit cost.

    Query: ?key=<product_key>. Reuses ProductionHandler.get_recipe (the engine),
    so the web never recomputes cost. Read-only."""
    from services.database import Database
    from features.production import ProductionHandler

    params = event.get("queryStringParameters") or {}
    key = str((params.get("key") or "")).strip()
    if not key:
        return _json(400, {"error": "product key required"})
    prod = ProductionHandler(None, Database())
    res = prod.get_recipe(user_id, key)
    if not res.get("ok"):
        return _json(404, res)
    return _json(200, res)


def _recipe_write(event, user_id: str):
    """Stage 2 — add or remove a recipe material, then restamp finished cost.

    Body: {action: "add_material"|"remove_material", key: <product_key>, ...}
      add_material:    {material_key, quantity, unit?, cost_per_unit?, mat_type?}
      remove_material: {index}
    All cost math lives in ProductionHandler (engine); no JS/forked math. The
    response echoes the fresh recipe + unit_cost so the UI reflects truth."""
    from services.database import Database
    from features.production import ProductionHandler

    data = _parse_body(event)
    action = str(data.get("action", "")).strip()
    key = str(data.get("key", "")).strip()
    if not key:
        return _json(400, {"error": "product key required"})
    if action not in ("add_material", "remove_material"):
        return _json(400, {"error": "bad request"})

    prod = ProductionHandler(None, Database())
    if action == "add_material":
        res = prod.web_add_material(
            user_id, key,
            material_key=str(data.get("material_key", "")).strip(),
            quantity=data.get("quantity"),
            unit=str(data.get("unit", "") or "").strip(),
            cost_per_unit=data.get("cost_per_unit"),
            mat_type=str(data.get("mat_type", "material") or "material").strip(),
            new_material_name=str(data.get("new_material_name", "") or "").strip(),
        )
    else:  # remove_material
        res = prod.web_remove_material(user_id, key, data.get("index"))

    if not res.get("ok"):
        # 404 for missing product/material, 400 for bad values.
        err = str(res.get("error", ""))
        code = 404 if "not found" in err else 400
        return _json(code, res)
    return _json(200, res)


def _produce_write(event, user_id: str):
    """Record a production run from the mini app (stateless). Body:
    {key: <finished_product_key>, quantity, waste?}. Cost + material deduction +
    landing_cost restamp + the type='production' transaction all happen in
    ProductionHandler.produce_web (the engine) — no forked math. Echoes the
    batch summary so the UI can confirm."""
    from services.database import Database
    from features.production import ProductionHandler

    data = _parse_body(event)
    key = str(data.get("key", "")).strip()
    if not key:
        return _json(400, {"error": "product key required"})

    prod = ProductionHandler(None, Database())
    res = prod.produce_web(user_id, key, data.get("quantity"), data.get("waste"),
                           unit=str(data.get("unit", "") or ""),
                           confirm=bool(data.get("confirm")))
    if not res.get("ok"):
        err = str(res.get("error", ""))
        # A soft, confirmable guardrail (not enough materials) → 409 so the
        # client can re-send with confirm:true; a real failure → 400/404.
        if res.get("needs_confirm"):
            code = 409
        else:
            code = 404 if "not found" in err else 400
        return _json(code, res)
    return _json(200, res)


def _cash_adjust_write(event, user_id: str):
    """Record a manual cash movement (owner withdrawal / capital injection /
    bank<->cash transfer / correction). Body: {amount, direction:'in'|'out',
    reason}. Reuses TransactionHandler.record_cash_adjustment (the engine) —
    saved as type='cash_adjustment', counted by cash_position, excluded from
    the P&L."""
    from services.database import Database
    from features.transactions import TransactionHandler

    data = _parse_body(event)
    tx = TransactionHandler(None, Database(), None, None)
    res = tx.record_cash_adjustment(
        user_id,
        amount=data.get("amount"),
        direction=str(data.get("direction", "in")),
        reason=str(data.get("reason", "") or ""),
    )
    if not res.get("ok"):
        return _json(400, res)
    return _json(200, res)


def _opening_cash_write(event, user_id: str):
    """Set the OPENING cash balance — the cash the business held before it
    started recording in Kashia. This is an ABSOLUTE figure (not a ± movement):
    `cash_position` / `position` add `user.opening_cash` to the running total, so
    both cash-at-hand and net worth shift by the new opening. Body: {amount}."""
    from services.database import Database
    from utils.money import money_round

    data = _parse_body(event)
    try:
        amount = money_round(data.get("amount") or 0)
    except Exception:
        amount = 0
    if amount < 0:
        return _json(400, {"error": "opening balance can't be negative"})
    try:
        Database().update_user_field(user_id, "opening_cash", amount)
        return _json(200, {"ok": True, "opening_cash": amount})
    except Exception as e:
        logger.error(f"_opening_cash_write failed: {e}")
        return _json(400, {"ok": False, "error": "could not save the opening balance"})


def _void_transaction_write(event, user_id: str):
    """Delete a recorded transaction and reverse its side-effects (stock, debt,
    contact totals). Body: {tx_id}. Reuses TransactionHandler.void_transaction_web
    (the engine). Production is blocked (returns 400 with a message)."""
    from services.database import Database
    from features.transactions import TransactionHandler

    data = _parse_body(event)
    tx_id = str(data.get("tx_id", "") or "").strip()
    if not tx_id:
        return _json(400, {"error": "transaction id required"})
    tx = TransactionHandler(None, Database(), None, None)
    res = tx.void_transaction_web(user_id, tx_id)
    if not res.get("ok"):
        err = str(res.get("error", ""))
        code = 404 if "not found" in err else 400
        return _json(code, res)
    return _json(200, res)


def _restore_transaction_write(event, user_id: str):
    """UNDO a just-deleted transaction. Body: {tx_id} (preferred — server looks
    it up from the soft-deleted row) OR {tx: <voided_tx row>} (legacy in-session
    Undo). Re-un-archives the row + re-applies its effects via
    TransactionHandler.restore_transaction_web (the engine)."""
    from services.database import Database
    from features.transactions import TransactionHandler

    data = _parse_body(event)
    # Accept tx_id directly (new style, from Recently-deleted list).
    tx_id = str(data.get("tx_id") or "").strip()
    if tx_id:
        tx = {"transaction_id": tx_id}
    else:
        tx = data.get("tx")
    if not isinstance(tx, dict) or not tx.get("transaction_id"):
        return _json(400, {"error": "nothing to restore"})
    handler = TransactionHandler(None, Database(), None, None)
    res = handler.restore_transaction_web(user_id, tx)
    return _json(200 if res.get("ok") else 400, res)


def _deleted_transactions_read(event, user_id: str):
    """Return the last 30 days of user-voided transactions for the
    Recently-deleted restore screen. GET /app/api/deleted-transactions."""
    from services.database import Database
    from features.transactions import TransactionHandler
    handler = TransactionHandler(None, Database(), None, None)
    res = handler.list_deleted_web(user_id, since_days=30)
    return _json(200, res)


def _prepaid_expense_write(event, user_id: str):
    """Record a PREPAID / periodic expense. Body: {amount, description, vendor?,
    category?, periods, cadence?('month'|'quarter'|'year')}. Full cash leaves now;
    the expense is spread across `periods` periods. Reuses
    TransactionHandler.record_prepaid_expense_web (the engine)."""
    from services.database import Database
    from features.transactions import TransactionHandler

    data = _parse_body(event)
    periods = data.get("periods")
    cadence = str(data.get("cadence", "month") or "month")
    tx = TransactionHandler(None, Database(), None, None)
    res = tx.record_prepaid_expense_web(user_id, {
        "amount": data.get("amount"),
        "description": (data.get("description") or "").strip(),
        "vendor": (data.get("vendor") or "").strip(),
        "category": data.get("category"),
        "details": data.get("details"),
    }, periods, cadence)
    return _json(200 if res.get("ok") else 400, res)


def _edit_transaction_write(event, user_id: str):
    """Edit a recorded transaction in place (reverse old effects → update →
    re-apply). Body: {tx_id, amount?, quantity?, description?, vendor?, date?,
    payment_method?, deposit_amount?}. Reuses TransactionHandler.edit_transaction_web."""
    from services.database import Database
    from features.transactions import TransactionHandler

    data = _parse_body(event)
    tx_id = str(data.get("tx_id", "") or "").strip()
    if not tx_id:
        return _json(400, {"error": "transaction id required"})
    tx = TransactionHandler(None, Database(), None, None)
    res = tx.edit_transaction_web(user_id, tx_id, data)
    if not res.get("ok"):
        err = str(res.get("error", ""))
        code = 404 if "not found" in err else 400
        return _json(code, res)
    return _json(200, res)


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
        from utils.money import money_round
        amount = money_round(data.get("amount"))   # kobo-precise
    except (TypeError, ValueError):
        return _json(400, {"error": "amount must be a number"})
    if amount <= 0:
        return _json(400, {"error": "amount must be greater than 0"})

    db = Database()

    # ── SALE GUARDRAIL (soft, confirmable) — run BEFORE claiming the submit_id.
    # If we claimed first, the confirm retry (same submit_id) would come back as
    # a duplicate and the sale would never record. Checking here keeps the
    # submit_id unclaimed until the owner confirms. 409 => client re-sends with
    # confirm:true. Only sales, only when not already confirmed.
    if tx_type == "sale" and not data.get("confirm") and not data.get("is_service_job"):
        _guard_tx = TransactionHandler(None, db, None, None)
        _warn = _guard_tx._sale_stock_warning(user_id, {
            "type": "sale",
            "description": (data.get("description") or "").strip(),
            "brand": data.get("brand"),
            "catalog_product": data.get("catalog_product"),
        })
        if _warn:
            return _json(409, {"ok": False, "error": _warn,
                               "needs_confirm": True, "warning": _warn})

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
        # Optional sub-account/site tag (Stage 2) — carried onto the open item.
        "site": (data.get("site") or "").strip(),
        # The handler already ran the (pre-claim) sale guardrail above, so tell
        # the engine's defense-in-depth guard not to re-block this call.
        "confirm": True,
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
    # SUPPLIERS = only real goods suppliers. Expense payees (landlord, PHCN,
    # fuel station) are NOT suppliers — they get their own bucket so the
    # supplier directory isn't polluted with every expense payee (the reported
    # "all expenses tagged as suppliers" bug).
    suppliers = [p for p in people if p["type"] in ("supplier", "both")]
    expense_payees = [p for p in people if p["type"] == "expense_payee"]
    customers.sort(key=lambda p: p["total_received"], reverse=True)
    suppliers.sort(key=lambda p: p["total_paid"], reverse=True)
    expense_payees.sort(key=lambda p: p["total_paid"], reverse=True)

    return _json(200, {
        "owed_to_me": owed_to_me,
        "i_owe": i_owe,
        "i_owe_suppliers": owe_suppliers,
        "i_owe_expenses": owe_expenses,
        "debtors": debtors,       # people who owe ME (collect)
        "creditors": creditors,   # people I owe (repay)
        "customers": customers,   # full customer directory (with details)
        "suppliers": suppliers,   # real goods suppliers only
        "expense_payees": expense_payees,  # landlords/utilities/etc — not suppliers
    })


def _contact_detail(event, user_id: str):
    """Full detail for ONE contact: their transaction HISTORY (last 20,
    newest first) + rolled-up stats. Query: ?name=<contact name>. Transactions
    are matched by their `vendor` field (normalised, case-insensitive) — the
    same value the record flow stores. Works for customers, suppliers and
    expense payees alike. Read-only."""
    from services.database import Database
    from utils.money import money_round
    db = Database()

    qs = event.get("queryStringParameters") or {}
    name = str(qs.get("name") or "").strip()
    if not name:
        return _json(400, {"error": "contact name required"})
    want = name.lower().strip()

    # Pull a generous recent window and filter to this contact by vendor.
    try:
        txns = db.get_transactions(user_id, limit=400) or []
    except Exception:
        txns = []
    mine = [t for t in txns
            if str(t.get("vendor") or "").strip().lower() == want]
    # Newest first.
    mine.sort(key=lambda t: t.get("created_at", t.get("date", "")), reverse=True)

    total_in = 0    # money they paid me (sales/income)
    total_out = 0   # money I paid them (purchase/expense)
    for t in mine:
        amt = int(t.get("amount", 0) or 0)
        if t.get("type") in ("sale", "income", "purchase_return"):
            total_in += amt
        elif t.get("type") in ("purchase", "expense", "sale_return"):
            total_out += amt

    from services.accounting import _is_debt_settlement as _is_settle
    rows = []
    for t in mine[:20]:
        item = (t.get("item_name") or t.get("description") or "").strip()
        rows.append({
            "type": t.get("type", ""),
            "amount": int(t.get("amount", 0) or 0),
            "item": item[:40],
            "date": str(t.get("date", "") or ""),
            # Full timestamp (#7): the app can show time alongside date.
            "at": str(t.get("created_at", "") or t.get("at", "") or ""),
            "qty": str(t.get("quantity", "") or ""),
            "payment": str(t.get("payment_method", "") or ""),
            # A debt repayment/collection is money received to clear a debt, NOT a
            # fresh sale — the app renders it with a distinct label/icon so it's
            # not confused with a new sale (the Muyideen display bug).
            "is_payment": bool(_is_settle(t)),
        })

    dates = [t.get("date") for t in mine if t.get("date")]
    contact = db.get_contact_by_name(user_id, name) or {}
    return _json(200, {
        "name": name,
        "type": (contact.get("type") or "contact").lower().strip(),
        "total_in": total_in,
        "total_out": total_out,
        "owes_me": money_round(contact.get("debt_owed_to_me", 0) or 0),
        "i_owe": money_round(contact.get("debt_i_owe", 0) or 0),
        "count": len(mine),
        "first_date": min(dates) if dates else "",
        "last_date": max(dates) if dates else "",
        "transactions": rows,
        "has_more": len(mine) > 20,
    })


def _open_items_read(event, user_id: str):
    """Stage 1/2 view: a contact's OPEN (unpaid) sales, grouped BY SITE. Query:
    ?name=<contact>&direction=owed_to_me|i_owe. Returns the per-site summary +
    the flat item list so the app can show 'Delta ₦8k / Asaba ₦4k' + let the owner
    pay a chosen site. Contact lump total still drives the headline number."""
    from services.database import Database
    from features.open_items import OpenItems, get_site_label
    qs = event.get("queryStringParameters") or {}
    name = (qs.get("name") or "").strip()
    direction = (qs.get("direction") or "owed_to_me").strip()
    if direction not in ("owed_to_me", "i_owe"):
        direction = "owed_to_me"
    if not name:
        return _json(400, {"error": "contact name required"})
    db = Database()
    oi = OpenItems(db)
    summary = oi.sites_summary(user_id, name, direction)
    items = oi.list_open_items(user_id, name, direction)
    return _json(200, {
        "ok": True,
        "name": name,
        "direction": direction,
        "site_label": get_site_label(db, user_id),
        "total": summary["total"],
        "sites": summary["sites"],
        "has_sites": summary["has_sites"],
        "items": items,
    })


def _settle_open_write(event, user_id: str):
    """Apply a debt payment to a contact's open items (optionally one SITE and/or
    picked item ids), then LOG the repayment cash row so accounting keeps it out of
    P&L. Body: {name, amount, direction?, site?, picked_ids?}. Mirrors the chat
    debt repayment (settle + log a 'Debt Repayment' row)."""
    from services.database import Database
    from features.open_items import OpenItems
    data = _parse_body(event)
    name = (data.get("name") or "").strip()
    direction = (data.get("direction") or "owed_to_me").strip()
    if direction not in ("owed_to_me", "i_owe"):
        direction = "owed_to_me"
    site = data.get("site")
    if site is not None:
        site = str(site).strip() or None
    picked = data.get("picked_ids") or None
    try:
        from utils.money import money_round
        amount = money_round(data.get("amount") or 0)
    except Exception:
        amount = 0
    if not name:
        return _json(400, {"error": "contact name required"})
    if not amount or amount <= 0:
        return _json(400, {"error": "amount must be greater than 0"})

    db = Database()
    oi = OpenItems(db)
    res = oi.settle_open_items(user_id, name, amount, direction=direction,
                               picked_ids=picked, site=site)
    if not res.get("ok"):
        return _json(400, {"error": res.get("error") or "could not apply the payment"})

    # Log the cash movement as a REPAYMENT so it's out of the P&L (mirror chat +
    # the collection webhook). owed_to_me repayment = money IN (a 'sale' row with
    # category 'Debt Repayment'); i_owe repayment = money OUT (an 'expense' row).
    applied = res["applied"]
    try:
        where = f" ({site})" if site else ""
        if direction == "owed_to_me":
            db.save_transaction(user_id, applied, "sale",
                                f"Debt repayment from {name}{where}", "Debt Repayment",
                                vendor=name, payment_method="cash",
                                extra_details={"source": "open_item_settle", "site": site or ""})
        else:
            db.save_transaction(user_id, applied, "expense",
                                f"Debt repayment to {name}{where}", "Debt Repayment",
                                vendor=name, payment_method="cash",
                                extra_details={"source": "open_item_settle", "site": site or ""})
    except Exception as e:
        logger.warning(f"settle-open repayment row failed: {e}")

    # They paid manually → cancel any of this customer's PENDING pay-links so a
    # stale link can't be double-paid later.
    try:
        db.cancel_pending_requests_for(user_id, name)
    except Exception:
        pass

    return _json(200, {"ok": True, "applied": applied,
                       "remaining_payment": res.get("remaining_payment", 0),
                       "items": res.get("items", [])})


def _site_label_write(event, user_id: str):
    """Set the owner's name for the sub-account layer (Stage 2). Body: {label}."""
    from services.database import Database
    from features.open_items import set_site_label
    data = _parse_body(event)
    label = str(data.get("label") or "").strip()
    db = Database()
    stored = set_site_label(db, user_id, label)
    return _json(200, {"ok": True, "site_label": stored})


def _num_or_str(v):
    """Return a number as int (when whole) or float, else the original string.
    Keeps production quantities clean for JSON ('400' not '400.0', '0.5' kept)."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v or "")
    return int(n) if n == int(n) else n


def _records(event, user_id: str):
    """Period/date-scoped transaction LIST for the Records view. Same window
    resolver as the dashboard (_resolve_range: named period OR custom from/to,
    single day when to==from), filtered by type. Paginated (limit/offset).
    Returns rows the UI renders + a running total for the whole (unpaged) set."""
    from services.database import Database
    from utils.parser import is_bad_vendor

    qs = event.get("queryStringParameters") or {}
    tx_type = (qs.get("type") or "sale").lower()
    if tx_type not in ("sale", "purchase", "expense", "production"):
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

    from services.accounting import _is_debt_settlement

    db = Database()
    all_txns = db.get_transactions_by_period(user_id, start, end) or []
    # Exclude debt repayments/collections — they're recorded as sale/expense
    # rows for cash tracking but must NOT appear under Sales/Expenses records
    # (they'd double-count as revenue/expense). This matches the export filter
    # (handle_filtered_export) so the list and the Excel/PDF now agree.
    rows = [t for t in all_txns
            if t.get("type") == tx_type and not _is_debt_settlement(t)]
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
        row = {
            "id": str(t.get("transaction_id") or t.get("id") or ""),
            "desc": _desc(t),
            "amount": int(t.get("amount", 0) or 0),
            "vendor": str(vendor or ""),
            "date": str(t.get("date", "") or ""),
            # Full timestamp (#7) so the Records list can show time as well as date.
            "at": str(t.get("created_at", "") or t.get("at", "") or ""),
            "qty": str(t.get("quantity", "") or ""),
            # Editable-field context for the Records edit sheet.
            "payment": str(t.get("payment_method", "") or ""),
            "deposit": int(float(t.get("deposit_amount",
                             (t.get("extra_details") or {}).get("deposit_amount") or 0) or 0)),
        }
        # Production rows carry a rich batch summary in extra_details — surface
        # it so the web Records view mirrors the chat production summary (batch #,
        # good/waste, cost per unit, materials used).
        if tx_type == "production":
            ed = t.get("extra_details") or {}
            mats = []
            for m in (ed.get("materials_used") or []):
                if not isinstance(m, dict):
                    continue
                mats.append({
                    "material": str(m.get("material") or ""),
                    "qty": _num_or_str(m.get("quantity_needed", m.get("recipe_qty", ""))),
                    "unit": str(m.get("unit") or ""),
                    "cost": int(float(m.get("cost") or 0)),
                })
            row["production"] = {
                "batch": str(ed.get("batch_number") or ""),
                "produced": _num_or_str(ed.get("production_quantity", t.get("quantity", ""))),
                "good": _num_or_str(ed.get("good_quantity", "")),
                "waste": _num_or_str(ed.get("waste", 0)),
                "waste_percent": int(float(ed.get("waste_percent") or 0)),
                "cost_per_unit": int(float(ed.get("cost_per_unit") or t.get("unit_cost") or 0)),
                "materials": mats,
            }
        out.append(row)

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
        # handle_filtered_export returns a chat response list; surface a concise,
        # HONEST status to the app (don't claim success on empty/failed delivery).
        txt = ""
        download_url = ""
        if isinstance(resp, list) and resp:
            txt = (resp[0].get("content") or "")
            download_url = resp[0].get("download_url") or ""
        empty = ("No " in txt and "to export" in txt)
        failed = ("couldn't deliver" in txt or "couldn't send" in txt
                  or "failed" in txt.lower())
        ok = not empty and not failed and ("exported" in txt.lower())
        return _json(200, {
            "ok": ok,
            "delivered_to_chat": ok,
            "empty": empty,
            # When chat delivery failed but the file built, hand back the direct
            # S3 download URL so the app can offer a working link instead of a
            # dead-end error.
            "download_url": download_url,
            "message": txt or ("Your %s export was sent to your chat." % tx_type),
        })
    except Exception as e:
        logger.error(f"miniapp export failed: {e}")
        return _json(500, {"error": "export failed"})


def _receipt_send(event, user_id: str):
    """Generate + deliver a PAYMENT RECEIPT PDF for a specific transaction
    (on-demand, Phase 5a via Option A). Body: {tx_id}. Builds the receipt with
    the shared PDFGenerator and delivers it to the owner's chat via ExportService
    (same S3-upload + platform-send pipeline as exports). PDF is tier-gated,
    mirroring _export. Returns {ok, delivered_to_chat, download_url, message}."""
    from services.database import Database
    from services.pdf_generator import PDFGenerator
    from services.export_service import ExportService

    data = _parse_body(event)
    tx_id = str(data.get("tx_id") or "").strip()
    if not tx_id:
        return _json(400, {"error": "tx_id required"})

    db = Database()
    # PDF receipts are a Basic/Pro feature (mirror the export paywall).
    try:
        from services.tier_manager import TierManager
        allowed, msg = TierManager(database=db).check_can_generate_pdf(user_id)
        if not allowed:
            return _json(403, {"error": "pdf_paywalled",
                               "message": msg or "Receipts are a Basic/Pro feature."})
    except Exception:
        pass

    # Document KIND: "receipt" (default, money already received) or "invoice"
    # (a bill for this sale). Both build from the SAME transaction so the app can
    # produce either straight from a Records row — no more hopping to chat "Bill a
    # Customer" for a single item. Multi-item stays in the chat flow for now.
    kind = str(data.get("kind") or "receipt").strip().lower()
    if kind not in ("receipt", "invoice"):
        kind = "receipt"

    try:
        pdfgen = PDFGenerator(database=db)
        if kind == "invoice":
            # Build a one-line invoice from the transaction (a Kashia sale is one
            # line item). Reuse generate_invoice with an items list so it renders
            # the proper Description/Qty/Unit Price/Amount table.
            tx = db.get_transaction(user_id, tx_id) or {}
            if not tx:
                return _json(404, {"error": "that transaction is no longer in your records"})
            from utils.money import money_round as _mr
            amount = _mr(tx.get("amount", 0) or 0)
            desc = (tx.get("item_name") or tx.get("description") or "Goods/Services")
            customer = (tx.get("vendor") or "").strip() or "Customer"
            qty = tx.get("quantity") or 1
            items = [{"description": str(desc)[:60], "quantity": qty, "amount": amount}]
            filepath, filename = pdfgen.generate_invoice(
                user_id, customer, amount, str(desc)[:60], items=items, kind="invoice")
            caption = "📄 Invoice"
        else:
            filepath, filename = pdfgen.generate_receipt(user_id, transaction_id=tx_id)
            caption = "🧾 Payment receipt"
        if not filepath:
            return _json(404, {"error": f"could not build a {kind} for that transaction"})
        svc = ExportService(database=db)
        success, url = svc.deliver_file(user_id, filepath, filename, caption=caption)
        return _json(200, {
            "ok": bool(success),
            "delivered_to_chat": bool(success),
            "download_url": url or "",
            "kind": kind,
            "message": ((f"{kind.title()} sent to your chat.") if success
                        else f"Built the {kind} but couldn't deliver it — use the link."),
        })
    except Exception as e:
        logger.error(f"miniapp doc ({kind}) failed: {e}")
        return _json(500, {"error": f"could not generate the {kind}"})


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
        from utils.money import money_round
        amount = money_round(data.get("amount") or 0)   # kobo-precise
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
        # Paid manually → cancel this customer's stale PENDING pay-links so they
        # can't be double-paid (only for money-in / a customer collection).
        if direction == "in":
            try:
                db.cancel_pending_requests_for(user_id, name)
            except Exception:
                pass
        return _json(200, {
            "ok": True, "name": name, "direction": direction,
            "amount": amount, "remaining": int(remaining or 0),
        })
    except Exception as e:
        logger.error(f"miniapp debt payment failed: {e}")
        return _json(500, {"error": "could not record the payment"})


def _payment_request_write(event, user_id: str):
    """Create a Paystack payment-collection link (Phase 4). Body:
    {amount, description?, customer?}. Mints the link via
    PaystackService.initialize_collection and persists a PaymentRequest so the
    webhook can auto-mark it paid. Returns {ok, payment_url, reference}. The
    owner forwards the link to the customer (v1 — we don't message them directly).
    """
    from services.database import Database
    from services.paystack import PaystackService
    import uuid as _uuid

    data = _parse_body(event)
    try:
        from utils.money import money_round
        amount = money_round(data.get("amount") or 0)   # kobo-precise (naira)
    except (TypeError, ValueError):
        return _json(400, {"error": "amount must be a number"})
    if amount <= 0:
        return _json(400, {"error": "enter an amount greater than 0"})
    description = (data.get("description") or "").strip() or "Payment"
    customer = (data.get("customer") or "").strip()

    payreq_id = "payreq#" + _uuid.uuid4().hex[:12]
    svc = PaystackService()
    res = svc.initialize_collection(user_id, float(amount), description, payreq_id,
                                    customer_name=customer or None)
    if not res.get("success"):
        return _json(502, {"error": res.get("error") or "could not create the pay-link"})

    ref = res["reference"]
    url = res["payment_url"]
    db = Database()
    stored = db.create_payment_request(
        user_id, float(amount), description, payreq_id, ref,
        customer_name=customer or None, payment_url=url)
    if not stored:
        # The link exists but we couldn't persist the request → the webhook won't
        # find it to auto-mark paid. Fail loudly rather than hand out a link that
        # won't reconcile.
        return _json(500, {"error": "could not save the payment request — try again"})

    return _json(200, {"ok": True, "payment_url": url, "reference": ref,
                       "amount": amount, "customer": customer,
                       "description": description})


def _payment_requests_read(event, user_id: str):
    """List the owner's payment requests (Phase 5b), newest first. Optional
    ?status=pending|paid|cancelled. Trimmed to the fields the list needs."""
    from services.database import Database
    params = event.get("queryStringParameters") or {}
    status = (params.get("status") or "").strip().lower() or None
    db = Database()
    rows = db.list_payment_requests(user_id, status=status, limit=50)
    # A pending link older than this many days is flagged STALE in the UI (#11).
    # It stays payable (we don't kill the Paystack link), but the owner sees it's
    # been sitting unpaid and can cancel/remove it.
    from datetime import datetime, timedelta
    STALE_DAYS = 14
    stale_before = (datetime.now() - timedelta(days=STALE_DAYS))
    out = []
    for r in rows:
        created_full = str(r.get("created_at") or "")
        is_stale = False
        if str(r.get("status")) == "pending" and created_full:
            try:
                cdt = datetime.fromisoformat(created_full)
                is_stale = cdt < stale_before
            except Exception:
                is_stale = False
        out.append({
            "payreq_id": str(r.get("transaction_id") or ""),
            "status": str(r.get("status") or ""),
            "amount": int(float(r.get("amount") or 0)),
            "description": str(r.get("description") or ""),
            "customer": str(r.get("customer_name") or ""),
            "payment_url": str(r.get("payment_url") or ""),
            # Full timestamp (#7) so the list can show date + time.
            "created_at": created_full,
            "paid_at": str(r.get("paid_at") or ""),
            "paid_tx_id": str(r.get("paid_tx_id") or ""),
            "stale": is_stale,
            "stale_days": STALE_DAYS,
        })
    return _json(200, {"ok": True, "requests": out})


def _cancel_payment_request_write(event, user_id: str):
    """Cancel a PENDING payment request (Phase 5b). Body: {payreq_id}."""
    from services.database import Database
    data = _parse_body(event)
    payreq_id = str(data.get("payreq_id") or "").strip()
    if not payreq_id:
        return _json(400, {"error": "payreq_id required"})
    db = Database()
    ok = db.cancel_payment_request(user_id, payreq_id)
    if not ok:
        return _json(400, {"error": "could not cancel — it may be paid or already cancelled"})
    return _json(200, {"ok": True, "payreq_id": payreq_id})


def _delete_payment_request_write(event, user_id: str):
    """Remove a PAID/CANCELLED payment link from the list (#11). Body: {payreq_id}.
    A live pending link must be cancelled first. Does not touch the paid sale."""
    from services.database import Database
    data = _parse_body(event)
    payreq_id = str(data.get("payreq_id") or "").strip()
    if not payreq_id:
        return _json(400, {"error": "payreq_id required"})
    db = Database()
    ok = db.delete_payment_request(user_id, payreq_id)
    if not ok:
        return _json(400, {"error": "could not remove — cancel a pending link first"})
    return _json(200, {"ok": True, "payreq_id": payreq_id})


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
  /* Values can be long (₦2,180,000,000). Let them WRAP within the card rather
     than overflow/clip (clipping money digits is worse than a 2-line value). */
  .v { font-size: 22px; font-weight: 700; margin-top: 4px;
       overflow-wrap: anywhere; word-break: break-word;
       font-variant-numeric: tabular-nums; line-height: 1.15; }
  .row { display: flex; gap: 10px; }
  .row .card { flex: 1; min-width: 0; }   /* min-width:0 lets flex kids shrink */
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
  /* Every bottom-sheet is scrollable and capped to the viewport, so a sheet
     taller than the screen (e.g. the record form on a small phone) can always
     reach its lower fields/buttons instead of being clipped/trapped. */
  .sheet { width: 100%; background: var(--bg); border-radius: 16px 16px 0 0;
    padding: 18px 16px 26px; box-shadow: 0 -4px 24px rgba(0,0,0,.4);
    max-height: 90vh; overflow-y: auto; -webkit-overflow-scrolling: touch; }
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
  .linkbtn { background: none; border: none; cursor: pointer; font-size: 16px;
    padding: 2px 4px; margin-top: 4px; opacity: .7; }
  .linkbtn:active { opacity: 1; }
</style>
</head>
<body>
  <h1 id="biz">Kashia</h1>
  <div class="sub" id="period">Loading…</div>

  <div class="tabs">
    <div class="tab active" id="tab-dash" onclick="showTab('dash')">📊 Dashboard</div>
    <div class="tab" id="tab-cat" onclick="showTab('cat')">📦 Inventory</div>
    <div class="tab" id="tab-crm" onclick="showTab('crm')">👥 Customers</div>
    <div class="tab" id="tab-rec" onclick="showTab('rec')">📋 Records</div>
  </div>
  <button class="btn save" id="recordBtn" style="width:100%;margin-bottom:12px" onclick="openRecord()">➕ Record a transaction</button>

  <div id="view-dash">
    <!-- Stage 4: catalog-first setup nudge. Gentle + dismissable; never blocks. -->
    <div id="catnudge" class="hidden" style="background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:12px;padding:12px 14px;margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
        <div style="font-weight:700;font-size:14px" id="catnudge-title">Finish setting up your inventory</div>
        <div onclick="dismissNudge()" style="cursor:pointer;color:var(--hint);font-size:18px;line-height:1">✕</div>
      </div>
      <div class="sub2" style="margin:4px 0 8px">Good inventory setup drives accurate cost, margin and reports.</div>
      <div id="catnudge-tips"></div>
      <button class="btn save" style="width:100%;margin-top:8px" onclick="showTab('cat')">📦 Open inventory</button>
    </div>
    <div class="chips" id="chips"></div>
    <div class="chips hidden" id="dash-more-panel" style="flex-wrap:wrap"></div>
    <div class="datebox hidden" id="datebox">
      <div class="df"><label>From</label><input type="date" id="date-from"></div>
      <div class="df"><label>To (blank = single day)</label><input type="date" id="date-to"></div>
      <button class="apply" onclick="applyDateRange()">Apply</button>
      <div class="sheeterr" id="date-err" style="flex-basis:100%"></div>
    </div>

    <div class="seclabel" id="periodlabel">This period</div>
    <div class="card"><div class="k">Net profit <span class="sub" id="netpct"></span></div><div class="v" id="net">—</div></div>
    <div class="row">
      <div class="card"><div class="k">Revenue</div><div class="v" id="rev">—</div></div>
      <div class="card"><div class="k">Cost of sales</div><div class="v" id="cogs">—</div></div>
    </div>
    <div class="row">
      <div class="card"><div class="k">Gross profit <span class="sub" id="gmpct"></span></div><div class="v" id="gp">—</div></div>
      <div class="card"><div class="k">Expenses</div><div class="v" id="opex">—</div></div>
    </div>
    <div class="card"><div class="k">Cash in - out</div><div class="v" id="cash">—</div></div>

    <div class="seclabel">Current balances · as of today</div>
    <div class="card"><div class="k">💵 Cash at hand</div><div class="v" id="cashhand">—</div><div class="sub">Received in, less paid out — all time</div>
      <button class="btn cancel" style="width:100%;margin-top:10px" onclick="openCashAdjust()">± Adjust cash</button></div>
    <div class="row">
      <div class="card"><div class="k">Owed to you</div><div class="v pos" id="owed">—</div></div>
      <div class="card"><div class="k">You owe</div><div class="v neg" id="iowe">—</div><div class="sub" id="iowebreak"></div></div>
    </div>
    <div class="row">
      <div class="card tappable" onclick="showTab('cat')"><div class="k">Inventory value ›</div><div class="v" id="invval">—</div></div>
      <div class="card"><div class="k">Net worth</div><div class="v" id="netpos">—</div><div class="sub">Cash + stock + owed to you − you owe</div></div>
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
      <div class="card tappable" onclick="focusCatalogList()"><div class="k">Total stock ›</div><div class="v" id="cat-units">—</div></div>
    </div>
    <div class="card"><div class="k">Stock value (at cost)</div><div class="v" id="cat-value">—</div></div>
    <div class="card hidden" id="cat-lowcard"><div class="k">Low stock</div><div class="v neg" id="cat-low">—</div></div>
    <button class="btn save" style="width:100%;margin-bottom:10px" onclick="openAddProduct()">➕ Add product</button>
    <button class="btn cancel hidden" id="cat-add-raw" style="width:100%;margin-bottom:10px" onclick="openAddRawMaterial()">🧱 Add raw material</button>
    <input class="search" id="catsearch" placeholder="Search inventory..." oninput="renderCatalog()">
    <div id="catgroups"><div class="muted">Loading...</div></div>
    <div id="catmsg" class="muted"></div>
  </div>

  <!-- Add-product sheet -->
  <div id="addOverlay" class="overlay hidden">
    <div class="sheet">
      <h2>Add a product</h2>
      <div class="sub2">Creates a catalog item. Set price/cost/stock after.</div>
      <div class="field">
        <label>Product name</label>
        <input id="add-name" placeholder="e.g. Hilux">
      </div>
      <!-- Item type — only shown for Manufacturing/Hybrid. Choosing "finished"
           up front means the edit sheet will treat its cost as recipe-driven,
           while "raw material / supply" keeps a normal buy-cost. Trading never
           sees this (its products are plain goods). -->
      <div class="field hidden" id="add-type-wrap">
        <label>What kind of item is this?</label>
        <div class="chips" id="add-type">
          <div class="chip active" data-it="finished_product" onclick="addSetType('finished_product')">🏭 Finished product</div>
          <div class="chip" data-it="raw_material" onclick="addSetType('raw_material')">🧱 Raw material</div>
          <div class="chip" data-it="supply" onclick="addSetType('supply')">🧰 Supply</div>
        </div>
        <div class="sub2" id="add-type-hint" style="margin-top:6px">A finished product's cost is calculated from its recipe. After adding, tap it → "📋 Set / edit recipe" to build the recipe here.</div>
      </div>
      <div class="field">
        <label>Category (optional)</label>
        <input id="add-cat" placeholder="e.g. Vehicles">
      </div>
      <!-- Raw-material extras: only shown when adding a raw material directly.
           A raw material you already have on hand can carry what it cost you, its
           PRIMARY (base) stock unit, an opening quantity, and taught conversions
           — without being tied to any product yet. All optional. -->
      <div id="add-raw-extra" class="hidden">
        <!-- Material vs Overhead (#6): a raw material is stock-tracked and
             deducted on production; an overhead is a rate × usage, not stocked. -->
        <div class="field">
          <label>What kind is this?</label>
          <div class="chips" id="add-raw-kind">
            <div class="chip active" data-rk="raw_material" onclick="addRawSetKind('raw_material')">🧱 Raw material</div>
            <div class="chip" data-rk="overhead" onclick="addRawSetKind('overhead')">⚡ Overhead</div>
          </div>
          <div class="sub2" id="add-raw-kind-hint" style="margin-top:6px">Raw material: stock-tracked, deducted on production (e.g. nylon in kg).</div>
        </div>
        <div class="field">
          <label id="add-cost-label">Cost per unit (optional)</label>
          <input id="add-cost" type="number" inputmode="decimal" min="0" step="any" placeholder="e.g. 400">
        </div>
        <div class="field">
          <label>Primary (base) unit — how you track its stock</label>
          <input id="add-unit" list="add-unit-list" placeholder="e.g. kg, bag, litre">
          <datalist id="add-unit-list"></datalist>
          <div class="sub2" style="margin-top:6px">This is the PRIMARY unit — cost + stock are counted in it. Standard units (kg, g, litre, ml, m, piece) already convert; teach custom ones below.</div>
        </div>
        <div class="field" id="add-stock-wrap">
          <label>Opening stock on hand (optional)</label>
          <input id="add-stock" type="number" inputmode="decimal" min="0" step="any" placeholder="e.g. 10">
        </div>
        <!-- Teach a custom conversion (#5), e.g. "1 bag = 20 pieces". Resolved by
             the units engine so any unit you use later maps back to the base. -->
        <div class="field" id="add-conv-wrap">
          <label>Teach a unit (optional)</label>
          <input id="add-conv" placeholder="e.g. 1 bag = 20 pieces">
          <div class="sub2" style="margin-top:6px">Only needed for custom units (bag, carton, truck). Add more from the item's edit screen later.</div>
        </div>
      </div>
      <div class="sheeterr" id="add-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeAddProduct()">Cancel</button>
        <button class="btn save" id="add-save" onclick="saveAddProduct()">Add</button>
      </div>
    </div>
  </div>

  <!-- Recipe / BOM editor (Stage 2 — mfg/hybrid finished goods) -->
  <div id="recipeOverlay" class="overlay hidden">
    <div class="sheet" style="max-height:88vh;overflow-y:auto">
      <h2 id="rc-title">Recipe</h2>
      <div class="sub2">What goes into one unit. Cost is calculated from this.</div>
      <div class="card" style="margin:10px 0">
        <div class="k">Cost per unit (from recipe)</div>
        <div class="v" id="rc-unitcost">—</div>
      </div>
      <div id="rc-list"><div class="muted">Loading…</div></div>
      <!-- Add-material mini form -->
      <div class="field" style="margin-top:12px">
        <label>Add material / cost</label>
        <select id="rc-mat" style="width:100%;padding:10px;border-radius:10px;border:1px solid var(--line)" onchange="rcMatChanged()"></select>
      </div>
      <!-- New-material fields (shown only when "New material…" is picked). The
           datalist suggests EXISTING catalog names as you type, so you reuse a
           registered material instead of creating a near-duplicate (e.g. "Nylon"
           vs "nylon" vs "Wrapping Nylon"). -->
      <div class="field hidden" id="rc-newname-wrap">
        <label>New material name</label>
        <input id="rc-newname" list="rc-newname-list" autocomplete="off" placeholder="e.g. Nylon, Electricity" oninput="document.getElementById('rc-err').textContent=''">
        <datalist id="rc-newname-list"></datalist>
        <div class="sub2" id="rc-newname-hint" style="margin-top:4px"></div>
      </div>
      <!-- Raw material (stock-tracked) vs Overhead (rate x usage, no stock). -->
      <div class="field hidden" id="rc-type-wrap">
        <label>What kind of input?</label>
        <div class="chips" id="rc-type">
          <div class="chip active" data-mt="material" onclick="rcSetMatType('material')">🧱 Raw material</div>
          <div class="chip" data-mt="overhead" onclick="rcSetMatType('overhead')">⚡ Overhead</div>
        </div>
        <div class="sub2" id="rc-type-hint" style="margin-top:6px">Raw material is stock-tracked (e.g. nylon). Overhead is a rate × usage with no stock (e.g. electricity, labour time).</div>
      </div>
      <div class="row">
        <div class="field" style="flex:1">
          <label>Quantity per unit</label>
          <input id="rc-qty" type="number" inputmode="decimal" min="0" step="any" placeholder="e.g. 0.5">
        </div>
        <div class="field" style="flex:1">
          <label id="rc-unit-label">Unit</label>
          <input id="rc-unit" placeholder="e.g. kg, litre, kW, sec">
        </div>
      </div>
      <div class="sub2" id="rc-unit-hint" style="margin-top:-6px;margin-bottom:12px">
        💡 Use the SAME unit you track this material's stock in (e.g. if you buy nylon in kg, write kg here). Mixing units — kg here but grams in stock — makes the cost and stock deduction wrong.
      </div>
      <div class="field">
        <label id="rc-cost-label">Cost of ONE unit of this material (optional)</label>
        <input id="rc-cost" type="number" inputmode="decimal" min="0" step="any" placeholder="uses catalog cost">
      </div>
      <div class="sub2" id="rc-cost-hint" style="margin-top:-6px;margin-bottom:12px">
        💡 Enter the price of ONE unit (e.g. ₦1,200 per kg of nylon), NOT the total for the batch. Leave blank to use the cost already saved on the material. The product's per-unit cost is worked out for you: quantity × this cost.
      </div>
      <div class="sheeterr" id="rc-err"></div>
      <button class="btn save" id="rc-add" style="width:100%" onclick="recipeAddMaterial()">➕ Add to recipe</button>
      <div class="actions" style="margin-top:10px">
        <button class="btn cancel" onclick="closeRecipe()">Done</button>
      </div>
    </div>
  </div>

  <div id="view-crm" class="hidden">
    <div class="row">
      <div class="card tappable" onclick="crmFocusDebt('owed')"><div class="k">Owed to you ›</div><div class="v pos" id="crm-owed">—</div></div>
      <div class="card tappable" onclick="crmFocusDebt('iowe')"><div class="k">You owe ›</div><div class="v neg" id="crm-iowe">—</div><div class="sub" id="crm-iowebreak"></div></div>
    </div>
    <div class="chips" id="crm-dir-tabs">
      <div class="chip active" data-cd="customers" onclick="crmSetDir('customers')">👤 Customers</div>
      <div class="chip" data-cd="suppliers" onclick="crmSetDir('suppliers')">🏭 Suppliers</div>
      <div class="chip" data-cd="expenses" onclick="crmSetDir('expenses')">🧾 Expenses</div>
    </div>
    <div style="text-align:right;margin:2px 0 6px">
      <button class="linkbtn" onclick="openSiteLabel()" style="font-size:13px;color:var(--hint);margin-right:10px" id="crm-sitelabel-btn">⚙ Location name</button>
      <button class="linkbtn" onclick="openPaymentLinks()" style="font-size:13px;color:var(--hint)">💳 Payment links</button>
    </div>
    <input class="search" id="crmsearch" placeholder="Search people..." oninput="renderCrm()">
    <div id="crmlists"><div class="muted">Loading...</div></div>
    <div id="crmmsg" class="muted"></div>
  </div>

  <!-- Payment-links overlay: the owner's created pay-links + their status.
       Pending ones can be copied or cancelled; paid ones show as recorded. -->
  <div id="paymentLinksOverlay" class="overlay hidden">
    <div class="sheet">
      <h2>💳 Payment links</h2>
      <div class="sub2">Links you've created. Paid ones are recorded automatically.</div>
      <div id="pll-list" style="margin-top:12px"><div class="muted">Loading…</div></div>
      <div class="actions" style="margin-top:16px">
        <button class="btn cancel" onclick="closePaymentLinks()">Close</button>
      </div>
    </div>
  </div>

  <!-- Site-label setting (Stage 2): the owner names the sub-account layer. -->
  <div id="siteLabelOverlay" class="overlay hidden">
    <div class="sheet">
      <h2>Name your location layer</h2>
      <div class="sub2">What do you call the places a customer can owe at? (e.g. Mall, Property, Branch, Outlet, Project). Leave blank for "Location".</div>
      <div class="field" style="margin-top:12px">
        <label>Layer name</label>
        <input id="sitelabel-input" placeholder="e.g. Mall" maxlength="24">
      </div>
      <div class="sheeterr" id="sitelabel-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeSiteLabel()">Cancel</button>
        <button class="btn save" id="sitelabel-save" onclick="saveSiteLabel()">Save</button>
      </div>
    </div>
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
      <!-- By-site balances (Stage 2): shown only when this customer's debt is
           split across sites. Each row = one site + its balance + a Pay button
           that settles ONLY that site's items. -->
      <div class="card hidden" id="cd-sitescard">
        <div class="k" id="cd-sites-k">By location</div>
        <div id="cd-sites"></div>
      </div>
      <div class="sub2 hidden" id="cd-phone"></div>
      <div class="seclabel">Transactions</div>
      <div id="cd-txlist"><div class="muted">Loading…</div></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeContact()">Close</button>
        <button class="btn hidden" id="cd-paylink" onclick="cdGetPayLink()" style="background:var(--card);border:1px solid var(--line)">💳 Get pay-link</button>
        <button class="btn save hidden" id="cd-pay" onclick="cdRecordPayment()">💵 Record payment</button>
      </div>
    </div>
  </div>

  <!-- Payment-collection sheet: create a Paystack pay-link to send the customer -->
  <div id="payLinkOverlay" class="overlay hidden">
    <div class="sheet">
      <h2>💳 Request payment</h2>
      <div class="sub2" id="pl-sub">Create a link the customer can pay online.</div>
      <!-- Optional product picker: choosing a product prefills the amount (its
           selling price) and the description. "Something else" keeps free text.
           Populated from the catalog (invData). -->
      <div class="field" style="margin-top:12px">
        <label>Product (optional)</label>
        <select id="pl-product" onchange="plPickProduct()">
          <option value="">Something else (type below)</option>
        </select>
      </div>
      <div class="field">
        <label>Amount (₦)</label>
        <input id="pl-amount" type="number" inputmode="decimal" min="0" step="any" placeholder="e.g. 2500">
      </div>
      <div class="field">
        <label>What's it for?</label>
        <input id="pl-desc" placeholder="e.g. Invoice, 5 bags of rice">
      </div>
      <div class="sheeterr" id="pl-err"></div>
      <!-- Result: the pay-link to copy/forward -->
      <div id="pl-result" class="hidden" style="margin-top:12px">
        <div class="sub2">Send this link to the customer. When they pay, it's recorded automatically.</div>
        <div style="display:flex;gap:6px;margin-top:6px">
          <input id="pl-url" readonly style="flex:1;font-size:12px">
          <button class="btn save" id="pl-copy" onclick="plCopy()" style="flex:0 0 auto;padding:8px 12px">Copy</button>
        </div>
      </div>
      <div class="actions" style="margin-top:16px">
        <button class="btn cancel" onclick="closePayLink()">Close</button>
        <button class="btn save" id="pl-create" onclick="plCreate()">Create link</button>
      </div>
    </div>
  </div>

  <div id="view-rec" class="hidden">
    <div class="chips" id="rec-type-tabs">
      <div class="chip active" data-rt="sale" onclick="recSetType('sale')">💰 Sales</div>
      <div class="chip" data-rt="purchase" onclick="recSetType('purchase')">📦 Purchases</div>
      <div class="chip" data-rt="expense" onclick="recSetType('expense')">💸 Expenses</div>
      <div class="chip hidden" data-rt="production" id="rec-tab-production" onclick="recSetType('production')">🏭 Production</div>
    </div>
    <div class="chips" id="rec-chips"></div>
    <div class="chips hidden" id="rec-more-panel" style="flex-wrap:wrap"></div>
    <div class="datebox hidden" id="rec-datebox">
      <div class="df"><label>From</label><input type="date" id="rec-date-from"></div>
      <div class="df"><label>To (blank = single day)</label><input type="date" id="rec-date-to"></div>
      <button class="apply" onclick="recApplyDate()">Apply</button>
      <div class="sheeterr" id="rec-date-err" style="flex-basis:100%"></div>
    </div>
    <div class="card"><div class="k" id="rec-total-k">Total</div><div class="v" id="rec-total">—</div></div>
    <div class="row" id="rec-export-row">
      <button class="btn save" style="flex:1" onclick="recExport('excel')">⬇️ Excel</button>
      <button class="btn cancel" style="flex:1" onclick="recExport('pdf')">🧾 PDF</button>
    </div>
    <div style="text-align:right;margin:2px 0 6px">
      <button class="linkbtn" onclick="openRecentlyDeleted()" style="font-size:13px;color:var(--hint)">🗑 Recently deleted</button>
    </div>
    <div id="rec-undo-bar" style="display:none;margin:6px 0;padding:5px 6px 5px 10px;border-radius:8px;background:var(--card);border:1px solid var(--line);align-items:center;justify-content:space-between;gap:8px">
      <span id="rec-undo-text" class="muted" style="flex:1;font-size:12px"></span>
      <button class="linkbtn" style="flex:0 0 auto;padding:3px 8px;font-size:13px;font-weight:600;color:var(--accent)" onclick="recUndo()">↩︎ Undo</button>
    </div>
    <div id="rec-list"><div class="muted">Loading...</div></div>
    <div id="rec-msg" class="muted"></div>
  </div>

  <!-- Recently-deleted overlay: shows soft-deleted transactions from the last
       30 days; owner taps Restore to un-delete each one. -->
  <div id="recentlyDeletedOverlay" class="overlay hidden">
    <div class="sheet">
      <h2>🗑 Recently deleted</h2>
      <div class="sub2">Deleted in the last 30 days. Tap Restore to bring one back.</div>
      <div id="rd-list" style="margin-top:12px"><div class="muted">Loading…</div></div>
      <div class="actions" style="margin-top:16px">
        <button class="btn cancel" onclick="closeRecentlyDeleted()">Close</button>
      </div>
    </div>
  </div>

  <!-- Debt-payment sheet (CRM write) -->
  <div id="payOverlay" class="overlay hidden">
    <div class="sheet">
      <h2 id="pay-title">Record a payment</h2>
      <div class="sub2" id="pay-sub"></div>
      <div class="field">
        <label id="pay-amount-label">Amount (\u20a6)</label>
        <input id="pay-amount" type="number" inputmode="decimal" min="0" step="any" oninput="payHint()">
        <div class="sub2" id="pay-hint"></div>
      </div>
      <div class="sheeterr" id="pay-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closePay()">Cancel</button>
        <button class="btn save" id="pay-save" onclick="savePay()">Record payment</button>
      </div>
    </div>
  </div>

  <!-- Cash adjustment sheet (owner withdrawal / capital / bank transfer / fix) -->
  <div id="cashOverlay" class="overlay hidden">
    <div class="sheet">
      <h2>Adjust cash</h2>
      <div class="sub2" id="cash-current"></div>
      <div class="field">
        <label>What kind of move?</label>
        <div class="chips" id="cash-reason">
          <div class="chip active" data-cr="withdrawal" onclick="cashReason('withdrawal')">🏧 Owner withdrawal</div>
          <div class="chip" data-cr="injection" onclick="cashReason('injection')">➕ Money in (capital)</div>
          <div class="chip" data-cr="bank_transfer" onclick="cashReason('bank_transfer')">🏦 Bank/cash transfer</div>
          <div class="chip" data-cr="correction" onclick="cashReason('correction')">✏️ Correction</div>
          <div class="chip" data-cr="opening" onclick="cashReason('opening')">🏁 Set opening balance</div>
        </div>
      </div>
      <div class="field" id="cash-dir-wrap">
        <label>Direction</label>
        <div class="chips" id="cash-dir">
          <div class="chip active" data-cd="out" onclick="cashDir('out')">Money out</div>
          <div class="chip" data-cd="in" onclick="cashDir('in')">Money in</div>
        </div>
      </div>
      <div class="field">
        <label id="cash-amount-label">Amount (\u20a6)</label>
        <input id="cash-amount" type="number" inputmode="decimal" min="0" step="any" placeholder="e.g. 5000">
        <div class="sub2" id="cash-amount-hint"></div>
      </div>
      <div class="sheeterr" id="cash-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeCashAdjust()">Cancel</button>
        <button class="btn save" id="cash-save" onclick="saveCashAdjust()">Record</button>
      </div>
    </div>
  </div>

  <!-- Edit a recorded transaction (Records) -->
  <div id="recEditOverlay" class="overlay hidden">
    <div class="sheet">
      <h2>Edit transaction</h2>
      <div class="sub2">Changes update your stock, debt and cash to match.</div>
      <div class="field">
        <label id="re-desc-label">Description</label>
        <input id="re-desc" placeholder="what it was">
      </div>
      <div class="field">
        <label>Amount (\u20a6)</label>
        <input id="re-amount" type="number" inputmode="decimal" min="0" step="any" oninput="reBalanceHint()">
      </div>
      <div class="field" id="re-qty-wrap">
        <label>Quantity</label>
        <div class="row" style="gap:8px">
          <input id="re-qty" type="number" inputmode="decimal" min="0" step="any" style="flex:2">
          <input id="re-unit" placeholder="unit e.g. pack, kg" style="flex:1" autocomplete="off">
        </div>
      </div>
      <div class="field">
        <label id="re-who-label">Customer / supplier (optional)</label>
        <input id="re-who" placeholder="name">
      </div>
      <div class="field">
        <label>Date</label>
        <input id="re-date" type="date">
      </div>
      <div class="field">
        <label>Payment</label>
        <div class="chips" id="re-pay">
          <div class="chip" data-p="cash" onclick="reSetPay('cash')">💵 Cash</div>
          <div class="chip" data-p="transfer" onclick="reSetPay('transfer')">🏦 Transfer</div>
          <div class="chip" data-p="credit" onclick="reSetPay('credit')">📝 Credit</div>
          <div class="chip" data-p="deposit" onclick="reSetPay('deposit')">💳 Part</div>
        </div>
      </div>
      <div class="field hidden" id="re-deposit-wrap">
        <label>Deposit paid now (\u20a6)</label>
        <input id="re-deposit" type="number" inputmode="decimal" min="0" step="any" placeholder="amount paid so far" oninput="reBalanceHint()">
        <div class="sub2" id="re-balance-hint"></div>
      </div>
      <div class="sheeterr" id="re-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="recCloseEdit()">Cancel</button>
        <button class="btn save" id="re-save" onclick="recSaveEdit()">Save changes</button>
      </div>
    </div>
  </div>

  <!-- Edit sheet (bottom modal) -->
  <div id="overlay" class="overlay hidden">
    <div class="sheet" style="max-height:88vh;overflow-y:auto">
      <h2 id="sh-name">Product</h2>
      <div class="sub2" id="sh-sub"></div>
      <div class="field">
        <label>Name</label>
        <input id="sh-rename" placeholder="Product name">
      </div>
      <div class="field" id="sh-stock-wrap">
        <label>Stock</label>
        <input id="sh-stock" type="number" inputmode="decimal" min="0" step="any">
        <div class="steppers">
          <div class="step" onclick="bump(-5)">-5</div>
          <div class="step" onclick="bump(-1)">-1</div>
          <div class="step" onclick="bump(1)">+1</div>
          <div class="step" onclick="bump(5)">+5</div>
          <div class="step" onclick="bump(10)">+10</div>
        </div>
      </div>
      <div class="field" id="sh-price-wrap">
        <label>Selling price (\u20a6)</label>
        <input id="sh-price" type="number" inputmode="decimal" min="0" step="any">
      </div>
      <div class="field" id="sh-cost-wrap">
        <label id="sh-cost-label2">Cost per unit (\u20a6)</label>
        <input id="sh-cost" type="number" inputmode="decimal" min="0" step="any">
      </div>
      <!-- Mfg/Hybrid finished goods: cost is recipe-driven (Decision A). Show
           the rolled-up cost read-only + point the owner to Set Recipe in chat.
           Hidden for Trading and for raw materials, which keep a manual cost. -->
      <div class="field hidden" id="sh-recipe-cost-wrap">
        <label>Cost (from recipe)</label>
        <div id="sh-recipe-cost" class="readonly-val" style="padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card2,#f5f5f7)"></div>
        <div class="sub2" id="sh-recipe-hint" style="margin-top:6px"></div>
        <button class="btn save" id="sh-recipe-btn" style="width:100%;margin-top:8px" onclick="openRecipe()">📋 Set / edit recipe</button>
      </div>
      <div class="row">
        <div class="field" style="flex:1">
          <label>Primary unit</label>
          <input id="sh-unit" placeholder="e.g. piece, kg">
        </div>
        <div class="field" style="flex:1" id="sh-reorder-wrap">
          <label>Reorder level</label>
          <input id="sh-reorder" type="number" inputmode="numeric" min="0">
        </div>
      </div>
      <div class="field">
        <label>Category</label>
        <input id="sh-cat" placeholder="e.g. Vehicles">
      </div>
      <div class="field" id="sh-conv-wrap">
        <label>Units &amp; conversions</label>
        <div class="sub2">Standard units (kg, g, litre, ml...) work automatically. Teach custom ones like a bag or carton.</div>
        <div id="sh-units-list" class="sub2" style="margin:4px 0"></div>
        <div style="display:flex;gap:6px">
          <input id="sh-conv" placeholder="e.g. 1 bag = 20 pieces" style="flex:1">
          <button class="btn" id="sh-conv-add" onclick="addConversion()" style="white-space:nowrap">Add</button>
        </div>
        <div class="sheeterr" id="sh-conv-err"></div>
      </div>
      <div class="sheeterr" id="sh-err"></div>
      <div class="actions">
        <button class="btn cancel" onclick="closeSheet()">Cancel</button>
        <button class="btn save" id="sh-save" onclick="saveSheet()">Save changes</button>
      </div>
      <button class="btn cancel" id="sh-delete" style="width:100%;margin-top:8px;color:var(--neg)" onclick="deleteProduct()">🗑️ Delete product</button>
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
        <div class="chip hidden" data-t="produce" id="rec-type-produce" onclick="recType('produce')">🏭 Produce</div>
      </div>
      <!-- Produce: waste field (mfg/hybrid only). Amount/cost are recipe-driven. -->
      <div class="field hidden" id="rec-waste-wrap">
        <label>Waste / spoilage (optional)</label>
        <input id="rec-waste" type="number" inputmode="decimal" min="0" step="any" placeholder="units spoiled">
        <div class="sub2">💡 Units that came out bad. Cost per good unit is worked out from your recipe.</div>
      </div>
      <!-- Product picker (sale/purchase) — tap to choose from your catalog. -->
      <div class="field" id="rec-prod-wrap">
        <label id="rec-desc-label">What did you sell?</label>
        <div class="step" id="rec-prod-btn" style="text-align:left;padding:11px 12px" onclick="openPicker()">
          <span id="rec-prod-text" style="color:var(--hint)">Tap to choose a product</span>
        </div>
        <input id="rec-desc" class="hidden" placeholder="e.g. Hilux">
        <!-- Live stock + price for the picked product (sale/purchase). -->
        <div class="sub2 hidden" id="rec-prod-info" style="margin-top:6px"></div>
      </div>
      <div class="field">
        <label id="rec-amount-label">Amount received (\u20a6)</label>
        <input id="rec-amount" type="number" inputmode="decimal" min="0" step="any" oninput="recBalanceHint()">
      </div>
      <!-- Prepaid / periodic spread (EXPENSE only): pay the full amount now but
           spread the expense across N periods so one month's profit isn't
           crushed (e.g. a year's rent paid upfront). -->
      <div class="field hidden" id="rec-spread-wrap">
        <label>Spread this expense over</label>
        <select id="rec-spread" onchange="recSpreadHint()" style="width:100%;padding:11px 8px">
          <option value="">Don't spread — count it all now</option>
          <option value="3:month">3 months</option>
          <option value="6:month">6 months</option>
          <option value="12:month">12 months (1 year)</option>
          <option value="1:quarter">Each quarter (custom count below)</option>
          <option value="custom">Custom number of months…</option>
        </select>
        <input id="rec-spread-n" type="number" inputmode="numeric" min="2" max="60" step="1"
               class="hidden" placeholder="how many months (e.g. 9)" style="margin-top:6px">
        <div class="sub2" id="rec-spread-hint"></div>
      </div>
      <div class="field" id="rec-qty-wrap">
        <label id="rec-qty-label">Quantity</label>
        <div class="row" style="gap:8px">
          <input id="rec-qty" type="number" inputmode="decimal" min="0" step="any" value="1" style="flex:2">
          <select id="rec-unit-sel" class="hidden" style="flex:1;padding:11px 8px"></select>
          <input id="rec-unit-free" class="hidden" placeholder="unit e.g. litres" style="flex:1">
        </div>
        <div class="sub2" id="rec-qty-hint">💡 Counted in this item's unit (set the unit on the product in Catalog). Can be fractional, e.g. 0.5 kg. Stock drops by this amount.</div>
      </div>
      <div class="field" id="rec-cost-wrap">
        <label id="rec-cost-label">Cost of goods (total, \u20a6) — optional</label>
        <input id="rec-cost" type="number" inputmode="decimal" min="0" step="any" placeholder="for accurate profit">
        <div class="sub2">💡 Leave blank to use the cost saved on the product. Enter the TOTAL cost for this sale (all units), not per-unit — it sets your profit.</div>
      </div>
      <div class="field">
        <label id="rec-who-label">Customer (optional)</label>
        <!-- Custom tappable dropdown (NOT a native <datalist> — on mobile that
             only feeds the keyboard suggestion strip, which is fragile and
             vanishes on a tap). This is a real positioned list that filters as
             you type, is tappable, and still lets you type a brand-new name. -->
        <div style="position:relative">
          <input id="rec-who" placeholder="name" autocomplete="off"
                 oninput="recWhoFilter()" onfocus="recWhoFilter()"
                 onblur="recWhoBlur()">
          <div id="rec-who-drop" class="hidden" style="position:absolute;left:0;right:0;top:100%;z-index:60;background:var(--card);border:1px solid var(--line);border-radius:10px;margin-top:4px;max-height:210px;overflow-y:auto;box-shadow:0 6px 20px rgba(0,0,0,.35)"></div>
        </div>
        <div class="chips" id="rec-who-quick" style="margin-top:6px">
          <div class="chip" onclick="recWho('walkin')">🚶 Walk-in</div>
          <div class="chip" onclick="recWho('skip')">⏭️ Skip</div>
        </div>
      </div>
      <div class="field">
        <label>Payment</label>
        <div class="chips" id="rec-pay">
          <div class="chip active" data-p="cash" onclick="recPay('cash')">💵 Cash</div>
          <div class="chip" data-p="transfer" onclick="recPay('transfer')">🏦 Transfer</div>
          <div class="chip" data-p="credit" onclick="recPay('credit')">📝 Credit</div>
          <div class="chip" data-p="part" onclick="recPay('part')">💳 Part</div>
          <div class="chip hidden" data-p="paylink" id="rec-pay-paylink" onclick="recPay('paylink')">🔗 Pay-link</div>
        </div>
        <div class="sub2 hidden" id="rec-paylink-hint" style="margin-top:6px">Records the sale as owing, then makes a Paystack link to send. When they pay, it auto-clears the debt.</div>
      </div>
      <div class="field hidden" id="rec-deposit-wrap">
        <label id="rec-deposit-label">Deposit paid now (\u20a6)</label>
        <input id="rec-deposit" type="number" inputmode="decimal" min="0" step="any" placeholder="amount paid so far" oninput="recBalanceHint()">
        <div class="sub2" id="rec-balance-hint"></div>
      </div>
      <!-- Sub-account / SITE (Stage 2): tag an owing sale to a site so the same
           customer's balances stay separate (Delta vs Asaba). Only meaningful for
           a sale that creates a debt (credit / part / pay-link). Optional. -->
      <div class="field hidden" id="rec-site-wrap">
        <label id="rec-site-label">Location (optional)</label>
        <input id="rec-site" list="rec-site-list" placeholder="e.g. Ikeja shop, Delta">
        <datalist id="rec-site-list"></datalist>
        <div class="sub2" id="rec-site-hint">Tag this sale to a site so this customer's balance there stays separate.</div>
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
// DIAGNOSTIC: surface ANY runtime JS error on-screen (Telegram WebViews hide the
// console). If the app "loads but nothing works", the banner shows the real
// error + line so we can fix it instead of guessing. Registered BEFORE the app
// IIFE so it catches errors thrown during init too.
window.onerror = function (msg, src, line, col, err) {
  try {
    var b = document.getElementById("js-err-banner");
    if (!b) {
      b = document.createElement("div");
      b.id = "js-err-banner";
      b.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:9999;" +
        "background:#ff5c5c;color:#fff;font:12px monospace;padding:8px 10px;" +
        "white-space:pre-wrap;word-break:break-word";
      document.body.appendChild(b);
    }
    b.textContent = "JS error: " + msg + " @ line " + line + ":" + col;
  } catch (e) {}
  return false;   // still log to console where available
};
(function () {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  // Dismiss the keyboard when tapping an empty area (no input). Telegram's
  // WebView keeps the keyboard up otherwise, which the owner found frustrating.
  // If the tap target isn't a form field (or inside one), blur the focused input.
  document.addEventListener("touchend", function (e) {
    var t = e.target;
    if (t && t.closest && t.closest("input, textarea, select, button, a, [contenteditable]")) return;
    var a = document.activeElement;
    if (a && a.blur && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) a.blur();
  }, true);
  var initData = (tg && tg.initData) || "";
  // Quick chips = the common ranges. Specific Quarter/Month/Year (any one, not
  // just the current) live in the "More periods…" dropdown so the user is never
  // stuck on the current quarter.
  var PERIODS = [["today","Today"],["week","Week"],["month","This month"],["last_month","Last month"]];
  var curPeriod = "month";
  var invData = null;
  var invLoaded = false;
  var crmData = null;
  var crmLoaded = false;
  // Custom date range (single day or range). When set, overrides curPeriod.
  var curFrom = "";
  var curTo = "";
  var curSpecific = "";   // dashboard specific month/quarter/year query, if picked
  // Records tab state (independent period + range + type).
  // NB: named recTabType (NOT recType) — recType is the record-SHEET's type
  // switcher function (window.recType). Sharing the name let the Records tab
  // overwrite that function with a string, which killed the "Record a
  // transaction" button after visiting Records. Keep them separate.
  var recTabType = "sale";
  var recPeriod = "month";
  var recFrom = "";
  var recTo = "";
  var recSpecific = "";   // records specific month/quarter/year query, if picked

  // ── Industry awareness (Stage 0) ──────────────────────────────────────
  // The server tells us the business industry via /api/summary. The mini app
  // uses this ONLY to shape UI/terminology (labels, which catalog fields to
  // show). NO cost/accounting math lives in JS — the engine owns that.
  // Default "trading" keeps the control industry byte-for-byte unchanged even
  // before summary loads. Manufacturing + Hybrid share the mfg model (recipes).
  var APP = { industry: "trading", siteLabel: "Location", knownSites: [] };
  function isTrading()  { return APP.industry === "trading"; }
  function isMfg()      { return APP.industry === "manufacturing"; }
  function isServices() { return APP.industry === "services"; }
  function isHybrid()   { return APP.industry === "hybrid"; }
  // "recipe model" = finished goods derive cost from a recipe (mfg + hybrid).
  function usesRecipes() { return isMfg() || isHybrid(); }

  // ── Industry terminology (Stage 3) ───────────────────────────────────
  // Mirrors the chat-side industry TERMS so the web speaks the same language.
  // Trading is the baseline — its strings match the HTML byte-for-byte, so
  // applying labels for a Trading user is a no-op (the control never changes).
  // Only the specific labelled elements below are touched; all money math and
  // data remain identical across industries.
  var TERMS = {
    trading: {
      sale: "Sale", sales: "Sales", purchase: "Purchase", purchases: "Purchases",
      catalog: "Inventory", customers: "Customers", cogs: "Cost of sales",
      sale_emoji: "\\ud83d\\udcb0", purchase_emoji: "\\ud83d\\udce6",
      // Record-form inner labels. Trading == the original hardcoded HTML strings
      // (byte-for-byte), so the control never changes.
      sell_q: "What did you sell?", buy_q: "What did you buy?",
      customer_opt: "Customer (optional)", supplier_opt: "Supplier (optional)",
      pick_hint: "Tap to choose a product"
    },
    manufacturing: {
      sale: "Output sale", sales: "Output sales", purchase: "Raw material",
      purchases: "Raw materials", catalog: "Products & materials",
      customers: "Customers", cogs: "Production cost",
      sale_emoji: "\\ud83c\\udff7\\ufe0f", purchase_emoji: "\\ud83e\\uddf1",
      sell_q: "What finished product did you sell?", buy_q: "What raw material did you buy?",
      customer_opt: "Buyer (optional)", supplier_opt: "Supplier (optional)",
      pick_hint: "Tap to choose a product"
    },
    services: {
      sale: "Job / service", sales: "Jobs / services", purchase: "Supply purchase",
      purchases: "Supply purchases", catalog: "Services & supplies",
      customers: "Clients", cogs: "Direct costs",
      sale_emoji: "\\ud83d\\udcbc", purchase_emoji: "\\ud83d\\udce6",
      sell_q: "What service did you provide?", buy_q: "What supplies did you buy?",
      customer_opt: "Client (optional)", supplier_opt: "Supplier (optional)",
      pick_hint: "Tap to choose a service"
    },
    hybrid: {
      sale: "Sale / service", sales: "Sales / services", purchase: "Purchase",
      purchases: "Purchases", catalog: "Products & supplies",
      customers: "Customers", cogs: "Cost of sales",
      sale_emoji: "\\ud83d\\udcb0", purchase_emoji: "\\ud83d\\udce6",
      sell_q: "What did you sell?", buy_q: "What did you buy?",
      customer_opt: "Customer (optional)", supplier_opt: "Supplier (optional)",
      pick_hint: "Tap to choose a product / service"
    }
  };
  function t(key) {
    var set = TERMS[APP.industry] || TERMS.trading;
    return set[key] != null ? set[key] : (TERMS.trading[key] || "");
  }
  // Set an element's text only if it exists (defensive against markup changes).
  function setText(id, s) { var el = document.getElementById(id); if (el) el.textContent = s; }
  // Apply industry wording to the static labels. Idempotent; safe to re-run.
  var _labelsApplied = false;
  function applyIndustryLabels() {
    if (_labelsApplied) return;
    _labelsApplied = true;
    // Bottom nav — Customers vs Clients.
    setText("tab-crm", "\\ud83d\\udc65 " + t("customers"));
    setText("tab-cat", "\\ud83d\\udce6 " + t("catalog"));
    // Dashboard "Cost of sales" card label.
    var cogsCard = document.querySelector('#cogs') &&
                   document.querySelector('#cogs').parentElement.querySelector('.k');
    if (cogsCard) cogsCard.textContent = t("cogs");
    // Record sheet type chips (sale/purchase; expense stays "Expense").
    var recSale = document.querySelector('#rec-type .chip[data-t="sale"]');
    if (recSale) recSale.textContent = t("sale_emoji") + " " + t("sale");
    var recPur = document.querySelector('#rec-type .chip[data-t="purchase"]');
    if (recPur) recPur.textContent = t("purchase_emoji") + " " + t("purchase");
    // Records-tab tabs (Sales/Purchases).
    var rtSale = document.querySelector('#rec-type-tabs .chip[data-rt="sale"]');
    if (rtSale) rtSale.textContent = t("sale_emoji") + " " + t("sales");
    var rtPur = document.querySelector('#rec-type-tabs .chip[data-rt="purchase"]');
    if (rtPur) rtPur.textContent = t("purchase_emoji") + " " + t("purchases");
    // CRM directory tab — Customers vs Clients.
    var crmCust = document.querySelector('#crm-dir-tabs .chip[data-cd="customers"]');
    if (crmCust) crmCust.textContent = "\\ud83d\\udc64 " + t("customers");
    // Production is a manufacturing/hybrid concept — reveal its Records tab +
    // the "Produce" record type only for those industries.
    if (usesRecipes()) {
      var pTab = document.getElementById("rec-tab-production");
      if (pTab) pTab.classList.remove("hidden");
      var pRec = document.getElementById("rec-type-produce");
      if (pRec) pRec.classList.remove("hidden");
      // Raw materials are a manufacturing/hybrid concept — reveal the direct
      // "Add raw material" button so you can register stock not yet tied to a
      // product. Trading/services never see it (they don't keep raw materials).
      var rawBtn = document.getElementById("cat-add-raw");
      if (rawBtn) rawBtn.classList.remove("hidden");
    }
  }

  // ── Catalog-first setup nudge (Stage 4) ─────────────────────────────
  // Session-scoped dismiss: hidden until the app is reopened, then shown again
  // if setup is still incomplete. No blocking, ever.
  var nudgeDismissed = false;
  window.dismissNudge = function () {
    nudgeDismissed = true;
    var el = document.getElementById("catnudge");
    if (el) el.classList.add("hidden");
  };
  function renderCatNudge(h) {
    var box = document.getElementById("catnudge");
    if (!box) return;
    // Nothing to nudge: no catalog yet handled elsewhere; hide when complete,
    // dismissed, or no issues.
    if (nudgeDismissed || !h || !h.issue_count || h.complete) {
      box.classList.add("hidden");
      return;
    }
    var tips = h.tips || [];
    var wrap = document.getElementById("catnudge-tips");
    wrap.innerHTML = "";
    tips.forEach(function (tp) {
      var row = document.createElement("div");
      row.className = "sub2";
      row.style.cssText = "margin:3px 0;color:var(--text)";
      row.textContent = "• " + tp;
      wrap.appendChild(row);
    });
    box.classList.remove("hidden");
  }

  // Web page (not a PDF) so the ₦ glyph is safe and reads cleaner than "NGN".
  function naira(n) { return "\u20a6" + Number(n||0).toLocaleString("en-NG"); }
  // Show date + time when a full timestamp is available, else just the date.
  // `at` is an ISO timestamp (created_at); `date` is the YYYY-MM-DD fallback.
  function fmtWhen(at, date) {
    if (at) {
      var d = new Date(at);
      if (!isNaN(d.getTime())) {
        var hh = ("0" + d.getHours()).slice(-2);
        var mm = ("0" + d.getMinutes()).slice(-2);
        var day = at.slice(0, 10);
        return day + " " + hh + ":" + mm;
      }
    }
    return date || "";
  }
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
        if (!r.ok) {
          throw new Error(r.status === 401
            ? "Session expired — close and reopen the app from the ☰ menu button."
            : ("Error " + r.status));
        }
        return r.json();
      });
  }
  // Build the date query for summary/charts:
  //  - a custom from/to range (single day = from==to), OR
  //  - a SPECIFIC month/quarter/year picked from the dropdown (period + y + m/q),
  //  - else the named quick period.
  function periodQuery() {
    if (curFrom) {
      return "from=" + encodeURIComponent(curFrom) +
             "&to=" + encodeURIComponent(curTo || curFrom);
    }
    if (curSpecific) return curSpecific;   // e.g. "period=quarter&y=2026&q=1"
    return "period=" + curPeriod;
  }

  // ── Shared "specific period" options — used by both Dashboard + Records ──
  // Produces a list of {val, label} for ANY quarter/month/year (not just current).
  function buildPeriodOptions() {
    // A TRIMMED, dynamic set — the old grid (8 quarters + 3 years + 12 months
    // = 23 chips) looked cluttered. Keep it tidy: the 4 quarters of THIS year,
    // the last 6 months, and 2 years. Still fully dynamic (built from today),
    // and the 📅 Pick date control covers anything outside this set.
    var now = new Date();
    var yNow = now.getFullYear();
    var MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    var opts = [];
    for (var q = 1; q <= 4; q++) {
      opts.push({val: "period=quarter&y=" + yNow + "&q=" + q, label: "Q" + q + " " + yNow});
    }
    for (var i = 0; i < 6; i++) {
      var d = new Date(yNow, now.getMonth() - i, 1);
      opts.push({val: "period=month&y=" + d.getFullYear() + "&m=" + (d.getMonth() + 1),
                 label: MON[d.getMonth()] + " " + d.getFullYear()});
    }
    [yNow, yNow - 1].forEach(function (y) {
      opts.push({val: "period=year&y=" + y, label: "Year " + y});
    });
    return opts;
  }
  // Toggle an expanded list of specific-period chips below the main chips row.
  function toggleSpecificPanel(panelId, curSpec, onPick) {
    var panel = document.getElementById(panelId);
    if (!panel) return;
    if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
    panel.innerHTML = "";
    buildPeriodOptions().forEach(function (o) {
      var el = document.createElement("div");
      el.className = "chip" + (o.val === curSpec ? " active" : "");
      el.textContent = o.label;
      el.onclick = function () { panel.classList.add("hidden"); onPick(o.val); };
      panel.appendChild(el);
    });
    panel.classList.remove("hidden");
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
      // A quick chip is active only when no custom range / specific period set.
      el.className = "chip" + (!curFrom && !curSpecific && p[0] === curPeriod ? " active" : "");
      el.textContent = p[1];
      el.onclick = function () {
        curFrom = ""; curTo = ""; curSpecific = "";   // clear custom / specific
        curPeriod = p[0];
        toggleDatePicker(false);
        renderChips(); loadSummary();
      };
      c.appendChild(el);
    });
    // "More periods…" button — tap to expand a panel of specific quarters/months/years.
    // Uses a tap-first div panel (no <select>) for full Telegram WebView compatibility.
    var more = document.createElement("div");
    more.className = "chip" + (curSpecific ? " active" : "");
    more.textContent = curSpecific ? "Specific \u25be" : "More \u25be";
    more.onclick = function () {
      toggleSpecificPanel("dash-more-panel", curSpecific, function (val) {
        curFrom = ""; curTo = ""; curSpecific = val;
        renderChips(); loadSummary();
      });
    };
    c.appendChild(more);
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
        // Stage 0: capture the industry from the summary payload so catalog +
        // other tabs can shape their UI. Falls back to "trading" (control).
        if (d.industry) APP.industry = d.industry;
        if (d.site_label) APP.siteLabel = d.site_label;   // Stage 2 layer name
        // Stage 3: apply industry wording to static labels (once).
        applyIndustryLabels();
        // Stage 4: catalog-first setup nudge (gentle, dismissable).
        renderCatNudge(d.catalog_health);
        document.getElementById("biz").textContent = d.business || "Kashia";
        document.getElementById("period").textContent = (d.period_label || "");
        var pl = document.getElementById("periodlabel");
        if (pl) pl.textContent = (d.period_label ? (d.period_label + " · this period") : "This period");
        setSigned("net", d.pnl.net_profit);
        // Net profit margin % as a subtitle on the Net profit card.
        var npEl = document.getElementById("netpct");
        if (npEl) npEl.textContent = (d.pnl.revenue > 0) ? ("· net margin " + (d.pnl.net_margin_pct || 0) + "%") : "";
        document.getElementById("rev").textContent = naira(d.pnl.revenue);
        document.getElementById("cogs").textContent = naira(d.pnl.cogs);
        // Gross PROFIT amount + gross margin % subtitle (was showing only the %).
        document.getElementById("gp").textContent = naira(d.pnl.gross_profit);
        var gpEl = document.getElementById("gmpct");
        if (gpEl) gpEl.textContent = "· margin " + (d.pnl.gross_margin_pct || 0) + "%";
        document.getElementById("opex").textContent = naira(d.pnl.opex);
        setSigned("cash", d.cash.net);
        // All-time cash at hand (snapshot). Guard: older payloads may omit it.
        var chEl = document.getElementById("cashhand");
        if (chEl && d.cash && d.cash.at_hand != null) setSigned("cashhand", d.cash.at_hand);
        // Remember the editable opening balance for the "Set opening balance" flow.
        curOpeningCash = (d.cash && d.cash.opening != null) ? d.cash.opening : 0;
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
        // Clear the "Loading…" state so the user isn't stuck staring at it.
        document.getElementById("biz").textContent = "Kashia";
        document.getElementById("period").textContent = "";
        msg.innerHTML = '<span class="err">' + (e.message || "Could not load") +
          '</span>';
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

  // Returns the fetch promise so callers (e.g. the produce/sale picker) can
  // render off the .then instead of racing a fixed setTimeout.
  function loadInventory() {
    invLoaded = true;
    return api("api/inventory")
      .then(function (d) { invData = d.products || []; renderCatalog(); })
      .catch(function (e) {
        document.getElementById("catmsg").innerHTML =
          '<span class="err">' + (e.message || "Could not load") + '</span>';
      });
  }

  // ── Catalog tab (Inventory merged in): totals up top, then products grouped
  // by category, searchable. Every product is tappable — non-variant opens the
  // edit sheet (stock/price/cost), variant opens the read-only tree viewer.
  // Tapping the "Total stock" tile brings the per-product stock list into view
  // (the detail the user asked to "see" when tapping total stock).
  window.focusCatalogList = function () {
    var groups = document.getElementById("catgroups");
    if (groups && groups.scrollIntoView) {
      groups.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    var s = document.getElementById("catsearch");
    if (s) s.focus();
  };

  window.renderCatalog = function () {
    if (!invData) return;
    var q = (document.getElementById("catsearch").value || "").toLowerCase().trim();
    var rows = invData.filter(function (p) {
      return !q || (p.name || "").toLowerCase().indexOf(q) >= 0
                || (p.category || "").toLowerCase().indexOf(q) >= 0;
    });

    // Totals (across the FULL catalog, not just the filtered view).
    // The "Products" stat card counts SELLABLE products only (finished_product /
    // plain product), NOT raw materials / supplies / overhead — those have their
    // own sections. Stock value/units still span the whole catalog.
    function isSellable(p) {
      var t = (p.item_type || "").toLowerCase();
      return t === "" || t === "product" || t === "finished_product";
    }
    var totUnits = 0, totValue = 0, lowCount = 0, prodCount = 0;
    invData.forEach(function (p) {
      totUnits += Number(p.stock || 0);
      totValue += Number(p.stock_value || 0);
      if (p.low_stock) lowCount += 1;
      if (isSellable(p)) prodCount += 1;
    });
    document.getElementById("cat-count").textContent = prodCount.toLocaleString();
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

    // TOP-LEVEL grouping by item TYPE so a manufacturer's sellable PRODUCTS are
    // kept apart from RAW MATERIALS and OVERHEAD (they were all mixed before).
    // Each type section then lists its items (category shown inline on the row).
    function typeGroup(p) {
      var t = (p.item_type || "").toLowerCase();
      if (t === "raw_material") return "raw";
      if (t === "supply" || t === "consumable") return "supply";
      if (t === "overhead") return "overhead";
      if (t === "service") return "service";
      return "product";   // finished_product / product / "" (trading)
    }
    var TYPE_ORDER = ["product", "raw", "supply", "overhead", "service"];
    var TYPE_META = {
      product:  "Products",
      raw:      "Raw materials",
      supply:   "Supplies",
      overhead: "Overheads",
      service:  "Services"
    };
    var byType = {};
    rows.forEach(function (p) {
      var g = typeGroup(p);
      (byType[g] = byType[g] || []).push(p);
    });

    // Render one product row into a card.
    function appendRow(card, p) {
      var badges = "";
      if (p.low_stock) badges += '<span class="badge low">low</span>';
      if (p.has_variants) badges += '<span class="badge var">variants</span>';
      var sub = [];
      if (p.cost) sub.push("cost " + naira(p.cost));
      if (p.sale_price) sub.push("price " + naira(p.sale_price));
      // OVERHEAD is a rate/allocation (e.g. rent, electricity per unit), not a
      // physical count — showing "0 unit" reads like out-of-stock. Show the
      // unit/rate label only, no quantity number.
      var isOverhead = typeGroup(p) === "overhead";
      var stockLine = isOverhead
        ? escapeHtml(p.unit || "")
        : (Number(p.stock||0).toLocaleString() + ' ' + escapeHtml(p.unit || ""));
      var div = document.createElement("div");
      div.className = "item tappable";
      div.innerHTML = '<div><div class="name">' + escapeHtml(p.name || "?") + badges +
        '</div><div class="meta">' + (sub.join(" \u00b7 ") || "no price/cost set") + '</div></div>' +
        '<div class="right"><div class="stock">' + stockLine +
        (p.has_variants ? ' \u203a' : '') + '</div><div class="meta">' +
        (p.stock_value ? naira(p.stock_value) : "") + '</div></div>';
      div.onclick = p.has_variants
        ? (function (prod) { return function () { openVarView(prod); }; })(p)
        : (function (prod) { return function () { openSheet(prod); }; })(p);
      card.appendChild(div);
    }

    wrap.innerHTML = "";
    TYPE_ORDER.forEach(function (g) {
      var items = byType[g];
      if (!items || !items.length) return;
      var gValue = 0;
      items.forEach(function (p) { gValue += Number(p.stock_value || 0); });

      // Type section header (Products / Raw materials / …).
      var head = document.createElement("div");
      head.className = "k";
      head.style.margin = "16px 2px 6px";
      head.textContent = TYPE_META[g] + " \u00b7 " + items.length + " item(s) \u00b7 " + naira(gValue);
      wrap.appendChild(head);

      // Sub-group by CATEGORY within the type (Products → Juice, Water). Keeps
      // the familiar category browsing the owner had before, nested under type.
      var byCat = {};
      items.forEach(function (p) {
        var c = (p.category || "").trim() || "Uncategorized";
        (byCat[c] = byCat[c] || []).push(p);
      });
      var cats = Object.keys(byCat).sort(function (a, b) {
        if (a === "Uncategorized") return 1;
        if (b === "Uncategorized") return -1;
        return a.toLowerCase() < b.toLowerCase() ? -1 : 1;
      });

      cats.forEach(function (c) {
        var catItems = byCat[c].sort(function (a, b) {
          return (a.name || "").toLowerCase() < (b.name || "").toLowerCase() ? -1 : 1;
        });
        // Show a category sub-header only when there's more than one category in
        // this type section (a single category doesn't need a divider).
        if (cats.length > 1) {
          var sub = document.createElement("div");
          sub.className = "meta";
          sub.style.margin = "10px 4px 4px";
          sub.textContent = c + " \u00b7 " + catItems.length;
          wrap.appendChild(sub);
        }
        var card = document.createElement("div");
        card.className = "card";
        card.style.padding = "4px 0";
        catItems.forEach(function (p) { appendRow(card, p); });
        wrap.appendChild(card);
      });
    });
    document.getElementById("catmsg").textContent = rows.length + " item(s)";
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
  var crmDebtFilter = "";     // "" | "owed" (they owe you) | "iowe" (you owe)
  window.crmSetDir = function (d) {
    crmDir = d;
    var tabs = document.getElementById("crm-dir-tabs").children;
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("active", tabs[i].getAttribute("data-cd") === d);
    }
    // Switching directory clears a debt focus (it belongs to the previous view).
    crmDebtFilter = "";
    renderCrm();
  };
  // Tapping the "Owed to you" / "You owe" cards focuses the people list on
  // debtors / creditors (#9). Owed-to-you lives under Customers; you-owe spans
  // suppliers + expenses, so switch to suppliers as the primary creditor view.
  window.crmFocusDebt = function (which) {
    crmDebtFilter = which;
    if (which === "owed") crmDir = "customers";
    else if (which === "iowe") crmDir = "suppliers";
    var tabs = document.getElementById("crm-dir-tabs").children;
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("active", tabs[i].getAttribute("data-cd") === crmDir);
    }
    renderCrm();
    var el = document.getElementById("crmlists");
    if (el && el.scrollIntoView) el.scrollIntoView({behavior: "smooth", block: "start"});
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

    var list = (crmDir === "suppliers" ? crmData.suppliers
                : crmDir === "expenses" ? crmData.expense_payees
                : crmData.customers) || [];
    list = list.filter(function (c) {
      return !q || (c.name || "").toLowerCase().indexOf(q) >= 0;
    });
    // Debt focus (#9): tapping the "Owed to you" / "You owe" cards filters the
    // people list to just debtors / creditors, so the cards are a real link into
    // a debtor/creditor registry rather than a cold number.
    if (crmDebtFilter === "owed") {
      list = list.filter(function (c) { return (c.owes_me || 0) > 0; });
    } else if (crmDebtFilter === "iowe") {
      list = list.filter(function (c) { return (c.i_owe || 0) > 0; });
    }
    // Sort debtors/creditors by amount (biggest first) when focused.
    if (crmDebtFilter === "owed") list.sort(function (a, b) { return (b.owes_me||0) - (a.owes_me||0); });
    else if (crmDebtFilter === "iowe") list.sort(function (a, b) { return (b.i_owe||0) - (a.i_owe||0); });

    var wrap = document.getElementById("crmlists");
    wrap.innerHTML = "";
    // Show a clear-filter chip when a debt focus is active.
    if (crmDebtFilter) {
      var chip = document.createElement("div");
      chip.className = "muted";
      chip.style.cssText = "padding:4px 0;cursor:pointer";
      chip.innerHTML = (crmDebtFilter === "owed" ? "Showing people who owe you"
                        : "Showing people you owe") + " \u2014 <b>show all \u2715</b>";
      chip.onclick = function () { crmDebtFilter = ""; renderCrm(); };
      wrap.appendChild(chip);
    }
    if (!list.length) {
      var none = document.createElement("div");
      none.className = "muted";
      none.textContent = crmDebtFilter
        ? ("No one " + (crmDebtFilter === "owed" ? "owes you" : "you owe") + " right now.")
        : ("No " + crmDir + " yet. Record a sale (with a name) or a purchase to build your list.");
      wrap.appendChild(none);
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
    // By-site balances (Stage 2): load open items grouped by site. Shown only
    // when the debt is actually split across named sites.
    cdLoadSites(c);
    // "Get pay-link" — only for people you COLLECT from: a customer, a
    // customer+supplier ("both"), or anyone who currently owes you. Hidden for a
    // pure supplier OR an expense payee (you PAY them; you don't bill them). The
    // old check only excluded "supplier", so it wrongly showed on expense payees.
    var payLinkBtn = document.getElementById("cd-paylink");
    if (payLinkBtn) {
      var ct = String(c.type || "").toLowerCase();
      var isCollectable = (ct === "customer" || ct === "both" || ct === "client");
      var canBill = isCollectable || (c.owes_me > 0);
      payLinkBtn.classList.toggle("hidden", !canBill);
    }

    var phoneEl = document.getElementById("cd-phone");
    if (c.phone) { phoneEl.textContent = "📞 " + c.phone; phoneEl.classList.remove("hidden"); }
    else { phoneEl.classList.add("hidden"); }

    // Load this contact's transaction history (last 20) + fuller stats.
    var txbox = document.getElementById("cd-txlist");
    txbox.innerHTML = '<div class="muted">Loading…</div>';
    api("api/contact-detail?name=" + encodeURIComponent(c.name || ""))
      .then(function (d) { cdRenderTx(d); })
      .catch(function () { txbox.innerHTML = '<div class="muted">Could not load transactions.</div>'; });

    document.getElementById("cdOverlay").classList.remove("hidden");
  };
  function cdRenderTx(d) {
    var box = document.getElementById("cd-txlist");
    var rows = (d && d.transactions) || [];
    // Enrich the header stats now that we have first/last + count from history.
    if (d && d.first_date && d.last_date) {
      document.getElementById("cd-last").textContent =
        (d.first_date === d.last_date) ? d.last_date : (d.first_date + " → " + d.last_date);
    }
    if (d && d.count != null) document.getElementById("cd-txns").textContent = d.count;
    if (!rows.length) { box.innerHTML = '<div class="muted">No transactions yet.</div>'; return; }
    box.innerHTML = "";
    var card = document.createElement("div");
    card.className = "card"; card.style.padding = "4px 0";
    var ICON = { sale: "💰", purchase: "📦", expense: "💸",
                 sale_return: "↩️", purchase_return: "↩️", income: "💰" };
    rows.forEach(function (t) {
      // A debt repayment/collection is money received to CLEAR a debt, not a
      // fresh sale — show it distinctly (🧾 + "Payment received") so it isn't
      // confused with a new sale in the customer's history.
      var isPay = !!t.is_payment;
      var ic = isPay ? "🧾" : (ICON[t.type] || "•");
      var title = isPay ? "Payment received" : (t.item || t.type || "?");
      var meta = [fmtWhen(t.at, t.date)];
      if (!isPay && t.qty) meta.push(String(t.qty));
      if (t.payment) meta.push(t.payment);
      var div = document.createElement("div");
      div.className = "item";
      div.innerHTML = '<div><div class="name">' + ic + " " + escapeHtml(title) +
        '</div><div class="meta">' + escapeHtml(meta.filter(Boolean).join(" · ")) + '</div></div>' +
        '<div class="right"><div class="stock">' + naira(t.amount || 0) + '</div></div>';
      card.appendChild(div);
    });
    box.appendChild(card);
    if (d.has_more) {
      var more = document.createElement("div");
      more.className = "muted";
      more.textContent = "Showing latest 20 of " + d.count + ". Use Records or export for all.";
      box.appendChild(more);
    }
  }
  // Stage 2: load this contact's open balance grouped by site + render a Pay
  // button per site. Only shown when the debt is split across NAMED sites.
  function cdLoadSites(c) {
    var card = document.getElementById("cd-sitescard");
    var box = document.getElementById("cd-sites");
    if (!card || !box) return;
    card.classList.add("hidden"); box.innerHTML = "";
    var owes = (c.owes_me || 0) > 0, iowe = (c.i_owe || 0) > 0;
    if (!owes && !iowe) return;
    var direction = owes ? "owed_to_me" : "i_owe";
    api("api/open-items?name=" + encodeURIComponent(c.name) + "&direction=" + direction)
      .then(function (d) {
        if (!d || !d.has_sites) return;   // flat customer → no by-site card
        document.getElementById("cd-sites-k").textContent = "By " + (d.site_label || "location").toLowerCase();
        // Remember sites so the record sheet can suggest them.
        (d.sites || []).forEach(function (s) {
          if (s.site && APP.knownSites.indexOf(s.site) < 0) APP.knownSites.push(s.site);
        });
        box.innerHTML = "";
        (d.sites || []).forEach(function (s) {
          var row = document.createElement("div");
          row.className = "item"; row.style.alignItems = "center";
          var label = s.site || "No location";
          row.innerHTML = '<div style="flex:1"><div class="name">' + escapeHtml(label) +
            '</div><div class="meta">' + s.count + ' item(s)</div></div>' +
            '<div class="right" style="align-items:center;gap:6px">' +
            '<div class="stock">' + naira(s.balance) + '</div></div>';
          var right = row.querySelector(".right");
          // Only offer Pay for money OWED TO YOU (a collection). "You owe" is
          // informational here (repay via the normal Record payment).
          if (owes && s.balance > 0) {
            var b = document.createElement("button");
            b.className = "linkbtn"; b.style.cssText = "padding:4px 8px;color:var(--accent)";
            b.textContent = "Pay";
            b.addEventListener("click", (function (site, bal) {
              return function () { cdPaySite(c.name, site, bal, direction); };
            })(s.site, s.balance));
            right.appendChild(b);
          }
          box.appendChild(row);
        });
        card.classList.remove("hidden");
      })
      .catch(function () { /* non-fatal — the flat balance card still shows */ });
  }
  // Settle ONE site's items for this contact (site-level "I pick"). Prompts for
  // the amount via the existing pay sheet, pre-scoped to the site.
  window.cdPaySite = function (name, site, balance, direction) {
    cdSiteCtx = { name: name, site: site, direction: direction };
    closeContact();
    openPay(name, balance, direction === "owed_to_me" ? "in" : "out");
    // Tag the pay sheet so savePay routes through the open-item settle endpoint
    // scoped to this site.
    var t = document.getElementById("pay-title");
    if (t) t.textContent = "Payment for " + (site || "location") + " — " + name;
  };
  var cdSiteCtx = null;
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

  // ── Payment collection: create a Paystack pay-link for this customer ──
  var plCtx = null;   // {name}
  // Populate the pay-link product dropdown from the catalog (SELLABLE items
  // only — raw materials/overheads aren't things a customer pays for). invData
  // may be null if the Catalog tab was never opened, so load it if needed.
  function plFillProducts() {
    var sel = document.getElementById("pl-product");
    if (!sel) return;
    function build() {
      // Keep the first "Something else" option, drop the rest, then refill.
      sel.options.length = 1;
      var list = (invData || []).filter(function (p) {
        var it = p.item_type || "";
        return (it === "" || it === "product" || it === "finished_product");
      });
      list.sort(function (a, b) {
        return String(a.name || "").localeCompare(String(b.name || ""));
      });
      for (var i = 0; i < list.length; i++) {
        var p = list[i];
        var o = document.createElement("option");
        o.value = p.key;
        o.textContent = p.name || p.key;
        sel.appendChild(o);
      }
    }
    if (invData) { build(); }
    else { loadInventory().then(build).catch(function () {}); }
  }
  // When a product is chosen, prefill the amount (its selling price) + the
  // description. Choosing "Something else" leaves whatever's typed.
  window.plPickProduct = function () {
    var sel = document.getElementById("pl-product");
    var err = document.getElementById("pl-err");
    if (err) err.textContent = "";
    if (!sel || !sel.value) return;
    var p = (invData || []).filter(function (x) { return x.key === sel.value; })[0];
    if (!p) return;
    var price = parseFloat(p.sale_price || p.price || 0);
    document.getElementById("pl-desc").value = p.name || "";
    if (price > 0) {
      document.getElementById("pl-amount").value = price;
    } else {
      // #3: no selling price set on this product — say so instead of leaving the
      // owner wondering why the amount didn't fill. They just type it in.
      document.getElementById("pl-amount").value = "";
      if (err) err.textContent = "No selling price set for " + (p.name || "this item") +
        " — type the amount, or set a price in the catalog.";
    }
  };
  window.cdGetPayLink = function () {
    if (!cdCtx) return;
    plCtx = { name: cdCtx.name || "" };
    closeContact();
    document.getElementById("pl-sub").textContent =
      plCtx.name ? ("Create a link " + plCtx.name + " can pay online.")
                 : "Create a link the customer can pay online.";
    // Prefill the amount with what they owe (common case: collect a debt).
    document.getElementById("pl-amount").value =
      (cdCtx && cdCtx.owes_me > 0) ? cdCtx.owes_me : "";
    document.getElementById("pl-desc").value = "";
    var psel = document.getElementById("pl-product"); if (psel) psel.value = "";
    plFillProducts();
    document.getElementById("pl-err").textContent = "";
    document.getElementById("pl-result").classList.add("hidden");
    var _plc = document.getElementById("pl-create");
    _plc.disabled = false; _plc.textContent = "Create link"; _plc.classList.remove("hidden");
    document.getElementById("payLinkOverlay").classList.remove("hidden");
  };
  window.closePayLink = function () {
    document.getElementById("payLinkOverlay").classList.add("hidden");
    plCtx = null;
  };
  window.plCreate = function () {
    var err = document.getElementById("pl-err");
    err.textContent = "";
    var amt = parseFloat(document.getElementById("pl-amount").value);
    if (!(amt > 0)) { err.textContent = "Enter an amount greater than 0."; return; }
    var desc = (document.getElementById("pl-desc").value || "").trim();
    var btn = document.getElementById("pl-create");
    btn.disabled = true; btn.textContent = "Creating…";
    apiPost("api/payment-request", {
      amount: amt, description: desc, customer: (plCtx && plCtx.name) || ""
    })
      .then(function (r) {
        // A link now EXISTS — keep the button LOCKED so a second tap can't mint a
        // duplicate link (which was creating double entries). To make another,
        // the owner closes + reopens. Hide it entirely and show the result.
        btn.disabled = true;
        btn.textContent = "\u2705 Link created";
        btn.classList.add("hidden");
        var urlBox = document.getElementById("pl-url");
        urlBox.value = r.payment_url || "";
        document.getElementById("pl-result").classList.remove("hidden");
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      })
      .catch(function (e) {
        btn.disabled = false; btn.textContent = "Create link";
        err.textContent = (e && e.message) || "Could not create the link.";
      });
  };
  window.plCopy = function () {
    var urlBox = document.getElementById("pl-url");
    urlBox.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) {}
    // Modern clipboard API where available (execCommand is deprecated).
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(urlBox.value).then(function () {}, function () {});
      ok = true;
    }
    var b = document.getElementById("pl-copy");
    b.textContent = ok ? "Copied!" : "Select + copy";
    setTimeout(function () { b.textContent = "Copy"; }, 2000);
  };

  // ── Payment-links list (Phase 5b): see created links + status, cancel pending ──
  window.openPaymentLinks = function () {
    document.getElementById("paymentLinksOverlay").classList.remove("hidden");
    var list = document.getElementById("pll-list");
    list.innerHTML = '<div class="muted">Loading\u2026</div>';
    api("api/payment-requests")
      .then(function (d) { renderPaymentLinks((d && d.requests) || []); })
      .catch(function (e) {
        list.innerHTML = '<div class="err">' + escapeHtml((e && e.message) || "Could not load") + '</div>';
      });
  };
  window.closePaymentLinks = function () {
    document.getElementById("paymentLinksOverlay").classList.add("hidden");
  };
  // Site-label setting (Stage 2): name the sub-account layer.
  window.openSiteLabel = function () {
    var inp = document.getElementById("sitelabel-input");
    if (inp) inp.value = (APP.siteLabel && APP.siteLabel !== "Location") ? APP.siteLabel : "";
    document.getElementById("sitelabel-err").textContent = "";
    document.getElementById("sitelabel-save").disabled = false;
    document.getElementById("siteLabelOverlay").classList.remove("hidden");
  };
  window.closeSiteLabel = function () {
    document.getElementById("siteLabelOverlay").classList.add("hidden");
  };
  window.saveSiteLabel = function () {
    var v = (document.getElementById("sitelabel-input").value || "").trim();
    var btn = document.getElementById("sitelabel-save");
    btn.disabled = true;
    apiPost("api/site-label", { label: v })
      .then(function (r) {
        APP.siteLabel = (r && r.site_label) || "Location";
        // Reflect the new label on the CRM link + record field immediately.
        var b = document.getElementById("crm-sitelabel-btn");
        if (b) b.textContent = "\u2699 " + APP.siteLabel + " name";
        closeSiteLabel();
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      })
      .catch(function (e) {
        btn.disabled = false;
        document.getElementById("sitelabel-err").textContent = (e && e.message) || "Could not save.";
      });
  };
  function renderPaymentLinks(rows) {
    var list = document.getElementById("pll-list");
    if (!rows.length) {
      list.innerHTML = '<div class="muted">No payment links yet. Create one from a customer\u2019s card.</div>';
      return;
    }
    var BADGE = { pending: "\u23f3 Pending", paid: "\u2705 Paid", cancelled: "\u2716 Cancelled" };
    list.innerHTML = "";
    var card = document.createElement("div");
    card.className = "card"; card.style.padding = "4px 0";
    rows.forEach(function (r) {
      var div = document.createElement("div");
      div.className = "item"; div.style.alignItems = "center";
      var info = document.createElement("div");
      info.style.flex = "1";
      // Stale pending links (#11) get a badge so old unpaid links stand out.
      var badge = (BADGE[r.status] || r.status);
      if (r.stale) badge += " \u00b7 \u26a0\ufe0f " + r.stale_days + "d+ unpaid";
      var meta = [badge];
      if (r.customer) meta.push(escapeHtml(r.customer));
      meta.push(escapeHtml(fmtWhen(r.created_at, (r.created_at || "").slice(0,10))));  // date + time (#7)
      info.innerHTML = '<div class="name">' + escapeHtml(r.description || "Payment") + '</div>' +
        '<div class="meta">' + meta.join(" \u00b7 ") + '</div>';
      var right = document.createElement("div");
      right.className = "right"; right.style.gap = "6px"; right.style.alignItems = "center";
      var amt = document.createElement("div");
      amt.className = "stock"; amt.textContent = naira(r.amount || 0);
      right.appendChild(amt);
      // Pending links get Copy + Cancel; PAID links get a Receipt button.
      if (r.status === "pending" && r.payment_url) {
        var copy = document.createElement("button");
        copy.className = "linkbtn"; copy.style.cssText = "padding:4px 8px;color:var(--accent)";
        copy.textContent = "Copy";
        copy.addEventListener("click", function () {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(r.payment_url).then(function () {}, function () {});
          }
          copy.textContent = "Copied!";
          setTimeout(function () { copy.textContent = "Copy"; }, 1500);
        });
        var cancel = document.createElement("button");
        cancel.className = "linkbtn"; cancel.style.cssText = "padding:4px 8px;color:var(--neg)";
        cancel.textContent = "Cancel";
        cancel.addEventListener("click", function () { pllCancel(r.payreq_id, cancel); });
        right.appendChild(copy); right.appendChild(cancel);
      } else if (r.status === "paid" && r.paid_tx_id) {
        var rcpt = document.createElement("button");
        rcpt.className = "linkbtn"; rcpt.style.cssText = "padding:4px 8px;color:var(--accent)";
        rcpt.textContent = "🧾 Receipt";
        rcpt.addEventListener("click", function () { pllReceipt(r.paid_tx_id, rcpt); });
        right.appendChild(rcpt);
      }
      // Paid or cancelled links can be REMOVED from the list (#11) once the owner
      // is done with them. Pending links must be cancelled first (server-guarded).
      if (r.status === "paid" || r.status === "cancelled") {
        var rm = document.createElement("button");
        rm.className = "linkbtn"; rm.style.cssText = "padding:4px 8px;color:var(--neg)";
        rm.textContent = "Remove";
        rm.addEventListener("click", function () { pllDelete(r.payreq_id, rm); });
        right.appendChild(rm);
      }
      div.appendChild(info); div.appendChild(right);
      card.appendChild(div);
    });
    list.appendChild(card);
    // Discoverability nudge: tell the owner receipts exist (some won't know).
    var tip = document.createElement("div");
    tip.className = "muted"; tip.style.cssText = "font-size:12px;margin-top:8px";
    tip.textContent = "💡 Tap 🧾 Receipt on a paid item to send yourself a PDF receipt.";
    list.appendChild(tip);
  }
  window.pllReceipt = function (txId, btn) {
    if (!txId) return;
    btn.disabled = true; btn.textContent = "Sending…";
    apiPost("api/receipt", { tx_id: txId })
      .then(function (r) {
        btn.disabled = false; btn.textContent = "🧾 Receipt";
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        if (tg && tg.showAlert) {
          tg.showAlert(r.delivered_to_chat ? "Receipt sent to your chat."
                       : (r.message || "Couldn't deliver the receipt."));
        }
      })
      .catch(function (e) {
        btn.disabled = false; btn.textContent = "🧾 Receipt";
        var err = document.createElement("div");
        err.className = "err"; err.style.fontSize = "12px";
        err.textContent = (e && e.body && e.body.message) || (e && e.message) || "Could not send receipt";
        btn.parentNode.appendChild(err);
      });
  };
  window.pllCancel = function (payreqId, btn) {
    if (!payreqId) return;
    btn.disabled = true; btn.textContent = "Cancelling\u2026";
    apiPost("api/cancel-payment-request", { payreq_id: payreqId })
      .then(function () { openPaymentLinks(); })   // refresh the list
      .catch(function (e) {
        btn.disabled = false; btn.textContent = "Cancel";
        var err = document.createElement("div");
        err.className = "err"; err.style.fontSize = "12px";
        err.textContent = (e && e.message) || "Could not cancel";
        btn.parentNode.appendChild(err);
      });
  };
  // Remove a paid/cancelled link from the list (#11). Two-tap confirm.
  var pllPendingDel = null;
  window.pllDelete = function (payreqId, btn) {
    if (!payreqId) return;
    if (pllPendingDel !== payreqId) {
      pllPendingDel = payreqId;
      btn.textContent = "Tap again";
      setTimeout(function () {
        if (pllPendingDel === payreqId) { pllPendingDel = null; btn.textContent = "Remove"; }
      }, 3000);
      return;
    }
    pllPendingDel = null;
    btn.disabled = true; btn.textContent = "Removing\u2026";
    apiPost("api/delete-payment-request", { payreq_id: payreqId })
      .then(function () { openPaymentLinks(); })   // refresh the list
      .catch(function (e) {
        btn.disabled = false; btn.textContent = "Remove";
        var err = document.createElement("div");
        err.className = "err"; err.style.fontSize = "12px";
        err.textContent = (e && e.message) || "Could not remove";
        btn.parentNode.appendChild(err);
      });
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
    cdSiteCtx = null;   // clear any site scoping
  };
  window.payHint = function () {
    if (!payCtx) return;
    var v = parseFloat(document.getElementById("pay-amount").value) || 0;   // money, kobo
    var rem = Math.max(0, (payCtx.amount || 0) - v);
    document.getElementById("pay-hint").textContent =
      v > 0 ? ("Remaining after this: " + naira(rem)) : "";
  };
  window.savePay = function () {
    if (!payCtx) return;
    var v = parseFloat(document.getElementById("pay-amount").value) || 0;   // money, kobo
    var err = document.getElementById("pay-err");
    if (v <= 0) { err.textContent = "Enter an amount greater than 0."; return; }
    var btn = document.getElementById("pay-save");
    btn.disabled = true;
    function done() {
      closePay();
      crmLoaded = false; loadCrm();   // a payment moves cash + the balance
      loadSummary();
    }
    function fail(e) {
      btn.disabled = false;
      err.textContent = (e && e.message) || "Could not record the payment.";
    }
    // Site-scoped payment (Stage 2): route through the open-item settle endpoint
    // so it clears ONLY that site's items (the engine logs the repayment row).
    if (cdSiteCtx && cdSiteCtx.name === payCtx.name) {
      apiPost("api/settle-open", {
        name: cdSiteCtx.name, amount: v,
        direction: cdSiteCtx.direction, site: cdSiteCtx.site
      }).then(function () { cdSiteCtx = null; done(); }).catch(fail);
      return;
    }
    var body = {
      submit_id: "pay_" + Date.now() + "_" + Math.random().toString(36).slice(2, 8),
      name: payCtx.name, amount: v, direction: payCtx.direction
    };
    apiPost("api/debt-payment", body).then(done).catch(fail);
  };

  // ── Cash adjustment (manual cash in/out that isn't a sale/purchase/expense) ──
  var cashReasonVal = "withdrawal";
  var cashDirVal = "out";
  var curOpeningCash = 0;   // last-known opening balance (for the opening flow)
  window.openCashAdjust = function () {
    cashReasonVal = "withdrawal"; cashDirVal = "out";
    document.getElementById("cash-amount").value = "";
    document.getElementById("cash-err").textContent = "";
    document.getElementById("cash-save").disabled = false;
    // Show the current balance for context (from the last loaded summary).
    var cur = document.getElementById("cashhand");
    var curTxt = cur ? (cur.textContent || "").trim() : "";
    document.getElementById("cash-current").textContent =
      (curTxt && curTxt !== "\u2014") ? ("Now: " + curTxt + " at hand") : "";
    cashReason("withdrawal");
    document.getElementById("cashOverlay").classList.remove("hidden");
  };
  window.closeCashAdjust = function () {
    document.getElementById("cashOverlay").classList.add("hidden");
  };
  function cashSyncChips(group, val, attr) {
    var chips = document.querySelectorAll('#' + group + ' .chip');
    chips.forEach(function (c) { c.classList.toggle("active", c.getAttribute(attr) === val); });
  }
  window.cashReason = function (r) {
    cashReasonVal = r;
    cashSyncChips("cash-reason", r, "data-cr");
    var dirWrap = document.getElementById("cash-dir-wrap");
    var amtLabel = document.getElementById("cash-amount-label");
    var amtHint = document.getElementById("cash-amount-hint");
    var amtInput = document.getElementById("cash-amount");
    if (r === "opening") {
      // ABSOLUTE starting balance, not a ± movement. Hide direction, relabel,
      // and prefill with the current opening value.
      dirWrap.style.display = "none";
      amtLabel.textContent = "Opening cash balance (\u20a6)";
      if (amtHint) amtHint.textContent =
        "\\uD83C\\uDFC1 The cash you had before you started recording in Kashia. " +
        "It shifts your cash at hand \\u0026 net worth.";
      amtInput.value = (curOpeningCash != null ? curOpeningCash : 0);
      return;
    }
    // Non-opening reasons: a ± movement. Restore the amount label + clear hint.
    amtLabel.textContent = "Amount (\u20a6)";
    if (amtHint) amtHint.textContent = "";
    if (amtInput.value && cashReasonVal !== "opening") { /* keep typed value */ }
    // Withdrawal is always out; capital injection always in. Bank transfer +
    // correction can go either way, so let the owner pick the direction.
    if (r === "withdrawal") { cashDir("out"); dirWrap.style.display = "none"; }
    else if (r === "injection") { cashDir("in"); dirWrap.style.display = "none"; }
    else { dirWrap.style.display = ""; }
  };
  window.cashDir = function (d) {
    cashDirVal = d;
    cashSyncChips("cash-dir", d, "data-cd");
  };
  window.saveCashAdjust = function () {
    var err = document.getElementById("cash-err");
    err.textContent = "";
    var raw = document.getElementById("cash-amount").value;
    var amt = parseFloat(raw) || 0;
    var btn = document.getElementById("cash-save");
    // OPENING BALANCE: an ABSOLUTE value (0 is allowed — clears it). Separate
    // endpoint, no direction. Everything else is a ± movement (must be > 0).
    if (cashReasonVal === "opening") {
      if (raw === "" || amt < 0) { err.textContent = "Enter the opening balance (0 or more)."; return; }
      btn.disabled = true;
      apiPost("api/opening-cash", { amount: amt })
        .then(function () {
          closeCashAdjust();
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
          loadSummary();   // opening shifts cash at hand + net worth.
        })
        .catch(function (e) {
          btn.disabled = false;
          err.textContent = (e && e.message) || "Could not save the opening balance.";
        });
      return;
    }
    if (amt <= 0) { err.textContent = "Enter an amount greater than 0."; return; }
    btn.disabled = true;
    apiPost("api/cash-adjust", { amount: amt, direction: cashDirVal, reason: cashReasonVal })
      .then(function () {
        closeCashAdjust();
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        loadSummary();   // cash at hand + net worth both move.
      })
      .catch(function (e) {
        btn.disabled = false;
        err.textContent = (e && e.message) || "Could not record the adjustment.";
      });
  };

  // ── Records tab: period/date-scoped transaction list + export ──
  var REC_PERIODS = [["today","Today"],["week","Week"],["month","This month"],
                     ["last_month","Last month"]];
  var recExpFilter = "";   // active expense-name drill (Expenses tab); "" = all
  window.recSetType = function (t) {
    recTabType = t;
    recExpFilter = "";     // clear any expense drill when switching type
    var tabs = document.getElementById("rec-type-tabs").children;
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("active", tabs[i].getAttribute("data-rt") === t);
    }
    var k = document.getElementById("rec-total-k");
    // Industry wording (param 't' shadows the term helper here, so read the
    // TERMS set directly). Lower-cased to read naturally after "Total".
    var _ts = TERMS[APP.industry] || TERMS.trading;
    var _word = (t === "sale" ? _ts.sales
                 : t === "purchase" ? _ts.purchases
                 : t === "production" ? "production cost"
                 : "expenses");
    k.textContent = "Total " + String(_word).toLowerCase();
    // Export (Excel/PDF) covers sale/purchase/expense only — hide it for the
    // Production tab (the exporter has no production filter).
    var exRow = document.getElementById("rec-export-row");
    if (exRow) exRow.style.display = (t === "production") ? "none" : "";
    loadRecords();
  };
  function recRenderChips() {
    var c = document.getElementById("rec-chips");
    c.innerHTML = "";
    REC_PERIODS.forEach(function (p) {
      var el = document.createElement("div");
      el.className = "chip" + (!recFrom && !recSpecific && p[0] === recPeriod ? " active" : "");
      el.textContent = p[1];
      el.onclick = function () {
        recFrom = ""; recTo = ""; recSpecific = ""; recPeriod = p[0];
        document.getElementById("rec-datebox").classList.add("hidden");
        recRenderChips(); loadRecords();
      };
      c.appendChild(el);
    });
    // "More periods…" button — tap-first panel, no <select>, Telegram WebView safe.
    var more2 = document.createElement("div");
    more2.className = "chip" + (recSpecific ? " active" : "");
    more2.textContent = recSpecific ? "Specific \u25be" : "More \u25be";
    more2.onclick = function () {
      toggleSpecificPanel("rec-more-panel", recSpecific, function (val) {
        recFrom = ""; recTo = ""; recSpecific = val;
        document.getElementById("rec-datebox").classList.add("hidden");
        recRenderChips(); loadRecords();
      });
    };
    if (recSpecific) more2.classList.add("active");
    c.appendChild(more2);
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
    recFrom = f; recTo = t || f; recSpecific = "";
    recRenderChips(); loadRecords();
  };
  function recQuery() {
    var q = "type=" + recTabType;
    if (recFrom) {
      q += "&from=" + encodeURIComponent(recFrom) + "&to=" + encodeURIComponent(recTo || recFrom);
    } else if (recSpecific) {
      q += "&" + recSpecific;
    } else {
      q += "&period=" + recPeriod;
    }
    return q;
  }
  function loadRecords() {
    recExpFilter = "";   // a fresh period/type load starts unfiltered
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
    recRenderUndoBar();   // persistent Undo survives tab switches / reloads
    var list = document.getElementById("rec-list");
    var rows = d.records || [];
    if (!rows.length) {
      list.innerHTML = '<div class="muted">No records in ' + escapeHtml(d.period_label || "this period") + '.</div>';
      document.getElementById("rec-msg").textContent = "";
      return;
    }
    list.innerHTML = "";

    // Production records get their own richer layout (batch #, good/waste,
    // cost/unit, materials used) mirroring the chat production summary.
    if (recTabType === "production") {
      rows.forEach(function (t) {
        var p = t.production || {};
        var card = document.createElement("div");
        card.className = "card";
        var head = '<div class="item" style="border:none;padding:6px 0">' +
          '<div><div class="name">' + (p.batch ? escapeHtml(p.batch) + " \u00b7 " : "") +
          escapeHtml(t.desc || "?") + '</div><div class="meta">' + escapeHtml(t.date || "") +
          (p.good !== "" && p.good != null ? " \u00b7 " + p.good + " good" : "") +
          (Number(p.waste) > 0 ? " \u00b7 " + p.waste + " waste (" + p.waste_percent + "%)" : "") +
          '</div></div><div class="right"><div class="stock">' + naira(t.amount || 0) +
          '</div><div class="meta">' + (p.cost_per_unit ? naira(p.cost_per_unit) + "/unit" : "") +
          '</div></div></div>';
        var matsHtml = "";
        (p.materials || []).forEach(function (m) {
          matsHtml += '<div class="meta" style="padding:2px 0">\u2022 ' +
            escapeHtml(m.material) + ": " + (m.qty !== "" ? m.qty + " " : "") +
            escapeHtml(m.unit || "") + (m.cost ? " \u00b7 " + naira(m.cost) : "") + '</div>';
        });
        card.innerHTML = head + (matsHtml
          ? '<div style="border-top:1px solid var(--line);margin-top:4px;padding-top:6px">' +
            '<div class="k" style="margin-bottom:4px">Materials used</div>' + matsHtml + '</div>'
          : "");
        list.appendChild(card);
      });
      var pnote = d.count + " batch(es) \u00b7 " + (d.period_label || "");
      if (d.has_more) pnote += " \u00b7 showing " + rows.length + " of " + d.count;
      document.getElementById("rec-msg").textContent = pnote;
      return;
    }

    // Expense analysis: group by name (e.g. Fuel, Rent) so the owner sees where
    // money goes at a glance and can tap a name to see just those entries. Only
    // for the Expenses tab; sales/purchases keep the plain chronological list.
    if (recTabType === "expense" && !recExpFilter) {
      var groups = {};
      rows.forEach(function (t) {
        var name = (t.desc || "Other").trim();
        var key = name.toLowerCase();
        if (!groups[key]) groups[key] = { name: name, total: 0, count: 0 };
        groups[key].total += Number(t.amount || 0);
        groups[key].count += 1;
      });
      var gk = Object.keys(groups);
      if (gk.length > 1) {   // a single expense name adds no insight
        gk.sort(function (a, b) { return groups[b].total - groups[a].total; });
        var seclbl = document.createElement("div");
        seclbl.className = "seclabel"; seclbl.textContent = "Spend by expense";
        list.appendChild(seclbl);
        var gcard = document.createElement("div");
        gcard.className = "card"; gcard.style.padding = "4px 0";
        gk.forEach(function (k) {
          var g = groups[k];
          var gd = document.createElement("div");
          gd.className = "item tappable";
          gd.innerHTML = '<div><div class="name">' + escapeHtml(g.name) +
            '</div><div class="meta">' + g.count + " entr" + (g.count === 1 ? "y" : "ies") + '</div></div>' +
            '<div class="right"><div class="stock">' + naira(g.total) + '</div></div>';
          gd.onclick = (function (nm) { return function () { recExpFilter = nm; renderRecords(d); }; })(g.name);
          gcard.appendChild(gd);
        });
        list.appendChild(gcard);
        var alllbl = document.createElement("div");
        alllbl.className = "seclabel"; alllbl.textContent = "All entries";
        list.appendChild(alllbl);
      }
    }

    // Apply an active expense-name filter (set by tapping a group above).
    var shown = rows;
    if (recTabType === "expense" && recExpFilter) {
      var want = recExpFilter.toLowerCase();
      shown = rows.filter(function (t) { return (t.desc || "").trim().toLowerCase() === want; });
      var back = document.createElement("div");
      back.className = "item tappable";
      back.innerHTML = '<div class="name">\\u2b05\\ufe0f All expenses</div>' +
        '<div class="right"><div class="meta">' + escapeHtml(recExpFilter) + '</div></div>';
      back.onclick = function () { recExpFilter = ""; renderRecords(d); };
      list.appendChild(back);
    }

    var card = document.createElement("div");
    card.className = "card"; card.style.padding = "4px 0";
    shown.forEach(function (t) {
      var meta = [fmtWhen(t.at, t.date)];   // date + time (#7) when available
      if (t.qty) meta.push(String(t.qty));
      if (t.vendor) meta.push(t.vendor);
      var div = document.createElement("div");
      div.className = "item";
      // ✏️ edit + 🗑 delete affordances per row. Edit opens a prefilled sheet
      // and re-applies effects server-side; delete reverses + removes.
      // 🧾 receipt + 📄 invoice let you generate a document straight from a SALE
      // row (no more hopping to chat "Bill a Customer" for a single item).
      var editBtn = t.id
        ? '<button class="linkbtn recedit" title="Edit">✏️</button>' : '';
      var delBtn = t.id
        ? '<button class="linkbtn recdel" title="Delete">🗑️</button>' : '';
      var docBtns = (t.id && recTabType === "sale")
        ? '<button class="linkbtn recrcpt" title="Receipt">🧾</button>' +
          '<button class="linkbtn recinv" title="Invoice">📄</button>' : '';
      div.innerHTML = '<div><div class="name">' + escapeHtml(t.desc || "?") +
        '</div><div class="meta">' + escapeHtml(meta.filter(Boolean).join(" \u00b7 ")) + '</div></div>' +
        '<div class="right"><div class="stock">' + naira(t.amount || 0) + '</div>' +
        '<div>' + docBtns + editBtn + delBtn + '</div></div>';
      var eb = div.querySelector(".recedit");
      if (eb) eb.onclick = (function (row) {
        return function (ev) { ev.stopPropagation(); recOpenEdit(row); };
      })(t);
      var btn = div.querySelector(".recdel");
      if (btn) btn.onclick = (function (id, label) {
        return function (ev) { ev.stopPropagation(); recDelete(id, label); };
      })(t.id, t.desc || "this entry");
      var rb = div.querySelector(".recrcpt");
      if (rb) rb.onclick = (function (id) {
        return function (ev) { ev.stopPropagation(); recSendDoc(id, "receipt"); };
      })(t.id);
      var ib = div.querySelector(".recinv");
      if (ib) ib.onclick = (function (id) {
        return function (ev) { ev.stopPropagation(); recSendDoc(id, "invoice"); };
      })(t.id);
      card.appendChild(div);
    });
    list.appendChild(card);
    var note = d.count + " record(s) · " + (d.period_label || "");
    if (d.has_more) note += " · showing " + rows.length + " of " + d.count + " (narrow the date or export for all)";
    document.getElementById("rec-msg").textContent = note;
  }
  // Generate a receipt or invoice for a single SALE record and deliver the PDF
  // to the owner's chat (same pipeline as the on-demand receipt). One tap.
  window.recSendDoc = function (id, kind) {
    if (!id) return;
    var msg = document.getElementById("rec-msg");
    msg.textContent = (kind === "invoice" ? "Building invoice\u2026" : "Building receipt\u2026");
    apiPost("api/receipt", { tx_id: id, kind: kind })
      .then(function (r) {
        msg.textContent = (r && r.message) || "Done.";
        if (r && !r.delivered_to_chat && r.download_url) {
          msg.textContent = (r.message || "") + " " + r.download_url;
        }
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      })
      .catch(function (e) {
        msg.textContent = (e && e.message) || ("Could not build the " + kind + ".");
      });
  };
  // Delete a recorded transaction (two-tap confirm). First tap arms; a second
  // tap within a few seconds confirms. Reversal happens server-side.
  var recPendingDelete = null;
  window.recDelete = function (id, label) {
    var msg = document.getElementById("rec-msg");
    if (recPendingDelete === id) {
      recPendingDelete = null;
      msg.textContent = "Deleting…";
      apiPost("api/void-transaction", { tx_id: id })
        .then(function (res) {
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
          // Stash the deleted row so a PERSISTENT Undo bar can restore it
          // verbatim. Set BEFORE loadRecords so the re-render paints the bar.
          if (res && res.voided_tx) recSetUndo(res.voided_tx, label);
          loadRecords();
          loadSummary();   // stock/debt/cash may have moved.
        })
        .catch(function (e) {
          msg.textContent = (e && e.message) || "Could not delete.";
        });
      return;
    }
    recPendingDelete = id;
    msg.textContent = "Tap 🗑 again to permanently delete " + label + ".";
    setTimeout(function () {
      if (recPendingDelete === id) { recPendingDelete = null; msg.textContent = ""; }
    }, 4000);
  };
  // ── Persistent Undo-after-delete ──
  // The deleted row is kept in recUndoTx and shown in the rec-undo-bar, which
  // is REDRAWN on every renderRecords — so switching tabs, reloading, or a
  // stray tap does NOT lose the Undo. It clears only when: the owner taps Undo,
  // deletes another entry (that becomes the new undoable), or ~5 minutes pass.
  var recUndoTx = null, recUndoLabel = "", recUndoTimer = null;
  function recSetUndo(tx, label) {
    recUndoTx = tx; recUndoLabel = label || "entry";
    if (recUndoTimer) clearTimeout(recUndoTimer);
    recUndoTimer = setTimeout(function () { recUndoTx = null; recRenderUndoBar(); }, 300000);
    recRenderUndoBar();
  }
  function recRenderUndoBar() {
    var bar = document.getElementById("rec-undo-bar");
    if (!bar) return;
    // Show the Undo bar ONLY on the tab whose type matches the deleted row, so
    // it never appears on a tab where that entry doesn't belong (and never on
    // Production, which has no delete). The deleted row's own type is the gate.
    var undoType = recUndoTx ? String(recUndoTx.type || "") : "";
    var showable = recUndoTx && undoType === recTabType;
    // NB: the bar has an INLINE display:flex, which OVERRIDES the .hidden class
    // (.hidden{display:none} loses to an inline style). That's why adding/removing
    // .hidden never actually hid the bar — it stayed visible on every tab incl.
    // Production and kept a stuck "Restoring…". Toggle the inline display DIRECTLY.
    if (!showable) { bar.style.display = "none"; return; }
    var txt = document.getElementById("rec-undo-text");
    if (txt) txt.textContent = "Deleted \u201c" + recUndoLabel + "\u201d.";
    bar.style.display = "flex";
  }
  // Force-hide the Undo bar and forget the undoable row. Used by a hard timer
  // so "Restoring…" can NEVER stick on screen, even if the network response is
  // slow or never resolves.
  function recClearUndo() {
    recUndoTx = null;
    if (recUndoTimer) { clearTimeout(recUndoTimer); recUndoTimer = null; }
    recRenderUndoBar();
  }
  window.recUndo = function () {
    if (!recUndoTx) return;
    var tx = recUndoTx;
    var txt = document.getElementById("rec-undo-text");
    if (txt) txt.textContent = "Restoring\u2026";
    // Guaranteed auto-clear: whatever happens with the request, wipe the bar
    // after 5s so the "Restoring…" label always disappears (the fix the owner
    // asked for). Success clears it sooner via the .then below.
    if (recUndoTimer) { clearTimeout(recUndoTimer); }
    recUndoTimer = setTimeout(recClearUndo, 5000);
    apiPost("api/restore-transaction", { tx: tx })
      .then(function () {
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        recClearUndo();     // hide immediately on success
        loadRecords();
        loadSummary();
      })
      .catch(function (e) {
        // On failure, show the error briefly but still let the 5s timer clear it.
        if (txt) txt.textContent = (e && e.message) || "Could not undo — tap Undo again.";
      });
  };

  // ── Recently-deleted screen ────────────────────────────────────────────────
  // Shows all user-voided transactions from the last 30 days (persisted
  // server-side as soft-deletes) so the owner can restore any of them, even
  // after closing and reopening the mini app.
  window.openRecentlyDeleted = function () {
    document.getElementById("recentlyDeletedOverlay").classList.remove("hidden");
    var list = document.getElementById("rd-list");
    list.innerHTML = '<div class="muted">Loading\u2026</div>';
    api("api/deleted-transactions")
      .then(function (d) { renderRecentlyDeleted(d.deleted || []); })
      .catch(function (e) {
        list.innerHTML = '<div class="err">' + escapeHtml((e && e.message) || "Could not load") + '</div>';
      });
  };
  window.closeRecentlyDeleted = function () {
    document.getElementById("recentlyDeletedOverlay").classList.add("hidden");
  };
  function renderRecentlyDeleted(rows) {
    var list = document.getElementById("rd-list");
    // Scope the list to the record type the owner is currently looking at, so a
    // deleted expense doesn't show under Raw materials etc. The active tab's
    // type (recTabType) maps 1:1 to a tx type; production is never restoreable
    // here (the server already omits it), so this list is empty on that tab.
    var all = rows || [];
    var scoped = all.filter(function (t) { return String(t.type || "") === recTabType; });
    if (!scoped.length) {
      list.innerHTML = '<div class="muted">Nothing deleted here in the last 30 days.</div>';
      return;
    }
    list.innerHTML = "";
    var card = document.createElement("div");
    card.className = "card"; card.style.padding = "4px 0";
    scoped.forEach(function (t) {
      var div = document.createElement("div");
      div.className = "item";
      div.style.alignItems = "center";
      var info = document.createElement("div");
      info.style.flex = "1";
      info.innerHTML =
        '<div class="name">' + escapeHtml(t.desc || "Entry") + '</div>' +
        '<div class="meta">' + escapeHtml(t.date || "") +
          (t.vendor ? " \u00b7 " + escapeHtml(t.vendor) : "") +
          ' \u00b7 deleted ' + escapeHtml(t.deleted_at || "?") + '</div>';
      var right = document.createElement("div");
      right.className = "right";
      right.style.gap = "6px"; right.style.alignItems = "center";
      var amt = document.createElement("div");
      amt.className = "stock"; amt.textContent = naira(t.amount || 0);
      var btn = document.createElement("button");
      btn.className = "btn save";
      btn.style.padding = "5px 12px"; btn.style.fontSize = "13px";
      btn.style.flex = "0 0 auto";
      btn.textContent = "Restore";
      // Bind the handler in a closure instead of an inline onclick with an
      // interpolated id — a transaction_id containing a quote/backslash broke
      // the inline onclick attribute, so the tap did nothing (no request ever
      // fired). This binding is immune to the id's contents.
      btn.addEventListener("click", function () { rdRestore(t.transaction_id, btn); });
      right.appendChild(amt); right.appendChild(btn);
      div.appendChild(info); div.appendChild(right);
      card.appendChild(div);
    });
    list.appendChild(card);
  }
  window.rdRestore = function (txId, btn) {
    if (!txId) return;
    btn.disabled = true; btn.textContent = "Restoring\u2026";
    apiPost("api/restore-transaction", { tx_id: txId })
      .then(function () {
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        btn.textContent = "Restored!";
        // Clear the inline Undo bar for this tx too (covers the common case where
        // the owner used the list instead of the bar for the most-recent delete).
        if (recUndoTx && recUndoTx.transaction_id === txId) {
          recUndoTx = null;
          if (recUndoTimer) { clearTimeout(recUndoTimer); recUndoTimer = null; }
          recRenderUndoBar();
        }
        loadRecords(); loadSummary();
        // Refresh the overlay list so the restored row drops out of it.
        api("api/deleted-transactions")
          .then(function (d) { renderRecentlyDeleted(d.deleted || []); })
          .catch(function () {});
      })
      .catch(function (e) {
        btn.disabled = false; btn.textContent = "Restore";
        var err = document.createElement("div");
        err.className = "err"; err.style.fontSize = "12px";
        err.textContent = (e && e.message) || "Could not restore";
        btn.parentNode.appendChild(err);
      });
  };

  // ── Edit a recorded transaction ──
  var reEditId = null, rePayVal = "cash";
  window.recOpenEdit = function (row) {
    reEditId = row.id;
    rePayVal = (row.payment || "cash").toLowerCase();
    document.getElementById("re-desc").value = row.desc || "";
    document.getElementById("re-amount").value = row.amount || "";
    // Split a stored qty string like "5 pack" into number + unit so the owner
    // can see and edit the unit (the reported "no unit in the records edit").
    var qraw = String(row.qty || "").trim();
    var qm = qraw.match(/^\s*(-?\d*\.?\d+)\s*(.*)$/);
    var qn = qm ? parseFloat(qm[1]) : parseFloat(row.qty);
    document.getElementById("re-qty").value = (qn > 0) ? qn : "";
    var reUnitEl = document.getElementById("re-unit");
    if (reUnitEl) reUnitEl.value = qm ? (qm[2] || "").trim() : "";
    document.getElementById("re-who").value = row.vendor || "";
    document.getElementById("re-date").value = row.date || "";
    // Prefill the deposit for a part payment so it can be edited too.
    document.getElementById("re-deposit").value = (row.deposit > 0) ? row.deposit : "";
    reSetPay(rePayVal);
    reBalanceHint();
    document.getElementById("re-err").textContent = "";
    document.getElementById("re-save").disabled = false;
    document.getElementById("recEditOverlay").classList.remove("hidden");
  };
  window.recCloseEdit = function () {
    document.getElementById("recEditOverlay").classList.add("hidden");
    reEditId = null;
  };
  window.reSetPay = function (p) {
    rePayVal = p;
    var chips = document.querySelectorAll("#re-pay .chip");
    chips.forEach(function (c) { c.classList.toggle("active", c.getAttribute("data-p") === p); });
    // Deposit field only for a Part payment.
    document.getElementById("re-deposit-wrap").classList.toggle("hidden", p !== "deposit");
    if (p === "deposit") reBalanceHint();
  };
  window.reBalanceHint = function () {
    var amount = parseFloat(document.getElementById("re-amount").value) || 0;
    var dep = parseFloat(document.getElementById("re-deposit").value) || 0;
    var hint = document.getElementById("re-balance-hint");
    if (rePayVal !== "deposit" || !amount) { hint.textContent = ""; return; }
    if (dep >= amount) { hint.textContent = "Fully paid — will record as paid, not part."; return; }
    hint.textContent = "Balance owed: " + naira(Math.max(0, amount - dep));
  };
  window.recSaveEdit = function () {
    var err = document.getElementById("re-err");
    err.textContent = "";
    if (!reEditId) return;
    var amount = parseFloat(document.getElementById("re-amount").value) || 0;
    if (amount <= 0) { err.textContent = "Enter an amount greater than 0."; return; }
    var who = (document.getElementById("re-who").value || "").trim();
    var isPart = (rePayVal === "deposit");
    var deposit = isPart ? (parseFloat(document.getElementById("re-deposit").value) || 0) : 0;
    if (isPart) {
      if (deposit > amount) { err.textContent = "The deposit can't be more than the total."; return; }
      if (deposit <= 0) { err.textContent = "Enter the deposit paid now (or choose Credit)."; return; }
    }
    if ((rePayVal === "credit" || isPart) && !who) {
      err.textContent = "A credit/part entry needs a name."; return;
    }
    var qraw = parseFloat(document.getElementById("re-qty").value);
    var body = {
      tx_id: reEditId,
      amount: amount,
      description: (document.getElementById("re-desc").value || "").trim(),
      vendor: who,
      date: document.getElementById("re-date").value || "",
      payment_method: rePayVal,
    };
    if (isPart) body.deposit_amount = deposit;
    if (qraw > 0) {
      // Keep the unit with the quantity string ("5 pack") so editing a record
      // no longer strips the unit (save_transaction stores the numeric part;
      // the unit rides along for display, same as the record form).
      var reUnit = (document.getElementById("re-unit").value || "").trim();
      body.quantity = reUnit ? (String(qraw) + " " + reUnit) : String(qraw);
    }
    var btn = document.getElementById("re-save");
    btn.disabled = true;
    apiPost("api/edit-transaction", body)
      .then(function () {
        recCloseEdit();
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        loadRecords();
        loadSummary();
        // Editing a sale/purchase reverses+reapplies stock and COGS, so the
        // cached catalog/inventory is now stale — invalidate it and refresh
        // the catalog view if it is on screen (mirrors saveRecord).
        invLoaded = false;
        var catView = document.getElementById("view-cat");
        if (catView && !catView.classList.contains("hidden")) loadInventory();
      })
      .catch(function (e) {
        btn.disabled = false;
        err.textContent = (e && e.message) || "Could not save the edit.";
      });
  };
  window.recExport = function (fmt) {
    var msg = document.getElementById("rec-msg");
    msg.textContent = "Preparing " + fmt.toUpperCase() + " export...";
    // Read the JSON body even on a non-2xx status, so a structured error (e.g.
    // the PDF tier gate: 403 {error:'pdf_paywalled', message:'...'}) surfaces
    // its friendly message instead of a bare "Error 403". Only a 401 (expired
    // session) keeps the generic reopen hint.
    fetch(BASE + "/api/export?" + recQuery() + "&fmt=" + fmt,
          { headers: { "X-Telegram-Init-Data": initData } })
      .then(function (r) {
        return r.text().then(function (body) {
          var d = {};
          try { d = body ? JSON.parse(body) : {}; } catch (e) { d = {}; }
          if (r.status === 401) {
            msg.textContent = "Session expired — close and reopen the app from the \u2630 menu button.";
            return;
          }
          // Chat delivery failed but the file is on S3 → offer a direct link.
          if (d.download_url) {
            msg.innerHTML = escapeHtml(d.message || "Ready.") +
              ' <a href="' + escapeHtml(d.download_url) + '" target="_blank" rel="noopener">Download</a>';
            return;
          }
          if (d.message) { msg.textContent = d.message; return; }
          if (!r.ok) { msg.textContent = "Export failed (" + r.status + ")."; return; }
          msg.textContent = d.ok ? "Export sent to your chat." : "Nothing to export.";
        });
      })
      .catch(function (e) {
        msg.textContent = (e && e.message) || "Export failed.";
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
      // Read as text first so a NON-JSON error body (e.g. an API Gateway
      // "Missing Authentication Token" 403, or an HTML 5xx) doesn't blow up
      // r.json() with an opaque browser "Load failed". Give a real message.
      return r.text().then(function (t) {
        var j = null;
        try { j = t ? JSON.parse(t) : null; } catch (e) { j = null; }
        if (!r.ok || !(j && j.ok)) {
          if (j && j.error) {
            var e2 = new Error(j.error);
            e2.body = j;               // carry needs_confirm/warning etc. to caller
            e2.status = r.status;
            throw e2;
          }
          if (r.status === 401 || r.status === 403)
            throw new Error("Session expired — close and reopen from the ☰ Menu button.");
          throw new Error("Couldn't reach the server (error " + r.status +
                          "). Please try again in a moment.");
        }
        return j;
      });
    });
  }
  window.openSheet = function (p) {
    editing = p;
    document.getElementById("sh-name").textContent = p.name || "Product";
    document.getElementById("sh-sub").textContent = "Edits save to your catalog";
    document.getElementById("sh-rename").value = p.name || "";
    document.getElementById("sh-stock").value = Number(p.stock || 0);
    document.getElementById("sh-price").value = p.sale_price ? Number(p.sale_price) : "";
    document.getElementById("sh-cost").value = p.cost ? Number(p.cost) : "";
    document.getElementById("sh-unit").value = p.unit || "";
    document.getElementById("sh-reorder").value = p.reorder_level ? Number(p.reorder_level) : "";
    document.getElementById("sh-cat").value = p.category || "";
    document.getElementById("sh-err").textContent = "";
    document.getElementById("sh-save").disabled = false;
    renderUnitsList(p);
    var ce = document.getElementById("sh-conv-err"); if (ce) ce.textContent = "";
    var ci = document.getElementById("sh-conv"); if (ci) ci.value = "";
    // Stage 1 — recipe-driven cost for mfg/hybrid finished goods (Decision A).
    // Hide the manual cost input and show the rolled-up recipe cost read-only.
    // Trading + raw materials keep the manual cost field exactly as before.
    applyCostFieldMode(p);
    // Show only the fields that make sense for this item TYPE (overheads/raw
    // materials don't sell, overheads aren't stocked, etc.).
    applyTypeFields(p);
    document.getElementById("overlay").classList.remove("hidden");
  };
  // Show/hide edit-sheet fields by item type:
  //   overhead      → rate × usage, not stocked, not sold: hide stock, selling
  //                   price, reorder, conversions; keep cost(=rate)/unit/category.
  //   raw_material  → stocked + consumed, not sold: hide selling price; keep
  //                   stock, cost, unit, reorder, conversions.
  //   supply        → like raw material (not sold): hide selling price.
  //   product/""    → everything (sellable).
  function applyTypeFields(p) {
    var t = (p.item_type || "").toLowerCase();
    var isOverhead = (t === "overhead");
    var isInput = (t === "raw_material" || t === "supply");
    function show(id, on) {
      var el = document.getElementById(id);
      if (el) el.classList.toggle("hidden", !on);
    }
    // Selling price: only sellable products.
    show("sh-price-wrap", !isOverhead && !isInput);
    // Stock + reorder + conversions: not meaningful for overhead (a rate).
    show("sh-stock-wrap", !isOverhead);
    show("sh-reorder-wrap", !isOverhead);
    show("sh-conv-wrap", !isOverhead);
    // Cost label reads as a rate for overhead.
    var cl = document.getElementById("sh-cost-label2");
    if (cl) cl.textContent = isOverhead
      ? "Rate per unit of usage (\\u20a6)" : "Cost per unit (\\u20a6)";
    // Delete button wording (materials/overhead aren't "products").
    var del = document.getElementById("sh-delete");
    if (del) del.textContent = "\\ud83d\\uddd1\\ufe0f Delete "
      + (isOverhead ? "overhead" : isInput ? "material" : "product");
  }
  // Decide whether the edit sheet shows a manual cost input or a read-only
  // "Cost (from recipe)" display, based on industry + the product's item_type.
  function applyCostFieldMode(p) {
    var manualWrap = document.getElementById("sh-cost-wrap");
    var recipeWrap = document.getElementById("sh-recipe-cost-wrap");
    if (!manualWrap || !recipeWrap) return;
    var isFinished = (p.item_type === "finished_product");
    var recipeDriven = usesRecipes() && isFinished;
    if (recipeDriven) {
      manualWrap.classList.add("hidden");
      recipeWrap.classList.remove("hidden");
      document.getElementById("sh-recipe-cost").textContent =
        p.cost ? naira(p.cost) : "Not set yet";
      document.getElementById("sh-recipe-hint").textContent = p.has_recipe
        ? "Cost is calculated from this item's recipe. Tap the Set / edit recipe button below to change it."
        : "No recipe yet. Tap the Set / edit recipe button below so its cost is calculated automatically.";
    } else {
      manualWrap.classList.remove("hidden");
      recipeWrap.classList.add("hidden");
    }
  }
  function renderUnitsList(p) {
    var box = document.getElementById("sh-units-list");
    if (!box) return;
    var base = p.base_unit || p.unit || "";
    var defs = p.unit_defs || {};
    var keys = Object.keys(defs);
    if (!base && !keys.length) { box.textContent = "No base unit set yet."; return; }
    var parts = [];
    if (base) parts.push("Base: " + base);
    keys.forEach(function (u) {
      parts.push("1 " + u + " = " + fmtNum(defs[u]) + " " + base);
    });
    box.textContent = parts.join("  •  ");
  }
  function fmtNum(n) {
    n = Number(n || 0);
    return (n === Math.round(n)) ? String(Math.round(n)) : String(n);
  }
  window.addConversion = function () {
    if (!editing) return;
    var input = document.getElementById("sh-conv");
    var err = document.getElementById("sh-conv-err");
    var btn = document.getElementById("sh-conv-add");
    err.textContent = "";
    var rule = (input.value || "").trim();
    if (!rule) { err.textContent = "Type a rule like 1 bag = 20 pieces"; return; }
    btn.disabled = true;
    apiPost("api/product", { action: "set_conversion", key: editing.key, rule: rule })
      .then(function (j) {
        btn.disabled = false;
        if (j && j.product) {
          editing = j.product;
          // patch the inventory row so the list stays in sync
          invData = (invData || []).map(function (x) {
            return x.key === editing.key ? editing : x;
          });
          renderUnitsList(editing);
          input.value = "";
        }
      })
      .catch(function (e) {
        btn.disabled = false;
        err.textContent = e.message || "Could not add that conversion";
      });
  };
  window.closeSheet = function () {
    document.getElementById("overlay").classList.add("hidden");
    editing = null;
  };
  window.bump = function (n) {
    var el = document.getElementById("sh-stock");
    // Preserve a fractional base (e.g. 0.5) when stepping by whole amounts.
    var cur = parseFloat(el.value) || 0;
    var next = Math.max(0, cur + n);
    // Keep whole values whole for a clean field (5, not 5.0).
    el.value = (next === Math.round(next)) ? Math.round(next) : next;
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
          // A leaf reached directly — show EDITABLE stock/cost for this leaf.
          renderLeafEditor(list, varPath, d.stock, d.cost);
        }
        if (d.axis) {
          var ax = document.createElement("div");
          ax.className = "sub2"; ax.style.margin = "4px 0";
          ax.textContent = d.axis;
          list.appendChild(ax);
        }
        kids.forEach(function (c) {
          var row = document.createElement("div");
          row.className = "item tappable";
          var meta = c.is_leaf
            ? (Number(c.stock||0).toLocaleString() + " in stock"
               + (c.cost ? " · cost " + naira(c.cost) : "") + " · tap to edit")
            : (Number(c.stock||0).toLocaleString() + " total →");
          row.innerHTML = '<div><div class="name">' + escapeHtml(c.value) +
            '</div><div class="meta">' + meta + '</div></div>';
          row.onclick = (function (val, isLeaf, st, co) {
            return function () {
              if (isLeaf) {
                // Edit this leaf inline (path + this value).
                renderLeafEditor(list, varPath.concat([val]), st, co, val);
              } else {
                varPath.push(val); drillVarView();
              }
            };
          })(c.value, c.is_leaf, c.stock, c.cost);
          list.appendChild(row);
        });
      })
      .catch(function (e) {
        list.innerHTML = "";
        err.textContent = e.message || "Could not load variants";
      });
  }
  // Editable stock/cost for a specific leaf (path = full value path to the leaf).
  function renderLeafEditor(list, path, stock, cost, leafLabel) {
    var box = document.createElement("div");
    box.className = "card"; box.style.marginTop = "8px";
    var title = leafLabel ? escapeHtml(leafLabel) : (path.length ? escapeHtml(path[path.length-1]) : "This variant");
    box.innerHTML =
      '<div class="k" style="margin-bottom:6px">Edit ' + title + '</div>' +
      '<div class="field"><label>Stock</label>' +
      '<input id="leaf-stock" type="number" inputmode="decimal" min="0" step="any" value="' + Number(stock||0) + '"></div>' +
      '<div class="field"><label>Cost per unit (\u20a6)</label>' +
      '<input id="leaf-cost" type="number" inputmode="decimal" min="0" step="any" value="' + (cost ? Number(cost) : "") + '"></div>' +
      '<div class="sheeterr" id="leaf-err"></div>';
    var btn = document.createElement("button");
    btn.className = "btn save"; btn.style.width = "100%"; btn.textContent = "Save variant";
    btn.onclick = function () { saveLeaf(path, Number(stock||0), Number(cost||0)); };
    box.appendChild(btn);
    list.appendChild(box);
  }
  function saveLeaf(path, oldStock, oldCost) {
    var err = document.getElementById("leaf-err");
    var st = Math.max(0, parseFloat(document.getElementById("leaf-stock").value) || 0);  // stock may be fractional
    var coRaw = document.getElementById("leaf-cost").value;
    var co = coRaw === "" ? null : Math.max(0, parseFloat(coRaw) || 0);   // cost money, kobo
    var ops = [];
    if (st !== oldStock)
      ops.push({ action: "set_leaf_stock", key: varProd.key, path: path, value: st });
    if (co !== null && co !== oldCost)
      ops.push({ action: "set_leaf_cost", key: varProd.key, path: path, value: co });
    if (!ops.length) { drillVarView(); return; }
    if (err) err.textContent = "";
    ops.reduce(function (chain, op) {
      return chain.then(function () { return apiPost("api/product", op); });
    }, Promise.resolve())
    .then(function () {
      // Refresh the product row (stock rolled up) + re-render the drill.
      invLoaded = false; loadInventory();
      drillVarView();
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    })
    .catch(function (e) {
      if (err) err.textContent = e.message || "Save failed";
    });
  }
  window.saveSheet = function () {
    if (!editing) return;
    var key = editing.key;
    var newStock = Math.max(0, parseFloat(document.getElementById("sh-stock").value) || 0);
    var priceRaw = document.getElementById("sh-price").value;
    var costRaw = document.getElementById("sh-cost").value;
    var newPrice = priceRaw === "" ? null : Math.max(0, parseFloat(priceRaw) || 0);   // money, kobo
    var newCost = costRaw === "" ? null : Math.max(0, parseFloat(costRaw) || 0);       // money, kobo

    var newName = (document.getElementById("sh-rename").value || "").trim();
    var newUnit = (document.getElementById("sh-unit").value || "").trim();
    var reorderRaw = document.getElementById("sh-reorder").value;
    var newReorder = reorderRaw === "" ? null : Math.max(0, parseInt(reorderRaw, 10) || 0);
    var newCat = (document.getElementById("sh-cat").value || "").trim();

    // Which fields are relevant for this item type (mirrors applyTypeFields):
    var t = (editing.item_type || "").toLowerCase();
    var isOverhead = (t === "overhead");
    var isInput = (t === "raw_material" || t === "supply");
    var canSell = !isOverhead && !isInput;   // only sellable products have a price
    var canStock = !isOverhead;              // overhead is a rate, not stocked

    // Only send the fields that actually changed AND are relevant to the type.
    var ops = [];
    if (newName && newName !== (editing.name || ""))
      ops.push({ action: "rename", key: key, name: newName });
    if (canStock && newStock !== Number(editing.stock || 0))
      ops.push({ action: "set_stock", key: key, value: newStock });
    if (canSell && newPrice !== null && newPrice !== Number(editing.sale_price || 0))
      ops.push({ action: "set_price", key: key, value: newPrice });
    // Recipe-driven finished goods (mfg/hybrid) never send a manual cost — the
    // cost field is hidden for them and cost comes from the recipe (Decision A).
    var costLocked = usesRecipes() && (editing.item_type === "finished_product");
    if (!costLocked && newCost !== null && newCost !== Number(editing.cost || 0))
      ops.push({ action: "set_cost", key: key, value: newCost });
    if (newUnit !== (editing.unit || ""))
      ops.push({ action: "set_unit", key: key, unit: newUnit });
    if (canStock && newReorder !== null && newReorder !== Number(editing.reorder_level || 0))
      ops.push({ action: "set_reorder", key: key, value: newReorder });
    if (newCat !== (editing.category || ""))
      ops.push({ action: "set_category", key: key, category: newCat });

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

  window.deleteProduct = function () {
    if (!editing) return;
    var key = editing.key;
    var err = document.getElementById("sh-err");
    var btn = document.getElementById("sh-delete");
    // Two-tap confirm — no confirm() dialog (not supported in Telegram WebView).
    if (btn.getAttribute("data-confirm") !== "1") {
      btn.setAttribute("data-confirm","1");
      btn.textContent = "Tap again to confirm delete";
      setTimeout(function(){btn.removeAttribute("data-confirm");btn.textContent="\\ud83d\\uddd1\\ufe0f Delete product";},4000);
      return;
    }
    btn.removeAttribute("data-confirm");
    btn.textContent = "\\ud83d\\uddd1\\ufe0f Delete product";
    if (err) err.textContent = "";
    apiPost("api/product", { action: "delete", key: key, confirm: true })
      .then(function () {
        invData = (invData || []).filter(function (p) { return p.key !== key; });
        closeSheet();
        renderCatalog();
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      })
      .catch(function (e) {
        document.getElementById("sh-err").textContent = e.message || "Delete failed";
      });
  };

  // ── Add product ──
  // Chosen item type for a NEW product (mfg/hybrid only). Default finished.
  var addItemType = "finished_product";
  // When true, the Add sheet is in "Add raw material" mode: item type is forced
  // to raw_material and the cost/unit/opening-stock fields are shown. A raw
  // material can exist on its own, not tied to any product's recipe yet.
  var addRawMode = false;
  window.addSetType = function (t) {
    addItemType = t;
    var chips = document.querySelectorAll("#add-type .chip");
    for (var i = 0; i < chips.length; i++) {
      chips[i].classList.toggle("active", chips[i].getAttribute("data-it") === t);
    }
    // Raw material / supply carry a real buy-cost; show the cost/unit/stock
    // extras for those so you can register what you already have. A finished
    // product's cost comes from its recipe, so hide them.
    var extra = document.getElementById("add-raw-extra");
    if (extra) extra.classList.toggle("hidden", t === "finished_product");
    document.getElementById("add-type-hint").textContent = (t === "finished_product")
      ? "A finished product's cost is calculated from its recipe. After adding, tap it to Set / edit recipe."
      : "A raw material or supply has a normal buy-cost. You can set what it cost you, its stock unit, and what you already have on hand below.";
  };
  window.openAddProduct = function () {
    addRawMode = false;
    document.getElementById("add-name").value = "";
    document.getElementById("add-cat").value = "";
    document.getElementById("add-err").textContent = "";
    document.getElementById("add-save").disabled = false;
    document.querySelector("#addOverlay h2").textContent = "Add a product";
    var cost = document.getElementById("add-cost"); if (cost) cost.value = "";
    var unit = document.getElementById("add-unit"); if (unit) unit.value = "";
    var stk = document.getElementById("add-stock"); if (stk) stk.value = "";
    var extra = document.getElementById("add-raw-extra"); if (extra) extra.classList.add("hidden");
    // Item-type chooser only for mfg/hybrid; default to finished product.
    var typeWrap = document.getElementById("add-type-wrap");
    if (usesRecipes()) { typeWrap.classList.remove("hidden"); addSetType("finished_product"); }
    else { typeWrap.classList.add("hidden"); addItemType = ""; document.getElementById("add-raw-extra").classList.add("hidden"); }
    document.getElementById("addOverlay").classList.remove("hidden");
  };
  // Direct "Add raw material" entry — a raw material not yet tied to any product.
  window.openAddRawMaterial = function () {
    addRawMode = true;
    document.getElementById("add-name").value = "";
    document.getElementById("add-cat").value = "";
    document.getElementById("add-err").textContent = "";
    document.getElementById("add-save").disabled = false;
    document.querySelector("#addOverlay h2").textContent = "\\ud83e\\uddf1 Add raw material";
    var cost = document.getElementById("add-cost"); if (cost) cost.value = "";
    var unit = document.getElementById("add-unit"); if (unit) unit.value = "";
    var stk = document.getElementById("add-stock"); if (stk) stk.value = "";
    var conv = document.getElementById("add-conv"); if (conv) conv.value = "";
    // Hide the type chooser (kind is chosen inside the extras) and show the extras.
    document.getElementById("add-type-wrap").classList.add("hidden");
    addItemType = "raw_material";
    addRawSetKind("raw_material");     // default; also relabels cost + toggles stock
    addRawFillUnits();                 // suggest existing units in the datalist
    document.getElementById("add-raw-extra").classList.remove("hidden");
    document.getElementById("addOverlay").classList.remove("hidden");
    document.getElementById("add-name").focus();
  };
  // Raw-material KIND (#6): raw_material (stock-tracked) vs overhead (rate×usage).
  var addRawKind = "raw_material";
  window.addRawSetKind = function (k) {
    addRawKind = k;
    var chips = document.querySelectorAll("#add-raw-kind .chip");
    for (var i = 0; i < chips.length; i++) {
      chips[i].classList.toggle("active", chips[i].getAttribute("data-rk") === k);
    }
    var isOh = (k === "overhead");
    // Overhead cost is a RATE per unit of usage, and it isn't stocked.
    var lbl = document.getElementById("add-cost-label");
    if (lbl) lbl.textContent = isOh ? "Rate per unit of usage (optional)" : "Cost per unit (optional)";
    var sw = document.getElementById("add-stock-wrap");
    if (sw) sw.classList.toggle("hidden", isOh);
    var hint = document.getElementById("add-raw-kind-hint");
    if (hint) hint.textContent = isOh
      ? "Overhead: a rate \u00d7 usage with no stock (e.g. electricity in kW, labour in minutes)."
      : "Raw material: stock-tracked, deducted on production (e.g. nylon in kg).";
  };
  // Suggest already-registered units so the owner reuses "kg"/"bag" consistently.
  function addRawFillUnits() {
    var dl = document.getElementById("add-unit-list");
    if (!dl) return;
    var seen = {}; dl.innerHTML = "";
    (invData || []).forEach(function (p) {
      var u = (p.unit || p.base_unit || "").trim();
      if (u && !seen[u.toLowerCase()]) {
        seen[u.toLowerCase()] = 1;
        var o = document.createElement("option"); o.value = u; dl.appendChild(o);
      }
    });
  }
  window.closeAddProduct = function () {
    document.getElementById("addOverlay").classList.add("hidden");
  };
  window.saveAddProduct = function () {
    var name = (document.getElementById("add-name").value || "").trim();
    var cat = (document.getElementById("add-cat").value || "").trim();
    var err = document.getElementById("add-err");
    var label = addRawMode ? "raw material" : "product";
    if (!name) { err.textContent = "Enter a " + label + " name."; return; }
    var btn = document.getElementById("add-save");
    btn.disabled = true; err.textContent = "";
    var body = { action: "add", name: name, category: cat };
    var convRule = "";   // a taught conversion to apply after the add, if any
    // Raw-material mode forces the type (material vs overhead) + carries the
    // optional cost/unit/stock. Otherwise only mfg/hybrid tag an item type;
    // trading stays a plain product.
    if (addRawMode) {
      // #6: material vs overhead. An overhead is a rate×usage with no stock.
      body.item_type = (addRawKind === "overhead") ? "overhead" : "raw_material";
      var cv = (document.getElementById("add-cost").value || "").trim();
      var uv = (document.getElementById("add-unit").value || "").trim();
      var sv = (document.getElementById("add-stock").value || "").trim();
      if (cv !== "") body.cost = parseFloat(cv);
      if (uv !== "") body.unit = uv;
      if (sv !== "" && addRawKind !== "overhead") body.stock = parseFloat(sv);
      convRule = (document.getElementById("add-conv").value || "").trim();
    } else if (usesRecipes() && addItemType) {
      body.item_type = addItemType;
      // A raw material / supply added via the type chooser can also carry its
      // cost/unit/stock (the extras are shown for those types).
      if (addItemType !== "finished_product") {
        var cv2 = (document.getElementById("add-cost").value || "").trim();
        var uv2 = (document.getElementById("add-unit").value || "").trim();
        var sv2 = (document.getElementById("add-stock").value || "").trim();
        if (cv2 !== "") body.cost = parseFloat(cv2);
        if (uv2 !== "") body.unit = uv2;
        if (sv2 !== "") body.stock = parseFloat(sv2);
      }
    }
    apiPost("api/product", body)
      .then(function (j) {
        if (j.product) (invData = invData || []).push(j.product);
        // If the owner taught a conversion (#5), apply it to the new item via the
        // existing set_conversion action (engine validates + rebuilds unit_defs).
        if (convRule && j.product && j.product.key) {
          return apiPost("api/product", {
            action: "set_conversion", key: j.product.key, rule: convRule
          }).catch(function () { /* non-fatal: the item is already created */ });
        }
      })
      .then(function () {
        closeAddProduct();
        renderCatalog();
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      })
      .catch(function (e) {
        btn.disabled = false;
        err.textContent = e.message || "Could not add product";
      });
  };

  // ── Recipe / BOM editor (Stage 2) ──
  // Cost is engine-owned: we only display unit_cost from the server and POST
  // add/remove material ops. No cost math in JS.
  var recipeKey = null;   // product_key whose recipe is open
  window.openRecipe = function () {
    if (!editing) return;
    recipeKey = editing.key;
    document.getElementById("rc-title").textContent = "Recipe \u00b7 " + (editing.name || "");
    document.getElementById("rc-err").textContent = "";
    document.getElementById("rc-qty").value = "";
    document.getElementById("rc-cost").value = "";
    document.getElementById("rc-unit").value = "";
    document.getElementById("rc-newname").value = "";
    rcNewMatType = "material";
    document.getElementById("rc-list").innerHTML = '<div class="muted">Loading\u2026</div>';
    // Close the edit sheet FIRST — both use the same .overlay/z-index, so if the
    // edit sheet stays open it renders ON TOP and the recipe screen looks
    // unresponsive (hidden behind it). Hide it, show the recipe overlay.
    document.getElementById("overlay").classList.add("hidden");
    document.getElementById("recipeOverlay").classList.remove("hidden");
    loadRecipe();
  };
  window.closeRecipe = function () {
    document.getElementById("recipeOverlay").classList.add("hidden");
    recipeKey = null;
    // Refresh the catalog (recipe cost may have changed). We do NOT reopen the
    // edit sheet — returning to the catalog grid is the cleaner flow.
    editing = null;
    invLoaded = false; loadInventory();
  };
  function loadRecipe() {
    api("api/recipe?key=" + encodeURIComponent(recipeKey))
      .then(renderRecipe)
      .catch(function (e) {
        document.getElementById("rc-list").innerHTML =
          '<div class="muted">' + escapeHtml(e.message || "Could not load recipe") + '</div>';
      });
  }
  function renderRecipe(d) {
    document.getElementById("rc-unitcost").textContent =
      d.unit_cost ? naira(d.unit_cost) : "Not costed yet";
    // Keep the edit sheet's recipe-cost line in sync if it's still open.
    var shCost = document.getElementById("sh-recipe-cost");
    if (shCost && editing && editing.key === d.product_key) {
      shCost.textContent = d.unit_cost ? naira(d.unit_cost) : "Not set yet";
      editing.cost = d.unit_cost || 0;
      editing.has_recipe = (d.materials || []).length > 0;
    }
    // Material lines with a remove button.
    var list = document.getElementById("rc-list");
    var mats = d.materials || [];
    list.innerHTML = "";
    if (!mats.length) {
      list.innerHTML = '<div class="muted">No materials yet. Add the first one below.</div>';
    } else {
      mats.forEach(function (m, i) {
        var isOh = (m.type === "overhead");
        var per = isOh ? (m.rate || 0) : (m.cost_per_unit || 0);
        var row = document.createElement("div");
        row.className = "item";
        row.innerHTML =
          '<div><div class="name">' + (isOh ? "⚡ " : "🧱 ") + escapeHtml(m.material || "") +
          '</div><div class="meta">' + escapeHtml(String(m.quantity || 0)) + " " +
          escapeHtml(m.unit || "") + (per ? (" @ " + naira(per)) : "") + '</div></div>';
        var rm = document.createElement("button");
        rm.className = "btn cancel";
        rm.style.cssText = "padding:6px 10px;color:var(--neg)";
        rm.textContent = "Remove";
        rm.onclick = function () { recipeRemoveMaterial(i); };
        row.appendChild(rm);
        list.appendChild(row);
      });
    }
    // Populate the material picker from catalog raw materials / supplies, and
    // ALWAYS offer "➕ New material…" so a fresh account can build a recipe
    // without leaving the app (the server creates the material inline).
    var sel = document.getElementById("rc-mat");
    var avail = d.available_materials || [];
    sel.innerHTML = "";
    document.getElementById("rc-add").disabled = false;
    // Build one option, formatting the money as a per-unit figure and marking
    // OVERHEADS as rates so they aren't mistaken for a raw-material's unit cost.
    // An overhead's number is a RATE per unit of usage (e.g. \u20a61.5/sec of
    // labour), whereas a material's number is the cost of ONE unit you consume
    // (e.g. \u20a670/piece of bottle). Same "\u20a6X" with no context was
    // misleading, so overheads read "... \u20a61.5/sec (rate)".
    function mkOpt(m) {
      var o = document.createElement("option");
      o.value = m.key;
      o.setAttribute("data-unit", m.unit || "");
      o.setAttribute("data-type", m.item_type || "material");
      var isOverhead = (m.item_type === "overhead");
      var per = m.unit ? ("/" + m.unit) : "";
      var money = m.cost ? (naira(m.cost) + per) : "";
      var label = m.name;
      if (isOverhead) {
        // \u26a1 lightning marks an overhead at a glance; "(rate)" spells it out.
        label = "\\u26a1 " + m.name + (money ? (" \u2014 " + money + " (rate)") : " (rate)");
      } else {
        label = m.name + (money ? (" \u2014 " + money) : (m.unit ? (" (" + m.unit + ")") : ""));
      }
      o.textContent = label;
      return o;
    }
    // Group into Raw materials vs Overheads so rates and costs aren't
    // interleaved. Anything not an overhead is treated as a stock material here.
    var mats = avail.filter(function (m) { return m.item_type !== "overhead"; });
    var ovh = avail.filter(function (m) { return m.item_type === "overhead"; });
    if (mats.length) {
      var g1 = document.createElement("optgroup");
      g1.label = "Raw materials";
      mats.forEach(function (m) { g1.appendChild(mkOpt(m)); });
      sel.appendChild(g1);
    }
    if (ovh.length) {
      var g2 = document.createElement("optgroup");
      g2.label = "Overheads (rate \u00d7 usage)";
      ovh.forEach(function (m) { g2.appendChild(mkOpt(m)); });
      sel.appendChild(g2);
    }
    var nn = document.createElement("option");
    nn.value = "__new__";
    nn.textContent = "\u2795 New material\u2026";
    sel.appendChild(nn);
    // Default to "New material…" when the catalog has none yet.
    if (!avail.length) sel.value = "__new__";
    rcMatChanged();
  }
  // Material type chosen for a NEW recipe input (raw material vs overhead).
  var rcNewMatType = "material";
  window.rcSetMatType = function (mt) {
    rcNewMatType = mt;
    var chips = document.querySelectorAll("#rc-type .chip");
    for (var i = 0; i < chips.length; i++) {
      chips[i].classList.toggle("active", chips[i].getAttribute("data-mt") === mt);
    }
    document.getElementById("rc-type-hint").textContent = (mt === "overhead")
      ? "Overhead: a rate × usage with no stock (e.g. electricity in kW, labour in minutes)."
      : "Raw material: stock-tracked and deducted on production (e.g. nylon in kg).";
    // Cost label follows the type: overhead is a rate per unit of usage.
    document.getElementById("rc-cost-label").textContent =
      (mt === "overhead") ? "Rate per unit of usage (optional)"
                          : "Cost of ONE unit of this material (optional)";
    var ch = document.getElementById("rc-cost-hint");
    if (ch) ch.textContent = (mt === "overhead")
      ? "\\ud83d\\udca1 Enter the rate for ONE unit (e.g. \\u20a60.06 per kW of electricity). The cost added is quantity \\u00d7 this rate."
      : "\\ud83d\\udca1 Enter the price of ONE unit (e.g. \\u20a61,200 per kg of nylon), NOT the batch total. Blank = use the material's saved cost. Product cost = quantity \\u00d7 this.";
  };
  // Fill the new-material name datalist from the catalog (invData) so typing a
  // name suggests already-registered items — grouped by category label so the
  // owner reuses "Nylon" instead of creating "nylon"/"Wrapping Nylon" again.
  // Options carry the category in their label; the value stays the bare name so
  // picking one just fills the input.
  var IT_LABEL = { raw_material: "Raw material", supply: "Supply",
                   overhead: "Overhead", product: "Product",
                   finished_product: "Product", service: "Service" };
  function recFillMaterialNames() {
    var dl = document.getElementById("rc-newname-list");
    if (!dl) return;
    dl.innerHTML = "";
    var items = (invData || []).slice();
    // Materials/overheads/supplies first (most likely recipe inputs), then rest.
    var order = { raw_material: 0, supply: 1, overhead: 2 };
    items.sort(function (a, b) {
      var oa = (order[a.item_type] !== undefined) ? order[a.item_type] : 9;
      var ob = (order[b.item_type] !== undefined) ? order[b.item_type] : 9;
      if (oa !== ob) return oa - ob;
      return String(a.name || "").localeCompare(String(b.name || ""));
    });
    var seen = {};
    items.forEach(function (p) {
      var nm = String(p.name || "").trim();
      if (!nm) return;
      var lk = nm.toLowerCase();
      if (seen[lk]) return;
      seen[lk] = 1;
      var o = document.createElement("option");
      o.value = nm;
      var lbl = IT_LABEL[p.item_type] || "Item";
      o.label = lbl + (p.unit ? (" \u00b7 " + p.unit) : "");
      dl.appendChild(o);
    });
    var hint = document.getElementById("rc-newname-hint");
    if (hint) {
      hint.className = "sub2";
      hint.textContent = Object.keys(seen).length
        ? "Start typing to pick an existing item and avoid duplicates."
        : "";
    }
    // If the catalog hasn't loaded yet, pull it then refill once (guard the
    // re-entry so we don't loop).
    if (!invLoaded && !recFillMaterialNames._loading) {
      recFillMaterialNames._loading = true;
      loadInventory().then(function () {
        recFillMaterialNames._loading = false;
        recFillMaterialNames();
      });
    }
  }
  // React to picker change: show the new-material fields (name + type + unit)
  // only for "New material…". For an existing catalog material, its unit/type
  // come from the catalog, so pre-fill unit and hide the editable extras.
  window.rcMatChanged = function () {
    var sel = document.getElementById("rc-mat");
    var isNew = sel.value === "__new__";
    var opt = sel.options[sel.selectedIndex];
    document.getElementById("rc-newname-wrap").classList.toggle("hidden", !isNew);
    document.getElementById("rc-type-wrap").classList.toggle("hidden", !isNew);
    document.getElementById("rc-err").textContent = "";
    var unitInput = document.getElementById("rc-unit");
    var costInput = document.getElementById("rc-cost");
    if (isNew) {
      rcSetMatType("material");
      unitInput.value = "";
      unitInput.readOnly = false;
      costInput.placeholder = "enter buy-cost";
      recFillMaterialNames();   // suggest existing catalog names to avoid dupes
    } else {
      // Existing material: unit + type are fixed by the catalog row.
      var u = opt ? (opt.getAttribute("data-unit") || "") : "";
      unitInput.value = u;
      unitInput.readOnly = true;   // can't change a catalog material's unit here
      var isOh = opt && opt.getAttribute("data-type") === "overhead";
      document.getElementById("rc-cost-label").textContent =
        isOh ? "Rate per unit of usage (optional)"
             : "Cost of ONE unit of this material (optional)";
      costInput.placeholder = "uses catalog cost";
    }
  };
  window.recipeAddMaterial = function () {
    var sel = document.getElementById("rc-mat");
    var matKey = sel.value;
    var err = document.getElementById("rc-err");
    if (!matKey) { err.textContent = "Pick a material."; return; }
    var isNew = matKey === "__new__";
    var newName = (document.getElementById("rc-newname").value || "").trim();
    if (isNew && !newName) { err.textContent = "Enter the new material's name."; return; }
    var qtyRaw = document.getElementById("rc-qty").value;
    var qty = qtyRaw === "" ? null : parseFloat(qtyRaw);
    if (qty === null || !(qty > 0)) { err.textContent = "Enter a quantity per unit."; return; }
    var unitVal = (document.getElementById("rc-unit").value || "").trim();
    if (isNew && !unitVal) { err.textContent = "Enter a unit (e.g. kg, litre, kW, sec)."; return; }
    var costRaw = document.getElementById("rc-cost").value;
    var opt = sel.options[sel.selectedIndex];
    // Type: for a new material the user picks it (raw material / overhead); for
    // an existing one it's fixed by the catalog row.
    var matType = isNew
      ? rcNewMatType
      : ((opt && opt.getAttribute("data-type") === "overhead") ? "overhead" : "material");
    var body = {
      action: "add_material", key: recipeKey,
      material_key: isNew ? "" : matKey,
      quantity: qty,
      unit: unitVal,
      mat_type: matType
    };
    if (isNew) body.new_material_name = newName;
    // Cost/rate may be fractional (e.g. ₦0.05/gram, ₦0.5/sec) — parseFloat, not
    // parseInt, so small per-unit rates aren't truncated to 0. Engine takes float.
    if (costRaw !== "") body.cost_per_unit = Math.max(0, parseFloat(costRaw) || 0);
    var btn = document.getElementById("rc-add");
    btn.disabled = true; err.textContent = "";
    apiPost("api/recipe", body)
      .then(function (d) {
        document.getElementById("rc-qty").value = "";
        document.getElementById("rc-cost").value = "";
        document.getElementById("rc-newname").value = "";
        document.getElementById("rc-unit").value = "";
        renderRecipe(d);   // re-lists materials (the new one now appears) + resets picker
        btn.disabled = false;
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      })
      .catch(function (e) {
        btn.disabled = false;
        err.textContent = e.message || "Could not add material";
      });
  };
  function recipeRemoveMaterial(index) {
    var err = document.getElementById("rc-err");
    err.textContent = "";
    apiPost("api/recipe", { action: "remove_material", key: recipeKey, index: index })
      .then(renderRecipe)
      .catch(function (e) { err.textContent = e.message || "Could not remove material"; });
  }

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
    // Render only AFTER inventory has actually arrived (no fixed-delay race
    // that left the dropdown empty on the first tap).
    if (!invData) {
      document.getElementById("pick-list").innerHTML =
        '<div class="muted">Loading products\u2026</div>';
      loadInventory().then(renderPickerProducts);
    } else renderPickerProducts();
  };
  window.closePicker = function () {
    document.getElementById("pickOverlay").classList.add("hidden");
  };
  window.pickFilter = function () { if (pickMode === "products") renderPickerProducts(); };

  function renderPickerProducts() {
    var q = (document.getElementById("pick-search").value || "").toLowerCase().trim();
    var list = document.getElementById("pick-list");
    // Filter by transaction type so a SALE never lists raw materials/overheads
    // and a PURCHASE never lists finished goods you make (not buy). item_type:
    // "" (trading), "product"/"finished_product" (sellable), "raw_material",
    // "supply", "overhead", "service".
    function typeAllowed(p) {
      var it = (p.item_type || "").toLowerCase();
      if (recTypeVal === "sale") {
        // Sellable outputs only: finished/product/service or untyped (trading).
        return it === "" || it === "product" || it === "finished_product"
            || it === "service";
      }
      if (recTypeVal === "purchase") {
        // Things you BUY: raw materials, supplies, or untyped trading stock.
        // Not finished goods (you produce those) and not overhead (a rate, not
        // a stocked purchase — recorded as an expense).
        return it === "" || it === "product" || it === "raw_material"
            || it === "supply";
      }
      if (recTypeVal === "produce") {
        // You can only PRODUCE a finished good that has a recipe.
        return (it === "finished_product" || it === "product") && p.has_recipe;
      }
      return true;  // expense picker is hidden, but never over-filter.
    }
    var rows = (invData || []).filter(function (p) {
      if (!typeAllowed(p)) return false;
      return !q || (p.name || "").toLowerCase().indexOf(q) >= 0;
    });
    list.innerHTML = "";
    if (!rows.length) {
      var hint = (recTypeVal === "purchase")
        ? "No raw materials or stock to buy. Add one in Catalog first."
        : (recTypeVal === "produce")
        ? "No products with a recipe. Add a recipe to a finished product first."
        : "No sellable products. Add one in Catalog first.";
      list.innerHTML = '<div class="muted">' + hint + '</div>';
      return;
    }
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
      // Simple product — done. Keep the unit info so the form can offer a
      // sale/purchase UNIT selector (bag vs piece) instead of forcing a raw
      // base-unit number.
      pick = { key: p.key, name: p.name, variant: null,
               base_unit: p.base_unit || p.unit || "", unit_defs: p.unit_defs || {},
               stock: Number(p.stock || 0), sale_price: Number(p.sale_price || 0),
               cost: Number(p.cost || 0), unit: p.unit || "" };
      applyPick();
      closePicker();
      return;
    }
    // Variant product — drill the tree.
    pick = { key: p.key, name: p.name, variant: null,
             base_unit: p.base_unit || p.unit || "", unit_defs: p.unit_defs || {},
             stock: Number(p.stock || 0), sale_price: Number(p.sale_price || 0),
             cost: Number(p.cost || 0), unit: p.unit || "" };
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
    recPopulateUnits();
    // Show what's in stock (+ selling price on a sale) for the picked product,
    // so the owner sees availability and their set price without leaving the
    // form. Hidden for produce/expense and when there's nothing to show.
    var info = document.getElementById("rec-prod-info");
    if (info) {
      var parts = [];
      var u = pick.unit || pick.base_unit || "";
      if (recTypeVal === "sale" || recTypeVal === "purchase") {
        parts.push("📦 " + Number(pick.stock || 0).toLocaleString() +
                   (u ? " " + u : "") + " in stock");
        if (recTypeVal === "sale" && pick.sale_price > 0)
          parts.push("🏷️ price " + naira(pick.sale_price));
        if (recTypeVal === "purchase" && pick.cost > 0)
          parts.push("cost " + naira(pick.cost));
      } else if (recTypeVal === "produce") {
        // Sanity hint: show the recipe's cost per ONE base unit so an inflated
        // per-unit cost (e.g. a per-pack recipe left on a per-piece base) is
        // obvious BEFORE producing. If a custom unit exists (pack), also show
        // what a whole pack works out to, so "per piece or per pack?" is clear.
        if (pick.cost > 0) parts.push("≈ " + naira(pick.cost) + " to make 1 " + (pick.base_unit || u || "unit"));
        var defs = pick.unit_defs || {};
        var packKeys = Object.keys(defs);
        if (pick.cost > 0 && packKeys.length) {
          var uk = packKeys[0];
          var f = Number(defs[uk]) || 0;
          if (f > 1) parts.push("(1 " + uk + " = " + f + " " + (pick.base_unit || "") +
                                " → " + naira(pick.cost * f) + "/" + uk + ")");
        }
      }
      if (parts.length) { info.textContent = parts.join("  \u00b7  "); info.classList.remove("hidden"); }
      else { info.textContent = ""; info.classList.add("hidden"); }
    }
  }

  // Populate the qty UNIT selector from the picked product's base_unit +
  // unit_defs. Base unit is the stored/stock unit (factor 1); each custom unit's
  // factor (base-per-unit) lets the owner enter e.g. "10 bags" and we convert to
  // base for stock + COGS. Hidden when the product has no custom units (nothing
  // to choose) so simple products behave exactly as before.
  function recPopulateUnits() {
    var sel = document.getElementById("rec-unit-sel");
    var hint = document.getElementById("rec-qty-hint");
    var freeUnit = document.getElementById("rec-unit-free");
    if (!sel) return;
    sel.innerHTML = "";
    if (freeUnit) { freeUnit.classList.add("hidden"); }
    var base = (pick.base_unit || "").trim();
    var defs = pick.unit_defs || {};
    var custom = Object.keys(defs);
    var isProduce = (recTypeVal === "produce");
    // IMPORTANT: only ever offer units the engine can CONVERT — the base unit and
    // its TAUGHT conversions (unit_defs). We deliberately do NOT allow a free-text
    // "new unit" here: the bot doesn't know a new unit's relationship to the base,
    // so producing/selling in it would be rejected server-side anyway (produce_web
    // returns "cannot convert X to <base> — set up that conversion first"). To use
    // a new unit, teach it once in the product's "Units & conversions"; it then
    // shows up here automatically.
    if (!base) {
      // Truly unit-less product: nothing safe to choose — plain numeric qty.
      sel.classList.add("hidden");
      if (hint && isProduce) hint.textContent =
        "\\uD83D\\uDCA1 Quantity produced. Set a base unit on the product (Catalog) to record in bags/pieces/etc.";
      return;
    }
    if (!custom.length && !isProduce) {
      // Sale/purchase, no alternative units → plain numeric qty in the product's
      // own unit; still NAME that unit in the hint.
      sel.classList.add("hidden");
      if (hint) {
        hint.textContent = (recTypeVal === "purchase")
          ? "\\uD83D\\uDCA1 Quantity in " + base + " (the unit this item is stocked \\u0026 used in)."
          : "\\uD83D\\uDCA1 Quantity in " + base + ". Stock drops by this amount.";
      }
      return;
    }
    // Base first (factor 1), then each taught custom unit. No free-text option.
    var opts = [[base, 1]];
    custom.forEach(function (u) {
      var f = Number(defs[u]) || 0;
      if (f > 0) opts.push([u, f]);
    });
    opts.forEach(function (o) {
      var op = document.createElement("option");
      op.value = String(o[1]);           // factor-to-base
      op.setAttribute("data-unit", o[0]);
      op.textContent = o[0];
      sel.appendChild(op);
    });
    sel.value = "1";                      // default to base unit
    sel.classList.remove("hidden");
    if (hint) {
      var teach = (custom.length)
        ? " Need another unit? Teach it in the product's Units \\u0026 conversions."
        : " To record in bags/cartons/etc., teach that unit in the product's Units \\u0026 conversions.";
      hint.textContent = (isProduce
        ? "\\uD83D\\uDCA1 Unit you produced in. Stock is kept in " + base + "."
        : "\\uD83D\\uDCA1 Choose the unit you're recording in. Stock \\u0026 cost are kept in " + base + ".") + teach;
    }
  }
  // Factor to multiply the typed qty by to get BASE units (1 when no selector).
  function recUnitFactor() {
    var sel = document.getElementById("rec-unit-sel");
    if (!sel || sel.classList.contains("hidden")) return 1;
    var f = parseFloat(sel.value);
    return (f > 0) ? f : 1;
  }
  // Name of the unit currently chosen in the qty selector ("" when none shown).
  // Only ever a base/taught unit the engine can convert (no free-text).
  function recUnitName() {
    var sel = document.getElementById("rec-unit-sel");
    if (!sel || sel.classList.contains("hidden")) return "";
    var op = sel.options[sel.selectedIndex];
    return op ? (op.getAttribute("data-unit") || "") : "";
  }

  function uuid() {
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0, v = c === "x" ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }
  function recSyncLabels() {
    var t = recTypeVal;
    // Industry wording (the param name 't' shadows the term helper, so read the
    // TERMS set directly). Trading's terms equal the original strings → no-op.
    var _ts = TERMS[APP.industry] || TERMS.trading;
    document.getElementById("rec-desc-label").textContent =
      t === "sale" ? _ts.sell_q : (t === "purchase" ? _ts.buy_q : "What was it for?");
    // Keep the picker placeholder industry-worded while nothing is picked yet.
    var _pt = document.getElementById("rec-prod-text");
    if (_pt && !pick.name) _pt.textContent = _ts.pick_hint;
    document.getElementById("rec-amount-label").textContent =
      t === "sale" ? "Amount received (\u20a6)" : (t === "purchase" ? "Amount paid (\u20a6)" : "Amount (\u20a6)");
    // Sale/purchase/produce pick from the catalog; expense is free text.
    var isExpense = (t === "expense");
    document.getElementById("rec-prod-btn").classList.toggle("hidden", isExpense);
    var descInput = document.getElementById("rec-desc");
    descInput.classList.toggle("hidden", !isExpense);
    if (isExpense) descInput.placeholder = "e.g. Fuel, Rent";
    // Quantity is useful for ALL types now — an expense like "50 litres fuel"
    // wants a quantity + a free-text unit (expenses have no catalog product, so
    // no unit selector; the owner just types the unit). Sale/purchase keep the
    // catalog-driven unit selector.
    document.getElementById("rec-qty-wrap").style.display = "";
    var qtyLabel = document.getElementById("rec-qty-label");
    if (qtyLabel) qtyLabel.textContent = isExpense ? "Quantity (optional)" : "Quantity";
    var freeUnit = document.getElementById("rec-unit-free");
    var unitSel = document.getElementById("rec-unit-sel");
    if (isExpense) {
      // Free-text unit for expenses; hide the catalog unit selector.
      if (unitSel) unitSel.classList.add("hidden");
      if (freeUnit) freeUnit.classList.remove("hidden");
      var qh = document.getElementById("rec-qty-hint");
      if (qh) qh.textContent = "\\uD83D\\uDCA1 Optional \\u2014 e.g. 50 litres of fuel. Helps you analyse spend later.";
      // Clear the sale default of "1" so expense qty is genuinely optional.
      var rq = document.getElementById("rec-qty");
      if (rq && rq.value === "1") rq.value = "";
    } else {
      if (freeUnit) { freeUnit.classList.add("hidden"); freeUnit.value = ""; }
      // unit selector visibility is managed by recPopulateUnits on pick.
    }
    document.getElementById("rec-cost-wrap").style.display = (t === "sale") ? "" : "none";
    document.getElementById("rec-who-label").textContent =
      t === "purchase" ? _ts.supplier_opt : (t === "sale" ? _ts.customer_opt : "Paid to (optional)");
    // Walk-in only makes sense for a SALE customer; hide the quick chips for
    // purchase/expense (still keep the autocomplete + free-text name field).
    var _wq = document.getElementById("rec-who-quick");
    if (_wq) _wq.classList.toggle("hidden", t !== "sale");
    // Part payment (deposit + balance) doesn't apply to expenses — hide that chip
    // and fall back to cash if it was selected.
    var partChip = document.querySelector('#rec-pay .chip[data-p="part"]');
    if (partChip) partChip.classList.toggle("hidden", isExpense);
    if (isExpense && recPayVal === "part") recPay("cash");
    // Pay-link is a SALE-only collection method (you collect from a customer).
    var plChip = document.getElementById("rec-pay-paylink");
    if (plChip) plChip.classList.toggle("hidden", t !== "sale");
    if (t !== "sale" && recPayVal === "paylink") recPay("cash");
    // Prepaid/periodic spread is an EXPENSE-only option.
    var spreadWrap = document.getElementById("rec-spread-wrap");
    if (spreadWrap) spreadWrap.classList.toggle("hidden", !isExpense);

    // PRODUCE mode: pick a finished good + quantity produced (+ optional waste).
    // Amount/cost/customer/payment don't apply — cost comes from the recipe.
    var isProduce = (t === "produce");
    // Produce uses the catalog picker (a finished good to make), never free text.
    if (isProduce) {
      document.getElementById("rec-prod-btn").classList.remove("hidden");
      descInput.classList.add("hidden");
    }
    // Amount field wrapper is the parent .field of rec-amount.
    var amountField = document.getElementById("rec-amount").closest(".field");
    if (amountField) amountField.style.display = isProduce ? "none" : "";
    // Waste field only for produce.
    var wasteWrap = document.getElementById("rec-waste-wrap");
    if (wasteWrap) wasteWrap.classList.toggle("hidden", !isProduce);
    // Hide customer + payment for produce (internal event, no money in/out).
    var whoField = document.getElementById("rec-who").closest(".field");
    if (whoField) whoField.style.display = isProduce ? "none" : "";
    var payField = document.querySelector('#rec-pay').closest(".field");
    if (payField) payField.style.display = isProduce ? "none" : "";
    if (isProduce) {
      document.getElementById("rec-desc-label").textContent = "What did you produce?";
      var _pt2 = document.getElementById("rec-prod-text");
      if (_pt2 && !pick.name) _pt2.textContent = "Tap to choose a product to make";
      var qtyLabel2 = document.getElementById("rec-qty-label");
      if (qtyLabel2) qtyLabel2.textContent = "Quantity produced";
      if (freeUnit) { freeUnit.classList.add("hidden"); freeUnit.value = ""; }
      var rq2 = document.getElementById("rec-qty");
      if (rq2 && (rq2.value === "" )) rq2.value = "1";
    }
  }
  window.recType = function (t) {
    recTypeVal = t;
    // Reset the picked product (and its unit selector) when switching type.
    pick = { key: null, name: null, variant: null };
    var _us2 = document.getElementById("rec-unit-sel");
    if (_us2) { _us2.innerHTML = ""; _us2.classList.add("hidden"); }
    document.getElementById("rec-prod-text").textContent = "Tap to choose a product";
    document.getElementById("rec-prod-text").style.color = "var(--hint)";
    var chips = document.querySelectorAll("#rec-type .chip");
    chips.forEach(function (c) { c.classList.toggle("active", c.getAttribute("data-t") === t); });
    recSyncLabels();
    recFillContacts();   // re-scope name suggestions to the new type
  };
  window.recPay = function (p) {
    recPayVal = p;
    var chips = document.querySelectorAll("#rec-pay .chip");
    chips.forEach(function (c) { c.classList.toggle("active", c.getAttribute("data-p") === p); });
    // Show the deposit field only for a Part payment (deposit now + balance owed).
    var isPart = (p === "part");
    document.getElementById("rec-deposit-wrap").classList.toggle("hidden", !isPart);
    if (isPart) recBalanceHint();
    // Pay-link explainer (#10) only when that method is chosen.
    var plh = document.getElementById("rec-paylink-hint");
    if (plh) plh.classList.toggle("hidden", p !== "paylink");
    // SITE field (Stage 2): only for a SALE that creates a debt (credit/part/
    // pay-link) — that's when a per-site open balance is created.
    var owes = (recTypeVal === "sale") && (p === "credit" || p === "part" || p === "paylink");
    var sw = document.getElementById("rec-site-wrap");
    if (sw) {
      sw.classList.toggle("hidden", !owes);
      if (owes) recFillSites();
    }
  };
  // Populate the site datalist from sites already used on open items across the
  // catalog of contacts (best-effort; falls back to empty). Also relabels the
  // field with the owner's chosen layer name.
  function recFillSites() {
    var lblEl = document.getElementById("rec-site-label");
    if (lblEl) lblEl.textContent = (APP.siteLabel || "Location") + " (optional)";
    var dl = document.getElementById("rec-site-list");
    if (!dl) return;
    // Reuse any sites we've already seen this session (collected when contact
    // cards load their site summaries); keep it simple + non-blocking.
    dl.innerHTML = "";
    var seen = {};
    (APP.knownSites || []).forEach(function (s) {
      if (s && !seen[s.toLowerCase()]) {
        seen[s.toLowerCase()] = 1;
        var o = document.createElement("option"); o.value = s; dl.appendChild(o);
      }
    });
  }
  // Live "balance owed" preview under the deposit field.
  window.recBalanceHint = function () {
    var amount = parseFloat(document.getElementById("rec-amount").value) || 0;   // money, kobo
    var dep = parseFloat(document.getElementById("rec-deposit").value) || 0;
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
    var _pi = document.getElementById("rec-prod-info");
    if (_pi) { _pi.textContent = ""; _pi.classList.add("hidden"); }
    var _sp = document.getElementById("rec-spread");
    if (_sp) _sp.value = "";
    var _spn = document.getElementById("rec-spread-n");
    if (_spn) { _spn.value = ""; _spn.classList.add("hidden"); }
    var _sph = document.getElementById("rec-spread-hint");
    if (_sph) _sph.textContent = "";
    recType("sale"); recPay("cash");
    document.getElementById("rec-desc").value = "";
    document.getElementById("rec-amount").value = "";
    var _rs = document.getElementById("rec-site"); if (_rs) _rs.value = "";
    document.getElementById("rec-qty").value = "1";
    // No product picked yet → no alternative units to choose.
    var _us = document.getElementById("rec-unit-sel");
    if (_us) { _us.innerHTML = ""; _us.classList.add("hidden"); }
    document.getElementById("rec-cost").value = "";
    document.getElementById("rec-who").value = "";
    var _rw = document.getElementById("rec-waste");
    if (_rw) _rw.value = "";
    document.getElementById("rec-deposit").value = "";
    document.getElementById("rec-deposit-wrap").classList.add("hidden");
    document.getElementById("rec-balance-hint").textContent = "";
    document.getElementById("rec-err").textContent = "";
    document.getElementById("rec-save").disabled = false;
    recFillContacts();
    document.getElementById("recOverlay").classList.remove("hidden");
  };
  window.closeRecord = function () {
    document.getElementById("recOverlay").classList.add("hidden");
  };
  // Prepaid spread: resolve the chosen option into {periods, cadence} or null.
  window.recSpreadHint = function () {
    var sel = document.getElementById("rec-spread");
    var nBox = document.getElementById("rec-spread-n");
    var hint = document.getElementById("rec-spread-hint");
    if (!sel) return;
    var v = sel.value;
    var showN = (v === "custom" || v === "1:quarter");
    if (nBox) nBox.classList.toggle("hidden", !showN);
    if (!hint) return;
    var res = recSpreadValue();
    if (!res) { hint.textContent = ""; return; }
    var amt = parseFloat(document.getElementById("rec-amount").value) || 0;
    var per = amt > 0 ? naira(Math.round(amt / res.periods)) : "";
    var word = res.cadence === "quarter" ? "quarter" : (res.cadence === "year" ? "year" : "month");
    hint.textContent = amt > 0
      ? ("Pay the full amount now; the expense shows " + per + " per " + word +
         " for " + res.periods + " " + word + (res.periods > 1 ? "s" : "") + ".")
      : ("Full amount leaves now; the expense is spread over " + res.periods + " " + word + "s.");
  };
  function recSpreadValue() {
    var sel = document.getElementById("rec-spread");
    if (!sel || !sel.value) return null;
    if (sel.value === "custom") {
      var n = parseInt(document.getElementById("rec-spread-n").value, 10);
      return (n >= 2) ? { periods: n, cadence: "month" } : null;
    }
    if (sel.value === "1:quarter") {
      var q = parseInt(document.getElementById("rec-spread-n").value, 10);
      return (q >= 2) ? { periods: q, cadence: "quarter" } : null;
    }
    var parts = sel.value.split(":");
    return { periods: parseInt(parts[0], 10), cadence: parts[1] || "month" };
  }
  // Walk-in / Skip quick buttons for the customer/supplier field.
  //   walkin → records a generic "Walk-in customer" so it still shows on records
  //   skip   → leaves the name blank (records nothing for who)
  window.recWho = function (mode) {
    var el = document.getElementById("rec-who");
    if (!el) return;
    el.value = (mode === "walkin") ? "Walk-in customer" : "";
    var drop = document.getElementById("rec-who-drop");
    if (drop) { drop.classList.add("hidden"); drop.innerHTML = ""; }
    el.focus();
  };
  // Build the scoped name list for the CUSTOM dropdown (see #rec-who-drop). We
  // moved off the native <datalist> because on mobile it only feeds the keyboard
  // suggestion strip (fragile, vanishes on tap). recWhoNames holds the names
  // relevant to the current record type; recWhoFilter renders the tappable list.
  var recWhoNames = [];
  function recFillContacts() {
    function build() {
      if (!crmData) { recWhoNames = []; return; }
      // ONLY the names for this record type's role (sale→customers,
      // purchase→suppliers, expense→payees); fall back to all if empty.
      var primary = (recTypeVal === "purchase") ? ["suppliers"]
                  : (recTypeVal === "expense") ? ["expense_payees"]
                  : ["customers"];
      var seen = {}, names = [];
      function collect(keys) {
        keys.forEach(function (k) {
          (crmData[k] || []).forEach(function (c) {
            var n = (c.name || "").trim();
            if (n && !seen[n.toLowerCase()]) { seen[n.toLowerCase()] = 1; names.push(n); }
          });
        });
      }
      collect(primary);
      if (!names.length) collect(["customers", "suppliers", "expense_payees"]);
      names.sort(function (a, b) { return a.localeCompare(b); });
      recWhoNames = names;
    }
    if (crmData) { build(); return; }
    api("api/contacts").then(function (d) { crmData = d; crmLoaded = true; build(); })
      .catch(function () { /* best-effort */ });
  }
  // Render the tappable suggestion list under #rec-who, filtered by what's typed.
  window.recWhoFilter = function () {
    var input = document.getElementById("rec-who");
    var drop = document.getElementById("rec-who-drop");
    if (!input || !drop) return;
    var q = (input.value || "").trim().toLowerCase();
    // Substring match; if the box is empty, show all scoped names.
    var matches = recWhoNames.filter(function (n) {
      return !q || n.toLowerCase().indexOf(q) !== -1;
    }).slice(0, 8);
    // Don't show a 1-item list that exactly equals what's typed (nothing to add).
    if (!matches.length || (matches.length === 1 && matches[0].toLowerCase() === q)) {
      drop.classList.add("hidden"); drop.innerHTML = ""; return;
    }
    drop.innerHTML = "";
    matches.forEach(function (n) {
      var row = document.createElement("div");
      row.textContent = n;
      row.style.cssText = "padding:10px 12px;cursor:pointer;border-bottom:1px solid var(--line)";
      // mousedown (not click) fires BEFORE the input's blur, so the pick lands.
      row.addEventListener("mousedown", function (e) {
        e.preventDefault();
        input.value = n;
        drop.classList.add("hidden"); drop.innerHTML = "";
      });
      drop.appendChild(row);
    });
    drop.classList.remove("hidden");
  };
  // Hide the dropdown shortly after blur (delay lets a tap register first).
  window.recWhoBlur = function () {
    setTimeout(function () {
      var drop = document.getElementById("rec-who-drop");
      if (drop) { drop.classList.add("hidden"); }
    }, 150);
  };
  window.saveRecord = function () {
    var err0 = document.getElementById("rec-err");
    // PRODUCE: a separate, simpler path — pick a finished good, enter quantity
    // produced (+ optional waste). Cost is recipe-driven server-side, so no
    // amount/cost/customer/payment. Calls api/produce (not api/transaction).
    if (recTypeVal === "produce") {
      err0.textContent = "";
      if (!pick.key) { err0.textContent = "Choose a product to produce."; return; }
      var pQty = parseFloat(document.getElementById("rec-qty").value);
      if (!(pQty > 0)) { err0.textContent = "Enter how many you produced."; return; }
      var pWaste = parseFloat(document.getElementById("rec-waste").value) || 0;
      var pUnit = recUnitName();   // e.g. "pack" — server converts to base units
      var pbtn = document.getElementById("rec-save");
      pbtn.disabled = true;
      var pBody = { key: pick.key, quantity: pQty, waste: pWaste, unit: pUnit };
      // Two-tap "produce anyway" when materials are short (409 needs_confirm).
      function sendProduce() {
        apiPost("api/produce", pBody)
          .then(function () {
            closeRecord();
            if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
            loadSummary();
            invLoaded = false;
            var cv = document.getElementById("view-cat");
            if (cv && !cv.classList.contains("hidden")) loadInventory();
          })
          .catch(function (e) {
            pbtn.disabled = false;
            if (e && e.body && e.body.needs_confirm && !pBody.confirm) {
              pBody.confirm = true;
              err0.textContent = (e.body.warning || e.message) + " — tap Save again to proceed.";
              if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("warning");
            } else {
              err0.textContent = (e && e.message) || "Could not record production";
            }
          });
      }
      sendProduce();
      return;
    }
    var isExpense = (recTypeVal === "expense");
    // Description: expense = free text; sale/purchase = picked product name.
    var desc = isExpense
      ? (document.getElementById("rec-desc").value || "").trim()
      : (pick.name || "");
    var amount = parseFloat(document.getElementById("rec-amount").value) || 0;   // money, kobo
    // Quantity may be fractional (0.5 kg, 2.5 L) — parseFloat, not parseInt.
    // Still at least a positive amount; a blank/0 falls back to 1.
    var qtyRawV = parseFloat(document.getElementById("rec-qty").value);
    var qtyEntered = (qtyRawV > 0) ? qtyRawV : 1;
    // Convert the entered qty (in the CHOSEN unit) to BASE units, so stock and
    // COGS stay in the product's base unit. Factor is 1 when there's no unit
    // selector (simple product). e.g. 10 bags × 20 = 200 pieces if base=piece,
    // or 10 bags × 1 = 10 bags if base=bag.
    var qty = qtyEntered * recUnitFactor();
    var cost = parseFloat(document.getElementById("rec-cost").value) || 0;   // COGS money, kobo
    var who = (document.getElementById("rec-who").value || "").trim();
    var err = document.getElementById("rec-err");
    err.textContent = "";
    if (!isExpense && !pick.key) { err.textContent = "Please choose a product."; return; }
    if (isExpense && !desc) { err.textContent = "Please enter what it was for."; return; }
    if (amount <= 0) { err.textContent = "Please enter an amount."; return; }

    // PREPAID / periodic EXPENSE: pay full now, spread the expense over N periods.
    // Goes to the dedicated engine endpoint (full cash out today + N recognition
    // rows), not the normal single-row save.
    var spread = isExpense ? recSpreadValue() : null;
    if (spread) {
      var sbtn = document.getElementById("rec-save");
      sbtn.disabled = true;
      apiPost("api/prepaid-expense", {
        amount: amount, description: desc,
        vendor: (document.getElementById("rec-who").value || "").trim(),
        periods: spread.periods, cadence: spread.cadence
      }).then(function () {
        closeRecord();
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        loadSummary();
      }).catch(function (e) {
        sbtn.disabled = false;
        err.textContent = (e && e.message) || "Could not record the prepaid expense";
      });
      return;
    }

    // PAY-LINK (#10): record the sale as OWING (credit), then mint a Paystack
    // link for the amount. When the customer pays, the existing webhook
    // auto-settles that debt as a repayment (no double-count). So under the
    // hood it's a credit sale + a pay-link; it needs a customer name.
    var isPayLink = (recPayVal === "paylink");

    // Part payment = deposit now + balance owed. If the deposit covers the full
    // amount, treat it as a normal (paid) transfer — mirrors the chat flow.
    var isPart = recPayVal === "part";
    var deposit = isPart ? (parseFloat(document.getElementById("rec-deposit").value) || 0) : 0;   // money, kobo
    if (isPart && deposit >= amount) { isPart = false; recPayVal = "transfer"; }
    var isCredit = (recPayVal === "credit") || isPart || isPayLink;  // all create a debt

    if (isPayLink && !who) {
      err.textContent = "A pay-link sale needs a customer name (the link settles their debt).";
      return;
    }
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
      // Pay-link records as a normal CREDIT sale (the engine has no 'paylink'
      // method); the link is created after the save.
      payment_method: isPart ? "deposit" : (isPayLink ? "credit" : recPayVal),
      vendor: who, has_credit: isCredit,
    };
    if (isPart) {
      body.deposit_amount = deposit;
      body.balance_owed = amount - deposit;
    }
    // Site tag (Stage 2) — only for a debt sale (credit/part/pay-link).
    if (isCredit) {
      var siteV = (document.getElementById("rec-site").value || "").trim();
      if (siteV) {
        body.site = siteV;
        if (APP.knownSites.indexOf(siteV) < 0) APP.knownSites.push(siteV);
      }
    }
    if (!isExpense) {
      body.quantity = String(qty);
      if (pick.key) { body.catalog_product = pick.key; body.catalog_product_name = pick.name; }
      if (pick.variant) body.variant = pick.variant;
    } else {
      // Expense quantity is OPTIONAL. Only send it when the owner actually
      // typed one (>0). Combine with the free-text unit so "50 litres" is kept
      // as the quantity string (save_transaction stores the numeric part; the
      // unit rides along in the string for display/analysis).
      var eq = parseFloat(document.getElementById("rec-qty").value);
      if (eq > 0) {
        var eu = (document.getElementById("rec-unit-free").value || "").trim();
        body.quantity = eu ? (String(eq) + " " + eu) : String(eq);
      }
    }
    if (recTypeVal === "sale" && cost > 0) body.landing_cost = cost;
    // P2: a Services business's sale is a job/service, not a stocked product —
    // tell the engine so it stamps sale_kind="service" (matches the chat flow).
    // Only pure Services auto-tags; Hybrid genuinely sells products too, so it
    // stays a product sale unless/until a service/product split is added there.
    if (recTypeVal === "sale" && isServices()) body.is_service_job = true;
    var btn = document.getElementById("rec-save");
    btn.disabled = true;
    // sendSale posts the sale; on a soft guardrail (409 needs_confirm) it relabels
    // the Save button so a second tap re-posts with confirm:true (the two-tap
    // "proceed anyway" pattern — no confirm() dialog, unsupported in Telegram).
    function afterSale() {
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      loadSummary();
      invLoaded = false;
      var catView = document.getElementById("view-cat");
      if (catView && !catView.classList.contains("hidden")) loadInventory();
    }
    function sendSale() {
      apiPost("api/transaction", body)
        .then(function () {
          // Pay-link (#10): the credit sale is recorded; now mint the link for
          // this customer + amount and show it in the pay-link sheet to forward.
          if (isPayLink) {
            return apiPost("api/payment-request", {
              amount: amount, description: desc || "Payment", customer: who
            }).then(function (r) {
              closeRecord();
              afterSale();
              // Reuse the pay-link result UI: open the sheet showing the URL.
              plCtx = { name: who };
              document.getElementById("pl-sub").textContent =
                "Link created for " + who + " — send it to collect.";
              var psel = document.getElementById("pl-product"); if (psel) psel.value = "";
              document.getElementById("pl-amount").value = amount;
              document.getElementById("pl-desc").value = desc || "";
              document.getElementById("pl-err").textContent = "";
              document.getElementById("pl-url").value = (r && r.payment_url) || "";
              document.getElementById("pl-result").classList.remove("hidden");
              // A link already exists (created with the sale) — hide Create so a
              // second tap can't mint a duplicate.
              var _plc2 = document.getElementById("pl-create");
              _plc2.disabled = true; _plc2.classList.add("hidden");
              document.getElementById("payLinkOverlay").classList.remove("hidden");
            });
          }
          closeRecord();
          afterSale();
        })
        .catch(function (e) {
          btn.disabled = false;
          if (e && e.body && e.body.needs_confirm && !body.confirm) {
            body.confirm = true;
            err.textContent = (e.body.warning || e.message) + " — tap Save again to proceed.";
            if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("warning");
          } else {
            err.textContent = (e && e.message) || "Could not record";
          }
        });
    }
    sendSale();
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
  <div style="text-align:center;margin:14px 0 8px;font-size:11px;color:var(--hint);opacity:.6">Kashia \u00b7 build __BUILD_STAMP__</div>
</body>
</html>"""
