#!/usr/bin/env python3
"""Kashia admin audit tool — inspect (and, guardedly, fix) a single account.

Read-only by default. Reuses the engine's own Database accessors, so it reads
exactly what the bot reads. Identity key = the `phone_number` partition key:
a bare number is a WhatsApp user; `tg:<chat_id>` is a Telegram user.

USAGE
  # Dump one account (read-only)
  python scripts/audit_user.py tg:1072412276
  python scripts/audit_user.py 2349016403500 --stage dev --limit 15

  # Find by business name / Telegram @username / display name
  python scripts/audit_user.py --name "Banky"

  # Guarded fixes (NOTHING happens without --confirm)
  python scripts/audit_user.py tg:1072412276 --edit reset-session --confirm
  python scripts/audit_user.py tg:1072412276 --edit set-field --field tier --value pro --confirm

SAFETY
  * Read-only unless --edit AND --confirm are BOTH given.
  * Secret-ish fields (pin/hash/token/secret/password) are shown as
    "<set>"/"<empty>" — their values are never printed.
  * Runs against --stage dev by default.
"""

import argparse
import json
import os
import sys

# Make the engine importable whether run from repo root or scripts/.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

SECRET_HINTS = ("pin", "hash", "token", "secret", "password", "api_key")


def _mask(key, value):
    """Never print secret values — show presence only."""
    if any(h in key.lower() for h in SECRET_HINTS):
        return "<set>" if value not in (None, "", 0) else "<empty>"
    return value


def _naira(v):
    try:
        return f"NGN {int(float(v)):,}"
    except (TypeError, ValueError):
        return str(v)


def _print_header(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def _find_by_name(db, needle):
    """Scan the users table for a business_name / tg_username / tg_name match.
    Admin-only, low-volume — a table Scan is acceptable here."""
    needle = needle.lower().strip()
    matches = []
    try:
        resp = db.users.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = db.users.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
    except Exception as e:
        print(f"⚠️  Could not scan users table: {e}")
        return []
    for u in items:
        hay = " ".join(str(u.get(k, "")) for k in
                       ("business_name", "tg_username", "tg_name")).lower()
        if needle in hay:
            matches.append(u)
    return matches


def dump_user(db, user_id, limit):
    user = db.get_user(user_id)
    if not user:
        print(f"❌ No user found for id: {user_id}")
        return

    platform = "Telegram" if str(user_id).startswith("tg:") else "WhatsApp"
    _print_header(f"ACCOUNT  {user_id}   ({platform})")
    for k in ("business_name", "tg_username", "tg_name", "business_type",
              "industry_class", "tier", "onboarding_complete", "created_at",
              "transaction_count", "business_address", "email", "tin",
              "show_tin_on_documents", "bank_name", "account_number",
              "logo_s3_key", "pin_hash"):
        if k in user:
            print(f"  {k:22s}: {_mask(k, user.get(k))}")

    # Transactions
    _print_header(f"LAST {limit} TRANSACTIONS")
    txns = db.get_transactions(user_id, limit=limit) or []
    if not txns:
        print("  (none)")
    for t in txns:
        print(f"  {t.get('date','?'):10s}  {t.get('type','?'):8s}  "
              f"{_naira(t.get('amount', 0)):>16s}  {t.get('category','')[:18]:18s}  "
              f"{(t.get('item_name') or t.get('description') or '')[:30]}")

    # Contacts
    _print_header("CONTACTS")
    contacts = db.get_contacts(user_id, limit=100) or []
    if not contacts:
        print("  (none)")
    for c in contacts:
        print(f"  {c.get('name','?')[:24]:24s}  type={c.get('contact_type','?'):9s}  "
              f"owes_me={_naira(c.get('debt_owed_to_me', 0))}  "
              f"i_owe={_naira(c.get('debt_i_owe', 0))}")

    # Debts (receivables / payables)
    _print_header("DEBTS")
    debtors = db.get_all_debtors(user_id) or []
    creditors = db.get_all_creditors(user_id) or []
    recv = sum(int(d.get("amount", 0)) for d in debtors)
    pay = sum(int(c.get("amount", 0)) for c in creditors)
    print(f"  Receivables (owed to user): {_naira(recv)} across {len(debtors)} debtor(s)")
    for d in debtors:
        print(f"    • {d.get('name','?')[:24]:24s} {_naira(d.get('amount',0))}")
    print(f"  Payables (user owes):        {_naira(pay)} across {len(creditors)} creditor(s)")
    for c in creditors:
        print(f"    • {c.get('name','?')[:24]:24s} {_naira(c.get('amount',0))}")

    # Session (why is someone stuck?)
    _print_header("SESSION")
    sess = db.get_session(user_id)
    if not sess:
        print("  (no active session — IDLE)")
    else:
        print(f"  state:   {sess.get('state','?')}")
        print(f"  version: {sess.get('version','?')}")
        ctx = sess.get("context", {}) or {}
        keys = list(ctx.keys())
        print(f"  context keys: {keys}")


def do_edit(db, user_id, action, field, value, confirm):
    if not confirm:
        print("🛑 Refusing to edit without --confirm. Nothing changed.")
        print(f"   Would run: --edit {action}"
              + (f" --field {field} --value {value}" if action == "set-field" else ""))
        return

    user = db.get_user(user_id)
    if not user:
        print(f"❌ No user found for id: {user_id} — nothing to edit.")
        return

    if action == "reset-session":
        db.clear_session(user_id)
        print(f"✅ Session reset to IDLE for {user_id}.")
        return

    if action == "set-field":
        if not field:
            print("❌ --field is required for set-field.")
            return
        if any(h in field.lower() for h in SECRET_HINTS):
            print(f"🛑 Refusing to set a secret-ish field ('{field}') from this tool.")
            return
        old = user.get(field)
        db.update_user_field(user_id, field, value)
        print(f"✅ {field}: {old!r} -> {value!r} for {user_id}.")
        return

    print(f"❌ Unknown --edit action: {action}")


def main():
    ap = argparse.ArgumentParser(description="Kashia admin audit tool (read-only by default).")
    ap.add_argument("user_id", nargs="?", help="tg:<chat_id> or bare WhatsApp number")
    ap.add_argument("--name", help="Search users by business name / @username / display name")
    ap.add_argument("--stage", default="dev", help="DynamoDB stage (default: dev)")
    ap.add_argument("--limit", type=int, default=10, help="How many recent transactions to show")
    ap.add_argument("--edit", choices=["reset-session", "set-field"],
                    help="Guarded fix (requires --confirm)")
    ap.add_argument("--field", help="Field name for --edit set-field")
    ap.add_argument("--value", help="New value for --edit set-field")
    ap.add_argument("--confirm", action="store_true", help="Actually apply an --edit")
    args = ap.parse_args()

    from services.database import Database
    db = Database(stage=args.stage)

    if args.name:
        matches = _find_by_name(db, args.name)
        if not matches:
            print(f"No accounts match '{args.name}'.")
            return
        print(f"Found {len(matches)} match(es) for '{args.name}':")
        for u in matches:
            print(f"  {u.get('phone_number','?'):18s}  {u.get('business_name','')[:28]:28s}  "
                  f"@{u.get('tg_username','')}  {u.get('tg_name','')}")
        print("\nRe-run with a specific id to dump the full account.")
        return

    if not args.user_id:
        ap.error("provide a user_id (tg:<chat_id> or bare number) or --name to search")

    if args.edit:
        do_edit(db, args.user_id, args.edit, args.field, args.value, args.confirm)
        print("\n--- account after edit ---")
        dump_user(db, args.user_id, args.limit)
    else:
        dump_user(db, args.user_id, args.limit)


if __name__ == "__main__":
    main()
