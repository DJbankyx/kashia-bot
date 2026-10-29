#!/usr/bin/env python3
"""Kashia one-off backfill — LOCK each past SALE's COGS (stop profit drift).

WHY
  A sale's COGS (cost of goods sold) is meant to be STAMPED on the transaction
  at sale time (cost_used_total / cost_unit / cost_source), so it's frozen — the
  cost of a sale is what it cost THEN and must never change later. The accounting
  reader (Accounting.cogs_for_sale) reads that stamp FIRST.

  But sales recorded while the product had NO resolvable cost yet (e.g. the recipe
  cost wasn't set at the moment of sale) were left UNSTAMPED. For those rows,
  cogs_for_sale falls through to the product's CURRENT weighted-average / catalog
  cost — which DRIFTS. So when a later PRODUCTION run restamps the product's
  landing_cost to a cheaper figure, every unstamped past sale's COGS silently
  drops and net profit JUMPS (the reported bug: net profit flips negative→positive
  after producing).

WHAT IT DOES  (option A — lock at the cost on record NOW)
  For each SALE with NO stamp (no cost_used_total) it resolves the COGS using the
  SAME engine the sale flow uses (Accounting.resolve_sale_cost_now) against the
  product's CURRENT cost, and writes it onto the row (cost_used_total / cost_unit
  / cost_source, plus cost_backfilled_at for an audit trail). From then on
  cogs_for_sale reads the stamp and the sale's COGS never drifts again.

  It does NOT touch sales that already carry a stamp. It does NOT change amounts,
  stock, cash, or debt — only the per-sale COGS stamp. Idempotent: a second run
  finds nothing to stamp.

USAGE
  # Dry-run a single account (reports, writes NOTHING)
  python scripts/backfill_sale_cogs.py --user tg:1072412276
  # Dry-run every account
  python scripts/backfill_sale_cogs.py --all --stage dev
  # Actually apply (NOTHING is written without --confirm)
  python scripts/backfill_sale_cogs.py --user tg:1072412276 --confirm
  python scripts/backfill_sale_cogs.py --all --confirm

SAFETY
  * DRY-RUN by default. Writes only when --confirm is given.
  * Stamps ONLY sales that have no stamp AND whose cost resolves to > 0 now.
  * A sale whose cost still can't be resolved is LEFT uncosted (honest), flagged.
  * Never rewrites an existing stamp (no clobber of a locked/corrected cost).
"""

import argparse
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))


def _has_stamp(tx):
    extra = tx.get("extra_details") or {}
    v = extra.get("cost_used_total")
    if v in (None, ""):
        v = tx.get("cost_used_total")
    try:
        return v not in (None, "") and float(v) > 0
    except (TypeError, ValueError):
        return False


def _is_sale(tx):
    return tx.get("type") == "sale"


def analyze_user(db, acct, user_id):
    """Classify every sale: already_stamped / to_stamp / unresolvable.
    Returns a report dict; pure analysis (no writes)."""
    from services.accounting import _is_debt_settlement

    txns = db.get_transactions(user_id, limit=100000) or []
    report = {"to_stamp": [], "already": 0, "unresolvable": [], "sales": 0}

    for t in txns:
        if not _is_sale(t):
            continue
        # Debt collection rows are recorded as 'sale' for cash but carry no COGS.
        if _is_debt_settlement(t):
            continue
        report["sales"] += 1
        if _has_stamp(t):
            report["already"] += 1
            continue
        # Resolve the cost the SAME way the sale flow would, against current cost.
        try:
            total, unit, source = acct.resolve_sale_cost_now(user_id, t)
        except Exception as e:
            total, unit, source = 0, 0, f"ERR:{e}"
        tx_id = str(t.get("transaction_id") or t.get("id") or "")
        entry = {
            "tx_id": tx_id,
            "desc": (t.get("item_name") or t.get("description") or "")[:40],
            "amount": int(float(t.get("amount", 0) or 0)),
            "qty": str(t.get("quantity", "") or ""),
            "total": int(total or 0),
            "unit": int(unit or 0),
            "source": str(source),
        }
        if total and total > 0 and tx_id:
            report["to_stamp"].append(entry)
        else:
            report["unresolvable"].append(entry)
    return report


def apply_user(db, report, user_id):
    """Write the stamp for every to_stamp sale. Returns count written."""
    from utils.money import money_round
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for e in report["to_stamp"]:
        stamp = {
            "cost_used_total": money_round(e["total"]),
            "cost_unit": money_round(e["unit"]),
            "cost_source": e["source"] if not e["source"].startswith("ERR") else "weighted_avg",
            "cost_backfilled_at": now,
        }
        try:
            db.update_transaction(user_id, e["tx_id"], stamp)
            written += 1
        except Exception as ex:
            print(f"    !! write failed for {e['tx_id']}: {ex}")
    return written


def _print_report(user_id, report, confirm):
    if report["sales"] == 0:
        return
    verb = "STAMPED" if confirm else "WOULD STAMP"
    print("\n" + "=" * 64)
    print(f"ACCOUNT {user_id}  (sales={report['sales']}, "
          f"already locked={report['already']})")
    print("=" * 64)
    for e in report["to_stamp"]:
        print(f"  {verb}: {e['tx_id'][:13]:<14} {e['desc'][:24]:<24} "
              f"amount {e['amount']:>9} -> COGS {e['total']:>9} "
              f"(unit {e['unit']}, {e['source']})")
    for e in report["unresolvable"]:
        print(f"  ⚠️  UNCOSTED (no cost resolvable): {e['tx_id'][:13]:<14} "
              f"{e['desc'][:24]:<24} amount {e['amount']:>9} — left as-is")
    print(f"\n  {('stamped' if confirm else 'to stamp')}: {len(report['to_stamp'])}  "
          f"uncosted (left alone): {len(report['unresolvable'])}  "
          f"already locked: {report['already']}")


def _scan_all_users(db):
    ids = []
    try:
        resp = db.users.scan(ProjectionExpression="phone_number")
        ids.extend(i.get("phone_number") for i in resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = db.users.scan(ProjectionExpression="phone_number",
                                 ExclusiveStartKey=resp["LastEvaluatedKey"])
            ids.extend(i.get("phone_number") for i in resp.get("Items", []))
    except Exception as e:
        print(f"⚠️  Could not scan users table: {e}")
    return [i for i in ids if i]


def main():
    ap = argparse.ArgumentParser(
        description="Backfill (lock) per-sale COGS so profit stops drifting "
                    "(dry-run unless --confirm).")
    ap.add_argument("--user", help="tg:<chat_id> or bare WhatsApp number")
    ap.add_argument("--all", action="store_true", help="Process every account")
    ap.add_argument("--stage", default="dev", help="DynamoDB stage (default: dev)")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually write the stamps (otherwise dry-run)")
    args = ap.parse_args()

    if not args.user and not args.all:
        ap.error("provide --user <id> or --all")

    from services.database import Database
    from services.accounting import Accounting
    db = Database(stage=args.stage)
    acct = Accounting(db)

    if not args.confirm:
        print("🔎 DRY-RUN — no changes will be written. Re-run with --confirm to apply.")

    targets = [args.user] if args.user else _scan_all_users(db)
    totals = {"stamped": 0, "uncosted": 0, "users": 0}
    for uid in targets:
        report = analyze_user(db, acct, uid)
        if report["sales"] == 0:
            continue
        if report["to_stamp"] or report["unresolvable"]:
            if args.confirm and report["to_stamp"]:
                apply_user(db, report, uid)
            _print_report(uid, report, args.confirm)
            totals["stamped"] += len(report["to_stamp"])
            totals["uncosted"] += len(report["unresolvable"])
            totals["users"] += 1

    print("\n" + "#" * 64)
    print(f"SUMMARY  users scanned: {len(targets)}  users touched: {totals['users']}")
    print(f"         sales {'stamped' if args.confirm else 'to stamp'}: {totals['stamped']}  "
          f"uncosted (left alone): {totals['uncosted']}")
    if not args.confirm and totals["stamped"]:
        print("         (dry-run) re-run with --confirm to lock these COGS.")
    print("#" * 64)


if __name__ == "__main__":
    main()
