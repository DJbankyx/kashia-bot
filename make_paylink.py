"""make_paylink.py — DEV/OPS tool to create a Paystack payment-collection link
WITHOUT the Mini App UI (Phase 4 not built yet), so Phase 2 (the webhook that
auto-marks a collection paid) can be tested end-to-end with a real TEST-mode
payment.

It uses the SAME engine the bot will use:
  PaystackService.initialize_collection  → makes the pay-link
  db.create_payment_request              → persists the PaymentRequest

Usage (from project root, venv active, AWS creds for eu-west-1):

  # Create a ₦2,500 pay-link for "Sandra" against owner tg:1072412276
  python make_paylink.py tg:1072412276 2500 "Invoice BFH-00012" --customer Sandra

  # List an owner's payment requests + their status
  python make_paylink.py tg:1072412276 --list

Then:
  1) Open the printed payment_url in a browser.
  2) Pay with a Paystack TEST card: 4084 0840 8408 4081, any future expiry,
     CVV 408, PIN 0000, OTP 123456. (TEST-mode secret required in SSM.)
  3) Within seconds the OWNER should get a "💰 Payment received!" message,
     a paid SALE appears in Records, and — if the customer had an open
     receivable — it's settled. Re-run --list to see status flip to 'paid'.

Notes:
  * owner_id is the namespaced id: bare digits (WhatsApp) or "tg:<chat_id>".
  * The webhook must be reachable + registered in the Paystack dashboard (it
    already is for subscriptions — same URL). Requires the ContactsTable-CRUD
    deploy (Phase 2) or the paid path will AccessDeny.
  * This tool does NOT mark anything paid — only the live webhook does that,
    which is exactly what we're testing.
"""

import sys
import uuid
import argparse

sys.path.insert(0, "src")


def main():
    ap = argparse.ArgumentParser(description="Create a Paystack collection pay-link (dev tool).")
    ap.add_argument("owner_id", help='Owner namespaced id, e.g. "tg:1072412276" or bare digits.')
    ap.add_argument("amount", nargs="?", type=float, help="Amount in NAIRA, e.g. 2500")
    ap.add_argument("description", nargs="?", default="Payment", help="What it's for.")
    ap.add_argument("--customer", default="", help="Customer name (for settle + notify).")
    ap.add_argument("--list", action="store_true", help="List this owner's payment requests + status.")
    args = ap.parse_args()

    from services.database import Database
    from services.paystack import PaystackService

    db = Database()

    if args.list:
        reqs = db.list_payment_requests(args.owner_id)
        if not reqs:
            print("No payment requests for", args.owner_id)
            return
        print(f"{'STATUS':9} {'AMOUNT':>10}  {'CREATED':19}  DESCRIPTION / REF")
        for r in reqs:
            print(f"{str(r.get('status','?')):9} "
                  f"{float(r.get('amount',0)):>10,.0f}  "
                  f"{str(r.get('created_at',''))[:19]:19}  "
                  f"{str(r.get('description',''))[:30]:30}  {r.get('paystack_ref','')}")
        return

    if not args.amount or args.amount <= 0:
        ap.error("amount (naira) is required to create a pay-link")

    payreq_id = "payreq#" + uuid.uuid4().hex[:12]
    svc = PaystackService()
    res = svc.initialize_collection(
        args.owner_id, args.amount, args.description, payreq_id,
        customer_name=args.customer or None)

    if not res.get("success"):
        print("❌ Could not create pay-link:", res.get("error"))
        sys.exit(1)

    ref = res["reference"]
    url = res["payment_url"]
    stored = db.create_payment_request(
        args.owner_id, args.amount, args.description, payreq_id, ref,
        customer_name=args.customer or None, payment_url=url)
    if not stored:
        print("⚠️  Pay-link created but the PaymentRequest didn't persist — the "
              "webhook won't find it. Check DynamoDB perms/logs.")

    print("✅ Pay-link created.")
    print("   owner    :", args.owner_id)
    print(f"   amount   : NGN {args.amount:,.0f}")
    print("   customer :", args.customer or "(none)")
    print("   payreq_id:", payreq_id)
    print("   reference:", ref)
    print("   PAY HERE :", url)
    print("\nOpen PAY HERE, pay with the TEST card, then run:")
    print(f'   python make_paylink.py {args.owner_id} --list')


if __name__ == "__main__":
    main()
