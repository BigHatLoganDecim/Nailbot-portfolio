"""
Database layer for long-term storage (clients, bookings, reviews).

This module is designed for high-quality data retention and future AI analysis.

Current strategy:
- SQLite for durable, queryable long-term data.
- Redis (via DialogStateStore) remains only for short-lived dialog state.

This separation allows:
- Reliable history for years.
- Proper SQL queries for analytics and weekly AI reports.
- Easier future migration to Postgres if needed.
"""

from __future__ import annotations

from .connection import get_connection, get_db_path, init_db, close_connection
from .dialog_state import DialogStateStore
from .repositories import BookingsRepository, ClientsRepository, Database

__all__ = [
    "get_connection",
    "get_db_path",
    "init_db",
    "close_connection",
    "Database",
    "DialogStateStore",
    "BookingsRepository",
    "ClientsRepository",
]
