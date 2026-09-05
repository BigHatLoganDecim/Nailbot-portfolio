"""Sync SQLAlchemy engine for Postgres (slot source of truth).

Flask + telebot are sync. Spec wants async later; P0 does not rewrite the bot stack.
If DATABASE_URL uses postgresql+asyncpg:// it is rewritten to +psycopg.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger("nailbot.pg")

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def _clean(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('"').strip("'")


def database_url() -> str:
    return _clean(os.getenv("DATABASE_URL", ""))


def normalize_db_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url.split("://", 1)[1]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.split("://", 1)[1]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.split("://", 1)[1]
    return url


def hold_minutes() -> int:
    try:
        return max(1, min(30, int(_clean(os.getenv("HOLD_MINUTES", "5")) or "5")))
    except ValueError:
        return 5


def prepay_enabled() -> bool:
    return _clean(os.getenv("PREPAY", "false")).lower() in ("1", "true", "yes", "on")


def google_credentials_present() -> bool:
    return bool(_clean(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")))


def google_enabled() -> bool:
    # По умолчанию пишем в Calendar, если есть ключ. false — только явный запрет.
    raw = _clean(os.getenv("GOOGLE_ENABLED", "true")).lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def pg_enabled() -> bool:
    return bool(database_url())


def calendar_write_enabled() -> bool:
    """Always mirror bookings to Google when the service-account JSON is set."""
    return google_credentials_present()


def init_pg() -> bool:
    """Create engine if DATABASE_URL is set. Returns True when Postgres is the slot SoT."""
    global _engine, _Session
    url = database_url()
    if not url:
        log.warning(
            "DATABASE_URL не задан — слоты остаются на SQLite/Calendar. "
            "P0 SoT выключен."
        )
        return False
    url = normalize_db_url(url)
    _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    log.info(
        "Postgres slot SoT on | HOLD_MINUTES=%s PREPAY=%s GOOGLE_ENABLED=%s",
        hold_minutes(), prepay_enabled(), google_enabled(),
    )
    return True


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Postgres не инициализирован (нет DATABASE_URL)")
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _Session is None:
        raise RuntimeError("Postgres не инициализирован (нет DATABASE_URL)")
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping_pg() -> bool:
    if _engine is None:
        return False
    try:
        with _engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        log.warning("postgres ping failed: %s", e)
        return False
