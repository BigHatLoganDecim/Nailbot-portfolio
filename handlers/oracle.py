"""
Хендлеры фичи «Оракул» (psychologist).

Изолированы от main.py: всё, что нужно из общего бота — приходит через
ctx (SimpleNamespace). Регистрация хендлеров происходит в register().

Внешний API (что main.py использует):
  - handle_psy_button(chat_id, user_id) — клиент нажал «Оракул» в меню
  - continue_psy_dialog(message, state) — клиент в активной сессии
"""

from __future__ import annotations

import logging
from typing import Any

from telebot import types

import psychologist

log = logging.getLogger("nailbot.oracle")


def register(ctx) -> dict:
    """
    Регистрирует все хендлеры Оракула на ctx.bot и возвращает dict
    с публичными функциями, которые main.py зовёт извне.

    ctx должен содержать: bot, llm, state_store, config, now_moscow,
    get_state, save_state, main_menu_kb, STEP_PSY_DIALOG, STEP_IDLE,
    PSY_MAX_SESSIONS_PER_DAY, PSY_MAX_TURNS.
    """
    bot = ctx.bot
    llm = ctx.llm
    state_store = ctx.state_store
    config = ctx.config
    # Лимиты Оракула — динамика из kb.settings (для /reload без рестарта)
    def _max_sessions_per_day():
        return ctx.kb.settings["psy_max_sessions_per_day"]
    def _max_turns():
        return ctx.kb.settings["psy_max_turns"]

    def _psy_today_count(state: dict[str, Any]) -> int:
        """Сколько сессий клиент сделал сегодня (с авто-сбросом счётчика на новый день)."""
        today = ctx.now_moscow().date().isoformat()
        if state.get("psy_today_date") != today:
            return 0
        return int(state.get("psy_today_count") or 0)

    def _psy_increment_today(state: dict[str, Any]) -> None:
        today = ctx.now_moscow().date().isoformat()
        if state.get("psy_today_date") != today:
            state["psy_today_date"] = today
            state["psy_today_count"] = 0
        state["psy_today_count"] = int(state.get("psy_today_count") or 0) + 1
        state["psy_total_used"] = int(state.get("psy_total_used") or 0) + 1
        # Глобальный счётчик в Redis — для общей статистики
        if state_store.redis:
            try:
                state_store.redis.incr("nailbot:psy:total")
                state_store.redis.incr(f"nailbot:psy:user:{state.get('_uid', '')}")
            except Exception as e:
                log.debug("psy stats incr: %s", e)

    def _psy_consent_kb() -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup(row_width=1)
        kb_.add(types.InlineKeyboardButton("✅ Я понимаю, продолжаем", callback_data="psy:consent"))
        kb_.add(types.InlineKeyboardButton("❌ Нет, спасибо", callback_data="psy:decline"))
        return kb_

    def _psy_personality_kb() -> types.InlineKeyboardMarkup:
        """Три кнопки выбора личности."""
        kb_ = types.InlineKeyboardMarkup(row_width=3)
        kb_.row(
            types.InlineKeyboardButton("🧑 Психолог", callback_data="psy:pick:psychologist"),
            types.InlineKeyboardButton("🌙 Экстрасенс", callback_data="psy:pick:extrasens"),
            types.InlineKeyboardButton("🔮 Мистик", callback_data="psy:pick:mystic"),
        )
        return kb_

    def _psy_dialog_kb() -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup()
        kb_.add(types.InlineKeyboardButton("🚪 Завершить сессию", callback_data="psy:end"))
        return kb_

    def handle_psy_button(chat_id: int, user_id: int) -> None:
        """Клиент нажал «Оракул» в меню."""
        # Kill-switch: фича может быть выключена в knowledge.md `## Фича-флаги`.
        # Для бизнесов где Оракул не нужен (автосервис, барбершоп).
        if not ctx.kb.features.get("oracle", True):
            bot.send_message(chat_id, "Эта функция в нашем салоне не активирована.")
            return
        state = ctx.get_state(user_id)
        if not state.get("psy_consent"):
            bot.send_message(chat_id, psychologist.WELCOME_TEXT,
                              reply_markup=_psy_consent_kb(), parse_mode="HTML")
            return
        # Согласие есть — проверяем лимит и сразу к выбору личности
        if _psy_today_count(state) >= _max_sessions_per_day():
            bot.send_message(
                chat_id,
                psychologist.DAILY_LIMIT_TEXT.format(
                    used=_psy_today_count(state), limit=_max_sessions_per_day()),
            )
            return
        bot.send_message(chat_id, psychologist.PICK_PERSONALITY_TEXT,
                          reply_markup=_psy_personality_kb(), parse_mode="HTML")

    def _start_psy_session(chat_id: int, user_id: int, state: dict[str, Any],
                            personality_key: str) -> None:
        """Открыть сессию с выбранной личностью."""
        personality = psychologist.PERSONALITIES.get(personality_key)
        if not personality:
            bot.send_message(chat_id, "Не нашёл такую личность, попробуй снова.")
            return

        # Тянем дату рождения из карточки клиента (если есть)
        card = state.get("card") or {}
        birthday = (card.get("birthday") or "").strip()
        system_prompt = psychologist.build_system_prompt(personality_key, birthday=birthday)

        state["step"] = ctx.STEP_PSY_DIALOG
        state["psy_session"] = {
            "personality": personality_key,
            "messages": [{"role": "system", "content": system_prompt}],
            "turn_count": 0,
            "started_at": ctx.now_moscow().isoformat(),
        }
        state["_uid"] = user_id
        ctx.save_state(user_id, state)

        # Анонс — какую личность открыли (без раскрытия её характера / промпта)
        bot.send_message(
            chat_id,
            f"{personality['icon']} <b>{personality['name']}</b> к твоим услугам.\n\n"
            + psychologist.INSTRUCTION_TEXT,
            parse_mode="HTML", reply_markup=_psy_dialog_kb(),
        )

    def _end_psy_session(chat_id: int, user_id: int, reason: str = "user") -> None:
        """Закрыть сессию, вернуть в основное меню."""
        state = ctx.get_state(user_id)
        state["step"] = ctx.STEP_IDLE
        state["psy_session"] = None
        ctx.save_state(user_id, state)
        msg_map = {
            "user": "Сессия завершена. Возвращайся, когда будет новый вопрос.",
            "limit": "Лимит реплик на одну сессию достигнут. Спасибо за разговор.",
        }
        bot.send_message(chat_id, msg_map.get(reason, "Сессия завершена."),
                          reply_markup=ctx.main_menu_kb())

    def continue_psy_dialog(message: types.Message, state: dict[str, Any]) -> None:
        """Клиент в активной сессии «Помощник» — обрабатываем его реплику через LLM."""
        user_id = message.from_user.id
        chat_id = message.chat.id
        text = (message.text or "").strip()

        session = state.get("psy_session") or {}
        if not session:
            # На всякий случай — если состояние битое
            _end_psy_session(chat_id, user_id)
            return

        # Кризисный детектор — приоритетно, без вызова LLM
        if psychologist.contains_crisis(text):
            bot.send_message(chat_id, psychologist.CRISIS_RESPONSE, parse_mode="HTML")
            _end_psy_session(chat_id, user_id, reason="user")
            return

        # Слишком длинный диалог
        turn_count = int(session.get("turn_count") or 0)
        if turn_count >= _max_turns():
            _end_psy_session(chat_id, user_id, reason="limit")
            return

        # Защита от race condition: если LLM уже думает над предыдущей репликой,
        # вторую игнорируем — иначе она прочитает старый session без первой реплики.
        if session.get("_busy"):
            bot.send_message(chat_id, "⏳ Подожди, я ещё формулирую ответ на предыдущее...")
            return

        # Если это первая реплика клиента в сессии — увеличиваем счётчик дня
        if turn_count == 0:
            _psy_increment_today(state)

        messages = list(session.get("messages") or [])
        messages.append({"role": "user", "content": text[:2000]})

        if not llm.enabled:
            bot.send_message(chat_id, "AI временно недоступен. Попробуй позже.")
            return

        # Сохраняем user-message в state ДО вызова LLM — на случай повторного сообщения
        session["messages"] = messages
        session["_busy"] = True
        state["psy_session"] = session
        ctx.save_state(user_id, state)

        # Подсказка пользователю что мы думаем
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass

        try:
            reply = llm.psy_chat(messages)
        finally:
            # Снимаем busy-флаг в любом случае
            fresh_state = state_store.get(user_id) or {}
            fresh_session = fresh_state.get("psy_session") or {}
            if fresh_session:
                fresh_session["_busy"] = False
                fresh_state["psy_session"] = fresh_session
                state_store.set(user_id, fresh_state)

        if not reply:
            bot.send_message(chat_id, "Не получилось ответить — попробуй ещё раз через минуту.")
            return

        # Перечитываем state — он мог измениться (busy=False выше)
        state = state_store.get(user_id) or {}
        session = state.get("psy_session") or {}
        messages = list(session.get("messages") or [])
        messages.append({"role": "assistant", "content": reply})
        session["messages"] = messages
        session["turn_count"] = turn_count + 1
        state["psy_session"] = session
        ctx.save_state(user_id, state)

        bot.send_message(chat_id, reply, reply_markup=_psy_dialog_kb())

    @bot.callback_query_handler(func=lambda c: c.data.startswith("psy:"))
    def on_psy_callback(call: types.CallbackQuery) -> None:
        user_id = call.from_user.id
        parts = call.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        state = ctx.get_state(user_id)

        if action == "consent":
            state["psy_consent"] = True
            ctx.save_state(user_id, state)
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                                message_id=call.message.message_id,
                                                reply_markup=None)
            except Exception:
                pass
            if _psy_today_count(state) >= _max_sessions_per_day():
                bot.send_message(call.message.chat.id,
                                  psychologist.DAILY_LIMIT_TEXT.format(
                                      used=_psy_today_count(state),
                                      limit=_max_sessions_per_day()))
                return
            # После согласия — выбор личности
            bot.send_message(call.message.chat.id, psychologist.PICK_PERSONALITY_TEXT,
                              reply_markup=_psy_personality_kb(), parse_mode="HTML")
            return

        if action == "pick":
            personality_key = parts[2] if len(parts) > 2 else ""
            if personality_key not in psychologist.PERSONALITIES:
                bot.answer_callback_query(call.id, "Неизвестная личность")
                return
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                                message_id=call.message.message_id,
                                                reply_markup=None)
            except Exception:
                pass
            _start_psy_session(call.message.chat.id, user_id, state, personality_key)
            return

        if action == "decline":
            bot.answer_callback_query(call.id)
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id,
                                                message_id=call.message.message_id,
                                                reply_markup=None)
            except Exception:
                pass
            bot.send_message(call.message.chat.id, "Хорошо. Возвращайся, если передумаешь.",
                              reply_markup=ctx.main_menu_kb())
            return

        if action == "end":
            bot.answer_callback_query(call.id, "Сессия завершена")
            _end_psy_session(call.message.chat.id, user_id, reason="user")
            return

    @bot.message_handler(commands=["delete_psy_data"])
    def cmd_delete_psy_data(message: types.Message) -> None:
        """Право клиента удалить свои данные по ФЗ-152."""
        user_id = message.from_user.id
        state = ctx.get_state(user_id)
        state["psy_consent"] = False
        state["psy_session"] = None
        state["psy_total_used"] = 0
        state["psy_today_date"] = None
        state["psy_today_count"] = 0
        ctx.save_state(user_id, state)
        # Сбрасываем глобальный счётчик пользователя в Redis
        # Также декрементим nailbot:psy:total — иначе общий счётчик уезжает вверх
        if state_store.redis:
            try:
                user_key = f"nailbot:psy:user:{user_id}"
                user_count = int(state_store.redis.get(user_key) or 0)
                pipe = state_store.redis.pipeline()
                pipe.delete(user_key)
                if user_count > 0:
                    pipe.decrby("nailbot:psy:total", user_count)
                pipe.execute()
            except Exception:
                pass
        bot.send_message(message.chat.id,
                          "✅ Ваши данные по фиче «Помощник» удалены. "
                          "При следующем использовании потребуется новое согласие.")

    @bot.message_handler(commands=["psy_stats"])
    def cmd_psy_stats(message: types.Message) -> None:
        """Статистика по фиче «Помощник». Только для админа."""
        if message.chat.id not in config.admin_ids:
            return
        if not state_store.redis:
            bot.send_message(message.chat.id, "Redis не подключён — статистики нет.")
            return
        try:
            total = int(state_store.redis.get("nailbot:psy:total") or 0)
        except Exception:
            total = 0

        # Уникальные пользователи и их счётчики
        user_counts: list[tuple[int, int]] = []
        try:
            for key in state_store.redis.scan_iter("nailbot:psy:user:*"):
                try:
                    if isinstance(key, bytes):
                        key = key.decode()
                    uid = int(key.split(":")[-1])
                    cnt = int(state_store.redis.get(key) or 0)
                    user_counts.append((uid, cnt))
                except Exception:
                    pass
        except Exception as e:
            log.warning("psy_stats scan: %s", e)

        unique = len(user_counts)
        user_counts.sort(key=lambda x: -x[1])
        top_lines = []
        for uid, cnt in user_counts[:10]:
            s = state_store.get(uid) or {}
            name = (s.get("card") or {}).get("name") or "(без имени)"
            top_lines.append(f"• {name} (id={uid}): {cnt} сессий")

        text = (
            f"🪞 <b>Статистика «Помощник»</b>\n\n"
            f"Всего сессий: <b>{total}</b>\n"
            f"Уникальных пользователей: <b>{unique}</b>\n\n"
            f"<b>Топ-10 по количеству:</b>\n"
            + ("\n".join(top_lines) if top_lines else "—")
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML")

    return {
        "handle_psy_button": handle_psy_button,
        "continue_psy_dialog": continue_psy_dialog,
        "cmd_psy_stats": cmd_psy_stats,
    }
