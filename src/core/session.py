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

    def update_context(self, phone_number: str, updates: dict):
        """Merge updates into existing context without changing state."""
        session = self.get(phone_number)
        state = session.get("state", IDLE)
        context = session.get("context", {})
        context.update(updates)
        self.save(phone_number, state, context)
