"""
Восстановление SQLite из Redis state после wipe эфемерного диска (Render free).

Идемпотентно: уже существующие booking id не трогаем.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("nailbot.database.reconcile")


def reconcile_redis_to_sqlite(db, state_store) -> dict[str, int]:
    """
    Пробегает nailbot:all_clients / state, upsert клиентов и INSERT недостающих bookings.

    Returns:
        clients_upserted, bookings_inserted, bookings_skipped, errors
    """
    stats = {
        "clients_upserted": 0,
        "bookings_inserted": 0,
        "bookings_skipped": 0,
        "errors": 0,
    }
    if db is None or state_store is None:
        return stats

    try:
        user_ids = list(state_store.all_users())
    except Exception as e:
        log.warning("reconcile: all_users failed: %s", e)
        return stats

    for uid in user_ids:
        try:
            st = state_store.get(uid) or {}
        except Exception as e:
            log.debug("reconcile: get %s: %s", uid, e)
            stats["errors"] += 1
            continue

        card = st.get("card") or {}
        try:
            db.clients.upsert_client(
                int(uid),
                chat_id=int(uid),
                name=(card.get("name") or None),
                notes=(card.get("notes") or None),
                birthday=(card.get("birthday") or None),
                tags=card.get("tags") if isinstance(card.get("tags"), list) else None,
            )
            # total_visits / referral fields — best-effort from state
            visits = st.get("total_visits")
            if isinstance(visits, int) and visits >= 0:
                try:
                    from database.connection import get_connection
                    from datetime import datetime
                    with get_connection() as conn:
                        conn.execute(
                            """
                            UPDATE clients SET total_visits = MAX(total_visits, ?),
                                               updated_at = ?
                            WHERE user_id = ?
                            """,
                            (visits, datetime.utcnow().isoformat(), int(uid)),
                        )
                        conn.commit()
                except Exception:
                    pass
            stats["clients_upserted"] += 1
        except Exception as e:
            log.warning("reconcile: upsert client %s: %s", uid, e)
            stats["errors"] += 1

        for b in st.get("bookings") or []:
            if not isinstance(b, dict):
                continue
            bid = b.get("id")
            if not bid:
                stats["bookings_skipped"] += 1
                continue
            try:
                existing = db.bookings.get_booking(str(bid))
                if existing:
                    stats["bookings_skipped"] += 1
                    continue
                row: dict[str, Any] = {
                    "id": str(bid),
                    "user_id": int(b.get("user_id") or uid),
                    "master": b.get("master") or "—",
                    "services": b.get("services") or [],
                    "services_raw": b.get("services_raw") or "",
                    "datetime": b.get("datetime") or b.get("datetime_text") or "",
                    "datetime_iso": b.get("datetime_iso") or "",
                    "duration_min": b.get("duration_min") or 60,
                    "phone": b.get("phone") or (card.get("phone") if card else None),
                    "chat_id": b.get("chat_id") or uid,
                    "status": b.get("status") or "confirmed",
                    "calendar_event_id": b.get("calendar_event_id"),
                    "promo_code": b.get("promo_code"),
                    "notes": b.get("notes") or [],
                    "reminder_job_ids": b.get("reminder_job_ids") or [],
                }
                if not row["datetime_iso"]:
                    stats["bookings_skipped"] += 1
                    continue
                db.bookings.create_booking(row)
                stats["bookings_inserted"] += 1
            except Exception as e:
                # duplicate id / FK — skip
                log.debug("reconcile: booking %s: %s", bid, e)
                stats["bookings_skipped"] += 1
                stats["errors"] += 1

    log.info(
        "reconcile Redis→SQLite: clients=%s bookings_new=%s skipped=%s errors=%s",
        stats["clients_upserted"],
        stats["bookings_inserted"],
        stats["bookings_skipped"],
        stats["errors"],
    )
    return stats
