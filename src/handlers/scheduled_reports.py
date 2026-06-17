# src/handlers/scheduled_reports.py
"""Scheduled Reports Lambda - auto-sends daily/weekly summaries to active users"""

import logging
from datetime import datetime, timedelta

from src.services.database import Database
from src.services.reports import ReportGenerator
from src.services.whatsapp_client import WhatsAppClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Triggered by EventBridge schedule:
    - Daily at 7PM UTC (8PM WAT): send daily summary
    - Sunday at 5PM UTC (6PM WAT): send weekly summary

    The 'event' contains info about which schedule triggered it.
    """
    try:
        # Determine which schedule triggered this
        source = event.get('source', '')
        detail_type = event.get('detail-type', '')
        resources = event.get('resources', [])

        # Check if it's weekly (Sunday) or daily
        is_weekly = 'WeeklyReport' in str(resources) or datetime.now().weekday() == 6
        report_type = 'weekly' if is_weekly else 'daily'

        logger.info(f"Scheduled report triggered: {report_type}")

        # Initialize services
        db = Database()
        reports = ReportGenerator(database=db)
        whatsapp = WhatsAppClient()

        # Get all active users (those who transacted in last 7 days)
        active_users = get_active_users(db)

        if not active_users:
            logger.info("No active users to send reports to.")
            return {"status": "ok", "users_notified": 0}

        # Send reports
        success_count = 0
        error_count = 0

        for user in active_users:
            phone = user.get('phone_number', '')
            if not phone:
                continue

            try:
                # Generate report
                if report_type == 'weekly':
                    report_text = reports.generate_weekly(phone)
                else:
                    report_text = reports.generate_daily(phone)

                # Skip if no transactions in period
                if 'No transactions' in report_text:
                    continue

                # Add header
                if report_type == 'weekly':
                    header = "📊 *Your Weekly Summary*\n\n"
                else:
                    header = "📊 *End of Day Summary*\n\n"

                full_message = header + report_text + "\n\n_Sent by Kashia_"

                # Send via WhatsApp
                sent = whatsapp.send_text(phone, full_message)

                if sent:
                    success_count += 1
                else:
                    error_count += 1

            except Exception as e:
                logger.error(f"Error sending report to {phone}: {e}")
                error_count += 1
                continue  # Don't let one user's error stop others

        logger.info(f"Reports sent: {success_count} success, {error_count} errors")

        return {
            "status": "ok",
            "report_type": report_type,
            "users_notified": success_count,
            "errors": error_count
        }

    except Exception as e:
        logger.error(f"Scheduled reports Lambda error: {e}")
        return {"status": "error", "message": str(e)}


def get_active_users(db):
    """
    Get users who have recorded at least 1 transaction in the last 7 days.
    
    Note: In production with many users, you'd use a GSI or 
    a separate 'active_users' tracking mechanism. For MVP (<1000 users),
    scanning the users table is acceptable.
    """
    try:
        # Scan users table (fine for MVP scale)
        response = db.users.scan(
            FilterExpression='attribute_exists(phone_number)'
        )
        all_users = response.get('Items', [])

        # Filter to active users (had transactions in last 7 days)
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        today = datetime.now().strftime('%Y-%m-%d')

        active_users = []
        for user in all_users:
            phone = user.get('phone_number', '')
            transactions = db.get_transactions_by_period(phone, seven_days_ago, today)
            if transactions:
                active_users.append(user)

        logger.info(f"Found {len(active_users)} active users out of {len(all_users)} total")
        return active_users

    except Exception as e:
        logger.error(f"Error getting active users: {e}")
        return []
