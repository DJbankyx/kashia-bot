# src/handlers/monthly_reset.py
"""Monthly Counter Reset Lambda — zeroes per-month usage counters for all users.

Triggered by EventBridge on the 1st of each month. Resets the counters that
tier limits are measured against (exports_this_month, invoices_this_month).
Transaction counts are computed live from dated records, so they self-reset
and are not touched here.
"""

import logging

from services.database import Database
from services.tier_manager import TierManager

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Reset monthly usage counters for every user. Idempotent — safe to re-run."""
    try:
        db = Database()
        tier = TierManager(database=db)

        reset_count = 0
        error_count = 0

        for phone in _iter_all_user_phones(db):
            try:
                tier.reset_monthly_counters(phone)
                reset_count += 1
            except Exception as e:
                logger.error(f"Error resetting counters for {phone}: {e}")
                error_count += 1
                continue

        logger.info(f"Monthly counter reset complete: {reset_count} users, {error_count} errors")
        return {"status": "ok", "users_reset": reset_count, "errors": error_count}

    except Exception as e:
        logger.error(f"Monthly reset Lambda error: {e}")
        return {"status": "error", "message": str(e)}


def _iter_all_user_phones(db):
    """Yield every user's phone_number, handling DynamoDB scan pagination.

    Projects only phone_number to keep the scan cheap.
    """
    try:
        kwargs = {
            "ProjectionExpression": "phone_number",
            "FilterExpression": "attribute_exists(phone_number)",
        }
        response = db.users.scan(**kwargs)
        for item in response.get("Items", []):
            phone = item.get("phone_number")
            if phone:
                yield phone

        while "LastEvaluatedKey" in response:
            response = db.users.scan(
                ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs
            )
            for item in response.get("Items", []):
                phone = item.get("phone_number")
                if phone:
                    yield phone
    except Exception as e:
        logger.error(f"Error scanning users for monthly reset: {e}")
        return
