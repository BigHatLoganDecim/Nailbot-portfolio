"""Appointment SoT: hold, unique/exclude, transition, occupancy, seed.

Google Calendar is never consulted here.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .models import (
    ACTIVE_STATUSES,
    ALLOWED_TRANSITIONS,
    Appointment,
    AuditLog,
    Master,
    Outbox,
    Service,
    WorkInterval,
)
from .pg import hold_minutes, pg_enabled, prepay_enabled, session_scope

log = logging.getLogger("nailbot.appointments")


class SlotConflict(Exception):
    """Active unique/exclude rejected the insert."""


class IllegalTransition(Exception):
    def __init__(self, old: str, new: str) -> None:
        super().__init__(f"illegal transition {old} → {new}")
        self.old = old
        self.new = new


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _audit(session, actor: str, action: str, appointment_id: uuid.UUID | None, payload: dict | None) -> None:
    session.add(AuditLog(
        actor=actor,
        action=action,
        appointment_id=appointment_id,
        payload=payload or {},
    ))


def _transition_unlocked(session, appt: Appointment, new_status: str, actor: str, **extra: Any) -> Appointment:
    old = appt.status
    allowed = ALLOWED_TRANSITIONS.get(old, frozenset())
    if new_status != old and new_status not in allowed:
        raise IllegalTransition(old, new_status)
    appt.status = new_status
    if new_status not in ACTIVE_STATUSES:
        appt.hold_until = None
    if new_status == "confirmed":
        appt.hold_until = None
    if extra.get("cancel_reason"):
        appt.cancel_reason = extra["cancel_reason"]
    if extra.get("google_event_id") is not None:
        appt.google_event_id = extra["google_event_id"]
    appt.updated_at = _now()
    if old != new_status:
        _enqueue_status_side_effects(session, appt, old, new_status)
    _audit(session, actor, f"transition:{old}->{new_status}", appt.id, {
        "old": old,
        "new": new_status,
        **{k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
           for k, v in extra.items()},
    })
    return appt


def expire_stale_holds() -> int:
    if not pg_enabled():
        return 0
    n = 0
    with session_scope() as session:
        now = _now()
        rows = session.scalars(
            select(Appointment).where(
                Appointment.status.in_(("held", "pending_payment")),
                Appointment.hold_until.is_not(None),
                Appointment.hold_until <= now,
            )
        ).all()
        for appt in rows:
            try:
                _transition_unlocked(session, appt, "expired", actor="system:expire")
                n += 1
            except IllegalTransition:
                log.warning("expire skip %s status=%s", appt.id, appt.status)
    if n:
        log.info("expired %s stale holds", n)
    return n


def get_master_by_name(session, name: str) -> Master | None:
    return session.scalar(select(Master).where(Master.name == name, Master.is_active.is_(True)))


def enqueue_outbox(session, kind: str, payload: dict, when: datetime | None = None) -> None:
    session.add(Outbox(
        kind=kind,
        payload=payload,
        status="pending",
        attempts=0,
        next_attempt_at=when or _now(),
    ))


def _calendar_payload(session, appt: Appointment) -> dict:
    master = session.get(Master, appt.master_id)
    titles = (appt.services_json or {}).get("titles") or []
    services = ", ".join(titles) or "Запись"
    tz = (master.timezone if master else None) or "Europe/Moscow"
    return {
        "appointment_id": str(appt.id),
        "master_name": master.name if master else "",
        "calendar_id": (master.google_calendar_id if master else None) or "",
        "google_event_id": appt.google_event_id,
        "summary": f"[{master.name if master else ''}] {services}".strip(),
        "description": (
            f"Запись через бот NailBot.\n"
            f"source=nailbot appointment_id={appt.id}\n"
            f"Мастер: {master.name if master else '—'}\n"
            f"Телефон: {appt.client_phone or '—'}\n"
            f"Telegram ID клиента: {appt.client_tg_id}"
        ),
        "start": _as_utc(appt.start_at).isoformat(),
        "end": _as_utc(appt.end_at).isoformat(),
        "time_zone": tz,
    }


def _enqueue_reminders(session, appt: Appointment) -> None:
    now = _now()
    start = _as_utc(appt.start_at)
    for hours in (24, 2):
        when = start - timedelta(hours=hours)
        if when <= now:
            continue
        enqueue_outbox(session, "notify_client", {
            "appointment_id": str(appt.id),
            "hours_before": hours,
        }, when=when)


def _enqueue_status_side_effects(session, appt: Appointment, old: str, new: str) -> None:
    if new == "confirmed" and old != "confirmed":
        enqueue_outbox(session, "calendar_upsert", _calendar_payload(session, appt))
        _enqueue_reminders(session, appt)
    if new in ("cancelled", "expired", "superseded") and (appt.google_event_id or old == "confirmed"):
        payload = _calendar_payload(session, appt)
        enqueue_outbox(session, "calendar_delete", payload)


def transition(appointment_id: uuid.UUID | str, new_status: str, actor: str, **extra: Any) -> Appointment:
    if not pg_enabled():
        raise RuntimeError("Postgres SoT выключен")
    aid = appointment_id if isinstance(appointment_id, uuid.UUID) else uuid.UUID(str(appointment_id))
    with session_scope() as session:
        appt = session.get(Appointment, aid)
        if appt is None:
            raise KeyError(f"appointment {aid} not found")
        _transition_unlocked(session, appt, new_status, actor, **extra)
        session.flush()
        session.refresh(appt)
        return appt


def try_hold(
    *,
    master_name: str,
    start_at: datetime,
    end_at: datetime,
    client_tg_id: int,
    client_name: str = "",
    client_phone: str | None = None,
    service_titles: list[str] | None = None,
    sqlite_booking_id: str | None = None,
    source: str = "bot",
    actor: str = "client",
    duration_min: int = 60,
) -> tuple[Appointment, bool]:
    """INSERT held. Same client+slot reuses the row (False). Other client → SlotConflict."""
    if not pg_enabled():
        raise RuntimeError("Postgres SoT выключен")
    expire_stale_holds()
    start_at = _as_utc(start_at)
    end_at = _as_utc(end_at)
    with session_scope() as session:
        master = get_master_by_name(session, master_name)
        if master is None:
            raise KeyError(f"master {master_name!r} not in Postgres — seed from knowledge.md")

        existing = session.scalar(
            select(Appointment).where(
                Appointment.master_id == master.id,
                Appointment.start_at == start_at,
                Appointment.client_tg_id == client_tg_id,
                Appointment.status.in_(ACTIVE_STATUSES),
            )
        )
        if existing is not None:
            _audit(session, actor, "hold_idempotent", existing.id, {"start_at": start_at.isoformat()})
            return existing, False

        service_id = None
        titles = service_titles or []
        if titles:
            svc = session.scalar(
                select(Service).where(
                    Service.master_id == master.id,
                    Service.title == titles[0],
                    Service.is_active.is_(True),
                )
            )
            if svc is not None:
                service_id = svc.id
                duration_min = max(duration_min, svc.duration_min + svc.buffer_min)

        appt = Appointment(
            master_id=master.id,
            client_tg_id=client_tg_id,
            client_name=client_name or "",
            client_phone=client_phone,
            service_id=service_id,
            services_json={"titles": titles} if titles else None,
            start_at=start_at,
            end_at=end_at,
            status="held",
            hold_until=_now() + timedelta(minutes=hold_minutes()),
            payment_status="none",
            source=source,
            sqlite_booking_id=sqlite_booking_id,
            duration_min=duration_min,
        )
        session.add(appt)
        try:
            session.flush()
        except IntegrityError as e:
            session.rollback()
            log.info("slot conflict master=%s start=%s: %s", master_name, start_at.isoformat(), e)
            raise SlotConflict(str(e)) from e

        if not prepay_enabled() and actor in ("client", "admin", "bot"):
            # PREPAY=false: confirm in the same transaction after explicit hold.
            _transition_unlocked(session, appt, "confirmed", actor=actor)
        _audit(session, actor, "hold_created", appt.id, {
            "master": master_name,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "prepay": prepay_enabled(),
        })
        session.refresh(appt)
        return appt, True


def confirm_held(appointment_id: uuid.UUID | str, actor: str) -> Appointment:
    return transition(appointment_id, "confirmed", actor)


def reschedule_take(
    *,
    old_id: uuid.UUID | str,
    master_name: str,
    start_at: datetime,
    end_at: datetime,
    client_tg_id: int,
    client_name: str = "",
    client_phone: str | None = None,
    service_titles: list[str] | None = None,
    sqlite_booking_id: str | None = None,
    duration_min: int = 60,
    actor: str = "client",
) -> tuple[Appointment, bool]:
    """INSERT new held/confirmed first. On conflict old stays confirmed."""
    if not pg_enabled():
        raise RuntimeError("Postgres SoT выключен")
    expire_stale_holds()
    start_at = _as_utc(start_at)
    end_at = _as_utc(end_at)
    with session_scope() as session:
        old = None
        if isinstance(old_id, uuid.UUID):
            old = session.get(Appointment, old_id)
        else:
            try:
                old = session.get(Appointment, uuid.UUID(str(old_id)))
            except ValueError:
                old = session.scalar(
                    select(Appointment).where(Appointment.sqlite_booking_id == str(old_id))
                )
            if old is None:
                old = session.scalar(
                    select(Appointment).where(Appointment.sqlite_booking_id == str(old_id))
                )
        if old is not None and old.status != "confirmed":
            raise IllegalTransition(old.status, "superseded")

        master = get_master_by_name(session, master_name)
        if master is None:
            raise KeyError(f"master {master_name!r} not in Postgres")

        new = Appointment(
            master_id=master.id,
            client_tg_id=client_tg_id,
            client_name=client_name or "",
            client_phone=client_phone,
            services_json={"titles": service_titles or []},
            start_at=start_at,
            end_at=end_at,
            status="held",
            hold_until=_now() + timedelta(minutes=hold_minutes()),
            payment_status="none",
            source="bot",
            sqlite_booking_id=sqlite_booking_id,
            duration_min=duration_min,
            supersedes_id=old.id if old is not None else None,
        )
        session.add(new)
        try:
            session.flush()
        except IntegrityError as e:
            session.rollback()
            raise SlotConflict(str(e)) from e

        if not prepay_enabled():
            _transition_unlocked(session, new, "confirmed", actor=actor)
        if old is not None:
            _transition_unlocked(session, old, "superseded", actor=actor)
        _audit(session, actor, "reschedule", new.id, {
            "old_id": str(old.id) if old is not None else None,
            "start_at": start_at.isoformat(),
        })
        session.refresh(new)
        return new, True


def busy_intervals(master_name: str, day_start: datetime, day_end: datetime) -> list[tuple[datetime, datetime]]:
    """Active appointments + time_off. Never Google."""
    if not pg_enabled():
        return []
    expire_stale_holds()
    day_start = _as_utc(day_start)
    day_end = _as_utc(day_end)
    out: list[tuple[datetime, datetime]] = []
    with session_scope() as session:
        master = get_master_by_name(session, master_name)
        if master is None:
            return []
        rows = session.scalars(
            select(Appointment).where(
                Appointment.master_id == master.id,
                Appointment.status.in_(ACTIVE_STATUSES),
                Appointment.start_at < day_end,
                Appointment.end_at > day_start,
            )
        ).all()
        for a in rows:
            out.append((_as_utc(a.start_at), _as_utc(a.end_at)))

        tz = ZoneInfo(master.timezone or "Europe/Moscow")
        local_date = day_start.astimezone(tz).date()
        intervals = session.scalars(
            select(WorkInterval).where(
                WorkInterval.master_id == master.id,
                WorkInterval.kind == "time_off",
            )
        ).all()
        for iv in intervals:
            if iv.on_date is not None and iv.on_date != local_date:
                continue
            if iv.on_date is None and iv.weekday is not None and iv.weekday != local_date.weekday():
                continue
            if iv.on_date is None and iv.weekday is None:
                continue
            start_l = datetime.combine(local_date, iv.start_local, tzinfo=tz)
            end_l = datetime.combine(local_date, iv.end_local, tzinfo=tz)
            out.append((_as_utc(start_l), _as_utc(end_l)))
    return out


SLOT_HOURS = (10, 12, 14, 16, 18, 20)


def month_work_map(master_name: str, year: int, month: int) -> dict[str, list[int]]:
    """date iso -> hours. Empty dict = no PG schedule for this month (use knowledge)."""
    if not pg_enabled():
        return {}
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    out: dict[str, list[int]] = {}
    with session_scope() as session:
        master = get_master_by_name(session, master_name)
        if master is None:
            return {}
        rows = session.scalars(
            select(WorkInterval).where(
                WorkInterval.master_id == master.id,
                WorkInterval.on_date >= start,
                WorkInterval.on_date <= end,
            )
        ).all()
        if not rows:
            return {}
        for iv in rows:
            if iv.on_date is None:
                continue
            key = iv.on_date.isoformat()
            out.setdefault(key, [])
            if iv.kind == "time_off":
                continue
            if iv.kind == "work":
                h = iv.start_local.hour
                if h not in out[key]:
                    out[key].append(h)
        for k in list(out):
            out[k] = sorted(h for h in out[k] if h in SLOT_HOURS)
    return out


def hours_on_date(master_name: str, day: date) -> list[int] | None:
    """None = fallback to knowledge.md for this month."""
    m = month_work_map(master_name, day.year, day.month)
    if not m:
        return None
    return list(m.get(day.isoformat(), []))


def set_hours_on_date(master_name: str, day: date, hours: list[int], actor: str) -> list[int]:
    hours = sorted({int(h) for h in hours if int(h) in SLOT_HOURS})
    if not pg_enabled():
        raise RuntimeError("Postgres выключен")
    with session_scope() as session:
        master = get_master_by_name(session, master_name)
        if master is None:
            raise KeyError(master_name)
        session.execute(
            delete(WorkInterval).where(
                WorkInterval.master_id == master.id,
                WorkInterval.on_date == day,
            )
        )
        if not hours:
            session.add(WorkInterval(
                master_id=master.id,
                on_date=day,
                weekday=None,
                start_local=time(0, 0),
                end_local=time(23, 59),
                kind="time_off",
            ))
        else:
            for h in hours:
                end_h = min(h + 2, 23)
                session.add(WorkInterval(
                    master_id=master.id,
                    on_date=day,
                    weekday=None,
                    start_local=time(h, 0),
                    end_local=time(end_h, 0),
                    kind="work",
                ))
        _audit(session, actor, "set_hours", None, {
            "master": master_name, "date": day.isoformat(), "hours": hours,
        })
    return hours


def fill_weekdays(master_name: str, year: int, month: int, actor: str) -> int:
    from calendar import monthrange
    n = 0
    last = monthrange(year, month)[1]
    for d in range(1, last + 1):
        day = date(year, month, d)
        if day.weekday() >= 5:
            set_hours_on_date(master_name, day, [], actor)
        else:
            set_hours_on_date(master_name, day, list(SLOT_HOURS), actor)
            n += 1
    return n


def copy_month_schedule(master_name: str, src_year: int, src_month: int,
                        dst_year: int, dst_month: int, actor: str) -> int:
    from calendar import monthrange
    src = month_work_map(master_name, src_year, src_month)
    last = monthrange(dst_year, dst_month)[1]
    n = 0
    for d in range(1, last + 1):
        src_last = monthrange(src_year, src_month)[1]
        src_day = min(d, src_last)
        hours = src.get(date(src_year, src_month, src_day).isoformat(), [])
        set_hours_on_date(master_name, date(dst_year, dst_month, d), hours, actor)
        if hours:
            n += 1
    return n


def close_window(
    *,
    master_name: str,
    on_date: date,
    start_local: time,
    end_local: time,
    actor: str,
    reason: str = "time_off",
) -> WorkInterval:
    if not pg_enabled():
        raise RuntimeError("Postgres SoT выключен")
    with session_scope() as session:
        master = get_master_by_name(session, master_name)
        if master is None:
            raise KeyError(f"master {master_name!r} not in Postgres")
        iv = WorkInterval(
            master_id=master.id,
            on_date=on_date,
            weekday=None,
            start_local=start_local,
            end_local=end_local,
            kind="time_off",
        )
        session.add(iv)
        session.flush()
        _audit(session, actor, "close_window", None, {
            "master": master_name,
            "date": on_date.isoformat(),
            "start": start_local.isoformat(),
            "end": end_local.isoformat(),
            "reason": reason,
        })
        session.refresh(iv)
        return iv


def appointment_to_dict(appt: Appointment, master_name: str = "") -> dict[str, Any]:
    titles = (appt.services_json or {}).get("titles") or []
    start = _as_utc(appt.start_at)
    return {
        "id": str(appt.id),
        "sqlite_booking_id": appt.sqlite_booking_id,
        "master": master_name,
        "client_tg_id": appt.client_tg_id,
        "client_name": appt.client_name or "",
        "client_phone": appt.client_phone,
        "services": titles,
        "start_at": start.isoformat(),
        "end_at": _as_utc(appt.end_at).isoformat(),
        "datetime_iso": start.isoformat(),
        "status": appt.status,
        "google_event_id": appt.google_event_id,
        "calendar_event_id": appt.google_event_id,
        "source": appt.source,
        "duration_min": appt.duration_min,
    }


def _load_appointment(session, key: str) -> Appointment | None:
    try:
        appt = session.get(Appointment, uuid.UUID(str(key)))
        if appt is not None:
            return appt
    except (ValueError, TypeError):
        pass
    return session.scalar(
        select(Appointment).where(Appointment.sqlite_booking_id == str(key))
    )


def list_upcoming_for_client(tg_id: int, now: datetime) -> list[dict[str, Any]]:
    if not pg_enabled():
        return []
    now = _as_utc(now)
    with session_scope() as session:
        rows = session.scalars(
            select(Appointment)
            .where(
                Appointment.client_tg_id == int(tg_id),
                Appointment.status.in_(ACTIVE_STATUSES),
                Appointment.start_at > now,
            )
            .order_by(Appointment.start_at)
        ).all()
        out = []
        for a in rows:
            master = session.get(Master, a.master_id)
            out.append(appointment_to_dict(a, master.name if master else ""))
        return out


def cancel_appointment(key: str, actor: str, reason: str = "client_cancel") -> dict[str, Any] | None:
    """Cancel in Postgres. Idempotent if already cancelled. Returns dict or None if missing."""
    if not pg_enabled():
        return None
    with session_scope() as session:
        appt = _load_appointment(session, key)
        if appt is None:
            return None
        master = session.get(Master, appt.master_id)
        name = master.name if master else ""
        if appt.status in ("cancelled", "expired", "superseded"):
            d = appointment_to_dict(appt, name)
            d["already"] = True
            return d
        if appt.status not in ACTIVE_STATUSES:
            d = appointment_to_dict(appt, name)
            d["already"] = True
            return d
        _transition_unlocked(session, appt, "cancelled", actor=actor, cancel_reason=reason)
        session.flush()
        d = appointment_to_dict(appt, name)
        d["already"] = False
        return d


def list_for_master(master_name: str, day_start: datetime, day_end: datetime) -> list[dict[str, Any]]:
    if not pg_enabled():
        return []
    day_start = _as_utc(day_start)
    day_end = _as_utc(day_end)
    with session_scope() as session:
        master = get_master_by_name(session, master_name)
        if master is None:
            return []
        rows = session.scalars(
            select(Appointment)
            .where(
                Appointment.master_id == master.id,
                Appointment.start_at < day_end,
                Appointment.end_at > day_start,
                Appointment.status.in_(("held", "pending_payment", "confirmed", "completed", "no_show")),
            )
            .order_by(Appointment.start_at)
        ).all()
        return [appointment_to_dict(a, master.name) for a in rows]


def list_services(master_name: str) -> list[dict[str, Any]]:
    if not pg_enabled():
        return []
    with session_scope() as session:
        master = get_master_by_name(session, master_name)
        if master is None:
            return []
        rows = session.scalars(
            select(Service).where(Service.master_id == master.id).order_by(Service.sort_order, Service.title)
        ).all()
        return [
            {
                "id": str(s.id),
                "title": s.title,
                "duration_min": s.duration_min,
                "price_minor": s.price_minor,
                "is_active": s.is_active,
            }
            for s in rows
        ]


def update_service(service_id: str, **fields: Any) -> dict[str, Any] | None:
    if not pg_enabled():
        return None
    aid = uuid.UUID(str(service_id))
    allowed = {"is_active", "price_minor", "duration_min", "title"}
    with session_scope() as session:
        row = session.get(Service, aid)
        if row is None:
            return None
        for k, v in fields.items():
            if k in allowed:
                setattr(row, k, v)
        session.flush()
        return {
            "id": str(row.id),
            "title": row.title,
            "duration_min": row.duration_min,
            "price_minor": row.price_minor,
            "is_active": row.is_active,
        }


def week_stats(master_name: str, day_start: datetime, day_end: datetime) -> dict[str, int]:
    if not pg_enabled():
        return {"confirmed": 0, "cancelled": 0, "no_show": 0, "completed": 0}
    day_start = _as_utc(day_start)
    day_end = _as_utc(day_end)
    counts = {"confirmed": 0, "cancelled": 0, "no_show": 0, "completed": 0, "superseded": 0}
    with session_scope() as session:
        master = get_master_by_name(session, master_name)
        if master is None:
            return counts
        rows = session.scalars(
            select(Appointment).where(
                Appointment.master_id == master.id,
                Appointment.start_at >= day_start,
                Appointment.start_at < day_end,
            )
        ).all()
        for a in rows:
            if a.status in counts:
                counts[a.status] += 1
    return counts


def enqueue_missing_calendar() -> int:
    """Queue Calendar upsert for live appointments that never got an event (GOOGLE was off)."""
    if not pg_enabled():
        return 0
    n = 0
    now = _now()
    with session_scope() as session:
        rows = session.scalars(
            select(Appointment).where(
                Appointment.status.in_(ACTIVE_STATUSES),
                Appointment.google_event_id.is_(None),
                Appointment.end_at >= now,
            )
        ).all()
        pending_ids = set()
        pending_rows = session.scalars(
            select(Outbox).where(
                Outbox.kind == "calendar_upsert",
                Outbox.status.in_(("pending", "processing")),
            )
        ).all()
        for ob in pending_rows:
            aid = (ob.payload or {}).get("appointment_id")
            if aid:
                pending_ids.add(str(aid))
        for appt in rows:
            if str(appt.id) in pending_ids:
                continue
            payload = _calendar_payload(session, appt)
            if not payload.get("calendar_id"):
                continue
            enqueue_outbox(session, "calendar_upsert", payload)
            n += 1
    if n:
        log.info("enqueued %s missing calendar upserts", n)
    return n


def set_google_event_id(appointment_id: uuid.UUID | str, event_id: str | None) -> None:
    if not pg_enabled() or not appointment_id:
        return
    try:
        aid = appointment_id if isinstance(appointment_id, uuid.UUID) else uuid.UUID(str(appointment_id))
    except Exception:
        return
    with session_scope() as session:
        appt = session.get(Appointment, aid)
        if appt is None:
            return
        appt.google_event_id = event_id
        appt.updated_at = _now()


def lookup_pg_id(sqlite_booking_id: str | None) -> uuid.UUID | None:
    if not pg_enabled() or not sqlite_booking_id:
        return None
    with session_scope() as session:
        appt = session.scalar(
            select(Appointment).where(Appointment.sqlite_booking_id == sqlite_booking_id)
        )
        return appt.id if appt else None


def _parse_price_minor(price: str) -> int:
    digits = re.sub(r"[^\d]", "", price or "")
    if not digits:
        return 0
    return int(digits) * 100  # rubles → kopecks if knowledge stores whole rubles


def _parse_duration_min(duration: str, default: int = 60) -> int:
    m = re.search(r"(\d+)", duration or "")
    return int(m.group(1)) if m else default


def seed_from_knowledge(kb: Any) -> None:
    """Idempotent upsert of masters/services from knowledge.md."""
    if not pg_enabled() or kb is None:
        return
    default_tz = os_tz()
    inserted = 0
    with session_scope() as session:
        for sort_i, (name, m) in enumerate(kb.masters.items()):
            tg_id = None
            raw = str(getattr(m, "telegram_id", "") or "").strip()
            if raw.isdigit():
                tg_id = int(raw)
            row = session.scalar(select(Master).where(Master.name == name))
            cal = (getattr(m, "calendar_id", None) or "").strip() or None
            if cal and "заполнить" in cal.lower():
                cal = None
            if row is None:
                row = Master(
                    name=name,
                    tg_user_id=tg_id,
                    timezone=default_tz,
                    google_calendar_id=cal,
                    is_active=True,
                )
                session.add(row)
                session.flush()
            else:
                if tg_id:
                    row.tg_user_id = tg_id
                row.google_calendar_id = cal
                row.is_active = True

            for svc in kb.services:
                masters = list(getattr(svc, "masters", None) or [])
                if masters and name not in masters:
                    continue
                existing = session.scalar(
                    select(Service).where(Service.master_id == row.id, Service.title == svc.name)
                )
                if existing is None:
                    session.add(Service(
                        master_id=row.id,
                        title=svc.name,
                        duration_min=_parse_duration_min(getattr(svc, "duration", "") or ""),
                        price_minor=_parse_price_minor(getattr(svc, "price", "") or ""),
                        buffer_min=0,
                        is_active=True,
                        sort_order=sort_i,
                    ))
                    inserted += 1
                # existing: do not touch price_minor / duration_min / is_active.
                # Master edits those in the bot; knowledge.md is only a template
                # for rows that are not in Postgres yet.
        log.info("seeded masters/services from knowledge.md insert=%s", inserted)


def os_tz() -> str:
    import os
    tz = (os.getenv("TZ") or "Europe/Moscow").strip()
    return tz or "Europe/Moscow"
