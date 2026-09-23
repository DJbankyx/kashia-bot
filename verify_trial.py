#!/usr/bin/env python3
"""verify_trial.py — make the 14-day trial gate (Test C) verifiable WITHOUT
waiting 14 days.

This is a DEV/OPS tool, not wired into the bot. It reads a user's trial state
straight from the same engine the bot uses (TierManager), so what it prints is
exactly what a real record attempt would see.

Usage (from the project root, with the venv active):

  # 1) Read-only: show a user's current trial + subscription status.
  python verify_trial.py tg:1072412276

  # 2) Simulate the trial having started N days ago, to SEE the post-trial
  #    behaviour (soft monthly cap kicks in; history/reports stay open).
  #    It backdates created_at, prints the result, then RESTORES the original
  #    value — a dry run. Add --commit to leave the backdated value in place.
  python verify_trial.py tg:1072412276 --simulate-days 20
  python verify_trial.py tg:1072412276 --simulate-days 20 --commit

Notes:
  * "phone_number" is the namespaced user id: bare digits for WhatsApp,
    "tg:<chat_id>" for Telegram (copy it from the Paystack receipt / logs).
  * --simulate-days only touches created_at. It never charges, never upgrades,
    and (without --commit) restores the original created_at before exiting.
"""

import sys
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, "src")


def _fmt(v):
    return "—" if v in (None, "") else str(v)


def show(tier_mgr, db, phone):
    user = db.get_user(phone) or {}
    if not user:
        print(f"  (no such user: {phone})")
        return None

    created = user.get("created_at")
    trial_days, trial_left = tier_mgr._trial_status(phone)
    tier = tier_mgr.get_user_tier(phone)
    limits = tier_mgr.get_tier_limits(tier)
    cap = limits.get("transactions_per_month")
    used = db.count_transactions_this_month(phone)
    allowed, msg = tier_mgr.check_can_record(phone)
    sub = tier_mgr.subscription_status(phone)

    in_trial = (trial_left is not None and trial_left > 0)
    print(f"  user                : {phone}")
    print(f"  tier                : {tier}")
    print(f"  created_at          : {_fmt(created)}")
    print(f"  trial length (days) : {trial_days}")
    print(f"  trial days left     : {_fmt(trial_left)}"
          + ("  (in trial)" if in_trial else "  (trial over)" if trial_left == 0 else ""))
    print(f"  monthly cap         : {cap}")
    print(f"  used this month     : {used}")
    print(f"  can record now?     : {allowed}")
    print(f"  record message      : {_fmt(msg)}")
    print(f"  subscription state  : {sub.get('state')} "
          f"(ends {_fmt(sub.get('ends'))}, days_left {_fmt(sub.get('days_left'))})")
    print(f"  history/reports     : always open (never gated by the cap)")
    return created


def main():
    ap = argparse.ArgumentParser(description="Verify the 14-day trial gate for a user.")
    ap.add_argument("phone", help="namespaced user id, e.g. tg:1072412276 or 2348012345678")
    ap.add_argument("--simulate-days", type=int, default=None,
                    help="pretend the account was created N days ago (dry run unless --commit)")
    ap.add_argument("--commit", action="store_true",
                    help="with --simulate-days, PERSIST the backdated created_at (not a dry run)")
    args = ap.parse_args()

    from services.database import Database
    from services.tier_manager import TierManager

    db = Database()
    tier_mgr = TierManager(database=db)
    phone = args.phone

    print("\n=== CURRENT STATE ===")
    original_created = show(tier_mgr, db, phone)
    if original_created is None:
        sys.exit(1)

    if args.simulate_days is None:
        print("\n(no simulation requested — read-only)\n")
        return

    n = args.simulate_days
    backdated = (datetime.now() - timedelta(days=n)).isoformat()
    print(f"\n=== SIMULATE: created_at = {n} days ago ({backdated[:10]}) ===")
    if not args.commit:
        print("  (dry run — created_at will be RESTORED before exit)")

    db.update_user(phone, {"created_at": backdated})
    try:
        show(tier_mgr, db, phone)
    finally:
        if not args.commit:
            # Restore the real created_at so this stays a non-destructive dry run.
            db.update_user(phone, {"created_at": original_created})
            print(f"\n  restored created_at -> {_fmt(original_created)}")
        else:
            print(f"\n  COMMITTED: created_at now {backdated[:10]} "
                  f"(run again with --simulate-days 0 --commit to reset, or set a real date)")
    print()


if __name__ == "__main__":
    main()
