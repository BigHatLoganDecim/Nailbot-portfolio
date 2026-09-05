"""
Repository layer for long-term data access.

These classes are the primary interface for working with clients and bookings.
Designed to be:
- Safe and explicit
- Easy to query for analytics
- Ready for future AI reporting features
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from .connection import get_connection

log = logging.getLogger("nailbot.database")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_dumps(obj: Any) -> str:
    if obj is None:
        return "[]"
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# ClientsRepository
# ---------------------------------------------------------------------------

class ClientsRepository:
    """
    Long-term storage for client profiles and aggregated data.

    This is the source of truth for client cards, history, and referral data.
    """

    def upsert_client(
        self,
        user_id: int,
        *,
        chat_id: int | None = None,
        name: str | None = None,
        notes: str | None = None,
        birthday: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Create or update basic client information."""
        now = datetime.utcnow().isoformat()

        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO clients (user_id, telegram_chat_id, name, notes, birthday, tags,
                                     first_seen_at, last_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    telegram_chat_id = COALESCE(excluded.telegram_chat_id, telegram_chat_id),
                    name = COALESCE(excluded.name, name),
                    notes = COALESCE(excluded.notes, notes),
                    birthday = COALESCE(excluded.birthday, birthday),
                    tags = COALESCE(excluded.tags, tags),
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    chat_id,
                    name or "",
                    notes or "",
                    birthday or "",
                    _json_dumps(tags or []),
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_client(self, user_id: int) -> dict[str, Any] | None:
        """Get full client record."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM clients WHERE user_id = ?",
                (user_id,)
            ).fetchone()

            if not row:
                return None

            return {
                "user_id": row["user_id"],
                "chat_id": row["telegram_chat_id"],
                "name": row["name"] or "",
                "notes": row["notes"] or "",
                "birthday": row["birthday"] or "",
                "tags": _json_loads(row["tags"], []),
                "total_visits": row["total_visits"] or 0,
                "referred_by": row["referred_by"],
                "referral_used": bool(row["referral_used"]),
                "referrals_brought": row["referrals_brought"] or 0,
                "design_bonuses": row["design_bonuses"] or 0,
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
            }

    def delete_client_and_bookings(self, user_id: int) -> dict[str, int]:
        """Полное удаление карточки и всех записей клиента (ФЗ-152 / запрос субъекта)."""
        with get_connection() as conn:
            cur_b = conn.execute("DELETE FROM bookings WHERE user_id = ?", (user_id,))
            bookings_deleted = cur_b.rowcount
            cur_c = conn.execute("DELETE FROM clients WHERE user_id = ?", (user_id,))
            clients_deleted = cur_c.rowcount
            conn.commit()
        log.info(
            "delete_client_and_bookings: user=%s bookings=%s client=%s",
            user_id, bookings_deleted, clients_deleted,
        )
        return {"bookings": bookings_deleted, "clients": clients_deleted}

    def get_clients_with_birthday_today(self, day: int, month: int) -> list[dict[str, Any]]:
        """Клиенты, у которых сегодня день рождения.
        birthday хранится как 'DD.MM' или 'DD.MM.YYYY' — матчим по началу строки."""
        prefix = f"{day:02d}.{month:02d}"
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT user_id, telegram_chat_id, name, birthday, total_visits
                   FROM clients
                   WHERE birthday LIKE ? AND telegram_chat_id IS NOT NULL""",
                (prefix + "%",),
            ).fetchall()
            return [
                {
                    "user_id": r["user_id"],
                    "chat_id": r["telegram_chat_id"],
                    "name": r["name"] or "",
                    "birthday": r["birthday"] or "",
                    "total_visits": r["total_visits"] or 0,
                }
                for r in rows
            ]

    def increment_visits(self, user_id: int) -> int:
        """Increase visit counter and return new total."""
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE clients
                SET total_visits = total_visits + 1,
                    last_seen_at = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (datetime.utcnow().isoformat(), datetime.utcnow().isoformat(), user_id),
            )
            conn.commit()

            row = conn.execute(
                "SELECT total_visits FROM clients WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            return row[0] if row else 0

    def decrement_visits(self, user_id: int, reason: str = "unknown", only_if_confirmed: bool = True) -> int:
        """Decrease visit counter safely.

        If only_if_confirmed=True (default), it will only decrement if there is at least one
        confirmed booking for this user in the recent history. This prevents accidental
        double-decrements on race conditions or repeated calls.

        Returns new total. Reason is logged for auditability.
        """
        with get_connection() as conn:
            if only_if_confirmed:
                # Safety check: only decrement if user still has confirmed visits recorded
                confirmed_count = conn.execute(
                    "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND status = 'confirmed'",
                    (user_id,)
                ).fetchone()[0]
                if confirmed_count == 0:
                    log.warning("decrement_visits skipped (no confirmed bookings left): user=%s, reason=%s", user_id, reason)
                    row = conn.execute("SELECT total_visits FROM clients WHERE user_id = ?", (user_id,)).fetchone()
                    return row[0] if row else 0

            conn.execute(
                """
                UPDATE clients
                SET total_visits = MAX(0, total_visits - 1),
                    updated_at = ?
                WHERE user_id = ?
                """,
                (datetime.utcnow().isoformat(), user_id),
            )
            conn.commit()

            row = conn.execute(
                "SELECT total_visits FROM clients WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            new_total = row[0] if row else 0
            log.info("decrement_visits: user=%s, reason=%s, new_total=%s", user_id, reason, new_total)
            return new_total

    def repair_visits(self, user_id: int) -> int:
        """
        Recalculates and fixes the total_visits counter for a client
        based on actual confirmed + completed bookings in the long-term DB.
        (Cancelled/rescheduled/no_show are excluded as they do not count as visits.)
        Returns the corrected total_visits.
        """
        with get_connection() as conn:
            actual_count = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND status IN ('confirmed', 'completed')",
                (user_id,)
            ).fetchone()[0]

            conn.execute(
                """
                UPDATE clients
                SET total_visits = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (actual_count, datetime.utcnow().isoformat(), user_id)
            )
            conn.commit()

            log.warning("repair_visits: user=%s, corrected total_visits to %s", user_id, actual_count)
            return actual_count

    def count_design_bonuses_used(self, user_id: int) -> int:
        """Rough count of how many times this client used a design bonus."""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT notes FROM bookings WHERE user_id = ? AND status IN ('confirmed', 'completed')",
                (user_id,)
            ).fetchall()
            count = 0
            for (notes_json,) in rows:
                if notes_json:
                    try:
                        notes = json.loads(notes_json) if isinstance(notes_json, str) else notes_json
                        if any("дизайн-бонус" in str(n).lower() or "referrer_bonus" in str(n).lower() for n in notes):
                            count += 1
                    except Exception:
                        pass
            return count

    def get_referrals_brought(self, user_id: int) -> int:
        """Returns how many referrals this user has successfully brought."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT referrals_brought FROM clients WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            return row[0] if row else 0

    def update_card(
        self,
        user_id: int,
        *,
        name: str | None = None,
        notes: str | None = None,
        birthday: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Update client card fields (partial update supported)."""
        now = datetime.utcnow().isoformat()
        fields = []
        values: list[Any] = []

        if name is not None:
            fields.append("name = ?")
            values.append(name)
        if notes is not None:
            fields.append("notes = ?")
            values.append(notes)
        if birthday is not None:
            fields.append("birthday = ?")
            values.append(birthday)
        if tags is not None:
            fields.append("tags = ?")
            values.append(_json_dumps(tags))

        if not fields:
            return

        fields.append("updated_at = ?")
        fields.append("last_seen_at = ?")
        values.extend([now, now, user_id])

        with get_connection() as conn:
            conn.execute(
                f"""
                UPDATE clients
                SET {", ".join(fields)}
                WHERE user_id = ?
                """,
                values,
            )
            conn.commit()

    def add_tag(self, user_id: int, tag: str) -> None:
        """Add a tag to the client (idempotent)."""
        client = self.get_client(user_id) or {}
        current_tags = client.get("tags", [])
        if tag not in current_tags:
            current_tags.append(tag)
            self.update_card(user_id, tags=current_tags)

    def update_referral_info(
        self,
        user_id: int,
        *,
        referred_by: int | None = None,
        referral_used: bool | None = None,
        increment_referrals_brought: bool = False,
        increment_design_bonuses: int = 0,
    ) -> None:
        """Update referral-related fields."""
        now = datetime.utcnow().isoformat()
        sets = []
        values: list[Any] = []

        if referred_by is not None:
            sets.append("referred_by = ?")
            values.append(referred_by)
        if referral_used is not None:
            sets.append("referral_used = ?")
            values.append(1 if referral_used else 0)
        if increment_referrals_brought:
            sets.append("referrals_brought = referrals_brought + 1")
        if increment_design_bonuses != 0:
            sets.append("design_bonuses = design_bonuses + ?")
            values.append(increment_design_bonuses)

        if not sets:
            return

        sets.append("updated_at = ?")
        values.append(now)
        values.append(user_id)

        with get_connection() as conn:
            conn.execute(
                f"UPDATE clients SET {', '.join(sets)} WHERE user_id = ?",
                values,
            )
            conn.commit()

    def set_migration_data(
        self,
        user_id: int,
        *,
        total_visits: int | None = None,
        design_bonuses: int | None = None,
        referrals_brought: int | None = None,
    ) -> None:
        """
        Special method for legacy data migration only.
        Sets absolute counter values safely (bypasses normal increment logic).
        """
        now = datetime.utcnow().isoformat()
        sets = []
        values: list[Any] = []

        if total_visits is not None:
            sets.append("total_visits = ?")
            values.append(max(0, int(total_visits)))
        if design_bonuses is not None:
            sets.append("design_bonuses = ?")
            values.append(max(0, int(design_bonuses)))
        if referrals_brought is not None:
            sets.append("referrals_brought = ?")
            values.append(max(0, int(referrals_brought)))

        if not sets:
            return

        sets.append("updated_at = ?")
        values.append(now)
        values.append(user_id)

        with get_connection() as conn:
            conn.execute(
                f"UPDATE clients SET {', '.join(sets)} WHERE user_id = ?",
                values,
            )
            conn.commit()

    def get_full_client_profile(self, user_id: int) -> dict[str, Any] | None:
        """
        Returns rich client data: card + stats + recent bookings.
        Extremely useful for AI analysis and master dashboards.
        """
        client = self.get_client(user_id)
        if not client:
            return None

        # Get recent bookings for this client
        bookings_repo = BookingsRepository()
        recent_bookings = bookings_repo.get_bookings_for_user(user_id, limit=20)

        # Calculate simple stats
        ratings = [b["rating"] for b in recent_bookings if b.get("rating")]
        avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None

        all_services = []
        for b in recent_bookings:
            all_services.extend(b.get("services") or [])

        from collections import Counter
        fav_service = Counter(all_services).most_common(1)
        favorite_service = fav_service[0][0] if fav_service else None

        client["recent_bookings"] = recent_bookings
        client["stats"] = {
            "total_visits": client.get("total_visits", 0),
            "avg_rating": avg_rating,
            "favorite_service": favorite_service,
            "bookings_with_reviews": len([b for b in recent_bookings if b.get("review_text")]),
        }
        return client


# ---------------------------------------------------------------------------
# BookingsRepository
# ---------------------------------------------------------------------------

class BookingsRepository:
    """
    Primary repository for booking history.

    This table will be the main source for weekly AI analytics reports.
    """

    def create_booking(self, booking: dict[str, Any]) -> str:
        """Insert a new booking record. Returns booking id."""
        booking_id = booking.get("id") or __import__("uuid").uuid4().hex[:12]

        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO bookings (
                    id, user_id, master, services, services_raw,
                    datetime_text, datetime_iso, duration_min, phone,
                    chat_id, status, calendar_event_id, promo_code,
                    notes, reminder_job_ids, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    booking_id,
                    booking["user_id"],
                    booking["master"],
                    _json_dumps(booking.get("services") or []),
                    booking.get("services_raw", ""),
                    booking.get("datetime"),
                    booking.get("datetime_iso"),
                    booking.get("duration_min", 60),
                    booking.get("phone"),
                    booking.get("chat_id"),
                    booking.get("status", "confirmed"),
                    booking.get("calendar_event_id"),
                    booking.get("promo_code"),
                    _json_dumps(booking.get("notes") or []),
                    _json_dumps(booking.get("reminder_job_ids") or []),
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()

        log.info("Booking created in DB: %s (user=%s)", booking_id, booking.get("user_id"))
        return booking_id

    def get_bookings_for_user(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Get historical bookings for a client (newest first)."""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM bookings
                WHERE user_id = ?
                ORDER BY datetime_iso DESC, created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()

            return [self._row_to_dict(row) for row in rows]

    def update_booking(self, booking_id: str, **fields: Any) -> bool:
        """Update specific fields on a booking (rating, review, status, etc.)."""
        if not fields:
            return False

        allowed = {
            "status", "rating", "review_text", "calendar_event_id",
            "reminder_job_ids", "notes", "payment_status", "payment_amount"
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        sets = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(datetime.utcnow().isoformat())
        values.append(booking_id)

        with get_connection() as conn:
            cur = conn.execute(
                f"UPDATE bookings SET {sets}, updated_at = ? WHERE id = ?",
                values,
            )
            conn.commit()
            return cur.rowcount > 0

    def get_booking(self, booking_id: str) -> dict[str, Any] | None:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def get_booking_by_calendar_event_id(self, calendar_event_id: str) -> dict[str, Any] | None:
        """Найти запись по ID события в Google Calendar."""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM bookings WHERE calendar_event_id = ? LIMIT 1",
                (calendar_event_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def update_rating_and_review(
        self,
        booking_id: str,
        *,
        rating: int | None = None,
        review_text: str | None = None,
    ) -> bool:
        """Save client review after the visit. Very important for future AI analysis."""
        if rating is None and review_text is None:
            return False

        updates = {}
        if rating is not None:
            updates["rating"] = max(1, min(5, int(rating)))
        if review_text is not None:
            updates["review_text"] = review_text.strip()

        return self.update_booking(booking_id, **updates)

    def mark_booking_status(self, booking_id: str, status: str) -> bool:
        """Change status (confirmed, cancelled, completed, no_show)."""
        return self.update_booking(booking_id, status=status)

    def count_bookings_on_day(
        self,
        day_iso: str,
        statuses: tuple[str, ...] = ("confirmed", "completed"),
    ) -> int:
        """Сколько записей с datetime_iso на календарный день YYYY-MM-DD.

        datetime_iso часто с таймзоной (2026-07-09T14:00:00+03:00) —
        сравниваем по префиксу даты, не date() SQLite.
        """
        prefix = f"{day_iso}%"
        placeholders = ",".join("?" * len(statuses))
        with get_connection() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) FROM bookings
                WHERE datetime_iso LIKE ?
                  AND status IN ({placeholders})
                """,
                (prefix, *statuses),
            ).fetchone()
            return int(row[0] if row else 0)

    def get_bookings_for_period(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        master: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """
        Get bookings in a date range. Extremely useful for weekly/monthly AI reports.
        Dates should be in ISO format (YYYY-MM-DD).
        """
        conditions = []
        params: list[Any] = []

        if start_date:
            conditions.append("date(datetime_iso) >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("date(datetime_iso) <= ?")
            params.append(end_date)
        if master:
            conditions.append("master = ?")
            params.append(master)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT * FROM bookings
            {where}
            ORDER BY datetime_iso DESC
            LIMIT ?
        """
        params.append(limit)

        with get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def get_five_star_reviews(self, master: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Возвращает только 5-звёздочные отзывы с текстом, отсортированные по дате."""
        with get_connection() as conn:
            if master:
                rows = conn.execute(
                    """SELECT b.*, c.name as client_name FROM bookings b
                       LEFT JOIN clients c ON b.user_id = c.user_id
                       WHERE b.rating = 5 AND b.review_text IS NOT NULL
                         AND b.review_text != '' AND b.master = ?
                       ORDER BY b.datetime_iso DESC LIMIT ?""",
                    (master, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT b.*, c.name as client_name FROM bookings b
                       LEFT JOIN clients c ON b.user_id = c.user_id
                       WHERE b.rating = 5 AND b.review_text IS NOT NULL
                         AND b.review_text != ''
                       ORDER BY b.datetime_iso DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            result = []
            for row in rows:
                d = self._row_to_dict(row)
                d["client_name"] = row["client_name"] or ""
                result.append(d)
            return result

    def get_recent_bookings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get most recent bookings across all clients (useful for admin views)."""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM bookings ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def get_bookings_stats(self, master: str | None = None, days: int = 30) -> dict[str, Any]:
        """
        Basic statistics for a period. Good building block for weekly/monthly AI reports.
        """
        with get_connection() as conn:
            base_where = ""
            params: list[Any] = []

            if master:
                base_where = "WHERE master = ? AND datetime_iso >= date('now', ?)"
                params = [master, f"-{days} days"]
            else:
                base_where = "WHERE datetime_iso >= date('now', ?)"
                params = [f"-{days} days"]

            row = conn.execute(f"""
                SELECT 
                    COUNT(*) as total_bookings,
                    COUNT(DISTINCT user_id) as unique_clients,
                    AVG(duration_min) as avg_duration,
                    AVG(rating) as avg_rating,
                    COUNT(rating) as rated_bookings
                FROM bookings
                {base_where}
            """, params).fetchone()

            return {
                "period_days": days,
                "master": master,
                "total_bookings": row["total_bookings"] or 0,
                "unique_clients": row["unique_clients"] or 0,
                "avg_duration_min": round(row["avg_duration"] or 0, 1),
                "avg_rating": round(row["avg_rating"], 2) if row["avg_rating"] else None,
                "rated_bookings": row["rated_bookings"] or 0,
            }

    def get_top_masters(self, days: int = 30, limit: int = 10) -> list[dict]:
        """Which masters are getting the most bookings recently."""
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT master, COUNT(*) as bookings, AVG(rating) as avg_rating
                FROM bookings
                WHERE datetime_iso >= date('now', ?)
                GROUP BY master
                ORDER BY bookings DESC
                LIMIT ?
            """, (f"-{days} days", limit)).fetchall()

            return [
                {
                    "master": r["master"],
                    "bookings": r["bookings"],
                    "avg_rating": round(r["avg_rating"], 2) if r["avg_rating"] else None,
                }
                for r in rows
            ]

    def get_weekly_summary_data(self, weeks_back: int = 1) -> dict[str, Any]:
        """
        Prepares structured data that can be fed to LLM for weekly reports.
        Returns clients activity, top masters, ratings distribution, etc.
        """
        with get_connection() as conn:
            # Bookings in the last N weeks
            rows = conn.execute("""
                SELECT 
                    date(datetime_iso) as day,
                    master,
                    COUNT(*) as count,
                    AVG(rating) as avg_rating
                FROM bookings
                WHERE datetime_iso >= date('now', ?)
                GROUP BY day, master
                ORDER BY day
            """, (f"-{weeks_back*7} days",)).fetchall()

            return {
                "period_weeks": weeks_back,
                "daily_breakdown": [dict(row) for row in rows],
                "top_masters": self.get_top_masters(days=weeks_back*7, limit=5),
                "overall_stats": self.get_bookings_stats(days=weeks_back*7),
            }

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "master": row["master"],
            "services": _json_loads(row["services"], []),
            "services_raw": row["services_raw"] or "",
            "datetime": row["datetime_text"],
            "datetime_iso": row["datetime_iso"],
            "duration_min": row["duration_min"],
            "phone": row["phone"],
            "status": row["status"],
            "calendar_event_id": row["calendar_event_id"],
            "promo_code": row["promo_code"],
            "notes": _json_loads(row["notes"], []),
            "rating": row["rating"],
            "review_text": row["review_text"],
            "reminder_job_ids": _json_loads(row["reminder_job_ids"], []),
            # payment_* добавлены в schema v2 — safe-доступ для старых строк
            "payment_status": row["payment_status"] if "payment_status" in keys else "unpaid",
            "payment_amount": row["payment_amount"] if "payment_amount" in keys else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def set_payment(self, booking_id: str, status: str, amount: int | None = None) -> bool:
        """Отметить оплату записи. status: cash | card | unpaid."""
        fields: dict[str, Any] = {"payment_status": status}
        if amount is not None:
            fields["payment_amount"] = int(amount)
        return self.update_booking(booking_id, **fields)

    def get_unpaid_bookings(self, master: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Записи прошедших визитов с непогашенной оплатой (для /debts).
        Берём completed/no_show либо confirmed с прошедшим временем."""
        with get_connection() as conn:
            query = """SELECT * FROM bookings
                       WHERE (payment_status = 'unpaid' OR payment_status IS NULL)
                         AND status IN ('completed', 'confirmed')
                         AND datetime_iso < ?"""
            params: list[Any] = [datetime.utcnow().isoformat()]
            if master:
                query += " AND master = ?"
                params.append(master)
            query += " ORDER BY datetime_iso DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Convenient Facade
# ---------------------------------------------------------------------------

class Database:
    """
    High-level database access point.

    This is the main entry point for long-term client and booking data.

    Example usage for AI analytics:
        db = Database()
        profile = db.clients.get_full_client_profile(user_id)
        stats = db.bookings.get_bookings_stats(days=7)
        masters = db.bookings.get_top_masters(days=30)
    """

    def __init__(self):
        self.clients = ClientsRepository()
        self.bookings = BookingsRepository()

    def repair_client_visits(self, user_id: int) -> int:
        """Convenience method to repair a client's visit counter."""
        return self.clients.repair_visits(user_id)

    def full_repair_user(self, user_id: int, repair_bonuses: bool = True) -> dict:
        """
        Performs a full repair for a user: visits + optionally design bonuses.
        Returns a rich dict with before/after values and audit info.
        This is the single recommended entry point for admin repairs.
        """
        result = {"user_id": user_id, "success": True, "errors": []}

        # Repair visits
        old_visits = None
        try:
            client = self.clients.get_client(user_id)
            if client:
                old_visits = client.get("total_visits")
        except Exception as e:
            result["errors"].append(f"Could not read old visits: {e}")

        try:
            new_visits = self.clients.repair_visits(user_id)
            result["visits"] = {"old": old_visits, "new": new_visits}
        except Exception as e:
            result["success"] = False
            result["errors"].append(f"Visits repair failed: {e}")

        if repair_bonuses:
            try:
                used = self.bookings.count_design_bonuses_used(user_id)
                brought = self.clients.get_referrals_brought(user_id)
                correct_bonuses = max(0, brought - used)

                self.clients.update_referral_info(user_id, design_bonuses=correct_bonuses)
                result["bonuses"] = {
                    "brought": brought,
                    "used": used,
                    "corrected_to": correct_bonuses
                }
            except Exception as e:
                result["success"] = False
                result["errors"].append(f"Bonuses repair failed: {e}")

        return result

    def init(self) -> None:
        """Initialize the database (create tables, etc.). Safe to call on startup."""
        from .connection import init_db
        init_db()

    def close(self) -> None:
        from .connection import close_connection
        close_connection()

