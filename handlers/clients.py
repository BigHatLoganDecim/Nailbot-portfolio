"""
Карточки клиентов — для мастеров.

Внешний API:
  - handle_card_edit_input(message, pending) — мастер ввёл новое значение
    для поля карточки. Вызывается из _on_text_inner когда в state
    обнаружен pending_card_edit.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from telebot import types

log = logging.getLogger("nailbot.handlers.clients")


def register(ctx) -> dict:
    """
    Регистрирует все card-хендлеры.

    ctx должен содержать: bot, state_store, now_moscow, parse_iso_aware,
    master_by_telegram_id, db (optional, long-term storage).
    """
    bot = ctx.bot
    state_store = ctx.state_store
    db = getattr(ctx, "db", None)  # Long-term database for clients and bookings history

    def _build_card_text(uid: int, s: dict) -> str:
        """Текст карточки клиента."""
        card = s.get("card") or {}
        bookings = s.get("bookings") or []
        name = card.get("name") or "(имя не указано)"
        notes = card.get("notes") or "—"
        bday = card.get("birthday") or "—"

        phone = next((b.get("phone") for b in reversed(bookings) if b.get("phone")), "—")

        visits = len(bookings)
        ratings = [b.get("rating") for b in bookings if b.get("rating")]
        avg_rating = f"{sum(ratings)/len(ratings):.1f}/5 ({len(ratings)} оценок)" if ratings else "нет оценок"

        all_services: list[str] = []
        for b in bookings:
            all_services.extend(b.get("services") or [])
        fav = Counter(all_services).most_common(1)
        fav_str = f"{fav[0][0]} ({fav[0][1]}×)" if fav else "—"

        # Последний и предстоящий визит
        now = ctx.now_moscow()
        past = []
        future = []
        for b in bookings:
            iso = b.get("datetime_iso")
            if not iso:
                continue
            try:
                dt = ctx.parse_iso_aware(iso)
            except Exception:
                continue
            (past if dt <= now else future).append((dt, b))
        past.sort(key=lambda x: x[0], reverse=True)
        future.sort(key=lambda x: x[0])

        last_str = "—"
        if past:
            dt, b = past[0]
            last_str = f"{dt.strftime('%d.%m.%Y')} — {b.get('master', '?')}"
        next_str = "—"
        if future:
            dt, b = future[0]
            next_str = f"{dt.strftime('%d.%m.%Y %H:%M')} — {b.get('master', '?')}"

        # История — последние 5
        history_lines = []
        for dt, b in past[:5]:
            srv = ", ".join(b.get("services") or []) or b.get("services_raw") or "—"
            if len(srv) > 60:
                srv = srv[:57] + "…"
            rating = b.get("rating")
            rating_str = f" {'⭐' * int(rating)}" if rating else ""
            history_lines.append(f"• {dt.strftime('%d.%m')} — {b.get('master', '?')} — {srv}{rating_str}")
        history_str = "\n".join(history_lines) if history_lines else "—"

        tags = card.get("tags") or []
        tags_str = " ".join(f"#{t}" for t in tags) if tags else "—"

        return (
            f"🪪 <b>Карточка клиента</b>\n\n"
            f"<b>Имя:</b> {name}\n"
            f"<b>Телефон:</b> {phone}\n"
            f"<b>День рождения:</b> {bday}\n"
            f"<b>Метки:</b> {tags_str}\n"
            f"<b>Telegram ID:</b> <code>{uid}</code>\n\n"
            f"📊 <b>Статистика</b>\n"
            f"Всего записей: {visits}\n"
            f"Последний визит: {last_str}\n"
            f"Следующий визит: {next_str}\n"
            f"Средняя оценка: {avg_rating}\n"
            f"Любимая услуга: {fav_str}\n\n"
            f"📝 <b>Заметки</b>\n{notes}\n\n"
            f"📅 <b>История (последние 5)</b>\n{history_str}"
        )

    def _card_keyboard(uid: int) -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup(row_width=2)
        kb_.row(
            types.InlineKeyboardButton("✏️ Имя", callback_data=f"ce:{uid}:name"),
            types.InlineKeyboardButton("🎂 ДР", callback_data=f"ce:{uid}:birthday"),
        )
        kb_.row(
            types.InlineKeyboardButton("📝 Заметки", callback_data=f"ce:{uid}:notes"),
            types.InlineKeyboardButton("🏷 Метки", callback_data=f"ce:{uid}:tags"),
        )
        kb_.add(types.InlineKeyboardButton("📜 Вся история", callback_data=f"ch:{uid}"))
        return kb_

    def handle_card_edit_input(message: types.Message, pending: dict) -> None:
        """Мастер ввёл новое значение для поля карточки. pending = {user_id, field}."""
        user_id = message.chat.id  # chat.id == user.id в приватном боте
        text = (message.text or "").strip()
        master_state = state_store.get(user_id) or {}
        field = pending.get("field")
        client_uid_raw = pending.get("user_id")

        if text.lower() in ("отмена", "не надо", "забей"):
            master_state["pending_card_edit"] = None
            state_store.set(user_id, master_state)
            bot.send_message(message.chat.id, "Редактирование отменено.")
            return

        try:
            cuid = int(client_uid_raw)
        except (TypeError, ValueError):
            master_state["pending_card_edit"] = None
            state_store.set(user_id, master_state)
            bot.send_message(message.chat.id, "Ошибка: неверный ID клиента.")
            return

        client_state = state_store.get(cuid) or {}
        card = client_state.get("card") or {"name": "", "notes": "", "birthday": "", "tags": []}
        value = text[:500]
        label_map = {"name": "Имя", "birthday": "ДР", "notes": "Заметки", "tags": "Метки"}
        if field == "tags":
            # Метки — через пробел или запятую, чистим от #
            card["tags"] = [t.strip().lstrip("#") for t in re.split(r"[,\s]+", value) if t.strip()]
        elif field in ("name", "birthday", "notes"):
            card[field] = value
        else:
            bot.send_message(message.chat.id, "Неизвестное поле.")
            return
        client_state["card"] = card
        state_store.set(cuid, client_state)
        state_store.track_user(cuid)  # для надёжности заносим в индекс

        # === New: Also persist card changes to long-term database ===
        if db is not None:
            try:
                db.clients.update_card(
                    cuid,
                    name=card.get("name"),
                    notes=card.get("notes"),
                    birthday=card.get("birthday"),
                    tags=card.get("tags"),
                )
            except Exception as e:
                log.error("Failed to persist card edit to long-term DB: %s", e)

        master_state["pending_card_edit"] = None
        state_store.set(user_id, master_state)

        bot.send_message(message.chat.id, f"✅ Сохранено: <b>{label_map.get(field, field)}</b>",
                          parse_mode="HTML")
        # Сразу показываем обновлённую карточку
        bot.send_message(message.chat.id, _build_card_text(cuid, client_state),
                          reply_markup=_card_keyboard(cuid), parse_mode="HTML")

    @bot.message_handler(commands=["clients", "клиенты"])
    def cmd_clients_list(message: types.Message) -> None:
        """Список клиентов салона (последние активные сверху). Только для мастеров."""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        user_ids = state_store.all_users()
        cards = []
        for uid in user_ids:
            s = state_store.get(uid) or {}
            bookings = s.get("bookings") or []
            if not bookings:
                continue
            card = s.get("card") or {}
            name = card.get("name") or "(без имени)"
            phone = next((b.get("phone") for b in reversed(bookings) if b.get("phone")), "—")
            last_iso = max((b.get("datetime_iso") or "" for b in bookings), default="")
            cards.append({"uid": uid, "name": name, "phone": phone,
                           "visits": len(bookings), "last_iso": last_iso})
        cards.sort(key=lambda c: c["last_iso"], reverse=True)
        if not cards:
            bot.send_message(message.chat.id, "Клиентов с записями пока нет.")
            return
        top = cards[:20]
        lines = [f"📒 <b>Клиенты салона</b> (всего: {len(cards)})\n"]
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for c in top:
            keyboard.add(types.InlineKeyboardButton(
                f"🪪 {c['name']} — {c['phone']} ({c['visits']})",
                callback_data=f"card:{c['uid']}",
            ))
        if len(cards) > 20:
            lines.append(f"<i>Показаны последние 20 из {len(cards)}. "
                          "Найти конкретного: /client &lt;имя или телефон&gt;</i>")
        bot.send_message(message.chat.id, "\n".join(lines),
                         reply_markup=keyboard, parse_mode="HTML")

    @bot.message_handler(commands=["client"])
    def cmd_client_search(message: types.Message) -> None:
        """Поиск клиента по имени или телефону. Только для мастеров."""
        master = ctx.master_by_telegram_id(message.chat.id)
        if not master:
            return
        parts = (message.text or "").split(maxsplit=1)
        query = parts[1].strip().lower() if len(parts) > 1 else ""
        if not query:
            bot.send_message(message.chat.id, "Используйте: <code>/client имя</code> или <code>/client +7999...</code>",
                              parse_mode="HTML")
            return
        digits_query = re.sub(r"\D", "", query)
        matches = []
        for uid in state_store.all_users():
            s = state_store.get(uid) or {}
            card = s.get("card") or {}
            bookings = s.get("bookings") or []
            name = (card.get("name") or "").lower()
            found = False
            if query in name:
                found = True
            elif digits_query:
                for b in bookings:
                    p = re.sub(r"\D", "", b.get("phone") or "")
                    if p and digits_query in p:
                        found = True
                        break
            if found:
                matches.append((uid, card, bookings))
        if not matches:
            bot.send_message(message.chat.id, f"Не нашёл клиента по запросу «{query}».")
            return
        lines = [f"🔍 Найдено: <b>{len(matches)}</b>\n"]
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        for uid, card, bks in matches[:10]:
            name = card.get("name") or "(без имени)"
            phone = next((b.get("phone") for b in reversed(bks) if b.get("phone")), "—")
            keyboard.add(types.InlineKeyboardButton(
                f"🪪 {name} — {phone} ({len(bks)})",
                callback_data=f"card:{uid}",
            ))
        bot.send_message(message.chat.id, "\n".join(lines),
                          reply_markup=keyboard, parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("card:"))
    def on_card_open(call: types.CallbackQuery) -> None:
        if not ctx.master_by_telegram_id(call.from_user.id):
            bot.answer_callback_query(call.id, "Только для мастеров")
            return
        try:
            uid = int(call.data.split(":")[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id)
            return
        s = state_store.get(uid) or {}
        bot.send_message(call.message.chat.id, _build_card_text(uid, s),
                          reply_markup=_card_keyboard(uid), parse_mode="HTML")
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ce:"))
    def on_card_edit(call: types.CallbackQuery) -> None:
        if not ctx.master_by_telegram_id(call.from_user.id):
            bot.answer_callback_query(call.id, "Только для мастеров")
            return
        parts = call.data.split(":")
        if len(parts) < 3:
            bot.answer_callback_query(call.id)
            return
        client_uid = parts[1]
        field = parts[2]
        master_state = state_store.get(call.from_user.id) or {}
        master_state["pending_card_edit"] = {"user_id": client_uid, "field": field}
        state_store.set(call.from_user.id, master_state)
        prompt_map = {
            "name": "Введите имя клиента:",
            "birthday": "Введите день рождения (например, <code>15.03</code> или <code>15.03.1990</code>):",
            "notes": "Введите заметку о клиенте (можно длинный текст):",
            "tags": "Введите метки через пробел или запятую (например, <code>VIP постоянный</code>):",
        }
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            prompt_map.get(field, "Введите значение:") + "\n\n<i>Отмена — напишите «отмена».</i>",
            parse_mode="HTML",
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("ch:"))
    def on_card_history(call: types.CallbackQuery) -> None:
        if not ctx.master_by_telegram_id(call.from_user.id):
            bot.answer_callback_query(call.id, "Только для мастеров")
            return
        try:
            uid = int(call.data.split(":")[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id)
            return
        s = state_store.get(uid) or {}
        bookings = s.get("bookings") or []
        if not bookings:
            bot.send_message(call.message.chat.id, "История пуста.")
            bot.answer_callback_query(call.id)
            return
        sorted_b = sorted(bookings, key=lambda b: b.get("datetime_iso") or "", reverse=True)
        lines = ["📜 <b>Полная история визитов</b>\n"]
        for b in sorted_b:
            try:
                dt = ctx.parse_iso_aware(b["datetime_iso"])
                d = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                d = "?"
            srv = ", ".join(b.get("services") or []) or b.get("services_raw") or "—"
            rating = b.get("rating")
            rating_str = f" {'⭐' * int(rating)}" if rating else ""
            review = b.get("review_text")
            line = f"<b>{d}</b> — {b.get('master', '?')} — {srv}{rating_str}"
            if review:
                line += f"\n   <i>«{review[:200]}»</i>"
            lines.append(line)
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (обрезано)"
        bot.send_message(call.message.chat.id, text, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    return {
        "handle_card_edit_input": handle_card_edit_input,
        "cmd_clients_list": cmd_clients_list,
    }
