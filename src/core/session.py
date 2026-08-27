# src/core/session.py
"""Session management — thin wrapper around database session ops."""

import logging
from core.states import IDLE

logger = logging.getLogger(__name__)


class SessionManager:
    """Get, save, and reset conversation state."""

    def __init__(self, database):
        self.db = database

    def get(self, phone_number: str) -> dict:
        """Get current session or return fresh IDLE session."""
        session = self.db.get_session(phone_number)
        if not session:
            return {"state": IDLE, "context": {}}
        return session

    def save(self, phone_number: str, state: str, context: dict = None):
        """Save state + context to DynamoDB."""
        self.db.save_session(phone_number, state, context or {})

    def reset(self, phone_number: str):
        """Reset to IDLE with empty context."""
        self.save(phone_number, IDLE, {})

    def get_state(self, phone_number: str) -> str:
        """Quick getter for just the state string."""
        session = self.get(phone_number)
        return session.get("state", IDLE)

    def get_context(self, phone_number: str) -> dict:
        """Quick getter for just the context dict."""
        session = self.get(phone_number)
        return session.get("context", {})

    def update_context(self, phone_number: str, updates: dict, _max_retries: int = 3):
        """
        Merge updates into existing context without changing state.

        Uses optimistic locking: re-reads and retries on a version conflict so
        two concurrent messages can't clobber each other's context (lost-update
        race). Falls back to a plain write if the DB layer doesn't support
        conditional writes.
        """
        for _ in range(_max_retries):
            session = self.get(phone_number)
            state = session.get("state", IDLE)
            context = dict(session.get("context", {}))
            context.update(updates)
            version = session.get("version")

            # If we have a version, do a conditional write and retry on conflict.
            if version is not None and hasattr(self.db, "save_session"):
                ok = self.db.save_session(phone_number, state, context,
                                          expected_version=version)
                if ok:
                    return
                # Conflict — another writer won; loop to re-read and re-merge.
                continue

            # No version available (fresh/legacy session) — best-effort write.
            self.save(phone_number, state, context)
            return

        # Exhausted retries — do a final unconditional write so the update isn't lost.
        logger.warning(f"update_context: version conflicts persisted for {phone_number}; "
                       f"writing unconditionally")
        session = self.get(phone_number)
        state = session.get("state", IDLE)
        context = dict(session.get("context", {}))
        context.update(updates)
        self.save(phone_number, state, context)
