"""
SQLite connection management and schema initialization.

Designed for long-term reliability and analytics.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger("nailbot.database")


DEFAULT_DB_PATH = "data/nailbot.db"


def _get_db_path() -> str:
    """Resolve database path from environment or default."""
    env_path = os.getenv("DATABASE_PATH", "").strip()
    if env_path:
        return env_path

    # Default: data/nailbot.db relative to project root
    project_root = Path(__file__).resolve().parents[1]
    db_dir = project_root / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "nailbot.db")


def get_db_path() -> str:
    """Public version of _get_db_path for logging and external use."""
    return _get_db_path()


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """
    Context manager that yields a SQLite connection.

    - WAL mode enabled for better concurrency.
    - Foreign keys enabled.
    - Reasonable timeout.
    """
    db_path = _get_db_path()
    conn = sqlite3.connect(
        db_path,
        timeout=30,
        isolation_level=None,  # autocommit mode for simplicity + safety
    )
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """
    Initialize database schema if needed.
    Idempotent — safe to call on every startup.
    """
    from .schema import create_all_tables

    with get_connection() as conn:
        create_all_tables(conn)
        log.info("Database initialized: %s", _get_db_path())


def close_connection() -> None:
    """Placeholder for future connection pool if needed."""
    pass
