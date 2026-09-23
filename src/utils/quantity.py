"""Shared, decimal-safe quantity/stock helpers.

Kashia tracks QUANTITY and STOCK as numbers that may be fractional (e.g. 0.5 kg
of nylon, 2.5 litres). MONEY (cost, price, amount) stays integer naira and is NOT
handled here. This module is the single source of truth for:

  * parsing a quantity out of user/DB values ("0.5", "2.5 kg", "10 pairs", 3),
  * formatting a quantity for display (whole numbers show as "2", fractions show
    trimmed: "0.5", "2.5" — never "2.0").

Kept tiny and pure so catalog / transactions / accounting can all import it and
never disagree. Everything is defensive: bad input coerces to a sane default,
never raises.
"""

import re

# Leading number, optionally decimal, optionally followed by a unit word.
# Matches "0.5", "2", "2.5 kg", "10 pairs", ".5", "-3" (deltas).
_QTY_RE = re.compile(r"^\s*(-?\d*\.?\d+)")


def to_qty(value, default: float = 0.0) -> float:
    """Coerce any value (str/int/float/Decimal/None) to a float quantity.

    Accepts a bare number or a leading number in a string like "2.5 kg".
    Returns `default` for empty/unparseable input. Never raises.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return default
    m = _QTY_RE.match(str(value))
    if not m:
        return default
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return default


def fmt_qty(value) -> str:
    """Format a quantity for display: whole numbers as integers ("2"), fractional
    values trimmed of trailing zeros ("0.5", "2.5"). Safe on any input.
    """
    q = to_qty(value, default=0.0)
    if q == int(q):
        return str(int(q))
    # Trim trailing zeros without scientific notation; cap at 4 dp (plenty for
    # kg/litre/kW use). e.g. 0.5 -> "0.5", 2.50 -> "2.5", 0.125 -> "0.125".
    s = f"{q:.4f}".rstrip("0").rstrip(".")
    return s


def qty_is_whole(value) -> bool:
    """True if the quantity has no fractional part (2.0 -> True, 0.5 -> False)."""
    q = to_qty(value, default=0.0)
    return q == int(q)
