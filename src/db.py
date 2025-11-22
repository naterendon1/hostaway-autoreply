# file: src/db.py
"""
Database Module for Hostaway AutoReply
--------------------------------------
Handles:
- Duplicate detection for webhook events
- Logging AI exchanges for learning/debugging
- Simple in-memory storage (can be upgraded to SQLite/PostgreSQL later)
"""

import os
import logging
from typing import Optional
from datetime import datetime, timedelta

# In-memory storage for processed events (consider using Redis or SQLite for production)
_processed_events = set()
_ai_exchanges = []

# Configuration
MAX_PROCESSED_EVENTS = 10000  # Prevent memory overflow
EVENT_TTL_HOURS = 24  # How long to keep processed event IDs


def already_processed(event_key: str) -> bool:
    """
    Check if an event has already been processed.

    Args:
        event_key: Unique identifier for the event (e.g., "message_id:conversation_id")

    Returns:
        True if already processed, False otherwise
    """
    # Clean up old entries if set is getting too large
    if len(_processed_events) > MAX_PROCESSED_EVENTS:
        _processed_events.clear()
        logging.warning("[db] Cleared processed events cache (exceeded max size)")

    return event_key in _processed_events


def mark_processed(event_key: str) -> None:
    """
    Mark an event as processed to prevent duplicate handling.

    Args:
        event_key: Unique identifier for the event
    """
    _processed_events.add(event_key)
    logging.debug(f"[db] Marked event as processed: {event_key}")


def log_ai_exchange(
    conversation_id: str,
    guest_message: str,
    ai_suggestion: str,
    intent: str = "general",
    metadata: Optional[dict] = None
) -> None:
    """
    Log an AI exchange for learning and debugging purposes.

    Args:
        conversation_id: Hostaway conversation ID
        guest_message: The guest's message
        ai_suggestion: AI's suggested reply
        intent: Detected intent (e.g., "general", "checkin", "checkout")
        metadata: Additional context (guest name, property, etc.)
    """
    exchange = {
        "timestamp": datetime.utcnow().isoformat(),
        "conversation_id": conversation_id,
        "guest_message": guest_message,
        "ai_suggestion": ai_suggestion,
        "intent": intent,
        "metadata": metadata or {}
    }

    _ai_exchanges.append(exchange)

    # Keep only recent exchanges to prevent memory overflow
    if len(_ai_exchanges) > 1000:
        _ai_exchanges.pop(0)

    logging.info(f"[db] Logged AI exchange for conversation {conversation_id}")


def get_recent_exchanges(limit: int = 10) -> list:
    """
    Retrieve recent AI exchanges for debugging/analysis.

    Args:
        limit: Maximum number of exchanges to return

    Returns:
        List of recent AI exchange records
    """
    return _ai_exchanges[-limit:]


def clear_old_processed_events() -> None:
    """
    Clear all processed events (useful for testing or scheduled cleanup).
    In production, you'd implement TTL-based cleanup with timestamps.
    """
    _processed_events.clear()
    logging.info("[db] Cleared all processed events")


# TODO: Implement persistent storage with SQLite or PostgreSQL
# Example SQLite schema:
# CREATE TABLE processed_events (
#     event_key TEXT PRIMARY KEY,
#     processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# );
#
# CREATE TABLE ai_exchanges (
#     id INTEGER PRIMARY KEY AUTOINCREMENT,
#     conversation_id TEXT,
#     guest_message TEXT,
#     ai_suggestion TEXT,
#     intent TEXT,
#     metadata TEXT,
#     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# );
