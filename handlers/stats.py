"""
/stats — статистика записей за периоды (только для админа).
/llm_usage — расход Groq за сегодня (для мастера и админа).
Трекинг Groq usage с алертами при 50/80/90% квоты — это колбэк к llm.usage_callback.

Внешний API:
  - track_groq_call(usage) — выставляется в llm.usage_callback после register().
"""

from __future__ import annotations

import datetime as _dt
import logging
import re

from telebot import types

log = logging.getLogger("nailbot.handlers.stats")


def register(ctx) -> dict:
    """
    ctx должен содержать:
      bot, kb, state_store, config,
      now_moscow, master_by_telegram_id, fetch_events_for_range, safe_send_long,
      _groq_request_limit(), _groq_token_limit().
    """
    bot = ctx.bot
    kb = ctx.kb
    state_store = ctx.state_store
    config = ctx.config
    # Лимиты Groq — динамика из kb.settings (для /reload без рестарта)
    def _groq_request_limit():
        return kb.settings["llm_daily_request_limit"]
    def _groq_token_limit():
        return kb.settings["llm_daily_token_limit"]

    def _check_groq_threshold(date: str, reqs: int, tokens_total: int) -> None:
        """Уведомить админа если пересечён один из порогов 50/80/90%."""
        if not state_store.redis:
            return
        req_pct = (reqs / _groq_request_limit()) * 100
        tok_pct = (tokens_total / _groq_token_limit()) * 100
        pct = max(req_pct, tok_pct)
        if pct < 50:
            return
        key = f"nailbot:groq:notified:{date}"
        try:
            last_notified = int(state_store.redis.get(key) or 0)
        except Exception:
            last_notified = 0
        new_threshold = 0
        if pct >= 90 and last_notified < 90:
            new_threshold = 90
        elif pct >= 80 and last_notified < 80:
            new_threshold = 80
        elif pct >= 50 and last_notified < 50:
            new_threshold = 50
        if not new_threshold:
            return
        try:
            state_store.redis.set(key, new_threshold, ex=60 * 60 * 30)
        except Exception:
            pass
        emoji = {50: "🟡", 80: "🟠", 90: "🔴"}.get(new_threshold, "⚠️")
        msg = (
            f"{emoji} <b>Groq: использовано ~{new_threshold}% дневной квоты</b>\n\n"
            f"Запросов: {reqs:,} / {_groq_request_limit():,} ({req_pct:.0f}%)\n"
            f"Токенов: {tokens_total:,} / {_groq_token_limit():,} ({tok_pct:.0f}%)\n"
            f"Дата: {date}\n\n"
            "Лимит сбросится завтра в 00:00 UTC."
        )
        for admin_id in config.admin_ids:
            try:
                bot.send_message(admin_id, msg, parse_mode="HTML")
            except Exception as e:
                log.debug("groq threshold notify admin %d: %s", admin_id, e)

    def track_groq_call(usage: dict) -> None:
        """Колбэк после каждого вызова Groq. Обновляет счётчики и шлёт алерты."""
        if not state_store.redis:
            return
        today = ctx.now_moscow().date().isoformat()
        in_t = int(usage.get("in") or 0)
        out_t = int(usage.get("out") or 0)
        try:
            pipe = state_store.redis.pipeline()
            pipe.incr(f"nailbot:groq:requests:{today}")
            pipe.incrby(f"nailbot:groq:tokens_in:{today}", in_t)
            pipe.incrby(f"nailbot:groq:tokens_out:{today}", out_t)
            for k in (f"nailbot:groq:requests:{today}",
                       f"nailbot:groq:tokens_in:{today}",
                       f"nailbot:groq:tokens_out:{today}"):
                pipe.expire(k, 60 * 60 * 24 * 7)
            results = pipe.execute()
            reqs_now = int(results[0])
            tok_total = (int(state_store.redis.get(f"nailbot:groq:tokens_in:{today}") or 0) +
                         int(state_store.redis.get(f"nailbot:groq:tokens_out:{today}") or 0))
            _check_groq_threshold(today, reqs_now, tok_total)
        except Exception as e:
            log.debug("track_groq_call: %s", e)

    @bot.message_handler(commands=["llm_usage", "groq", "квота"])
    def cmd_llm_usage(message: types.Message) -> None:
        """Текущая загрузка Groq: запросы и токены за сегодня. Доступно мастерам и админу."""
        user_id = message.chat.id  # в приватном боте chat.id == user.id; from_user — бот при вызове из меню
        is_master = bool(ctx.master_by_telegram_id(user_id))
        is_admin = user_id in config.admin_ids
        if not (is_master or is_admin):
            return
        if not state_store.redis:
            bot.send_message(message.chat.id,
                              "Redis не подключён — статистики LLM нет (счётчики живут в памяти процесса).")
            return
        today = ctx.now_moscow().date().isoformat()
        try:
            reqs = int(state_store.redis.get(f"nailbot:groq:requests:{today}") or 0)
            tok_in = int(state_store.redis.get(f"nailbot:groq:tokens_in:{today}") or 0)
            tok_out = int(state_store.redis.get(f"nailbot:groq:tokens_out:{today}") or 0)
        except Exception as e:
            log.warning("llm_usage: %s", e)
            reqs = tok_in = tok_out = 0
        tok_total = tok_in + tok_out
        req_pct = (reqs / _groq_request_limit()) * 100
        tok_pct = (tok_total / _groq_token_limit()) * 100

        def bar(pct: float, width: int = 12) -> str:
            pct = max(0.0, min(100.0, pct))
            filled = int(pct / 100 * width)
            return "█" * filled + "░" * (width - filled)

        text = (
            f"📊 <b>Использование Groq за сегодня</b>\n"
            f"<i>({today})</i>\n\n"
            f"<b>Запросы:</b> {reqs:,} / {_groq_request_limit():,} ({req_pct:.1f}%)\n"
            f"<code>{bar(req_pct)}</code>\n\n"
            f"<b>Токены:</b> {tok_total:,} / {_groq_token_limit():,} ({tok_pct:.1f}%)\n"
            f"<code>{bar(tok_pct)}</code>\n"
            f"   ↳ in: {tok_in:,}\n"
            f"   ↳ out: {tok_out:,}\n\n"
            f"<i>Лимит сбрасывается в 00:00 UTC. "
            f"Если перешагнём 50/80/90% — админ получит уведомление автоматически.</i>"
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML")

    @bot.message_handler(commands=["stats", "статистика"])
    def cmd_stats(message: types.Message) -> None:
        """Статистика записей: DB (всегда) + Calendar (если настроен). Только для админа."""
        if message.chat.id not in config.admin_ids:
            return

        now = ctx.now_moscow()
        db = getattr(ctx, "db", None)

        # ── Helpers ─────────────────────────────────────────────────────────
        def fmt_dict(d: dict, top: int = 5) -> str:
            if not d:
                return "   —"
            items = sorted(d.items(), key=lambda x: -x[1])[:top]
            return "\n".join(f"   • {k}: {v}" for k, v in items)

        def fmt_bar(d: dict, keys_order: list, label_fn=str, width: int = 12) -> str:
            if not d:
                return "   —"
            max_val = max((d.get(k, 0) for k in keys_order), default=0) or 1
            lines = []
            for k in keys_order:
                v = d.get(k, 0)
                bar_len = int(v / max_val * width)
                bar = "█" * bar_len + "░" * (width - bar_len)
                lines.append(f"   <code>{label_fn(k):<3} {bar}</code> {v}")
            return "\n".join(lines)

        def star_rating(avg: float | None, count: int) -> str:
            if avg is None or count == 0:
                return "—"
            return f"{'⭐' * round(avg)} {avg:.1f}/5 ({count} оц.)"

        # ── Секция 1: SQLite DB ──────────────────────────────────────────────
        db_lines: list[str] = []
        if db is not None:
            try:
                w7  = db.bookings.get_bookings_stats(days=7)
                w30 = db.bookings.get_bookings_stats(days=30)
                masters_7  = db.bookings.get_top_masters(days=7)
                masters_30 = db.bookings.get_top_masters(days=30)

                # Тренд недельный
                trend = ""
                if w30["total_bookings"] > 0:
                    ratio = (w7["total_bookings"] * 4) / w30["total_bookings"]
                    trend = ("📈" if ratio > 1.15 else "📉" if ratio < 0.85 else "➡️") + (
                        " выше нормы" if ratio > 1.15 else " ниже нормы" if ratio < 0.85 else " в норме"
                    )

                # Топ услуг из DB (из JSON-поля services)
                def _top_services(days: int, top: int = 5) -> dict[str, int]:
                    try:
                        import json as _j
                        from database.connection import get_connection
                        with get_connection() as conn:
                            rows = conn.execute("""
                                SELECT services FROM bookings
                                WHERE datetime_iso >= date('now', ?)
                                  AND status NOT IN ('cancelled','no_show')
                            """, (f"-{days} days",)).fetchall()
                        counts: dict[str, int] = {}
                        for r in rows:
                            for svc in (_j.loads(r[0] or "[]") or []):
                                svc = str(svc).strip()[:40]
                                if svc:
                                    counts[svc] = counts.get(svc, 0) + 1
                        return counts
                    except Exception:
                        return {}

                svc7  = _top_services(7)
                svc30 = _top_services(30)

                # Статусы за месяц
                def _status_counts(days: int) -> dict[str, int]:
                    try:
                        from database.connection import get_connection
                        with get_connection() as conn:
                            rows = conn.execute("""
                                SELECT status, COUNT(*) as cnt FROM bookings
                                WHERE datetime_iso >= date('now', ?)
                                GROUP BY status
                            """, (f"-{days} days",)).fetchall()
                        return {r[0]: r[1] for r in rows}
                    except Exception:
                        return {}

                st30 = _status_counts(30)
                confirmed30 = st30.get("confirmed", 0) + st30.get("completed", 0)
                cancelled30  = st30.get("cancelled", 0)
                cancel_pct   = f" (отмен: {cancelled30 / (confirmed30 + cancelled30) * 100:.0f}%)" if (confirmed30 + cancelled30) > 0 else ""

                m7_lines  = "\n".join(f"   • {m['master']}: {m['bookings']} зап."
                                      + (f", рейтинг {m['avg_rating']:.1f}⭐" if m['avg_rating'] else "")
                                      for m in masters_7) or "   —"
                m30_lines = "\n".join(f"   • {m['master']}: {m['bookings']} зап."
                                      + (f", рейтинг {m['avg_rating']:.1f}⭐" if m['avg_rating'] else "")
                                      for m in masters_30) or "   —"

                db_lines = [
                    f"<b>📊 Статистика (из БД бота)</b>",
                    f"<i>{now.strftime('%d.%m.%Y %H:%M')} МСК  {trend}</i>",
                    "",
                    f"<b>━ 7 дней ━</b>",
                    f"Записей через бота: <b>{w7['total_bookings']}</b>  |  клиентов: {w7['unique_clients']}",
                    f"По мастерам:\n{m7_lines}",
                    f"Рейтинг: {star_rating(w7['avg_rating'], w7['rated_bookings'])}",
                    f"Топ услуг:\n{fmt_dict(svc7, 4)}",
                    "",
                    f"<b>━ 30 дней ━</b>",
                    f"Записей: <b>{w30['total_bookings']}</b>{cancel_pct}  |  клиентов: {w30['unique_clients']}",
                    f"По мастерам:\n{m30_lines}",
                    f"Рейтинг: {star_rating(w30['avg_rating'], w30['rated_bookings'])}",
                    f"Топ услуг:\n{fmt_dict(svc30, 5)}",
                ]
            except Exception as e:
                log.warning("cmd_stats: DB error: %s", e)
                db_lines = ["<i>⚠️ БД временно недоступна</i>"]
        else:
            db_lines = ["<i>БД не инициализирована</i>"]

        # ── Секция 2: Google Calendar (если настроен) ────────────────────────
        cal_lines: list[str] = []
        try:
            svc = ctx.calendar_service()
            if svc:
                week_ago   = now - _dt.timedelta(days=7)
                month_ago  = now - _dt.timedelta(days=30)
                week_ahead = now + _dt.timedelta(days=7)

                def _cal_stats(start: _dt.datetime, end: _dt.datetime) -> dict:
                    by_hour: dict[int, int] = {}
                    by_weekday: dict[int, int] = {}
                    total = 0
                    blocked = 0
                    for m in kb.masters.values():
                        cid = (m.calendar_id or "").strip()
                        if not cid or "заполнить" in cid.lower():
                            continue
                        for e in ctx.fetch_events_for_range(m, start, end):
                            if (e.get("summary", "") or "").startswith("🚫"):
                                blocked += 1
                                continue
                            total += 1
                            iso = (e.get("start") or {}).get("dateTime", "")
                            try:
                                dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                                by_hour[dt.hour] = by_hour.get(dt.hour, 0) + 1
                                by_weekday[dt.weekday()] = by_weekday.get(dt.weekday(), 0) + 1
                            except Exception:
                                pass
                    return {"total": total, "blocked": blocked,
                            "by_hour": by_hour, "by_weekday": by_weekday}

                c7   = _cal_stats(week_ago, now)
                c_fwd = _cal_stats(now, week_ahead)
                weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
                hour_keys    = list(range(9, 22))
                weekday_keys = list(range(7))

                cal_lines = [
                    "",
                    f"<b>━ Google Calendar (7 дн.) ━</b>",
                    f"Событий: {c7['total']}  |  блокировок: {c7['blocked']}",
                    f"По часам:\n{fmt_bar(c7['by_hour'], hour_keys, lambda h: f'{h}ч')}",
                    f"По дням:\n{fmt_bar(c7['by_weekday'], weekday_keys, lambda d: weekdays[d])}",
                    "",
                    f"<b>━ Впереди (7 дн.) ━</b>",
                    f"Событий: <b>{c_fwd['total']}</b>",
                    f"По часам:\n{fmt_bar(c_fwd['by_hour'], hour_keys, lambda h: f'{h}ч')}",
                ]
        except Exception as e:
            log.warning("cmd_stats: Calendar error: %s", e)

        full_text = "\n".join(db_lines + cal_lines)
        ctx.safe_send_long(message.chat.id, full_text, parse_mode="HTML")

    return {
        "track_groq_call": track_groq_call,
        "cmd_stats": cmd_stats,
        "cmd_llm_usage": cmd_llm_usage,
    }
