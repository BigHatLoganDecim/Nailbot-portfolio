"""P1 worker tick: expire holds, no-show, outbox (calendar + reminders).

Runs in-process via APScheduler. Same code can be a separate process later.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .models import Appointment, Outbox
from .pg import calendar_write_enabled, pg_enabled, session_scope
from .appointments import IllegalTransition, _as_utc, _now, _transition_unlocked, expire_stale_holds

log = logging.getLogger("nailbot.worker")

RETRY_DELAYS_SEC = (60, 300, 1800, 7200)  # 1м, 5м, 30м, 2ч, потом dead


def noshow_grace_min() -> int:
    raw = (os.getenv("NOSHOW_GRACE_MIN", "15") or "15").strip()
    try:
        return max(0, min(180, int(raw)))
    except ValueError:
        return 15


def next_retry_at(attempts: int) -> datetime | None:
    if attempts >= len(RETRY_DELAYS_SEC):
        return None
    return _now() + timedelta(seconds=RETRY_DELAYS_SEC[attempts])


def mark_no_shows() -> int:
    if not pg_enabled():
        return 0
    n = 0
    cutoff = _now() - timedelta(minutes=noshow_grace_min())
    with session_scope() as session:
        rows = session.scalars(
            select(Appointment).where(
                Appointment.status == "confirmed",
                Appointment.start_at <= cutoff,
            )
        ).all()
        for appt in rows:
            try:
                _transition_unlocked(session, appt, "no_show", actor="system:noshow")
                n += 1
            except IllegalTransition:
                continue
    if n:
        log.info("no_show marked: %s", n)
    return n


def _fail_or_retry(row: Outbox, err: str) -> None:
    row.attempts += 1
    row.last_error = (err or "")[:2000]
    nxt = next_retry_at(row.attempts)
    if nxt is None:
        row.status = "dead"
        row.next_attempt_at = _now()
        log.error("outbox dead id=%s kind=%s err=%s", row.id, row.kind, err)
    else:
        row.status = "pending"
        row.next_attempt_at = nxt
        log.warning("outbox retry id=%s attempt=%s next=%s err=%s", row.id, row.attempts, nxt, err)


def _handle_calendar(row: Outbox) -> None:
    from services.calendar import create_event, delete_event, get_service, update_event

    if not calendar_write_enabled():
        raise RuntimeError("calendar credentials missing or GOOGLE_ENABLED=false")
    payload = dict(row.payload or {})
    svc = get_service()
    if not svc:
        raise RuntimeError("google calendar service unavailable")

    aid = payload.get("appointment_id")
    calendar_id = payload.get("calendar_id") or ""
    event_id = payload.get("google_event_id") or ""
    if aid:
        from .pg import session_scope as _ss
        from .models import Appointment as _Appt, Master as _Master
        import uuid as _uuid
        try:
            with _ss() as session:
                appt = session.get(_Appt, _uuid.UUID(str(aid)))
                if appt is not None:
                    if appt.google_event_id:
                        event_id = appt.google_event_id
                    master = session.get(_Master, appt.master_id)
                    if master and master.google_calendar_id:
                        calendar_id = calendar_id or master.google_calendar_id
        except Exception as e:
            log.warning("calendar outbox load appt: %s", e)

    if not calendar_id:
        raise RuntimeError("no calendar_id on master/payload")

    if row.kind == "calendar_delete":
        if event_id:
            delete_event(svc, calendar_id, event_id)
        row.status = "done"
        return

    # calendar_upsert
    event = {
        "summary": payload.get("summary") or "Запись",
        "description": payload.get("description") or "",
        "start": {"dateTime": payload["start"], "timeZone": payload.get("time_zone") or "Europe/Moscow"},
        "end": {"dateTime": payload["end"], "timeZone": payload.get("time_zone") or "Europe/Moscow"},
        "extendedProperties": {
            "private": {
                "source": "nailbot",
                "appointment_id": payload.get("appointment_id") or "",
            }
        },
    }
    if event_id:
        result = update_event(svc, calendar_id, event_id, event)
        if not result:
            result = create_event(svc, calendar_id, event)
    else:
        result = create_event(svc, calendar_id, event)
    if not result:
        raise RuntimeError("calendar upsert returned None")
    new_id = result.get("id")
    aid = payload.get("appointment_id")
    if aid and new_id:
        from .appointments import set_google_event_id
        set_google_event_id(aid, new_id)
        payload = dict(payload)
        payload["google_event_id"] = new_id
        row.payload = payload
    row.status = "done"


def _handle_notify_client(row: Outbox, bot: Any) -> None:
    if bot is None:
        raise RuntimeError("bot is not available")
    payload = row.payload or {}
    aid = payload.get("appointment_id")
    if not aid:
        row.status = "done"
        return
    with session_scope() as session:
        appt = session.get(Appointment, uuid.UUID(str(aid)))
        if appt is None or appt.status not in ("confirmed", "held", "pending_payment"):
            row.status = "done"
            return
        hours = int(payload.get("hours_before") or 0)
        titles = (appt.services_json or {}).get("titles") or []
        services = ", ".join(titles) or "процедура"
        when = _as_utc(appt.start_at).astimezone().strftime("%d.%m %H:%M")
        if hours >= 24:
            when_label = "завтра"
        elif hours >= 2:
            when_label = "через 2 часа"
        else:
            when_label = "скоро"
        text = (
            f"⏰ Напоминание: {when_label} ваша запись.\n"
            f"Время: {when}\n"
            f"Процедуры: {services}"
        )
        chat_id = appt.client_tg_id
        status = appt.status
    if status != "confirmed":
        row.status = "done"
        return
    bot.send_message(chat_id, text)
    row.status = "done"


def process_outbox(bot: Any = None, limit: int = 20) -> dict[str, int]:
    stats = {"done": 0, "retry": 0, "dead": 0}
    if not pg_enabled():
        return stats
    now = _now()
    with session_scope() as session:
        rows = session.scalars(
            select(Outbox)
            .where(
                Outbox.status == "pending",
                Outbox.next_attempt_at <= now,
            )
            .order_by(Outbox.next_attempt_at)
            .limit(limit)
        ).all()
        for row in rows:
            row.status = "processing"
        session.flush()
        ids = [r.id for r in rows]

    for oid in ids:
        with session_scope() as session:
            row = session.get(Outbox, oid)
            if row is None:
                continue
            prev = row.status
            try:
                if row.kind in ("calendar_upsert", "calendar_delete"):
                    _handle_calendar(row)
                elif row.kind == "notify_client":
                    _handle_notify_client(row, bot)
                elif row.kind == "notify_master":
                    row.status = "done"
                else:
                    row.status = "done"
            except Exception as e:
                _fail_or_retry(row, str(e))
            if row.status == "done":
                stats["done"] += 1
            elif row.status == "dead":
                stats["dead"] += 1
            elif row.status == "pending":
                stats["retry"] += 1
            elif prev == "processing":
                _fail_or_retry(row, "handler left processing")
                stats["retry"] += 1
    return stats


def tick(bot: Any = None) -> dict[str, Any]:
    """One worker pass. Safe to call every 30–60s."""
    result: dict[str, Any] = {"expired": 0, "no_show": 0, "outbox": {}}
    if not pg_enabled():
        return result
    try:
        result["expired"] = expire_stale_holds()
    except Exception as e:
        log.exception("worker expire: %s", e)
    try:
        result["no_show"] = mark_no_shows()
    except Exception as e:
        log.exception("worker noshow: %s", e)
    try:
        from .pg import calendar_write_enabled
        from .appointments import enqueue_missing_calendar
        if calendar_write_enabled():
            result["cal_backfill"] = enqueue_missing_calendar()
    except Exception as e:
        log.exception("worker calendar backfill: %s", e)
    try:
        result["outbox"] = process_outbox(bot=bot)
    except Exception as e:
        log.exception("worker outbox: %s", e)
    if result["expired"] or result["no_show"] or any(result["outbox"].values()):
        log.info("worker tick %s", result)
    return result
