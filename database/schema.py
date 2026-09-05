"""
Database schema definitions.

This schema is designed for:
- Long-term storage of booking history and client data
- High-quality analytics and AI reporting
- Easy querying and future extensibility

Version: 1 (initial)
"""

from __future__ import annotations

import logging
import sqlite3


SCHEMA_VERSION = 2


def create_all_tables(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not exist. Idempotent."""

    # Clients table — core customer profile + analytics fields
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            user_id           INTEGER PRIMARY KEY,
            telegram_chat_id  INTEGER,
            name              TEXT DEFAULT '',
            notes             TEXT DEFAULT '',
            birthday          TEXT DEFAULT '',
            tags              TEXT DEFAULT '[]',           -- JSON array
            total_visits      INTEGER DEFAULT 0,
            referred_by       INTEGER,
            referral_used     INTEGER DEFAULT 0,
            referrals_brought INTEGER DEFAULT 0,
            design_bonuses    INTEGER DEFAULT 0,
            first_seen_at     TEXT,
            last_seen_at      TEXT,
            created_at        TEXT DEFAULT (datetime('now')),
            updated_at        TEXT DEFAULT (datetime('now'))
        )
    """)

    # Bookings table — the most important table for analysis
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id                  TEXT PRIMARY KEY,
            user_id             INTEGER NOT NULL,
            master              TEXT NOT NULL,
            services            TEXT NOT NULL,             -- JSON array of service names
            services_raw        TEXT DEFAULT '',
            datetime_text       TEXT,
            datetime_iso        TEXT,
            duration_min        INTEGER DEFAULT 60,
            phone               TEXT,
            chat_id             INTEGER,
            status              TEXT DEFAULT 'confirmed',  -- confirmed, cancelled, rescheduled, completed, no_show
            -- 
            -- Status meanings and effects on counters:
            --   confirmed    : Active future booking. Counts toward total_visits when finalized.
            --   completed    : Visit happened. Counts toward total_visits.
            --   cancelled    : Client or system cancelled. Does NOT count toward total_visits.
            --                  Design bonus used on this booking is usually restored on client cancel.
            --   rescheduled  : Client moved the booking to another time. Does NOT count as lost visit.
            --                  Design bonus is NOT restored (bonus was for completing a visit).
            --   no_show      : Client did not show up. Counts or not depending on business rules (currently treated as non-counting for visits in most reports).
            calendar_event_id   TEXT,
            promo_code          TEXT,
            notes               TEXT DEFAULT '[]',         -- JSON array of notes
            rating              INTEGER,                   -- 1-5, filled after visit
            review_text         TEXT,
            reminder_job_ids    TEXT DEFAULT '[]',         -- JSON
            payment_status      TEXT DEFAULT 'unpaid',     -- unpaid, cash, card
            payment_amount      INTEGER,                   -- фактическая сумма оплаты в рублях
            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now')),

            FOREIGN KEY (user_id) REFERENCES clients(user_id) ON DELETE CASCADE
        )
    """)

    # Useful indexes for analytics and common queries
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user_id ON bookings(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_master ON bookings(master)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_datetime ON bookings(datetime_iso)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_rating ON bookings(rating)")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_referred_by ON clients(referred_by)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_last_seen ON clients(last_seen_at)")

    # Simple schema version tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),)
    )

    # Миграции для уже существующих БД (CREATE TABLE IF NOT EXISTS не добавляет колонки)
    _migrate_schema(conn)

    conn.commit()
    log_schema_info(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Идемпотентные миграции: добавляет недостающие колонки в существующие таблицы.
    SQLite ALTER TABLE ADD COLUMN безопасен и не трогает существующие данные."""
    log = logging.getLogger("nailbot.database")

    # bookings: payment_status, payment_amount (v2)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(bookings)").fetchall()}
    migrations = {
        "payment_status": "ALTER TABLE bookings ADD COLUMN payment_status TEXT DEFAULT 'unpaid'",
        "payment_amount": "ALTER TABLE bookings ADD COLUMN payment_amount INTEGER",
    }
    for col, ddl in migrations.items():
        if col not in existing:
            try:
                conn.execute(ddl)
                log.info("Миграция: добавлена колонка bookings.%s", col)
            except Exception as e:
                log.warning("Миграция bookings.%s не удалась: %s", col, e)

    # Обновляем версию схемы
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),)
    )


def log_schema_info(conn: sqlite3.Connection) -> None:
    """Log current schema version and table counts (for observability)."""
    cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'")
    row = cur.fetchone()
    version = row[0] if row else "unknown"

    cur = conn.execute("SELECT COUNT(*) FROM clients")
    clients_count = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM bookings")
    bookings_count = cur.fetchone()[0]

    logging.getLogger("nailbot.database").info(
        "Schema v%s | clients=%d, bookings=%d",
        version, clients_count, bookings_count
    )
