# TEMPLATE_PLAN.md — статус шаблонизации

> **Большинство хардкодов вынесено.** Бот теперь можно развернуть под другой
> бизнес (барбершоп, автосервис, клиника, коуч) только правкой `knowledge.md`
> и `.env`, без правки `.py`. Этот файл — статус-чеклист.

---

## ✅ Done (готово)

### Контент бизнеса в knowledge.md
- ✅ Бренд (name, short_name, channel, emoji)
- ✅ Мастера — любые имена (раньше был фильтр под два хардкод-имени)
- ✅ Мастера — `aliases` для распознавания в свободном тексте
- ✅ Услуги с категориями
- ✅ Категории — иконки (kb.categories)
- ✅ Синонимы услуг — kb.service_synonyms
- ✅ Правила, контакты, сертификаты
- ✅ Промокоды (с автоматическим выбором первого как примера в UI)
- ✅ Сообщения бота (welcome, ask_master, ask_phone, off_topic, confirm, booked, error)
- ✅ Реферальная программа: program_name, скидка%, тексты бонусов и нот мастеру

### Настройки в knowledge.md → kb.settings
- ✅ `fixed_slot_hours` (10,12,14,16,18,20 для маникюра; 9,9:30,10... для барбершопа)
- ✅ `min_hours_before_booking` (3 для маникюра; 1 для парикмахерской)
- ✅ `slot_duration_min`, `default_service_duration_min`
- ✅ `date_picker_max_days`
- ✅ `review_delay_minutes_after_end`
- ✅ `psy_max_sessions_per_day`, `psy_max_turns` (Оракул)
- ✅ `off_topic_strike_limit`, `off_topic_session_limit` (anti-spam)
- ✅ `idle_client_threshold_days`, `idle_msg_cooldown_days` (idle-маркетинг)
- ✅ `llm_daily_request_limit`, `llm_daily_token_limit` (квоты Groq)

### Парсер — динамика по kb
- ✅ `_detect_master_for_kb` — regex из kb.masters + aliases с русской морфологией
- ✅ `_detect_services` — kb.service_synonyms (с legacy fallback)
- ✅ `_detect_phone` — устойчив к смешению цифр времени и телефона (баг #5)

### Live-reload
- ✅ `/reload` через `Knowledge.replace_from()` — мутирует kb in-place
- ✅ Handler-модули видят изменения kb мгновенно через геттеры (нет
  capture константных значений в register-time)
- ✅ main-константы (ALL_MASTERS, FIXED_SLOTS_HOURS и т.п.) обновляются
  в cmd_reload

### Бренд-чистка кода
- ✅ Эмодзи 💅 (5 мест) → `kb.brand['emoji']`
- ✅ «Запись через NailBot» → «Запись через бот {brand}»
- ✅ Backup filename `nailbot_backup_*` → `{brand_slug}_backup_*`
- ✅ Sentry-test text → «{brand} Sentry test»
- ✅ Flask root endpoint → «{brand} is running.»
- ✅ Header «Реферальная программа «Подружка»» → `program_name` из kb
- ✅ Хардкод промокода `ВДВОЁМ` → первый valid из kb.promo_codes

---

## 🟡 Известные ограничения

### Legacy fallback'и в parsing.py
Если в knowledge.md НЕ задана секция `## Синонимы услуг` — используется
`_SERVICE_SYNONYMS` под маникюр. Если НЕ заданы мастера в kb.masters
(шаблон совсем пустой) — `_detect_master` legacy ловит «денис»/«юля».

Это **намеренно**: чтобы старый knowledge.md без новых секций работал
без обновления. Для шаблона нового бизнеса просто переопределяй
секции — legacy fallback не сработает (синонимы из kb приоритетнее).

### Промпты Оракула захардкожены под аудиторию салона
`psychologist.py` содержит 3 личности (Психолог Сергей, Экстрасенс Веда,
Мистик Алекс) — заточены под женскую аудиторию. Для автосервиса/клиники
отключай через `kb.features.oracle: no` в knowledge.md.

✅ **Kill-switch реализован**:
- Кнопка «🪞 Оракул» исчезает из главного меню
- `handle_psy_button` отказывает: «Эта функция не активирована»
- Тексты самих 3 личностей остаются захардкожены — для русскоязычной
  аудитории это OK, для англ./каз. потребуется перевод.

### CLONE_GUIDE.md устарел
Был написан под старую структуру kb. Под новую (с aliases, settings,
categories, synonyms, referral, emoji) надо пересобрать.

---

## ❌ Не делалось (low priority)

### Шаблоны под популярные вертикали
Идея — папка `templates/` с готовыми knowledge.md:
- `templates/barbershop/knowledge.md`
- `templates/autoservice/knowledge.md`
- `templates/clinic/knowledge.md`
- `templates/tutor/knowledge.md`

Плюс CLI `python init_template.py barbershop` — копирует template + просит env.

Когда нужно: только если клиент попросит реально клонировать бот.

### Полная i18n клиентских текстов
~179 user-facing строк в коде (ошибки, кнопки, заголовки). Большинство
не бренд-специфичных — типа «Не понял мастера», «Подтверждаю», «Хорошо».
Их вынос в kb.messages — много работы, мало ценности (русский остаётся
русским).

Когда нужно: только если будет английская / казахская версия.

### Фича-флаги для kb.features
✅ **Реализовано.** Секция `## Фича-флаги` в knowledge.md:
```
- oracle: yes        # вкл/выкл «Оракул» (3 личности LLM)
- news: yes          # вечерняя сводка городских новостей
- referrals: yes     # реферальная программа «приведи друга»
- llm_parser: yes    # LLM-fallback парсера при unknown intent
```

Подключено к:
- `handlers/menu.py` — скрывает кнопки в главном меню
- `handlers/oracle.py` — `handle_psy_button` отказывает
- `handlers/news.py` — `_news_enabled()` учитывает kb-флаг
- `main.py` — `_send_invite()` отказывает при `referrals: no`,
  on_text не зовёт LLM при `llm_parser: no`

Меняется через `/reload` без рестарта.

### Параметризация города новостей
✅ **Реализовано.** Секция `## Новости города`:
```
- city: Санкт-Петербург
- city_short: СПб
- rss_url: https://www.fontanka.ru/fontanka.rss
```

`news.py` `REWRITE_SYSTEM_TEMPLATE` использует `{city}`. Подменив на
«Москва» и rss-url RIA Москвы — фича работает под москвичей.

---

## 📊 Метрика готовности шаблона

| Критерий | Статус |
|----------|--------|
| Развернуть копию без правки `.py` (только knowledge + env) | ✅ |
| Все клиент-видимые бренд-имена/эмодзи берутся из kb | ✅ |
| Все числовые параметры в kb.settings (можно менять без рестарта через `/reload`) | ✅ |
| Парсер свободного текста распознаёт любых мастеров из kb | ✅ |
| Готовые `templates/` для популярных бизнесов | ❌ (не нужно пока) |
| Англ./каз. локализация | ❌ (не нужно пока) |
| `CLONE_GUIDE.md` обновлён под новую kb | ⏳ (имеет смысл сделать) |

---

## 🎯 Если нужно реально клонировать под другой салон

1. Скопировать репо.
2. Заменить `knowledge.md` целиком — заполнить под новый бизнес:
   - `## Бренд` → name + emoji
   - `## Мастера` → новые имена + aliases
   - `## Услуги` → свои с категориями
   - `## Категории услуг` → свои иконки
   - `## Синонимы услуг` → свои («волосы → стрижка» для барбершопа)
   - `## Настройки` → slot_hours (барбершопу ставить 30-минутные)
   - `## Реферальная программа` → свой program_name, скидка%, бонус
3. Создать в BotFather бота, прописать в Render env:
   - `BOT_TOKEN`, `WEBHOOK_URL`, `REDIS_URL`, `ADMIN_IDS`
   - `GROQ_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`
   - (опц.) `SENTRY_DSN`, `LLM_BASE_URL` для DeepSeek/VseGPT
4. Деплой → проверить `/start`, `/menu`, запись клиента.

Никаких правок `.py` файлов не нужно.
