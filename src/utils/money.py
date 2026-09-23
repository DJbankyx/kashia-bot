"""Shared, kobo-precise MONEY helpers.

Kashia money is NAIRA and may carry kobo (2 decimal places) — a raw material
can cost ₦0.06/kWh, ₦0.5/ml, etc. This module is the single source of truth for
parsing, rounding, and formatting money. QUANTITY/STOCK is a SEPARATE concern
handled by utils.quantity — do not use these for quantities.

Design (see docs/MONEY_PRECISION_PLAN.md):
  * Compute in full precision (Decimal), round to 2dp ONCE at the storage
    boundary — never mid-calculation.
  * Store as NAIRA. DynamoDB stores numbers as Decimal and
    Database._sanitize_for_dynamo already turns a float into Decimal(str(x))
    (whole -> int), so money_round() may safely return a plain int/float and the
    DB layer persists it losslessly.
  * Always build Decimals from str, never from a binary float.
Everything is defensive: bad input -> a sane default, never raises.
"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

_CENT = Decimal("0.01")


def to_money(value, default="0") -> Decimal:
    """Coerce any value (str/int/float/Decimal/None) to a Decimal naira amount.

    Uses str() so binary-float artifacts (0.06 -> 0.0599999…) never appear.
    Accepts a leading number inside a string (e.g. "₦1,500" -> 1500, "0.06/kWh"
    -> 0.06). Returns Decimal(default) for empty/unparseable input.
    """
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(default)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    # String: strip currency symbols/commas/spaces, take the leading number.
    s = str(value).strip().replace(",", "").replace("\u20a6", "").replace("NGN", "").replace("ngn", "")
    import re
    m = re.match(r"^\s*(-?\d*\.?\d+)", s)
    if not m:
        return Decimal(default)
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return Decimal(default)


def money_round(value):
    """Round money to 2dp (kobo) and return a JSON/DB-friendly number:
    an int when the amount is whole (₦3500 -> 3500, not 3500.00), else a float
    rounded to 2dp (₦0.06 -> 0.06, ₦20.5 -> 20.5).

    Quantize once here, at the storage boundary; keep upstream math in Decimal.
    """
    d = to_money(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    if d == d.to_integral_value():
        return int(d)
    return float(d)


def money_is_whole(value) -> bool:
    """True if the (2dp-rounded) amount has no kobo part (₦20.00 -> True)."""
    d = to_money(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    return d == d.to_integral_value()


def fmt_money(value) -> str:
    """Format money as ₦X,XXX(.kk): whole numbers show no decimals, kobo amounts
    show 2dp, sub-naira shows up to 4dp. Sign-aware. Single source of truth for
    money display (mirrors utils.whatsapp_ui.format_amount)."""
    d = to_money(value)
    if d == 0:
        return "\u20a60"
    sign = "-" if d < 0 else ""
    mag = abs(d)
    if mag < 1:
        body = f"{float(mag):.4f}".rstrip("0").rstrip(".")
    elif mag == mag.to_integral_value():
        body = f"{int(mag):,}"
    else:
        body = f"{float(mag):,.2f}"
    return f"{sign}\u20a6{body}"
