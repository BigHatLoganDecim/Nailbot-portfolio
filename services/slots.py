"""
Логика слотов и времени бронирования.

Pure-logic модуль: ничего не регистрирует на боте. Возвращает дикт функций,
которые main.py использует через прокинутые глобалы (для booking flow).

Зависимости (через ctx, фабрика create(ctx)):
  - kb (для services lookup в _total_duration_minutes)
  - get_vacation_dates (callable, set в register() после vacation_sick)
  - now_moscow, moscow_tz, calendar_service, get_busy_intervals, slot_is_free
  - _date_picker_max_days(), _min_hours_before_booking(), _fixed_slot_hours(),
    _default_service_duration_min(), _slot_duration_min()
"""

from __future__ import annotations

import datetime as _dt
import logging
import re

from telebot import types

log = logging.getLogger("nailbot.services.slots")


def create(ctx) -> dict:
    """Возвращает словарь функций. main.py биндит их в свои глобалы.

    Константы (_fixed_slot_hours(), etc) читаются из ctx.kb.settings в call-time
    через локальные геттеры — чтобы /reload пробрасывал новые значения без
    рестарта бота. ctx.kb — тот же Knowledge объект всегда (Knowledge.replace_from
    мутирует поля in-place).
    """
    kb_ref = ctx.kb
    # Геттеры из kb.settings — лениво, при каждом вызове функции
    def _fixed_slot_hours():
        return list(kb_ref.settings["fixed_slot_hours"])
    def _date_picker_max_days():
        return kb_ref.settings["date_picker_max_days"]
    def _min_hours_before_booking():
        return kb_ref.settings["min_hours_before_booking"]
    def _default_service_duration_min():
        return kb_ref.settings["default_service_duration_min"]
    def _slot_duration_min():
        return kb_ref.settings["slot_duration_min"]

    def occupancy_busy(master, day_start: _dt.datetime, day_end: _dt.datetime) -> list:
        """Busy intervals for client slots. Postgres SoT never reads Google."""
        try:
            from database.pg import pg_enabled
            from database.appointments import busy_intervals
            if pg_enabled():
                return busy_intervals(getattr(master, "name", ""), day_start, day_end)
        except Exception as e:
            log.warning("occupancy_busy pg: %s", e)
        svc = ctx.calendar_service()
        calendar_id = (getattr(master, "calendar_id", None) or "").strip()
        if svc and calendar_id and "заполнить" not in calendar_id.lower():
            return ctx.get_busy_intervals(
                svc, calendar_id, day_start, day_end,
                master_name=getattr(master, "name", None),
            )
        return []

    def allowed_slot_hours(master, day: _dt.date) -> list[int]:
        """Часы старта на день: Postgres-график, иначе open_slots из knowledge."""
        try:
            from database.appointments import hours_on_date
            pg_h = hours_on_date(getattr(master, "name", ""), day)
            if pg_h is not None:
                return list(pg_h)
        except Exception as e:
            log.warning("allowed_slot_hours pg: %s", e)
        fixed = [int(h) for h in _fixed_slot_hours()]
        oh = getattr(master, "open_slots", None) or {}
        day_key = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if day_key in oh and oh[day_key]:
            allowed = {int(h) for h in oh[day_key]}
            # пересечение с сеткой; если сетка не совпала — сырые open_slots
            result = [h for h in fixed if h in allowed]
            if not result:
                result = sorted(allowed)
                log.warning("open_slots %s %s вне fixed_slot_hours → raw %s",
                            getattr(master, "name", "?"), day_key, result)
            return result
        return list(fixed)

    def parse_work_dates(s: str) -> set[_dt.date]:
        """Парсит строку рабочих дат `2026-06-02, 2026-06-03, ...` в set дат."""
        if not s or "заполнить" in s.lower() or "уточня" in s.lower():
            return set()
        dates: set[_dt.date] = set()
        for item in s.split(","):
            item = item.strip()
            try:
                dates.add(_dt.date.fromisoformat(item))
            except ValueError:
                log.warning("work_dates: не распознал дату '%s'", item)
        return dates

    def parse_work_hours(s: str) -> tuple[int, int]:
        """`10:00-22:00` → (10, 22). При ошибке — (10, 22) по умолчанию.
        Конец clamp'ится к 23 чтобы datetime.replace(hour=...) не упал."""
        m = re.match(r"\s*(\d{1,2})[:.]?\d{0,2}\s*[-—]\s*(\d{1,2})[:.]?\d{0,2}", s or "")
        if m:
            start = max(0, min(23, int(m.group(1))))
            end = max(start + 1, min(23, int(m.group(2))))
            return start, end
        return 10, 22

    def work_hours_for(master, date: _dt.date) -> tuple[int, int]:
        """Часы работы мастера на конкретную дату (учитывает будни / выходные)."""
        is_weekend = date.weekday() in (5, 6)
        if is_weekend and master.work_hours_weekend:
            return parse_work_hours(master.work_hours_weekend)
        if not is_weekend and master.work_hours_weekday:
            return parse_work_hours(master.work_hours_weekday)
        return parse_work_hours(master.work_hours)

    def resolve_vague_time(text_lower: str, base_dt: _dt.datetime,
                             work_start: int, work_end: int) -> _dt.datetime:
        """«вечер» → 18:00, «обед» → 13:00, ..."""
        if re.search(r"\b\d{1,2}[:.\-]\d{2}\b", text_lower):
            return base_dt
        if re.search(r"\b(в|к)\s*\d{1,2}\b", text_lower):
            return base_dt
        if "поздн" in text_lower and "вечер" in text_lower:
            return base_dt.replace(hour=max(work_start, work_end - 1), minute=0, second=0, microsecond=0)
        if "вечер" in text_lower:
            return base_dt.replace(hour=min(work_end - 1, 18), minute=0, second=0, microsecond=0)
        if "обед" in text_lower:
            return base_dt.replace(hour=13, minute=0, second=0, microsecond=0)
        if "днём" in text_lower or "днем" in text_lower or "после обеда" in text_lower:
            return base_dt.replace(hour=14, minute=0, second=0, microsecond=0)
        if ("рано" in text_lower or "пораньше" in text_lower) and "утр" in text_lower:
            return base_dt.replace(hour=work_start, minute=0, second=0, microsecond=0)
        if "утр" in text_lower:
            return base_dt.replace(hour=work_start, minute=0, second=0, microsecond=0)
        return base_dt

    def total_duration_minutes(service_names: list[str], master_name: str | None = None) -> int:
        """Суммарная длительность выбранных услуг (Postgres, иначе knowledge.md)."""
        from services.catalog import duration_minutes
        return duration_minutes(
            service_names,
            list(kb_ref.services),
            master_name,
            default=_default_service_duration_min(),
        )

    def find_free_slots(busy: list[tuple], day_start: _dt.datetime, day_end: _dt.datetime,
                          around: _dt.datetime, duration_min: int | None = None,
                          count: int = 3,
                          allowed_hours: list[int] | None = None) -> list[_dt.datetime]:
        """Ближайшие свободные слоты (fixed или open_slots дня)."""
        if duration_min is None:
            duration_min = _slot_duration_min()
        hours = list(allowed_hours) if allowed_hours is not None else _fixed_slot_hours()
        candidates: list[_dt.datetime] = []
        duration = _dt.timedelta(minutes=duration_min)
        for h in hours:
            slot = day_start.replace(hour=h, minute=0, second=0, microsecond=0)
            if slot < day_start:
                continue
            if slot + duration > day_end:
                continue
            if ctx.slot_is_free(busy, slot, slot + duration):
                candidates.append(slot)
        candidates.sort(key=lambda x: abs((x - around).total_seconds()))
        return candidates[:count]

    def format_slot(dt: _dt.datetime) -> str:
        """`2026-05-22 10:00` → «22 мая в 10:00»."""
        months = ["января", "февраля", "марта", "апреля", "мая", "июня",
                  "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        return f"{dt.day} {months[dt.month - 1]} в {dt.strftime('%H:%M')}"

    def available_dates(master) -> list[_dt.date]:
        """Список дат для date-picker. work_dates, отпуск, open_slots + min_hours."""
        now = ctx.now_moscow()
        today = now.date()
        tz = ctx.moscow_tz()
        min_h = _min_hours_before_booking()
        work_dates = set(parse_work_dates(master.work_dates))
        candidates = [today + _dt.timedelta(days=i) for i in range(0, _date_picker_max_days())]
        vacation_dates = ctx.get_vacation_dates(master.name)
        result = []
        for d in candidates:
            if d in vacation_dates:
                continue
            pg_hours = None
            try:
                from database.appointments import hours_on_date
                pg_hours = hours_on_date(getattr(master, "name", ""), d)
            except Exception:
                pg_hours = None
            if pg_hours is not None:
                if not pg_hours:
                    continue
            elif work_dates and d not in work_dates:
                continue
            work_start, work_end = work_hours_for(master, d)
            hours = allowed_slot_hours(master, d)
            has_future = False
            for h in hours:
                if h < work_start or h >= work_end:
                    continue
                slot = _dt.datetime.combine(d, _dt.time(h, 0), tzinfo=tz)
                if (slot - now).total_seconds() / 3600 >= min_h:
                    has_future = True
                    break
            if not has_future:
                continue
            result.append(d)
        return result

    def date_picker_kb(master) -> types.InlineKeyboardMarkup:
        months = ["янв", "фев", "мар", "апр", "май", "июн",
                  "июл", "авг", "сен", "окт", "ноя", "дек"]
        weekdays = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        kb_ = types.InlineKeyboardMarkup(row_width=2)
        dates = available_dates(master)
        if not dates:
            kb_.add(types.InlineKeyboardButton(
                "ℹ️ График мастера на ближайшее время не задан",
                callback_data="dt:noop"))
            kb_.add(types.InlineKeyboardButton("✏️ Ввести день текстом",
                                                callback_data="dt:manual"))
            return kb_
        buttons = []
        for d in dates:
            label = f"{d.day} {months[d.month - 1]} ({weekdays[d.weekday()]})"
            buttons.append(types.InlineKeyboardButton(label, callback_data=f"dt:d:{d.isoformat()}"))
        for i in range(0, len(buttons), 2):
            kb_.row(*buttons[i:i + 2])
        kb_.add(types.InlineKeyboardButton("✏️ Ввести вручную", callback_data="dt:manual"))
        return kb_

    def time_picker_kb(date_iso: str, master, services: list[str]):
        date = _dt.date.fromisoformat(date_iso)
        work_start, work_end = work_hours_for(master, date)
        total_min = total_duration_minutes(services, getattr(master, "name", None))
        now = ctx.now_moscow()
        tz = ctx.moscow_tz()

        day_start = _dt.datetime.combine(date, _dt.time(work_start, 0), tzinfo=tz)
        day_end = _dt.datetime.combine(date, _dt.time(work_end, 0), tzinfo=tz)
        busy = occupancy_busy(master, day_start, day_end)

        kb_ = types.InlineKeyboardMarkup(row_width=3)
        buttons = []
        for h in allowed_slot_hours(master, date):
            slot = _dt.datetime.combine(date, _dt.time(h, 0), tzinfo=tz)
            slot_end = slot + _dt.timedelta(minutes=total_min)
            if (slot - now).total_seconds() / 3600 < _min_hours_before_booking():
                continue
            closing = _dt.datetime.combine(date, _dt.time(work_end, 0), tzinfo=tz)
            if slot_end > closing:
                continue
            if not ctx.slot_is_free(busy, slot, slot_end):
                continue
            buttons.append(types.InlineKeyboardButton(f"{h}:00", callback_data=f"dt:t:{date_iso}:{h}"))

        months = ["января", "февраля", "марта", "апреля", "мая", "июня",
                  "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        pretty = f"{date.day} {months[date.month - 1]}"
        if not buttons:
            hours = allowed_slot_hours(master, date)
            hours_str = ", ".join(f"{h}:00" for h in hours) if hours else "—"
            text = (
                f"На {pretty} у {master.name} нет доступных окон.\n\n"
                f"Окна мастера: {hours_str}.\n"
                f"Запись не раньше чем за {_min_hours_before_booking()} ч до начала "
                f"(прошедшие / слишком близкие скрыты). "
                f"Либо окна уже заняты.\n"
                f"Длительность выбранных услуг: ~{total_min} мин.\n\n"
                "Выберите другой день или меньше услуг."
            )
        else:
            text = (f"📅 <b>{pretty}</b>\n"
                    f"Длительность процедур: ~{total_min} мин.\n"
                    "Выберите время:")
        for i in range(0, len(buttons), 3):
            kb_.row(*buttons[i:i + 3])
        kb_.add(types.InlineKeyboardButton("◀️ К датам", callback_data="dt:back"))
        return text, kb_

    def resolve_booking_time(text: str, master, services: list[str] | None = None,
                              transcript: list[str] | None = None,
                              preparsed_dt: _dt.datetime | None = None) -> dict:
        """
        Полная проверка времени записи.

        Если передан preparsed_dt (из пикера времени), то пропускаем ненадёжный text parsing
        и сразу используем готовый datetime. Это решает проблему "Не понял дату" при выборе из кнопок.
        """
        now = ctx.now_moscow()
        total_minutes = total_duration_minutes(services or [], getattr(master, "name", None))

        parsed = None

        if preparsed_dt is not None:
            # Путь из пикера — самый надёжный, без участия dateparser
            parsed = preparsed_dt
            log.debug("resolve_booking_time: использован preparsed_dt из пикера: %s", parsed)
        else:
            # 1. Парсим дату из текста (свободный ввод пользователя)
            try:
                import dateparser  # type: ignore
                parsed = dateparser.parse(
                    text, languages=["ru"],
                    settings={
                        "PREFER_DATES_FROM": "future",
                        "TIMEZONE": "Europe/Moscow",
                        "RETURN_AS_TIMEZONE_AWARE": True,
                        "RELATIVE_BASE": now,
                        "DATE_ORDER": "DMY",   # важно для русского формата
                    },
                )
            except Exception as e:
                log.warning("resolve_booking_time: dateparser упал на тексте %r: %s", text, e)
                parsed = None

            # Fallback: пробуем несколько распространённых форматов вручную
            if not parsed:
                for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M",
                            "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M"):
                    try:
                        naive = _dt.datetime.strptime(text.strip(), fmt)
                        # ctx.moscow_tz() возвращает ZoneInfo или datetime.timezone —
                        # ни у одного нет .localize() (это метод только pytz).
                        # Используем .replace(tzinfo=...) — работает с обоими.
                        tz = ctx.moscow_tz() if hasattr(ctx, "moscow_tz") else _dt.timezone(_dt.timedelta(hours=3))
                        parsed = naive.replace(tzinfo=tz)
                        log.info("resolve_booking_time: fallback strptime сработал для %r", text)
                        break
                    except ValueError:
                        continue

        if not parsed:
            slots_str = ", ".join(f"{h}:00" for h in _fixed_slot_hours())
            return {"status": "unparseable", "datetime": None, "datetime_text": None,
                    "duration_min": total_minutes,
                    "message": ("Не понял, когда вы хотите прийти. Запись возможна на фиксированные часы: "
                                f"{slots_str}. Напишите день и время — например: «завтра в 14:00» или выберите из календаря."),
                    "suggestions": []}

        # 2. Резолвим расплывчатое время (только если пришли из текста, а не из пикера)
        work_start, work_end = work_hours_for(master, parsed.date())
        if preparsed_dt is None:
            parsed = resolve_vague_time((text or "").lower(), parsed, work_start, work_end)

        # 3a-pre. Отпуск мастера
        if parsed.date() in ctx.get_vacation_dates(master.name):
            return {"status": "outside_hours", "datetime": None, "datetime_text": None,
                    "duration_min": total_minutes,
                    "message": (f"В этот день у мастера {master.name} запланирован отпуск. "
                                "Выбери другую дату через /menu → Записаться."),
                    "suggestions": []}

        # 3a. Рабочие дни мастера
        work_dates = parse_work_dates(master.work_dates)
        if work_dates and parsed.date() not in work_dates:
            future_dates = sorted(d for d in work_dates if d >= now.date())[:5]
            if future_dates:
                months = ["января", "февраля", "марта", "апреля", "мая", "июня",
                          "июля", "августа", "сентября", "октября", "ноября", "декабря"]
                dates_str = ", ".join(f"{d.day} {months[d.month - 1]}" for d in future_dates)
                msg = (f"В этот день мастер {master.name} не работает. "
                       f"Ближайшие рабочие дни: {dates_str}. Какой день подходит?")
            else:
                msg = (f"В этот день мастер {master.name} не работает. "
                       "График на ближайшие даты уточняется — напишите позже.")
            return {"status": "outside_hours", "datetime": None, "datetime_text": None,
                    "duration_min": total_minutes, "message": msg, "suggestions": []}

        # 3b. Часы работы мастера
        if parsed.hour < work_start or parsed.hour >= work_end:
            return {"status": "outside_hours", "datetime": None, "datetime_text": None,
                    "duration_min": total_minutes,
                    "message": (f"К сожалению, мастер {master.name} работает с "
                                f"{work_start:02d}:00 до {work_end:02d}:00. "
                                "Подскажите другое время в этом промежутке."),
                    "suggestions": []}

        # 4. Запись только на разрешённые слоты дня (open_slots или fixed)
        day_hours = allowed_slot_hours(master, parsed.date())
        if parsed.hour not in day_hours or parsed.minute != 0:
            day_base = parsed.replace(minute=0, second=0, microsecond=0)
            candidates = [day_base.replace(hour=h) for h in day_hours]
            suggestions = [c for c in candidates
                           if (c - now).total_seconds() / 3600 >= _min_hours_before_booking()]
            slots_str = ", ".join(f"{h}:00" for h in day_hours) if day_hours else "—"
            msg = (f"В этот день к {master.name} можно записаться на: {slots_str}. "
                   "Какой слот вам подходит?")
            return {"status": "not_a_slot", "datetime": None, "datetime_text": None,
                    "duration_min": total_minutes, "message": msg,
                    "suggestions": suggestions[:6]}

        # 5. Минимум _min_hours_before_booking() часов в будущее
        diff = (parsed - now).total_seconds() / 3600
        if diff < _min_hours_before_booking():
            earliest = now + _dt.timedelta(hours=_min_hours_before_booking())
            return {"status": "too_soon", "datetime": None, "datetime_text": None,
                    "duration_min": total_minutes,
                    "message": (f"Запись возможна минимум за {_min_hours_before_booking()} часа от текущего момента. "
                                f"Самое раннее — {format_slot(earliest)}. Подскажите другое время."),
                    "suggestions": []}

        # 6. Длительность процедур до закрытия
        slot_start = parsed
        slot_end = slot_start + _dt.timedelta(minutes=total_minutes)
        closing = parsed.replace(hour=work_end, minute=0, second=0, microsecond=0)
        if slot_end > closing:
            return {"status": "too_long", "datetime": None, "datetime_text": None,
                    "duration_min": total_minutes,
                    "message": (f"Выбранные процедуры займут {total_minutes} мин, "
                                f"а от {parsed.strftime('%H:%M')} до закрытия ({work_end:02d}:00) "
                                f"не хватает времени. Выберите более раннее время или меньше услуг."),
                    "suggestions": []}

        # 7. Occupancy: Postgres appointments when SoT is on, else legacy Calendar.
        # Button path (preparsed_dt) already checked in time_picker; finalize re-checks.
        if preparsed_dt is None:
            day_start = parsed.replace(hour=work_start, minute=0, second=0, microsecond=0)
            day_end = parsed.replace(hour=work_end, minute=0, second=0, microsecond=0)
            busy = occupancy_busy(master, day_start, day_end)
            if busy and not ctx.slot_is_free(busy, slot_start, slot_end):
                suggestions = find_free_slots(
                    busy, day_start, day_end, slot_start,
                    duration_min=total_minutes, count=3,
                    allowed_hours=day_hours,
                )
                if not suggestions:
                    next_day_start = day_start + _dt.timedelta(days=1)
                    next_day_end = day_end + _dt.timedelta(days=1)
                    next_busy = occupancy_busy(master, next_day_start, next_day_end)
                    next_hours = allowed_slot_hours(master, next_day_start.date())
                    suggestions = find_free_slots(
                        next_busy, next_day_start, next_day_end,
                        next_day_start,
                        allowed_hours=next_hours,
                        duration_min=total_minutes, count=3,
                    )
                if suggestions:
                    slots_str = "\n".join(f"• {format_slot(s)}" for s in suggestions)
                    msg = (f"К сожалению, на {format_slot(slot_start)} уже занято.\n"
                           f"Свободные ближайшие окна у {master.name}:\n{slots_str}\n\n"
                           "Какое из них подходит?")
                else:
                    msg = (f"На {format_slot(slot_start)} занято, и ближайшие дни тоже плотные. "
                           "Подскажите другое удобное время.")
                return {"status": "busy", "datetime": None, "datetime_text": None,
                        "duration_min": total_minutes, "message": msg,
                        "suggestions": suggestions}

        # 8. Всё ок
        return {"status": "ok", "datetime": parsed,
                "datetime_text": format_slot(parsed),
                "duration_min": total_minutes, "message": "", "suggestions": []}

    return {
        "parse_work_dates": parse_work_dates,
        "work_hours_for": work_hours_for,
        "parse_work_hours": parse_work_hours,
        "resolve_vague_time": resolve_vague_time,
        "total_duration_minutes": total_duration_minutes,
        "find_free_slots": find_free_slots,
        "format_slot": format_slot,
        "available_dates": available_dates,
        "date_picker_kb": date_picker_kb,
        "time_picker_kb": time_picker_kb,
        "resolve_booking_time": resolve_booking_time,
        "allowed_slot_hours": allowed_slot_hours,
    }
