"""P1: outbox for Calendar/notify retries

Revision ID: 0002_p1_outbox
Revises: 0001_p0_slot_sot
Create Date: 2026-08-31
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002_p1_outbox"
down_revision: Union[str, Sequence[str], None] = "0001_p0_slot_sot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE outbox (
            id UUID PRIMARY KEY,
            kind VARCHAR(32) NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ix_outbox_pending ON outbox (status, next_attempt_at) "
        "WHERE status IN ('pending', 'processing')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS outbox")
