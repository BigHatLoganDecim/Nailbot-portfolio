"""P0 Postgres schema: slot truth lives here, not in SQLite or Google."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


ACTIVE_STATUSES = ("held", "pending_payment", "confirmed")

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "held": frozenset({"pending_payment", "confirmed", "expired", "cancelled"}),
    "pending_payment": frozenset({"confirmed", "expired", "cancelled"}),
    "confirmed": frozenset({"completed", "cancelled", "no_show", "superseded"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
    "no_show": frozenset({"completed"}),
    "superseded": frozenset(),
}

PAYMENT_STATUSES = ("none", "pending", "paid", "refund_pending", "refunded", "failed")
SOURCES = ("bot", "admin", "google_block")
INTERVAL_KINDS = ("work", "break", "time_off")


class Base(DeclarativeBase):
    pass


class Master(Base):
    __tablename__ = "masters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tg_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Europe/Moscow")
    google_calendar_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    google_watch_channel_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    google_watch_resource_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    google_watch_expiration: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    google_sync_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    services: Mapped[list["Service"]] = relationship(back_populates="master")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="master")


class Service(Base):
    __tablename__ = "services"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    master_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("masters.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buffer_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    master: Mapped[Master] = relationship(back_populates="services")


class WorkInterval(Base):
    __tablename__ = "work_intervals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    master_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("masters.id"), nullable=False)
    weekday: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    on_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    start_local: Mapped[time] = mapped_column(Time, nullable=False)
    end_local: Mapped[time] = mapped_column(Time, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    master_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("masters.id"), nullable=False)
    client_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    client_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    client_phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    service_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id"), nullable=True)
    services_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="held")
    hold_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    payment_id: Mapped[Optional[str]] = mapped_column(Text, unique=True, nullable=True)
    google_event_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="bot")
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    supersedes_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("appointments.id"), nullable=True
    )
    sqlite_booking_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    master: Mapped[Master] = relationship(back_populates="appointments")


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    appointment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
