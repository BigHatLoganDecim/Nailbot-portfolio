"""
Booking flow — самый большой модуль бота.

Включает:
  - Полный диалог записи (start/continue/advance/finalize)
  - Все callback'и: bk:, svc:, dt:, cf:, rv:, master:, phc:, empty:book,
    reschedule_bk:, resched_yes:, cancel_bk:, cancel_back, cancel_yes:,
    rm:yes:, rm:cancel:, rm:cancel_yes:, rm:cancel_back: (напоминания)
  - on_contact (Telegram contact share)
  - Корзина услуг (services keyboards)
  - notify_master — уведомления мастеру (booked / cancelled / review)
  - handle_my_bookings — список предстоящих записей клиента
  - schedule_reminder + sync_with_google_calendar + _schedule_review

Внешний API:
  - start_booking, continue_booking, handle_my_bookings, notify_master
    (зовутся из main._on_text_inner и main.cmd_my_bookings)
  - send_reminder_impl, send_review_prompt_impl — для APScheduler-шимов в
    main.py (они остаются на __main__ ради backward-compat с jobs в Redis)
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import uuid
from typing import Any

from telebot import types

import parsing

log = logging.getLogger("nailbot.handlers.booking")


def register(ctx) -> dict:
    """
    ctx должен содержать:
      bot, kb, state_store, scheduler, llm, llm_usage, config,
      now_moscow, parse_iso_aware, moscow_tz,
      master_by_telegram_id, normalize_tg_id, calendar_service,
      get_state, save_state, reset_dialog,
      main_menu_kb, masters_inline_kb, reply,
      resolve_booking_time, date_picker_kb, time_picker_kb,
      format_slot, total_duration_minutes,
      ALL_MASTERS, CATEGORY_ICONS, _default_service_duration_min(),
      STEP_IDLE, STEP_CHOOSE_MASTER, STEP_CHOOSE_SERVICES,
      STEP_ENTER_DATETIME, STEP_ENTER_PHONE, STEP_CONFIRM,
      STEP_PROMO_INPUT, STEP_AWAITING_REVIEW,
      REVIEW_DELAY_MINUTES_AFTER_END,
      db  # New long-term Database (clients + bookings history)
    """
    bot = ctx.bot
    kb = ctx.kb
    state_store = ctx.state_store
    scheduler = ctx.scheduler
    llm = ctx.llm
    llm_usage = ctx.llm_usage
    db = getattr(ctx, "db", None)  # New long-term database (may be None during transition)
    # Динамические геттеры — kb мутируется in-place при /reload, ctx.kb тот же объект.
    # Раньше захватывали значения в register-time → /reload не пробрасывал изменения.
    def _default_service_duration_min():
        return kb.settings["default_service_duration_min"]
    def _category_icons():
        return kb.categories
    def _review_delay_minutes():
        return kb.settings["review_delay_minutes_after_end"]
    def _all_masters():
        return tuple(kb.masters.keys())

    # ---- Phone helpers ----

    def _get_known_phone(state: dict[str, Any]) -> str | None:
        card = state.get("card") or {}
        phone = card.get("phone")
        if phone:
            return phone
        for b in reversed(state.get("bookings") or []):
            if b.get("phone"):
                return b["phone"]
        return None

    def _remember_phone(state: dict[str, Any], phone: str) -> None:
        card = state.get("card") or {"name": "", "notes": "", "birthday": "", "tags": []}
        card["phone"] = phone
        state["card"] = card

    def _prompt_pdn_consent(chat_id: int) -> None:
        """Короткий запрос согласия перед телефоном — без «страшилок»."""
        text = (kb.msg("pdn_consent") or (
            "📞 Нужен контактный телефон — только чтобы мастер и бот "
            "связались по записи и прислали напоминание.\n\n"
            "Нажимая «Ок», вы соглашаетесь на обработку данных для записи. "
            "Подробнее: /privacy · Правила: /rules"
        ))
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(
            "✅ Ок, продолжить", callback_data="pdn:ok"))
        markup.add(types.InlineKeyboardButton(
            "📜 Правила", callback_data="pdn:rules"))
        markup.add(types.InlineKeyboardButton(
            "🔒 Подробнее о данных", callback_data="pdn:privacy"))
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    def _prompt_phone(chat_id: int, state: dict[str, Any]) -> None:
        known_phone = _get_known_phone(state)
        if known_phone:
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton(
                "✅ Да, использовать этот", callback_data="phc:yes"))
            markup.add(types.InlineKeyboardButton(
                "📞 Указать другой", callback_data="phc:change"))
            bot.send_message(
                chat_id,
                f"📞 Использовать этот номер для связи?\n\n<b>{known_phone}</b>",
                reply_markup=markup, parse_mode="HTML",
            )
            return
        contact_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        contact_kb.row(types.KeyboardButton("📱 Отправить мой номер", request_contact=True))
        contact_kb.row("Отмена")
        prompt = (kb.msg("ask_phone")
                  or "Оставьте контактный телефон — кнопка ниже или ввод вручную.")
        bot.send_message(chat_id, prompt, reply_markup=contact_kb)

    # ---- Service categories / cart ----

    def _live_services(master_name: str, *, active_only: bool = True):
        from services.catalog import overlay_services
        return overlay_services(kb.services, master_name, active_only=active_only)

    def _categories_for_master(master_name: str) -> list[tuple[str, list]]:
        order: list[str] = []
        by_cat: dict[str, list] = {}
        for svc in _live_services(master_name):
            if svc.masters and master_name not in svc.masters:
                continue
            if svc.category not in by_cat:
                by_cat[svc.category] = []
                order.append(svc.category)
            by_cat[svc.category].append(svc)
        return [(c, by_cat[c]) for c in order]

    def _service_idx(svc_name: str) -> int:
        for i, s in enumerate(kb.services):
            if s.name == svc_name:
                return i
        return -1

    def _cart_text(cart: list[str], master_name: str = "") -> str:
        if not cart:
            return "🛒 Корзина пуста. Выберите услуги по категориям:"
        live = {s.name: s for s in _live_services(master_name, active_only=False)}
        lines = ["🛒 <b>В корзине:</b>"]
        for name in cart:
            svc = live.get(name) or next((s for s in kb.services if s.name == name), None)
            if svc:
                lines.append(f"• {svc.name} — {svc.price}")
            else:
                lines.append(f"• {name}")
        total = ctx.total_duration_minutes(cart, master_name)
        lines.append(f"\n⏱ Время на запись: ~{total} мин")
        lines.append("\nДобавляйте ещё или нажмите «Готово»:")
        return "\n".join(lines)

    def _services_main_kb(cart: list[str], master_name: str) -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup(row_width=1)
        cats = _categories_for_master(master_name)
        for i, (cat_name, _) in enumerate(cats):
            icon = _category_icons().get(cat_name, "📋")
            kb_.add(types.InlineKeyboardButton(f"{icon} {cat_name}", callback_data=f"svc:c:{i}"))
        if cart:
            kb_.add(types.InlineKeyboardButton(f"✅ Готово ({len(cart)})", callback_data="svc:done"))
        return kb_

    def _services_cat_kb(cat_idx: int, cart: list[str], master_name: str) -> types.InlineKeyboardMarkup:
        cats = _categories_for_master(master_name)
        if cat_idx < 0 or cat_idx >= len(cats):
            return _services_main_kb(cart, master_name)
        _, services = cats[cat_idx]
        kb_ = types.InlineKeyboardMarkup(row_width=1)
        for svc in services:
            prefix = "✅ " if svc.name in cart else "▫️ "
            label = f"{prefix}{svc.name} — {svc.price}"
            if len(label) > 60:
                label = label[:57] + "…"
            i = _service_idx(svc.name)
            kb_.add(types.InlineKeyboardButton(label, callback_data=f"svc:t:{i}"))
        kb_.add(types.InlineKeyboardButton("◀️ К категориям", callback_data="svc:home"))
        if cart:
            kb_.add(types.InlineKeyboardButton(f"✅ Готово ({len(cart)})", callback_data="svc:done"))
        return kb_

    # ---- Reminders & Review (impls) ----

    REMINDER_CONFIRMED_NOTE = "✅ Клиент подтвердил визит (напоминание)"

    def _find_booking(user_id: int, booking_id: str) -> dict[str, Any] | None:
        """Запись из state или long-term DB (для callback'ов из напоминаний)."""
        state = ctx.get_state(user_id)
        for b in state.get("bookings") or []:
            if b.get("id") == booking_id:
                return b
        if db is None:
            return None
        row = db.bookings.get_booking(booking_id)
        if not row or row.get("user_id") != user_id:
            return None
        merged = {
            **row,
            "datetime": row.get("datetime") or row.get("datetime_text"),
            "chat_id": row.get("chat_id") or state.get("chat_id"),
        }
        # state может быть устаревшим — статус и notes берём из DB
        for b in state.get("bookings") or []:
            if b.get("id") == booking_id:
                return {**b, **merged}
        return merged

    def _client_display_name(user_id: int, booking: dict[str, Any]) -> str:
        card = (ctx.get_state(user_id).get("card") or {})
        if card.get("name"):
            return str(card["name"]).strip()
        if db is not None:
            try:
                client = db.clients.get_client(user_id)
                if client and client.get("name"):
                    return str(client["name"]).strip()
            except Exception:
                pass
        return (booking.get("client_name") or "").strip()

    def _ensure_booking_in_state(user_id: int, booking: dict[str, Any]) -> None:
        """Чтобы отмена из напоминания работала даже после истечения TTL state."""
        state = ctx.get_state(user_id)
        bookings = list(state.get("bookings") or [])
        bid = booking.get("id")
        if bid and not any(b.get("id") == bid for b in bookings):
            bookings.append(booking)
            state["bookings"] = bookings
            ctx.save_state(user_id, state)

    def _booking_reminder_confirmed(booking: dict[str, Any]) -> bool:
        if REMINDER_CONFIRMED_NOTE in (booking.get("notes") or []):
            return True
        bid = booking.get("id")
        if db is not None and bid:
            row = db.bookings.get_booking(bid)
            if row and REMINDER_CONFIRMED_NOTE in (row.get("notes") or []):
                return True
        return False

    def _is_rm_cancel_prompt(data: str) -> bool:
        """rm:cancel:<id> — но не rm:cancel_yes: / rm:cancel_back: (иначе коллизия prefix)."""
        return (
            data.startswith("rm:cancel:")
            and not data.startswith("rm:cancel_yes:")
            and not data.startswith("rm:cancel_back:")
        )

    def _reload_booking_from_db(booking: dict[str, Any]) -> dict[str, Any]:
        """Актуальный статус/notes из SQLite (APScheduler хранит снимок на момент записи)."""
        bid = booking.get("id")
        if not bid or db is None:
            return booking
        row = db.bookings.get_booking(bid)
        if not row:
            return booking
        merged = {**booking, **row}
        merged["datetime"] = row.get("datetime") or row.get("datetime_text")
        return merged

    def _delete_google_for_booking(target: dict[str, Any]) -> None:
        event_id = target.get("google_event_id") or target.get("calendar_event_id")
        if not event_id:
            return
        try:
            svc = ctx.calendar_service()
            master = kb.masters.get(target.get("master") or "")
            cal_id = (master.calendar_id if master else None) or ""
            if svc and cal_id and "заполнить" not in cal_id.lower():
                from services.calendar import delete_event
                delete_event(svc, cal_id, event_id)
                log.info("calendar: событие %s удалено по отмене", event_id)
        except Exception as e:
            log.warning("calendar: не удалось удалить событие %s: %s", event_id, e)

    def _drop_booking_from_state(user_id: int, target: dict[str, Any], booking_id: str) -> None:
        state = ctx.get_state(user_id)
        ids = {
            booking_id,
            target.get("id"),
            target.get("sqlite_booking_id"),
        }
        iso = target.get("datetime_iso") or target.get("start_at")
        master = target.get("master")
        kept = []
        for b in state.get("bookings") or []:
            if b.get("id") in ids or b.get("pg_appointment_id") in ids:
                continue
            if iso and master and b.get("master") == master and (
                b.get("datetime_iso") == iso or (b.get("datetime_iso") or "").startswith(str(iso)[:16])
            ):
                continue
            kept.append(b)
        state["bookings"] = kept
        ctx.save_state(user_id, state)

    def _finalize_client_cancel(
        user_id: int,
        target: dict[str, Any],
        booking_id: str,
        *,
        notify_outcome: str,
        decrement_reason: str,
        transcript: list[str] | None = None,
    ) -> bool:
        """Общая логика отмены клиентом. Возвращает late_cancel (<24ч)."""
        late = False
        iso = target.get("datetime_iso") or target.get("start_at")
        try:
            if iso:
                dt = ctx.parse_iso_aware(iso)
                late = (dt - ctx.now_moscow()).total_seconds() / 3600 < 24
        except Exception:
            pass

        pg_row = None
        try:
            from database.pg import pg_enabled
            from database.appointments import cancel_appointment
            if pg_enabled():
                pg_row = cancel_appointment(
                    target.get("pg_appointment_id") or target.get("id") or booking_id,
                    actor=f"client:{user_id}",
                    reason=decrement_reason,
                )
                if pg_row:
                    target = {**target, **pg_row}
        except Exception as e:
            log.error("client_cancel postgres: %s", e)

        _delete_google_for_booking(target)
        try:
            from database.worker import process_outbox
            process_outbox(bot=bot, limit=8)
        except Exception:
            pass

        for job_id in target.get("reminder_job_ids") or []:
            try:
                if scheduler:
                    scheduler.remove_job(job_id)
            except Exception:
                pass
        if target.get("review_job_id"):
            try:
                if scheduler:
                    scheduler.remove_job(target["review_job_id"])
            except Exception:
                pass

        _drop_booking_from_state(user_id, target, booking_id)

        if db is not None:
            try:
                # decrement ДО mark cancelled — иначе only_if_confirmed пропустит единственную запись
                db.clients.decrement_visits(user_id, reason=decrement_reason)
                sqlite_id = target.get("sqlite_booking_id") or booking_id
                db.bookings.mark_booking_status(sqlite_id, "cancelled")
                if target.get("id") and target.get("id") != sqlite_id:
                    db.bookings.mark_booking_status(str(target.get("id")), "cancelled")
                notes_str = " ".join(target.get("notes") or [])
                if (kb.referral.get("referrer_bonus_master_note", "") in notes_str
                        or "дизайн-бонус" in notes_str.lower()):
                    db.clients.update_referral_info(user_id, increment_design_bonuses=1)
            except Exception as e:
                log.error("client_cancel: DB update failed %s: %s", booking_id, e)

        target["client_name"] = _client_display_name(user_id, target)
        notify_master(user_id, target, outcome=notify_outcome, transcript=transcript)
        return late

    def send_reminder_impl(chat_id: int, booking: dict[str, Any], hours_before: int) -> None:
        """Отправить клиенту напоминание. Вызывается планировщиком через шим в main."""
        try:
            booking = _reload_booking_from_db(booking)
            if (booking.get("status") or "confirmed") != "confirmed":
                log.info("reminder: пропуск — запись %s уже не active (%s)",
                         booking.get("id"), booking.get("status"))
                return

            services = (", ".join(booking.get("services") or [])
                        or booking.get("services_raw") or "процедура")
            when_label = "завтра в это же время" if hours_before >= 24 else "через час"
            bk_id = booking.get("id")
            already_confirmed = _booking_reminder_confirmed(booking)

            lines = [
                f"⏰ Напоминание: {when_label} ваша запись.",
                f"Мастер: {booking.get('master')}",
                f"Процедуры: {services}",
                f"Время: {booking.get('datetime')}",
                "",
            ]
            if already_confirmed:
                lines.append("✅ Вы уже подтвердили этот визит.")
            else:
                lines.append("Подтвердите визит или отмените запись кнопками ниже.")

            markup = None
            if bk_id and not already_confirmed:
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.row(
                    types.InlineKeyboardButton(
                        "✅ Подтверждаю", callback_data=f"rm:yes:{bk_id}",
                    ),
                    types.InlineKeyboardButton(
                        "❌ Отменить", callback_data=f"rm:cancel:{bk_id}",
                    ),
                )

            bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)
            log.info("reminder: отправлено -%dч → chat_id=%s", hours_before, chat_id)
        except Exception as e:
            log.error("reminder: ошибка отправки -%dч → chat_id=%s: %s",
                      hours_before, chat_id, e)

    def schedule_reminder(booking: dict[str, Any]) -> None:
        """Планирует 2 напоминания клиенту: за 24 ч и за 1 ч до визита.
        Target — main._send_reminder (шим, который зовёт send_reminder_impl).
        Это нужно чтобы APScheduler сохранил ссылку как __main__:_send_reminder
        и старые задачи в Redis JobStore переживали миграцию.
        """
        chat_id = booking.get("chat_id")
        if not scheduler:
            log.info("reminder: scheduler не запущен, только лог")
            return
        if not chat_id:
            log.warning("reminder: нет chat_id, пропускаем")
            return

        iso = booking.get("datetime_iso")
        if not iso:
            log.warning("reminder: нет datetime_iso в booking — напоминания не планирую")
            return
        try:
            parsed_dt = ctx.parse_iso_aware(iso)
        except Exception as e:
            log.warning("reminder: невалидный datetime_iso=%r: %s", iso, e)
            return

        # Импорт целевой функции из main — для APScheduler стабильная qualified-name
        import sys
        main_mod = sys.modules.get("__main__")
        target_fn = getattr(main_mod, "_send_reminder", None) if main_mod else None
        if target_fn is None:
            log.warning("reminder: main._send_reminder не найден — пропуск")
            return

        now = _dt.datetime.now(parsed_dt.tzinfo)
        scheduled = []
        job_ids: list[str] = []
        bk_id = booking.get("id") or uuid.uuid4().hex[:12]
        for hours in (24, 1):
            run_at = parsed_dt - _dt.timedelta(hours=hours)
            if run_at <= now:
                log.info("reminder: -%dч уже в прошлом (%s), пропускаем", hours, run_at)
                continue
            try:
                job_id = f"reminder_{bk_id}_h{hours}"
                scheduler.add_job(
                    target_fn,
                    trigger="date",
                    run_date=run_at,
                    args=[chat_id, booking, hours],
                    misfire_grace_time=3600,
                    id=job_id,
                    replace_existing=True,
                )
                job_ids.append(job_id)
                scheduled.append(f"-{hours}ч на {run_at:%Y-%m-%d %H:%M}")
            except Exception as e:
                log.error("reminder: не удалось запланировать -%dч: %s", hours, e)

        booking["reminder_job_ids"] = job_ids
        if scheduled:
            log.info("reminder: запланировано → %s (мастер=%s, chat_id=%s)",
                     ", ".join(scheduled), booking.get("master"), chat_id)

    def send_review_prompt_impl(chat_id: int, booking: dict[str, Any]) -> None:
        """Через час после визита просим оценить."""
        try:
            master = booking.get("master") or "мастеру"
            services = ", ".join(booking.get("services") or []) or booking.get("services_raw") or "процедура"
            markup = types.InlineKeyboardMarkup(row_width=5)
            markup.row(*[
                types.InlineKeyboardButton(f"{i}⭐", callback_data=f"rv:{booking['id']}:r:{i}")
                for i in range(1, 6)
            ])
            markup.add(types.InlineKeyboardButton("✍️ Написать отзыв",
                                                    callback_data=f"rv:{booking['id']}:t"))
            markup.add(types.InlineKeyboardButton("📅 Записаться снова",
                                                    callback_data=f"rv:{booking['id']}:b"))
            markup.add(types.InlineKeyboardButton("Пропустить",
                                                    callback_data=f"rv:{booking['id']}:s"))
            bot.send_message(
                chat_id,
                f"Спасибо, что были у нас! {kb.brand.get('emoji', '✨')}\n\n"
                f"Как прошёл визит к {master} ({services})?\n"
                "Поставьте оценку или напишите впечатления — это помогает нам становиться лучше.",
                reply_markup=markup,
            )
        except Exception as e:
            log.warning("send_review_prompt: %s", e)

    def _schedule_review(booking: dict[str, Any]) -> None:
        """Через REVIEW_DELAY_MINUTES_AFTER_END после конца визита — запросить отзыв."""
        if not scheduler:
            return
        iso = booking.get("datetime_iso")
        if not iso:
            return
        try:
            start_dt = ctx.parse_iso_aware(iso)
            duration = int(booking.get("duration_min") or _default_service_duration_min())
            ask_at = start_dt + _dt.timedelta(minutes=duration + _review_delay_minutes())
            if ask_at <= ctx.now_moscow():
                return
            import sys
            main_mod = sys.modules.get("__main__")
            target_fn = getattr(main_mod, "_send_review_prompt", None) if main_mod else None
            if target_fn is None:
                log.warning("review: main._send_review_prompt не найден — пропуск")
                return
            job_id = f"review_{booking['id']}"
            scheduler.add_job(
                target_fn,
                trigger="date", run_date=ask_at,
                args=[booking["chat_id"], booking],
                id=job_id, replace_existing=True,
                misfire_grace_time=6 * 3600,
            )
            booking["review_job_id"] = job_id
            log.info("review: запланирован для %s на %s", booking["id"], ask_at)
        except Exception as e:
            log.warning("schedule_review: %s", e)

    # ---- Early availability check (used before any persistence) ----
    def _is_slot_available_early(booking: dict[str, Any]) -> dict:
        """
        Быстрая проверка доступности слота перед сохранением записи.
        Используется в finalize_booking, чтобы минимизировать грязные состояния при гонках.

        Возвращает:
            {"available": True} или
            {"available": False, "message": "...", "suggestions": [...]}
        """
        master_name = booking.get("master")
        master = kb.masters.get(master_name) if master_name else None
        if not master:
            return {"available": True}

        iso = booking.get("datetime_iso")
        if not iso:
            return {"available": True}

        try:
            parsed_dt = ctx.parse_iso_aware(iso)
            duration_min = int(booking.get("duration_min") or _default_service_duration_min())
            date = parsed_dt.date()

            # === Local constraints (can change since user picked the slot) ===
            # 1. Vacation (most important local constraint)
            if date in ctx.get_vacation_dates(master.name):
                return {
                    "available": False,
                    "message": f"К сожалению, у мастера {master.name} на {parsed_dt.strftime('%d.%m')} отпуск. Выберите другую дату.",
                    "suggestions": []
                }

            # 2. Разрешённые слоты дня — та же логика, что allowed_slot_hours
            # (open_slots ∩ fixed, иначе raw open_slots, иначе fixed)
            fixed = _fixed_slot_hours()
            oh = getattr(master, "open_slots", None) or {}
            day_key = date.isoformat()
            if day_key in oh and oh[day_key]:
                raw = {int(x) for x in oh[day_key]}
                allowed = [h for h in fixed if h in raw] or sorted(raw)
            else:
                allowed = list(fixed)
            if parsed_dt.hour not in allowed or parsed_dt.minute != 0:
                return {
                    "available": False,
                    "message": (
                        f"В этот день к {master.name} можно: "
                        f"{', '.join(f'{h}:00' for h in allowed) or '—'}."
                    ),
                    "suggestions": [],
                }

            # Note: Full work hours validation is done via resolve_booking_time at selection time.
            # We keep the early check focused on vacation + fixed slots + calendar for speed and safety.

            slot_end = parsed_dt + _dt.timedelta(minutes=duration_min)
            day_start = parsed_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + _dt.timedelta(days=1)
            busy: list = []
            try:
                from database.pg import pg_enabled
                from database.appointments import busy_intervals
                if pg_enabled():
                    busy = busy_intervals(master.name, day_start, day_end)
                else:
                    calendar_id = (master.calendar_id or "").strip()
                    if calendar_id and "заполнить" not in calendar_id.lower():
                        service = ctx.calendar_service()
                        if service:
                            busy = ctx.get_busy_intervals(
                                service, calendar_id, day_start, day_end,
                                master_name=master.name,
                            )
            except Exception as e:
                log.warning("early occupancy check: %s", e)

            if busy and not ctx.slot_is_free(busy, parsed_dt, slot_end):
                log.warning("finalize_booking early check: слот %s уже занят", iso)
                suggestions = ctx.find_free_slots(
                    busy, day_start, day_end, parsed_dt,
                    duration_min=duration_min, count=3
                )
                message = (
                    "Это время только что заняли. Вот ближайшие свободные."
                )
                if suggestions:
                    suggestions_text = "\n".join(f"• {ctx.format_slot(s)}" for s in suggestions)
                    message += f"\n\n{suggestions_text}"
                return {
                    "available": False,
                    "message": message,
                    "suggestions": suggestions or []
                }

            return {"available": True}

        except Exception as e:
            log.warning("early availability check failed: %s", e)
            return {"available": True}  # при ошибке не блокируем (лучше записать, чем потерять клиента)

    # ---- Google Calendar sync ----

    def sync_with_google_calendar(booking: dict[str, Any]) -> None:
        """Создать событие в Google Calendar мастера. Любая ошибка не падает."""
        master_name = booking.get("master")
        master = kb.masters.get(master_name) if master_name else None
        if not master:
            log.info("calendar: пропуск — мастер не найден")
            return

        calendar_id = (master.calendar_id or "").strip()
        if not calendar_id or "заполнить" in calendar_id.lower():
            log.info("calendar: пропуск — calendar_id для %s не задан", master_name)
            return

        try:
            iso = booking.get("datetime_iso")
            if not iso:
                log.warning("calendar: нет datetime_iso в booking — событие не создаю")
                return
            parsed_dt = ctx.parse_iso_aware(iso)
            duration_min = int(booking.get("duration_min") or _default_service_duration_min())

            from database.pg import calendar_write_enabled
            if not calendar_write_enabled():
                log.info("calendar: skip write — projection off (GOOGLE_ENABLED=false)")
                return

            service = ctx.calendar_service()
            if not service:
                log.info("calendar: skip write — service unavailable; appointment stays")
                return

            # P0: never roll back the booking if Google is busy/down.
            end_dt = parsed_dt + _dt.timedelta(minutes=duration_min)
            services_str = (", ".join(booking.get("services") or [])
                            or booking.get("services_raw")
                            or "Запись")

            live = {s.name: s for s in _live_services(master_name, active_only=False)}
            svc_lines = []
            for sname in booking.get("services") or []:
                svc = live.get(sname) or next((s for s in kb.services if s.name == sname), None)
                if svc:
                    m = re.search(r"(\d+)", svc.duration or "")
                    mark = f"{m.group(1)} мин" if m else "доп., время уточнит мастер"
                    svc_lines.append(f"• {svc.name} — {svc.price} ({mark})")
                else:
                    svc_lines.append(f"• {sname}")
            svc_block = "\n".join(svc_lines) if svc_lines else "—"

            event = {
                "summary": (
                    f"[{master_name}] {kb.brand.get('emoji', '✨')} {services_str}"
                ),
                "description": (
                    f"Запись через бот {kb.brand.get('name', 'Salon')}.\n"
                    f"Мастер: {master_name}\n\n"
                    f"Состав записи:\n{svc_block}\n\n"
                    f"Плановая длительность: {duration_min} мин\n"
                    f"Телефон клиента: {booking.get('phone', '—')}\n"
                    f"Сказал клиент: {booking.get('services_raw') or '—'}\n"
                    f"Telegram ID клиента: {booking.get('user_id', '—')}"
                ),
                "start": {"dateTime": parsed_dt.isoformat(), "timeZone": "Europe/Moscow"},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Moscow"},
            }

            from services.calendar import create_event as _calendar_create_event
            result = _calendar_create_event(service, calendar_id, event)
            if result:
                booking["calendar_event_id"] = result.get("id")
                log.info("calendar: событие создано id=%s → %s",
                         result.get("id"), result.get("htmlLink"))
                try:
                    from database.appointments import set_google_event_id
                    pg_id = booking.get("pg_appointment_id")
                    if pg_id:
                        set_google_event_id(pg_id, result.get("id"))
                except Exception as e:
                    log.warning("calendar: не записал google_event_id в Postgres: %s", e)
            else:
                log.warning("calendar: create_event вернул None для %s (calendar_id=%s) — "
                            "нет прав на запись или неверный calendar_id", master_name, calendar_id)
                tg_id = ctx.normalize_tg_id(master.telegram_id) if master else ""
                if tg_id:
                    try:
                        dt_text = booking.get("datetime") or iso
                        bot.send_message(
                            tg_id,
                            f"⚠️ Запись клиента ({dt_text}) создана в боте, "
                            f"но <b>не попала в Google Calendar</b>.\n\n"
                            f"Вероятная причина: сервисный аккаунт бота не имеет прав на запись "
                            f"в твой календарь <code>{calendar_id}</code>.\n"
                            f"Нужно открыть настройки Google Календаря → "
                            f"«Поделиться с конкретными людьми» → добавить email сервисного аккаунта "
                            f"с правом «Изменять события».\n\n"
                            f"Используй /calendar_check для диагностики.",
                            parse_mode="HTML",
                        )
                    except Exception as notify_e:
                        log.warning("calendar: не смог уведомить мастера об ошибке: %s", notify_e)
        except Exception as e:
            log.exception("calendar: ошибка создания события: %s", e)

    # ---- notify_master ----

    def notify_master(client_user_id: int, booking: dict[str, Any],
                      outcome: str = "booked",
                      transcript: list[str] | None = None) -> None:
        master_name = booking.get("master")
        master = kb.masters.get(master_name) if master_name else None
        tg_id = ctx.normalize_tg_id(master.telegram_id) if master else ""
        if not tg_id:
            log.warning("notify_master: telegram_id для %s не указан в knowledge.md", master_name)
            return

        services_str = (", ".join(booking.get("services") or [])
                        or booking.get("services_raw")
                        or "(не указано)")

        header = {
            "booked": "📝 <b>Новая запись</b>",
            "cancelled": "🚫 <b>Клиент передумал на этапе подтверждения</b>",
            "cancelled_by_client": "❌ <b>Клиент отменил запись</b>",
            "cancelled_from_reminder": "❌ <b>Клиент отменил запись (из напоминания)</b>",
            "reminder_confirmed": "✅ <b>Клиент подтвердил визит (напоминание)</b>",
            "rescheduled": "🔄 <b>Клиент перенёс запись</b>",
            "abandoned": "⌛ <b>Клиент не дошёл до подтверждения</b>",
            "review_rating": "⭐ <b>Оценка после визита</b>",
            "review_text": "💬 <b>Отзыв после визита</b>",
        }.get(outcome, "ℹ️ <b>Событие</b>")

        summary = llm.summarize(booking, outcome, transcript=transcript) if llm.enabled else None

        client_name = (booking.get("client_name") or "").strip()
        client_line = (f"Клиент: <b>{client_name}</b> (tg: <code>{client_user_id}</code>)"
                       if client_name
                       else f"Клиент (tg id): <code>{client_user_id}</code>")
        lines = [
            header,
            f"Мастер: {master_name or '—'}",
            f"Процедуры: {services_str}",
            f"Когда: {booking.get('datetime') or '—'}",
            f"Телефон: {booking.get('phone') or '—'}",
            client_line,
        ]
        notes = booking.get("notes") or []
        if notes:
            lines.append("")
            lines.append("<b>Скидки и бонусы:</b>")
            for n in notes:
                lines.append(f"• {n}")

        if outcome == "review_rating" and booking.get("rating"):
            lines.append("")
            lines.append(f"Оценка: {'⭐' * int(booking['rating'])} ({booking['rating']}/5)")
        if outcome == "review_text" and booking.get("review_text"):
            lines.append("")
            lines.append(f"<b>Текст отзыва:</b>\n«{booking['review_text']}»")

        if summary:
            lines.append("")
            lines.append(f"<i>Кратко: {summary}</i>")

        try:
            bot.send_message(tg_id, "\n".join(lines), parse_mode="HTML")
        except Exception as e:
            log.error("Не удалось отправить уведомление мастеру %s: %s", master_name, e)

    # ---- Booking flow: start / advance / continue / finalize ----

    def start_booking(chat_id: int, user_id: int,
                      prefilled: parsing.ParsedMessage | None = None) -> None:
        prev = ctx.get_state(user_id)
        bookings = prev.get("bookings") or []
        last_b = bookings[-1] if bookings else None

        if (not prefilled and last_b
                and last_b.get("master") in kb.masters
                and (last_b.get("services") or last_b.get("services_raw"))):
            master = last_b.get("master")
            services = last_b.get("services") or []
            services_str = ", ".join(services) or last_b.get("services_raw") or "—"
            card = prev.get("card") or {}
            phone = card.get("phone") or last_b.get("phone") or "—"
            name = (card.get("name") or "клиент").split()[0]
            text = (
                f"Привет, {name}! 👋\n"
                f"У тебя уже было {len(bookings)} записей у нас.\n\n"
                f"Записать <b>как обычно</b>?\n"
                f"• Мастер: <b>{master}</b>\n"
                f"• Процедуры: {services_str}\n"
                f"• Телефон: {phone}\n\n"
                "Останется только выбрать дату."
            )
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(types.InlineKeyboardButton("✅ Как обычно — к выбору даты",
                                                    callback_data="bk:asusual"))
            markup.add(types.InlineKeyboardButton("🔧 Изменить услуги/мастера",
                                                    callback_data="bk:change"))
            markup.add(types.InlineKeyboardButton("❌ Не записываюсь",
                                                    callback_data="bk:cancelnew"))
            bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            return

        state = {
            "step": ctx.STEP_CHOOSE_MASTER,
            "master": prefilled.master if prefilled else None,
            "services": prefilled.services if prefilled else [],
            "services_raw": prefilled.raw_services_text if prefilled else "",
            "datetime_text": prefilled.datetime_text if prefilled else None,
            "phone": prefilled.phone if prefilled else None,
            "bookings": bookings,
            "total_visits": prev.get("total_visits", 0),
            "off_topic_streak": 0,
            "total_off_topic": prev.get("total_off_topic", 0),
            "transcript": prev.get("transcript", []),
        }
        ctx.save_state(user_id, state)
        advance_booking(chat_id, user_id)

    def advance_booking(chat_id: int, user_id: int) -> None:
        state = ctx.get_state(user_id)

        if not state["master"]:
            state["step"] = ctx.STEP_CHOOSE_MASTER
            ctx.save_state(user_id, state)
            bot.send_message(chat_id, kb.msg("ask_master") or "К какому мастеру?",
                             reply_markup=ctx.masters_inline_kb())
            return

        if not state["services"] and not state["services_raw"]:
            state["step"] = ctx.STEP_CHOOSE_SERVICES
            ctx.save_state(user_id, state)
            master_name = state.get("master") or ""
            bot.send_message(
                chat_id,
                _cart_text([], master_name) + "\n\n<i>Можно также написать услуги текстом — пойму.</i>",
                reply_markup=_services_main_kb([], master_name),
                parse_mode="HTML",
            )
            return

        if not state["datetime_text"]:
            state["step"] = ctx.STEP_ENTER_DATETIME
            ctx.save_state(user_id, state)
            master_obj = kb.masters.get(state.get("master") or "")
            if master_obj:
                bot.send_message(
                    chat_id,
                    f"📅 Выберите день записи (мастер {master_obj.name}):",
                    reply_markup=ctx.date_picker_kb(master_obj),
                )
            else:
                bot.send_message(chat_id, kb.msg("ask_datetime") or "Когда удобно?")
            return

        if not state["phone"]:
            state["step"] = ctx.STEP_ENTER_PHONE
            ctx.save_state(user_id, state)
            # Мягкое согласие на ПДн — один раз, в том же потоке записи (не отдельный сайт)
            if not state.get("pdn_consent"):
                _prompt_pdn_consent(chat_id)
                return
            _prompt_phone(chat_id, state)
            return

        # Всё заполнено — подтверждение
        state["step"] = ctx.STEP_CONFIRM
        ctx.save_state(user_id, state)
        services_str = ", ".join(state["services"]) or state["services_raw"] or "(не указано)"
        text = kb.msg("confirm",
                      master=state["master"],
                      services=services_str,
                      datetime=state["datetime_text"],
                      phone=state["phone"]) or "Проверьте данные и подтвердите запись."

        promo = state.get("promo_code")
        if promo:
            promo_info = kb.promo_codes.get(promo.upper(), {})
            text += f"\n\n🎟 Промокод: <b>{promo}</b>\n<i>{promo_info.get('description', '')}</i>"

        if state.get("design_bonus_available", 0) > 0:
            text += (f"\n\n🎁 У вас доступен реферальный бонус: "
                     f"{kb.referral['referrer_bonus_text']}. "
                     "Будет применён при подтверждении.")

        if state.get("referred_by") and not state.get("referral_used") and state.get("total_visits", 0) == 0:
            text += f"\n\n🎁 {kb.referral['new_client_confirm_text']}"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.row(
            types.InlineKeyboardButton("✅ Подтвердить", callback_data="cf:yes"),
            types.InlineKeyboardButton("🎟 Промокод", callback_data="cf:promo"),
        )
        markup.add(types.InlineKeyboardButton("❌ Отменить запись", callback_data="cf:no"))
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    def continue_booking(message: types.Message, state: dict[str, Any]) -> None:
        user_id = message.from_user.id
        chat_id = message.chat.id
        text = (message.text or "").strip()

        if ctx.is_cancel_inside_dialog(text):
            resched_id = state.pop("rescheduling_from_booking_id", None)
            if resched_id:
                # User started "Перенести" but cancelled before picking new time.
                # Old booking was already freed (calendar event + reminders deleted, DB marked rescheduled).
                # Treat it as cancelled now (no new booking created) + adjust counters.
                bookings = state.get("bookings", [])
                target = next((bb for bb in bookings if bb.get("id") == resched_id), None)
                if target:
                    state["bookings"] = [bb for bb in bookings if bb.get("id") != resched_id]
                    ctx.save_state(user_id, state)
                    if db is not None:
                        try:
                            db.bookings.mark_booking_status(resched_id, "cancelled")
                            db.clients.decrement_visits(user_id, reason="reschedule_abandoned")
                            notes_str = " ".join(target.get("notes") or [])
                            if (kb.referral.get("referrer_bonus_master_note", "") in notes_str
                                    or "дизайн-бонус" in notes_str.lower()):
                                db.clients.update_referral_info(user_id, increment_design_bonuses=1)
                                log.info("reschedule_abandoned: restored design bonus user=%s old_booking=%s", user_id, resched_id)
                        except Exception as ex:
                            log.error("reschedule_abandon cleanup failed user=%s bk=%s: %s", user_id, resched_id, ex)
                    log.info("reschedule abandoned (no new time picked): user=%s, old=%s treated as cancelled", user_id, resched_id)
                    try:
                        bot.send_message(chat_id, "Перенос отменён. Старая запись отменена (время освобождено).", reply_markup=ctx.main_menu_kb())
                        ctx.reset_dialog(user_id)
                        return
                    except Exception:
                        pass
            ctx.reset_dialog(user_id)
            bot.send_message(chat_id, "Запись отменена.", reply_markup=ctx.main_menu_kb())
            return

        step = state["step"]

        if step == ctx.STEP_CHOOSE_MASTER:
            # Сначала пробуем динамический матчер (по kb.masters + aliases),
            # потом fallback на legacy hardcoded.
            lower = text.lower()
            master = parsing._detect_master_for_kb(lower, kb) or parsing._detect_master(lower)
            if not master:
                bot.send_message(chat_id, "Не понял мастера. Выберите кнопкой:",
                                 reply_markup=ctx.masters_inline_kb())
                return
            state["master"] = master

        elif step == ctx.STEP_CHOOSE_SERVICES:
            parsed = parsing.parse(text, kb)
            services = list(parsed.services or [])
            if not services and llm.enabled and llm_usage.can_call(user_id):
                llm_usage.register_call(user_id)
                llm_result = llm.parse(text)
                if llm_result and isinstance(llm_result.get("services"), list):
                    services = [str(s) for s in llm_result["services"] if s]
            current_cart = list(state.get("services") or [])
            for svc_name in services:
                if svc_name not in current_cart:
                    current_cart.append(svc_name)
            state["services"] = current_cart
            existing_raw = state.get("services_raw") or ""
            new_raw = (existing_raw + " | " + text).strip(" |")
            state["services_raw"] = new_raw[:1000]
            ctx.save_state(user_id, state)
            master_name = state.get("master") or ""
            bot.send_message(
                chat_id, _cart_text(current_cart, master_name),
                reply_markup=_services_main_kb(current_cart, master_name),
                parse_mode="HTML",
            )
            return

        elif step == ctx.STEP_ENTER_DATETIME:
            master_obj = kb.masters.get(state.get("master") or "")
            if not master_obj:
                bot.send_message(chat_id, "Ошибка: мастер не найден. Начнём заново — /start.")
                return
            verdict = ctx.resolve_booking_time(
                text, master_obj,
                services=state.get("services"),
                transcript=state.get("transcript"),
            )
            if verdict["status"] != "ok":
                bot.send_message(chat_id, verdict["message"])
                return
            state["datetime_text"] = verdict["datetime_text"]
            state["datetime_iso"] = verdict["datetime"].isoformat()
            state["duration_min"] = verdict["duration_min"]

        elif step == ctx.STEP_ENTER_PHONE:
            if text.strip().lower() == "отмена":
                ctx.reset_dialog(user_id)
                bot.send_message(chat_id, "Запись отменена.", reply_markup=ctx.main_menu_kb())
                return
            if not state.get("pdn_consent"):
                _prompt_pdn_consent(chat_id)
                return
            phone = parsing._detect_phone(text)
            if not phone:
                bot.send_message(chat_id,
                                  "Не похоже на телефон. Введи в любом из форматов:\n"
                                  "<code>+7 999 123-45-67</code>, <code>89991234567</code>, "
                                  "<code>9991234567</code>\n\nИли нажми кнопку «📱 Отправить мой номер».")
                return
            state["phone"] = phone
            _remember_phone(state, phone)
            bot.send_message(chat_id, "✅ Принял.", reply_markup=ctx.main_menu_kb())

        elif step == ctx.STEP_PROMO_INPUT:
            code = text.strip().upper()
            promo = kb.promo_codes.get(code)
            if not promo or not promo.get("valid"):
                bot.send_message(chat_id, "Такой промокод не найден. Попробуйте ещё раз или вернитесь к подтверждению.")
                return
            state["promo_code"] = code
            state["step"] = ctx.STEP_CONFIRM
            ctx.save_state(user_id, state)
            bot.send_message(chat_id, f"✅ Промокод <b>{code}</b> применён: {promo['description']}",
                              parse_mode="HTML")
            advance_booking(chat_id, user_id)
            return

        elif step == ctx.STEP_CONFIRM:
            lower = text.lower()
            if any(w in lower for w in ("да", "верно", "подтверж", "ок", "yes")):
                finalize_booking(chat_id, user_id, state)
                return
            if any(w in lower for w in ("нет", "не верно", "заново", "сначала", "отмен")):
                booking_preview = {
                    "master": state.get("master"),
                    "services": state.get("services") or [],
                    "services_raw": state.get("services_raw") or "",
                    "datetime": state.get("datetime_text"),
                    "phone": state.get("phone"),
                }
                notify_master(user_id, booking_preview, outcome="cancelled",
                              transcript=state.get("transcript"))
                llm_usage.reset(user_id)
                ctx.reset_dialog(user_id)
                bot.send_message(chat_id, "Хорошо, начнём заново. Напишите «записаться».",
                                 reply_markup=ctx.main_menu_kb())
                return
            bot.send_message(chat_id, "Нажмите кнопку — Подтвердить, Промокод или Отменить.")
            return

        ctx.save_state(user_id, state)
        advance_booking(chat_id, user_id)

    def _persist_pg_slot(booking: dict[str, Any], user_id: int, reschedule_id: str | None) -> bool:
        """Take the slot in Postgres. False = tell the client to pick another time."""
        try:
            from database.pg import pg_enabled
            from database.appointments import SlotConflict, reschedule_take, try_hold
        except Exception as e:
            log.warning("pg slot layer unavailable: %s", e)
            return True
        if not pg_enabled():
            return True
        iso = booking.get("datetime_iso")
        if not iso:
            bot.send_message(booking.get("chat_id"), "Не понял время записи. Выберите слот ещё раз.")
            return False
        try:
            start = ctx.parse_iso_aware(iso)
            duration_min = int(booking.get("duration_min") or _default_service_duration_min())
            end = start + _dt.timedelta(minutes=duration_min)
            kwargs = dict(
                master_name=booking.get("master") or "",
                start_at=start,
                end_at=end,
                client_tg_id=user_id,
                client_name=booking.get("client_name") or "",
                client_phone=booking.get("phone"),
                service_titles=list(booking.get("services") or []),
                sqlite_booking_id=booking.get("id"),
                duration_min=duration_min,
                actor=f"client:{user_id}",
            )
            if reschedule_id:
                appt, created = reschedule_take(old_id=reschedule_id, **kwargs)
            else:
                appt, created = try_hold(**kwargs)
            booking["pg_appointment_id"] = str(appt.id)
            booking["status"] = appt.status
            booking["pg_idempotent"] = not created
            log.info(
                "pg slot %s status=%s sqlite=%s",
                appt.id, appt.status, booking.get("id"),
            )
            return True
        except SlotConflict:
            log.info("pg slot conflict user=%s iso=%s", user_id, iso)
            msg = "Это время только что заняли. Вот ближайшие свободные."
            try:
                from database.appointments import busy_intervals
                parsed = ctx.parse_iso_aware(iso)
                day_start = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = day_start + _dt.timedelta(days=1)
                dur = int(booking.get("duration_min") or _default_service_duration_min())
                busy = busy_intervals(booking.get("master") or "", day_start, day_end)
                alts = ctx.find_free_slots(busy, day_start, day_end, parsed, duration_min=dur, count=3)
                if alts:
                    msg += "\n" + "\n".join(f"• {ctx.format_slot(s)}" for s in alts)
            except Exception:
                pass
            try:
                bot.send_message(booking.get("chat_id"), msg)
            except Exception:
                pass
            return False
        except Exception as e:
            log.exception("pg slot persist failed: %s", e)
            try:
                bot.send_message(
                    booking.get("chat_id"),
                    "Не удалось закрепить слот. Попробуйте ещё раз — старое время не трогали.",
                )
            except Exception:
                pass
            return False

    def _release_old_after_reschedule(old_booking: dict[str, Any] | None) -> None:
        if not old_booking:
            return
        event_id = old_booking.get("calendar_event_id")
        if event_id:
            try:
                from database.pg import calendar_write_enabled
                if calendar_write_enabled():
                    svc = ctx.calendar_service()
                    master = kb.masters.get(old_booking.get("master") or "")
                    if svc and master and master.calendar_id:
                        from services.calendar import delete_event
                        delete_event(svc, master.calendar_id, event_id)
            except Exception as e:
                log.warning("reschedule: old calendar delete after success: %s", e)
        for job_id in old_booking.get("reminder_job_ids") or []:
            try:
                if scheduler:
                    scheduler.remove_job(job_id)
            except Exception:
                pass
        if old_booking.get("review_job_id"):
            try:
                if scheduler:
                    scheduler.remove_job(old_booking["review_job_id"])
            except Exception:
                pass
        if db is not None and old_booking.get("id"):
            try:
                db.bookings.mark_booking_status(old_booking["id"], "rescheduled")
            except Exception as e:
                log.error("reschedule: sqlite mark failed: %s", e)

    def finalize_booking(chat_id: int, user_id: int, state: dict[str, Any]) -> None:
        """
        Finalizes a booking.

        Booking statuses and their meaning (see also database/schema.py):
          - confirmed   → active booking, will count toward visits when completed
          - rescheduled → moved to another time (old record marked this way)
          - cancelled   → never happened / client cancelled
          - completed   → visit took place
          - no_show     → client didn't show

        Important counter rules:
          - Only 'confirmed' and 'completed' bookings increase total_visits in normal flow.
          - When cancelling a confirmed booking that used a design bonus, the bonus is restored (in most paths).
          - Rescheduling does NOT restore a used design bonus.
        """
        log.info("finalize_booking started: user=%s, master=%s, services=%s, datetime=%s",
                 user_id, state.get("master"), state.get("services"), state.get("datetime_text"))

        promo_code = state.get("promo_code")
        promo_info = kb.promo_codes.get(promo_code.upper()) if promo_code else None

        is_referred_first_visit = (
            state.get("referred_by")
            and not state.get("referral_used")
            and state.get("total_visits", 0) == 0
        )
        referral_note = kb.referral["new_client_master_note"] if is_referred_first_visit else ""
        bonus_used = state.get("design_bonus_available", 0) > 0
        bonus_note = kb.referral["referrer_bonus_master_note"] if bonus_used else ""

        notes = [n for n in (
            f"Промокод {promo_code}: {promo_info['description']}" if promo_info else "",
            referral_note,
            bonus_note,
        ) if n]

        booking = {
            "id": uuid.uuid4().hex[:12],
            "master": state["master"],
            "services": state["services"],
            "services_raw": state["services_raw"],
            "datetime": state["datetime_text"],
            "datetime_iso": state.get("datetime_iso"),
            "duration_min": state.get("duration_min", _default_service_duration_min()),
            "phone": state["phone"],
            "client_name": (state.get("card") or {}).get("name") or "",
            "chat_id": chat_id,
            "user_id": user_id,
            "calendar_event_id": None,
            "reminder_job_ids": [],
            "promo_code": promo_code,
            "notes": notes,
        }

        # Store reschedule linkage inside notes for now (no DB migration needed)
        reschedule_from = state.get("rescheduling_from_booking_id")
        if reschedule_from:
            booking["notes"] = notes + [f"rescheduled_from:{reschedule_from}"]

        # === Early race condition check ===
        # Проверяем доступность слота ДО любой записи в БД и ДО отправки успеха клиенту.
        # Это сильно снижает вероятность грязных состояний при одновременных записях.
        availability = _is_slot_available_early(booking)
        if not availability.get("available", True):
            log.warning("finalize_booking: early availability check failed for booking %s", booking.get("id"))
            chat_id = booking.get("chat_id")
            if chat_id:
                try:
                    bot.send_message(chat_id, availability.get("message", "Это время больше недоступно."))
                except Exception:
                    pass

            state["datetime_text"] = None
            state["datetime_iso"] = None
            state["step"] = ctx.STEP_ENTER_DATETIME
            ctx.save_state(user_id, state)
            return

        log.info("finalize_booking: early check passed for booking %s", booking.get("id"))

        is_reschedule = bool(state.get("rescheduling_from_booking_id"))
        reschedule_id = state.get("rescheduling_from_booking_id")
        old_booking = None
        if reschedule_id:
            old_booking = next(
                (b for b in (state.get("bookings") or []) if b.get("id") == reschedule_id),
                None,
            )

        # Redis lock — быстрый отказ, не замена unique в Postgres. TTL 10s по спеке.
        try:
            master_name = booking.get("master") or ""
            iso = booking.get("datetime_iso") or ""
            if master_name and iso and state_store.redis:
                lock_key = f"slot:{master_name}:{iso}"
                lock_acquired = bool(
                    state_store.redis.set(lock_key, str(user_id), nx=True, ex=10)
                )
                if not lock_acquired:
                    bot.send_message(
                        chat_id,
                        "Это время только что заняли. Вот ближайшие свободные.",
                    )
                    state["datetime_text"] = None
                    state["datetime_iso"] = None
                    state["step"] = ctx.STEP_ENTER_DATETIME
                    ctx.save_state(user_id, state)
                    return
        except Exception as e:
            log.warning("finalize_booking: slot lock failed (Postgres unique поймает): %s", e)

        if not _persist_pg_slot(booking, user_id, reschedule_id):
            state["datetime_text"] = None
            state["datetime_iso"] = None
            state["step"] = ctx.STEP_ENTER_DATETIME
            ctx.save_state(user_id, state)
            return

        if booking.get("pg_idempotent"):
            log.info(
                "finalize_booking idempotent user=%s pg=%s",
                user_id, booking.get("pg_appointment_id"),
            )
            ctx.reply(chat_id, "booked", master=state.get("master") or "")
            ctx.reset_dialog(user_id)
            return

        # === ВАЖНО: upsert_client ОБЯЗАТЕЛЬНО ДО create_booking ===
        # У bookings.user_id есть FOREIGN KEY → clients(user_id) с CASCADE.
        # Для нового клиента (первая запись) в clients ещё нет строки → INSERT
        # в bookings упал бы с FK constraint failed. Раньше это молча терялось
        # в `except Exception`, и записи новичков не попадали в long-term DB
        # (нет статистики, нет rating/review_text, нет AI-аналитики).
        if db is not None:
            try:
                card = state.get("card") or {}
                db.clients.upsert_client(
                    user_id,
                    chat_id=chat_id,
                    name=card.get("name"),
                    notes=card.get("notes"),
                )
            except Exception as e:
                log.error("finalize_booking: upsert_client failed for user=%s: %s", user_id, e)
                # Не пробрасываем — следующий create_booking всё равно попробует,
                # просто будет видно в логах.

        # === Persist booking to long-term database (for history + AI analytics) ===
        if db is not None:
            try:
                db.bookings.create_booking(booking)
                log.info("finalize_booking: long-term DB record created for %s", booking.get("id"))
            except Exception as e:
                log.error("finalize_booking: Failed to persist booking %s to long-term DB: %s", booking.get("id"), e)
                # Do not break the existing flow

        bookings = list(state.get("bookings") or [])

        if is_reschedule:
            # Replace the old booking with the new one.
            # This ensures the booking remains visible in "Мои записи" during the entire reschedule process.
            reschedule_id = state.get("rescheduling_from_booking_id")
            bookings = [b for b in bookings if b.get("id") != reschedule_id]
            bookings.append(booking)
        else:
            bookings.append(booking)

        if len(bookings) > 50:
            bookings = bookings[-50:]

        # On reschedule we do NOT increment total_visits again (it's the same visit, just moved)
        if is_reschedule:
            total_visits = state.get("total_visits", 0)
        else:
            total_visits = state.get("total_visits", 0) + 1

        # Design bonus list mutation — клиент уже upsert'нут выше, тут только мутации.
        if db is not None:
            try:
                # Note: increment_visits is deferred until after final calendar success
                # to reduce dirty data on race conditions.
                # On reschedule we do not consume another design bonus.
                if bonus_used and not is_reschedule:
                    db.clients.update_referral_info(
                        user_id,
                        increment_design_bonuses=-1,
                    )
            except Exception as e:
                log.error("Failed to update referral info in long-term DB: %s", e)

        new_state = {
            "step": ctx.STEP_IDLE,
            "master": None, "services": [], "services_raw": "",
            "datetime_text": None, "datetime_iso": None, "phone": None,
            "promo_code": None,
            "off_topic_streak": 0, "total_off_topic": 0,
            "transcript": [],
            "bookings": bookings,
            "total_visits": total_visits,
            "referred_by": state.get("referred_by"),
            "referral_used": state.get("referral_used", False) or bool(is_referred_first_visit),
            "design_bonus_available": max(0, state.get("design_bonus_available", 0) - (1 if (bonus_used and not is_reschedule) else 0)),
            "referrals_brought_count": state.get("referrals_brought_count", 0),
            "card": state.get("card", {"name": "", "notes": "", "birthday": "", "tags": []}),
        }

        # Clear the reschedule flag after use
        state.pop("rescheduling_from_booking_id", None)

        # Reset usage and track user early (no longer need intermediate save)
        state_store.track_user(user_id)
        llm_usage.reset(user_id)

        if is_reschedule:
            _release_old_after_reschedule(old_booking)

        # Postgres SoT: calendar + T-24h/T-2h уходят в outbox (воркер).
        # Без Postgres — прежний путь, но Calendar всё равно не откатывает запись.
        try:
            from database.pg import pg_enabled
            pg_sot = pg_enabled()
        except Exception:
            pg_sot = False
        if not pg_sot:
            schedule_reminder(booking)
        # Календарь всегда сразу (проекция). Ошибка Google запись в боте не откатывает.
        # Outbox догонит, если insert не вышел с первого раза.
        sync_with_google_calendar(booking)
        if pg_sot:
            try:
                from database.worker import process_outbox
                process_outbox(bot=bot, limit=10)
            except Exception as e:
                log.warning("finalize: outbox kick: %s", e)

        _schedule_review(booking)

        # Referral award moved here for safety.
        if is_referred_first_visit:
            try:
                referrer_id = int(state["referred_by"])
                ref_state = state_store.get(referrer_id) or {}
                ref_state["design_bonus_available"] = ref_state.get("design_bonus_available", 0) + 1
                ref_state["referrals_brought_count"] = ref_state.get("referrals_brought_count", 0) + 1
                state_store.set(referrer_id, ref_state)

                if db is not None:
                    try:
                        db.clients.update_referral_info(
                            referrer_id,
                            increment_referrals_brought=True,
                            increment_design_bonuses=1,
                        )
                    except Exception as e:
                        log.error("Failed to persist referrer bonus to long-term DB: %s", e)

                try:
                    bot.send_message(
                        referrer_id,
                        "🎉 Ваш друг записался к нам по вашей ссылке — спасибо за рекомендацию!\n\n"
                        f"У вас появился бонус: <b>{kb.referral['referrer_bonus_offer']}</b>.\n"
                        "Применится автоматически при следующей записи.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    log.warning("referral notify failed: %s", e)
            except Exception as e:
                log.warning("referral award failed: %s", e)

        ctx.reply(chat_id, "booked", master=state["master"])
        notify_master(user_id, booking, transcript=state.get("transcript"))

        # Deferred DB updates after final success (only if we survived calendar check)
        if db is not None:
            try:
                db.clients.increment_visits(user_id)
            except Exception as e:
                log.error("Failed to increment_visits in long-term DB after success: %s", e)

        log.info("finalize_booking SUCCESS: booking %s finalized for user %s", booking.get("id"), user_id)

        # Single final save: all mutations from side effects (calendar_event_id, reminder_job_ids, review_job_id)
        # are now inside the booking object that lives in new_state["bookings"].
        ctx.save_state(user_id, new_state)

    # ---- handle_my_bookings ----

    def handle_my_bookings(chat_id: int, user_id: int) -> None:
        state = ctx.get_state(user_id)
        bookings = list(state.get("bookings") or [])
        now = ctx.now_moscow()
        resched_id = state.get("rescheduling_from_booking_id")

        pg_upcoming: list[dict] = []
        pg_ok = False
        try:
            from database.pg import pg_enabled
            from database.appointments import list_upcoming_for_client
            if pg_enabled():
                pg_upcoming = list_upcoming_for_client(user_id, now)
                pg_ok = True
        except Exception as e:
            log.warning("handle_my_bookings pg: %s", e)

        if pg_ok:
            upcoming = []
            for row in pg_upcoming:
                iso = row.get("datetime_iso") or row.get("start_at")
                try:
                    dt = ctx.parse_iso_aware(iso)
                except Exception:
                    continue
                when = ctx.format_slot(dt) if hasattr(ctx, "format_slot") else dt.strftime("%d.%m %H:%M")
                b = {
                    "id": row["id"],
                    "pg_appointment_id": row["id"],
                    "sqlite_booking_id": row.get("sqlite_booking_id"),
                    "master": row.get("master"),
                    "services": row.get("services") or [],
                    "datetime": when,
                    "datetime_iso": iso,
                    "duration_min": row.get("duration_min") or 60,
                    "phone": row.get("client_phone"),
                    "calendar_event_id": row.get("google_event_id"),
                    "google_event_id": row.get("google_event_id"),
                    "status": row.get("status") or "confirmed",
                    "chat_id": chat_id,
                    "user_id": user_id,
                }
                upcoming.append((b, dt))
            # fall through to render
            keyboard = types.InlineKeyboardMarkup(row_width=2)
            lines = ["📋 <b>Предстоящие записи:</b>"]
            if not upcoming:
                kb_empty = types.InlineKeyboardMarkup()
                kb_empty.add(types.InlineKeyboardButton("📅 Записаться", callback_data="empty:book"))
                bot.send_message(
                    chat_id,
                    "📋 Предстоящих записей пока нет.\nСамое время записаться!",
                    reply_markup=kb_empty,
                    parse_mode="HTML",
                )
                return
            for i, (b, dt) in enumerate(upcoming, 1):
                services = ", ".join(b.get("services") or []) or "—"
                lines.append(
                    f"\n<b>{i}.</b> {b.get('datetime')}\n"
                    f"   Мастер: {b.get('master') or '—'}\n"
                    f"   Процедуры: {services}"
                )
                keyboard.row(
                    types.InlineKeyboardButton(f"🔄 Перенести #{i}",
                                                callback_data=f"reschedule_bk:{b['id']}"),
                    types.InlineKeyboardButton(f"❌ Отменить #{i}",
                                                callback_data=f"cancel_bk:{b['id']}"),
                )
            bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard, parse_mode="HTML")
            return

        # === DB reconcile + reconstruction ===
        # Раньше при истечении TTL Redis-state клиент терял свои bookings и
        # видел "Записей нет", хотя в long-term DB всё на месте. TTL поднят
        # до 30 дней, но это всё равно может случиться — добавляем восстановление.
        db_rows: list[dict] = []
        if db is not None:
            try:
                db_rows = db.bookings.get_bookings_for_user(user_id, limit=50)
            except Exception as ex:
                log.warning("handle_my_bookings: DB fetch failed for user=%s: %s", user_id, ex)

        # Reconstruction: future confirmed bookings из DB, которых нет в state.
        # Только confirmed — cancelled/rescheduled/no_show не возвращаем.
        state_bk_ids = {b.get("id") for b in bookings if b.get("id")}
        reconstructed = 0
        for row in db_rows:
            rid = row.get("id")
            if not rid or rid in state_bk_ids:
                continue
            if (row.get("status") or "confirmed") != "confirmed":
                continue
            iso = row.get("datetime_iso")
            if not iso:
                continue
            try:
                dt = ctx.parse_iso_aware(iso)
            except Exception:
                continue
            if dt <= now:
                continue  # прошлое — в "Мои записи" не показываем
            bookings.append({
                "id":               rid,
                "master":           row.get("master"),
                "services":         row.get("services") or [],
                "services_raw":     row.get("services_raw") or "",
                "datetime":         row.get("datetime"),
                "datetime_iso":     iso,
                "duration_min":     row.get("duration_min") or 60,
                "phone":            row.get("phone"),
                "calendar_event_id": row.get("calendar_event_id"),
                "reminder_job_ids": row.get("reminder_job_ids") or [],
                "promo_code":       row.get("promo_code"),
                "notes":            row.get("notes") or [],
                "chat_id":          chat_id,
                "user_id":          user_id,
            })
            state_bk_ids.add(rid)
            reconstructed += 1

        if reconstructed:
            log.info("handle_my_bookings: восстановлено %d записей из DB для user=%s "
                     "(state TTL expired или sync gap)", reconstructed, user_id)
            state["bookings"] = bookings
            # Сохраняем — иначе следующий раз снова реконструкция; плюс кнопки
            # «Перенести/Отменить» зовут через state.bookings.
            ctx.save_state(user_id, state)

        # Теперь собираем upcoming из (возможно восстановленного) bookings
        upcoming: list = []
        for b in bookings:
            iso = b.get("datetime_iso")
            if not iso:
                continue
            try:
                dt = ctx.parse_iso_aware(iso)
            except Exception:
                continue
            if dt > now:
                upcoming.append((b, dt))
        upcoming.sort(key=lambda x: x[1])

        # Reconcile: ghost-cleanup — если в state booking активный, а в DB он
        # cancelled/rescheduled/no_show → выкинуть из показа.
        if db_rows:
            db_statuses = {row.get("id"): (row.get("status") or "confirmed") for row in db_rows}
            cleaned = []
            for b, dt in upcoming:
                bid = b.get("id")
                if bid and bid in db_statuses:
                    st = db_statuses[bid]
                    if st in ("cancelled", "rescheduled", "no_show"):
                        log.info("handle_my_bookings: drop ghost %s (DB status=%s, user=%s)",
                                 bid, st, user_id)
                        continue
                cleaned.append((b, dt))
            upcoming = cleaned

        # Stale reschedule flag cleanup (old booking gone or filtered)
        if resched_id and not any((b.get("id") == resched_id) for b, _dt in upcoming):
            state.pop("rescheduling_from_booking_id", None)
            resched_id = None
            # no immediate save to avoid write spam; will be cleaned on next dialog step

        if not upcoming:
            past_count = sum(1 for b in bookings if b.get("datetime_iso"))
            kb_empty = types.InlineKeyboardMarkup()
            kb_empty.add(types.InlineKeyboardButton("📅 Записаться", callback_data="empty:book"))
            text = (
                "📋 Предстоящих записей пока нет.\n\n"
                + (f"<i>Всего у нас уже было {past_count} визитов 💕</i>" if past_count
                   else "Самое время записаться!")
            )
            if resched_id:
                text = "🔄 Перенос записи был начат, но не завершён.\nСтарая запись больше не активна.\n\n" + text
            bot.send_message(chat_id, text, reply_markup=kb_empty, parse_mode="HTML")
            return

        keyboard = types.InlineKeyboardMarkup(row_width=2)
        lines = ["📋 <b>Предстоящие записи:</b>"]
        if resched_id:
            lines.append("🔄 <i>Одна из записей в процессе переноса — выберите новое время.</i>")

        for i, (b, dt) in enumerate(upcoming, 1):
            services = ", ".join(b.get("services") or []) or b.get("services_raw") or "—"
            when = b.get("datetime") or "—"
            master = b.get("master") or "—"
            marker = ""
            if b.get("id") == resched_id:
                marker = " 🔄 <i>(переносится)</i>"
            lines.append(f"\n<b>{i}.</b> {when}{marker}\n   Мастер: {master}\n   Процедуры: {services}")
            bk_id = b.get("id")
            if bk_id:
                keyboard.row(
                    types.InlineKeyboardButton(f"🔄 Перенести #{i}",
                                                callback_data=f"reschedule_bk:{bk_id}"),
                    types.InlineKeyboardButton(f"❌ Отменить #{i}",
                                                callback_data=f"cancel_bk:{bk_id}"),
                )

        total_visits = state.get("total_visits", 0)
        if total_visits >= 5:
            if total_visits in (5, 10, 20, 30, 50):
                lines.append(f"\n🎉 У вас уже <b>{total_visits} записей</b> с нами — спасибо!")
            else:
                lines.append(f"\n<i>Записей всего: {total_visits}</i>")

        bot.send_message(chat_id, "\n".join(lines),
                         reply_markup=keyboard if keyboard.keyboard else None,
                         parse_mode="HTML")

    # ---- Callback handlers ----

    @bot.callback_query_handler(func=lambda c: c.data.startswith("pdn:"))
    def on_pdn_consent(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        state = ctx.get_state(user_id)
        action = call.data.split(":", 1)[1]

        if action == "rules":
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, kb.rules or "Правила пока не заполнены.")
            return
        if action == "privacy":
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                kb.msg("privacy") or (
                    "🔒 Данные нужны только для записи и связи.\n"
                    "Полный текст: команда /privacy"
                ),
                parse_mode="HTML",
            )
            return
        if action != "ok":
            bot.answer_callback_query(call.id)
            return

        state["pdn_consent"] = True
        try:
            state["pdn_consent_at"] = ctx.now_moscow().isoformat(timespec="seconds")
        except Exception:
            pass
        state["step"] = ctx.STEP_ENTER_PHONE
        ctx.save_state(user_id, state)
        bot.answer_callback_query(call.id, "Спасибо")
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass
        _prompt_phone(call.message.chat.id, state)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("phc:"))
    def on_phone_confirm(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        state = ctx.get_state(user_id)
        if state.get("step") != ctx.STEP_ENTER_PHONE:
            bot.answer_callback_query(call.id, "Этот выбор уже неактуален")
            return
        action = call.data.split(":", 1)[1]

        if action == "yes":
            known = _get_known_phone(state)
            if not known:
                bot.answer_callback_query(call.id, "Не нашёл сохранённый номер")
                return
            state["phone"] = known
            _remember_phone(state, known)
            ctx.save_state(user_id, state)
            bot.answer_callback_query(call.id, f"Принял: {known}")
            try:
                bot.edit_message_text(
                    f"📞 Использую номер: <b>{known}</b>",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            advance_booking(call.message.chat.id, user_id)
            return

        if action == "change":
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
            contact_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            contact_kb.row(types.KeyboardButton("📱 Отправить мой номер", request_contact=True))
            contact_kb.row("Отмена")
            bot.send_message(
                call.message.chat.id,
                "Окей. Пришли новый номер — нажми кнопку ниже или введи вручную:",
                reply_markup=contact_kb,
            )
            return

    @bot.message_handler(content_types=["contact"])
    def on_contact(message: types.Message) -> None:
        if message.chat.type != "private":
            return
        contact = message.contact
        if not contact or not contact.phone_number:
            return
        user_id = message.from_user.id
        state = ctx.get_state(user_id)
        if state.get("step") != ctx.STEP_ENTER_PHONE:
            return
        if not state.get("pdn_consent"):
            _prompt_pdn_consent(message.chat.id)
            return
        if contact.user_id and contact.user_id != user_id:
            bot.send_message(message.chat.id,
                              "Похоже, это чужой контакт. Введи свой номер вручную, "
                              "или ещё раз нажми кнопку — поделимся твоим.",
                              reply_markup=ctx.main_menu_kb())
            return
        phone = contact.phone_number.strip()
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("+")
        state["phone"] = phone
        _remember_phone(state, phone)
        ctx.save_state(user_id, state)
        bot.send_message(message.chat.id, f"✅ Принял номер: {phone}",
                          reply_markup=ctx.main_menu_kb())
        advance_booking(message.chat.id, user_id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("bk:"))
    def on_quickbook_callback(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        action = call.data.split(":", 1)[1]
        state = ctx.get_state(user_id)
        bookings = state.get("bookings") or []
        last_b = bookings[-1] if bookings else None

        if action == "asusual":
            if not last_b:
                bot.answer_callback_query(call.id, "Прошлая запись не найдена")
                return
            state["step"] = ctx.STEP_ENTER_DATETIME
            state["master"] = last_b.get("master")
            state["services"] = list(last_b.get("services") or [])
            state["services_raw"] = last_b.get("services_raw") or ""
            state["datetime_text"] = None
            state["datetime_iso"] = None
            card_phone = (state.get("card") or {}).get("phone")
            state["phone"] = card_phone or last_b.get("phone")
            ctx.save_state(user_id, state)
            bot.answer_callback_query(call.id, "Открываю выбор даты")
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                                message_id=call.message.message_id,
                                                reply_markup=None)
            except Exception:
                pass
            master_obj = kb.masters.get(last_b.get("master") or "")
            if master_obj:
                bot.send_message(
                    call.message.chat.id,
                    f"📅 Выбери день записи (мастер {master_obj.name}):",
                    reply_markup=ctx.date_picker_kb(master_obj),
                )
            return

        if action == "change":
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                                message_id=call.message.message_id,
                                                reply_markup=None)
            except Exception:
                pass
            state["master"] = None
            state["services"] = []
            state["services_raw"] = ""
            state["datetime_text"] = None
            state["datetime_iso"] = None
            state["phone"] = None
            state["step"] = ctx.STEP_CHOOSE_MASTER
            ctx.save_state(user_id, state)
            advance_booking(call.message.chat.id, user_id)
            return

        if action == "cancelnew":
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_text("Хорошо. Возвращайся, когда будет нужно.",
                                        chat_id=call.message.chat.id,
                                        message_id=call.message.message_id)
            except Exception:
                pass
            return

    @bot.callback_query_handler(func=lambda c: c.data.startswith("svc:"))
    def on_service_callback(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        state = ctx.get_state(user_id)
        if state.get("step") != ctx.STEP_CHOOSE_SERVICES:
            bot.answer_callback_query(call.id, "Этот выбор уже неактуален")
            return

        parts = call.data.split(":")
        action = parts[1]
        cart = list(state.get("services") or [])
        master_name = state.get("master") or ""

        if action == "home":
            new_text = _cart_text(cart, master_name)
            new_kb = _services_main_kb(cart, master_name)
        elif action == "c":
            cat_idx = int(parts[2]) if len(parts) > 2 else 0
            new_text = _cart_text(cart, master_name)
            new_kb = _services_cat_kb(cat_idx, cart, master_name)
        elif action == "t":
            svc_idx = int(parts[2]) if len(parts) > 2 else -1
            if svc_idx < 0 or svc_idx >= len(kb.services):
                bot.answer_callback_query(call.id, "Услуга не найдена")
                return
            svc_name = kb.services[svc_idx].name
            live_names = {s.name for s in _live_services(master_name)}
            if svc_name not in live_names:
                bot.answer_callback_query(call.id, "Услуга выключена")
                return
            cat_idx = None
            cats = _categories_for_master(master_name)
            for i, (_, services) in enumerate(cats):
                if any(s.name == svc_name for s in services):
                    cat_idx = i
                    break
            if svc_name in cart:
                cart.remove(svc_name)
                note = "Убрано"
            else:
                cart.append(svc_name)
                note = "Добавлено"
            state["services"] = cart
            state["services_raw"] = ", ".join(cart)
            ctx.save_state(user_id, state)
            new_text = _cart_text(cart, master_name)
            new_kb = _services_cat_kb(cat_idx or 0, cart, master_name) if cat_idx is not None else _services_main_kb(cart, master_name)
            bot.answer_callback_query(call.id, f"{note}: {svc_name}")
        elif action == "done":
            if not cart:
                bot.answer_callback_query(call.id, "Выберите хотя бы одну услугу", show_alert=True)
                return
            state["services"] = cart
            state["services_raw"] = ", ".join(cart)
            ctx.save_state(user_id, state)
            try:
                bot.edit_message_text(
                    _cart_text(cart, master_name) + "\n\n✅ Услуги выбраны.",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="HTML",
                )
            except Exception:
                pass
            bot.answer_callback_query(call.id, f"Выбрано: {len(cart)}")
            advance_booking(call.message.chat.id, user_id)
            return
        else:
            bot.answer_callback_query(call.id)
            return

        try:
            bot.edit_message_text(
                new_text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=new_kb, parse_mode="HTML",
            )
        except Exception as e:
            log.warning("svc edit_message_text: %s", e)
        if action not in ("t",):
            bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("dt:"))
    def on_datetime_callback(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        state = ctx.get_state(user_id)
        if state.get("step") != ctx.STEP_ENTER_DATETIME:
            bot.answer_callback_query(call.id, "Этот выбор уже неактуален")
            return

        master_name = state.get("master") or ""
        master = kb.masters.get(master_name)
        if not master:
            bot.answer_callback_query(call.id, "Мастер не найден")
            return

        parts = call.data.split(":")
        action = parts[1]

        if action == "manual":
            try:
                bot.edit_message_text(
                    "Напишите дату и время текстом — например: «завтра в 14:00» или «5 июня в 18:00».",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                )
            except Exception:
                pass
            bot.answer_callback_query(call.id)
            return

        if action == "back":
            try:
                bot.edit_message_text(
                    f"📅 Выберите день записи (мастер {master_name}):",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=ctx.date_picker_kb(master),
                )
            except Exception:
                pass
            bot.answer_callback_query(call.id)
            return

        if action == "busy":
            bot.answer_callback_query(call.id, "Это время занято", show_alert=False)
            return

        if action == "noop":
            bot.answer_callback_query(call.id, "Это подсказка, нажми «Ввести день текстом»",
                                      show_alert=False)
            return

        if action == "d":
            date_iso = parts[2]
            text, ikb = ctx.time_picker_kb(date_iso, master, state.get("services") or [])
            try:
                bot.edit_message_text(
                    text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=ikb, parse_mode="HTML",
                )
            except Exception as e:
                log.warning("dt time edit: %s", e)
            bot.answer_callback_query(call.id)
            return

        if action == "t":
            date_iso = parts[2]
            hour = int(parts[3])
            tz = ctx.moscow_tz()
            slot_dt = _dt.datetime.combine(_dt.date.fromisoformat(date_iso), _dt.time(hour, 0), tzinfo=tz)

            # Надёжный путь: передаём уже готовый datetime, минуя dateparser полностью.
            # Это решает проблему "Не понял дату" при выборе времени из пикера.
            verdict = ctx.resolve_booking_time(
                "",  # текст игнорируется при preparsed_dt
                master,
                services=state.get("services") or [],
                transcript=state.get("transcript"),
                preparsed_dt=slot_dt,
            )

            if verdict["status"] != "ok":
                # Это почти не должно случаться при выборе из пикера, но на всякий случай
                bot.answer_callback_query(call.id, "Слот больше недоступен", show_alert=True)
                try:
                    bot.edit_message_text(
                        "⚠️ " + verdict.get("message", "Это время больше недоступно. Выберите другое."),
                        chat_id=call.message.chat.id, message_id=call.message.message_id,
                    )
                except Exception:
                    pass
                return

            state["datetime_text"] = verdict["datetime_text"]
            state["datetime_iso"] = verdict["datetime"].isoformat()
            state["duration_min"] = verdict["duration_min"]
            ctx.save_state(user_id, state)

            try:
                bot.edit_message_text(
                    f"✅ Дата: <b>{verdict['datetime_text']}</b>",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    parse_mode="HTML",
                )
            except Exception:
                pass

            bot.answer_callback_query(call.id, "Время выбрано")
            advance_booking(call.message.chat.id, user_id)
            return

        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("cf:"))
    def on_confirm_button(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        state = ctx.get_state(user_id)
        if state.get("step") not in (ctx.STEP_CONFIRM, ctx.STEP_PROMO_INPUT):
            bot.answer_callback_query(call.id, "Это уже неактуально")
            return
        action = call.data.split(":", 1)[1]

        if action == "yes":
            bot.answer_callback_query(call.id, "Подтверждаю")
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                               message_id=call.message.message_id,
                                               reply_markup=None)
            except Exception:
                pass
            finalize_booking(call.message.chat.id, user_id, state)
            return

        if action == "no":
            booking_preview = {
                "master": state.get("master"),
                "services": state.get("services") or [],
                "services_raw": state.get("services_raw") or "",
                "datetime": state.get("datetime_text"),
                "phone": state.get("phone"),
            }
            notify_master(user_id, booking_preview, outcome="cancelled",
                          transcript=state.get("transcript"))
            llm_usage.reset(user_id)
            ctx.reset_dialog(user_id)
            bot.answer_callback_query(call.id, "Отменено")
            try:
                bot.edit_message_text(
                    "Хорошо, начнём заново. Напишите «записаться».",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                )
            except Exception:
                pass
            return

        if action == "promo":
            state["step"] = ctx.STEP_PROMO_INPUT
            ctx.save_state(user_id, state)
            bot.answer_callback_query(call.id)
            back_kb = types.InlineKeyboardMarkup()
            back_kb.add(types.InlineKeyboardButton(
                "◀️ Без промокода — назад к подтверждению",
                callback_data="cf:promoback",
            ))
            # Подсказку «например: КОД» берём из первого активного промокода —
            # если их нет вообще, просто просим ввести без примера.
            example_code = next((c for c, info in kb.promo_codes.items()
                                  if info.get("valid")), None)
            prompt = ("🎟 Введи промокод текстом"
                       + (f" (например: <code>{example_code}</code>):" if example_code else ":"))
            try:
                bot.edit_message_text(
                    prompt,
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=back_kb, parse_mode="HTML",
                )
            except Exception:
                pass
            return

        if action == "promoback":
            state["step"] = ctx.STEP_CONFIRM
            ctx.save_state(user_id, state)
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            advance_booking(call.message.chat.id, user_id)
            return

    @bot.callback_query_handler(func=lambda c: c.data.startswith("rv:"))
    def on_review_callback(call: types.CallbackQuery) -> None:
        parts = call.data.split(":")
        if len(parts) < 3:
            bot.answer_callback_query(call.id)
            return
        bk_id = parts[1]
        action = parts[2]
        user_id = call.from_user.id
        state = ctx.get_state(user_id)
        target = next((b for b in state.get("bookings", []) if b.get("id") == bk_id), None)
        if not target:
            bot.answer_callback_query(call.id, "Запись не найдена")
            return

        if action == "r" and len(parts) >= 4:
            try:
                rating = int(parts[3])
            except ValueError:
                bot.answer_callback_query(call.id)
                return
            target["rating"] = rating
            ctx.save_state(user_id, state)
            bot.answer_callback_query(call.id, f"Спасибо за {rating}⭐!")

            # Persist review to long-term DB
            if db is not None:
                try:
                    db.bookings.update_rating_and_review(bk_id, rating=rating)
                except Exception as e:
                    log.error("Failed to save rating to long-term DB: %s", e)
            try:
                bot.edit_message_text(
                    (call.message.text or "") + f"\n\n✅ Ваша оценка: {rating}⭐\nСпасибо за обратную связь!",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
            notify_master(user_id, target, outcome="review_rating", transcript=None)
            return

        if action == "t":
            state["step"] = ctx.STEP_AWAITING_REVIEW
            state["review_for_booking"] = bk_id
            ctx.save_state(user_id, state)
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_text(
                    "✍️ Напишите ваш отзыв одним сообщением — я передам мастеру.\n"
                    "Если передумали — напишите «отмена».",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
            return

        if action == "b":
            master_name = target.get("master") or ""
            master_obj = kb.masters.get(master_name)
            state["step"] = ctx.STEP_ENTER_DATETIME
            state["master"] = master_name
            state["services"] = list(target.get("services") or [])
            state["services_raw"] = target.get("services_raw") or ""
            state["datetime_text"] = None
            state["datetime_iso"] = None
            state["phone"] = target.get("phone")
            ctx.save_state(user_id, state)
            bot.answer_callback_query(call.id, "Открываю выбор даты")
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                                message_id=call.message.message_id,
                                                reply_markup=None)
            except Exception:
                pass
            if master_obj:
                services_str = ", ".join(state["services"]) or "услуги"
                bot.send_message(
                    call.message.chat.id,
                    f"📅 Повторная запись к {master_name} ({services_str}). Выберите день:",
                    reply_markup=ctx.date_picker_kb(master_obj),
                )
            return

        if action == "s":
            bot.answer_callback_query(call.id, "Спасибо!")
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                                message_id=call.message.message_id,
                                                reply_markup=None)
            except Exception:
                pass
            return

    @bot.callback_query_handler(func=lambda c: c.data.startswith("reschedule_bk:"))
    def on_reschedule_ask(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        booking_id = call.data.split(":", 1)[1]
        state = ctx.get_state(user_id)
        target = next((b for b in (state.get("bookings") or []) if b.get("id") == booking_id), None)
        if not target:
            bot.answer_callback_query(call.id, "Запись не найдена", show_alert=True)
            return
        when = target.get("datetime") or "—"
        master = target.get("master") or "—"
        services = ", ".join(target.get("services") or []) or target.get("services_raw") or "—"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.row(
            types.InlineKeyboardButton("✅ Да, перенести", callback_data=f"resched_yes:{booking_id}"),
            types.InlineKeyboardButton("◀️ Назад", callback_data="cancel_back"),
        )
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                f"🔄 <b>Перенести эту запись?</b>\n\n"
                f"📅 {when}\n"
                f"👤 Мастер: {master}\n"
                f"{kb.brand.get('emoji', '✨')} Процедуры: {services}\n\n"
                "Старое время останется за вами, пока не возьмём новое.",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=markup, parse_mode="HTML",
            )
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda c: c.data.startswith("resched_yes:"))
    def on_reschedule_booking(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        booking_id = call.data.split(":", 1)[1]
        log.info("reschedule started: user=%s, old_booking=%s", user_id, booking_id)

        state = ctx.get_state(user_id)
        bookings = state.get("bookings", [])
        target = next((b for b in bookings if b.get("id") == booking_id), None)
        if not target:
            bot.answer_callback_query(call.id, "Запись не найдена", show_alert=True)
            return

        # P0: не освобождаем старый слот, пока новый не взят.
        # Calendar/reminders/sqlite status меняются только после успешного finalize.

        # Design bonus handling on reschedule:
        # The bonus used on the original booking stays consumed.
        # We do NOT restore it and we do NOT consume it again.
        # We only log for audit purposes.
        notes_str = " ".join(target.get("notes") or [])
        used_bonus = (
            kb.referral.get("referrer_bonus_master_note", "") in notes_str
            or "дизайн-бонус" in notes_str.lower()
        )
        if used_bonus:
            log.info(
                "reschedule: old booking %s had consumed a design bonus (stays consumed, as intended)",
                booking_id
            )

        state["step"] = ctx.STEP_ENTER_DATETIME
        state["master"] = target.get("master")
        state["services"] = list(target.get("services") or [])
        state["services_raw"] = target.get("services_raw") or ""
        state["datetime_text"] = None
        state["datetime_iso"] = None
        state["phone"] = target.get("phone")

        # Mark that the upcoming booking is a reschedule of an existing one.
        # This will prevent double-increment of total_visits and double consumption of design bonus.
        state["rescheduling_from_booking_id"] = booking_id

        ctx.save_state(user_id, state)

        bot.answer_callback_query(call.id, "Выберите новое время")

        services_str = ", ".join(state["services"]) or state.get("services_raw") or "услуги"
        try:
            bot.edit_message_text(
                f"🔄 <b>Перенос записи</b>\n"
                f"Мастер: {state['master']}\n"
                f"Процедуры: {services_str}\n"
                f"Старое время ({target.get('datetime')}) пока за вами.\n"
                "Если новое займут — оставим старое.\n\n"
                "Выберите новую дату:",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

        master_obj = kb.masters.get(state["master"] or "")
        if master_obj:
            bot.send_message(
                call.message.chat.id,
                f"📅 Выберите новый день (мастер {master_obj.name}):",
                reply_markup=ctx.date_picker_kb(master_obj),
            )

        log.info("reschedule: user=%s moved from booking %s to new booking flow", user_id, booking_id)

    @bot.callback_query_handler(func=lambda c: c.data == "empty:book")
    def on_empty_book(call: types.CallbackQuery) -> None:
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                            message_id=call.message.message_id,
                                            reply_markup=None)
        except Exception:
            pass
        start_booking(call.message.chat.id, call.from_user.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("rm:yes:"))
    def on_reminder_confirm(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        booking_id = call.data.split(":", 2)[2]
        target = _find_booking(user_id, booking_id)
        if not target:
            bot.answer_callback_query(call.id, "Запись не найдена", show_alert=True)
            return

        status = (target.get("status") or "confirmed")
        if status != "confirmed":
            bot.answer_callback_query(call.id, "Запись уже отменена или перенесена", show_alert=True)
            return

        if _booking_reminder_confirmed(target):
            bot.answer_callback_query(call.id, "Вы уже подтвердили этот визит")
            return

        notes = list(target.get("notes") or [])
        notes.append(REMINDER_CONFIRMED_NOTE)
        target["notes"] = notes
        if db is not None:
            try:
                db.bookings.update_booking(
                    booking_id, notes=json.dumps(notes, ensure_ascii=False),
                )
            except Exception as e:
                log.error("reminder_confirm: DB update failed %s: %s", booking_id, e)

        _ensure_booking_in_state(user_id, target)
        state = ctx.get_state(user_id)
        for b in state.get("bookings") or []:
            if b.get("id") == booking_id:
                b["notes"] = notes
                break
        ctx.save_state(user_id, state)
        target["client_name"] = _client_display_name(user_id, target)
        notify_master(user_id, target, outcome="reminder_confirmed")

        bot.answer_callback_query(call.id, "Визит подтверждён ✅")
        try:
            bot.edit_message_text(
                f"⏰ Напоминание о записи\n\n"
                f"Мастер: {target.get('master')}\n"
                f"Время: {target.get('datetime')}\n\n"
                f"✅ <b>Вы подтвердили визит.</b> Ждём вас!",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML",
            )
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=types.InlineKeyboardMarkup(),
            )
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda c: _is_rm_cancel_prompt(c.data))
    def on_reminder_cancel_ask(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        booking_id = call.data.split(":", 2)[2]
        target = _find_booking(user_id, booking_id)
        if not target:
            bot.answer_callback_query(call.id, "Запись не найдена", show_alert=True)
            return
        if (target.get("status") or "confirmed") != "confirmed":
            bot.answer_callback_query(call.id, "Запись уже отменена", show_alert=True)
            return

        _ensure_booking_in_state(user_id, target)

        note = ""
        try:
            dt = ctx.parse_iso_aware(target["datetime_iso"])
            hours_until = (dt - ctx.now_moscow()).total_seconds() / 3600
            if hours_until < 24:
                note = ("\n\n⚠️ <i>До записи осталось меньше 24 часов — "
                        "предоплата (если была) не возвращается.</i>")
        except Exception:
            pass

        when = target.get("datetime") or "—"
        master = target.get("master") or "—"
        services = ", ".join(target.get("services") or []) or target.get("services_raw") or "—"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.row(
            types.InlineKeyboardButton("✅ Да, отменить", callback_data=f"rm:cancel_yes:{booking_id}"),
            types.InlineKeyboardButton("◀️ Назад", callback_data=f"rm:cancel_back:{booking_id}"),
        )
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                f"Точно отменить запись?\n\n"
                f"📅 <b>{when}</b>\n"
                f"👤 Мастер: {master}\n"
                f"{kb.brand.get('emoji', '✨')} Процедуры: {services}{note}",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=markup, parse_mode="HTML",
            )
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda c: c.data.startswith("rm:cancel_back:"))
    def on_reminder_cancel_back(call: types.CallbackQuery) -> None:
        booking_id = call.data.split(":", 2)[2]
        user_id = call.from_user.id
        target = _find_booking(user_id, booking_id)
        bot.answer_callback_query(call.id)
        if not target:
            return
        services = ", ".join(target.get("services") or []) or target.get("services_raw") or "процедура"
        confirmed = _booking_reminder_confirmed(target)
        lines = [
            "⏰ Напоминание о записи",
            f"Мастер: {target.get('master')}",
            f"Процедуры: {services}",
            f"Время: {target.get('datetime')}",
            "",
        ]
        if confirmed:
            lines.append("✅ Вы уже подтвердили этот визит.")
        else:
            lines.append("Подтвердите визит или отмените запись кнопками ниже.")
        markup = None
        if not confirmed:
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.row(
                types.InlineKeyboardButton(
                    "✅ Подтверждаю", callback_data=f"rm:yes:{booking_id}",
                ),
                types.InlineKeyboardButton(
                    "❌ Отменить", callback_data=f"rm:cancel:{booking_id}",
                ),
            )
        try:
            bot.edit_message_text(
                "\n".join(lines),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup,
            )
            if confirmed:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=types.InlineKeyboardMarkup(),
                )
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda c: c.data.startswith("rm:cancel_yes:"))
    def on_reminder_cancel_yes(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        booking_id = call.data.split(":", 2)[2]
        target = _find_booking(user_id, booking_id)
        if not target:
            bot.answer_callback_query(call.id, "Запись не найдена или уже отменена", show_alert=True)
            return
        if (target.get("status") or "confirmed") != "confirmed":
            bot.answer_callback_query(call.id, "Запись уже отменена", show_alert=True)
            return

        _ensure_booking_in_state(user_id, target)
        state = ctx.get_state(user_id)
        late = _finalize_client_cancel(
            user_id, target, booking_id,
            notify_outcome="cancelled_from_reminder",
            decrement_reason="client_cancel_reminder",
            transcript=state.get("transcript"),
        )

        note = ""
        if late:
            note = ("\n\n⚠️ Отмена менее чем за 24 часа — по правилам предоплата "
                    "(если была) не возвращается.")
        bot.answer_callback_query(call.id, "Запись отменена")
        try:
            bot.edit_message_text(
                f"❌ Запись отменена: {target.get('datetime', '—')} — {target.get('master', '—')}.{note}",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
            )
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=types.InlineKeyboardMarkup(),
            )
        except Exception:
            bot.send_message(call.message.chat.id, f"❌ Запись отменена.{note}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_bk:"))
    def on_cancel_booking_ask(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        booking_id = call.data.split(":", 1)[1]
        state = ctx.get_state(user_id)
        target = next((b for b in (state.get("bookings") or []) if b.get("id") == booking_id
                       or b.get("pg_appointment_id") == booking_id), None)
        if not target:
            try:
                from database.pg import pg_enabled
                from database.appointments import list_upcoming_for_client
                if pg_enabled():
                    rows = list_upcoming_for_client(user_id, ctx.now_moscow())
                    row = next((r for r in rows if r.get("id") == booking_id), None)
                    if row:
                        iso = row.get("datetime_iso")
                        try:
                            dt = ctx.parse_iso_aware(iso)
                            when = dt.strftime("%d.%m %H:%M")
                        except Exception:
                            when = iso or "—"
                        target = {
                            "id": row["id"],
                            "pg_appointment_id": row["id"],
                            "master": row.get("master"),
                            "services": row.get("services") or [],
                            "datetime": when,
                            "datetime_iso": iso,
                            "google_event_id": row.get("google_event_id"),
                            "status": row.get("status"),
                        }
            except Exception as e:
                log.warning("cancel_bk pg lookup: %s", e)
        if not target:
            bot.answer_callback_query(call.id, "Запись не найдена", show_alert=True)
            return

        note = ""
        try:
            dt = ctx.parse_iso_aware(target["datetime_iso"])
            hours_until = (dt - ctx.now_moscow()).total_seconds() / 3600
            if hours_until < 24:
                note = ("\n\n⚠️ <i>До записи осталось меньше 24 часов — "
                        "предоплата (если была) не возвращается.</i>")
        except Exception:
            pass

        when = target.get("datetime") or "—"
        master = target.get("master") or "—"
        services = ", ".join(target.get("services") or []) or target.get("services_raw") or "—"

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.row(
            types.InlineKeyboardButton("✅ Да, отменить", callback_data=f"cancel_yes:{booking_id}"),
            types.InlineKeyboardButton("◀️ Назад", callback_data="cancel_back"),
        )
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                f"Точно отменить?\n\n"
                f"📅 <b>{when}</b>\n"
                f"👤 Мастер: {master}\n"
                f"{kb.brand.get('emoji', '✨')} Процедуры: {services}{note}",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                reply_markup=markup, parse_mode="HTML",
            )
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda c: c.data == "cancel_back")
    def on_cancel_back(call: types.CallbackQuery) -> None:
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        handle_my_bookings(call.message.chat.id, call.from_user.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_yes:"))
    def on_cancel_booking(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        booking_id = call.data.split(":", 1)[1]
        target = _find_booking(user_id, booking_id)
        if not target:
            target = {
                "id": booking_id,
                "pg_appointment_id": booking_id,
                "user_id": user_id,
            }
        st = (target.get("status") or "confirmed")
        if st in ("cancelled", "expired", "superseded"):
            _delete_google_for_booking(target)
            _drop_booking_from_state(user_id, target, booking_id)
            bot.answer_callback_query(call.id, "Уже отменена — убрал из списка")
            try:
                bot.edit_message_text(
                    "❌ Эта запись уже отменена.",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                )
            except Exception:
                pass
            return

        _ensure_booking_in_state(user_id, target)
        state = ctx.get_state(user_id)
        late = _finalize_client_cancel(
            user_id, target, booking_id,
            notify_outcome="cancelled_by_client",
            decrement_reason="client_cancel",
            transcript=state.get("transcript"),
        )

        note = ""
        if late:
            note = ("\n\n⚠️ Отмена менее чем за 24 часа — по правилам предоплата "
                    "(если была) не возвращается.")
        bot.answer_callback_query(call.id, "Запись отменена")
        try:
            bot.edit_message_text(
                f"❌ Запись отменена: {target.get('datetime', '—')} — {target.get('master', '—')}.{note}",
                chat_id=call.message.chat.id, message_id=call.message.message_id,
            )
        except Exception:
            bot.send_message(call.message.chat.id,
                             f"❌ Запись отменена.{note}")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("master:"))
    def on_master_chosen(call: types.CallbackQuery) -> None:
        master = call.data.split(":", 1)[1]
        if master not in _all_masters():
            bot.answer_callback_query(call.id, "Неизвестный мастер")
            return
        user_id = call.from_user.id
        state = ctx.get_state(user_id)
        state["master"] = master
        ctx.save_state(user_id, state)
        bot.answer_callback_query(call.id, f"Мастер: {master}")
        advance_booking(call.message.chat.id, user_id)

    return {
        "start_booking": start_booking,
        "continue_booking": continue_booking,
        "handle_my_bookings": handle_my_bookings,
        "notify_master": notify_master,
        "send_reminder_impl": send_reminder_impl,
        "send_review_prompt_impl": send_review_prompt_impl,
        "schedule_reminder": schedule_reminder,
    }
