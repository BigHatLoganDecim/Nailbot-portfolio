# NAVIGATION.md — карта кода NailBot

Цель: за 10 секунд найти где живёт фича / куда добавить новую, без grep
по всему проекту. Экономит токены при работе.

**Обновляется вручную после каждой структурной правки.**

---

## 🗂 Дерево файлов

```
nailbot/
├── main.py                     ← entry: Flask webhook + telebot + helpers
├── utils.py                    ← Knowledge, StateStore, Config, лимитеры
├── parsing.py                  ← intent/master/services/date/phone парсер
├── llm.py                      ← OpenAI-совместимый клиент
├── psychologist.py             ← промпты «Оракула» (выключается флагом)
│
├── handlers/
│   ├── booking.py              ← запись, мои записи, напоминания
│   ├── master.py               ← день мастера, прайс, график месяца
│   ├── vacation_sick.py        ← отпуск / больничный + массовая отмена
│   ├── oracle.py               ← 3 личности AI
│   ├── menu.py                 ← /menu, late-binding
│   ├── clients.py              ← карточки клиентов
│   ├── admin.py                ← /broadcast /backup /sync
│   └── stats.py                ← /stats /llm_usage
│
├── services/
│   ├── slots.py                ← свободные окна, пикеры
│   ├── calendar.py             ← Google Calendar (проекция, не SoT)
│   └── catalog.py              ← прайс: Postgres overlay
│
├── database/                   ← Postgres SoT + SQLite legacy
├── alembic/versions/           ← 0001 слоты, 0002 outbox
├── knowledge.md                ← демо-контент Luna Studio
├── NAVIGATION.md               ← этот файл
└── CLONE_GUIDE.md              ← клон под другой бизнес
```

---

## 🎯 Архитектурный паттерн

Все handler-модули используют единый паттерн **register(ctx) → API**:

```python
# handlers/<feature>.py
def register(ctx) -> dict:
    bot = ctx.bot
    kb = ctx.kb
    # ... handlers с @bot.message_handler / @bot.callback_query_handler ...
    return {"public_fn1": ..., "public_fn2": ...}
```

`main._register_extracted_handlers()` создаёт `ctx` (41 поле) и зовёт
все `register(ctx)`. Возвращаемые public функции биндятся в main globals.

**Порядок регистрации важен:**
1. oracle → admin → clients → master → vacation_sick (независимые)
2. **slots.create(ctx)** — нужен `get_vacation_dates` из vacation_sick
3. stats + установка `llm.usage_callback`
4. **booking** — нужны slots + vacation API
5. **menu последним** — диспатчит во всё остальное через late-binding к `__main__`

После register → `_verify_register_complete()` проверяет что все 35
стабов подменились (предотвращает регрессии типа cmd_menu).

---

## 🌳 Дерево фич → файлы

### 📅 Запись (booking flow) — **handlers/booking.py**
- `start_booking`, `advance_booking`, `continue_booking`, `finalize_booking`
- Все callback'и: `bk:` `svc:` `dt:` `cf:` `rv:` `master:` `phc:` `empty:book`
  `reschedule_bk:` `cancel_bk:` `cancel_back` `cancel_yes:`
- `on_contact` — Telegram contact share
- `handle_my_bookings` — список предстоящих + кнопки перенести/отменить
- `notify_master` — уведомления мастеру (booked/cancelled/review)
- `schedule_reminder` + `sync_with_google_calendar` + `_schedule_review`
- Корзина услуг (`_cart_text`, `_services_main_kb`, `_services_cat_kb`)
- Phone helpers (`_get_known_phone`, `_remember_phone`)
- **Шимы** `_send_reminder` / `_send_review_prompt` остались в main.py для
  APScheduler-compat (Redis JobStore хранит `__main__:_send_reminder`).

### 🪞 Оракул (psychologist) — **handlers/oracle.py**
- 3 личности AI (Психолог Сергей / Экстрасенс Веда / Мистик Алекс)
- `handle_psy_button`, `continue_psy_dialog`, `_start_psy_session`,
  `_end_psy_session`, `on_psy_callback`, `cmd_delete_psy_data`, `cmd_psy_stats`
- Лимиты из `kb.settings.psy_max_sessions_per_day / psy_max_turns`
- Промпты и кризисный детектор — в **psychologist.py**

### 📰 Новости СПб — **handlers/news.py**
- `_prepare_news_pack` (17:00 — собираем + LLM rewrite)
- `_send_news_pack_for_approval` (отдаём админу на одобрение)
- `_format_news_broadcast` (футер с brand)
- `on_news_callback` (sub/nosub/edit/del/send/skip)
- `/news_subscribe`, `/news_unsubscribe`, `/news_now`, `/news_off`, `/news_on`
- RSS логика — в **news.py**

### 🛡 Админка — **handlers/admin.py**
- `/broadcast` + `on_broadcast_callback` (с lock от двойного клика)
- `/backup`, `_backup_state_to_admin` (для scheduler 3x/день)
- `/clear_reminders`, `/clear_my_bookings`, `/sentry_test`

### 📊 Статистика — **handlers/stats.py**
- `/stats` (ASCII-дашборд)
- `/llm_usage` + `track_groq_call` (колбэк к llm.usage_callback)
- Лимиты из `kb.settings.llm_daily_*_limit` с алертами 50/80/90%

### 🪪 Карточки клиентов — **handlers/clients.py**
- `_build_card_text`, `_card_keyboard`
- `/clients`, `/client <запрос>`
- `on_card_open`, `on_card_edit`, `on_card_history`
- `handle_card_edit_input` (зовётся из main._on_text_inner)

### 👤 Команды мастера — **handlers/master.py**
- `/today`, `/tomorrow`, `/week`, `/income`, `/block`
- `on_master_cancel` (mc:), `on_master_unblock` (mu:)
- `_send_master_events` — общий рендер расписания

### 🤒 Болезнь/отпуск — **handlers/vacation_sick.py**
- `/sick` + `on_sick_callback` (массовая отмена)
- `/vacation`, `/vacation_clear` + `on_vacation_callback`
- `get_vacation_dates` (нужен booking flow для исключения отпускных дней)

### 📋 Меню — **handlers/menu.py**
- `/menu` + `on_menu_callback`
- 3 keyboard builders: main / master / admin
- **Late-binding** к main через `importlib.import_module('__main__')` —
  диспатчит на 25 функций через `M.cmd_X(...)`

### 📅 Слоты и время — **services/slots.py**
- Pure-logic, factory `create(ctx) → dict`
- `parse_work_dates`, `work_hours_for`, `parse_work_hours`
- `resolve_vague_time` («вечер» → 18:00)
- `total_duration_minutes`, `find_free_slots`, `format_slot`
- `available_dates`, `date_picker_kb`, `time_picker_kb`
- `resolve_booking_time` — главная функция валидации
- **Все константы (FIXED_SLOTS_HOURS и т.п.) читаются ЛЕНИВО** через
  геттеры из `kb.settings` для live-reload поддержки

### 📦 Google Calendar — **services/calendar.py**
- `get_service`, `get_busy_intervals`, `slot_is_free`
- `list_events`, `create_event`, `delete_event`

### 🌐 Webhook / Flask — **main.py**
- `index`, `health_check`, `telegram_webhook`, `run`

### ⚙ State / Config / Knowledge — **utils.py**
- `Knowledge` dataclass с **replace_from()** (in-place мутация для /reload)
- `StateStore` (Redis + memory fallback, защищён от не-dict)
- `Config.from_env()`, `RateLimiter`, `LLMUsageLimiter`
- `DEFAULT_SETTINGS` — fallback'и для kb.settings
- `load_knowledge()` — парсер всех секций knowledge.md

### 💬 Свободный текст / парсер — **main.py + parsing.py**
- `on_text` (фильтр `_is_non_command_text` — пропускает не-/команды)
- `_on_text_inner` — главный switch
- `_handle_off_topic`, `_merge_llm_into_parsed`, `_is_cancel_inside_dialog`
- **parsing.py**: `parse(text, kb)`, `_detect_master_for_kb` (динамика по
  kb.masters+aliases с русской морфологией), `_detect_phone` (защищён от
  склеивания цифр времени с цифрами телефона), `_detect_services`

---

## 🎯 Cheatsheet: куда идти за X?

| Хочу… | Куда идти |
|-------|-----------|
| Изменить цену услуги | `knowledge.md` → `## Услуги` |
| Изменить текст приветствия | `knowledge.md` → `## Сообщения бота → ### welcome` |
| Добавить мастера | `knowledge.md` → `## Мастера` (с aliases для распознавания) |
| Изменить слоты записи | `knowledge.md` → `## Настройки → fixed_slot_hours` |
| Сменить бренд-эмодзи | `knowledge.md` → `## Бренд → emoji` |
| Реферальная программа: % скидки | `knowledge.md` → `## Реферальная программа → ### Параметры` |
| Лимиты Оракула / Groq | `knowledge.md` → `## Настройки` |
| Поправить логику Оракула | `handlers/oracle.py` |
| Поправить промпт Оракула | `psychologist.py` |
| Сменить LLM-провайдер | env: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_*` |
| Поправить парсер свободного текста | `parsing.py` |
| Поправить логику слотов | `services/slots.py` |
| Добавить новую slash-команду | соответствующий `handlers/*.py` register() |
| Поменять что-то на старте бота | `main.py _register_extracted_handlers()` |

---

## 🧭 Принципы

1. **Фича в `handlers/` или `services/`** — она изолирована, ищи там.
2. **Фичи нет в этих папках** — она в `main.py` (`cmd_start`, info-команды).
3. **Контент** (цены, мастера, тексты, иконки) — `knowledge.md`. Не правь код.
4. **Настройки** (числа, лимиты) — `knowledge.md → ## Настройки`. Не правь код.
5. **Конфиг провайдеров** (env-vars) — `utils.Config.from_env`.

**После `/reload`** все handlers видят новые значения kb автоматически —
константы `FIXED_SLOTS_HOURS`, `ALL_MASTERS`, `CATEGORY_ICONS` и т.п.
читаются лениво через геттеры из `kb.settings` / `kb.categories` / `kb.masters`.

## 📚 Дополнительная документация

- [BOOKING_STATUSES.md](BOOKING_STATUSES.md) — Полное описание всех статусов записей (`confirmed`, `cancelled`, `rescheduled`, `completed`, `no_show`) и их влияния на счётчики визитов и дизайн-бонусов. Обязательно читать перед работой с repair-командами и аналитикой.
