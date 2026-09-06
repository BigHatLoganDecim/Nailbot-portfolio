# NailBot

Telegram-бот записи: клиент бронирует визит в чате, мастер ведёт день, график и прайс в том же боте.

**Живое демо:** [https://t.me/Atlas_Nailbot](https://t.me/Atlas_Nailbot)

Демо-контент — студия **Luna Studio** (`knowledge.md`). Ключи и персональные данные боевого салона в репозиторий не входят.

[English](#english)

## Что делает

| Роль | В боте |
|---|---|
| Клиент | запись живым текстом или кнопками, «Мои записи» (перенос / отмена), цены, правила, реферальная ссылка |
| Мастер | сегодня / завтра / неделя, свободные окна, выходной и отпуск, прайс, сетка графика на месяц, карточки клиентов |
| Админ | `/stats`, рассылка, бэкап, `/reload` контента без рестарта |

Напоминания клиенту: за 24 часа и за 2 часа до визита. Мастеру — сразу о новой записи и отмене.

Тот же контур ставится на барбершоп, автосервис, клинику, репетитора: меняются `knowledge.md` и env, не Python. Оценка первого деплоя — 2–4 часа, если контент готов. Разбор: [CLONE_GUIDE.md](CLONE_GUIDE.md).

## Как устроено

```
Telegram
   │
   ▼
Flask webhook  ──►  telebot
                        │
                        ├── parsing.py     обычные фразы, без модели
                        ├── llm.py         только если парсер не понял; ответ — JSON
                        ├── Redis          шаг диалога
                        ├── Postgres       правда слота
                        └── Google Cal     проекция, не база
```

- **Слот** хранится в Postgres (`appointments`, `work_intervals`, `services`, `outbox`). Пока клиент подтверждает, слот в статусе `held` (5 минут). Два клиента на одно окно не проходят: unique + исключение пересечений.
- **Google Calendar** пишется после записи. Если Google недоступен, бронь в боте не откатывается. Воркер догоняет через outbox.
- **`knowledge.md`** — бренд, тексты, шаблон услуг. Живой прайс, который мастер правит в боте, читается из Postgres; сид из файла не затирает уже сохранённые цены.
- **LLM** (Groq / DeepSeek / OpenRouter / любой OpenAI-совместимый) не ведёт беседу и не назначает слоты. Нет ключа — бот работает на парсере.

На бесплатном хостинге (512 MB): `LEAN_MODE`, keep-alive `/ping`, бэкап в Telegram. Данные клиента: `/privacy`, `/delete_my_data`.

## Откуда продукт

Собран для салона с живым потоком (~70 визитов в месяц, потолок около 200 клиентов) вместо платного Telegram Mini App. Мастер ведёт смену в боте, а не в календаре как в источнике правды.

| | |
|---|---|
| Май 2026 | запись, календарь, корзина, роли клиент / мастер |
| Июнь | карточки клиентов, отзывы, рефералка, сертификаты |
| Июль–август | режим под 512 MB, общие слоты, keep-alive |
| Сентябрь | Postgres как правда слота, outbox, меню мастера, прайс из базы |

Три инварианта, которые держат запись:

1. Одно окно — один клиент (`held` с TTL, конфликт ловит база, не календарь).
2. Отмена из «Мои записи» снимает бронь в Postgres и событие в Google, а не только шаг в Redis.
3. Файл услуг — шаблон. Цена, которую мастер поставил в боте, после деплоя не откатывается.

Карта кода: [NAVIGATION.md](NAVIGATION.md).

## Стек

Python 3.12 · pyTelegramBotAPI · Flask · SQLAlchemy 2 · Alembic · Postgres · Redis · APScheduler · Google Calendar API · OpenAI-совместимый LLM

## Запуск

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
cp .env.example .env            # BOT_TOKEN от @BotFather
docker compose up -d            # Postgres + Redis, по желанию
alembic upgrade head            # если задан DATABASE_URL
python main.py
```

Пустой `WEBHOOK_URL` — long polling. Нет `DATABASE_URL` — SQLite и календарь. Нет LLM-ключа — только парсер. Нет Redis — шаг диалога в памяти процесса.

```bash
python scripts/test_open_slots.py
python scripts/test_price_catalog.py
python scripts/test_shared_calendar.py
python scripts/test_p0_slots.py
python scripts/test_p1_worker.py
python scripts/test_reminder_routing.py
```

В демо выключен «Оракул» (`oracle: no` в `knowledge.md`) — для записи он не нужен. Mini App и предоплаты в этой версии нет.

## Лицензия

MIT, [LICENSE](LICENSE).

---

## English

Telegram booking bot: clients book in chat; the specialist runs the day, schedule and price list in the same bot.

**Live demo:** [https://t.me/Atlas_Nailbot](https://t.me/Atlas_Nailbot)

Built for a live studio (~70 visits/month) as a replacement for a paid Telegram Mini App. This snapshot uses the demo brand **Luna Studio** — no production secrets or personal data.

Slot source of truth is Postgres; Google Calendar is a projection. A deterministic parser handles the common path; an OpenAI-compatible LLM is a JSON-schema fallback only. Clone another vertical by editing `knowledge.md` and env, not Python.
