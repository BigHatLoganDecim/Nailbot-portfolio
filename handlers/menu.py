"""
Inline-меню (cmd_menu + on_menu_callback).

Меню — это диспетчер: оно перенаправляет на handler'ы из других модулей.
Чтобы не тянуть в ctx 25 функций, делаем late-binding через main-модуль:
все cmd_*, handle_* функции достаются из main.<name> в момент вызова callback'а.

К моменту первого тапа клиента на меню — _register_extracted_handlers() уже
отработал и все имена в main указывают на реальные функции из подмодулей.
"""

from __future__ import annotations

import importlib
import logging
import threading

from telebot import types

log = logging.getLogger("nailbot.handlers.menu")


def register(ctx) -> dict:
    """
    ctx должен содержать:
      bot, kb, state_store, config,
      get_state, save_state, master_by_telegram_id.
    """
    bot = ctx.bot
    kb = ctx.kb
    config = ctx.config
    # state_store не используется в menu — get_state/save_state идут через ctx.

    # Late-binding: тянем функции из main по имени в момент вызова.
    # Так menu не зависит от того, что именно в ctx, и обновления стабов на
    # реальные функции в main'е автоматически подхватываются.
    _main_mod = None

    def _main():
        nonlocal _main_mod
        if _main_mod is None:
            _main_mod = importlib.import_module("__main__")
            # Если __main__ это не наш main.py (например, при тестах) — пробуем main
            if not hasattr(_main_mod, "bot"):
                _main_mod = importlib.import_module("main")
        return _main_mod

    def _build_main_menu_kb(user_id: int) -> types.InlineKeyboardMarkup:
        """Главное меню с разделами в зависимости от роли.

        Кнопки Оракул/Пригласить/Новости показываются только если соответствующая
        фича включена в kb.features. Для бизнеса где Оракул не нужен (например
        автосервис) — поставить в knowledge.md `## Фича-флаги` `oracle: no`
        и кнопка исчезнет.
        """
        is_master = bool(ctx.master_by_telegram_id(user_id))
        is_admin = user_id in config.admin_ids
        features = ctx.kb.features
        kb_ = types.InlineKeyboardMarkup(row_width=2)
        if is_admin:
            kb_.add(types.InlineKeyboardButton("🛡 Админка →", callback_data="menu:admin"))
        if is_master:
            kb_.add(types.InlineKeyboardButton("👤 Раздел мастера →", callback_data="menu:master"))
        kb_.row(
            types.InlineKeyboardButton("📅 Записаться", callback_data="menu:book"),
            types.InlineKeyboardButton("📋 Мои записи", callback_data="menu:my"),
        )
        kb_.row(
            types.InlineKeyboardButton("💰 Цены", callback_data="menu:prices"),
            types.InlineKeyboardButton("🎁 Сертификаты", callback_data="menu:certs"),
        )
        kb_.row(
            types.InlineKeyboardButton("👥 Мастера", callback_data="menu:masters"),
            types.InlineKeyboardButton("🗓 График", callback_data="menu:schedule"),
        )
        # Условный ряд: Оракул + Пригласить (фичи независимо могут быть выключены)
        oracle_btn = (types.InlineKeyboardButton("🪞 Оракул", callback_data="menu:oracle")
                      if features.get("oracle", True) else None)
        invite_btn = (types.InlineKeyboardButton("🤝 Пригласить", callback_data="menu:invite")
                      if features.get("referrals", True) else None)
        pair = [b for b in (oracle_btn, invite_btn) if b]
        if pair:
            kb_.row(*pair)
        kb_.row(
            types.InlineKeyboardButton("📜 Правила", callback_data="menu:rules"),
            types.InlineKeyboardButton("🔒 Данные", callback_data="menu:privacy"),
        )
        kb_.add(types.InlineKeyboardButton("❓ Помощь / что умеет бот", callback_data="menu:help"))
        return kb_

    def _build_master_menu_kb() -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup(row_width=2)
        kb_.row(
            types.InlineKeyboardButton("📅 Сегодня", callback_data="menu:today"),
            types.InlineKeyboardButton("📆 Завтра", callback_data="menu:tomorrow"),
        )
        kb_.row(
            types.InlineKeyboardButton("🗓 Неделя", callback_data="menu:week"),
            types.InlineKeyboardButton("🟢 Свободные окна", callback_data="menu:freeslots"),
        )
        kb_.row(
            types.InlineKeyboardButton("🗓 График месяца", callback_data="menu:schedule_edit"),
        )
        kb_.row(
            types.InlineKeyboardButton("🚫 Не работаю сегодня", callback_data="menu:off_today"),
            types.InlineKeyboardButton("🏖 Отпуск", callback_data="menu:vacation"),
        )
        kb_.row(
            types.InlineKeyboardButton("👥 Клиенты", callback_data="menu:clients"),
            types.InlineKeyboardButton("💰 Прайс", callback_data="menu:prices"),
        )
        kb_.row(
            types.InlineKeyboardButton("🔍 Найти клиента", callback_data="menu:client_hint"),
            types.InlineKeyboardButton("🤒 Болею", callback_data="menu:sick"),
        )
        kb_.add(types.InlineKeyboardButton("◀️ Главное меню", callback_data="menu:main"))
        return kb_

    def _build_admin_menu_kb() -> types.InlineKeyboardMarkup:
        kb_ = types.InlineKeyboardMarkup(row_width=2)
        kb_.row(
            types.InlineKeyboardButton("📊 Статистика", callback_data="menu:stats"),
            types.InlineKeyboardButton("📣 Рассылка", callback_data="menu:broadcast"),
        )
        kb_.row(
            types.InlineKeyboardButton("🪞 Оракул статы", callback_data="menu:psy_stats"),
            types.InlineKeyboardButton("💾 Бэкап сейчас", callback_data="menu:backup"),
        )
        kb_.row(
            types.InlineKeyboardButton("🔄 Перечитать knowledge", callback_data="menu:reload"),
            types.InlineKeyboardButton("🧹 Очист. напомин.", callback_data="menu:clear_rem"),
        )
        kb_.add(types.InlineKeyboardButton("◀️ Главное меню", callback_data="menu:main"))
        return kb_

    def _menu_header(user_id: int) -> str:
        """Шапка над меню с приветствием по имени если есть."""
        state = ctx.get_state(user_id)
        name = ((state.get("card") or {}).get("name") or "").split(" ")[0]
        if name:
            return f"🏠 <b>{name}</b>, выбери раздел:"
        return f"🏠 <b>{kb.brand.get('name', 'Salon')}</b> — выбери раздел:"

    @bot.message_handler(commands=["menu", "меню"])
    def cmd_menu(message: types.Message) -> None:
        """Открыть структурированное inline-меню."""
        try:
            st = ctx.get_state(message.from_user.id)
            if st.pop("pending_price_edit", None):
                ctx.save_state(message.from_user.id, st)
        except Exception:
            pass
        bot.send_message(message.chat.id, _menu_header(message.from_user.id),
                          reply_markup=_build_main_menu_kb(message.from_user.id),
                          parse_mode="HTML")

    @bot.callback_query_handler(func=lambda c: c.data.startswith("menu:"))
    def on_menu_callback(call: types.CallbackQuery) -> None:
        action = call.data.split(":", 1)[1]
        chat_id = call.message.chat.id
        user_id = call.from_user.id
        msg_id = call.message.message_id
        is_master = bool(ctx.master_by_telegram_id(user_id))
        is_admin = user_id in config.admin_ids
        M = _main()
        # Выход в любое меню сбрасывает незавершённую правку прайса (не сохраняем).
        try:
            clearer = getattr(M, "clear_price_edit", None)
            if callable(clearer):
                clearer(user_id)
        except Exception:
            pass

        def _ack():
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass

        def _close_menu():
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass

        def _edit_to(text: str, markup):
            try:
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                        reply_markup=markup, parse_mode="HTML")
            except Exception:
                bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

        # Навигация между разделами
        if action == "main":
            _ack()
            _edit_to(_menu_header(user_id), _build_main_menu_kb(user_id))
            return
        if action == "master":
            if not is_master:
                _ack()
                return
            _ack()
            _edit_to("👤 <b>Раздел мастера</b>", _build_master_menu_kb())
            return
        if action == "admin":
            if not is_admin:
                _ack()
                return
            _ack()
            _edit_to("🛡 <b>Админка</b>", _build_admin_menu_kb())
            return

        # === Клиентские действия ===
        if action == "book":
            _ack()
            _close_menu()
            return M.start_booking(chat_id, user_id)
        if action == "my":
            _ack()
            _close_menu()
            return M.handle_my_bookings(chat_id, user_id)
        if action == "prices":
            _ack()
            _close_menu()
            if is_master and hasattr(M, "cmd_edit_prices"):
                return M.cmd_edit_prices(call.message)
            return M.cmd_prices(call.message)
        if action == "certs":
            _ack()
            _close_menu()
            return bot.send_message(chat_id, kb.certificates or "—", parse_mode="HTML")
        if action == "masters":
            _ack()
            _close_menu()
            return M.cmd_masters(call.message)
        if action == "schedule":
            _ack()
            _close_menu()
            return M.cmd_schedule(call.message)
        if action == "rules":
            _ack()
            _close_menu()
            return M.cmd_rules(call.message)
        if action == "privacy":
            _ack()
            _close_menu()
            return M.cmd_privacy(call.message)
        if action == "invite":
            _ack()
            _close_menu()
            return M._send_invite(chat_id, user_id)
        if action == "oracle":
            _ack()
            _close_menu()
            return M.handle_psy_button(chat_id, user_id)
        if action == "help":
            _ack()
            _close_menu()
            return M._send_help(chat_id, user_id)
        # === Действия мастера ===
        if action == "today" and is_master:
            _ack()
            _close_menu()
            return M.cmd_today(call.message)
        if action == "tomorrow" and is_master:
            _ack()
            _close_menu()
            return M.cmd_tomorrow(call.message)
        if action == "week" and is_master:
            _ack()
            _close_menu()
            return M.cmd_week(call.message)
        if action == "salon_today" and is_master:
            _ack()
            _close_menu()
            return M.cmd_salon_today(call.message)
        if action == "salon_week" and is_master:
            _ack()
            _close_menu()
            return M.cmd_salon_week(call.message)
        if action == "freeslots" and is_master:
            _ack()
            _close_menu()
            return M.cmd_free_slots(call.message)
        if action == "schedule_edit" and is_master:
            _ack()
            _close_menu()
            return M.cmd_edit_schedule(call.message)
        if action == "off_today" and is_master:
            _ack()
            _close_menu()
            return M.cmd_off_today(call.message)
        if action == "vacation" and is_master:
            _ack()
            _close_menu()
            return M.send_vacation_picker(chat_id, user_id)
        if action == "clients" and is_master:
            _ack()
            _close_menu()
            return M.cmd_clients_list(call.message)
        if action == "client_hint" and is_master:
            _ack()
            return bot.send_message(chat_id,
                "Найти клиента: <code>/client имя</code> или <code>/client +7999...</code>",
                parse_mode="HTML")
        if action == "income" and is_master:
            _ack()
            _close_menu()
            return M.cmd_income(call.message)
        if action == "llm_usage" and is_master:
            _ack()
            _close_menu()
            return M.cmd_llm_usage(call.message)
        if action == "sick" and is_master:
            _ack()
            _close_menu()
            return M.cmd_sick(call.message)
        # === Админка ===
        if action == "stats" and is_admin:
            _ack()
            _close_menu()
            return M.cmd_stats(call.message)
        if action == "broadcast" and is_admin:
            _ack()
            _close_menu()
            return M.cmd_broadcast_start(call.message)
        if action == "psy_stats" and is_admin:
            _ack()
            _close_menu()
            return M.cmd_psy_stats(call.message)
        if action == "backup" and is_admin:
            _ack()
            _close_menu()
            bot.send_message(chat_id, "💾 Запускаю бэкап...")
            threading.Thread(
                target=lambda: M._backup_state_to_admin(force=True),
                daemon=True,
            ).start()
            return
        if action == "reload" and is_admin:
            _ack()
            _close_menu()
            return M.cmd_reload(call.message)
        if action == "clear_rem" and is_admin:
            _ack()
            _close_menu()
            return M.cmd_clear_reminders(call.message)

        # Неизвестная команда — молча отвечаем
        _ack()

    return {
        "cmd_menu": cmd_menu,
    }
