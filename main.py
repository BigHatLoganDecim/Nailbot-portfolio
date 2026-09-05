"""
Шаблонный бот записи на услуги (демо-контент: Luna Studio).

Архитектура:
  Telegram --> Flask webhook --> telebot --> main.py
                                              │
                                              ├── handlers/*.py — фичи (booking,
                                              │   menu, oracle, admin, master,
                                              │   clients, stats, vacation_sick)
                                              ├── services/*.py — slots, calendar
                                              ├── parsing.py — intent/master/phone
                                              ├── llm.py — OpenAI-compat клиент
                                              └── utils.py — Knowledge, StateStore

Контент и настройки бота (бренд, мастера, услуги, цены, тексты, лимиты)
лежат в knowledge.md и перечитываются командой /reload без рестарта процесса.
Чтобы развернуть копию под другой бизнес — нужно отредактировать knowledge.md
и переменные окружения. Правки .py файлов обычно не требуются.

См. NAVIGATION.md (карта кода) и TEMPLATE_PLAN.md (статус шаблонизации).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import sys
from typing import Any

# Подгружаем .env до чтения переменных окружения. Если dotenv не установлен —
# не падаем, просто полагаемся на реальное окружение (как в Docker/Render/Heroku).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Sentry — мониторинг ошибок. Инициализируем как можно раньше, ДО Flask.
# Если SENTRY_DSN не задан или пакет не установлен — бот работает как раньше.
_sentry_enabled = False
_sentry_dsn = os.getenv("SENTRY_DSN", "").strip()
if _sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[
                FlaskIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
            # Только ошибки и выше — для бесплатного тарифа экономно
            traces_sample_rate=0.0,
            # Не отправляем PII (телеграм-имена и т.п.) автоматически
            send_default_pii=False,
            environment=os.getenv("SENTRY_ENV", "production"),
            release=os.getenv("RENDER_GIT_COMMIT", "")[:7] or None,
        )
        _sentry_enabled = True
    except ImportError:
        # sentry_sdk не установлен — игнорируем, бот не должен падать из-за этого
        pass
    except Exception as e:
        # Любая другая проблема инициализации — логируем, но не падаем
        logging.getLogger("nailbot").warning("Sentry init failed: %s", e)

import telebot
from flask import Flask, abort, request
from telebot import types

import parsing
# psychologist используется в handlers/oracle.py
# (они импортят их напрямую). В main больше не нужны.
from llm import LLMClient
from utils import (
    Config,
    Knowledge,
    LLMUsageLimiter,
    RateLimiter,
    StateStore,
    load_knowledge,
    setup_logging,
)

# New long-term data layer (SQLite for clients + bookings history)
from database import Database, init_db

log = logging.getLogger("nailbot.main")


# ---------------------------------------------------------------------------
# Шаги диалога записи
# ---------------------------------------------------------------------------

STEP_IDLE = "idle"
STEP_CHOOSE_MASTER = "choose_master"
STEP_CHOOSE_SERVICES = "choose_services"
STEP_ENTER_DATETIME = "enter_datetime"
STEP_ENTER_PHONE = "enter_phone"
STEP_CONFIRM = "confirm"
STEP_PROMO_INPUT = "promo_input"
STEP_AWAITING_REVIEW = "awaiting_review"
STEP_PSY_DIALOG = "psy_dialog"
STEP_BROADCAST_TEXT = "broadcast_text"

# REVIEW_DELAY_MINUTES_AFTER_END теперь из kb.settings (см. ниже после load_knowledge)
# PSY_MAX_*, GROQ_DAILY_* теперь из kb.settings — см. ниже после load_knowledge.
# Раньше были константами модуля; вынесли чтобы менять без рестарта (/reload).

# ALL_MASTERS теперь динамический — собирается из kb.masters после загрузки
# knowledge.md. Использовать его можно только ПОСЛЕ load_knowledge() (ниже).
# Раньше имена мастеров были захардкожены — мешало шаблонизации под другие бизнесы.
ALL_MASTERS: tuple[str, ...] = ()  # заполняется после load_knowledge ниже


# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------

config = Config.from_env()
setup_logging(config.log_level)

if _sentry_enabled:
    log.info("Sentry подключён — все ошибки полетят в дашборд")
else:
    log.info("Sentry не настроен (SENTRY_DSN не задан) — мониторинг ошибок выключен")

if not config.bot_token:
    log.error("BOT_TOKEN не задан в окружении. Бот не может стартовать.")
    sys.exit(1)

kb: Knowledge = load_knowledge(config.knowledge_path)
# Динамический ALL_MASTERS из загруженного knowledge.md. Если в файле мастеров
# нет (битый knowledge) — fallback на пустой tuple. Команды бота с пустым
# ALL_MASTERS работать не смогут, но это лучше чем NameError при импорте.
ALL_MASTERS = tuple(kb.masters.keys())
if not ALL_MASTERS:
    log.error("knowledge.md не содержит мастеров — booking flow работать не будет")
state_store = StateStore(
    redis_url=config.redis_url or None,
    require_redis=bool(config.webhook_url),
)

# === New Database Layer (long-term storage) ===
# Initialized early so it's available everywhere.
# This is the foundation for reliable history + future AI analytics.
db = Database()
try:
    db.init()
    from database.connection import get_db_path
    log.info("Long-term database initialized (SQLite): %s", get_db_path())
except Exception as e:
    log.error("Failed to initialize long-term database: %s", e)
    # We do not crash here yet during transition period.
    # In the future we may want to make this fatal.

try:
    from database.pg import init_pg, pg_enabled
    from database.appointments import seed_from_knowledge
    if init_pg() and pg_enabled():
        seed_from_knowledge(kb)
        log.info("Postgres slot SoT ready (masters/services seeded)")
except Exception as e:
    log.error("Postgres slot SoT init failed: %s", e)

rate_limiter = RateLimiter(max_msgs=15, window_sec=30)
llm_usage = LLMUsageLimiter(max_calls_per_session=10)
llm = LLMClient(
    api_key=config.llm_api_key,
    base_url=config.llm_base_url,
    model_parser=config.llm_model_parser,
    model_oracle=config.llm_model_oracle,
    model_summary=config.llm_model_summary,
    fallback_model=config.llm_fallback_model,
)

# Планировщик для напоминаний клиентам.
# Если задан REDIS_URL — используется RedisJobStore (задачи переживают редеплои).
# Иначе — in-memory (после рестарта Render задачи теряются).
scheduler = None
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    if config.redis_url:
        try:
            from urllib.parse import urlparse
            from apscheduler.jobstores.redis import RedisJobStore
            u = urlparse(config.redis_url)
            jobstore = RedisJobStore(
                host=u.hostname or "localhost",
                port=u.port or 6379,
                db=int(u.path.lstrip("/")) if u.path else 0,
                password=u.password,
                jobs_key="nailbot:jobs",
                run_times_key="nailbot:run_times",
            )
            scheduler = BackgroundScheduler(
                jobstores={"default": jobstore},
                timezone="Europe/Moscow",
            )
            scheduler.start()
            log.info("APScheduler запущен с Redis JobStore — задачи переживают редеплои")
        except Exception as e:
            log.warning("Redis JobStore не запустился: %s. Падаем на in-memory.", e)
            scheduler = None
    if not scheduler:
        scheduler = BackgroundScheduler(timezone="Europe/Moscow")
        scheduler.start()
        log.info("APScheduler запущен в режиме in-memory")
except Exception as e:
    log.warning("APScheduler не стартанул: %s — напоминания будут только в логах", e)
    scheduler = None

# Все настройки бота — теперь из kb.settings (knowledge.md `## Настройки`).
# Если секции нет — kb.settings содержит DEFAULT_SETTINGS из utils.py.
# Это позволяет под другой бизнес поменять параметры без правки кода.
OFF_TOPIC_STRIKE_LIMIT = kb.settings["off_topic_strike_limit"]
OFF_TOPIC_SESSION_LIMIT = kb.settings["off_topic_session_limit"]
MIN_HOURS_BEFORE_BOOKING = kb.settings["min_hours_before_booking"]
SLOT_DURATION_MIN = kb.settings["slot_duration_min"]
FIXED_SLOTS_HOURS = list(kb.settings["fixed_slot_hours"])
DEFAULT_SERVICE_DURATION_MIN = kb.settings["default_service_duration_min"]
DATE_PICKER_MAX_DAYS = kb.settings["date_picker_max_days"]
REVIEW_DELAY_MINUTES_AFTER_END = kb.settings["review_delay_minutes_after_end"]
PSY_MAX_SESSIONS_PER_DAY = kb.settings["psy_max_sessions_per_day"]
PSY_MAX_TURNS = kb.settings["psy_max_turns"]
GROQ_DAILY_REQUEST_LIMIT = kb.settings["llm_daily_request_limit"]
GROQ_DAILY_TOKEN_LIMIT = kb.settings["llm_daily_token_limit"]

# Иконки категорий услуг из knowledge.md `## Категории услуг`.
# Категории без иконки получают "📋" в местах использования.
# Для шаблона другого бизнеса — задавай иконки в knowledge.md.
CATEGORY_ICONS = dict(kb.categories)

# LEAN_MODE (default 1): free tier 512MB / 0.1 CPU — меньше потоков и фоновых задач.
_LEAN_MODE = os.getenv("LEAN_MODE", "1").strip().lower() not in ("0", "false", "no", "off")

# threaded=False: webhook обрабатывается синхронно — меньше потоков и RAM
bot = telebot.TeleBot(config.bot_token, threaded=not _LEAN_MODE, parse_mode="HTML")


def _install_retry_after(bot_obj) -> None:
    """Catch TelegramRetryAfter / 429: wait retry_after, cap outbound ~1 msg/s per chat."""
    orig = bot_obj.send_message
    last_by_chat: dict[int, float] = {}
    last_global = [0.0]
    lock = __import__("threading").Lock()

    def send_message(*args, **kwargs):
        import time as _t
        chat_id = args[0] if args else kwargs.get("chat_id")
        for attempt in range(4):
            with lock:
                now = _t.monotonic()
                wait = 0.0
                if chat_id is not None:
                    prev = last_by_chat.get(int(chat_id), 0.0)
                    wait = max(wait, 1.0 - (now - prev))
                wait = max(wait, 0.04 - (now - last_global[0]))  # ~25/s
                if wait > 0:
                    _t.sleep(min(wait, 5))
            try:
                result = orig(*args, **kwargs)
                with lock:
                    last_global[0] = _t.monotonic()
                    if chat_id is not None:
                        last_by_chat[int(chat_id)] = last_global[0]
                return result
            except Exception as e:
                retry_after = None
                err = getattr(e, "error_code", None)
                params = getattr(e, "result_json", None)
                if isinstance(params, dict):
                    err = err or params.get("error_code")
                    retry_after = (params.get("parameters") or {}).get("retry_after")
                text = str(e)
                if retry_after is None and "retry after" in text.lower():
                    import re as _re
                    m = _re.search(r"retry after (\d+)", text, _re.I)
                    if m:
                        retry_after = int(m.group(1))
                if err == 429 or retry_after:
                    delay = int(retry_after or 1)
                    log.warning("TelegramRetryAfter %ss (attempt %s)", delay, attempt + 1)
                    _t.sleep(min(delay, 30))
                    continue
                raise
        return orig(*args, **kwargs)

    bot_obj.send_message = send_message  # type: ignore[method-assign]


_install_retry_after(bot)
app = Flask(__name__)
if _LEAN_MODE:
    log.info("LEAN_MODE=on: free-tier профиль (1 worker, урезанные cron, без лишнего)")


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def reply(chat_id: int, key: str, **fmt) -> None:
    """Отправить шаблонное сообщение из knowledge.md по ключу."""
    text = kb.msg(key, **fmt)
    if not text:
        text = kb.msg("error") or "Сообщение временно недоступно."
    bot.send_message(chat_id, text)


def safe_send_long(chat_id: int, text: str, parse_mode: str | None = None,
                    reply_markup=None, max_chunk: int = 4000) -> None:
    """
    Отправляет длинное сообщение, разбивая на куски по переносам строк/абзацев.
    Telegram режет всё что длиннее 4096 символов — мы делим по 4000 для запаса.
    Reply markup и parse_mode применяются только к последнему куску.
    """
    if not text:
        return
    if len(text) <= max_chunk:
        try:
            bot.send_message(chat_id, text, parse_mode=parse_mode,
                              reply_markup=reply_markup)
        except Exception as e:
            log.warning("safe_send_long single: %s", e)
        return

    # Разбиваем по двойному переносу строки (абзацы)
    parts = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for p in parts:
        candidate = (current + "\n\n" + p) if current else p
        if len(candidate) <= max_chunk:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Сам параграф длинный — режем по одиночным \n
            if len(p) > max_chunk:
                lines = p.split("\n")
                buf = ""
                for ln in lines:
                    if len(buf) + len(ln) + 1 > max_chunk:
                        if buf:
                            chunks.append(buf)
                        buf = ln[:max_chunk]
                    else:
                        buf = (buf + "\n" + ln) if buf else ln
                if buf:
                    current = buf
                else:
                    current = ""
            else:
                current = p
    if current:
        chunks.append(current)

    # Для HTML — балансируем теги между чанками, чтобы Telegram не вернул 400
    if parse_mode and parse_mode.upper() == "HTML":
        chunks = _balance_html_chunks(chunks)

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        try:
            bot.send_message(
                chat_id, chunk,
                parse_mode=parse_mode,
                reply_markup=reply_markup if is_last else None,
            )
        except Exception as e:
            log.warning("safe_send_long chunk %d/%d: %s", i + 1, len(chunks), e)


_HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)(\s[^>]*)?>")


def _balance_html_chunks(chunks: list[str]) -> list[str]:
    """Если чанк оставил незакрытые теги — закрываем их в конце и переоткрываем
    в начале следующего. Так Telegram parse_mode=HTML не возвращает 400."""
    if not chunks:
        return chunks
    result: list[str] = []
    carry_open: list[tuple[str, str]] = []  # стек: (имя_тега, исходный_тег_с_атрибутами)
    for chunk in chunks:
        prefix = "".join(orig for _, orig in carry_open)
        body = chunk
        stack: list[tuple[str, str]] = list(carry_open)
        for m in _HTML_TAG_RE.finditer(body):
            is_close = m.group(1) == "/"
            name = m.group(2).lower()
            if is_close:
                # закрываем верхний с таким именем
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j][0] == name:
                        stack.pop(j)
                        break
            else:
                stack.append((name, m.group(0)))
        # закрываем оставшиеся открытые в конце текущего чанка
        suffix = "".join(f"</{name}>" for name, _ in reversed(stack))
        result.append(prefix + body + suffix)
        carry_open = stack
    return result


def main_menu_kb() -> types.ReplyKeyboardMarkup:
    """Компактная reply-клавиатура. Всё остальное — через 📋 Меню."""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row("Записаться", "Мои записи")
    keyboard.row("📋 Меню", '🔮 "Оракул"')
    return keyboard


def masters_inline_kb() -> types.InlineKeyboardMarkup:
    """Кнопки выбора мастера."""
    keyboard = types.InlineKeyboardMarkup()
    for name in ALL_MASTERS:
        keyboard.add(types.InlineKeyboardButton(name, callback_data=f"master:{name}"))
    return keyboard


def get_state(user_id: int) -> dict[str, Any]:
    s = state_store.get(user_id)
    if not isinstance(s, dict):
        s = {}
    s.setdefault("step", STEP_IDLE)
    s.setdefault("master", None)
    # Принудительно валидируем типы — защита от битых данных в Redis
    if not isinstance(s.get("services"), list):
        s["services"] = []
    if not isinstance(s.get("services_raw"), str):
        s["services_raw"] = ""
    s.setdefault("datetime_text", None)
    s.setdefault("datetime_iso", None)
    s.setdefault("phone", None)
    if not isinstance(s.get("bookings"), list):
        s["bookings"] = []
    if not isinstance(s.get("off_topic_streak"), int):
        s["off_topic_streak"] = 0
    if not isinstance(s.get("total_off_topic"), int):
        s["total_off_topic"] = 0
    if not isinstance(s.get("transcript"), list):
        s["transcript"] = []
    if not isinstance(s.get("total_visits"), int):
        s["total_visits"] = 0
    if not isinstance(s.get("card"), dict):
        s["card"] = {"name": "", "notes": "", "birthday": "", "tags": []}
    else:
        # Защищаем поля карточки
        card = s["card"]
        card.setdefault("name", "")
        card.setdefault("notes", "")
        card.setdefault("birthday", "")
        if not isinstance(card.get("tags"), list):
            card["tags"] = []
    return s


def save_state(user_id: int, state: dict[str, Any]) -> None:
    state_store.set(user_id, state)


def reset_dialog(user_id: int) -> None:
    """Сбросить диалог записи, сохранив все «клиентские» данные."""
    prev = get_state(user_id) or {}
    state_store.set(user_id, {
        # Диалоговое — сбрасываем
        "step": STEP_IDLE,
        "master": None, "services": [], "services_raw": "",
        "datetime_text": None, "datetime_iso": None, "phone": None,
        "promo_code": None,
        "off_topic_streak": 0, "total_off_topic": 0, "transcript": [],
        # Клиентское — сохраняем
        "bookings": prev.get("bookings", []),
        "total_visits": prev.get("total_visits", 0),
        "referred_by": prev.get("referred_by"),
        "referral_used": prev.get("referral_used", False),
        "design_bonus_available": prev.get("design_bonus_available", 0),
        "referrals_brought_count": prev.get("referrals_brought_count", 0),
        "card": prev.get("card", {"name": "", "notes": "", "birthday": "", "tags": []}),
    })


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message) -> None:
    user_id = message.from_user.id

    # Парсим реф-параметр: /start ref_12345 — это пришёл новый клиент по приглашению
    args = (message.text or "").split(maxsplit=1)
    ref_arg = args[1].strip() if len(args) > 1 else ""
    referrer_id: int | None = None
    if ref_arg.startswith("ref_"):
        try:
            candidate = int(ref_arg[4:])
            if candidate != user_id:
                referrer_id = candidate
        except ValueError:
            pass

    # Deep link для прямого перехода к записи: /start book_Анна
    # Публикуется в Instagram bio, шапке Telegram-канала и т.п.
    if ref_arg.startswith("book_"):
        master_slug = ref_arg[5:].strip().lower()
        matched_master = next(
            (name for name, m in kb.masters.items()
             if name.lower() == master_slug
             or master_slug in [a.lower() for a in (m.aliases or [])]),
            None
        )
        if matched_master:
            reset_dialog(user_id)
            start_booking(message.chat.id, user_id,
                          prefilled=parsing.ParsedMessage(
                              intent=parsing.INTENT_BOOK,
                              master=matched_master,
                          ))
            return

    # Перед reset_dialog запоминаем — есть ли уже бронирования (если есть, реферал не применяется)
    prev = state_store.get(user_id) or {}
    is_new = not prev.get("bookings")

    reset_dialog(user_id)

    # Если новый клиент пришёл по реф-ссылке — записываем приглашающего
    if referrer_id and is_new and not prev.get("referred_by"):
        state = get_state(user_id)
        state["referred_by"] = referrer_id
        save_state(user_id, state)

        # Persist to long-term DB as well
        if db is not None:
            try:
                db.clients.update_referral_info(user_id, referred_by=referrer_id)
            except Exception as e:
                log.error("Failed to persist referred_by to long-term DB: %s", e)

        bot.send_message(
            message.chat.id,
            f"🎁 Ты пришла(шёл) по приглашению друга — "
            f"{kb.referral['new_client_offer_text']} (применится автоматически).",
        )

    # Персонализация: для возвращающегося клиента — приветствие по имени и статистика
    state = get_state(user_id)
    card = state.get("card") or {}
    name = (card.get("name") or "").split(" ")[0] if card.get("name") else ""
    total_visits = state.get("total_visits", 0)
    bookings = state.get("bookings") or []

    base_text = kb.msg("welcome") or "Привет!"

    # Промо-баннер: показываем активные промокоды из knowledge.md
    active_promos = [(code, info) for code, info in kb.promo_codes.items()
                     if info.get("valid")]
    if active_promos:
        promo_lines = []
        for code, info in active_promos[:3]:  # макс 3 чтобы не раздувать
            desc = info.get("description", "")
            if len(desc) > 100:
                desc = desc[:97] + "…"
            promo_lines.append(f"🎟 <b>{code}</b> — {desc}")
        base_text += "\n\n<b>Актуальные акции:</b>\n" + "\n".join(promo_lines)
    if name and total_visits >= 1:
        # Готовим хвост-сводку: «Ты у нас уже 5-й раз» + ближайшая запись если есть
        now = _now_moscow()
        upcoming = []
        for b in bookings:
            iso = b.get("datetime_iso")
            if not iso:
                continue
            try:
                dt = _parse_iso_aware(iso)
                if dt > now:
                    upcoming.append((dt, b))
            except Exception:
                pass
        upcoming.sort(key=lambda x: x[0])

        greeting = f"<b>Привет, {name}!</b> 👋\n"
        if upcoming:
            dt, b = upcoming[0]
            greeting += (f"\n📅 Ближайшая запись: <b>{_format_slot(dt)}</b> "
                          f"к {b.get('master', '—')}.\n")
        if total_visits >= 5:
            milestone = ""
            if total_visits in (5, 10, 20, 50):
                milestone = " 🎉"
            greeting += f"\n<i>Ты у нас уже {total_visits}-й раз{milestone}. Спасибо что возвращаешься!</i>\n"
        greeting += "\nЧто нужно?"
        bot.send_message(message.chat.id, greeting,
                          reply_markup=main_menu_kb(), parse_mode="HTML")
        return

    bot.send_message(message.chat.id, base_text,
                      reply_markup=main_menu_kb(), parse_mode="HTML")



def _send_invite(chat_id: int, user_id: int) -> None:
    """Внутренняя функция — отправляет реф-ссылку клиента."""
    if not kb.features.get("referrals", True):
        bot.send_message(chat_id, "Реферальная программа сейчас не активна.")
        return
    state = get_state(user_id)
    bot_username = ""
    try:
        bot_username = bot.get_me().username
    except Exception:
        pass
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}" if bot_username else f"start=ref_{user_id}"
    brought = state.get("referrals_brought_count", 0)
    bonuses = state.get("design_bonus_available", 0)
    pct = kb.referral.get("new_client_discount_pct", 10)
    program = kb.referral.get("program_name", "Подружка")
    text = (
        f"🤝 <b>Реферальная программа «{program}»</b>\n\n"
        "Ваша ссылка для приглашения:\n"
        f"<code>{ref_link}</code>\n\n"
        "Как это работает:\n"
        "• Друг переходит по ссылке и записывается\n"
        f"• Он получает <b>−{pct}% на первый визит</b>\n"
        f"• Вы получаете <b>{kb.referral['referrer_bonus_text']}</b> "
        "на следующую запись\n\n"
        f"📊 Приглашено друзей: <b>{brought}</b>\n"
        f"🎁 Доступно бонусов: <b>{bonuses}</b>"
    )
    bot.send_message(chat_id, text, parse_mode="HTML")


@bot.message_handler(commands=["invite", "пригласить"])
def cmd_invite(message: types.Message) -> None:
    """Выдаёт реферальную ссылку клиента + статус реф-программы."""
    _send_invite(message.chat.id, message.from_user.id)


RULES_VOICE_PATH = os.path.join(os.path.dirname(__file__), "assets", "rules_voice.ogg")


@bot.message_handler(commands=["правила", "rules"])
def cmd_rules(message: types.Message) -> None:
    bot.send_message(message.chat.id, kb.rules or "Правила пока не заполнены.", parse_mode="HTML")
    # Голосовая версия правил — отправляем после текста, если файл на месте
    if os.path.exists(RULES_VOICE_PATH):
        try:
            with open(RULES_VOICE_PATH, "rb") as f:
                bot.send_voice(message.chat.id, f,
                               caption="🎧 Голосовая версия правил")
        except Exception as e:
            log.warning("cmd_rules: не отправил голосовое: %s", e)


@bot.message_handler(commands=["privacy", "данные", "политика"])
def cmd_privacy(message: types.Message) -> None:
    """Краткая политика данных — без отдельного «пугающего» сайта."""
    text = kb.msg("privacy") or (
        "🔒 Данные используются только для записи и связи с мастером.\n"
        "Удалить: /delete_my_data · Правила: /rules"
    )
    # Дополняем секцией knowledge «Политика данных», если есть
    extra = ""
    try:
        from utils import _split_sections
        sections = _split_sections(kb.raw or "")
        extra = (sections.get("Политика данных") or "").strip()
    except Exception:
        pass
    if extra and extra not in text:
        text = text + "\n\n" + extra
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["delete_my_data", "удалить_данные"])
def cmd_delete_my_data(message: types.Message) -> None:
    """Запрос субъекта на удаление данных из бота (карточка + записи SQLite + state)."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton("✅ Да, удалить", callback_data="delme:yes"),
        types.InlineKeyboardButton("◀️ Отмена", callback_data="delme:no"),
    )
    bot.send_message(
        message.chat.id,
        "Удалить ваши данные из бота?\n\n"
        "• карточка клиента и история записей в CRM\n"
        "• состояние диалога\n"
        "• согласие и настройки\n\n"
        "<i>События в Google Calendar мастерам могут остаться — "
        "при необходимости напишите мастеру. Активные записи лучше отменить в /my заранее.</i>",
        reply_markup=markup,
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("delme:"))
def on_delete_my_data(call: types.CallbackQuery) -> None:
    user_id = call.from_user.id
    if call.data == "delme:no":
        bot.answer_callback_query(call.id, "Ок, ничего не удаляем")
        try:
            bot.edit_message_text(
                "Удаление отменено.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
        except Exception:
            pass
        return

    # delme:yes
    bot.answer_callback_query(call.id, "Удаляю…")
    stats = {"bookings": 0, "clients": 0}
    try:
        if db is not None:
            # Сначала снимем calendar events по будущим записям из state
            st = get_state(user_id)
            for b in list(st.get("bookings") or []):
                eid = b.get("calendar_event_id")
                master_name = b.get("master")
                if not eid or not master_name:
                    continue
                master = kb.masters.get(master_name)
                if not master or not master.calendar_id:
                    continue
                try:
                    from services.calendar import delete_event, get_service
                    svc = get_service()
                    if svc:
                        delete_event(svc, master.calendar_id, eid)
                except Exception as e:
                    log.warning("delete_my_data calendar: %s", e)
            stats = db.clients.delete_client_and_bookings(user_id)
    except Exception as e:
        log.exception("delete_my_data DB: %s", e)

    try:
        state_store.clear(user_id)
        if state_store.redis:
            try:
                state_store.redis.srem("nailbot:all_clients", user_id)
            except Exception:
                pass
            # psy counters
            try:
                state_store.redis.delete(f"nailbot:psy:user:{user_id}")
            except Exception:
                pass
    except Exception as e:
        log.warning("delete_my_data state: %s", e)

    try:
        bot.edit_message_text(
            f"✅ Данные удалены из бота.\n"
            f"Записей в CRM: {stats.get('bookings', 0)}, карточка: "
            f"{'удалена' if stats.get('clients') else 'не найдена'}.\n\n"
            "Можно снова /start, если захотите записаться.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
    except Exception:
        bot.send_message(call.message.chat.id, "✅ Данные удалены. /start — если снова понадобится бот.")


@bot.message_handler(commands=["мой_график", "schedule"])
def cmd_schedule(message: types.Message) -> None:
    lines = ["<b>График работы:</b>"]
    for name, m in kb.masters.items():
        block = [f"\n<b>{name}</b>", m.schedule or "(график не указан)"]
        if m.contact_phone:
            block.append(f"Тел.: {m.contact_phone}")
        if m.telegram_channel:
            block.append(f"Канал: {m.telegram_channel}")
        lines.append("\n".join(block))
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")


def _collect_top_reviews(limit: int = 3) -> list[str]:
    """Топ 5⭐ отзывов для /prices. SQLite (не full scan Redis) + cache 6ч."""
    cache_key = "nailbot:cache:top_reviews"
    if state_store.redis:
        try:
            cached = state_store.redis.get(cache_key)
            if cached:
                import json as _json
                if isinstance(cached, bytes):
                    cached = cached.decode()
                return _json.loads(cached)
        except Exception:
            pass

    reviews: list[tuple[str, str]] = []
    # Источник — long-term DB (не грузим всех клиентов из Redis)
    if db is not None:
        try:
            rows = db.bookings.get_five_star_reviews(limit=max(limit * 3, 10))
            for b in rows:
                text = (b.get("review_text") or "").strip()
                if not (20 <= len(text) <= 200):
                    continue
                raw_name = (b.get("client_name") or "").strip()
                initial = (raw_name[:1].upper() + ".") if raw_name else "К."
                reviews.append((initial, text))
        except Exception as e:
            log.debug("_collect_top_reviews DB: %s", e)

    # Берём последние (свежие) отзывы
    selected = reviews[-limit:]
    formatted = [f"«{t}» — {n}, 5⭐" for n, t in selected]

    # Кэшируем
    if state_store.redis:
        try:
            import json as _json
            state_store.redis.setex(cache_key, 60 * 60 * 6, _json.dumps(formatted))
        except Exception:
            pass
    return formatted


@bot.message_handler(commands=["цены", "prices"])
def cmd_prices(message: types.Message) -> None:
    from services.catalog import overlay_services, preferred_master_name
    live = overlay_services(kb.services, preferred_master_name(kb), kb=kb)
    if not live:
        bot.send_message(message.chat.id, "Прайс пока не загружен.")
        return
    # Группируем по категориям для читаемости
    from collections import defaultdict
    by_cat: dict[str, list] = defaultdict(list)
    cat_order: list[str] = []
    for s in live:
        if s.category not in by_cat:
            cat_order.append(s.category)
        by_cat[s.category].append(s)
    icons = CATEGORY_ICONS
    parts = [f"<b>Прайс-лист {kb.brand.get('name', 'Salon')}</b>\n"]

    # Топ-3 отзыва в шапке — социальное доказательство
    try:
        top_reviews = _collect_top_reviews(limit=3)
    except Exception:
        top_reviews = []
    if top_reviews:
        parts.append("<i>💬 Что говорят клиенты:</i>")
        for r in top_reviews:
            parts.append(f"<i>{r}</i>")
        parts.append("")

    for cat in cat_order:
        icon = icons.get(cat, "📋")
        parts.append(f"\n<b>{icon} {cat}</b>")
        for s in by_cat[cat]:
            parts.append(f"• {s.name} — <b>{s.price}</b> ({s.duration})")
    safe_send_long(message.chat.id, "\n".join(parts), parse_mode="HTML")


@bot.message_handler(commands=["мои_записи", "my"])
def cmd_my_bookings(message: types.Message) -> None:
    handle_my_bookings(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["reviews", "отзывы"])
def cmd_reviews(message: types.Message) -> None:
    """Показывает только 5-звёздочные отзывы клиентов — для всех."""
    if db is None:
        bot.send_message(message.chat.id, "База отзывов недоступна.")
        return
    reviews = db.bookings.get_five_star_reviews(limit=10)
    if not reviews:
        bot.send_message(
            message.chat.id,
            f"⭐⭐⭐⭐⭐ Отзывы пока копятся — первый может быть твоим! 😊\n\n"
            f"После каждого визита бот попросит тебя оценить работу мастера.",
        )
        return

    lines = [f"⭐⭐⭐⭐⭐ <b>Отзывы о {kb.brand.get('name', 'студии')}</b>\n"]
    import datetime as _dt
    for r in reviews:
        master = r.get("master", "")
        text = (r.get("review_text") or "").strip()
        iso = r.get("datetime_iso") or ""
        # Имя: берём первую букву + точку для приватности, если есть
        raw_name = (r.get("client_name") or "").strip()
        display_name = (raw_name.split()[0] + " " + raw_name.split()[1][0] + "."
                        if len(raw_name.split()) >= 2 else raw_name or "Клиент")
        # Дата
        try:
            dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            date_str = dt.strftime("%-d %B").replace(
                "January","января").replace("February","февраля").replace("March","марта"
                ).replace("April","апреля").replace("May","мая").replace("June","июня"
                ).replace("July","июля").replace("August","августа").replace("September","сентября"
                ).replace("October","октября").replace("November","ноября").replace("December","декабря")
        except Exception:
            date_str = ""
        meta = f"<i>{display_name}" + (f" — {master}" if master else "") + (f", {date_str}" if date_str else "") + "</i>"
        lines.append(f"«{text}»\n{meta}\n")
    safe_send_long(message.chat.id, "\n".join(lines), parse_mode="HTML")


@bot.message_handler(commands=["сертификаты", "certificates"])
def cmd_certificates(message: types.Message) -> None:
    text = kb.certificates or "Информация о сертификатах пока не загружена."
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["masters", "мастера"])
def cmd_masters(message: types.Message) -> None:
    """Описания всех мастеров — кто чем занимается, контакты, расписание."""
    if not kb.masters:
        bot.send_message(message.chat.id, "Информация о мастерах пока не загружена.")
        return
    parts = ["<b>Наши мастера</b>\n"]
    for name, m in kb.masters.items():
        block = [f"\n👤 <b>{name}</b>"]
        if m.description:
            block.append(m.description)
        if m.speciality:
            block.append(f"<i>Делает: {m.speciality}</i>")
        block.append(f"Часы работы: {m.schedule.splitlines()[0] if m.schedule else '—'}")
        if m.contact_phone:
            block.append(f"📞 {m.contact_phone}")
        if m.telegram_channel:
            block.append(f"📣 Канал: {m.telegram_channel}")
        parts.append("\n".join(block))
    safe_send_long(message.chat.id, "\n".join(parts), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Категоризованное меню (inline) — на смену перегруженному slash-меню
# ---------------------------------------------------------------------------

# Меню — вынесено в handlers/menu.py.
# Регистрация декораторов cmd_menu и on_menu_callback там же.



def _send_help(chat_id: int, user_id: int) -> None:
    """Внутренняя функция — выводит /help для конкретного user_id.
    Используется и хендлером команды, и из меню (где from_user был бы ботом)."""
    is_master = bool(_master_by_telegram_id(user_id))
    is_admin = user_id in config.admin_ids

    client_block = (
        "📋 <b>Что я умею</b>\n\n"
        "<b>Запись:</b>\n"
        "• <b>Записаться</b> или /start — выбрать мастера, услуги, дату\n"
        "• <b>Мои записи</b> /my — список + кнопки перенести/отменить\n"
        "• <b>Цены</b> /prices — прайс-лист\n"
        "• <b>График</b> /schedule — когда работают мастера\n"
        "• <b>Сертификаты</b> /certificates — подарочные сертификаты\n"
        "• <b>Правила</b> /rules — правила салона\n"
        "• /privacy — как используем данные\n\n"
        "<b>Бонусы и приглашения:</b>\n"
        "• /invite — твоя реферальная ссылка. Друг записывается → у тебя бонус (бесплатный дизайн)\n\n"
        "<b>Прочее:</b>\n"
        "• 🪞 <b>Оракул</b> — AI-ассистент для саморефлексии (3 личности)\n"
        "• /delete_my_data — удалить мои данные из бота\n"
        "• /delete_psy_data — удалить только данные «Оракула»\n"
        "• /cancel — сбросить текущий диалог\n\n"
        "<b>Записаться можно живым текстом:</b> «хочу к Анне на маникюр завтра в 14:00, +79001000001»"
    )

    master_block = (
        "\n\n👤 <b>Команды мастера</b>\n"
        "• /today, /tomorrow, /week — своё расписание\n"
        "• /salon_today, /salon_week — расписание всего салона\n"
        "• /clients — список клиентов салона\n"
        "• /client &lt;имя/телефон&gt; — найти клиента\n"
        "• /income — твой заработок за периоды\n"
        "• /block &lt;дата время&gt; — заблокировать слот (обед, личное)\n"
        "• /add_booking телефон | имя | когда | услуги — записать клиента по телефону\n"
        "• /llm_usage — расход AI за сегодня"
    )

    admin_block = (
        "\n\n🛡 <b>Команды админа</b>\n"
        "• /stats — статистика записей\n"
        "• /psy_stats — статистика «Оракула»\n"
        "• /broadcast — массовая рассылка клиентам\n"
        "• /backup — ручной бэкап Redis+SQLite в TG\n"
        "  (авто 21:00 МСК только в дни с записями)\n"
        "• /reload — перечитать knowledge.md (после правки на GitHub)\n"
        "• /clear_reminders — очистить очередь напоминаний\n"
        "• /digest_now — вручную отправить утренний дайджест мастерам\n"
        "• /digest_evening — вручную отправить вечерний дайджест мастерам\n"
        "• /sync_calendar [дней] [verbose] — ручной синк (авто каждые 30 мин)\n"
        "• /sentry_test — проверить ловлю ошибок\n"
        "• /db_backfill_from_redis [audit] — починить DB-историю из Redis (одноразово)"
    )

    text = client_block
    if is_master:
        text += master_block
    if is_admin:
        text += admin_block
    safe_send_long(chat_id, text, parse_mode="HTML")


@bot.message_handler(commands=["help", "помощь"])
def cmd_help(message: types.Message) -> None:
    """Обзор возможностей — разный для клиента, мастера, админа."""
    _send_help(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["отмена", "cancel"])
def cmd_cancel(message: types.Message) -> None:
    reset_dialog(message.from_user.id)
    bot.send_message(message.chat.id, "Диалог сброшен. Чем могу помочь?", reply_markup=main_menu_kb())


# Clear-reminders — вынесен в handlers/admin.py. cmd_clear_reminders переустанавливается в register().
def cmd_clear_reminders(message) -> None:  # pragma: no cover
    log.error('cmd_clear_reminders вызван до register()')



# ---------------------------------------------------------------------------
# Трекинг расхода Groq — вынесено в handlers/stats.py.
# llm.usage_callback подключается в _register_extracted_handlers().
# ---------------------------------------------------------------------------

def _track_groq_call(usage):  # pragma: no cover
    pass  # реальная функция выставляется в register()


# ---------------------------------------------------------------------------
# Фича «Оракул» (psychologist) — код вынесен в handlers/oracle.py
# handle_psy_button и continue_psy_dialog заполняются в run() при register().
# До регистрации это стабы — но первое сообщение в боте приходит уже после
# инициализации, так что NameError тут не случится.
# ---------------------------------------------------------------------------

def handle_psy_button(chat_id: int, user_id: int) -> None:  # pragma: no cover
    log.error("handle_psy_button вызван до register() — оракул не доступен")

def continue_psy_dialog(message, state) -> None:  # pragma: no cover
    log.error("continue_psy_dialog вызван до register() — оракул не доступен")



# ---------------------------------------------------------------------------
# Карточки клиентов — для мастеров
# ---------------------------------------------------------------------------

# Карточки клиентов — вынесено в handlers/clients.py.
# Стаб только для _handle_card_edit_input — он зовётся из _on_text_inner.
def _handle_card_edit_input(message, pending) -> None:  # pragma: no cover
    log.error('_handle_card_edit_input вызван до register()')



# ---------------------------------------------------------------------------
# Команды для мастеров: /today, /tomorrow, /week
# ---------------------------------------------------------------------------

def _master_by_telegram_id(tg_id: int) -> Any:
    """Найти мастера по его telegram_id. Возвращает Master или None.

    Поддерживает только числовой telegram_id в knowledge.md.
    @username не поддерживается (Telegram API не отдаёт username в from_user
    callback'ов надёжно, плюс пользователь может его сменить — числовой ID
    стабилен). Если в knowledge.md задан @username — мастер не сможет
    пользоваться командами; лог-ворнинг подскажет.
    """
    for m in kb.masters.values():
        normalized = _normalize_tg_id(m.telegram_id)
        if not normalized:
            continue
        if normalized.startswith("@"):
            # Безопасно отметим в логах, но не сравниваем — это ошибка конфига
            continue
        if normalized == str(tg_id):
            return m
    return None


def _fetch_events_for_range(master, start: _dt.datetime, end: _dt.datetime) -> list[dict]:
    """Получить события мастера за период. Shared-календарь: только события этого мастера."""
    if not master.calendar_id or "заполнить" in (master.calendar_id or "").lower():
        return []
    svc = _calendar_service()
    if not svc:
        return []
    try:
        from services.calendar import event_belongs_to_master, list_events
        items = list_events(svc, master.calendar_id, start, end)
        return [e for e in items if event_belongs_to_master(e, master.name)]
    except Exception as e:
        log.warning("master_events: ошибка получения событий: %s", e)
        return []


# Today/tomorrow — вынесено в handlers/master.py. cmd_* регистрируются в register().
def cmd_today(message) -> None:  # pragma: no cover
    log.error('cmd_today вызван до register()')

def cmd_tomorrow(message) -> None:  # pragma: no cover
    log.error('cmd_tomorrow вызван до register()')



# Vacation/sick — вынесено в handlers/vacation_sick.py.
# get_vacation_dates переустанавливается в register() — нужна booking flow.
def _get_vacation_dates(master_name):  # pragma: no cover
    return set()

def cmd_sick(message) -> None:  # pragma: no cover
    log.error('cmd_sick вызван до register()')



# Income, block, week, salon — вынесено в handlers/master.py. cmd_* регистрируются в register().
def cmd_income(message) -> None:  # pragma: no cover
    log.error('cmd_income вызван до register()')

def cmd_week(message) -> None:  # pragma: no cover
    log.error('cmd_week вызван до register()')

def cmd_salon_today(message) -> None:  # pragma: no cover
    log.error('cmd_salon_today вызван до register()')

def cmd_salon_week(message) -> None:  # pragma: no cover
    log.error('cmd_salon_week вызван до register()')



# Broadcast — вынесен в handlers/admin.py. cmd_broadcast_start переустанавливается в register().
def cmd_broadcast_start(message) -> None:  # pragma: no cover
    log.error('cmd_broadcast_start вызван до register()')


# cmd_menu — вынесено в handlers/menu.py. Стаб для строки "📋 Меню" из on_text.
def cmd_menu(message) -> None:  # pragma: no cover
    log.error('cmd_menu вызван до register()')


# Стабы для команд, которые меню вызывает через late-binding к __main__.
# Реальные функции выставляются в _register_extracted_handlers().
def cmd_clients_list(message) -> None:  # pragma: no cover
    log.error('cmd_clients_list вызван до register()')


def cmd_psy_stats(message) -> None:  # pragma: no cover
    log.error('cmd_psy_stats вызван до register()')

def cmd_llm_usage(message) -> None:  # pragma: no cover
    log.error('cmd_llm_usage вызван до register()')



# cmd_stats — вынесено в handlers/stats.py. Переустанавливается в register().
def cmd_stats(message) -> None:  # pragma: no cover
    log.error('cmd_stats вызван до register()')



@bot.message_handler(commands=["reload", "перезагрузить"])
def cmd_reload(message: types.Message) -> None:
    """Админская команда: перечитать knowledge.md без рестарта процесса."""
    if message.chat.id not in config.admin_ids:
        return  # молча игнорируем
    # ВАЖНО: МУТИРУЕМ существующий kb, а не пересоздаём.
    # Handler-модули захватили ссылку на этот объект в register-time через ctx.kb.
    # Если перебиндить main.kb на новый объект — подмодули продолжат видеть старый.
    # Также обновляем зависимые константы (ALL_MASTERS, FIXED_SLOTS_HOURS и т.п.).
    global ALL_MASTERS, MIN_HOURS_BEFORE_BOOKING, SLOT_DURATION_MIN
    global FIXED_SLOTS_HOURS, DEFAULT_SERVICE_DURATION_MIN
    global DATE_PICKER_MAX_DAYS, REVIEW_DELAY_MINUTES_AFTER_END, CATEGORY_ICONS
    global OFF_TOPIC_STRIKE_LIMIT, OFF_TOPIC_SESSION_LIMIT
    global PSY_MAX_SESSIONS_PER_DAY, PSY_MAX_TURNS
    global GROQ_DAILY_REQUEST_LIMIT, GROQ_DAILY_TOKEN_LIMIT
    try:
        new_kb = load_knowledge(config.knowledge_path)
        kb.replace_from(new_kb)
        # Переcчитываем константы, которые зависят от kb.settings/categories.
        ALL_MASTERS = tuple(kb.masters.keys())
        MIN_HOURS_BEFORE_BOOKING = kb.settings["min_hours_before_booking"]
        SLOT_DURATION_MIN = kb.settings["slot_duration_min"]
        FIXED_SLOTS_HOURS = list(kb.settings["fixed_slot_hours"])
        DEFAULT_SERVICE_DURATION_MIN = kb.settings["default_service_duration_min"]
        DATE_PICKER_MAX_DAYS = kb.settings["date_picker_max_days"]
        REVIEW_DELAY_MINUTES_AFTER_END = kb.settings["review_delay_minutes_after_end"]
        OFF_TOPIC_STRIKE_LIMIT = kb.settings["off_topic_strike_limit"]
        OFF_TOPIC_SESSION_LIMIT = kb.settings["off_topic_session_limit"]
        PSY_MAX_SESSIONS_PER_DAY = kb.settings["psy_max_sessions_per_day"]
        PSY_MAX_TURNS = kb.settings["psy_max_turns"]
        GROQ_DAILY_REQUEST_LIMIT = kb.settings["llm_daily_request_limit"]
        GROQ_DAILY_TOKEN_LIMIT = kb.settings["llm_daily_token_limit"]
        CATEGORY_ICONS = dict(kb.categories)
        try:
            from database.pg import pg_enabled
            from database.appointments import seed_from_knowledge
            if pg_enabled():
                seed_from_knowledge(kb)
        except Exception as e:
            log.warning("reload seed: %s", e)
        bot.send_message(
            message.chat.id,
            f"✅ knowledge.md перечитан: мастеров={len(kb.masters)}, "
            f"услуг={len(kb.services)}, сообщений={len(kb.messages)}",
        )
    except Exception as e:
        log.exception("cmd_reload: %s", e)
        bot.send_message(message.chat.id, f"❌ Не удалось перечитать: {e}")


@bot.message_handler(commands=["digest_now"])
def cmd_digest_now(message: types.Message) -> None:
    """Админская команда: немедленно запустить утренний дайджест для мастеров (тест)."""
    if message.chat.id not in config.admin_ids:
        return
    bot.send_message(message.chat.id, "⏳ Запускаю утренний дайджест...")
    try:
        _morning_digest()
        bot.send_message(message.chat.id, "✅ Дайджест отправлен мастерам.")
    except Exception as e:
        log.exception("cmd_digest_now: %s", e)
        bot.send_message(message.chat.id, f"❌ Ошибка при отправке дайджеста: {e}")


@bot.message_handler(commands=["digest_evening"])
def cmd_digest_evening(message: types.Message) -> None:
    """Админская команда: немедленно запустить вечерний дайджест (превью завтра) для мастеров (тест)."""
    if message.chat.id not in config.admin_ids:
        return
    bot.send_message(message.chat.id, "⏳ Запускаю вечерний дайджест (превью завтра)...")
    try:
        _evening_digest()
        bot.send_message(message.chat.id, "✅ Вечерний дайджест отправлен мастерам.")
    except Exception as e:
        log.exception("cmd_digest_evening: %s", e)
        bot.send_message(message.chat.id, f"❌ Ошибка при отправке вечернего дайджеста: {e}")


# ---------------------------------------------------------------------------
# Свободный текст — главный обработчик
# ---------------------------------------------------------------------------

# Phone helpers + on_phone_confirm + on_contact — вынесено в handlers/booking.py.

# Booking flow + handle_my_bookings + notify_master — вынесено в handlers/booking.py.
# Заполняются в _register_extracted_handlers().
def start_booking(chat_id, user_id, prefilled=None):  # pragma: no cover
    log.error('start_booking called before register')

def continue_booking(message, state):  # pragma: no cover
    log.error('continue_booking called before register')

def handle_my_bookings(chat_id, user_id):  # pragma: no cover
    log.error('handle_my_bookings called before register')

def notify_master(client_user_id, booking, outcome='booked', transcript=None):  # pragma: no cover
    log.error('notify_master called before register')

# Reminders/Review — реальные импл в handlers/booking.py.
# Шимы в main.py остаются для backward-compat: APScheduler хранит ссылки на
# __main__:_send_reminder и __main__:_send_review_prompt в Redis JobStore.

_send_reminder_impl = None  # заполняется в _register_extracted_handlers()
_send_review_prompt_impl = None

def _send_reminder(chat_id, booking, hours_before):
    if _send_reminder_impl is not None:
        _send_reminder_impl(chat_id, booking, hours_before)
    else:
        log.error('_send_reminder called before register')

def _send_review_prompt(chat_id, booking):
    if _send_review_prompt_impl is not None:
        _send_review_prompt_impl(chat_id, booking)
    else:
        log.error('_send_review_prompt called before register')







def _is_non_command_text(m) -> bool:
    """Фильтр для on_text: матчит ЛЮБОЙ текст кроме slash-команд.

    Критично после рефакторинга: handlers/* регистрируют свои /command
    хендлеры через _register_extracted_handlers(), то есть ПОСЛЕ on_text.
    telebot отдаёт сообщение первому матчающему хендлеру в порядке
    регистрации. Без этого фильтра on_text перехватывал бы /menu, /today,
    /stats и все остальные вынесенные команды до того как они смогли бы
    отработать.
    """
    text = (m.text or "").strip()
    return not text.startswith("/")


@bot.message_handler(func=_is_non_command_text, content_types=["text"])
def on_text(message: types.Message) -> None:
    """Главный обработчик текстов. Все исключения внутри ловим — иначе один битый
    запрос отравил бы поток обработки webhook'а и сломал бы бота для всех."""
    try:
        _on_text_inner(message)
    except Exception as e:
        log.exception("on_text crashed: %s", e)
        try:
            bot.send_message(
                message.chat.id,
                "Что-то пошло не так. Попробуй ещё раз или /start.",
            )
        except Exception:
            pass


def _on_text_inner(message: types.Message) -> None:
    # Бот спроектирован для приватных чатов 1-на-1. В группах/каналах не работаем.
    if message.chat.type != "private":
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    text = (message.text or "").strip()

    if not text:
        return  # пустое/whitespace-only сообщение — игнорируем

    # Правка прайса мастером (кнопка Цена/Минуты) — раньше общего «записаться».
    try:
        fn = globals().get("handle_price_edit_text")
        if callable(fn) and fn(message):
            return
    except Exception as e:
        log.warning("price edit intercept: %s", e)

    if not rate_limiter.allow(user_id):
        log.info("RateLimit hit user_id=%s", user_id)
        return  # молча игнорируем

    # Авто-захват имени из Telegram при первом сообщении (если карточка пустая)
    state_for_name = state_store.get(user_id) or {}
    card_for_name = state_for_name.get("card") or {}
    if not card_for_name.get("name") and message.from_user.first_name:
        fn = (message.from_user.first_name or "").strip()
        ln = (message.from_user.last_name or "").strip()
        captured = (fn + (f" {ln}" if ln else "")).strip()
        if captured:
            card_for_name["name"] = captured[:80]
            state_for_name["card"] = card_for_name
            state_store.set(user_id, state_for_name)

    # Мастер редактирует карточку клиента — перехватываем ввод
    master_check = _master_by_telegram_id(user_id)
    if master_check:
        pending = (state_store.get(user_id) or {}).get("pending_card_edit")
        if pending:
            _handle_card_edit_input(message, pending)
            return

    # Кнопки главного меню
    if text == "Записаться":
        return start_booking(message.chat.id, user_id)
    if text == "Мои записи":
        return handle_my_bookings(message.chat.id, user_id)
    if text == "Правила":
        return cmd_rules(message)
    if text == "Цены":
        return cmd_prices(message)
    if text == "График":
        return cmd_schedule(message)
    if text == "Сертификаты":
        return cmd_certificates(message)
    if text == '🔮 "Оракул"':
        return handle_psy_button(message.chat.id, user_id)
    if text == "📋 Меню":
        return cmd_menu(message)

    state = get_state(user_id)
    # Сохраняем последние реплики клиента (нужно для LLM-саммари мастеру).
    transcript = state.get("transcript", [])
    transcript.append(text[:200])
    state["transcript"] = transcript[-10:]

    step = state["step"]


    # Админ вводит текст рассылки
    if step == STEP_BROADCAST_TEXT and user_id in config.admin_ids:
        state["pending_broadcast"] = {"text": text}
        state["step"] = STEP_IDLE
        save_state(user_id, state)
        count = len(state_store.all_users())
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(
            f"✅ Разослать ~{count} клиентам", callback_data="br:send"))
        markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="br:cancel"))
        bot.send_message(
            message.chat.id,
            f"📣 <b>Превью рассылки</b>\n\n────────────\n{text}\n────────────\n\n"
            f"<i>Получателей в индексе: {count}.</i>",
            reply_markup=markup, parse_mode="HTML",
        )
        return

    # Отдельный шаг — клиент в активной сессии «Помощник»
    if step == STEP_PSY_DIALOG:
        continue_psy_dialog(message, state)
        return

    # Отдельный шаг — клиент пишет текстовый отзыв после визита
    if step == STEP_AWAITING_REVIEW:
        bk_id = state.get("review_for_booking")
        if text.lower().strip() in ("отмена", "не надо", "забей"):
            state["step"] = STEP_IDLE
            state["review_for_booking"] = None
            save_state(user_id, state)
            bot.send_message(message.chat.id, "Хорошо, без отзыва. Спасибо что были у нас!")
            return
        target = next((b for b in state.get("bookings", []) if b.get("id") == bk_id), None)
        if target:
            target["review_text"] = text[:2000]
            notify_master(user_id, target, outcome="review_text", transcript=[text[:2000]])

            # Persist review text to long-term DB
            if db is not None:
                try:
                    bk_id = state.get("review_for_booking")
                    if bk_id:
                        db.bookings.update_rating_and_review(bk_id, review_text=text[:2000])
                except Exception as e:
                    log.error("Failed to save review text to long-term DB: %s", e)

        state["step"] = STEP_IDLE
        state["review_for_booking"] = None
        save_state(user_id, state)
        bot.send_message(message.chat.id, "🙏 Спасибо! Ваш отзыв передан мастеру.")
        return

    # Если мы в середине диалога — продолжаем по шагу
    if step != STEP_IDLE:
        save_state(user_id, state)
        return continue_booking(message, state)

    # Парсим намерение
    parsed = parsing.parse(text, kb)

    # Если парсер не уверен — пробуем LLM, но только в пределах лимитов:
    # 1) фича-флаг kb.features.llm_parser должен быть True
    # 2) не более OFF_TOPIC_SESSION_LIMIT болтовни за сессию,
    # 3) не более LLMUsageLimiter.max_calls вызовов без завершённой записи.
    if (parsed.intent == parsing.INTENT_UNKNOWN
            and llm.enabled
            and kb.features.get("llm_parser", True)):
        too_chatty = state.get("total_off_topic", 0) >= OFF_TOPIC_SESSION_LIMIT
        if too_chatty:
            log.info("Слишком много болтовни у user_id=%s, LLM пропускаем", user_id)
            parsed.intent = "off_topic"
        elif llm_usage.can_call(user_id):
            llm_usage.register_call(user_id)
            llm_result = llm.parse(text)
            if llm_result:
                parsed = _merge_llm_into_parsed(parsed, llm_result)
        else:
            log.info("LLM-лимит исчерпан для user_id=%s, не вызываем", user_id)
            parsed.intent = "off_topic"

    # Обновляем счётчики болтовни
    if _is_off_topic(parsed):
        state["off_topic_streak"] = state.get("off_topic_streak", 0) + 1
        state["total_off_topic"] = state.get("total_off_topic", 0) + 1
    else:
        state["off_topic_streak"] = 0
        # total_off_topic не сбрасываем — это накопленный счётчик за сессию
    save_state(user_id, state)

    if parsed.intent == parsing.INTENT_HELLO:
        reply(message.chat.id, "welcome")
        return
    if parsed.intent == parsing.INTENT_PRICES:
        return cmd_prices(message)
    if parsed.intent == parsing.INTENT_RULES:
        return cmd_rules(message)
    if parsed.intent == parsing.INTENT_SCHEDULE:
        return cmd_schedule(message)
    if parsed.intent == parsing.INTENT_MY_BOOKINGS:
        return handle_my_bookings(message.chat.id, user_id)
    if parsed.intent == parsing.INTENT_BOOK:
        return start_booking(message.chat.id, user_id, prefilled=parsed)

    # Off-topic / unknown — с эскалацией настойчивости
    _handle_off_topic(message.chat.id, state["off_topic_streak"])


def _is_off_topic(parsed: parsing.ParsedMessage) -> bool:
    return parsed.intent in ("unknown", "off_topic")


_CANCEL_INSIDE_DIALOG = re.compile(
    r"\b(отмена|отмени(?:ть)?|стоп|передумал[аи]?|"
    r"не\s+надо|не\s+хочу|забей|отбой|хватит|cancel)\b",
    re.IGNORECASE,
)


def _is_cancel_inside_dialog(text: str) -> bool:
    """Строгая проверка отмены внутри диалога: только целые слова/фразы."""
    return bool(_CANCEL_INSIDE_DIALOG.search(text))


def _handle_off_topic(chat_id: int, streak: int) -> None:
    """
    Реакция на болтовню: первый раз — мягко из knowledge.md,
    дальше — настойчивее, с подсказкой кнопок.
    """
    if streak <= OFF_TOPIC_STRIKE_LIMIT:
        reply(chat_id, "off_topic")
        return
    bot.send_message(
        chat_id,
        "Давайте вернёмся к делу. Я могу помочь только с записью, ценами, услугами, "
        "правилами, графиком работы и адресом салона. Выберите действие на клавиатуре.",
        reply_markup=main_menu_kb(),
    )


def _merge_llm_into_parsed(parsed: parsing.ParsedMessage,
                            llm_data: dict[str, Any]) -> parsing.ParsedMessage:
    """Аккуратно перенести то, что нашёл LLM, в ParsedMessage."""
    intent = llm_data.get("intent")
    if intent in ("book", "cancel", "my", "prices", "rules",
                  "schedule", "hello", "off_topic"):
        parsed.intent = intent
    if not parsed.master and llm_data.get("master") in ALL_MASTERS:
        parsed.master = llm_data["master"]
    if not parsed.services and isinstance(llm_data.get("services"), list):
        parsed.services = [str(x) for x in llm_data["services"] if x]
    if not parsed.datetime_text and llm_data.get("datetime_text"):
        parsed.datetime_text = str(llm_data["datetime_text"])
    if not parsed.phone and llm_data.get("phone"):
        parsed.phone = str(llm_data["phone"])
    return parsed














def _normalize_tg_id(raw: str) -> str:
    """Очистить telegram_id от заглушек/мусора. Возвращает строку с числовым ID или ''."""
    if not raw:
        return ""
    cleaned = raw.strip().strip("*()").strip()
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if "заполнить" in lower or lower in ("none", "null", "tbd", "—", "-"):
        return ""
    # Допускаем как числовой ID, так и @username
    if cleaned.startswith("@") and len(cleaned) > 1:
        return cleaned
    digits = cleaned.lstrip("-")
    if digits.isdigit():
        return cleaned
    return ""


# ---------------------------------------------------------------------------
# Inline-кнопки (выбор мастера)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Date / Time picker
# ---------------------------------------------------------------------------

# Date/Time pickers + slot helpers — вынесено в services/slots.py.
# Реальные функции выставляются в _register_extracted_handlers().
def _available_dates(master):  # pragma: no cover
    return []

def _date_picker_kb(master):  # pragma: no cover
    return types.InlineKeyboardMarkup()

def _time_picker_kb(date_iso, master, services):  # pragma: no cover
    return ('Slots не зарегистрированы', types.InlineKeyboardMarkup())





# ---------------------------------------------------------------------------
# Inline-кнопки (выбор мастера)
# ---------------------------------------------------------------------------

# Master callbacks — вынесено в handlers/master.py.
# on_master_cancel и on_master_unblock регистрируются в register().





















# ---------------------------------------------------------------------------
# «Мои записи»
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Заглушки на будущее
# ---------------------------------------------------------------------------



MOSCOW_TZ = None  # инициализируется лениво в _moscow_tz()


def _moscow_tz():
    """Лениво создаём tzinfo для Москвы. Если zoneinfo недоступен — UTC+3 fallback."""
    global MOSCOW_TZ
    if MOSCOW_TZ is not None:
        return MOSCOW_TZ
    try:
        from zoneinfo import ZoneInfo
        MOSCOW_TZ = ZoneInfo("Europe/Moscow")
    except Exception:
        MOSCOW_TZ = _dt.timezone(_dt.timedelta(hours=3))
    return MOSCOW_TZ


def _now_moscow() -> _dt.datetime:
    return _dt.datetime.now(tz=_moscow_tz())


def _parse_iso_aware(iso: str) -> _dt.datetime:
    """ISO-строка → aware datetime. Если в строке нет TZ — считаем Москву.
    Защищает от TypeError при сравнении со старыми booking без TZ."""
    dt = _dt.datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_moscow_tz())
    return dt












# Google Calendar — тонкие алиасы к services/calendar.py
# (Историческое именование с подчёркиванием сохранили чтобы не менять 30+ call-sites.)
from services.calendar import (  # noqa: E402
    get_service as _calendar_service,
)
# get_busy_intervals и slot_is_free используются ТОЛЬКО в services/slots.py и
# handlers/booking.py через прямой импорт. В main не нужны.








# Slots/time helpers — вынесено в services/slots.py.
# Реальные функции выставляются в _register_extracted_handlers().
def _format_slot(dt):  # pragma: no cover
    return dt.isoformat()

def _total_duration_minutes(service_names):  # pragma: no cover
    return DEFAULT_SERVICE_DURATION_MIN

def resolve_booking_time(text, master, services=None, transcript=None, preparsed_dt=None):  # pragma: no cover
    return {'status': 'unparseable', 'message': 'Slots не зарегистрированы',
            'datetime': None, 'datetime_text': None, 'duration_min': 0, 'suggestions': []}



# ---------------------------------------------------------------------------
# Google Calendar — создание события
# ---------------------------------------------------------------------------




# _backup_state_to_admin — задача APScheduler (cron).
# APScheduler с Redis JobStore пиклит ссылку как `__main__:_backup_state_to_admin`.
# Имена остаются МОДУЛЬ-УРОВНЕВЫМИ shim'ами, реальная реализация в _impl.

_backup_state_to_admin_impl = None
_morning_digest_impl = None
_evening_digest_impl = None
_sync_calendar_cron_impl = None


def _backup_state_to_admin(force: bool = False) -> None:
    """Cron 21:00 МСК (только если есть записи на сегодня) + ручной /backup(force=True)."""
    if _backup_state_to_admin_impl is None:
        log.error('_backup_state_to_admin: impl не зарегистрирован')
        return
    _backup_state_to_admin_impl(force=force)

def _morning_digest() -> None:
    """Cron-target 9:00 МСК. Рассылает каждому мастеру расписание на сегодня."""
    if _morning_digest_impl is None:
        log.error('_morning_digest: impl не зарегистрирован')
        return
    _morning_digest_impl()

def _evening_digest() -> None:
    """Cron-target 20:00 МСК. Рассылает каждому мастеру превью завтрашнего расписания."""
    if _evening_digest_impl is None:
        log.error('_evening_digest: impl не зарегистрирован')
        return
    _evening_digest_impl()


def _sync_calendar_cron() -> None:
    """Cron-target каждые 30 минут. Автосинк ручных записей из Google Calendar.
    Тихий — уведомляет админов только если что-то нашёл или упал."""
    if _sync_calendar_cron_impl is None:
        log.error('_sync_calendar_cron: impl не зарегистрирован')
        return
    _sync_calendar_cron_impl()


def _check_idle_clients() -> None:
    """
    Раз в неделю (по понедельникам в 12:00 МСК) проверяем всех клиентов:
    если последний визит был > IDLE_CLIENT_THRESHOLD_DAYS дней назад
    и за последние IDLE_MSG_COOLDOWN_DAYS не отправляли — шлём сообщение.
    """
    now = _now_moscow()
    user_ids = state_store.all_users()
    log.info("idle_check: запускаю проверку для %d клиентов", len(user_ids))
    sent = 0
    for uid in user_ids:
        try:
            s = state_store.get(uid) or {}
            bookings = s.get("bookings", [])
            dates = []
            for b in bookings:
                iso = b.get("datetime_iso")
                if iso:
                    try:
                        dates.append(_parse_iso_aware(iso))
                    except Exception:
                        pass
            if not dates:
                continue
            last_visit = max(dates)
            days_since = (now - last_visit).days
            if days_since < kb.settings["idle_client_threshold_days"]:
                continue
            # Cooldown — чтобы не спамить раз в неделю одного и того же
            last_msg_iso = s.get("last_idle_msg_at")
            if last_msg_iso:
                try:
                    last_msg_dt = _parse_iso_aware(last_msg_iso)
                    if (now - last_msg_dt).days < kb.settings["idle_msg_cooldown_days"]:
                        continue
                except Exception:
                    pass
            try:
                bot.send_message(
                    uid,
                    f"👋 Давно не виделись — последняя запись была около {days_since} "
                    "дней назад.\nНоготки заскучали? Запишемся: /start",
                )
                s["last_idle_msg_at"] = now.isoformat()
                state_store.set(uid, s)
                sent += 1
            except Exception as e:
                log.debug("idle_check: не доставлено user %s: %s", uid, e)
        except Exception as e:
            log.warning("idle_check: ошибка для %s: %s", uid, e)
    log.info("idle_check: отправлено сообщений: %d", sent)


def _check_birthdays() -> None:
    """
    Ежедневно в 11:00 МСК: поздравляем клиентов с днём рождения.
    Клиента поздравляем один раз в год (защита от повторов через last_birthday_greeted_year).
    Мастеров уведомляем — чтобы могли поздравить лично / предложить что-то.
    Если в knowledge.md есть промокод BIRTHDAY (valid: yes) — упоминаем его в поздравлении.
    """
    if db is None:
        return
    now = _now_moscow()
    try:
        clients_today = db.clients.get_clients_with_birthday_today(now.day, now.month)
    except Exception as e:
        log.warning("birthday_check: ошибка запроса: %s", e)
        return

    if not clients_today:
        return

    # Промокод BIRTHDAY из knowledge.md (опционально)
    birthday_promo = None
    for code, info in kb.promo_codes.items():
        if code.upper() == "BIRTHDAY" and info.get("valid"):
            birthday_promo = code
            break

    log.info("birthday_check: %d именинников сегодня", len(clients_today))
    greeted = 0
    for c in clients_today:
        uid = c["user_id"]
        chat_id = c.get("chat_id")
        if not chat_id:
            continue
        try:
            s = state_store.get(uid) or {}
            if s.get("last_birthday_greeted_year") == now.year:
                continue  # уже поздравили в этом году

            name = (c.get("name") or "").split(" ")[0]
            greeting = f"🎂 <b>С Днём рождения{', ' + name if name else ''}!</b>\n\n"
            greeting += f"Команда {kb.brand.get('name', 'студии')} желает тебе счастья и красоты! 💅✨"
            if birthday_promo:
                greeting += (f"\n\n🎁 Лови подарок — промокод <b>{birthday_promo}</b>. "
                             f"Введи его при записи в течение недели.")
            greeting += "\n\nЗаписаться: /start"

            bot.send_message(chat_id, greeting, parse_mode="HTML")
            s["last_birthday_greeted_year"] = now.year
            state_store.set(uid, s)
            greeted += 1
        except Exception as e:
            log.debug("birthday_check: не доставлено user %s: %s", uid, e)

    # Уведомляем мастеров об именинниках (чтобы поздравили лично)
    if clients_today:
        names = ", ".join(c.get("name") or f"id{c['user_id']}" for c in clients_today)
        for master in kb.masters.values():
            tg_id_raw = _normalize_tg_id(master.telegram_id)
            if tg_id_raw and tg_id_raw.lstrip("-").isdigit():
                try:
                    bot.send_message(int(tg_id_raw),
                                     f"🎂 Сегодня день рождения у: {names}")
                except Exception:
                    pass
    log.info("birthday_check: поздравлено клиентов: %d", greeted)


def _schedule_recurring_jobs() -> None:
    """Регистрирует периодические задачи (idle-check, и др.). Вызывается один раз при старте."""
    if not scheduler:
        return
    try:
        scheduler.add_job(
            _check_idle_clients,
            trigger="cron",
            day_of_week="mon", hour=12, minute=0,
            id="idle_check",
            replace_existing=True,
            misfire_grace_time=24 * 3600,
        )
        log.info("Регулярная задача idle_check запланирована на каждый пн 12:00")
    except Exception as e:
        log.warning("Не удалось запланировать idle_check: %s", e)

    # Поздравления с днём рождения — каждый день в 11:00 МСК
    try:
        scheduler.add_job(
            _check_birthdays,
            trigger="cron",
            hour=11, minute=0,
            id="birthday_check",
            replace_existing=True,
            misfire_grace_time=6 * 3600,
        )
        log.info("Регулярная задача birthday_check запланирована на каждый день 11:00")
    except Exception as e:
        log.warning("Не удалось запланировать birthday_check: %s", e)


    # Дайджесты — удобно, но жрут CPU/RAM на free; в LEAN выкл (есть /today)
    if not _LEAN_MODE:
        try:
            scheduler.add_job(
                _morning_digest,
                trigger="cron",
                hour=9, minute=0,
                id="morning_digest",
                replace_existing=True,
                misfire_grace_time=2 * 3600,
            )
            log.info("Утренний дайджест запланирован на 9:00 МСК ежедневно")
        except Exception as e:
            log.warning("Не удалось запланировать morning_digest: %s", e)
        try:
            scheduler.add_job(
                _evening_digest,
                trigger="cron",
                hour=20, minute=0,
                id="evening_digest",
                replace_existing=True,
                misfire_grace_time=2 * 3600,
            )
            log.info("Вечерний дайджест запланирован на 20:00 МСК ежедневно")
        except Exception as e:
            log.warning("Не удалось запланировать evening_digest: %s", e)
    else:
        log.info("LEAN: morning/evening digests выключены (используйте /today)")

    try:
        def _worker_tick():
            from database.worker import tick
            tick(bot=bot)

        scheduler.add_job(
            _worker_tick,
            trigger="interval",
            seconds=45,
            id="p1_worker_tick",
            replace_existing=True,
            misfire_grace_time=30,
        )
        log.info("P1 worker tick каждые 45с (expire / no-show / outbox)")
    except Exception as e:
        log.warning("Не удалось запланировать worker tick: %s", e)

    # Авто-синк Calendar: lean 2ч, иначе 30 мин
    _sync_min = 120 if _LEAN_MODE else 30
    try:
        scheduler.add_job(
            _sync_calendar_cron,
            trigger="interval",
            minutes=_sync_min,
            id="sync_calendar",
            replace_existing=True,
            misfire_grace_time=10 * 60,
        )
        log.info("Авто-синк календаря каждые %d мин", _sync_min)
    except Exception as e:
        log.warning("Не удалось запланировать sync_calendar: %s", e)

    # Бэкапы — 3 раза в день (утром, в обед, вечером) по МСК
    # Один раз в день (21:00 МСК): реже пики RAM на free tier 512 MB.
    # Утро/обед убраны — ручной /backup остаётся.
    for label, hour in (("backup_evening", 21),):
        try:
            scheduler.add_job(
                _backup_state_to_admin,
                trigger="cron",
                hour=hour, minute=0,
                id=label,
                replace_existing=True,
                misfire_grace_time=2 * 3600,
            )
            log.info("Регулярный бэкап '%s' запланирован на %02d:00 МСК", label, hour)
        except Exception as e:
            log.warning("Не удалось запланировать бэкап %s: %s", label, e)




# ---------------------------------------------------------------------------
# Flask webhook
# ---------------------------------------------------------------------------

_STARTED_AT = _now_moscow()


@app.route("/", methods=["GET"])
def index() -> str:
    return f"{kb.brand.get('name', 'Bot')} is running."


@app.route("/ping", methods=["GET"])
def ping() -> tuple[str, int]:
    """Сверхлёгкий keep-alive для UptimeRobot / free tier.

    Не ходит в Redis/SQLite/LLM — только «процесс жив».
    Именно этот URL пингуйте каждые 5 мин, иначе Render free усыпит
    инстанс и клиенты уйдут из-за cold start 30–60+ сек.
    """
    return "ok", 200


@app.route("/health", methods=["GET"])
def health_check():
    """Health-check. По умолчанию лёгкий; ?full=1 — Redis/SQLite/LLM детали."""
    try:
        uptime = int((_now_moscow() - _STARTED_AT).total_seconds())
        lean = bool(_LEAN_MODE)
        payload = {
            "status": "ok",
            "uptime_sec": uptime,
            "lean": lean,
            "now_moscow": _now_moscow().isoformat(timespec="seconds"),
        }
        try:
            from database.pg import pg_enabled, ping_pg
            if pg_enabled():
                pg_ok = ping_pg()
                payload["postgres"] = "ok" if pg_ok else "error"
                if not pg_ok:
                    payload["status"] = "error"
                    return payload, 503
        except Exception as e:
            if (config.database_url or "").strip():
                payload["postgres"] = "error"
                payload["status"] = "error"
                payload["postgres_error"] = str(e)
                return payload, 503
            log.warning("health: postgres optional check skipped: %s", e)

        # Лёгкий ответ — для частых мониторов (меньше CPU на free)
        if request.args.get("full") not in ("1", "true", "yes"):
            return payload, 200

        redis_ok = bool(state_store.redis)
        if redis_ok:
            try:
                state_store.redis.ping()
            except Exception:
                redis_ok = False

        db_ok = False
        try:
            from database.connection import get_connection
            with get_connection() as conn:
                conn.execute("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False

        scheduler_ok = bool(scheduler and scheduler.running)
        jobs_count = len(scheduler.get_jobs()) if scheduler_ok else 0
        llm_ok = bool(llm.enabled)
        payload.update({
            "redis": "ok" if redis_ok else "off",
            "database": "ok" if db_ok else "error",
            "scheduler": "ok" if scheduler_ok else "off",
            "scheduled_jobs": jobs_count,
            "llm": "ok" if llm_ok else "off",
            "llm_provider": {
                "base_url": config.llm_base_url,
                "model_parser": config.llm_model_parser,
                "model_oracle": config.llm_model_oracle,
            } if llm_ok else None,
            "knowledge": {
                "masters": len(kb.masters),
                "services": len(kb.services),
                "promo_codes": len(kb.promo_codes),
            },
        })
        return payload, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500


@app.route(config.webhook_path, methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    secret = (config.webhook_secret or "").strip()
    if secret:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != secret:
            abort(403)
    try:
        update = telebot.types.Update.de_json(request.get_data(as_text=True))
        bot.process_new_updates([update])
    except Exception as e:
        # Никогда не возвращаем 5xx Телеграму — иначе он начнёт ретраить.
        log.exception("Ошибка в webhook handler: %s", e)
    return "ok", 200


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def _setup_bot_commands() -> None:
    """
    Регистрирует список команд в меню Telegram (бургер слева от строки ввода).
    Клиенты видят базовые, мастера — плюс расписание и /block, админ — плюс /reload.
    """
    from telebot.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat

    # Минимальный slash-список — только самые ходовые команды.
    # Всё остальное доступно через /menu (категоризованное inline-меню).
    client_cmds = [
        BotCommand("menu", "📋 Главное меню"),
        BotCommand("start", "Перезапуск"),
        BotCommand("my", "Мои записи"),
        BotCommand("help", "Что умеет бот"),
        BotCommand("rules", "Правила салона"),
        BotCommand("privacy", "Как используем данные"),
        BotCommand("cancel", "Сбросить диалог"),
    ]
    master_cmds = client_cmds + [
        BotCommand("today", "Сегодня"),
        BotCommand("tomorrow", "Завтра"),
        BotCommand("salon_today", "Расписание всего салона сегодня"),
        BotCommand("income", "Мой заработок"),
        BotCommand("debts", "Неоплаченные визиты"),
        BotCommand("add_booking", "Записать клиента по телефону"),
        BotCommand("calendar_check", "Проверить свой Google Календарь"),
        BotCommand("reviews", "Отзывы клиентов"),
    ]
    admin_extra = [
        BotCommand("stats", "Статистика записей"),
        BotCommand("broadcast", "Рассылка"),
        BotCommand("reload", "Перечитать knowledge"),
        BotCommand("sync_calendar", "Синхронизировать календарь"),
    ]

    # Дефолтный набор — для всех
    try:
        bot.set_my_commands(client_cmds, scope=BotCommandScopeDefault())
        log.info("bot commands (default): %d", len(client_cmds))
    except Exception as e:
        log.warning("bot commands (default) failed: %s", e)

    # Под каждого мастера — расширенный
    handled_ids: set[int] = set()
    for m in kb.masters.values():
        tg_id_raw = _normalize_tg_id(m.telegram_id)
        if not tg_id_raw or not tg_id_raw.lstrip("-").isdigit():
            continue
        tg_id = int(tg_id_raw)
        cmds = master_cmds + (admin_extra if tg_id in config.admin_ids else [])
        try:
            bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=tg_id))
            log.info("bot commands (master %s, id=%d): %d", m.name, tg_id, len(cmds))
            handled_ids.add(tg_id)
        except Exception as e:
            log.warning("bot commands (master %s) failed: %s", m.name, e)

    # Админы, которые не мастера — тоже получают полный набор
    for admin_id in config.admin_ids:
        if admin_id in handled_ids:
            continue
        try:
            bot.set_my_commands(master_cmds + admin_extra,
                                 scope=BotCommandScopeChat(chat_id=admin_id))
            log.info("bot commands (admin id=%d): set", admin_id)
        except Exception as e:
            log.warning("bot commands (admin %d) failed: %s", admin_id, e)


def _register_extracted_handlers() -> None:
    """Регистрирует хендлеры из подмодулей handlers/*.
    Вызывается ОДИН раз перед стартом — после этого все handler'ы прицеплены к боту.
    """
    from types import SimpleNamespace
    from handlers import admin as _admin_mod
    from handlers import clients as _clients_mod
    from handlers import master as _master_mod
    from handlers import vacation_sick as _vac_mod
    from handlers import stats as _stats_mod
    from handlers import menu as _menu_mod

    ctx = SimpleNamespace(
        bot=bot, kb=kb, llm=llm, state_store=state_store, config=config,
        scheduler=scheduler,
        now_moscow=_now_moscow, parse_iso_aware=_parse_iso_aware,
        moscow_tz=_moscow_tz,
        master_by_telegram_id=_master_by_telegram_id,
        fetch_events_for_range=_fetch_events_for_range,
        calendar_service=_calendar_service,
        get_state=get_state, save_state=save_state,
        main_menu_kb=main_menu_kb,
        safe_send_long=safe_send_long,
        STEP_PSY_DIALOG=STEP_PSY_DIALOG, STEP_IDLE=STEP_IDLE,
        STEP_BROADCAST_TEXT=STEP_BROADCAST_TEXT,
        db=db,
    )
    global handle_psy_button, continue_psy_dialog, cmd_psy_stats
    # Оракул тяжёлый (LLM + psychologist.py) — не грузим модуль, если выключен
    if kb.features.get("oracle", True):
        from handlers import oracle as _oracle_mod
        oracle_api = _oracle_mod.register(ctx)
        handle_psy_button = oracle_api["handle_psy_button"]
        continue_psy_dialog = oracle_api["continue_psy_dialog"]
        cmd_psy_stats = oracle_api["cmd_psy_stats"]
    else:
        def handle_psy_button(chat_id, user_id):
            bot.send_message(chat_id, "Эта функция сейчас выключена.")

        def continue_psy_dialog(message, state):
            bot.send_message(message.chat.id, "Эта функция сейчас выключена.")

        def cmd_psy_stats(message):
            if message.chat.id in config.admin_ids:
                bot.send_message(message.chat.id, "Оракул выключен (oracle: no / LEAN).")
        log.info("oracle: off — handlers.oracle не загружен (экономия RAM)")


    admin_api = _admin_mod.register(ctx)
    global _backup_state_to_admin_impl, cmd_broadcast_start, cmd_clear_reminders
    global _sync_calendar_cron_impl
    # _backup_state_to_admin — shim в main остаётся, импл в _impl
    _backup_state_to_admin_impl = admin_api["backup_state_to_admin"]
    cmd_broadcast_start = admin_api["cmd_broadcast_start"]
    cmd_clear_reminders = admin_api["cmd_clear_reminders"]
    _sync_calendar_cron_impl = admin_api["sync_calendar_cron"]

    clients_api = _clients_mod.register(ctx)
    global _handle_card_edit_input, cmd_clients_list
    _handle_card_edit_input = clients_api["handle_card_edit_input"]
    cmd_clients_list = clients_api["cmd_clients_list"]

    master_api = _master_mod.register(ctx)
    global cmd_today, cmd_tomorrow, cmd_week, cmd_income, _morning_digest_impl, _evening_digest_impl
    global cmd_salon_today, cmd_salon_week, cmd_free_slots, cmd_off_today, cmd_edit_prices, cmd_edit_schedule
    global handle_price_edit_text, clear_price_edit
    cmd_today = master_api["cmd_today"]
    cmd_tomorrow = master_api["cmd_tomorrow"]
    cmd_week = master_api["cmd_week"]
    cmd_income = master_api["cmd_income"]
    cmd_salon_today = master_api["cmd_salon_today"]
    cmd_salon_week = master_api["cmd_salon_week"]
    cmd_free_slots = master_api["cmd_free_slots"]
    cmd_edit_schedule = master_api["cmd_edit_schedule"]
    cmd_edit_prices = master_api["cmd_edit_prices"]
    handle_price_edit_text = master_api["handle_price_edit_text"]
    clear_price_edit = master_api.get("clear_price_edit")
    cmd_off_today = master_api["cmd_off_today"]
    _morning_digest_impl = master_api["send_morning_digest"]
    _evening_digest_impl = master_api["send_evening_digest"]

    vac_api = _vac_mod.register(ctx)
    global cmd_sick, _get_vacation_dates, send_vacation_picker
    cmd_sick = vac_api["cmd_sick"]
    _get_vacation_dates = vac_api["get_vacation_dates"]
    send_vacation_picker = vac_api["send_vacation_picker"]

    # Слоты — pure-logic фабрика. Нужна после vacation (для get_vacation_dates).
    from services import slots as _slots_mod
    from services.calendar import get_busy_intervals as _get_busy_intervals_ref
    from services.calendar import slot_is_free as _slot_is_free_ref
    ctx.get_vacation_dates = _get_vacation_dates
    ctx.get_busy_intervals = _get_busy_intervals_ref
    ctx.slot_is_free = _slot_is_free_ref
    # Константы FIXED_SLOTS_HOURS, MIN_HOURS_BEFORE_BOOKING и т.п. больше
    # не передаются через ctx — handler-модули читают их из ctx.kb.settings
    # в call-time, чтобы /reload пробрасывал изменения без рестарта.
    slots_api = _slots_mod.create(ctx)
    global _available_dates, _date_picker_kb, _time_picker_kb
    global _format_slot, _total_duration_minutes, resolve_booking_time
    _available_dates = slots_api["available_dates"]
    _date_picker_kb = slots_api["date_picker_kb"]
    _time_picker_kb = slots_api["time_picker_kb"]
    _format_slot = slots_api["format_slot"]
    _total_duration_minutes = slots_api["total_duration_minutes"]
    resolve_booking_time = slots_api["resolve_booking_time"]
    ctx.find_free_slots = slots_api["find_free_slots"]

    stats_api = _stats_mod.register(ctx)
    global _track_groq_call, cmd_stats, cmd_llm_usage
    _track_groq_call = stats_api["track_groq_call"]
    cmd_stats = stats_api["cmd_stats"]
    cmd_llm_usage = stats_api["cmd_llm_usage"]
    # Подключаем колбэк к LLM (раньше делалось на module level — теперь после register)
    llm.usage_callback = _track_groq_call

    # Booking flow — нужны slots+vacation API из ctx, plus куча хелперов.
    from handlers import booking as _booking_mod
    ctx.llm_usage = llm_usage
    ctx.normalize_tg_id = _normalize_tg_id
    ctx.reset_dialog = reset_dialog
    ctx.reply = reply
    ctx.masters_inline_kb = masters_inline_kb
    ctx.is_cancel_inside_dialog = _is_cancel_inside_dialog
    ctx.resolve_booking_time = resolve_booking_time
    ctx.date_picker_kb = _date_picker_kb
    ctx.time_picker_kb = _time_picker_kb
    ctx.format_slot = _format_slot
    ctx.total_duration_minutes = _total_duration_minutes
    # ALL_MASTERS, CATEGORY_ICONS, REVIEW_DELAY_MINUTES_AFTER_END:
    # больше не передаются через ctx — booking module читает из kb напрямую
    # (kb мутируется in-place при /reload, новые значения подхватываются).
    ctx.STEP_CHOOSE_MASTER = STEP_CHOOSE_MASTER
    ctx.STEP_CHOOSE_SERVICES = STEP_CHOOSE_SERVICES
    ctx.STEP_ENTER_DATETIME = STEP_ENTER_DATETIME
    ctx.STEP_ENTER_PHONE = STEP_ENTER_PHONE
    ctx.STEP_CONFIRM = STEP_CONFIRM
    ctx.STEP_PROMO_INPUT = STEP_PROMO_INPUT
    ctx.STEP_AWAITING_REVIEW = STEP_AWAITING_REVIEW
    booking_api = _booking_mod.register(ctx)
    global start_booking, continue_booking, handle_my_bookings, notify_master
    global _send_reminder_impl, _send_review_prompt_impl
    start_booking = booking_api["start_booking"]
    continue_booking = booking_api["continue_booking"]
    handle_my_bookings = booking_api["handle_my_bookings"]
    notify_master = booking_api["notify_master"]
    _send_reminder_impl = booking_api["send_reminder_impl"]
    _send_review_prompt_impl = booking_api["send_review_prompt_impl"]
    ctx.schedule_reminder = booking_api["schedule_reminder"]

    # Меню — регистрируем ПОСЛЕДНИМ: он диспатчит во все остальные, и через
    # late-binding (importlib.import_module) тянет real функции из main.
    menu_api = _menu_mod.register(ctx)
    global cmd_menu
    cmd_menu = menu_api["cmd_menu"]


def _verify_register_complete() -> None:
    """Проверка после _register_extracted_handlers(): все стабы должны быть
    заменены на реальные функции из подмодулей. Если что-то осталось стабом
    (модуль 'main' / __main__), логируем ERROR в Sentry — это регрессия.

    Стабы из main допустимы только для функций которые НЕ выносились и живут
    в main.py изначально (cmd_prices, cmd_rules, и т.п.). Список проверки —
    только те имена которые должны были подмениться register'ом.
    """
    import sys
    main_mod = sys.modules.get(__name__)
    expected_module_map = {
        # name → ожидаемый __module__ после register
        # _backup_state_to_admin — shim в main для APScheduler
        "cmd_broadcast_start": "handlers.admin",
        "cmd_clear_reminders": "handlers.admin",
        "_handle_card_edit_input": "handlers.clients",
        "cmd_clients_list": "handlers.clients",
        "cmd_today": "handlers.master",
        "cmd_tomorrow": "handlers.master",
        "cmd_week": "handlers.master",
        "cmd_income": "handlers.master",
        "cmd_sick": "handlers.vacation_sick",
        "_get_vacation_dates": "handlers.vacation_sick",
        "_track_groq_call": "handlers.stats",
        "cmd_stats": "handlers.stats",
        "cmd_llm_usage": "handlers.stats",
        "_format_slot": "services.slots",
        "_total_duration_minutes": "services.slots",
        "resolve_booking_time": "services.slots",
        "_date_picker_kb": "services.slots",
        "_time_picker_kb": "services.slots",
        "_available_dates": "services.slots",
        "start_booking": "handlers.booking",
        "continue_booking": "handlers.booking",
        "handle_my_bookings": "handlers.booking",
        "notify_master": "handlers.booking",
        "_send_reminder_impl": "handlers.booking",
        "_send_review_prompt_impl": "handlers.booking",
        "cmd_menu": "handlers.menu",
    }
    if kb.features.get("oracle", True):
        expected_module_map.update({
            "handle_psy_button": "handlers.oracle",
            "continue_psy_dialog": "handlers.oracle",
            "cmd_psy_stats": "handlers.oracle",
        })
    broken: list[str] = []
    for name, expected in expected_module_map.items():
        fn = getattr(main_mod, name, None)
        if fn is None:
            broken.append(f"{name}: MISSING")
            continue
        actual = getattr(fn, "__module__", "?")
        if actual != expected:
            broken.append(f"{name}: ожидался {expected}, реальный {actual} (стаб?)")
    if broken:
        msg = ("REGISTER BROKEN — следующие имена не подменились реальными функциями:\n  "
               + "\n  ".join(broken))
        log.error(msg)
    else:
        log.info("register: все %d биндингов прошли проверку", len(expected_module_map))


def _startup_free_tier_heal() -> None:
    """Redis→SQLite reconcile только если CRM пустая (экономия RAM/CPU на free)."""
    import gc
    try:
        from database.connection import get_connection
        with get_connection() as conn:
            n = int(conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0])
        if n > 0 and _LEAN_MODE:
            log.info("startup reconcile: skip (bookings already=%s, LEAN)", n)
            return
        from database.reconcile import reconcile_redis_to_sqlite
        stats = reconcile_redis_to_sqlite(db, state_store)
        log.info(
            "startup reconcile Redis→SQLite: clients=%s bookings_new=%s skipped=%s",
            stats.get("clients_upserted"),
            stats.get("bookings_inserted"),
            stats.get("bookings_skipped"),
        )
    except Exception as e:
        log.warning("startup reconcile failed: %s", e)
    finally:
        gc.collect()


def run() -> None:
    try:
        _register_extracted_handlers()
    except Exception as e:
        log.exception("register_extracted_handlers failed: %s", e)
    try:
        _verify_register_complete()
    except Exception as e:
        log.warning("verify_register_complete failed: %s", e)
    try:
        _setup_bot_commands()
    except Exception as e:
        log.warning("Не удалось настроить меню команд: %s", e)
    try:
        _schedule_recurring_jobs()
    except Exception as e:
        log.warning("Не удалось запланировать периодические задачи: %s", e)

    # Free-tier: восстановление CRM из Redis (без уведомлений в TG)
    try:
        _startup_free_tier_heal()
    except Exception as e:
        log.warning("startup free-tier heal: %s", e)

    if config.webhook_url:
        full_url = config.webhook_url.rstrip("/") + config.webhook_path
        try:
            bot.remove_webhook()
            secret = (config.webhook_secret or "").strip() or None
            bot.set_webhook(url=full_url, secret_token=secret)
            log.info("Webhook установлен: %s secret=%s", full_url, "on" if secret else "off")
        except Exception as e:
            # Не падаем — Flask всё равно стартует, и при первом успешном
            # запросе телеграма webhook можно поставить вручную.
            log.error("Не удалось установить webhook (%s): %s", full_url, e)
        from waitress import serve
        # free: 1 поток — меньше RAM/CPU; paid/dev: до 2
        _threads = 1 if _LEAN_MODE else 2
        serve(app, host="0.0.0.0", port=config.port, threads=_threads,
              channel_timeout=60, cleanup_interval=30)
    else:
        log.info("WEBHOOK_URL не задан — запускаемся в режиме long polling (для разработки)")
        try:
            bot.remove_webhook()
        except Exception as e:
            log.warning("remove_webhook failed: %s", e)
        bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    run()
