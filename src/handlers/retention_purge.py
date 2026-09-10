"""Retention Purge Lambda — TRUE-deletes archived records past their retention.

Triggered by EventBridge monthly. Complements the N1 soft-delete design: when
a user "clears their data", rows are ARCHIVED (deleted flag + retain_until),
filtered out of every read path, and kept for the retention window so the data
is recoverable for audit/legal purposes. THIS job is the final step — it
physically removes archived rows whose retain_until has passed.

⚠️ SAFETY — DRY-RUN BY DEFAULT:
  Retention purging is IRREVERSIBLE, and RETENTION_DAYS in services/database.py
  is still a LEGAL PLACEHOLDER (~7 years) pending confirmation of the real
  window for the jurisdiction(s) we operate in. So this handler runs in
  DRY-RUN mode unless the env var PURGE_DRY_RUN is explicitly set to a falsey
  value ("false"/"0"/"no"). In dry-run it only COUNTS and LOGS what would be
  deleted — it removes nothing.

  To enable real deletion (later, once the window is confirmed):
    set PURGE_DRY_RUN=false on the RetentionPurgeFunction.
"""

import logging
import os

from services.database import Database

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_FALSEY = {"false", "0", "no", "off"}


def _is_dry_run() -> bool:
    """Dry-run unless PURGE_DRY_RUN is explicitly falsey. Defaults to True."""
    val = str(os.environ.get("PURGE_DRY_RUN", "true")).strip().lower()
    return val not in _FALSEY


def lambda_handler(event, context):
    """Purge (or, in dry-run, count) expired archived rows in transactions +
    contacts. Idempotent and safe to re-run. Never raises out."""
    dry_run = _is_dry_run()
    result = {"status": "ok", "dry_run": dry_run, "transactions": 0, "contacts": 0}

    if dry_run:
        logger.warning(
            "Retention purge running in DRY-RUN mode — NOTHING will be deleted. "
            "RETENTION_DAYS is a placeholder pending legal confirmation. "
            "Set PURGE_DRY_RUN=false to enable real deletion once the window "
            "is confirmed."
        )

    try:
        db = Database()
        for table in ("transactions", "contacts"):
            try:
                count = db.purge_expired(table_name=table, dry_run=dry_run)
                result[table] = int(count or 0)
                verb = "would delete" if dry_run else "deleted"
                logger.info(f"Retention purge [{table}]: {verb} {result[table]} expired archived rows")
            except Exception as e:
                logger.error(f"Retention purge error on {table}: {e}")
                result["status"] = "partial_error"
                continue

        logger.info(
            f"Retention purge complete (dry_run={dry_run}): "
            f"transactions={result['transactions']}, contacts={result['contacts']}"
        )
        return result

    except Exception as e:
        logger.error(f"Retention purge Lambda error: {e}")
        return {"status": "error", "dry_run": dry_run, "message": str(e)}
