"""Shared unit-conversion engine — the single source of truth for units.

Kashia products track stock + per-unit cost in ONE canonical **base unit**.
Every other unit a product uses is expressed as a **factor to that base**
(1 <unit> = factor <base>). This module:

  * ships a built-in STANDARD unit library (mass, volume, length, time, energy,
    count) — ratio-based systems the user should NEVER have to teach;
  * resolves ANY unit → the product's base unit, using the standard library
    first and then the product's own custom edges (multi-hop, both directions),
    with contradiction detection;
  * parses conversion RULES ("1 bag = 20 pieces", "a bag is 20 pieces") and
    QUANTITIES ("2 bags", "2.5 kg", "three cartons") deterministically — no AI;
  * converts a quantity from any recognised unit into the base unit.

Design notes:
  - **Ratio-only.** Temperature (offset math) is intentionally excluded — a
    stock/finance bot buys quantities, not temperatures.
  - **Cross-system conversion is refused** (kg → litre is meaningless).
  - **Fraction-safe** (delegates number parsing to utils.quantity); callers keep
    money kobo-safe via utils.money separately.
  - Everything is defensive: bad input returns a sane result, never raises.

Data shape on a product (engine-owned; see docs/UNIT_CONVERSION_PLAN.md):
    "base_unit": "pieces"              # canonical unit (mirrors legacy primary_unit)
    "unit_defs": {"bag": 20, "truck": 4000, "load": 100}   # 1 unit = N base
"""

import re
from utils.quantity import to_qty

# ─────────────────────────────────────────────────────────────────────────
# STANDARD UNIT LIBRARY (built-in, ratio-based). Each system maps a unit name
# to its factor in the system's INTERNAL base. Cross-system is never allowed.
# Add aliases freely; keep singular keys (we normalise plurals on lookup).
# ─────────────────────────────────────────────────────────────────────────
_SYSTEMS = {
    "mass": {
        "base": "g",
        "units": {
            "mg": 0.001, "g": 1.0, "gram": 1.0, "gramme": 1.0,
            "kg": 1000.0, "kilo": 1000.0, "kilogram": 1000.0, "kilogramme": 1000.0,
            "tonne": 1_000_000.0, "ton": 1_000_000.0,
            "oz": 28.3495, "ounce": 28.3495,
            "lb": 453.592, "pound": 453.592,
        },
    },
    "volume": {
        "base": "ml",
        "units": {
            "ml": 1.0, "millilitre": 1.0, "milliliter": 1.0,
            "cl": 10.0, "centilitre": 10.0, "centiliter": 10.0,
            "l": 1000.0, "litre": 1000.0, "liter": 1000.0,
            "gallon": 3785.41, "gal": 3785.41,
        },
    },
    "length": {
        "base": "m",
        "units": {
            "mm": 0.001, "millimetre": 0.001, "millimeter": 0.001,
            "cm": 0.01, "centimetre": 0.01, "centimeter": 0.01,
            "m": 1.0, "metre": 1.0, "meter": 1.0,
            "km": 1000.0, "kilometre": 1000.0, "kilometer": 1000.0,
        },
    },
    "time": {
        "base": "min",
        "units": {
            "min": 1.0, "minute": 1.0,
            "hr": 60.0, "hour": 60.0,
            "day": 1440.0,
            "week": 10080.0,
        },
    },
    "energy": {
        "base": "wh",
        "units": {
            "wh": 1.0, "watt": 1.0,        # treat watt as wh of usage in this bot
            "kwh": 1000.0, "kw": 1000.0,   # kW over an hour ≈ kWh for stock/costing
        },
    },
    "count": {
        "base": "piece",
        "units": {
            "piece": 1.0, "pieces": 1.0, "pc": 1.0, "pcs": 1.0,
            "unit": 1.0, "units": 1.0, "each": 1.0, "ea": 1.0,
        },
    },
}

# Small number-words for lenient quantity parsing ("three cartons").
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "dozen": 12, "half": 0.5,
}


def normalize_unit(unit) -> str:
    """Lowercase, strip, singularise a unit token. '' for empty/None."""
    if not unit:
        return ""
    u = str(unit).strip().lower()
    # Strip a trailing plural 's' unless the singular-with-s is a known key
    # (e.g. keep 'pcs'). We singularise only when it helps a lookup.
    return u


def _std_lookup(unit: str):
    """Return (system_name, factor_in_system_base) for a standard unit, or None.
    Tolerates plural/singular."""
    u = normalize_unit(unit)
    if not u:
        return None
    candidates = [u]
    if u.endswith("s"):
        candidates.append(u[:-1])          # bags -> bag (not standard, but cheap)
    else:
        candidates.append(u + "s")
    for cand in candidates:
        for sys_name, spec in _SYSTEMS.items():
            if cand in spec["units"]:
                return (sys_name, spec["units"][cand])
    return None


def standard_factor(from_unit: str, to_unit: str):
    """Factor to multiply a `from_unit` quantity by to get `to_unit`, when BOTH
    are in the SAME standard system. Returns float, or None if not both standard
    or cross-system. e.g. standard_factor('kg','g') -> 1000.0."""
    a = _std_lookup(from_unit)
    b = _std_lookup(to_unit)
    if not a or not b:
        return None
    if a[0] != b[0]:                       # cross-system (kg -> litre): refuse
        return None
    # from_factor (in system base) / to_factor (in system base)
    if b[1] == 0:
        return None
    return a[1] / b[1]


# ─────────────────────────────────────────────────────────────────────────
# PER-PRODUCT RESOLUTION — factor of ANY unit → the product's base unit.
# unit_defs stores custom edges already reduced to factor-to-base. We still
# support resolving via the standard library when base + unit share a system.
# ─────────────────────────────────────────────────────────────────────────
def factor_to_base(unit: str, base_unit: str, unit_defs: dict = None):
    """How many `base_unit` is 1 `unit`? Returns float, or None if unresolvable.

    Resolution order:
      1. unit == base            -> 1
      2. custom unit_defs[unit]  -> stored factor-to-base
      3. standard library        -> if unit & base share a standard system
    """
    u = normalize_unit(unit)
    b = normalize_unit(base_unit)
    if not u or not b:
        return None
    if u == b or (u.rstrip("s") == b.rstrip("s")):
        return 1.0
    defs = unit_defs or {}
    # custom edge (case-insensitive, plural-tolerant)
    for k, v in defs.items():
        nk = normalize_unit(k)
        if nk == u or nk.rstrip("s") == u.rstrip("s"):
            try:
                f = float(v)
                return f if f > 0 else None
            except (TypeError, ValueError):
                return None
    # standard library (same system as base)
    return standard_factor(u, b)


def convert(quantity, from_unit: str, base_unit: str, unit_defs: dict = None):
    """Convert `quantity` expressed in `from_unit` into `base_unit`.

    Returns (base_qty: float, ok: bool). On an unresolvable/cross-system unit,
    returns (quantity_as_is, False) so the caller can warn and store raw.
    Fraction-safe.
    """
    qty = to_qty(quantity)
    f = factor_to_base(from_unit, base_unit, unit_defs)
    if f is None:
        return (qty, False)
    return (qty * f, True)


# ─────────────────────────────────────────────────────────────────────────
# GRAPH BUILD + CONTRADICTION DETECTION
# Given raw edges (1 A = k B) build unit_defs (unit -> factor-to-base). Handles
# multi-hop (load -> bag -> pieces) and both directions. Detects contradictions.
# ─────────────────────────────────────────────────────────────────────────
def build_unit_defs(base_unit: str, edges: list):
    """Resolve a list of edges into {unit: factor_to_base}.

    edges: list of (qty_a, unit_a, qty_b, unit_b) meaning qty_a unit_a = qty_b
    unit_b. Uses the standard library for any leg it can, then propagates across
    custom units by repeated relaxation until stable (multi-hop, bidirectional).

    Returns (unit_defs: dict, conflicts: list[str]). A conflict is raised when an
    edge forces a unit to a factor that disagrees (>0.5%) with an already-known
    one.
    """
    base = normalize_unit(base_unit)
    known = {base: 1.0}
    conflicts = []

    # Normalise edges to (unit_a, per_a_in_b, unit_b): 1 unit_a = per_a_in_b unit_b
    norm_edges = []
    for e in (edges or []):
        try:
            qa, ua, qb, ub = e
            qa = to_qty(qa); qb = to_qty(qb)
            ua = normalize_unit(ua); ub = normalize_unit(ub)
            if not ua or not ub or qa <= 0 or qb <= 0:
                continue
            # 1 ua = (qb/qa) ub   and   1 ub = (qa/qb) ua
            norm_edges.append((ua, qb / qa, ub))
            norm_edges.append((ub, qa / qb, ua))
        except (TypeError, ValueError):
            continue

    def _set(unit, factor):
        if factor is None or factor <= 0:
            return
        prev = known.get(unit)
        if prev is None:
            known[unit] = factor
        else:
            # contradiction if they disagree by > 0.5%
            if prev > 0 and abs(prev - factor) / prev > 0.005:
                conflicts.append(
                    f"{unit}: {factor:g} vs known {prev:g} {base}"
                )

    # relaxation: keep propagating until no new unit resolves
    changed = True
    passes = 0
    while changed and passes < 50:
        changed = False
        passes += 1
        for ua, per_a_in_b, ub in norm_edges:
            # if ub resolves to base, then ua = per_a_in_b * factor(ub)
            fb = known.get(ub)
            if fb is None:
                fb = standard_factor(ub, base)
            if fb is not None:
                new = per_a_in_b * fb
                if ua not in known:
                    _set(ua, new)
                    changed = True
                else:
                    _set(ua, new)   # records a conflict if disagreeing
    # drop the base itself from the returned custom defs
    defs = {u: f for u, f in known.items() if u != base}
    return (defs, conflicts)


# ─────────────────────────────────────────────────────────────────────────
# LEGACY MIGRATION — upgrade old conversion shapes to base_unit + unit_defs.
# Non-destructive: run on read; if it changes anything the caller persists once.
# ─────────────────────────────────────────────────────────────────────────
def _legacy_conversions_to_edges(conversions: dict):
    """Turn a legacy `conversions` dict (either shape) into build_unit_defs edges.

    Shape A (live catalog):  {"1 carton": {"qty": 24, "unit": "pieces"}}
    Shape B (legacy db):     {"1 carton": "10 pairs"}   (str value)
    Both mean: <key> = <value>. Key/value are "<qty> <unit>".
    Returns list of (qa, ua, qb, ub).
    """
    edges = []
    for key, val in (conversions or {}).items():
        km = re.match(r"^\s*([\d.]+)\s+(.+?)\s*$", str(key).strip())
        if not km:
            continue
        qa = to_qty(km.group(1)); ua = km.group(2).strip().lower()
        if isinstance(val, dict):
            qb = to_qty(val.get("qty", 0)); ub = normalize_unit(val.get("unit", ""))
        else:
            vm = re.match(r"^\s*([\d.]+)\s+(.+?)\s*$", str(val).strip())
            if not vm:
                continue
            qb = to_qty(vm.group(1)); ub = vm.group(2).strip().lower()
        if qa > 0 and qb > 0 and ua and ub:
            edges.append((qa, ua, qb, ub))
    return edges


def upgrade_product_units(product: dict):
    """Ensure a product dict has engine-owned `base_unit` + `unit_defs`, upgrading
    from legacy `primary_unit` + `conversions` on the fly.

    Returns True if the dict was MODIFIED (so the caller can persist once).
    Idempotent and non-destructive: legacy fields are left in place; already-
    upgraded products (have unit_defs and base_unit matching primary_unit) are
    untouched. Never raises.
    """
    if not isinstance(product, dict):
        return False
    changed = False
    try:
        legacy_primary = normalize_unit(product.get("primary_unit", ""))
        base = normalize_unit(product.get("base_unit", "")) or legacy_primary

        # If there are legacy conversions and no unit_defs yet, build them.
        has_defs = isinstance(product.get("unit_defs"), dict) and product.get("unit_defs")
        conversions = product.get("conversions") or {}
        if conversions and not has_defs:
            edges = _legacy_conversions_to_edges(conversions)
            if edges:
                # If no base is set, infer it from the most common RHS unit of the
                # edges (the side legacy rules pointed *to*), else fall back.
                if not base:
                    rhs = {}
                    for (_qa, _ua, _qb, ub) in edges:
                        rhs[ub] = rhs.get(ub, 0) + 1
                    base = max(rhs, key=rhs.get) if rhs else ""
                if base:
                    defs, _conflicts = build_unit_defs(base, edges)
                    if defs:
                        product["unit_defs"] = defs
                        changed = True

        # Keep base_unit present + in sync with legacy primary_unit.
        if base and product.get("base_unit") != base:
            product["base_unit"] = base
            changed = True
        # Back-fill legacy primary_unit if the product only had base_unit (web/new).
        if base and not legacy_primary:
            product["primary_unit"] = base
            changed = True
    except Exception:
        return changed
    return changed


def product_units(product: dict):
    """Convenience read: return (base_unit, unit_defs) for a product, tolerant of
    legacy-only products (does an in-memory upgrade without persisting)."""
    if not isinstance(product, dict):
        return ("", {})
    base = normalize_unit(product.get("base_unit", "")) or normalize_unit(product.get("primary_unit", ""))
    defs = product.get("unit_defs")
    if not isinstance(defs, dict):
        defs = {}
    if not defs and (product.get("conversions") or not base):
        # in-memory upgrade (does not mutate the caller's persisted copy intent;
        # upgrade_product_units mutates + signals persist separately)
        tmp = dict(product)
        upgrade_product_units(tmp)
        base = normalize_unit(tmp.get("base_unit", "")) or base
        defs = tmp.get("unit_defs") if isinstance(tmp.get("unit_defs"), dict) else {}
    return (base, defs or {})


# ─────────────────────────────────────────────────────────────────────────
# DETERMINISTIC PARSERS (no AI)
# ─────────────────────────────────────────────────────────────────────────
# "1 bag = 20 pieces" | "1 bag = 20 piece" | "20 bags = 1 truck"
_RULE_EQ = re.compile(
    r"^\s*([\d.]+)\s*([a-zA-Z ]+?)\s*=\s*([\d.]+)\s*([a-zA-Z ]+?)\s*$"
)
# "a bag is 20 pieces" | "1 bag is 20 pieces" | "one bag = 20 pieces"
_RULE_IS = re.compile(
    r"^\s*(a|an|one|\d+)?\s*([a-zA-Z ]+?)\s+(?:is|=|equals?|holds?|contains?|has)\s+([\d.]+|\w+)\s*([a-zA-Z ]+?)\s*$",
    re.IGNORECASE,
)


def _num(token):
    """Parse a number token OR a small number-word; None if neither."""
    if token is None:
        return None
    t = str(token).strip().lower()
    if t == "":
        return None
    if t in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[t])
    try:
        return float(t)
    except ValueError:
        return None


def parse_rule(text: str):
    """Parse a conversion rule into (qty_a, unit_a, qty_b, unit_b), or None.

    Accepts:
      "1 bag = 20 pieces"      -> (1, 'bag', 20, 'pieces')
      "200 bags = 1 truck"     -> (200, 'bags', 1, 'truck')
      "a bag is 20 pieces"     -> (1, 'bag', 20, 'pieces')
      "1 crate = 24 bottles"   -> (1, 'crate', 24, 'bottles')
    """
    if not text:
        return None
    s = str(text).strip()

    m = _RULE_EQ.match(s)
    if m:
        qa = _num(m.group(1)); ua = m.group(2).strip().lower()
        qb = _num(m.group(3)); ub = m.group(4).strip().lower()
        if qa and qb and ua and ub:
            return (qa, ua, qb, ub)

    m = _RULE_IS.match(s)
    if m:
        qa = _num(m.group(1)) if m.group(1) else 1.0
        ua = m.group(2).strip().lower()
        qb = _num(m.group(3)); ub = m.group(4).strip().lower()
        if qa and qb and ua and ub:
            return (qa, ua, qb, ub)
    return None


# "2 bags" | "2.5 kg" | "three cartons" | "10 pairs" | ".5 litre"
_QTY_UNIT = re.compile(r"^\s*([\d.]+|[a-zA-Z]+)\s*([a-zA-Z ]+?)\s*$")


def parse_quantity(text: str):
    """Parse a quantity+unit string into (qty: float, unit: str) or None.

    "2 bags" -> (2.0, 'bags'); "2.5 kg" -> (2.5, 'kg'); "three cartons" ->
    (3.0, 'cartons'); "5" -> (5.0, ''); bare unit -> None.
    """
    if text is None:
        return None
    s = str(text).strip()
    if s == "":
        return None
    # bare number
    try:
        return (float(s), "")
    except ValueError:
        pass
    m = _QTY_UNIT.match(s)
    if not m:
        return None
    n = _num(m.group(1))
    unit = m.group(2).strip().lower()
    if n is None or not unit:
        return None
    return (n, unit)


# ─────────────────────────────────────────────────────────────────────────
# SELF-TEST (run: python -m utils.units  OR  python src/utils/units.py)
# ─────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    fails = []

    def ck(name, got, want, tol=1e-6):
        ok = (got == want) if not isinstance(want, float) else (
            got is not None and abs(got - want) <= tol)
        print(f"{'OK ' if ok else 'XX '}{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    # standard library
    ck("kg->g", standard_factor("kg", "g"), 1000.0)
    ck("g->kg", standard_factor("g", "kg"), 0.001)
    ck("litre->ml", standard_factor("litre", "ml"), 1000.0)
    ck("cross_system_refuse", standard_factor("kg", "litre"), None)
    ck("plural_kgs->g", standard_factor("kgs", "g"), 1000.0)

    # owner scenario — build in a DELIBERATELY messy order, base = pieces
    edges = [
        (1, "load", 5, "bags"),        # load before bag known
        (200, "bags", 1, "truck"),     # truck via bags
        (1, "bag", 20, "pieces"),
        (1, "truck", 4000, "pieces"),
    ]
    defs, conflicts = build_unit_defs("pieces", edges)
    ck("bag->pieces", defs.get("bag"), 20.0)
    ck("truck->pieces", defs.get("truck"), 4000.0)
    ck("load->pieces", defs.get("load"), 100.0)
    ck("no_conflicts", conflicts, [])

    # 200 bags = 1 truck consistency (200*20 == 4000)
    b2, ok2 = convert(200, "bags", "pieces", defs)
    t1, ok3 = convert(1, "truck", "pieces", defs)
    ck("200bags==1truck", b2, t1)

    # record quantities
    ck("2 trucks", convert(2, "trucks", "pieces", defs)[0], 8000.0)
    ck("3 bags", convert(3, "bags", "pieces", defs)[0], 60.0)
    ck("1 load", convert(1, "load", "pieces", defs)[0], 100.0)
    ck("1.5 bags frac", convert(1.5, "bags", "pieces", defs)[0], 30.0)

    # contradiction detection
    bad = [(1, "bag", 20, "pieces"), (1, "bag", 25, "pieces")]
    _d, _c = build_unit_defs("pieces", bad)
    ck("contradiction_flagged", len(_c) > 0, True)

    # standard works with NO custom defs (base kg)
    ck("no-teach kg base, 500 g", convert(500, "g", "kg")[0], 0.5)
    ck("cross refuse in convert", convert(1, "litre", "kg")[1], False)

    # parsers
    ck("parse '1 bag = 20 pieces'", parse_rule("1 bag = 20 pieces"), (1.0, "bag", 20.0, "pieces"))
    ck("parse '200 bags = 1 truck'", parse_rule("200 bags = 1 truck"), (200.0, "bags", 1.0, "truck"))
    ck("parse 'a bag is 20 pieces'", parse_rule("a bag is 20 pieces"), (1.0, "bag", 20.0, "pieces"))
    ck("parse '2.5 kg'", parse_quantity("2.5 kg"), (2.5, "kg"))
    ck("parse 'three cartons'", parse_quantity("three cartons"), (3.0, "cartons"))
    ck("parse '2 bags'", parse_quantity("2 bags"), (2.0, "bags"))

    # migration — legacy Model-A (dict values)
    prodA = {
        "primary_unit": "pieces",
        "conversions": {
            "1 bag": {"qty": 20, "unit": "pieces"},
            "1 truck": {"qty": 4000, "unit": "pieces"},
        },
    }
    chgA = upgrade_product_units(prodA)
    ck("migrateA changed", chgA, True)
    ck("migrateA base", prodA.get("base_unit"), "pieces")
    ck("migrateA bag", prodA["unit_defs"].get("bag"), 20.0)
    ck("migrateA truck", prodA["unit_defs"].get("truck"), 4000.0)
    ck("migrateA idempotent", upgrade_product_units(prodA), False)

    # migration — legacy Model-B (str values), base inferred
    prodB = {"conversions": {"1 carton": "24 pieces", "1 dozen": "12 pieces"}}
    chgB = upgrade_product_units(prodB)
    ck("migrateB changed", chgB, True)
    ck("migrateB base_inferred", prodB.get("base_unit"), "pieces")
    ck("migrateB carton", prodB["unit_defs"].get("carton"), 24.0)
    ck("migrateB dozen", prodB["unit_defs"].get("dozen"), 12.0)

    # product_units read on a legacy-only product doesn't need pre-upgrade
    base_r, defs_r = product_units({"primary_unit": "pieces",
                                    "conversions": {"1 bag": {"qty": 20, "unit": "pieces"}}})
    ck("product_units base", base_r, "pieces")
    ck("product_units bag", defs_r.get("bag"), 20.0)

    # already-upgraded product untouched
    up = {"base_unit": "kg", "primary_unit": "kg", "unit_defs": {"bag": 25.0}}
    ck("upgraded_untouched", upgrade_product_units(up), False)

    print()
    if fails:
        print("FAILED:", fails)
        raise SystemExit(1)
    print("ALL_UNITS_OK")
