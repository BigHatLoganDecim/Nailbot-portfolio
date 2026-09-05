"""
Вспомогательные модули: загрузка knowledge.md, хранилище состояний, анти-спам, логи.

Главный принцип: всё, что может упасть (Redis, файл, парсинг) — имеет fallback.
Бот должен работать даже при частичных сбоях.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("nailbot")


# ---------------------------------------------------------------------------
# Загрузка knowledge.md
# ---------------------------------------------------------------------------

@dataclass
class Master:
    name: str
    telegram_id: str = ""
    speciality: str = ""
    description: str = ""
    schedule: str = ""
    calendar_id: str = ""
    work_hours: str = "10:00-22:00"      # fallback, если не заданы отдельные для будней/выходных
    work_hours_weekday: str = ""         # часы по будням (пн-пт), например 14:00-22:00
    work_hours_weekend: str = ""         # часы по выходным (сб, вс), например 10:00-18:00
    work_dates: str = ""                 # список рабочих дат, ISO через запятую
    # День → разрешённые часы старта (10,12,…). Если день не в словаре — все fixed_slot_hours.
    # knowledge: `- open_slots: 2026-08-05:14,16; 2026-08-06:18`
    open_slots: dict = field(default_factory=dict)
    contact_phone: str = ""              # личный контактный телефон мастера
    telegram_channel: str = ""           # канал/блог в Telegram
    aliases: list[str] = field(default_factory=list)  # дополнительные формы имени для парсера


@dataclass
class Service:
    name: str
    price: str
    duration: str
    masters: list[str] = field(default_factory=list)
    category: str = "Прочее"


# Дефолтные настройки бота — используются если в knowledge.md нет секции `## Настройки`.
# Так старый knowledge.md продолжает работать.
DEFAULT_SETTINGS = {
    "fixed_slot_hours": (10, 12, 14, 16, 18, 20),
    "min_hours_before_booking": 3,
    "slot_duration_min": 60,
    "default_service_duration_min": 60,
    "date_picker_max_days": 14,
    "review_delay_minutes_after_end": 60,
    # Anti-spam / off-topic чат с ботом
    "off_topic_strike_limit": 2,         # после стольких подряд off_topic жёстко вернуть к теме
    "off_topic_session_limit": 5,        # абсолютный лимит болтовни за сессию (LLM не зовём)
    # Фича «Оракул»
    "psy_max_sessions_per_day": 3,       # сколько Оракул-сессий клиент может в день
    "psy_max_turns": 12,                 # максимум реплик пользователя за сессию
    # Idle-маркетинг (давно не были — напишем)
    "idle_client_threshold_days": 60,    # после стольких дней без визита считаем idle
    "idle_msg_cooldown_days": 45,        # не чаще одного raз в этот период не спамим
    # Квоты LLM-провайдера (для алертов админу при 50/80/90% использования)
    "llm_daily_request_limit": 14_400,   # Groq free tier по умолчанию
    "llm_daily_token_limit": 500_000,
}


@dataclass
class Knowledge:
    """Содержимое knowledge.md в структурированном виде."""
    masters: dict[str, Master] = field(default_factory=dict)   # имя -> Master
    services: list[Service] = field(default_factory=list)
    rules: str = ""
    contacts: str = ""
    certificates: str = ""
    brand: dict = field(default_factory=lambda: {
        "name": "Salon",
        "short_name": "Salon",
        "channel": "",
        "emoji": "✨",  # бренд-эмодзи: в календаре, "Процедуры:"
    })
    messages: dict[str, str] = field(default_factory=dict)     # ключ -> текст
    promo_codes: dict[str, dict] = field(default_factory=dict) # КОД -> {description, valid}
    # Настройки слотов, длительностей и т.п. — дефолтные значения в DEFAULT_SETTINGS.
    settings: dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    # Иконки категорий услуг для inline-кнопок. "Прочее" → "📋" если не задано.
    categories: dict[str, str] = field(default_factory=dict)
    # Синонимы услуг: подстрока в речи клиента → канонический корень.
    # Например: "ногти" → "маникюр", "ножки" → "педикюр".
    service_synonyms: dict[str, str] = field(default_factory=dict)
    # Реферальная программа: тексты-плейсхолдеры для скидок и бонусов.
    # Если в knowledge.md секции `## Реферальная программа` нет — используются дефолты.
    referral: dict = field(default_factory=lambda: {
        "enabled": True,
        "program_name": "Подружка",       # как называется реф-программа в /invite
        "new_client_discount_pct": 10,
        "new_client_offer_text": "на первый визит скидка 10%",
        "new_client_confirm_text": "На этот визит — реферальная скидка 10% (вы новый клиент).",
        "new_client_master_note": "Реферал: -10% (первый визит по приглашению)",
        "referrer_bonus_text": "бесплатный простой дизайн на все ногти",
        "referrer_bonus_offer": "1 бесплатный простой дизайн на все ногти",
        "referrer_bonus_short": "бесплатный простой дизайн",
        "referrer_bonus_master_note": "Реферальный бонус: бесплатный простой дизайн на все ногти",
    })
    # Фича-флаги — kill-switch для крупных фич. Все True по умолчанию.
    # Секция `## Фича-флаги` в knowledge.md может переопределить.
    # Используется в menu.py (скрывает кнопки) и в самих хендлерах (отказ).
    features: dict = field(default_factory=lambda: {
        "oracle": True,       # вкл/выкл «Оракул» (3 личности LLM-собеседника)
        "referrals": True,    # реферальная программа «приведи друга»
        "llm_parser": True,   # LLM-fallback парсера при INTENT_UNKNOWN
    })
    raw: str = ""                                              # исходный текст, на всякий случай

    def msg(self, key: str, **fmt) -> str:
        """Получить текст сообщения с подстановкой переменных. Никогда не падает."""
        template = self.messages.get(key, "")
        if not template:
            return ""
        try:
            return template.format(**fmt)
        except (KeyError, IndexError):
            # Если шаблон требует переменную, которой нет — отдаём как есть.
            return template

    def replace_from(self, other: "Knowledge") -> None:
        """МУТИРУЕТ self данными из other ВНУТРИ существующих контейнеров.
        Критично для /reload — handler-модули захватывают ссылки на kb,
        kb.settings, kb.categories и т.п. в register-time. Если перебиндить
        атрибуты (self.settings = other.settings), подмодули продолжат видеть
        старые dict'ы. Поэтому везде clear() + update()/extend().
        """
        # Dicts — clear + update (сохраняет тот же объект)
        self.masters.clear()
        self.masters.update(other.masters)
        self.brand.clear()
        self.brand.update(other.brand)
        self.messages.clear()
        self.messages.update(other.messages)
        self.promo_codes.clear()
        self.promo_codes.update(other.promo_codes)
        self.settings.clear()
        self.settings.update(other.settings)
        self.categories.clear()
        self.categories.update(other.categories)
        self.service_synonyms.clear()
        self.service_synonyms.update(other.service_synonyms)
        self.referral.clear()
        self.referral.update(other.referral)
        self.features.clear()
        self.features.update(other.features)
        # Lists — clear + extend
        self.services.clear()
        self.services.extend(other.services)
        # Strings — простой rebind (никто не держит ссылку на str отдельно)
        self.rules = other.rules
        self.contacts = other.contacts
        self.certificates = other.certificates
        self.raw = other.raw


def load_knowledge(path: str) -> Knowledge:
    """
    Прочитать knowledge.md и распарсить его в Knowledge.
    При любой ошибке возвращает максимально полное содержимое, не падая.
    """
    kb = Knowledge()
    try:
        with open(path, "r", encoding="utf-8") as f:
            kb.raw = f.read()
    except OSError as e:
        log.error("knowledge.md не прочитан: %s. Бот стартует с пустой базой.", e)
        return kb

    sections = _split_sections(kb.raw)

    # Мастера — принимаем любые имена из knowledge.md (раньше был хардкод
    # фильтра под два хардкод-имени, мешал шаблонизации под другие салоны).
    masters_block = sections.get("Мастера", "")
    for name, sub_block in _split_subsections(masters_block).items():
        m = Master(name=name)
        m.telegram_id = _extract_field(sub_block, "telegram_id")
        m.speciality = _extract_field(sub_block, "специализация")
        m.description = _extract_field(sub_block, "описание")
        m.calendar_id = _extract_field(sub_block, "calendar_id")
        wh = _extract_field(sub_block, "work_hours")
        if wh and "заполнить" not in wh.lower():
            m.work_hours = wh
        whd = _extract_field(sub_block, "work_hours_weekday")
        if whd and "заполнить" not in whd.lower():
            m.work_hours_weekday = whd
        whe = _extract_field(sub_block, "work_hours_weekend")
        if whe and "заполнить" not in whe.lower():
            m.work_hours_weekend = whe
        m.work_dates = _extract_field(sub_block, "work_dates")
        m.open_slots = _parse_open_slots(_extract_field(sub_block, "open_slots"))
        m.contact_phone = _extract_field(sub_block, "contact_phone")
        m.telegram_channel = _extract_field(sub_block, "telegram_channel")
        # Aliases — дополнительные формы имени для парсера свободного текста.
        # Формат: `- aliases: ден, денчик, дэн` через запятую.
        aliases_raw = _extract_field(sub_block, "aliases")
        if aliases_raw:
            m.aliases = [a.strip().lower() for a in aliases_raw.split(",") if a.strip()]
        kb.masters[name] = m

    # График — прикрепляем к мастерам
    schedule_block = sections.get("График работы", "")
    for name, sub_block in _split_subsections(schedule_block).items():
        if name in kb.masters:
            kb.masters[name].schedule = sub_block.strip()

    # Услуги — поддерживаем категории через комментарии `# --- Категория ---`
    services_block = sections.get("Услуги", "")
    current_category = "Прочее"
    cat_re = re.compile(r"^#\s*-+\s*(.+?)\s*-+\s*$")
    for line in services_block.splitlines():
        raw = line.rstrip()
        stripped = raw.strip()
        cat_match = cat_re.match(stripped)
        if cat_match:
            current_category = cat_match.group(1).strip()
            continue
        if not stripped.startswith("- "):
            continue
        parts = [p.strip() for p in stripped[2:].split("|")]
        if len(parts) < 4:
            continue
        name, price, duration, masters_str = parts[:4]
        masters_list = [m.strip() for m in masters_str.split(",") if m.strip()]
        kb.services.append(Service(name=name, price=price, duration=duration,
                                    masters=masters_list, category=current_category))

    # Правила, контакты, сертификаты
    kb.rules = sections.get("Правила", "").strip()
    kb.contacts = sections.get("Адрес и контакты", "").strip()
    kb.certificates = sections.get("Сертификаты", "").strip()

    # Бренд — для отображения в /menu, прайсе, рассылке
    brand_block = sections.get("Бренд", "")
    for key in ("name", "short_name", "channel", "emoji"):
        val = _extract_field(brand_block, key)
        if val and "заполнить" not in val.lower():
            kb.brand[key] = val

    # Сообщения бота
    messages_block = sections.get("Сообщения бота", "")
    for key, text in _split_subsections(messages_block).items():
        kb.messages[key] = text.strip()

    # Промокоды
    promo_block = sections.get("Промокоды", "")
    for code, sub_block in _split_subsections(promo_block).items():
        desc = _extract_field(sub_block, "description")
        valid_raw = _extract_field(sub_block, "valid").lower()
        valid = valid_raw in ("yes", "да", "true", "1") or not valid_raw  # по умолчанию валиден
        if desc:
            kb.promo_codes[code.upper().strip()] = {
                "description": desc.strip(),
                "valid": valid,
            }

    # Настройки — слоты, длительности, лимиты. Все опциональны, есть дефолты.
    # Формат: `- fixed_slot_hours: 10,12,14,16,18,20`
    #         `- min_hours_before_booking: 3`
    settings_block = sections.get("Настройки", "")
    if settings_block.strip():
        for key in ("fixed_slot_hours",):
            val = _extract_field(settings_block, key)
            if val:
                try:
                    hours = tuple(int(h.strip()) for h in val.split(",") if h.strip())
                    if hours:
                        kb.settings[key] = hours
                except ValueError:
                    log.warning("settings.%s='%s' не парсится — fallback на дефолт", key, val)
        for key in ("min_hours_before_booking", "slot_duration_min",
                    "default_service_duration_min", "date_picker_max_days",
                    "review_delay_minutes_after_end",
                    "off_topic_strike_limit", "off_topic_session_limit",
                    "psy_max_sessions_per_day", "psy_max_turns",
                    "idle_client_threshold_days", "idle_msg_cooldown_days",
                    "llm_daily_request_limit", "llm_daily_token_limit"):
            val = _extract_field(settings_block, key)
            if val:
                try:
                    kb.settings[key] = int(val)
                except ValueError:
                    log.warning("settings.%s='%s' не int — fallback на дефолт", key, val)

    # Категории услуг с иконками. Формат:
    #   ### Маникюр
    #   - icon: 💅
    categories_block = sections.get("Категории услуг", "")
    if categories_block.strip():
        for cat_name, sub_block in _split_subsections(categories_block).items():
            icon = _extract_field(sub_block, "icon")
            if icon:
                kb.categories[cat_name.strip()] = icon.strip()

    # Синонимы услуг для парсера свободного текста.
    # Формат: каждая строка `- ногти: маникюр` (слева — как клиент говорит,
    # справа — канонический корень из knowledge.md услуг).
    synonyms_block = sections.get("Синонимы услуг", "")
    if synonyms_block.strip():
        syn_re = re.compile(r"^[\-\*]\s*([^:]+?)\s*:\s*(.+?)\s*$", re.MULTILINE)
        for m in syn_re.finditer(synonyms_block):
            alias = m.group(1).strip().lower()
            canon = m.group(2).strip().lower()
            if alias and canon:
                kb.service_synonyms[alias] = canon

    # Реферальная программа — текстовки скидок/бонусов. Опциональна.
    referral_block = sections.get("Реферальная программа", "")
    if referral_block.strip():
        for key in ("enabled",):
            val = _extract_field(referral_block, key).lower()
            if val:
                kb.referral[key] = val in ("yes", "да", "true", "1", "on")
        for key in ("new_client_discount_pct",):
            val = _extract_field(referral_block, key)
            if val:
                try:
                    kb.referral[key] = int(val)
                except ValueError:
                    log.warning("referral.%s='%s' не int — fallback на дефолт", key, val)
        for key in ("program_name", "new_client_offer_text",
                     "new_client_confirm_text", "new_client_master_note",
                     "referrer_bonus_text", "referrer_bonus_offer",
                     "referrer_bonus_short", "referrer_bonus_master_note"):
            val = _extract_field(referral_block, key)
            if val:
                kb.referral[key] = val

    # Фича-флаги — опциональная секция `## Фича-флаги` (всё по умолчанию True).
    # Формат:  `- oracle: no`  или  `- referrals: yes`
    features_block = sections.get("Фича-флаги", "") or sections.get("Features", "")
    if features_block.strip():
        for key in list(kb.features.keys()):
            val = _extract_field(features_block, key).lower()
            if val:
                kb.features[key] = val in ("yes", "да", "true", "1", "on")

    log.info(
        "knowledge.md загружен: мастеров=%d, услуг=%d, сообщений=%d",
        len(kb.masters), len(kb.services), len(kb.messages),
    )
    return kb


def _split_sections(text: str) -> dict[str, str]:
    """Разделить документ по заголовкам уровня `## Название`."""
    # Заголовок: строка вида "## Что-то"
    pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    result: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[title] = text[start:end]
    return result


def _split_subsections(text: str) -> dict[str, str]:
    """Разделить блок по заголовкам уровня `### Название`."""
    pattern = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    result: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[title] = text[start:end]
    return result


def _extract_field(block: str, key: str) -> str:
    """Вытащить из блока строку вида `- key: value`."""
    pattern = re.compile(
        rf"^[\-\*]\s*{re.escape(key)}\s*:\s*(.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    m = pattern.search(block)
    return m.group(1).strip() if m else ""


def _parse_open_slots(raw: str) -> dict[str, tuple[int, ...]]:
    """`2026-08-05:14,16; 2026-08-06:18` → {'2026-08-05': (14, 16), ...}."""
    out: dict[str, tuple[int, ...]] = {}
    if not raw or "заполнить" in raw.lower() or "уточн" in raw.lower():
        return out
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})\s*:\s*(.+)$", part)
        if not m:
            log.warning("open_slots: не распознан фрагмент %r", part)
            continue
        day, hours_raw = m.group(1), m.group(2)
        hours: list[int] = []
        for h in hours_raw.split(","):
            h = h.strip()
            if not h:
                continue
            try:
                hours.append(int(h.split(":")[0]))
            except ValueError:
                log.warning("open_slots: час %r в %s", h, day)
        if hours:
            out[day] = tuple(hours)
    return out


# ---------------------------------------------------------------------------
# Хранилище состояний пользователей (Redis с fallback в память)
# ---------------------------------------------------------------------------

class StateStore:
    """
    Простое key-value хранилище для состояния диалогов.
    Если задан REDIS_URL и пакет redis установлен — использует Redis.
    Иначе — потокобезопасный словарь в памяти (данные теряются при рестарте).
    """

    def __init__(
        self,
        redis_url: str | None = None,
        ttl_seconds: int = 60 * 60 * 24 * 30,
        require_redis: bool = False,
    ):
        self.ttl = ttl_seconds
        self._mem: dict[str, dict[str, Any]] = {}
        self._mem_lock = threading.Lock()
        self._redis = None

        if redis_url:
            try:
                import redis  # type: ignore
                self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                log.info("StateStore: подключение к Redis установлено")
            except Exception as e:
                if require_redis:
                    raise RuntimeError(f"Redis обязателен для webhook FSM: {e}") from e
                log.warning("StateStore: Redis недоступен (%s) — fallback в память", e)
                self._redis = None
        elif require_redis:
            raise RuntimeError("REDIS_URL обязателен при WEBHOOK_URL (MemoryStorage на проде запрещён)")

    def get(self, user_id: int) -> dict[str, Any]:
        key = self._key(user_id)
        if self._redis:
            import json
            last_err: Exception | None = None
            for attempt in range(2):  # 1 ретрай — на transient errors
                try:
                    raw = self._redis.get(key)
                    return json.loads(raw) if raw else {}
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        time.sleep(0.05)
            # Оба ретрая упали — это ЧП, но fallback в память чтобы не сломать диалог
            log.error("StateStore.get: Redis недоступен после ретраев (%s) — "
                       "fallback в память. Возможно смешение источников state!", last_err)
        with self._mem_lock:
            raw = self._mem.get(key, {})
            # Защита от мусора в _mem (например, при ручной правке или после
            # crash). dict(str|int|None) падает с TypeError.
            if not isinstance(raw, dict):
                log.warning("StateStore.get: _mem[%s] не dict (type=%s), очищаю",
                            key, type(raw).__name__)
                return {}
            return dict(raw)

    def set(self, user_id: int, state: dict[str, Any]) -> None:
        key = self._key(user_id)
        if not isinstance(state, dict):
            log.error("StateStore.set: попытка сохранить не-dict для %s (type=%s) — игнорирую",
                      key, type(state).__name__)
            return
        if self._redis:
            import json
            payload = json.dumps(state, ensure_ascii=False)
            last_err: Exception | None = None
            for attempt in range(2):
                try:
                    self._redis.setex(key, self.ttl, payload)
                    return
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        time.sleep(0.05)
            log.error("StateStore.set: Redis недоступен после ретраев (%s) — "
                       "fallback в память. State может быть потерян после рестарта!", last_err)
        with self._mem_lock:
            self._mem[key] = dict(state)

    def clear(self, user_id: int) -> None:
        key = self._key(user_id)
        if self._redis:
            try:
                self._redis.delete(key)
                return
            except Exception as e:
                log.warning("StateStore.clear: ошибка Redis: %s", e)
        with self._mem_lock:
            self._mem.pop(key, None)

    def track_user(self, user_id: int) -> None:
        """Запомнить user_id в общем индексе клиентов (для рассылок типа 'давно не были')."""
        if self._redis:
            try:
                self._redis.sadd("nailbot:all_clients", user_id)
                return
            except Exception as e:
                log.warning("StateStore.track_user: Redis: %s", e)
        # Для in-memory режима индекс не нужен — at all_users() пройдёмся по _mem напрямую

    @property
    def redis(self):
        """Публичный доступ к Redis-клиенту для низкоуровневых операций
        (счётчики, locks, scan). None если Redis не подключён."""
        return self._redis

    def get_many(self, user_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Batch-получение state для списка user_id. Использует Redis pipeline
        чтобы избежать N+1 RTT. При ошибке Redis молча падает в обычный get()."""
        if not user_ids:
            return {}
        result: dict[int, dict[str, Any]] = {}
        if self._redis:
            try:
                import json
                pipe = self._redis.pipeline()
                for uid in user_ids:
                    pipe.get(self._key(uid))
                raws = pipe.execute()
                for uid, raw in zip(user_ids, raws):
                    if raw:
                        try:
                            result[uid] = json.loads(raw)
                        except Exception:
                            result[uid] = {}
                    else:
                        result[uid] = {}
                return result
            except Exception as e:
                log.warning("StateStore.get_many: pipeline ошибка, fallback на N+1: %s", e)
        for uid in user_ids:
            result[uid] = self.get(uid)
        return result

    def all_users(self) -> list[int]:
        """Все user_id, у кого когда-либо было состояние или запись."""
        ids: set[int] = set()
        if self._redis:
            try:
                for raw in self._redis.smembers("nailbot:all_clients"):
                    try:
                        ids.add(int(raw))
                    except Exception:
                        pass
                return list(ids)
            except Exception as e:
                log.warning("StateStore.all_users: Redis: %s", e)
        # In-memory fallback
        with self._mem_lock:
            for k in self._mem.keys():
                m = re.match(r"nailbot:state:(\d+)$", k)
                if m:
                    ids.add(int(m.group(1)))
        return list(ids)

    @staticmethod
    def _key(user_id: int) -> str:
        return f"nailbot:state:{user_id}"


# ---------------------------------------------------------------------------
# Анти-спам: простой rate limiter в памяти
# ---------------------------------------------------------------------------

class LLMUsageLimiter:
    """
    Лимитирует количество вызовов LLM на пользователя БЕЗ доведённой до конца записи.
    После успешной записи счётчик сбрасывается. Защита от того, чтобы клиент
    тратил токены на болтовню (LLM «помощник», а не «собеседник»).
    """

    def __init__(self, max_calls_per_session: int = 10):
        self.max_calls = max_calls_per_session
        self._counts: dict[int, int] = {}
        self._lock = threading.Lock()

    def can_call(self, user_id: int) -> bool:
        with self._lock:
            return self._counts.get(user_id, 0) < self.max_calls

    def register_call(self, user_id: int) -> int:
        """Зарегистрировать вызов LLM. Возвращает новое значение счётчика."""
        with self._lock:
            self._counts[user_id] = self._counts.get(user_id, 0) + 1
            return self._counts[user_id]

    def reset(self, user_id: int) -> None:
        with self._lock:
            self._counts.pop(user_id, None)


class RateLimiter:
    """
    Простейший лимит: не более `max_msgs` сообщений за `window` секунд от одного пользователя.
    В памяти — не критично, что данные теряются при рестарте.
    """

    def __init__(self, max_msgs: int = 15, window_sec: int = 30):
        self.max_msgs = max_msgs
        self.window = window_sec
        self._log: dict[int, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, user_id: int) -> bool:
        now = time.time()
        with self._lock:
            timestamps = [t for t in self._log.get(user_id, []) if now - t < self.window]
            if len(timestamps) >= self.max_msgs:
                self._log[user_id] = timestamps
                return False
            timestamps.append(now)
            self._log[user_id] = timestamps
            return True


# ---------------------------------------------------------------------------
# Конфиг — читаем переменные окружения с разумными умолчаниями.
# ---------------------------------------------------------------------------

# Пресеты LLM-провайдеров — «мозги» Оракула и остальных фич.
# Переключение в одну переменную: LLM_PROVIDER=deepseek (или groq/openai/...).
# Подставляет base_url + дефолтные модели. Точечные LLM_MODEL_* / LLM_BASE_URL
# по-прежнему переопределяют пресет.
#
# Где брать ключи:
#   groq       — console.groq.com (бесплатный tier)
#   deepseek   — platform.deepseek.com (дёшево, сильная модель, рубли через посредников)
#   openai     — platform.openai.com (дорого, топ-качество)
#   openrouter — openrouter.ai (агрегатор, один ключ на все модели)
#   vsegpt     — vsegpt.ru (российский reseller, оплата рублями)
#
# "smart" = модель для Оракула (нужно «умнее/эмпатичнее»),
# "fast"  = для парсера и саммари (быстро и дёшево).
LLM_PROVIDER_PRESETS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "smart": "llama-3.3-70b-versatile",
        "fast": "llama-3.1-8b-instant",
        "key_envs": ("GROQ_API_KEY",),
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "smart": "deepseek-chat",
        "fast": "deepseek-chat",
        "key_envs": ("DEEPSEEK_API_KEY",),
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "smart": "gpt-4o",
        "fast": "gpt-4o-mini",
        "key_envs": ("OPENAI_API_KEY",),
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "smart": "deepseek/deepseek-chat",
        "fast": "meta-llama/llama-3.1-8b-instruct",
        "key_envs": ("OPENROUTER_API_KEY",),
    },
    "vsegpt": {
        "base_url": "https://api.vsegpt.ru/v1",
        "smart": "deepseek/deepseek-chat",
        "fast": "deepseek/deepseek-chat",
        "key_envs": ("VSEGPT_API_KEY",),
    },
}
DEFAULT_LLM_PROVIDER = "groq"


@dataclass
class Config:
    bot_token: str
    webhook_url: str = ""           # https://your-domain/webhook — если пусто, используется long polling
    webhook_path: str = "/webhook"
    port: int = 8080
    knowledge_path: str = "knowledge.md"
    redis_url: str = ""
    # LLM провайдер (OpenAI-совместимый). По умолчанию — Groq.
    # Чтобы переключиться, например, на VseGPT — в Environment задать:
    #   LLM_API_KEY=<ключ от VseGPT>
    #   LLM_BASE_URL=https://api.vsegpt.ru/v1
    #   LLM_MODEL_ORACLE=deepseek/deepseek-chat (и т.д.)
    llm_provider: str = "groq"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model_parser: str = "llama-3.1-8b-instant"
    llm_model_oracle: str = "llama-3.3-70b-versatile"
    llm_model_summary: str = "llama-3.1-8b-instant"
    llm_fallback_model: str = "llama-3.3-70b-versatile"  # запасная при сбое основной
    log_level: str = "INFO"

    admin_ids: tuple[int, ...] = ()      # telegram user_id, кому доступен /reload
    database_url: str = ""
    hold_minutes: int = 5
    prepay: bool = False
    google_enabled: bool = False
    tz: str = "Europe/Moscow"
    webhook_secret: str = ""
    noshow_grace_min: int = 15

    @classmethod
    def from_env(cls) -> "Config":
        # _clean нормализует литералы "None"/"null"/" " — частая ошибка в Render env
        port_str = _clean(os.getenv("PORT", "8080")) or "8080"
        try:
            port = int(port_str)
        except ValueError:
            log.warning("PORT=%r не парсится в число — используем 8080", port_str)
            port = 8080
        admins_str = _clean(os.getenv("ADMIN_IDS", ""))
        admin_ids: tuple[int, ...] = ()
        if admins_str:
            try:
                admin_ids = tuple(int(x.strip()) for x in admins_str.split(",") if x.strip())
            except ValueError:
                log.warning("ADMIN_IDS=%r не парсится — пропускаем", admins_str)
        # --- LLM: выбор «мозга» через пресет провайдера + per-feature модели ---
        # 1) LLM_PROVIDER=groq|deepseek|openai|openrouter|vsegpt задаёт base_url
        #    и дефолтные модели (smart/fast).
        # 2) Точечные переменные LLM_BASE_URL / LLM_MODEL_* / LLM_FALLBACK_MODEL
        #    переопределяют пресет — для тонкой настройки.
        provider = (_clean(os.getenv("LLM_PROVIDER", "")) or DEFAULT_LLM_PROVIDER).lower()
        preset = LLM_PROVIDER_PRESETS.get(provider)
        if preset is None:
            log.warning("LLM_PROVIDER=%r неизвестен — откатываюсь на %s",
                        provider, DEFAULT_LLM_PROVIDER)
            provider = DEFAULT_LLM_PROVIDER
            preset = LLM_PROVIDER_PRESETS[DEFAULT_LLM_PROVIDER]

        # Ключ: общий LLM_API_KEY → ключ конкретного провайдера → legacy GROQ_API_KEY
        llm_api_key = _clean(os.getenv("LLM_API_KEY", ""))
        if not llm_api_key:
            for env_name in preset["key_envs"]:
                llm_api_key = _clean(os.getenv(env_name, ""))
                if llm_api_key:
                    break
        if not llm_api_key:
            llm_api_key = _clean(os.getenv("GROQ_API_KEY", ""))  # backward-compat

        llm_base_url = _clean(os.getenv("LLM_BASE_URL", "")) or preset["base_url"]

        model_fast = preset["fast"]
        model_smart = preset["smart"]
        legacy_groq_model = _clean(os.getenv("GROQ_MODEL", ""))  # совсем старый вариант
        model_parser = _clean(os.getenv("LLM_MODEL_PARSER", "")) or legacy_groq_model or model_fast
        model_oracle = _clean(os.getenv("LLM_MODEL_ORACLE", "")) or model_smart
        model_summary = _clean(os.getenv("LLM_MODEL_SUMMARY", "")) or model_fast
        fallback_model = _clean(os.getenv("LLM_FALLBACK_MODEL", "")) or model_smart

        log.info("LLM провайдер=%s | base_url=%s | oracle=%s | parser=%s | fallback=%s",
                 provider, llm_base_url, model_oracle, model_parser, fallback_model)

        return cls(
            bot_token=_clean(os.getenv("BOT_TOKEN", "")),
            webhook_url=_clean(os.getenv("WEBHOOK_URL", "")),
            webhook_path=_clean(os.getenv("WEBHOOK_PATH", "/webhook")) or "/webhook",
            port=port,
            knowledge_path=_clean(os.getenv("KNOWLEDGE_PATH", "knowledge.md")) or "knowledge.md",
            redis_url=_clean(os.getenv("REDIS_URL", "")),
            llm_provider=provider,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model_parser=model_parser,
            llm_model_oracle=model_oracle,
            llm_model_summary=model_summary,
            llm_fallback_model=fallback_model,
            log_level=_clean(os.getenv("LOG_LEVEL", "INFO")) or "INFO",
            admin_ids=admin_ids,
            database_url=_clean(os.getenv("DATABASE_URL", "")),
            hold_minutes=_hold_minutes_from_env(),
            prepay=_clean(os.getenv("PREPAY", "false")).lower() in ("1", "true", "yes", "on"),
            google_enabled=_clean(os.getenv("GOOGLE_ENABLED", "true")).lower() not in ("0", "false", "no", "off"),
            tz=_clean(os.getenv("TZ", "Europe/Moscow")) or "Europe/Moscow",
            webhook_secret=_clean(os.getenv("WEBHOOK_SECRET", "")),
            noshow_grace_min=_int_env("NOSHOW_GRACE_MIN", 15, 0, 180),
        )


def _hold_minutes_from_env() -> int:
    return _int_env("HOLD_MINUTES", 5, 1, 30)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = _clean(os.getenv(name, str(default))) or str(default)
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def _clean(value: str | None) -> str:
    """Нормализует значение из окружения: убирает пробелы, кавычки, литералы None/null."""
    if value is None:
        return ""
    v = value.strip().strip('"').strip("'")
    if v.lower() in ("none", "null", "nil", "undefined", "(none)"):
        return ""
    return v


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
