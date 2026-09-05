"""
Болезнь и отпуск мастера — массовая отмена записей.

/sick (/болею) — отменяет всех на ближайший рабочий день, шлёт извинения.
/vacation 2026-06-15 2026-06-20 — ставит отпуск + отменяет записи в окне.
/vacation_clear — снимает все отпускные дни.

Внешний API:
  - cmd_sick — для меню (menu:sick)
  - get_vacation_dates(master_name) — для booking flow (исключаем эти даты из date-picker и resolve_booking_time)
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import logging
import re
import threading
import time

from telebot import types

log = logging.getLogger("nailbot.handlers.vacation_sick")


def register(ctx) -> dict:
    """
    ctx должен содержать:
      bot, state_store, scheduler,
      master_by_telegram_id, fetch_events_for_range, calendar_service,
      now_moscow, moscow_tz, db (optional).
    """
    bot = ctx.bot
    kb = ctx.kb  # Needed for referral bonus note detection inside _safe_cancel
    state_store = ctx.state_store
    scheduler = ctx.scheduler
    db = getattr(ctx, "db", None)  # Long-term database for bookings history

    def _safe_cancel_booking(event_id, client_id, master, svc, when_str, reason="mass_cancel"):
        """
        Safely cancels one booking with best-effort for every step.
        Returns rich dict with success flags + counters for accurate reporting.
        """
        result = {
            "calendar_deleted": False,
            "state_cleaned": False,
            "db_updated": False,
            "notified": False,
            "bonus_restored": False,
            "visits_decremented": False,
        }

        # 1. Try to delete from Google Calendar (best effort)
        if svc and master.calendar_id and event_id:
            try:
                svc.events().delete(calendarId=master.calendar_id, eventId=event_id).execute()
                result["calendar_deleted"] = True
            except Exception as ex:
                log.warning(f"{reason}: failed to delete event {event_id}: %s", ex)

        # 2. Clean Redis state + reminders + DB
        target_booking = None
        if client_id:
            try:
                cs = state_store.get(client_id) or {}
                bks = cs.get("bookings") or []
                target_booking = next((b for b in bks if b.get("calendar_event_id") == event_id), None)
                cs["bookings"] = [b for b in bks if b.get("calendar_event_id") != event_id]
                state_store.set(client_id, cs)
                result["state_cleaned"] = True

                if target_booking:
                    for jid in target_booking.get("reminder_job_ids") or []:
                        try:
                            if scheduler:
                                scheduler.remove_job(jid)
                        except Exception:
                            pass
                    if target_booking.get("review_job_id"):
                        try:
                            if scheduler:
                                scheduler.remove_job(target_booking["review_job_id"])
                        except Exception:
                            pass

                if db is not None and target_booking and target_booking.get("id"):
                    try:
                        # decrement ДО mark cancelled (иначе only_if_confirmed пропускает)
                        db.clients.decrement_visits(client_id, reason=reason)
                        result["visits_decremented"] = True
                        db.bookings.mark_booking_status(target_booking["id"], "cancelled")

                        # Restore design bonus if used
                        notes_str = " ".join(target_booking.get("notes") or [])
                        if kb.referral.get("referrer_bonus_master_note", "") in notes_str or "дизайн-бонус" in notes_str.lower():
                            db.clients.update_referral_info(client_id, increment_design_bonuses=1)
                            result["bonus_restored"] = True
                        result["db_updated"] = True
                    except Exception as ex:
                        log.error(f"{reason}: DB cleanup failed for booking %s: %s", target_booking.get("id"), ex)
            except Exception as ex:
                log.warning(f"{reason}: state cleanup failed for client %s: %s", client_id, ex)

        # 3. Notify client (best effort)
        if client_id:
            try:
                apology_kb = types.InlineKeyboardMarkup()
                apology_kb.add(types.InlineKeyboardButton("📅 Выбрать другую дату", callback_data="empty:book"))
                # Текст уведомления подбираем по причине отмены.
                # Раньше тут было «Мастер Аня — 15 июня» без глагола — клиент
                # не понимал, что именно ему сообщают.
                if reason.startswith("sick"):
                    notice = (
                        f"😔 <b>Извини, твою запись пришлось отменить.</b>\n\n"
                        f"Мастер <b>{master.name}</b> заболел(а) и не сможет принять тебя "
                        f"{when_str}.\n\n"
                        "Выбери удобную дату — будем рады видеть тебя в другой день."
                    )
                elif reason.startswith("vacation"):
                    notice = (
                        f"😔 <b>Извини, твою запись пришлось перенести.</b>\n\n"
                        f"Мастер <b>{master.name}</b> уходит в отпуск и не сможет принять тебя "
                        f"{when_str}.\n\n"
                        "Выбери удобную дату — будем рады видеть тебя в другой день."
                    )
                else:
                    notice = (
                        f"😔 <b>Извини, твою запись пришлось отменить.</b>\n\n"
                        f"Мастер <b>{master.name}</b> не сможет принять тебя {when_str}.\n\n"
                        "Выбери удобную дату — будем рады видеть тебя снова."
                    )
                bot.send_message(client_id, notice, reply_markup=apology_kb, parse_mode="HTML")
                result["notified"] = True
            except Exception as ex:
                log.warning(f"{reason}: failed to notify client %s: %s", client_id, ex)

        return result

    def _vacation_key(master_name: str) -> str:
        return f"nailbot:vacation:{master_name}"

    def get_vacation_dates(master_name: str) -> set[_dt.date]:
        """Множество дат, в которые мастер в отпуске."""
        if not state_store.redis:
            return set()
        try:
            raw = state_store.redis.get(_vacation_key(master_name))
            if not raw:
                return set()
            if isinstance(raw, bytes):
                raw = raw.decode()
            data = _json.loads(raw)
            out: set[_dt.date] = set()
            for d_iso in data.get("dates", []):
                try:
                    out.add(_dt.date.fromisoformat(d_iso))
                except Exception:
                    pass
            return out
        except Exception as e:
            log.warning("vacation get: %s", e)
            return set()

    def _set_vacation_dates(master_name: str, dates: set[_dt.date]) -> None:
        if not state_store.redis:
            return
        try:
            state_store.redis.set(
                _vacation_key(master_name),
                _json.dumps({"dates": sorted(d.isoformat() for d in dates)}),
            )
        except Exception as e:
            log.warning("vacation set: %s", e)

    def _add_vacation_range(master_name: str, start: _dt.date, end: _dt.date) -> int:
        existing = get_vacation_dates(master_name)
        added = 0
        cur = start
        while cur <= end:
            if cur not in existing:
                existing.add(cur)
                added += 1
            cur += _dt.timedelta(days=1)
        _set_vacation_dates(master_name, existing)
        return added

    def _bookings_for_master_on_date(master, date: _dt.date) -> list[dict]:
        """
        Возвращает записи КЛИЕНТОВ (не блокировки) на дату.

        Включает:
          - TG-клиентов (description содержит "Telegram ID клиента:")
          - Walk-in клиентов из /add_booking (description содержит
            "Телефон клиента:", но НЕТ Telegram ID — мастер занёс по телефону)

        Исключает:
          - Блокировки /block (summary начинается с 🚫)
          - Прочие пустые/служебные события без признаков клиента
        """
        tz = ctx.moscow_tz()
        start = _dt.datetime.combine(date, _dt.time(0, 0), tzinfo=tz)
        end = start + _dt.timedelta(days=1)
        events = ctx.fetch_events_for_range(master, start, end)
        result = []
        for e in events:
            desc = (e.get("description") or "")
            summary = (e.get("summary") or "")
            if summary.startswith("🚫"):
                continue  # блокировка — не отменяем
            if "Telegram ID" in desc or "Телефон клиента:" in desc:
                result.append(e)
        return result

    def _is_walk_in_event(event: dict) -> bool:
        """True если событие — walk-in (телефонный клиент без Telegram)."""
        desc = (event.get("description") or "")
        return "Telegram ID" not in desc and "Телефон клиента:" in desc

    def _pick_target_day(master):
        """Сегодня (если записи остались) → завтра → послезавтра. Иначе (None, [])."""
        now = ctx.now_moscow()
        today = now.date()
        for offset in range(0, 3):
            d = today + _dt.timedelta(days=offset)
            evs = _bookings_for_master_on_date(master, d)
            if d == today:
                future_evs = []
                for e in evs:
                    try:
                        iso = e.get("start", {}).get("dateTime", "")
                        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                        if dt > now:
                            future_evs.append(e)
                    except Exception:
                        future_evs.append(e)
                evs = future_evs
            if evs:
                return d, evs
        return None, []

    def _vacation_day_buttons(start: _dt.date, n: int, prefix: str) -> types.InlineKeyboardMarkup:
        months = ["янв", "фев", "мар", "апр", "май", "июн",
                  "июл", "авг", "сен", "окт", "ноя", "дек"]
        weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        kb_ = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for i in range(n):
            d = start + _dt.timedelta(days=i)
            label = f"{d.day} {months[d.month - 1]} ({weekdays[d.weekday()]})"
            buttons.append(types.InlineKeyboardButton(label, callback_data=f"{prefix}{d.isoformat()}"))
        for i in range(0, len(buttons), 2):
            kb_.row(*buttons[i:i + 2])
        kb_.add(types.InlineKeyboardButton("❌ Отмена", callback_data="vac:no"))
        return kb_

    def _send_vacation_start_picker(chat_id: int, prefix_text: str = "") -> None:
        today = ctx.now_moscow().date()
        bot.send_message(
            chat_id,
            (prefix_text + "🏖 С какого дня отпуск?").strip(),
            reply_markup=_vacation_day_buttons(today, 14, "vac:from:"),
        )

    def send_vacation_picker(chat_id: int, user_id: int) -> None:
        master = ctx.master_by_telegram_id(user_id)
        if not master:
            return
        current = sorted(d for d in get_vacation_dates(master.name) if d >= ctx.now_moscow().date())[:8]
        extra = ""
        if current:
            extra = "Уже стоит: " + ", ".join(d.isoformat() for d in current) + "\n\n"
        _send_vacation_start_picker(chat_id, extra)

    def _send_vacation_end_picker(chat_id: int, start_d: _dt.date, message_id: int | None) -> None:
        text = f"До какого дня включительно? (начало {start_d.isoformat()})"
        markup = _vacation_day_buttons(start_d, 14, f"vac:to:{start_d.isoformat()}:")
        try:
            if message_id:
                bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, reply_markup=markup,
                )
                return
        except Exception:
            pass
        bot.send_message(chat_id, text, reply_markup=markup)

    def _ask_confirm_vacation(chat_id: int, master, start_d: _dt.date, end_d: _dt.date) -> None:
        from types import SimpleNamespace
        fake = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            text=f"/vacation {start_d.isoformat()} {end_d.isoformat()}",
        )
        cmd_vacation(fake)

    @bot.message_handler(commands=["vacation", "отпуск"])
    def cmd_vacation(message: types.Message) -> None:
        """Мастер задаёт период отпуска. Формат: /vacation 2026-06-15 2026-06-20"""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        parts = (message.text or "").split()
        if len(parts) < 3:
            current = sorted(get_vacation_dates(master.name))
            if current:
                months = ["янв", "фев", "мар", "апр", "май", "июн",
                          "июл", "авг", "сен", "окт", "ноя", "дек"]
                now = ctx.now_moscow().date()
                future = [d for d in current if d >= now][:14]
                if future:
                    text = "🏖 <b>Твои выходные/отпуск:</b>\n\n"
                    text += "\n".join(f"• {d.day} {months[d.month - 1]} {d.year}"
                                        for d in future)
                    text += "\n\n"
                else:
                    text = "🏖 <b>Будущих отпусков нет.</b>\n\n"
            else:
                text = "🏖 <b>Отпусков пока нет.</b>\n\n"
            text += "Выбери первый день отпуска кнопкой ниже."
            _send_vacation_start_picker(message.chat.id, text + "\n")
            return
        try:
            start_d = _dt.date.fromisoformat(parts[1])
            end_d = _dt.date.fromisoformat(parts[2])
        except ValueError:
            bot.send_message(message.chat.id,
                "Не понял даты. Формат: <code>/vacation 2026-06-15 2026-06-20</code> (ГГГГ-ММ-ДД)",
                parse_mode="HTML")
            return
        if end_d < start_d:
            bot.send_message(message.chat.id, "Конечная дата раньше начальной — проверь.")
            return
        today = ctx.now_moscow().date()
        if end_d < today:
            bot.send_message(message.chat.id,
                "Эти даты уже в прошлом 🙂 Поставь отпуск на будущее.")
            return
        if start_d < today:
            bot.send_message(message.chat.id,
                f"Старт в прошлом — начну отсчёт с сегодня ({today.isoformat()}).")
            start_d = today
        days = (end_d - start_d).days + 1
        if days > 60:
            bot.send_message(message.chat.id,
                "Уж 60+ дней? Если правда — поставь двумя порциями. Сейчас всё в одну "
                "командой страшновато.")
            return

        existing_events = []
        cur = start_d
        while cur <= end_d:
            existing_events.extend(_bookings_for_master_on_date(master, cur))
            cur += _dt.timedelta(days=1)

        months = ["января", "февраля", "марта", "апреля", "мая", "июня",
                  "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        start_str = f"{start_d.day} {months[start_d.month - 1]}"
        end_str = f"{end_d.day} {months[end_d.month - 1]}"

        text = (
            f"🏖 <b>Поставить отпуск с {start_str} по {end_str}?</b>\n\n"
            f"Дней: {days}\n"
        )
        if existing_events:
            text += f"\n⚠️ В этом окне уже есть <b>{len(existing_events)} записей</b>:\n"
            for e in existing_events[:10]:
                start_iso = e.get("start", {}).get("dateTime", "")
                try:
                    dt = _dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                    t_str = dt.strftime("%d.%m %H:%M")
                except Exception:
                    t_str = "?"
                desc = e.get("description", "") or ""
                m = re.search(r"Telegram ID клиента:\s*(\d+)", desc)
                cid = int(m.group(1)) if m else None
                name = "—"
                if cid:
                    cs = state_store.get(cid) or {}
                    name = ((cs.get("card") or {}).get("name") or "—").split()[0]
                text += f"• {t_str} — {name}\n"
            if len(existing_events) > 10:
                text += f"… и ещё {len(existing_events) - 10}\n"
            text += "\nВсе эти записи будут отменены, клиенты получат извинения."
        else:
            text += "\nВ этом окне записей нет — можно ставить спокойно."

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(
            f"✅ Поставить отпуск ({days} дн.)",
            callback_data=f"vac:yes:{master.name}:{start_d.isoformat()}:{end_d.isoformat()}",
        ))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="vac:no"))
        bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

    @bot.message_handler(commands=["vacation_clear", "очистить_отпуск"])
    def cmd_vacation_clear(message: types.Message) -> None:
        """Очищает ВСЕ запланированные дни отпуска мастера."""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        if not state_store.redis:
            bot.send_message(message.chat.id, "Redis не подключён, нечего очищать.")
            return
        try:
            state_store.redis.delete(_vacation_key(master.name))
            bot.send_message(message.chat.id,
                "🧹 Все запланированные дни отпуска очищены.\n"
                "<i>Существующие записи в эти дни уже могли быть отменены — это не откатывается.</i>",
                parse_mode="HTML")
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка: {e}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("vac:"))
    def on_vacation_callback(call: types.CallbackQuery) -> None:
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Только для мастера")
            return
        parts = call.data.split(":")
        action = parts[1]
        if action == "no":
            bot.answer_callback_query(call.id, "Отменено")
            try:
                bot.edit_message_text("Ок, не ставлю отпуск.",
                    chat_id=call.message.chat.id, message_id=call.message.message_id)
            except Exception:
                pass
            return
        if action == "from":
            bot.answer_callback_query(call.id)
            try:
                start_d = _dt.date.fromisoformat(parts[2])
            except Exception:
                return
            _send_vacation_end_picker(call.message.chat.id, start_d, call.message.message_id)
            return
        if action == "to":
            bot.answer_callback_query(call.id)
            try:
                start_d = _dt.date.fromisoformat(parts[2])
                end_d = _dt.date.fromisoformat(parts[3])
            except Exception:
                return
            _ask_confirm_vacation(call.message.chat.id, master, start_d, end_d)
            return
        if action != "yes":
            bot.answer_callback_query(call.id)
            return

        if len(parts) < 5:
            bot.answer_callback_query(call.id, "Кривая ссылка")
            return
        master_name = parts[2]
        try:
            start_d = _dt.date.fromisoformat(parts[3])
            end_d = _dt.date.fromisoformat(parts[4])
        except Exception:
            bot.answer_callback_query(call.id, "Дата кривая")
            return
        if master_name != master.name:
            bot.answer_callback_query(call.id, "Это запрос другого мастера")
            return

        if state_store.redis:
            lock_key = f"nailbot:lock:vac:{master.name}:{parts[3]}:{parts[4]}"
            try:
                # TTL=10 мин: при большом окне отпуска отмена сотен записей идёт долго
                acquired = state_store.redis.set(lock_key, "1", nx=True, ex=600)
                if not acquired:
                    bot.answer_callback_query(call.id, "Уже запущено, подожди отчёт")
                    return
            except Exception:
                pass

        bot.answer_callback_query(call.id, "Ставлю...")
        try:
            bot.edit_message_text("⏳ Ставлю отпуск и отменяю записи...",
                chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception:
            pass

        admin_chat = call.message.chat.id

        def _run() -> None:
          try:
            added = _add_vacation_range(master.name, start_d, end_d)

            svc = ctx.calendar_service()
            cancelled = notified = failed = bonus_restored_count = 0
            months_full = ["января", "февраля", "марта", "апреля", "мая", "июня",
                           "июля", "августа", "сентября", "октября", "ноября", "декабря"]
            cur = start_d
            while cur <= end_d:
                for e in _bookings_for_master_on_date(master, cur):
                    event_id = e.get("id")
                    desc = e.get("description", "") or ""
                    m = re.search(r"Telegram ID клиента:\s*(\d+)", desc)
                    client_id = int(m.group(1)) if m else None

                    try:
                        iso = e.get("start", {}).get("dateTime", "")
                        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                        when_str = f"{dt.day} {months_full[dt.month - 1]} в {dt.strftime('%H:%M')}"
                    except Exception:
                        when_str = cur.isoformat()

                    result = _safe_cancel_booking(
                        event_id, client_id, master, svc, when_str, reason="vacation_mass_cancel"
                    )

                    if result["calendar_deleted"] or result["state_cleaned"] or result["db_updated"]:
                        cancelled += 1
                    if result["notified"]:
                        notified += 1
                    if result["bonus_restored"]:
                        bonus_restored_count += 1
                    if not (result["calendar_deleted"] or result["state_cleaned"] or result["db_updated"]):
                        failed += 1

                    time.sleep(0.05)
                cur += _dt.timedelta(days=1)

            report = (
                f"🏖 <b>Отпуск поставлен!</b>\n\n"
                f"Добавлено дней: <b>{added}</b>\n"
                f"Обработано записей: <b>{cancelled}</b>\n"
                f"Уведомлено клиентов: <b>{notified}</b>\n"
                f"Бонусов возвращено: <b>{bonus_restored_count}</b>\n"
            )
            if failed:
                report += f"❌ Полностью не удалось обработать: {failed} (частичные успехи возможны — проверь /repair)\n"
            report += "\n<i>Чтобы убрать отпуск — /vacation_clear</i>"
            try:
                bot.send_message(admin_chat, report, parse_mode="HTML")
            except Exception:
                pass
          except Exception as e:
            log.exception("vacation thread crashed: %s", e)
            try:
                bot.send_message(admin_chat,
                    f"❌ Что-то пошло не так при установке отпуска: {e}\n"
                    "Проверь Google Calendar и /vacation что показывает.")
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    @bot.message_handler(commands=["болею", "sick", "mass_cancel"])
    def cmd_sick(message: types.Message) -> None:
        """Мастер заболел — массовая отмена записей с автоопределением даты."""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        target_date, events = _pick_target_day(master)
        if not target_date or not events:
            bot.send_message(message.chat.id,
                "На ближайшие 3 дня записей нет. Выздоравливай спокойно 💕")
            return

        months = ["января", "февраля", "марта", "апреля", "мая", "июня",
                  "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        when_str = f"{target_date.day} {months[target_date.month - 1]}"

        lines = [f"⚠️ <b>ВЫ ТОЧНО ХОТИТЕ ОТМЕНИТЬ ВСЕХ НА {when_str.upper()}?</b>\n"]
        lines.append(f"Записей: {len(events)}\n")
        walk_in_count = 0
        for e in events:
            start_iso = e.get("start", {}).get("dateTime", "")
            try:
                dt = _dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M")
            except Exception:
                time_str = "?"
            desc = e.get("description", "") or ""
            m = re.search(r"Telegram ID клиента:\s*(\d+)", desc)
            client_id = int(m.group(1)) if m else None
            name = "—"
            phone = ""
            walk_in = _is_walk_in_event(e)
            if walk_in:
                walk_in_count += 1
                # Парсим имя/телефон прямо из описания события
                m_name = re.search(r"Имя клиента:\s*(.+)", desc)
                m_phone = re.search(r"Телефон клиента:\s*(\+?[\d\s\-\(\)]+)", desc)
                name = (m_name.group(1).strip() if m_name else "—").split()[0]
                phone = (m_phone.group(1).strip() if m_phone else "")
                lines.append(f"• <b>{time_str}</b> — 📞 {name} {phone} <i>(walk-in)</i>")
            else:
                if client_id:
                    cs = state_store.get(client_id) or {}
                    name = ((cs.get("card") or {}).get("name") or "—").split()[0]
                    for b in (cs.get("bookings") or []):
                        if b.get("calendar_event_id") == e.get("id") and b.get("phone"):
                            phone = b["phone"]
                            break
                lines.append(f"• <b>{time_str}</b> — {name} {phone}")
        lines.append("\n<i>TG-клиентам отправлю извинения и предложу перенести.</i>")
        if walk_in_count:
            lines.append(f"<i>📞 {walk_in_count} walk-in клиентам нужно позвонить вручную — "
                          "в финальном отчёте дам список с телефонами.</i>")

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(
            f"⚠️ ДА, ОТМЕНИТЬ ВСЕХ ({when_str})",
            callback_data=f"sick:yes:{master.name}:{target_date.isoformat()}",
        ))
        markup.add(types.InlineKeyboardButton(
            "❌ Передумал(а), не отменять",
            callback_data="sick:no",
        ))
        bot.send_message(message.chat.id, "\n".join(lines),
                          reply_markup=markup, parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("sick:"))
    def on_sick_callback(call: types.CallbackQuery) -> None:
        """Подтверждение/отмена массовой отмены при болезни."""
        master = ctx.master_by_telegram_id(call.from_user.id)
        if not master:
            bot.answer_callback_query(call.id, "Только для мастера")
            return

        parts = call.data.split(":")
        action = parts[1]
        if action == "no":
            bot.answer_callback_query(call.id, "Отменено")
            try:
                bot.edit_message_text("Ок, ничего не отменяю.",
                    chat_id=call.message.chat.id, message_id=call.message.message_id)
            except Exception:
                pass
            return

        if action != "yes":
            bot.answer_callback_query(call.id)
            return

        master_name = parts[2]
        target_iso = parts[3]
        if master_name != master.name:
            bot.answer_callback_query(call.id, "Это запрос другого мастера")
            return
        try:
            target_date = _dt.date.fromisoformat(target_iso)
        except Exception:
            bot.answer_callback_query(call.id, "Дата кривая")
            return

        if state_store.redis:
            lock_key = f"nailbot:lock:sick:{master.name}:{target_iso}"
            try:
                # TTL=10 мин: десятки записей × несколько API вызовов на каждую = до минут
                acquired = state_store.redis.set(lock_key, "1", nx=True, ex=600)
                if not acquired:
                    bot.answer_callback_query(call.id, "Уже запущено, подожди отчёт")
                    return
            except Exception:
                pass

        bot.answer_callback_query(call.id, "Запускаю...")
        try:
            bot.edit_message_text("⏳ Отменяю записи и уведомляю клиентов...",
                chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception:
            pass

        admin_chat = call.message.chat.id

        def _run_mass_cancel() -> None:
          try:
            events = _bookings_for_master_on_date(master, target_date)
            svc = ctx.calendar_service()
            months = ["января", "февраля", "марта", "апреля", "мая", "июня",
                      "июля", "августа", "сентября", "октября", "ноября", "декабря"]
            when_str = f"{target_date.day} {months[target_date.month - 1]}"

            cancelled = 0
            notified = 0
            failed = 0
            bonus_restored_count = 0
            walk_ins_to_call: list[str] = []  # для финального отчёта мастеру
            for e in events:
                event_id = e.get("id")
                desc = e.get("description", "") or ""
                m = re.search(r"Telegram ID клиента:\s*(\d+)", desc)
                client_id = int(m.group(1)) if m else None
                time_str = "(время)"
                try:
                    iso = e.get("start", {}).get("dateTime", "")
                    dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    time_str = dt.strftime("%H:%M")
                except Exception:
                    pass

                # === Walk-in (из /add_booking) — нет TG, отдельная обработка ===
                # Просто удаляем событие из Calendar и кладём контакт в отчёт
                # чтобы мастер обзвонил вручную.
                if _is_walk_in_event(e):
                    try:
                        if svc and master.calendar_id and event_id:
                            svc.events().delete(calendarId=master.calendar_id,
                                                 eventId=event_id).execute()
                            cancelled += 1
                            log.info("sick: walk-in event %s deleted (master=%s, time=%s)",
                                     event_id, master.name, time_str)
                        m_name = re.search(r"Имя клиента:\s*(.+)", desc)
                        m_phone = re.search(r"Телефон клиента:\s*(\+?[\d\s\-\(\)]+)", desc)
                        name = (m_name.group(1).strip() if m_name else "—").split()[0]
                        phone = (m_phone.group(1).strip() if m_phone else "—")
                        walk_ins_to_call.append(f"  • {time_str} — {name} {phone}")
                    except Exception as ex:
                        log.warning("sick: walk-in delete failed for %s: %s", event_id, ex)
                        failed += 1
                    time.sleep(0.05)
                    continue

                # === Обычный TG-клиент ===
                try:
                    result = _safe_cancel_booking(
                        event_id, client_id, master, svc, f"{when_str} в {time_str}", reason="sick_mass_cancel"
                    )
                    if result["calendar_deleted"] or result["state_cleaned"] or result["db_updated"]:
                        cancelled += 1
                    if result["notified"]:
                        notified += 1
                    if result["bonus_restored"]:
                        bonus_restored_count += 1
                    if not (result["calendar_deleted"] or result["state_cleaned"] or result["db_updated"]):
                        failed += 1
                except Exception as ex:
                    log.warning("sick: failed processing event %s: %s", event_id, ex)
                    failed += 1

                time.sleep(0.05)

            report = (
                f"✅ <b>Готово!</b>\n\n"
                f"Обработано записей: <b>{cancelled}</b>\n"
                f"Уведомлено TG-клиентов: <b>{notified}</b>\n"
                f"Бонусов возвращено: <b>{bonus_restored_count}</b>\n"
            )
            if walk_ins_to_call:
                report += (
                    f"\n📞 <b>Нужно обзвонить ({len(walk_ins_to_call)}):</b>\n"
                    + "\n".join(walk_ins_to_call) + "\n"
                )
            if failed:
                report += f"❌ Полностью не удалось обработать: {failed} (частичные успехи возможны — используй /repair <user> bookings)\n"
            report += "\nВыздоравливай 💕"
            try:
                bot.send_message(admin_chat, report, parse_mode="HTML")
            except Exception:
                pass
          except Exception as e:
            log.exception("mass_cancel thread crashed: %s", e)
            try:
                bot.send_message(admin_chat,
                    f"❌ Что-то пошло не так при массовой отмене: {e}\n"
                    "Проверь календарь руками, часть могла отмениться.")
            except Exception:
                pass

        threading.Thread(target=_run_mass_cancel, daemon=True).start()

    return {
        "cmd_sick": cmd_sick,
        "get_vacation_dates": get_vacation_dates,
        "send_vacation_picker": send_vacation_picker,
    }
