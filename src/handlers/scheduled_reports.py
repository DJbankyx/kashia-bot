# src/handlers/scheduled_reports.py
"""Scheduled Reports Lambda - auto-sends daily/weekly summaries and debt notifications"""

import logging
from datetime import datetime, timedelta

from services.database import Database
from services.reports import ReportGenerator
from services.whatsapp_client import WhatsAppClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Triggered by EventBridge schedule:
    - Daily at 7PM UTC (8PM WAT): send daily summary + debt reminders
    - Sunday at 5PM UTC (6PM WAT): send weekly summary + full debt overview
    """
    try:
        resources = event.get('resources', [])
        is_weekly_trigger = 'WeeklyReport' in str(resources)
        is_sunday = datetime.now().weekday() == 6

        # If this is the DAILY trigger firing on Sunday, skip it
        # (the weekly trigger handles Sundays separately)
        if not is_weekly_trigger and is_sunday:
            logger.info("Daily trigger on Sunday — skipping (weekly handles it)")
            return {"status": "skipped", "reason": "Sunday handled by weekly trigger"}

        report_type = 'weekly' if is_weekly_trigger or is_sunday else 'daily'

        logger.info(f"Scheduled report triggered: {report_type}")

        db = Database()
        reports = ReportGenerator(database=db)
        whatsapp = WhatsAppClient()

        active_users = get_active_users(db)

        if not active_users:
            logger.info("No active users to notify.")
            return {"status": "ok", "users_notified": 0}

        success_count = 0
        error_count = 0

        for user in active_users:
            phone = user.get('phone_number', '')
            if not phone:
                continue

            try:
                messages = build_notifications(db, reports, phone, report_type, user)
                for msg in messages:
                    sent = whatsapp.send_text(phone, msg)
                    if sent:
                        success_count += 1
                    else:
                        error_count += 1

            except Exception as e:
                logger.error(f"Error sending notification to {phone}: {e}")
                error_count += 1
                continue

        logger.info(f"Notifications sent: {success_count} success, {error_count} errors")
        return {
            "status": "ok",
            "report_type": report_type,
            "users_notified": success_count,
            "errors": error_count
        }

    except Exception as e:
        logger.error(f"Scheduled reports Lambda error: {e}")
        return {"status": "error", "message": str(e)}


def build_notifications(db, reports, phone, report_type, user):
    """
    Build all notification messages for a user.
    Returns a list of message strings to send.
    """
    messages = []
    now = datetime.now()
    business_name = user.get('business_name', 'your business')

    # ============================================
    # 1. DAILY / WEEKLY TRANSACTION REPORT
    # ============================================
    try:
        if report_type == 'weekly':
            report_text = reports.generate_weekly(phone)
            if 'No transactions' not in report_text:
                messages.append(f"📊 *Weekly Summary — {business_name}*\n\n{report_text}\n\n_Sent by Kashia_")
        else:
            report_text = reports.generate_daily(phone)
            if 'No transactions' not in report_text:
                messages.append(f"📊 *End of Day Summary*\n\n{report_text}\n\n_Sent by Kashia_")
    except Exception as e:
        logger.error(f"Error generating report for {phone}: {e}")

    # ============================================
    # 2. DEBT REMINDERS
    # ============================================
    try:
        debt_msg = build_debt_notification(db, phone, report_type, now)
        if debt_msg:
            messages.append(debt_msg)
    except Exception as e:
        logger.error(f"Error building debt notification for {phone}: {e}")

    # ============================================
    # 3. OVERDUE DEBT ALERTS (daily only)
    # ============================================
    if report_type == 'daily':
        try:
            overdue_msg = build_overdue_alert(db, phone, now)
            if overdue_msg:
                messages.append(overdue_msg)
        except Exception as e:
            logger.error(f"Error building overdue alert for {phone}: {e}")

    # ============================================
    # 4. LOW ACTIVITY ALERT (daily only)
    # ============================================
    if report_type == 'daily':
        try:
            inactive_msg = build_inactivity_alert(db, phone, now)
            if inactive_msg:
                messages.append(inactive_msg)
        except Exception as e:
            logger.error(f"Error building inactivity alert for {phone}: {e}")

    return messages


def build_debt_notification(db, phone, report_type, now):
    """Build debt reminder message"""
    summary = db.get_debt_summary(phone)
    owed_to_me = summary.get('total_owed_to_me', 0)
    i_owe = summary.get('total_i_owe', 0)
    debtors = summary.get('debtors', [])
    creditors = summary.get('creditors', [])

    if owed_to_me == 0 and i_owe == 0:
        return None

    greeting = _time_greeting(now)

    if report_type == 'weekly':
        # Full weekly debt overview
        msg = f"{greeting} Here's your weekly debt overview:\n\n"
        if debtors:
            msg += f"💰 *People who owe you:*\n"
            for d in debtors[:5]:
                days_old = _days_since(d.get('last_date', ''))
                msg += f"  • {d['name']}: ₦{d['amount']:,}"
                if days_old:
                    msg += f" ({days_old} days)"
                msg += "\n"
            msg += f"  *Total: ₦{owed_to_me:,}*\n\n"

        if creditors:
            msg += f"🔴 *People you owe:*\n"
            for c in creditors[:5]:
                msg += f"  • {c['name']}: ₦{c['amount']:,}\n"
            msg += f"  *Total: ₦{i_owe:,}*\n\n"

        net = owed_to_me - i_owe
        if net > 0:
            msg += f"✅ Net position: +₦{net:,} in your favour"
        else:
            msg += f"⚠️ Net position: -₦{abs(net):,} (you owe more)"

        msg += "\n\n_Type \"who owes me\" to manage debts_"

    else:
        # Daily — just a brief reminder if there are debtors
        if not debtors:
            return None

        msg = f"{greeting}\n\n"
        msg += f"📋 *Quick Debt Reminder:*\n\n"

        for d in debtors[:3]:
            msg += f"  • {d['name']}: ₦{d['amount']:,}\n"

        if len(debtors) > 3:
            msg += f"  _...and {len(debtors) - 3} more_\n"

        msg += f"\n*Total owed to you: ₦{owed_to_me:,}*"
        if i_owe:
            msg += f"\n*You owe: ₦{i_owe:,}*"
        msg += "\n\n_Type \"who owes me\" to see full list_"

    return msg


def build_overdue_alert(db, phone, now):
    """Alert user about debts older than 14 days"""
    debtors = db.get_all_debtors(phone)
    overdue = []

    for d in debtors:
        days = _days_since(d.get('last_date', ''))
        if days and days >= 14:
            overdue.append({**d, 'days': days})

    if not overdue:
        return None

    overdue.sort(key=lambda x: x['days'], reverse=True)

    msg = "⚠️ *Overdue Debt Alert*\n\n"
    msg += "These debts are over 14 days old:\n\n"
    for d in overdue[:5]:
        msg += f"  • *{d['name']}* — ₦{d['amount']:,} ({d['days']} days old)\n"

    total = sum(d['amount'] for d in overdue)
    msg += f"\n*Total overdue: ₦{total:,}*"
    msg += "\n\n_Consider following up with these customers today._"

    return msg


def build_inactivity_alert(db, phone, now):
    """Alert user if they haven't recorded anything in 3 days"""
    three_days_ago = (now - timedelta(days=3)).strftime('%Y-%m-%d')
    today = now.strftime('%Y-%m-%d')

    recent = db.get_transactions_by_period(phone, three_days_ago, today)
    if recent:
        return None  # Active user, no alert needed

    # Check last transaction date
    all_tx = db.get_transactions(phone, limit=1)
    if not all_tx:
        return None  # New user, don't nag

    last_date = all_tx[0].get('date', '')
    days = _days_since(last_date)
    if not days or days < 3:
        return None

    msg = (
        f"👋 *Quick Check-in*\n\n"
        f"You haven't recorded any transactions in {days} days.\n\n"
        f"Don't forget to track your sales and expenses — it only takes a few seconds!\n\n"
        f"_Just type what happened e.g. \"sold shoes to Amaka for 15,000\"_"
    )
    return msg


# ============================================
# HELPERS
# ============================================

def get_active_users(db):
    """Get all users who have onboarded (active in last 30 days)"""
    try:
        response = db.users.scan(
            FilterExpression='attribute_exists(phone_number) AND onboarding_complete = :true',
            ExpressionAttributeValues={':true': True}
        )
        all_users = response.get('Items', [])

        # Filter to users active in last 30 days
        thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        today = datetime.now().strftime('%Y-%m-%d')

        active = []
        for user in all_users:
            phone = user.get('phone_number', '')
            # Include if they have transactions OR debts recorded
            transactions = db.get_transactions_by_period(phone, thirty_days_ago, today)
            debt_summary = db.get_debt_summary(phone)
            has_debts = debt_summary['total_owed_to_me'] > 0 or debt_summary['total_i_owe'] > 0

            if transactions or has_debts:
                active.append(user)

        logger.info(f"Found {len(active)} active users out of {len(all_users)} total")
        return active

    except Exception as e:
        logger.error(f"Error getting active users: {e}")
        return []


def _days_since(date_str):
    """Calculate days since a date string (YYYY-MM-DD)"""
    if not date_str:
        return None
    try:
        date = datetime.strptime(date_str, '%Y-%m-%d')
        return (datetime.now() - date).days
    except Exception:
        return None


def _time_greeting(now):
    """Return appropriate greeting based on time"""
    hour = now.hour
    if hour < 12:
        return "🌅 Good morning!"
    elif hour < 17:
        return "☀️ Good afternoon!"
    else:
        return "🌙 Good evening!"
