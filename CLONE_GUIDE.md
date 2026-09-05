# 🚀 CLONE_GUIDE — Развернуть бота под новый бизнес

Универсальный гайд по клонированию: маникюрный салон, барбершоп, автосервис, клиника, репетиторская студия — всё, что укладывается в модель «клиент записывается к мастеру/специалисту на услугу через Telegram».

**Время:** 2–4 часа на полный деплой. Самый длительный шаг — наполнение `knowledge.md` контентом нового бизнеса.

**Главное правило:** код **не правится**. Всё содержимое нового бота (бренд, мастера, услуги, тексты, лимиты, фичи) живёт в `knowledge.md` + env-переменных Render.

---

## 0. Архитектура одним взглядом

```
nailbot/
├── main.py                 # Bot setup, webhook, главный entry
├── handlers/               # Каждая фича — отдельный модуль
│   ├── booking.py          #   запись (главный flow + reschedule + reviews)
│   ├── master.py           #   день мастера, прайс, график месяца
│   ├── menu.py             #   inline /menu (диспетчер)
│   ├── admin.py            #   /stats, /broadcast, /sync_calendar, бэкап
│   ├── clients.py          #   карточки клиентов для мастеров
│   ├── oracle.py           #   фича «Оракул» (LLM-собеседник, 3 личности)
│   ├── vacation_sick.py    #   /vacation, /sick (массовая отмена)
│   └── stats.py            #   квоты LLM, бэкап-отчёты
├── services/
│   ├── slots.py            #   расчёт свободных слотов, дата/время-пикеры
│   ├── calendar.py         #   Google Calendar API обёртка (проекция)
│   └── catalog.py          #   прайс: Postgres overlay на knowledge.md
├── database/               # Postgres SoT слотов + SQLite legacy CRM
├── parsing.py              # Парсер свободного текста (intents, мастера)
├── llm.py                  # OpenAI-совместимый LLM-клиент
├── psychologist.py         # 3 личности «Оракула»
├── utils.py                # Knowledge, StateStore, Config, лимитеры
├── knowledge.md            # ⭐ ВЕСЬ контент бота
└── .env.example
```

**Слои данных:**
- `knowledge.md` → структурируется в `Knowledge` объект на старте, перечитывается `/reload` без рестарта
- **Redis** — состояние диалога клиента (TTL 30 дней). Очередь APScheduler-задач.
- **Postgres** (`DATABASE_URL`) — правда слотов: appointments, work_intervals, services, outbox
- **SQLite** — legacy CRM (клиенты/история), если Postgres ещё не задан
- **Google Calendar** — проекция расписания. Ошибка Google не откатывает запись в боте.

---

## 1. Что подготовить ДО клонирования (15 минут)

| Что | Где взять | Зачем |
|-----|-----------|-------|
| Telegram-бот | [@BotFather](https://t.me/BotFather) → `/newbot` | Сам бот |
| GitHub-аккаунт | github.com | Хостинг кода |
| Render-аккаунт | render.com | Бесплатный деплой |
| Google Cloud-аккаунт | console.cloud.google.com | Calendar API |
| (опц.) Groq API ключ | console.groq.com | Бесплатные LLM-фичи |
| (опц.) Sentry-проект | sentry.io | Мониторинг ошибок |
| Telegram ID мастеров | [@userinfobot](https://t.me/userinfobot) | Чтобы бот мог им писать |
| Контент нового бизнеса | мастера/услуги/цены/правила | Заливать в `knowledge.md` |

---

## 2. Клонирование (5 минут)

### Через GitHub UI (рекомендуется)
1. Открой репозиторий-источник на GitHub
2. **Use this template** → новый репо в твоём аккаунте
3. Имя `<имя-салона>-bot`, **Private**
4. `git clone https://github.com/<твой>/<имя>.git`

### Через `gh` CLI:
```bash
gh repo clone <owner>/<repo> новый-бот
cd новый-бот
```

---

## 3. Настройка `knowledge.md` (1–2 часа, главный шаг)

Открой `knowledge.md`. Внутри ~12 секций — заменяй под новый бизнес по очереди.

### 🏷 `## Бренд`
```markdown
## Бренд
- name: Sun Beauty Lab
- short_name: Sun
- channel: https://t.me/sunbeautylab
- emoji: 💅                # бренд-эмодзи в Calendar, новостях, шапках
```

### 👤 `## Мастера`
```markdown
### Аня
- telegram_id: 123456789       ← @userinfobot
- calendar_id: anya@gmail.com  ← email Google Calendar
- contact_phone: +79991234567
- work_hours: 10:00-22:00      ← либо одно значение, либо два:
- work_hours_weekday: 14:00-22:00
- work_hours_weekend: 10:00-18:00
- work_dates: 2026-06-02, 2026-06-03, ...
- специализация: маникюр, педикюр
- описание: Тёплая, опыт 7 лет, любит минимализм...
- aliases: аня, анют, анюта, анечка     ← опционально, для парсера
```

⚠️ **ВАЖНО:** Каждый мастер должен **один раз написать боту `/start`**, иначе Telegram запрещает боту слать сообщения первым.

`aliases` — дополнительные формы имени для распознавания свободного текста. Парсер автоматически добавляет морфологию (падежи), но всё равно полезно для уменьшительных.

### 💰 `## Услуги`
```markdown
## Услуги
Формат: `Название | Цена | Длительность | Мастера`

# --- Стрижки ---
- Мужская стрижка | 2000 ₽ | 60 мин | Аня, Маша
- Детская стрижка | 1500 ₽ | 45 мин | Аня

# --- Дополнительно (без длительности — мастер расширит) ---
- Камуфляж седины | 800 ₽ | — | Аня
```

- `# --- Категория ---` создаёт раздел в `/menu → Цены`
- Длительность с числом → суммируется при расчёте свободного слота
- Длительность `—` → услуга-аддон, длительность не влияет

### 📂 `## Категории услуг`
```markdown
## Категории услуг
- Стрижки: ✂️
- Окрашивание: 🎨
- Уход: 🧴
```

### 🔄 `## Синонимы услуг`
```markdown
## Синонимы услуг
- волосы: стрижка
- покрасить: окрашивание
- помыть: уход
```

Подстрока в речи клиента → канонический корень для поиска услуги.

### 🎟 `## Промокоды`
```markdown
## Промокоды

### НОВЫЙ2026
- description: -15% на первый визит для новых клиентов.
- valid: yes
```

### 📜 Адрес / Правила / Сертификаты
Просто текст. Поддерживаются HTML-теги (`<b>`, `<i>`, `<code>`).

### 💬 `## Сообщения бота`
```markdown
### welcome
Привет! 👋 Я бот студии <b>Sun Beauty Lab</b>.

### confirm
Проверь запись:
• Мастер: {master}
• Процедуры: {services}
• Когда: {datetime}
```

Переменные `{master}`, `{services}`, `{datetime}`, `{phone}` подставляются автоматически.

### 🤝 `## Реферальная программа`
```markdown
## Реферальная программа

### Параметры
- enabled: yes
- program_name: Друг
- new_client_discount_pct: 15
- new_client_offer_text: на первый визит скидка 15%
- referrer_bonus_text: бесплатный камуфляж седины
- referrer_bonus_master_note: Реф. бонус: бесплатный камуфляж
```

### ⚙️ `## Настройки`
```markdown
## Настройки
- fixed_slot_hours: 10,11,12,13,14,15,16,17,18,19   # барбершоп — часовые
- min_hours_before_booking: 1
- slot_duration_min: 60
- default_service_duration_min: 60
- date_picker_max_days: 21
- review_delay_minutes_after_end: 60
- psy_max_sessions_per_day: 3
- psy_max_turns: 12
- off_topic_strike_limit: 2
- off_topic_session_limit: 5
- idle_client_threshold_days: 60
- idle_msg_cooldown_days: 45
- llm_daily_request_limit: 14400
- llm_daily_token_limit: 500000
```

Все опциональны — если не указать, бот возьмёт дефолты из `utils.py DEFAULT_SETTINGS`.

### 🚦 `## Фича-флаги` (важно для адаптации под бизнес)
```markdown
## Фича-флаги
- oracle: no        # автосервис, барбершоп — Оракул не нужен
- referrals: yes
- llm_parser: yes
```

| Флаг | Что выключает |
|------|---------------|
| `oracle: no` | Скрывает кнопку «🪞 Оракул» из меню, `handle_psy_button` отказывает |
| `referrals: no` | `/invite` отказывает, кнопка «🤝 Пригласить» исчезает |
| `llm_parser: no` | LLM не вызывается на непонятных сообщениях (экономит токены) |

### 🌆 `## Новости города`
```markdown
## Новости города
- city: Москва
- city_short: Мск
- rss_url: https://www.m24.ru/feed.xml
```

По умолчанию — Санкт-Петербург / Фонтанка.

---

## 4. Деплой на Render (30 минут)

### 4.1 Запушить контент
```bash
git add knowledge.md
git commit -m "knowledge: контент нового бизнеса"
git push
```

### 4.2 Создать Web Service
1. [dashboard.render.com](https://dashboard.render.com) → **New** → **Web Service**
2. Подключи GitHub → выбери репо → **Connect**
3. Настройки:
   - **Name:** `<имя>-bot`
   - **Region:** Frankfurt
   - **Branch:** main
   - **Runtime:** Python 3
   - **Build:** `pip install -r requirements.txt`
   - **Start:** `python main.py`
   - **Instance:** Free
4. **Create Web Service** (пока не запустится — нет env)

### 4.3 Создать Redis
1. **New** → **Key Value**
2. **Name:** `<имя>-redis`, **Region** тот же, **Free**
3. Скопируй **Internal Redis URL**

### 4.4 Environment Variables
В Web Service → **Environment**:

| Key | Value | Откуда |
|-----|-------|--------|
| `BOT_TOKEN` | `8126...:AAH...` | @BotFather |
| `WEBHOOK_URL` | `https://<сервис>.onrender.com` | от Render |
| `REDIS_URL` | `redis://red-xxx:6379` | от Render Redis |
| `ADMIN_IDS` | `100000001,100000002` | через запятую |
| `LLM_PROVIDER` | `groq` (или `deepseek`/`openai`/`openrouter`/`vsegpt`) | задаёт base_url и дефолтные модели |
| `GROQ_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` / `VSEGPT_API_KEY` | ключ выбранного провайдера | соответствующий кабинет |
| `LLM_FALLBACK_MODEL` | `llama-3.3-70b-versatile` | запасная модель при сбое основной (см. ниже) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | весь JSON | см. шаг 5 |
| `SENTRY_DSN` | `https://...sentry.io/...` | sentry.io (опц.) |

**Тонкая настройка (опционально):** `LLM_MODEL_PARSER`, `LLM_MODEL_ORACLE`, `LLM_MODEL_NEWS`, `LLM_MODEL_SUMMARY`, `LLM_BASE_URL` — переопределяют пресет провайдера. Без них берутся дефолты из `LLM_PROVIDER_PRESETS` в `utils.py`.

**Авто-fallback:** при ошибке основной модели (decommissioned / 400 model_not_found / rate-limit по модели) `llm.py._chat` автоматически ретраит на `LLM_FALLBACK_MODEL`. НЕ ретраит на 401/permission/insufficient_quota и если уже на fallback. Снятие preview-моделей провайдером не роняет фичу.

После сохранения Render передеплоит. Жди в логах:
```
StateStore: подключение к Redis установлено
APScheduler запущен с Redis JobStore
Database initialized: /opt/render/.../nailbot.db
register: все 30 биндингов прошли проверку
Webhook установлен: https://<твой>.onrender.com/webhook
```

### 4.5 Проверка `/health`
```
https://<сервис>.onrender.com/health
```
Должен вернуть JSON со всеми компонентами `ok`.

### 4.6 Открой бота → `/start`
Должен ответить welcome из твоего `knowledge.md`.

---

## 5. Google Calendar (15 минут)

### 5.1 Service Account
1. [console.cloud.google.com](https://console.cloud.google.com) → новый проект `<имя>-bot`
2. **APIs & Services → Library** → **Google Calendar API** → **Enable**
3. **Credentials → + Create Credentials → Service account**
4. Имя `<имя>-bot-writer` → **Create**
5. **Keys → Add Key → JSON** → скачается файл
6. Скопируй email Service Account (вид `xxx@<проект>.iam.gserviceaccount.com`)

### 5.2 Расшарить календари
Каждый мастер на [calendar.google.com](https://calendar.google.com):
- его основной календарь → ⋮ → **Настройки и общий доступ**
- **Добавить пользователей** → email Service Account
- права: **Внесение изменений в мероприятия**

### 5.3 JSON в Render
Содержимое скачанного `.json` целиком → `GOOGLE_SERVICE_ACCOUNT_JSON`.

### 5.4 `calendar_id` мастеров в `knowledge.md`
Обычно gmail (`anya@gmail.com`). Запушь.

---

## 6. UptimeRobot (5 минут)

Чтобы Free Render не засыпал:
1. [uptimerobot.com](https://uptimerobot.com) → **+ New monitor**
2. Type: HTTP(s), URL: `https://<сервис>.onrender.com/health`, Interval: 5 минут

---

## 7. Avatar (опционально, 5 минут)

```bash
python avatar.py path/to/avatar.mp4
```

Требования: **640×640, ≤2 MB, ≤10 сек, без аудио, H.264**.

Конверсия:
```bash
ffmpeg -i input.mp4 -vf "scale=640:640:force_original_aspect_ratio=increase,crop=640:640" \
       -c:v libx264 -profile:v baseline -pix_fmt yuv420p -an -t 10 avatar.mp4
```

---

## 8. Чек-лист после деплоя

- [ ] `/start` отвечает welcome из твоего `knowledge.md`
- [ ] `/menu` показывает категории (без скрытых фича-флагами)
- [ ] Полный цикл записи: мастер → услуги → дата → телефон → подтверждение
- [ ] Запись появляется в Google Calendar мастера
- [ ] Мастер получает уведомление с именем клиента
- [ ] Через минуту — UptimeRobot показывает Up
- [ ] `/today` от мастера показывает запись
- [ ] (если включён Оракул) `/menu → Оракул → выбрать личность` → диалог
- [ ] Sentry ловит `/sentry_test`
- [ ] `/reload` от админа перечитывает `knowledge.md`
- [ ] `/llm_test` от админа показывает живой провайдер, base_url и какая модель реально отвечает (полезно после смены `LLM_PROVIDER`)

---

## 9. Что НЕ нужно править в коде

Эти подсистемы универсальны, любой бизнес работает «из коробки»:

| Подсистема | Файл | Почему универсальна |
|-----------|------|---------------------|
| Booking flow | `handlers/booking.py` | Не знает что за услуги — читает из kb |
| Парсер интентов | `parsing.py` | Динамически строит regex из kb.masters |
| Слот-логика | `services/slots.py` | Все часы/длительности из kb.settings |
| Reminder-задачи | `handlers/booking.py` | Тексты в kb.messages |
| Карточки клиентов | `handlers/clients.py` | Schema универсальный |
| Backup/restore | `handlers/admin.py` | Имя файла из kb.brand |
| Меню и команды | `handlers/menu.py` | Кнопки по features из kb |

**Только `knowledge.md` + env-переменные** = новый бот.

---

## 10. Адаптация под бизнес-вертикаль (без правки .py)

### Барбершоп
```markdown
## Настройки
- fixed_slot_hours: 10,11,12,13,14,15,16,17,18,19,20
- slot_duration_min: 60
- min_hours_before_booking: 1

## Фича-флаги
- oracle: no       # барбершопу Оракул не нужен
- referrals: yes

## Категории услуг
- Стрижки: ✂️
- Борода: 🧔
```

### Автосервис
```markdown
## Настройки
- fixed_slot_hours: 9,11,13,15,17     # 2-часовые слоты
- slot_duration_min: 120
- min_hours_before_booking: 24
- date_picker_max_days: 30

## Фича-флаги
- oracle: no       # психология тут не нужна
- news: no         # клиенты не приходят за новостями
- referrals: yes

## Категории услуг
- ТО: 🔧
- Шиномонтаж: 🛞
- Кузов: 🚗
```

### Клиника / врач
```markdown
## Настройки
- fixed_slot_hours: 9,10,11,12,14,15,16,17
- slot_duration_min: 30
- min_hours_before_booking: 4

## Фича-флаги
- oracle: no
- news: no
- referrals: no    # этика; платный кэшбек могут запретить
- llm_parser: no   # медицина — никаких LLM-импровизаций
```

### Репетитор / коуч
```markdown
## Настройки
- fixed_slot_hours: 15,16,17,18,19,20    # после школы/работы
- slot_duration_min: 60
- min_hours_before_booking: 12

## Фича-флаги
- oracle: yes      # уместно для коуча
- referrals: yes
```

---

## 11. Известные ограничения Free-тарифов

- **Render Free** засыпает за 15 минут без трафика → лечится UptimeRobot
- **Render Redis Free** — 25 MB, хватает на ~10 000 клиентов
- **Groq Free** — 14 400 запросов/день, 500K токенов/день
- **Telegram Bot API** — 30 сообщений/сек в массовой рассылке
- **Google Calendar Free** — 1 000 000 запросов/день, более чем достаточно

---

## 12. FAQ

| Вопрос | Где |
|--------|-----|
| Как добавить услугу? | `knowledge.md → ## Услуги` |
| Как изменить тексты бота? | `knowledge.md → ## Сообщения бота` |
| Как добавить мастера? | `knowledge.md → ## Мастера` + `## График работы` |
| Как поменять провайдера LLM? | env: `LLM_PROVIDER=deepseek\|openai\|openrouter\|vsegpt` + ключ провайдера; см. также `/llm_test` для проверки |
| Проверить какая модель реально отвечает (не заходя в Render)? | `/llm_test` от админа (алиасы `/llm_check`, `/мозги`); `/llm_test all` — все 4 фичи |
| Как выключить Оракул? | `knowledge.md → ## Фича-флаги → oracle: no` + `/reload` |
| Бот не отвечает | Render Logs + `/health` + Sentry |
| Календарь не создаёт события | Render Logs по слову `calendar:` |
| Напоминания не приходят | `/clear_reminders` от админа + новая запись |
| Клиент потерял запись | она в SQLite — `handle_my_bookings` восстановит из DB |
| Записать клиента «по телефону» | `/add_booking телефон \| имя \| когда \| услуги` |
| Ручные записи мастера в Calendar | `/sync_calendar` или авто-cron каждые 30 мин |

---

## 13. Эстимейт времени

| Сценарий | Время |
|----------|-------|
| Тот же бизнес (другой салон, та же модель) | 2 часа (одно знание + деплой) |
| Другая вертикаль (барбершоп / автосервис) | 3–4 часа (контент + чуть-чуть настроек) |
| Англоязычная локализация | + 4–8 часов (тексты в kb + psychologist.py) |
| Под другой LLM-провайдер (DeepSeek/OpenAI/VseGPT/OpenRouter) | + 5 минут (одна переменная `LLM_PROVIDER` + ключ) |

---

**Удачного запуска!** 🚀
