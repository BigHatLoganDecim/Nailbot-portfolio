"""P0: masters/services/work_intervals/appointments/audit_log + unique/exclude

Revision ID: 0001_p0_slot_sot
Revises:
Create Date: 2026-08-31
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001_p0_slot_sot"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("""
        CREATE TABLE masters (
            id UUID PRIMARY KEY,
            tg_user_id BIGINT UNIQUE,
            name TEXT NOT NULL UNIQUE,
            timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
            google_calendar_id TEXT,
            google_watch_channel_id TEXT,
            google_watch_resource_id TEXT,
            google_watch_expiration TIMESTAMPTZ,
            google_sync_token TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)
    op.execute("""
        CREATE TABLE services (
            id UUID PRIMARY KEY,
            master_id UUID NOT NULL REFERENCES masters(id),
            title TEXT NOT NULL,
            duration_min INTEGER NOT NULL,
            price_minor INTEGER NOT NULL DEFAULT 0,
            buffer_min INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    op.execute("""
        CREATE TABLE work_intervals (
            id UUID PRIMARY KEY,
            master_id UUID NOT NULL REFERENCES masters(id),
            weekday SMALLINT,
            on_date DATE,
            start_local TIME NOT NULL,
            end_local TIME NOT NULL,
            kind VARCHAR(16) NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE appointments (
            id UUID PRIMARY KEY,
            master_id UUID NOT NULL REFERENCES masters(id),
            client_tg_id BIGINT NOT NULL,
            client_name TEXT NOT NULL DEFAULT '',
            client_phone TEXT,
            service_id UUID REFERENCES services(id),
            services_json JSONB,
            start_at TIMESTAMPTZ NOT NULL,
            end_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'held',
            hold_until TIMESTAMPTZ,
            payment_status VARCHAR(32) NOT NULL DEFAULT 'none',
            payment_id TEXT UNIQUE,
            google_event_id TEXT,
            source VARCHAR(32) NOT NULL DEFAULT 'bot',
            cancel_reason TEXT,
            supersedes_id UUID REFERENCES appointments(id),
            sqlite_booking_id TEXT,
            duration_min INTEGER NOT NULL DEFAULT 60,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE audit_log (
            id UUID PRIMARY KEY,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            appointment_id UUID,
            payload JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_active_slot
        ON appointments (master_id, start_at)
        WHERE status IN ('held', 'pending_payment', 'confirmed')
    """)
    op.execute("""
        ALTER TABLE appointments
        ADD CONSTRAINT ux_active_overlap
        EXCLUDE USING gist (
            master_id WITH =,
            tstzrange(start_at, end_at, '[)') WITH &&
        )
        WHERE (status IN ('held', 'pending_payment', 'confirmed'))
    """)
    op.execute("CREATE INDEX ix_appointments_master_start ON appointments (master_id, start_at)")
    op.execute("CREATE INDEX ix_appointments_client ON appointments (client_tg_id)")
    op.execute("CREATE INDEX ix_work_intervals_master ON work_intervals (master_id, on_date)")


def downgrade() -> None:
    op.execute("ALTER TABLE appointments DROP CONSTRAINT IF EXISTS ux_active_overlap")
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TABLE IF EXISTS appointments")
    op.execute("DROP TABLE IF EXISTS work_intervals")
    op.execute("DROP TABLE IF EXISTS services")
    op.execute("DROP TABLE IF EXISTS masters")
