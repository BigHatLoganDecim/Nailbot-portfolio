"""
Команды мастера: /today, /tomorrow, /week, /income, /block.
Плюс callback'и отмены/разблокировки слота из расписания мастера.

Внешний API:
  - cmd_today, cmd_tomorrow, cmd_week, cmd_income — для меню (menu:today и т.д.)
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import time

from telebot import types

log = logging.getLogger("nailbot.handlers.master")


def register(ctx) -> dict:
    """
    ctx должен содержать:
      bot, state_store, scheduler,
      master_by_telegram_id, fetch_events_for_range, calendar_service,
      now_moscow, db (optional).
    """
    bot = ctx.bot
    state_store = ctx.state_store
    scheduler = ctx.scheduler
    db = getattr(ctx, "db", None)  # Long-term database

    def _send_master_events(chat_id: int, title: str, events: list[dict], master) -> None:
        """Отправляет события мастера отдельными сообщениями с кнопкой отмены для каждого."""
        if not events:
            bot.send_message(chat_id, f"<b>{title}</b>\n\nЗаписей нет.", parse_mode="HTML")
            return
        if title:
            bot.send_message(chat_id, f"<b>{title}</b>", parse_mode="HTML")
        now = ctx.now_moscow()
        for e in events:
            start_iso = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "")
            try:
                dt = _dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                when = dt.strftime("%d.%m %H:%M") if "T" in start_iso else dt.strftime("%d.%m весь день")
                is_past = dt < now
            except Exception:
                when = start_iso
                is_past = False
            summary = e.get("summary", "(без названия)")
            desc = (e.get("description", "") or "").strip()
            if len(desc) > 600:
                desc = desc[:600] + "…"

            text = f"<b>{when}</b> — {summary}"
            if desc:
                text += f"\n\n{desc}"

            markup = None
            event_id = e.get("id", "")
            if event_id and "Telegram ID" in desc:
                m_id = re.search(r"Telegram ID клиента:\s*(\d+)", desc)
                client_id = m_id.group(1) if m_id else None
                markup = types.InlineKeyboardMarkup(row_width=2)
                row = []
                if client_id:
                    row.append(types.InlineKeyboardButton("🪪 Карточка",
                                                           callback_data=f"card:{client_id}"))
                row.append(types.InlineKeyboardButton("❌ Отменить",
                                                        callback_data=f"mc:{event_id}"))
                markup.row(*row)
                # «Не пришёл» — только для прошедших записей
                if is_past:
                    markup.add(types.InlineKeyboardButton(
                        "👻 Не пришёл",
                        callback_data=f"mns:{event_id}",
                    ))
            elif event_id and summary.startswith("🚫"):
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton(
                    "🔓 Снять блокировку",
                    callback_data=f"mu:{event_id}",
                ))

            try:
                bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            except Exception as e_send:
                log.warning("master events: ошибка отправки: %s", e_send)
            time.sleep(0.05)

    def _send_master_appointments(chat_id: int, title: str, rows: list[dict]) -> None:
        if not rows:
            bot.send_message(chat_id, f"<b>{title}</b>\n\nЗаписей нет.", parse_mode="HTML")
            return
        bot.send_message(chat_id, f"<b>{title}</b>", parse_mode="HTML")
        now = ctx.now_moscow()
        for row in rows:
            try:
                dt = _dt.datetime.fromisoformat(row["start_at"].replace("Z", "+00:00"))
                when = dt.astimezone(now.tzinfo).strftime("%d.%m %H:%M")
                is_past = dt < now
            except Exception:
                when = row.get("start_at") or "—"
                is_past = False
            services = ", ".join(row.get("services") or []) or "—"
            name = row.get("client_name") or "клиент"
            phone = row.get("client_phone") or "—"
            st = row.get("status") or ""
            text = (
                f"<b>{when}</b> — {st}\n"
                f"{name} · {phone}\n"
                f"{services}"
            )
            markup = types.InlineKeyboardMarkup(row_width=2)
            aid = row.get("id") or ""
            uid = row.get("client_tg_id")
            buttons = []
            if uid:
                buttons.append(types.InlineKeyboardButton("🪪 Карточка", callback_data=f"card:{uid}"))
            if st in ("held", "pending_payment", "confirmed"):
                buttons.append(types.InlineKeyboardButton("❌ Отменить", callback_data=f"pa:c:{aid}"))
            if buttons:
                markup.row(*buttons)
            if st == "confirmed":
                markup.row(
                    types.InlineKeyboardButton("✅ Пришёл", callback_data=f"pa:d:{aid}"),
                    types.InlineKeyboardButton("👻 Не пришёл", callback_data=f"pa:n:{aid}"),
                )
            elif st == "no_show":
                markup.add(types.InlineKeyboardButton("✅ Всё-таки пришёл", callback_data=f"pa:d:{aid}"))
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            time.sleep(0.05)

    def _master_range(master, start, end, title: str, chat_id: int) -> None:
        try:
            from database.pg import pg_enabled
            from database.appointments import list_for_master
            if pg_enabled():
                rows = list_for_master(master.name, start, end)
                _send_master_appointments(chat_id, title, rows)
                return
        except Exception as e:
            log.warning("master list pg: %s", e)
        events = ctx.fetch_events_for_range(master, start, end)
        _send_master_events(chat_id, title, events, master)

    @bot.message_handler(commands=["today", "сегодня"])
    def cmd_today(message: types.Message) -> None:
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        now = ctx.now_moscow()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + _dt.timedelta(days=1)
        _master_range(master, start, end, f"📅 Сегодня — {master.name}", message.chat.id)

    @bot.message_handler(commands=["tomorrow", "завтра"])
    def cmd_tomorrow(message: types.Message) -> None:
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        now = ctx.now_moscow()
        start = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + _dt.timedelta(days=1)
        _master_range(master, start, end, f"📅 Завтра — {master.name}", message.chat.id)

    @bot.message_handler(commands=["week", "неделя"])
    def cmd_week(message: types.Message) -> None:
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        now = ctx.now_moscow()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + _dt.timedelta(days=7)
        _master_range(master, start, end, f"📅 Ближайшие 7 дней — {master.name}", message.chat.id)

    def _sched_month_text(master_name: str, year: int, month: int, mmap: dict) -> str:
        from calendar import monthrange, month_name
        names = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
                 "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
        last = monthrange(year, month)[1]
        work_days = sum(1 for d in range(1, last + 1) if mmap.get(f"{year:04d}-{month:02d}-{d:02d}"))
        return (
            f"🗓 <b>График — {master_name}</b>\n"
            f"{names[month]} {year}: рабочих дней {work_days}/{last}\n"
            f"🟢 точка = рабочий. Нажми день — слоты 10/12/14/16/18/20.\n"
            f"<i>Как только правишь месяц здесь, knowledge.md для него больше не действует.</i>"
        )

    def _sched_month_kb(year: int, month: int, mmap: dict) -> types.InlineKeyboardMarkup:
        from calendar import monthrange, weekday
        last = monthrange(year, month)[1]
        kb_ = types.InlineKeyboardMarkup(row_width=7)
        kb_.row(*[types.InlineKeyboardButton(x, callback_data="gs:noop") for x in
                  ("пн", "вт", "ср", "чт", "пт", "сб", "вс")])
        # pad first week
        pad = weekday(year, month, 1)  # Mon=0
        row = [types.InlineKeyboardButton(" ", callback_data="gs:noop") for _ in range(pad)]
        for d in range(1, last + 1):
            key = f"{year:04d}-{month:02d}-{d:02d}"
            hours = mmap.get(key) or []
            label = f"{d}{'·' if hours else ''}"
            row.append(types.InlineKeyboardButton(label, callback_data=f"gs:d:{key}"))
            if len(row) == 7:
                kb_.row(*row)
                row = []
        if row:
            while len(row) < 7:
                row.append(types.InlineKeyboardButton(" ", callback_data="gs:noop"))
            kb_.row(*row)
        prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
        next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
        kb_.row(
            types.InlineKeyboardButton("◀️", callback_data=f"gs:m:{prev_y:04d}-{prev_m:02d}"),
            types.InlineKeyboardButton("▶️", callback_data=f"gs:m:{next_y:04d}-{next_m:02d}"),
        )
        kb_.row(
            types.InlineKeyboardButton("Будни полные", callback_data=f"gs:fill:{year:04d}-{month:02d}"),
            types.InlineKeyboardButton("Копия прошлого", callback_data=f"gs:copy:{year:04d}-{month:02d}"),
        )
        kb_.add(types.InlineKeyboardButton("◀️ Меню мастера", callback_data="menu:master"))
        return kb_

    def cmd_edit_schedule(message: types.Message) -> None:
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        now = ctx.now_moscow()
        _show_sched_month(message.chat.id, master, now.year, now.month)

    def _show_sched_month(chat_id: int, master, year: int, month: int, message_id: int | None = None) -> None:
        from database.pg import pg_enabled
        from database.appointments import month_work_map
        if not pg_enabled():
            bot.send_message(chat_id, "График в боте — после Postgres (DATABASE_URL).")
            return
        mmap = month_work_map(master.name, year, month)
        text = _sched_month_text(master.name, year, month, mmap)
        kb_ = _sched_month_kb(year, month, mmap)
        if message_id:
            try:
                bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                      reply_markup=kb_, parse_mode="HTML")
                return
            except Exception:
                pass
        bot.send_message(chat_id, text, reply_markup=kb_, parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("gs:"))
    def on_schedule_grid(call: types.CallbackQuery) -> None:
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Только для мастера")
            return
        parts = (call.data or "").split(":")
        kind = parts[1] if len(parts) > 1 else ""
        if kind == "noop":
            bot.answer_callback_query(call.id)
            return
        actor = f"master:{call.from_user.id}"
        try:
            from database.appointments import (
                SLOT_HOURS, copy_month_schedule, fill_weekdays,
                hours_on_date, month_work_map, set_hours_on_date,
            )
        except Exception as e:
            bot.answer_callback_query(call.id, str(e), show_alert=True)
            return

        if kind == "m" and len(parts) >= 3:
            y, m = parts[2].split("-")
            bot.answer_callback_query(call.id)
            _show_sched_month(call.message.chat.id, master, int(y), int(m), call.message.message_id)
            return
        if kind == "fill" and len(parts) >= 3:
            y, m = parts[2].split("-")
            fill_weekdays(master.name, int(y), int(m), actor)
            bot.answer_callback_query(call.id, "Будни — полный день")
            _show_sched_month(call.message.chat.id, master, int(y), int(m), call.message.message_id)
            return
        if kind == "copy" and len(parts) >= 3:
            y, m = int(parts[2].split("-")[0]), int(parts[2].split("-")[1])
            sy, sm = (y, m - 1) if m > 1 else (y - 1, 12)
            n = copy_month_schedule(master.name, sy, sm, y, m, actor)
            bot.answer_callback_query(call.id, f"Скопировано, рабочих дней: {n}")
            _show_sched_month(call.message.chat.id, master, y, m, call.message.message_id)
            return
        if kind == "d" and len(parts) >= 3:
            day = _dt.date.fromisoformat(parts[2])
            hours = hours_on_date(master.name, day)
            if hours is None:
                hours = list(SLOT_HOURS)
            bot.answer_callback_query(call.id)
            kb_ = types.InlineKeyboardMarkup(row_width=3)
            row = []
            for h in SLOT_HOURS:
                mark = "✅" if h in hours else "·"
                row.append(types.InlineKeyboardButton(
                    f"{mark}{h:02d}", callback_data=f"gs:h:{day.isoformat()}:{h}",
                ))
            kb_.row(*row[:3])
            kb_.row(*row[3:])
            kb_.row(
                types.InlineKeyboardButton("Весь день", callback_data=f"gs:full:{day.isoformat()}"),
                types.InlineKeyboardButton("Выходной", callback_data=f"gs:off:{day.isoformat()}"),
            )
            kb_.add(types.InlineKeyboardButton("◀️ К месяцу", callback_data=f"gs:m:{day.year:04d}-{day.month:02d}"))
            hrs = ", ".join(f"{h:02d}:00" for h in hours) or "выходной"
            try:
                bot.edit_message_text(
                    f"🗓 <b>{day.strftime('%d.%m.%Y')}</b> — {master.name}\nСлоты: {hrs}",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=kb_, parse_mode="HTML",
                )
            except Exception:
                pass
            return
        if kind in ("h", "full", "off") and len(parts) >= 3:
            day = _dt.date.fromisoformat(parts[2])
            cur = hours_on_date(master.name, day) or []
            if kind == "full":
                cur = list(SLOT_HOURS)
            elif kind == "off":
                cur = []
            else:
                h = int(parts[3])
                cur = list(cur)
                if h in cur:
                    cur.remove(h)
                else:
                    cur.append(h)
            set_hours_on_date(master.name, day, cur, actor)
            bot.answer_callback_query(call.id, "Сохранил")
            hours = hours_on_date(master.name, day) or []
            kb2 = types.InlineKeyboardMarkup(row_width=3)
            row2 = []
            for hh in SLOT_HOURS:
                mark = "✅" if hh in hours else "·"
                row2.append(types.InlineKeyboardButton(
                    f"{mark}{hh:02d}", callback_data=f"gs:h:{day.isoformat()}:{hh}",
                ))
            kb2.row(*row2[:3])
            kb2.row(*row2[3:])
            kb2.row(
                types.InlineKeyboardButton("Весь день", callback_data=f"gs:full:{day.isoformat()}"),
                types.InlineKeyboardButton("Выходной", callback_data=f"gs:off:{day.isoformat()}"),
            )
            kb2.add(types.InlineKeyboardButton("◀️ К месяцу", callback_data=f"gs:m:{day.year:04d}-{day.month:02d}"))
            hrs = ", ".join(f"{h:02d}:00" for h in hours) or "выходной"
            try:
                bot.edit_message_text(
                    f"🗓 <b>{day.strftime('%d.%m.%Y')}</b> — {master.name}\nСлоты: {hrs}",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=kb2, parse_mode="HTML",
                )
            except Exception:
                pass
            return
        bot.answer_callback_query(call.id)

    def _hours_for_day(master, day: _dt.date) -> list[int]:
        kb = ctx.kb
        fixed = [int(h) for h in (kb.settings.get("fixed_slot_hours") or (10, 12, 14, 16, 18, 20))]
        oh = getattr(master, "open_slots", None) or {}
        key = day.isoformat()
        if key in oh and oh[key]:
            raw = {int(x) for x in oh[key]}
            return [h for h in fixed if h in raw] or sorted(raw)
        return list(fixed)

    def cmd_free_slots(message: types.Message) -> None:
        """Свободные окна сегодня — тап закрывает слот (как в MastApp)."""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        now = ctx.now_moscow()
        day = now.date()
        tz = now.tzinfo
        hours = _hours_for_day(master, day)
        day_start = _dt.datetime.combine(day, _dt.time(0, 0), tzinfo=tz)
        day_end = day_start + _dt.timedelta(days=1)
        busy = []
        try:
            from database.pg import pg_enabled
            from database.appointments import busy_intervals
            if pg_enabled():
                busy = busy_intervals(master.name, day_start, day_end)
            elif getattr(ctx, "get_busy_intervals", None) and master.calendar_id:
                svc = ctx.calendar_service()
                if svc:
                    busy = ctx.get_busy_intervals(
                        svc, master.calendar_id, day_start, day_end,
                        master_name=master.name,
                    )
        except Exception as e:
            log.warning("free_slots busy: %s", e)
        from services.calendar import slot_is_free
        markup = types.InlineKeyboardMarkup(row_width=3)
        free_btns = []
        for h in hours:
            slot = _dt.datetime.combine(day, _dt.time(h, 0), tzinfo=tz)
            if slot < now:
                continue
            end = slot + _dt.timedelta(hours=1)
            if busy and not slot_is_free(busy, slot, end):
                continue
            free_btns.append(
                types.InlineKeyboardButton(
                    f"{h:02d}:00",
                    callback_data=f"fs:c:{day.isoformat()}:{h}",
                )
            )
        if not free_btns:
            bot.send_message(
                message.chat.id,
                f"🟢 На {day.strftime('%d.%m')} свободных окон нет — всё занято или день закрыт.",
            )
            return
        for i in range(0, len(free_btns), 3):
            markup.row(*free_btns[i:i + 3])
        markup.add(types.InlineKeyboardButton("🚫 Закрыть весь день", callback_data="menu:off_today"))
        bot.send_message(
            message.chat.id,
            f"🟢 <b>Свободно {day.strftime('%d.%m')}</b>\nНажми час, чтобы закрыть окно.",
            reply_markup=markup,
            parse_mode="HTML",
        )

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("fs:c:"))
    def on_close_free_slot(call: types.CallbackQuery) -> None:
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Только для мастера")
            return
        try:
            _, _, day_s, hour_s = call.data.split(":")
            day = _dt.date.fromisoformat(day_s)
            hour = int(hour_s)
        except Exception:
            bot.answer_callback_query(call.id, "Некорректные данные")
            return
        start_t = _dt.time(hour, 0)
        end_hour = min(hour + 2, 23)
        end_t = _dt.time(end_hour, 0)
        try:
            from database.pg import pg_enabled
            from database.appointments import close_window
            if not pg_enabled():
                bot.answer_callback_query(call.id, "Нужен Postgres (DATABASE_URL)", show_alert=True)
                return
            close_window(
                master_name=master.name,
                on_date=day,
                start_local=start_t,
                end_local=end_t,
                actor=f"master:{call.from_user.id}",
                reason="close_slot",
            )
            bot.answer_callback_query(call.id, f"Закрыто {hour:02d}:00")
            try:
                bot.edit_message_text(
                    f"✅ Окно {day.strftime('%d.%m')} {hour:02d}:00 закрыто. Клиенты его не увидят.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
            except Exception:
                pass
        except Exception as e:
            log.error("close free slot: %s", e)
            bot.answer_callback_query(call.id, "Не удалось закрыть", show_alert=True)

    @bot.message_handler(commands=["off_today", "не_работаю"])
    def cmd_off_today(message: types.Message) -> None:
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        try:
            from database.pg import pg_enabled
            from database.appointments import close_window
            if not pg_enabled():
                bot.send_message(
                    message.chat.id,
                    "Чтобы закрыть день в боте без Google, нужен DATABASE_URL (Postgres).",
                )
                return
            today = ctx.now_moscow().date()
            close_window(
                master_name=master.name,
                on_date=today,
                start_local=_dt.time(0, 0),
                end_local=_dt.time(23, 59),
                actor=f"master:{message.chat.id}",
                reason="off_today",
            )
            bot.send_message(
                message.chat.id,
                f"✅ {today.isoformat()}: вы не работаете. Клиенты не запишутся на этот день.",
            )
        except Exception as e:
            log.error("off_today: %s", e)
            bot.send_message(message.chat.id, f"Не удалось закрыть день: {e}")

    @bot.message_handler(commands=["week_stats", "статистика"])
    def cmd_week_stats(message: types.Message) -> None:
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        try:
            from database.pg import pg_enabled
            from database.appointments import week_stats
            if not pg_enabled():
                bot.send_message(message.chat.id, "Статистика недели — после включения Postgres.")
                return
            now = ctx.now_moscow()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + _dt.timedelta(days=7)
            c = week_stats(master.name, start, end)
            bot.send_message(
                message.chat.id,
                (
                    f"📊 <b>Неделя — {master.name}</b>\n"
                    f"Записи: {c.get('confirmed', 0)}\n"
                    f"Пришли: {c.get('completed', 0)}\n"
                    f"Отмены: {c.get('cancelled', 0)}\n"
                    f"Неявки: {c.get('no_show', 0)}"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            log.error("week_stats: %s", e)
            bot.send_message(message.chat.id, f"Не удалось посчитать: {e}")

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("pa:"))
    def on_pg_appointment_action(call: types.CallbackQuery) -> None:
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Только для мастера")
            return
        parts = (call.data or "").split(":", 2)
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Некорректные данные")
            return
        action, aid = parts[1], parts[2]
        status_map = {"c": "cancelled", "n": "no_show", "d": "completed"}
        new_status = status_map.get(action)
        if not new_status:
            bot.answer_callback_query(call.id, "Неизвестное действие")
            return
        try:
            from database.appointments import transition
            appt = transition(aid, new_status, actor=f"master:{call.from_user.id}")
            if new_status == "cancelled" and getattr(appt, "google_event_id", None):
                try:
                    from services.calendar import delete_event, get_service
                    svc = get_service()
                    cal = master.calendar_id
                    if svc and cal:
                        delete_event(svc, cal, appt.google_event_id)
                except Exception as e:
                    log.warning("master cancel calendar: %s", e)
            bot.answer_callback_query(call.id, f"Статус: {appt.status}")
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
            if new_status == "cancelled" and appt.client_tg_id:
                try:
                    bot.send_message(
                        appt.client_tg_id,
                        "Мастер отменил вашу запись. Выберите другое время через «Записаться».",
                    )
                except Exception:
                    pass
        except Exception as e:
            log.warning("pa action: %s", e)
            bot.answer_callback_query(call.id, "Нельзя так сменить статус", show_alert=True)

    def _is_master_or_admin(chat_id: int) -> bool:
        """True если chat_id — зарегистрированный мастер или админ."""
        return bool(ctx.master_by_telegram_id(chat_id)) or (chat_id in ctx.config.admin_ids)

    @bot.message_handler(commands=["salon_today", "салон_сегодня"])
    def cmd_salon_today(message: types.Message) -> None:
        """Расписание всего салона на сегодня — доступно обоим мастерам и админу."""
        if not _is_master_or_admin(message.chat.id):
            return
        now = ctx.now_moscow()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + _dt.timedelta(days=1)
        weekday_names = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
        header = f"🏢 <b>Салон сегодня — {weekday_names[now.weekday()]}, {now.strftime('%d.%m.%Y')}</b>"
        bot.send_message(message.chat.id, header, parse_mode="HTML")
        any_sent = False
        for master in ctx.kb.masters.values():
            cal_id = (master.calendar_id or "").strip()
            if not cal_id or "заполнить" in cal_id.lower():
                continue
            try:
                events = ctx.fetch_events_for_range(master, start, end)
            except Exception as e:
                bot.send_message(message.chat.id,
                    f"⚠️ {master.name}: ошибка чтения календаря")
                log.warning("cmd_salon_today: calendar error %s: %s", master.name, e)
                continue
            booking_events = [e for e in events
                              if not (e.get("summary","") or "").startswith("🚫")]
            count = len(booking_events)
            noun = ("запись" if count == 1
                    else "записи" if count in (2,3,4) else "записей")
            section_title = (f"👤 <b>{master.name}</b> — {count} {noun}"
                             if count else f"👤 <b>{master.name}</b> — свободно ☕")
            _send_master_events(message.chat.id, section_title, booking_events, master)
            any_sent = True
        if not any_sent:
            bot.send_message(message.chat.id, "Календари мастеров не настроены.")

    @bot.message_handler(commands=["salon_week", "салон_неделя"])
    def cmd_salon_week(message: types.Message) -> None:
        """Расписание всего салона на 7 дней — доступно обоим мастерам и админу."""
        if not _is_master_or_admin(message.chat.id):
            return
        now = ctx.now_moscow()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + _dt.timedelta(days=7)
        header = f"🏢 <b>Салон — ближайшие 7 дней</b> (с {start.strftime('%d.%m')} по {(end - _dt.timedelta(days=1)).strftime('%d.%m')})"
        bot.send_message(message.chat.id, header, parse_mode="HTML")
        any_sent = False
        for master in ctx.kb.masters.values():
            cal_id = (master.calendar_id or "").strip()
            if not cal_id or "заполнить" in cal_id.lower():
                continue
            try:
                events = ctx.fetch_events_for_range(master, start, end)
            except Exception as e:
                bot.send_message(message.chat.id,
                    f"⚠️ {master.name}: ошибка чтения календаря")
                log.warning("cmd_salon_week: calendar error %s: %s", master.name, e)
                continue
            booking_events = [e for e in events
                              if not (e.get("summary","") or "").startswith("🚫")]
            count = len(booking_events)
            noun = ("запись" if count == 1
                    else "записи" if count in (2,3,4) else "записей")
            section_title = (f"👤 <b>{master.name}</b> — {count} {noun} на неделю"
                             if count else f"👤 <b>{master.name}</b> — на неделе свободно")
            _send_master_events(message.chat.id, section_title, booking_events, master)
            any_sent = True
        if not any_sent:
            bot.send_message(message.chat.id, "Календари мастеров не настроены.")

    @bot.message_handler(commands=["income", "доход"])
    def cmd_income(message: types.Message) -> None:
        """Финансовый отчёт мастеру (только свой). Считается из календарных событий."""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return

        now = ctx.now_moscow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + _dt.timedelta(days=1)
        # Понедельник этой недели
        week_start = today_start - _dt.timedelta(days=now.weekday())
        week_end = week_start + _dt.timedelta(days=7)
        # Первое число текущего месяца
        month_start = today_start.replace(day=1)
        if month_start.month == 12:
            next_month = month_start.replace(year=month_start.year + 1, month=1)
        else:
            next_month = month_start.replace(month=month_start.month + 1)
        # Прошлый месяц для сравнения
        if month_start.month == 1:
            prev_month_start = month_start.replace(year=month_start.year - 1, month=12)
        else:
            prev_month_start = month_start.replace(month=month_start.month - 1)

        price_re = re.compile(r"•\s*([^—\n]+?)\s+—\s+(?:от\s+)?([\d\s]+?)\s*₽", re.MULTILINE)

        def collect(start: _dt.datetime, end: _dt.datetime) -> dict:
            events = ctx.fetch_events_for_range(master, start, end)
            total = 0
            visits = 0
            services_count: dict[str, int] = {}
            for e in events:
                summary = e.get("summary", "") or ""
                if summary.startswith("🚫"):
                    continue
                desc = e.get("description", "") or ""
                if "Telegram ID" not in desc:
                    continue
                visits += 1
                for m in price_re.finditer(desc):
                    name = m.group(1).strip()
                    try:
                        price = int(m.group(2).replace(" ", ""))
                        total += price
                        services_count[name] = services_count.get(name, 0) + 1
                    except ValueError:
                        pass
            return {"total": total, "visits": visits, "services": services_count}

        today = collect(today_start, today_end)
        week = collect(week_start, week_end)
        month = collect(month_start, next_month)
        prev_month = collect(prev_month_start, month_start)

        top_services = sorted(month["services"].items(), key=lambda x: -x[1])[:5]
        top_str = "\n".join(f"   • {n}: {c}×" for n, c in top_services) or "   —"

        avg = (month["total"] / month["visits"]) if month["visits"] else 0
        if prev_month["total"] > 0:
            days_in_current = max((now - month_start).days, 1)
            days_in_prev = max((month_start - prev_month_start).days, 1)
            projected_month = month["total"] * (days_in_prev / days_in_current)
            delta = (projected_month - prev_month["total"]) / prev_month["total"] * 100
            delta_str = (f"   ~{'+' if delta >= 0 else ''}{delta:.0f}% к прошлому месяцу "
                         f"(прогноз по темпу за {days_in_current} дн.)")
        else:
            delta_str = ""

        text = (
            f"💰 <b>Заработок — {master.name}</b>\n\n"
            f"<b>Сегодня:</b> {today['total']:,} ₽ ({today['visits']} визитов)\n".replace(",", " ") +
            f"<b>На этой неделе:</b> {week['total']:,} ₽ ({week['visits']} визитов)\n".replace(",", " ") +
            f"<b>В этом месяце:</b> {month['total']:,} ₽ ({month['visits']} визитов)\n".replace(",", " ") +
            (f"{delta_str}\n" if delta_str else "") +
            f"<b>Средний чек:</b> {avg:,.0f} ₽\n\n".replace(",", " ") +
            f"<b>Топ услуг в этом месяце:</b>\n{top_str}\n\n"
            f"<i>Считается из календарных событий по описаниям. "
            f"Учтены только записи через бота — наличные мимо бота не учтены.</i>"
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML")

    @bot.message_handler(commands=["block", "заблокировать"])
    def cmd_block(message: types.Message) -> None:
        """Мастер блокирует слот: /block 14:00 завтра обед"""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        text = (message.text or "").split(maxsplit=1)
        args = text[1].strip() if len(text) > 1 else ""
        if not args:
            bot.send_message(
                message.chat.id,
                "Используйте: <code>/block &lt;дата и время&gt; [причина]</code>\n\n"
                "Примеры:\n"
                "• <code>/block завтра 14:00 обед</code>\n"
                "• <code>/block 5 июня 10:00 личное</code>\n"
                "• <code>/block сегодня 18:00</code>\n\n"
                "Длительность по умолчанию — 1 час. Клиенты не смогут записаться на это время.",
                parse_mode="HTML",
            )
            return

        try:
            import dateparser  # type: ignore
            parsed_dt = dateparser.parse(
                args, languages=["ru"],
                settings={"PREFER_DATES_FROM": "future", "TIMEZONE": "Europe/Moscow",
                           "RETURN_AS_TIMEZONE_AWARE": True, "RELATIVE_BASE": ctx.now_moscow()},
            )
        except Exception as e:
            log.warning("/block: dateparser упал: %s", e)
            parsed_dt = None

        if not parsed_dt:
            bot.send_message(message.chat.id, "Не понял дату. Пример: /block завтра 14:00 обед")
            return

        reason = "Заблокировано"
        m_after_time = re.search(r"\d{1,2}[:.]\d{2}\s*(.+)$", args)
        if m_after_time and m_after_time.group(1).strip():
            reason = m_after_time.group(1).strip()

        end_dt = parsed_dt + _dt.timedelta(hours=1)
        blocked_in_bot = False
        try:
            from database.pg import pg_enabled
            from database.appointments import close_window
            if pg_enabled():
                close_window(
                    master_name=master.name,
                    on_date=parsed_dt.date(),
                    start_local=parsed_dt.time().replace(tzinfo=None),
                    end_local=end_dt.time().replace(tzinfo=None),
                    actor=f"master:{message.chat.id}",
                    reason=reason,
                )
                blocked_in_bot = True
        except Exception as e:
            log.error("/block: Postgres time_off failed: %s", e)
            bot.send_message(message.chat.id, f"Не удалось закрыть окно в боте: {e}")
            return

        cal_ok = False
        try:
            from database.pg import calendar_write_enabled
            write_cal = calendar_write_enabled()
        except Exception:
            write_cal = not blocked_in_bot
        if write_cal:
            svc = ctx.calendar_service()
            if svc and master.calendar_id and "заполнить" not in (master.calendar_id or "").lower():
                event = {
                    "summary": f"🚫 {reason}",
                    "description": "Слот заблокирован мастером через бота.",
                    "start": {"dateTime": parsed_dt.isoformat(), "timeZone": "Europe/Moscow"},
                    "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Moscow"},
                }
                try:
                    svc.events().insert(calendarId=master.calendar_id, body=event).execute()
                    cal_ok = True
                except Exception as e:
                    log.error("/block: calendar insert: %s", e)

        if blocked_in_bot:
            extra = " Календарь тоже обновлён." if cal_ok else ""
            bot.send_message(
                message.chat.id,
                f"✅ Слот закрыт в боте: {parsed_dt.strftime('%d.%m %H:%M')} ({reason})."
                f"{extra}\nКлиенты не смогут записаться в это время.",
            )
            return
        if cal_ok:
            bot.send_message(
                message.chat.id,
                f"✅ Слот заблокирован: {parsed_dt.strftime('%d.%m %H:%M')} ({reason}).\n"
                "Клиенты не смогут записаться в это время.",
            )
            return
        bot.send_message(
            message.chat.id,
            "Не могу закрыть слот: нет Postgres и календарь не настроен. "
            "Задайте DATABASE_URL или calendar_id.",
        )

    @bot.message_handler(commands=["add_booking", "записать"])
    def cmd_add_booking(message: types.Message) -> None:
        """
        Записать клиента «по телефону» (walk-in), без Telegram-бота.

        Формат:
            /add_booking <телефон> | <имя> | <когда> | <услуги>

        Пример:
            /add_booking +79991234567 | Анна | завтра 14:00 | маникюр

        Логика:
          - Только мастер пишет, только в свой календарь.
          - Создаёт событие в Google Calendar с описанием в формате бота,
            но БЕЗ «Telegram ID клиента:» — потому что клиента в TG ещё нет.
          - sync_calendar такие события не подхватит (нет TG ID), поэтому
            дубля в DB не создаётся.
          - Запись будет видна в /today, /week, /salon_*.
          - Напоминания клиенту НЕ шлются (некуда — нет TG).
        """
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return

        usage_hint = (
            "📞 <b>Записать клиента вручную</b>\n\n"
            "Формат:\n"
            "<code>/add_booking телефон | имя | когда | услуги</code>\n\n"
            "Примеры:\n"
            "• <code>/add_booking +79991234567 | Анна | завтра 14:00 | маникюр</code>\n"
            "• <code>/add_booking 89991234567 | Ольга К | 5 июня 11:00 | педикюр</code>\n"
            "• <code>/add_booking +79991234567 | Маша | 27.05 16:30 | маникюр, дизайн</code>\n\n"
            "Бот создаст событие в твоём календаре. Клиенту напоминания "
            "не шлются (нет Telegram), но запись будет видна в /today и /week."
        )

        text = (message.text or "").split(maxsplit=1)
        args_raw = text[1].strip() if len(text) > 1 else ""
        if not args_raw:
            bot.send_message(message.chat.id, usage_hint, parse_mode="HTML")
            return

        parts = [p.strip() for p in args_raw.split("|")]
        if len(parts) < 4:
            bot.send_message(
                message.chat.id,
                "❌ Нужно 4 части через <code>|</code> — телефон, имя, когда, услуги.\n\n"
                + usage_hint,
                parse_mode="HTML",
            )
            return
        phone_raw, name, when_raw, services_raw = parts[0], parts[1], parts[2], "|".join(parts[3:])

        # Нормализуем телефон — оставляем цифры и ведущий +
        phone_digits = re.sub(r"[^\d+]", "", phone_raw)
        if not re.match(r"^\+?\d{10,15}$", phone_digits):
            bot.send_message(message.chat.id,
                "❌ Не распознал телефон. Пример: +79991234567 или 89991234567.")
            return
        if not name:
            bot.send_message(message.chat.id, "❌ Не указано имя клиента.")
            return

        # Парсим datetime через dateparser (как в /block)
        try:
            import dateparser  # type: ignore
            parsed_dt = dateparser.parse(
                when_raw, languages=["ru"],
                settings={"PREFER_DATES_FROM": "future", "TIMEZONE": "Europe/Moscow",
                           "RETURN_AS_TIMEZONE_AWARE": True, "RELATIVE_BASE": ctx.now_moscow()},
            )
        except Exception as e:
            log.warning("/add_booking: dateparser упал: %s", e)
            parsed_dt = None
        if not parsed_dt:
            bot.send_message(message.chat.id,
                f"❌ Не понял время «{when_raw}». Примеры: «завтра 14:00», «5 июня 11:00», «27.05 16:30».")
            return
        if parsed_dt < ctx.now_moscow():
            bot.send_message(message.chat.id, "❌ Это время уже в прошлом.")
            return

        # Парсим услуги — ищем известные имена в kb.services
        kb = ctx.kb
        known: list[str] = []
        services_tokens = [s.strip() for s in re.split(r"[,;/+]", services_raw) if s.strip()]
        for token in services_tokens:
            tl = token.lower()
            match = next((s for s in kb.services if s.name.lower() == tl), None)
            if not match:
                # подстрочный матч
                match = next((s for s in kb.services if tl in s.name.lower() or s.name.lower() in tl), None)
            if match:
                known.append(match.name)
        if not known:
            # Не нашли совпадений — берём raw как одну услугу
            known = [services_raw.strip()]
            log.info("/add_booking: услуги «%s» не нашлись в kb.services — пишем raw",
                     services_raw)

        # Длительность
        try:
            duration_min = ctx.total_duration_minutes(known, master.name)
        except Exception:
            duration_min = kb.settings.get("default_service_duration_min", 60)
        if not duration_min:
            duration_min = 60
        end_dt = parsed_dt + _dt.timedelta(minutes=int(duration_min))

        # Проверка свободного слота
        svc = ctx.calendar_service()
        cal_id = (master.calendar_id or "").strip()
        if not svc or not cal_id or "заполнить" in cal_id.lower():
            bot.send_message(message.chat.id, "❌ Календарь не настроен — записать не могу.")
            return

        try:
            day_start = parsed_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end   = day_start + _dt.timedelta(days=1)
            busy = ctx.get_busy_intervals(
                svc, cal_id, day_start, day_end, master_name=master.name,
            )
            if not ctx.slot_is_free(busy, parsed_dt, end_dt):
                bot.send_message(message.chat.id,
                    f"⚠️ Слот {parsed_dt.strftime('%d.%m %H:%M')} занят. "
                    "Открой /today чтобы посмотреть расписание.")
                return
        except Exception as e:
            log.warning("/add_booking: slot check failed: %s — продолжаю без проверки", e)

        # Формируем событие. Описание — БЕЗ «Telegram ID клиента:»: sync_calendar
        # его специально не подцепит, дубля в DB не будет.
        # [Мастер] в summary — shared-календарь: /today и busy-check фильтруют по тегу.
        services_str = ", ".join(known)
        description = (
            f"Запись по телефону через мастера ({master.name}).\n"
            f"Мастер: {master.name}\n"
            f"Имя клиента: {name}\n"
            f"Телефон клиента: {phone_digits}\n"
            f"Услуги: {services_str}\n"
            f"Длительность: {duration_min} мин"
        )
        emoji = kb.brand.get("emoji", "✨") if hasattr(kb, "brand") else "✨"
        event = {
            "summary": f"[{master.name}] {emoji} {services_str} — {name}",
            "description": description,
            "start": {"dateTime": parsed_dt.isoformat(), "timeZone": "Europe/Moscow"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/Moscow"},
        }
        try:
            result = svc.events().insert(calendarId=cal_id, body=event).execute()
            event_id = result.get("id", "")
            log.info("/add_booking: создал event=%s master=%s phone=%s when=%s",
                     event_id, master.name, phone_digits,
                     parsed_dt.strftime("%d.%m %H:%M"))
        except Exception as e:
            log.exception("/add_booking: insert failed: %s", e)
            bot.send_message(message.chat.id, f"❌ Не удалось создать событие: {e}")
            return

        bot.send_message(
            message.chat.id,
            f"✅ <b>Записал</b>\n\n"
            f"👤 {name}\n"
            f"📞 {phone_digits}\n"
            f"📅 {parsed_dt.strftime('%d.%m.%Y %H:%M')} ({duration_min} мин)\n"
            f"💅 {services_str}\n\n"
            f"<i>Событие в твоём календаре. Напоминания клиенту не отправляем — "
            f"нет Telegram. Если клиент позже зайдёт в бота, его новые записи "
            f"подцепятся отдельно.</i>",
            parse_mode="HTML",
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("mc:"))
    def on_master_cancel(call: types.CallbackQuery) -> None:
        """Мастер отменяет запись клиента из /today /tomorrow /week."""
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Команда только для мастеров")
            return
        event_id = call.data.split(":", 1)[1]

        svc = ctx.calendar_service()
        if not svc or not master.calendar_id:
            bot.answer_callback_query(call.id, "Календарь не настроен")
            return

        try:
            event = svc.events().get(calendarId=master.calendar_id, eventId=event_id).execute()
        except Exception as e:
            log.warning("master cancel: get event failed: %s", e)
            bot.answer_callback_query(call.id, "Событие не найдено")
            return

        desc = event.get("description", "") or ""
        m = re.search(r"Telegram ID клиента:\s*(\d+)", desc)
        client_user_id: int | None = int(m.group(1)) if m else None

        try:
            svc.events().delete(calendarId=master.calendar_id, eventId=event_id).execute()
        except Exception as e:
            log.error("master cancel: delete failed: %s", e)
            bot.answer_callback_query(call.id, "Не удалось удалить событие")
            return

        notified = False
        if client_user_id:
            try:
                client_state = state_store.get(client_user_id) or {}
                bookings = client_state.get("bookings", [])
                cancelled = None
                new_bookings = []
                for b in bookings:
                    if b.get("calendar_event_id") == event_id:
                        cancelled = b
                    else:
                        new_bookings.append(b)
                client_state["bookings"] = new_bookings
                state_store.set(client_user_id, client_state)

                # Persist cancellation to long-term DB (important for history and analytics)
                if db is not None and cancelled and cancelled.get("id"):
                    try:
                        # decrement ДО mark cancelled — иначе only_if_confirmed пропускает единственную запись
                        db.clients.decrement_visits(client_user_id, reason="master_cancel")
                        db.bookings.mark_booking_status(cancelled["id"], "cancelled")

                        # Restore design bonus on master-initiated cancellation (fairness)
                        notes_str = " ".join(cancelled.get("notes") or [])
                        if kb.referral.get("referrer_bonus_master_note", "") in notes_str or "дизайн-бонус" in notes_str.lower():
                            db.clients.update_referral_info(client_user_id, increment_design_bonuses=1)
                            log.info("master_cancel: restored design bonus for user=%s", client_user_id)
                    except Exception as e:
                        log.error("Failed to mark booking cancelled in long-term DB (master cancel): %s", e)

                if cancelled:
                    for job_id in cancelled.get("reminder_job_ids") or []:
                        try:
                            if scheduler:
                                scheduler.remove_job(job_id)
                        except Exception:
                            pass
                    if cancelled.get("review_job_id"):
                        try:
                            if scheduler:
                                scheduler.remove_job(cancelled["review_job_id"])
                        except Exception:
                            pass

                when_str = (cancelled or {}).get("datetime") or "запланированное время"
                try:
                    bot.send_message(
                        client_user_id,
                        f"❌ Ваша запись ({when_str} — мастер {master.name}) "
                        "была отменена мастером.\n"
                        "Свяжитесь с нами для уточнения причин или новой записи.",
                    )
                    notified = True
                except Exception as e:
                    log.warning("master cancel: notify client %s failed: %s",
                                client_user_id, e)
            except Exception as e:
                log.warning("master cancel: client cleanup failed: %s", e)

        if notified:
            bot.answer_callback_query(call.id, "Запись отменена, клиент уведомлён")
        elif client_user_id:
            bot.answer_callback_query(call.id, "Запись отменена, но уведомить клиента не удалось")
        else:
            bot.answer_callback_query(call.id, "Запись отменена (ID клиента не найден)")
        try:
            bot.edit_message_text(
                (call.message.text or "") + "\n\n❌ <b>ОТМЕНЕНО МАСТЕРОМ</b>",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda c: c.data.startswith("mu:"))
    def on_master_unblock(call: types.CallbackQuery) -> None:
        """Мастер снимает блокировку слота (удаляет событие 🚫)."""
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Команда только для мастеров")
            return
        event_id = call.data.split(":", 1)[1]
        svc = ctx.calendar_service()
        if not svc or not master.calendar_id:
            bot.answer_callback_query(call.id, "Календарь не настроен")
            return
        try:
            svc.events().delete(calendarId=master.calendar_id, eventId=event_id).execute()
        except Exception as e:
            bot.answer_callback_query(call.id, f"Не удалось снять: {e}")
            return
        bot.answer_callback_query(call.id, "Блок снят")
        try:
            bot.edit_message_text(
                (call.message.text or "") + "\n\n🔓 <b>БЛОК СНЯТ</b>",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda c: c.data.startswith("mns:"))
    def on_master_no_show(call: types.CallbackQuery) -> None:
        """Мастер отмечает, что клиент не пришёл (no-show).

        Защита от двойного клика:
          1. Проверка что нажавший — мастер этого события (master_by_telegram_id +
             события только из своего календаря).
          2. Идемпотентность: если запись уже no_show — ack "уже отмечено", без
             повторного decrement_visits и без приклеивания дубля "НЕ ПРИШЁЛ".
          3. Если запись cancelled/rescheduled — отказ с понятным сообщением.
        """
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Команда только для мастеров")
            return
        event_id = call.data.split(":", 1)[1]

        # Авторизация: событие должно быть из календаря именно этого мастера.
        # Если мастер нажал "Не пришёл" на чужую запись — event не найдётся
        # в её calendar_id, ловим 404 и отказываем.
        svc = ctx.calendar_service()
        if not svc or not master.calendar_id:
            bot.answer_callback_query(call.id, "Календарь не настроен")
            return
        try:
            event = svc.events().get(calendarId=master.calendar_id, eventId=event_id).execute()
        except Exception as e:
            log.warning("master no-show: get event failed (event=%s, master=%s): %s",
                        event_id, master.name, e)
            bot.answer_callback_query(call.id, "Событие не найдено", show_alert=True)
            return

        desc = event.get("description", "") or ""
        m = re.search(r"Telegram ID клиента:\s*(\d+)", desc)
        client_user_id: int | None = int(m.group(1)) if m else None
        if not client_user_id:
            bot.answer_callback_query(call.id, "В событии нет Telegram ID клиента",
                                       show_alert=True)
            return

        # === Идемпотентность через DB-статус ===
        # Главная защита от двойного клика. Любые изменения статуса делаем
        # только если текущий статус — confirmed/completed.
        matched_id: str | None = None
        current_status: str | None = None
        if db is not None:
            try:
                existing = db.bookings.get_booking_by_calendar_event_id(event_id)
                if existing:
                    matched_id = existing.get("id")
                    current_status = existing.get("status")
            except Exception as e:
                log.warning("master no-show: DB lookup failed: %s", e)

        if current_status == "no_show":
            bot.answer_callback_query(call.id, "Уже отмечено как 'не пришёл'")
            # На случай если предыдущий edit_message_text упал — убираем кнопки
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
            return

        if current_status in ("cancelled", "rescheduled"):
            bot.answer_callback_query(
                call.id,
                f"Нельзя — запись уже '{current_status}'",
                show_alert=True,
            )
            return

        # === Применяем no_show ===
        # Порядок важен: сначала меняем статус (теперь нет 'confirmed' для этой
        # записи), потом decrement_visits с only_if_confirmed=True — это
        # дополнительный страховочный слой, не даст словить отрицательный visits
        # при гонке.
        try:
            client_state = state_store.get(client_user_id) or {}
            bookings = client_state.get("bookings") or []
            matched = next((b for b in bookings
                            if b.get("calendar_event_id") == event_id), None)

            if db is not None and matched_id:
                try:
                    db.bookings.mark_booking_status(matched_id, "no_show")
                    db.clients.decrement_visits(client_user_id, reason="no_show")
                    log.info("master no-show: booking %s marked no_show, user=%s",
                             matched_id, client_user_id)
                except Exception as e:
                    log.error("master no-show: DB update failed: %s", e)

            # Снимаем напоминания/review-prompt — уже не нужны
            if matched:
                for job_id in matched.get("reminder_job_ids") or []:
                    try:
                        if scheduler:
                            scheduler.remove_job(job_id)
                    except Exception:
                        pass
                if matched.get("review_job_id"):
                    try:
                        if scheduler:
                            scheduler.remove_job(matched["review_job_id"])
                    except Exception:
                        pass
        except Exception as e:
            log.warning("master no-show: state update failed: %s", e)

        bot.answer_callback_query(call.id, "Отмечено: не пришёл")
        try:
            bot.edit_message_text(
                (call.message.text or "") + "\n\n👻 <b>НЕ ПРИШЁЛ</b>",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=None, parse_mode="HTML",
            )
        except Exception:
            pass

    # ── Утренний дайджест ────────────────────────────────────────────────────

    def send_morning_digest() -> None:
        """
        Отправляет каждому мастеру расписание на сегодня.
        Вызывается APScheduler в 9:00 МСК через shim _morning_digest в main.py.
        Также доступна через /digest_now (только для админа).
        """
        now = ctx.now_moscow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end   = today_start + _dt.timedelta(days=1)

        weekday_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        today_name = weekday_names[now.weekday()]
        today_str  = f"{today_name.capitalize()}, {now.strftime('%d.%m.%Y')}"

        sent_count = 0
        for master in ctx.kb.masters.values():
            tg_id = ctx.normalize_tg_id(master.telegram_id)
            if not tg_id:
                log.warning("morning_digest: нет telegram_id у мастера %s", master.name)
                continue

            events: list[dict] = []
            cal_ok = False
            cal_id = (master.calendar_id or "").strip()
            if cal_id and "заполнить" not in cal_id.lower():
                svc = ctx.calendar_service()
                if svc:
                    try:
                        events = ctx.fetch_events_for_range(master, today_start, today_end)
                        cal_ok = True
                    except Exception as e:
                        log.warning("morning_digest: calendar error for %s: %s", master.name, e)

            # Фильтруем блокировки — они не запись клиента
            bookings = [e for e in events
                        if not (e.get("summary", "") or "").startswith("🚫")]
            blocked  = [e for e in events
                        if (e.get("summary", "") or "").startswith("🚫")]

            # ── Заголовок ────────────────────────────────────────────────
            greet = f"☀️ Доброе утро, <b>{master.name}</b>!"
            if bookings:
                header = (
                    f"{greet}\n"
                    f"<i>{today_str}</i>\n\n"
                    f"Сегодня <b>{len(bookings)} {'запись' if len(bookings)==1 else 'записи' if len(bookings) in (2,3,4) else 'записей'}</b>:"
                )
            elif not cal_ok:
                header = (
                    f"{greet}\n"
                    f"<i>{today_str}</i>\n\n"
                    f"📵 Календарь пока не отвечает — расписание недоступно.\n"
                    f"Проверьте через /today"
                )
            else:
                header = (
                    f"{greet}\n"
                    f"<i>{today_str}</i>\n\n"
                    f"Записей нет ☕ Свободный день!"
                )

            try:
                bot.send_message(tg_id, header, parse_mode="HTML")
            except Exception as e:
                log.error("morning_digest: не смог отправить мастеру %s: %s", master.name, e)
                continue

            # ── Список записей ───────────────────────────────────────────
            if bookings:
                _send_master_events(tg_id, "", bookings, master)

            # ── Блокировки (кратко, отдельным сообщением) ────────────────
            if blocked:
                bl_lines = [f"🚫 <b>Заблокировано сегодня:</b>"]
                for e in blocked:
                    iso = (e.get("start") or {}).get("dateTime", "")
                    try:
                        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                        bl_lines.append(f"   • {dt.strftime('%H:%M')} — {e.get('summary','?')}")
                    except Exception:
                        bl_lines.append(f"   • {e.get('summary','?')}")
                try:
                    bot.send_message(tg_id, "\n".join(bl_lines), parse_mode="HTML")
                except Exception:
                    pass

            sent_count += 1
            time.sleep(0.2)  # небольшой throttle между мастерами

        log.info("morning_digest: отправлено %d/%d мастерам", sent_count, len(ctx.kb.masters))

    # ── Вечерний дайджест ────────────────────────────────────────────────────

    def send_evening_digest() -> None:
        """
        Отправляет каждому мастеру превью завтрашнего расписания в 20:00 МСК.
        Вызывается APScheduler через shim _evening_digest в main.py.
        Также доступна через /digest_evening (только для админа).
        """
        now = ctx.now_moscow()
        tomorrow_start = (now + _dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        tomorrow_end = tomorrow_start + _dt.timedelta(days=1)

        weekday_names = ["понедельник", "вторник", "среда", "четверг",
                         "пятница", "суббота", "воскресенье"]
        tomorrow_name = weekday_names[tomorrow_start.weekday()]
        tomorrow_str = f"{tomorrow_name.capitalize()}, {tomorrow_start.strftime('%d.%m.%Y')}"

        sent_count = 0
        for master in ctx.kb.masters.values():
            tg_id = ctx.normalize_tg_id(master.telegram_id)
            if not tg_id:
                log.warning("evening_digest: нет telegram_id у мастера %s", master.name)
                continue

            events: list[dict] = []
            cal_ok = False
            cal_id = (master.calendar_id or "").strip()
            if cal_id and "заполнить" not in cal_id.lower():
                svc = ctx.calendar_service()
                if svc:
                    try:
                        events = ctx.fetch_events_for_range(master, tomorrow_start, tomorrow_end)
                        cal_ok = True
                    except Exception as e:
                        log.warning("evening_digest: calendar error for %s: %s", master.name, e)

            bookings = [e for e in events
                        if not (e.get("summary", "") or "").startswith("🚫")]
            blocked  = [e for e in events
                        if (e.get("summary", "") or "").startswith("🚫")]

            # ── Заголовок ────────────────────────────────────────────────────
            greet = f"🌆 <b>{master.name}</b>, превью на завтра:"
            if bookings:
                cnt = len(bookings)
                noun = ("запись" if cnt == 1
                        else "записи" if cnt in (2, 3, 4)
                        else "записей")
                header = (
                    f"{greet}\n"
                    f"<i>{tomorrow_str}</i>\n\n"
                    f"Завтра <b>{cnt} {noun}</b> 📋"
                )
            elif not cal_ok:
                header = (
                    f"{greet}\n"
                    f"<i>{tomorrow_str}</i>\n\n"
                    f"📵 Календарь не отвечает — расписание недоступно.\n"
                    f"Проверьте через /tomorrow"
                )
            else:
                header = (
                    f"{greet}\n"
                    f"<i>{tomorrow_str}</i>\n\n"
                    f"Завтра пока свободно 🎉"
                )

            try:
                bot.send_message(tg_id, header, parse_mode="HTML")
            except Exception as e:
                log.error("evening_digest: не смог отправить мастеру %s: %s", master.name, e)
                continue

            # ── Список записей ───────────────────────────────────────────────
            if bookings:
                _send_master_events(tg_id, "", bookings, master)

            # ── Блокировки (кратко) ──────────────────────────────────────────
            if blocked:
                bl_lines = ["🚫 <b>Заблокировано:</b>"]
                for e in blocked:
                    iso = (e.get("start") or {}).get("dateTime", "")
                    try:
                        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                        bl_lines.append(f"   • {dt.strftime('%H:%M')} — {e.get('summary','?')}")
                    except Exception:
                        bl_lines.append(f"   • {e.get('summary','?')}")
                try:
                    bot.send_message(tg_id, "\n".join(bl_lines), parse_mode="HTML")
                except Exception:
                    pass

            sent_count += 1
            time.sleep(0.2)

        log.info("evening_digest: отправлено %d/%d мастерам", sent_count, len(ctx.kb.masters))

    def _booking_total_price(booking: dict) -> int:
        """Ожидаемая сумма записи в рублях (Postgres, иначе knowledge.md)."""
        from services.catalog import preferred_master_name, price_minor_total
        master_name = booking.get("master") or preferred_master_name(ctx.kb)
        return price_minor_total(
            booking.get("services") or [],
            master_name,
            list(ctx.kb.services),
        ) // 100

    @bot.message_handler(commands=["debts", "долги", "неоплачено"])
    def cmd_debts(message: types.Message) -> None:
        """Список неоплаченных прошедших визитов с кнопками отметки оплаты."""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        if db is None:
            bot.send_message(message.chat.id, "База недоступна.")
            return
        unpaid = db.bookings.get_unpaid_bookings(master=master.name, limit=30)
        if not unpaid:
            bot.send_message(message.chat.id, "✅ Все визиты оплачены — должников нет.")
            return

        bot.send_message(message.chat.id,
                         f"💰 <b>Неоплаченные визиты ({len(unpaid)}):</b>",
                         parse_mode="HTML")
        for b in unpaid:
            services_str = ", ".join(b.get("services") or []) or b.get("services_raw") or "визит"
            amount = _booking_total_price(b)
            when = b.get("datetime") or b.get("datetime_iso") or ""
            phone = b.get("phone") or ""
            text = (f"📅 {when}\n💅 {services_str}\n"
                    f"💵 Сумма: <b>{amount} ₽</b>" + (f"\n📞 {phone}" if phone else ""))
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(
                types.InlineKeyboardButton("💵 Наличные", callback_data=f"pay:cash:{b['id']}"),
                types.InlineKeyboardButton("💳 Карта", callback_data=f"pay:card:{b['id']}"),
            )
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("pay:"))
    def on_mark_payment(call: types.CallbackQuery) -> None:
        """Мастер отмечает оплату: pay:cash:<id> / pay:card:<id>."""
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Только для мастеров")
            return
        if db is None:
            bot.answer_callback_query(call.id, "База недоступна")
            return
        try:
            _, method, booking_id = call.data.split(":", 2)
        except ValueError:
            bot.answer_callback_query(call.id, "Ошибка данных")
            return
        booking = db.bookings.get_booking(booking_id)
        if not booking:
            bot.answer_callback_query(call.id, "Запись не найдена", show_alert=True)
            return
        amount = _booking_total_price(booking)
        db.bookings.set_payment(booking_id, method, amount)
        label = "наличными" if method == "cash" else "картой"
        try:
            bot.edit_message_text(
                f"✅ Оплачено {label}: <b>{amount} ₽</b>\n"
                f"<s>{(call.message.text or '').split(chr(10))[0]}</s>",
                call.message.chat.id, call.message.message_id, parse_mode="HTML",
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"Отмечено: {label}, {amount} ₽")

    def _price_table_text(master_name: str, rows: list[dict]) -> str:
        lines = [
            f"💰 <b>Прайс — {master_name}</b>",
            "<i>Нажми номер услуги, чтобы поменять цену, минуты или вкл/выкл.</i>",
            "",
            "<pre>",
            f"{'#':>2} {'Услуга':<22} {'₽':>6} {'мин':>4}",
            "-" * 38,
        ]
        for i, s in enumerate(rows[:28], 1):
            title = (s.get("title") or "")[:22]
            rub = (s.get("price_minor") or 0) // 100
            mark = " " if s.get("is_active") else "×"
            lines.append(f"{i:>2}{mark}{title:<22} {rub:>6} {s.get('duration_min') or 0:>4}")
        lines.append("</pre>")
        lines.append("× = выключена, клиент её не увидит.")
        return "\n".join(lines)

    def _price_index_kb(rows: list[dict]) -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup(row_width=5)
        btns = [
            types.InlineKeyboardButton(str(i), callback_data=f"pr:o:{s['id']}")
            for i, s in enumerate(rows[:28], 1)
        ]
        for i in range(0, len(btns), 5):
            kb_.row(*btns[i:i + 5])
        return kb_

    def _price_item_kb(sid: str) -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup(row_width=3)
        kb_.row(
            types.InlineKeyboardButton("Вкл/выкл", callback_data=f"pr:t:{sid}"),
            types.InlineKeyboardButton("Цена", callback_data=f"pr:p:{sid}"),
            types.InlineKeyboardButton("Минуты", callback_data=f"pr:d:{sid}"),
        )
        kb_.add(types.InlineKeyboardButton("◀️ К списку", callback_data="pr:l:-"))
        return kb_

    def _price_waiting_kb() -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup()
        kb_.add(types.InlineKeyboardButton("❌ Отмена", callback_data="pr:x:-"))
        return kb_

    def _price_state(user_id: int) -> dict:
        if hasattr(ctx, "get_state"):
            return ctx.get_state(user_id)
        return ctx.state_store.get(user_id) or {}

    def _save_price_state(user_id: int, st: dict) -> None:
        if hasattr(ctx, "save_state"):
            ctx.save_state(user_id, st)
        else:
            ctx.state_store.set(user_id, st)

    def clear_price_edit(user_id: int) -> bool:
        """Сбросить незавершённую правку цены. True если что-то сбросили."""
        st = _price_state(user_id)
        if not st.get("pending_price_edit"):
            return False
        st.pop("pending_price_edit", None)
        _save_price_state(user_id, st)
        return True

    def _price_item_text(s: dict) -> str:
        rub = (s.get("price_minor") or 0) // 100
        mark = "включена" if s.get("is_active") else "выключена"
        return (
            f"💰 <b>{s.get('title')}</b>\n"
            f"{rub} ₽ · {s.get('duration_min') or 0} мин · {mark}\n\n"
            "Цена — напиши новое число в рублях.\n"
            "Минуты — длительность."
        )

    def cmd_edit_prices(message: types.Message) -> None:
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        try:
            from database.pg import pg_enabled
            from database.appointments import list_services
            if not pg_enabled():
                bot.send_message(
                    message.chat.id,
                    "Прайс в боте заработает после DATABASE_URL (Postgres).\n"
                    "Сейчас цены в knowledge.md — /prices.",
                )
                return
            rows = list_services(master.name)
        except Exception as e:
            log.error("edit prices: %s", e)
            bot.send_message(message.chat.id, f"Не удалось загрузить прайс: {e}")
            return
        if not rows:
            bot.send_message(message.chat.id, "Услуг в Postgres нет — перезапусти бота, чтобы посеять knowledge.md.")
            return
        bot.send_message(
            message.chat.id,
            _price_table_text(master.name, rows),
            reply_markup=_price_index_kb(rows),
            parse_mode="HTML",
        )

    @bot.callback_query_handler(func=lambda c: (c.data or "").startswith("pr:"))
    def on_price_action(call: types.CallbackQuery) -> None:
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Только для мастера")
            return
        parts = (call.data or "").split(":", 2)
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Ошибка")
            return
        action, sid = parts[1], parts[2]
        try:
            from database.appointments import list_services, update_service
        except Exception as e:
            bot.answer_callback_query(call.id, str(e), show_alert=True)
            return

        def _show_list() -> None:
            rows = list_services(master.name)
            bot.edit_message_text(
                _price_table_text(master.name, rows),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=_price_index_kb(rows),
                parse_mode="HTML",
            )

        def _show_item(row: dict) -> None:
            bot.edit_message_text(
                _price_item_text(row),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=_price_item_kb(row["id"]),
                parse_mode="HTML",
            )

        pending_now = _price_state(call.from_user.id).get("pending_price_edit")
        if pending_now and action not in ("x",):
            bot.answer_callback_query(
                call.id,
                "Сначала число в чат — или «Отмена» / меню, тогда правка не сохранится",
                show_alert=True,
            )
            return

        if action == "x":
            clear_price_edit(call.from_user.id)
            bot.answer_callback_query(call.id, "Правку отменил")
            rows = {r["id"]: r for r in list_services(master.name)}
            sid_old = (pending_now or {}).get("service_id")
            cur = rows.get(sid_old) if sid_old else None
            try:
                if cur:
                    _show_item(cur)
                else:
                    _show_list()
            except Exception:
                pass
            return
        if action == "l":
            clear_price_edit(call.from_user.id)
            bot.answer_callback_query(call.id)
            try:
                _show_list()
            except Exception:
                pass
            return
        if action == "o":
            rows = {r["id"]: r for r in list_services(master.name)}
            cur = rows.get(sid)
            if not cur:
                bot.answer_callback_query(call.id, "Услуга не найдена")
                return
            bot.answer_callback_query(call.id)
            try:
                _show_item(cur)
            except Exception:
                pass
            return
        if action == "t":
            rows = {r["id"]: r for r in list_services(master.name)}
            cur = rows.get(sid)
            if not cur:
                bot.answer_callback_query(call.id, "Услуга не найдена")
                return
            updated = update_service(sid, is_active=not cur["is_active"])
            bot.answer_callback_query(call.id, "Включено" if updated and updated["is_active"] else "Выключено")
            if updated:
                try:
                    _show_item(updated)
                except Exception:
                    pass
            return
        if action in ("p", "d"):
            field = "price_minor" if action == "p" else "duration_min"
            pending = {
                "field": field,
                "service_id": sid,
                "chat_id": call.message.chat.id,
                "message_id": call.message.message_id,
            }
            st = ctx.get_state(call.from_user.id) if hasattr(ctx, "get_state") else (ctx.state_store.get(call.from_user.id) or {})
            prev = st.get("pending_price_edit") or {}
            already = prev.get("service_id") == sid and prev.get("field") == field
            st["pending_price_edit"] = pending
            _save_price_state(call.from_user.id, st)
            if already:
                bot.answer_callback_query(call.id, "Уже жду число — напиши его в чат")
                return
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=_price_waiting_kb(),
                )
            except Exception:
                pass
            hint = (
                "Новая цена в рублях, только число. Например: 2500\n"
                "Другие кнопки прайса сейчас не сработают. Меню — выход без сохранения."
                if action == "p"
                else "Длительность в минутах, только число. Например: 90\n"
                "Другие кнопки прайса сейчас не сработают. Меню — выход без сохранения."
            )
            bot.send_message(call.message.chat.id, hint)
            return
        bot.answer_callback_query(call.id)

    def handle_price_edit_text(message: types.Message) -> bool:
        """True если это ввод цены/минут — тогда общий чат записи не трогаем."""
        uid = getattr(message.from_user, "id", None)
        if not uid or not ctx.master_by_telegram_id(uid):
            return False
        if hasattr(ctx, "get_state"):
            s = ctx.get_state(uid)
        else:
            s = ctx.state_store.get(uid) or {}
        pending = s.get("pending_price_edit")
        if not pending:
            return False
        raw = (message.text or "").strip()
        low = raw.lower()
        if any(k in low for k in ("отмена", "cancel", "меню", "menu", "записаться")):
            s.pop("pending_price_edit", None)
            _save_price_state(uid, s)
            if "отмена" in low or low == "cancel":
                bot.send_message(message.chat.id, "Правку отменил, ничего не сохранил.")
                return True
            return False
        raw_num = raw.replace(" ", "").replace("₽", "")
        if not raw_num.isdigit():
            bot.send_message(
                message.chat.id,
                "Жду целое число (например 2500). Либо «отмена» / меню — тогда не сохраню.",
            )
            return True
        raw = raw_num
        n = int(raw)
        field = pending.get("field")
        sid = pending.get("service_id")
        s.pop("pending_price_edit", None)
        if hasattr(ctx, "save_state"):
            ctx.save_state(uid, s)
        else:
            ctx.state_store.set(uid, s)
        try:
            from database.appointments import update_service
            if field == "price_minor":
                updated = update_service(sid, price_minor=n * 100)
            else:
                updated = update_service(sid, duration_min=max(10, min(480, n)))
        except Exception as e:
            bot.send_message(message.chat.id, f"Не сохранилось: {e}")
            return True
        if not updated:
            bot.send_message(message.chat.id, "Услуга не найдена.")
            return True
        rub = (updated["price_minor"] or 0) // 100
        bot.send_message(
            message.chat.id,
            f"✅ {updated['title']}: {rub} ₽ / {updated['duration_min']} мин "
            f"({'вкл' if updated['is_active'] else 'выкл'})",
        )
        chat_id = pending.get("chat_id")
        message_id = pending.get("message_id")
        if chat_id and message_id:
            try:
                bot.edit_message_text(
                    _price_item_text(updated),
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=_price_item_kb(updated["id"]),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return True

    return {
        "cmd_today": cmd_today,
        "cmd_tomorrow": cmd_tomorrow,
        "cmd_week": cmd_week,
        "cmd_free_slots": cmd_free_slots,
        "cmd_edit_schedule": cmd_edit_schedule,
        "cmd_edit_prices": cmd_edit_prices,
        "handle_price_edit_text": handle_price_edit_text,
        "clear_price_edit": clear_price_edit,
        "cmd_off_today": cmd_off_today,
        "cmd_week_stats": cmd_week_stats,
        "cmd_income": cmd_income,
        "cmd_salon_today": cmd_salon_today,
        "cmd_salon_week": cmd_salon_week,
        "cmd_debts": cmd_debts,
        "send_morning_digest": send_morning_digest,
        "send_evening_digest": send_evening_digest,
    }
