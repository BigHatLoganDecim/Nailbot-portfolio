"""
Основной парсер сообщений клиента.

Принцип: быстрый и предсказуемый — никаких внешних вызовов.
Если ничего не распознали — отдаём intent=UNKNOWN, и main.py решает звать ли LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from utils import Knowledge, Service


# ---------------------------------------------------------------------------
# Намерения (intents) — что клиент хочет сделать
# ---------------------------------------------------------------------------

INTENT_BOOK = "book"           # хочет записаться
INTENT_CANCEL = "cancel"       # отменить запись / прервать диалог
INTENT_MY_BOOKINGS = "my"      # «мои записи»
INTENT_PRICES = "prices"       # узнать цены
INTENT_RULES = "rules"         # узнать правила
INTENT_SCHEDULE = "schedule"   # узнать график
INTENT_HELLO = "hello"         # приветствие
INTENT_UNKNOWN = "unknown"     # не распознали


@dataclass
class ParsedMessage:
    intent: str = INTENT_UNKNOWN
    master: str | None = None            # имя из knowledge.md
    services: list[str] = None           # совпадения по названиям услуг
    raw_services_text: str = ""          # исходный текст клиента (fallback для мастера)
    datetime_text: str | None = None     # «завтра в 15:00» (сырая строка)
    phone: str | None = None

    def __post_init__(self) -> None:
        if self.services is None:
            self.services = []


# ---------------------------------------------------------------------------
# Ключевые слова для намерений. Расширяйте смело — это не код, это данные.
# ---------------------------------------------------------------------------

_KEYWORDS = {
    INTENT_BOOK:        ["записаться", "запиши", "запись", "хочу к", "можно к",
                         "записать", "запишите", "хочу записаться", "хочу прийти",
                         "хочу попасть", "забронир", "можно записаться",
                         "запишусь", "пришла бы", "приду", "схожу"],
    INTENT_CANCEL:      ["отмена", "отменить", "отмени", "не надо", "передумал",
                         "передумала", "стоп", "хватит", "не нужно", "не хочу",
                         "забей", "отбой", "cancel"],
    INTENT_MY_BOOKINGS: ["мои записи", "моя запись", "мои визиты", "что у меня",
                         "когда я записан", "когда я записана", "мои визит",
                         "мой визит", "куда я записан"],
    INTENT_PRICES:      ["цен", "стоимость", "сколько стоит", "прайс", "по чём",
                         "почём", "сколько будет", "сколько надо", "тариф"],
    INTENT_RULES:       ["правил", "услови", "оферт"],
    INTENT_SCHEDULE:    ["график", "когда работает", "часы работы", "расписание",
                         "во сколько работает", "до скольки работ",
                         "со скольки работ", "когда открыт", "когда закрыт"],
    INTENT_HELLO:       ["привет", "здравствуй", "добрый день", "добрый вечер",
                         "доброе утро", "hi", "hello", "хай", "здарова",
                         "приветствую", "доброго времени"],
}


def parse(text: str, kb: Knowledge) -> ParsedMessage:
    """Главная точка входа парсера."""
    out = ParsedMessage(raw_services_text=text)
    if not text:
        return out

    lower = text.lower().strip()

    out.intent = _detect_intent(lower)
    # Динамическое распознавание мастеров — если в kb их нет, fallback на хардкод.
    out.master = _detect_master_for_kb(lower, kb) or _detect_master(lower)
    out.services = _detect_services(lower, kb.services, kb.service_synonyms)
    out.datetime_text = _detect_datetime(lower)
    out.phone = _detect_phone(text)

    # Если в сообщении явно есть мастер/услуга/дата — это похоже на запись.
    if out.intent == INTENT_UNKNOWN and (out.master or out.services or out.datetime_text):
        out.intent = INTENT_BOOK

    return out


# ---------------------------------------------------------------------------
# Внутренние функции
# ---------------------------------------------------------------------------

def _detect_intent(lower: str) -> str:
    for intent, words in _KEYWORDS.items():
        if any(w in lower for w in words):
            return intent
    return INTENT_UNKNOWN


def _detect_master(lower: str) -> str | None:
    """
    LEGACY fallback, когда нет kb. Демо-салон: Анна / Мария.
    Для клона используй _detect_master_for_kb — имена берутся из knowledge.md.
    """
    if re.search(r"\bанн[аеёуы]\w*\b", lower) or re.search(r"\bан[яею]\b", lower):
        return "Анна"
    if re.search(r"\bанечк", lower) or re.search(r"\bанют", lower):
        return "Анна"
    if re.search(r"\bмари[яиею]\w*\b", lower) or re.search(r"\bмаш[аеиу]\w*\b", lower):
        return "Мария"
    return None


def _name_patterns(word: str) -> list[str]:
    """Регексы для всех падежных форм имени.

    Стратегия:
      - Имя «Анна» (твёрдая а в конце) → стэм `анн` ловит «анну», «анной».
      - Имя «Мария» (гласная в конце) → стэм + падежи «марии», «марию».
    """
    n = word.lower().strip()
    if not n:
        return []
    patterns = [rf"\b{re.escape(n)}[а-яё]{{0,4}}\b"]
    if n.endswith(("а", "я", "ь")) and len(n) > 2:
        stem = n[:-1]
        patterns.append(rf"\b{re.escape(stem)}[а-яё]{{1,4}}\b")
    return patterns


def _detect_master_for_kb(lower: str, kb: Knowledge) -> str | None:
    """
    Динамическое распознавание мастера: строит regex из имён в kb.masters
    плюс aliases каждого мастера. Учитывает русскую морфологию (падежи).

    Если в knowledge.md задан мастер «Анна» с aliases «аня, анечка» —
    распознает падежи и уменьшительные.

    Возвращает None если ни одно имя не совпало — тогда вызывающий
    может fallback'нуться на _detect_master (legacy hardcoded).
    """
    if not kb.masters:
        return None
    for name, master in kb.masters.items():
        for pat in _name_patterns(name):
            if re.search(pat, lower):
                return name
        for alias in master.aliases:
            for pat in _name_patterns(alias):
                if re.search(pat, lower):
                    return name
    return None


# LEGACY синонимы услуг под маникюрный салон. Используется как fallback если
# в knowledge.md нет секции `## Синонимы услуг`. Для шаблонизации под другие
# бизнесы (барбершоп / автосервис / клиника) — задавай synonyms в knowledge.md.
_SERVICE_SYNONYMS = {
    "ногти":         "маникюр",
    "ноготки":       "маникюр",
    "ручки":         "маникюр",
    "руки":          "маникюр",
    "гель":          "гель-лак",
    "гель-лак":      "гель-лак",
    "гельлак":       "гель-лак",
    "покрытие":      "покрытие",
    "снять":         "снятие",
    "снятие":        "снятие",
    "педик":         "педикюр",
    "ножки":         "педикюр",
    "ноги":          "педикюр",
    "стопы":         "педикюр",
    "дизайн":        "дизайн",
    "рисунок":       "дизайн",
    "стразы":        "дизайн",
}


def _detect_services(lower: str, known: list[Service],
                      synonyms: dict[str, str] | None = None) -> list[str]:
    """
    Ищем услуги из knowledge.md в тексте клиента.

    Принцип: НЕ навязывать клиенту лишнего. Если по одному ключевому слову
    подходит несколько услуг (например, «маникюр» матчит и «Маникюр классический»,
    и «Маникюр с покрытием гель-лак») — берём только самую базовую (короткую) услугу.
    Клиент потом сам уточнит на этапе подтверждения или в комментарии мастеру.

    Стратегия:
      1) Группируем услуги по «корневому слову» из названия.
      2) Для каждого корня, упомянутого в тексте, выбираем ОДНУ услугу
         (самую короткую = самую базовую).
      3) Доп. сигналы из синонимов («ногти» → «маникюр», «ножки» → «педикюр»)
         добавляют корни в список упомянутых.
      4) Если клиент упомянул конкретные слова из расширения («гель-лак»,
         «покрытие», «дизайн») — добавляем соответствующую услугу к базовой.
    """
    # Используем synonyms из knowledge.md если задан, иначе legacy fallback.
    effective_synonyms = synonyms if synonyms else _SERVICE_SYNONYMS

    # Корневое слово услуги — первое значащее слово (>= 4 букв) её названия.
    # «Маникюр классический» → «маникюр», «Снятие покрытия» → «снятие».
    def root_of(name: str) -> str | None:
        words = [w.lower() for w in re.findall(r"[А-Яа-яёЁ\-]+", name) if len(w) >= 4]
        return words[0] if words else None

    # 1. Группируем услуги по корню
    by_root: dict[str, list[Service]] = {}
    for svc in known:
        r = root_of(svc.name)
        if r:
            by_root.setdefault(r, []).append(svc)

    # 2. Какие корни упомянул клиент — прямо или через синонимы
    mentioned_roots: set[str] = set()
    for root in by_root.keys():
        if root in lower:
            mentioned_roots.add(root)
    for syn, canon in effective_synonyms.items():
        if syn in lower and canon in by_root:
            mentioned_roots.add(canon)

    # 3. Для каждого упомянутого корня — берём САМУЮ КОРОТКУЮ услугу (базовый вариант).
    found: list[str] = []
    for root in mentioned_roots:
        variants = by_root[root]
        explicit = None
        for svc in variants:
            extra_words = [w.lower() for w in re.findall(r"[А-Яа-яёЁ\-]+", svc.name)
                           if len(w) >= 4 and w.lower() != root]
            if extra_words and any(w in lower for w in extra_words):
                explicit = svc
                break
        if not explicit:
            for syn, canon in effective_synonyms.items():
                if syn in lower and canon != root:
                    for svc in variants:
                        if canon in svc.name.lower():
                            explicit = svc
                            break
                    if explicit:
                        break
        chosen = explicit or min(variants, key=lambda s: len(s.name))
        if chosen.name not in found:
            found.append(chosen.name)

    return found


# Детектор даты/времени. Не разбираем дату строго — отдаём текстом мастеру,
# бот лишь убеждается, что фраза похожа на дату/время.
_DATE_HINTS = [
    # относительные дни
    r"\bсегодня\b", r"\bзавтра\b", r"\bпослезавтра\b", r"\bпослепослезавтра\b",
    r"\bна\s+днях?\b",
    # дни недели (с любыми окончаниями: «в понедельник», «понедельника»)
    r"\bпонедельник\w*\b", r"\bвторник\w*\b", r"\bсред[ауыоe]\w*\b",
    r"\bчетверг\w*\b", r"\bпятниц\w*\b", r"\bсуббот\w*\b", r"\bвоскресен\w*\b",
    r"\bвыходн\w+\b", r"\bбудн\w+\b",
    # «на этой/следующей неделе», «через неделю/две»
    r"\bна\s+(?:этой|след(?:ующей)?|будущей)\s+неделе\b",
    r"\bчерез\s+\d*\s*(?:недел|день|дня|дней|час|часа|часов|месяц)\w*\b",
    r"\bна\s+след(?:ующ\w+)?\b",
    # точное время: 15:00, 15.00, 15-00, 9 30
    r"\b\d{1,2}[:.\-]\d{2}\b",
    # «в 5», «в 17 часов», «к 18», «к семи»
    r"\bв\s+\d{1,2}(?:\s*(?:час|ч)\w*)?\b",
    r"\bк\s+\d{1,2}(?:\s*(?:час|ч)\w*)?\b",
    r"\b\d{1,2}\s*(?:час|ч)\w*\b",
    r"\bк\s+(?:одн|двум|трем|трём|четырём|пяти|шести|семи|восьми|девяти|десяти|одиннадцати|двенадцати)\w*\b",
    # числа месяцев: «5 июня», «15 декабря»
    r"\b\d{1,2}\s*(?:-го|го)?\s+(?:январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*",
    # формат даты: 12.05, 12/05, 12.05.2026
    r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b",
    # время суток
    r"\bутром\b", r"\bднём\b", r"\bднем\b", r"\bвечером\b", r"\bночью\b",
    r"\bв\s+обед\w*\b", r"\bпосле\s+обеда\b", r"\bдо\s+обеда\b",
    r"\bпораньше\b", r"\bпопозже\b",
    # «в первой/второй половине дня»
    r"\bв\s+перв\w+\s+половин\w+\b", r"\bво\s+втор\w+\s+половин\w+\b",
]
_DATE_RE = re.compile("|".join(_DATE_HINTS), re.IGNORECASE)


def _detect_datetime(lower: str) -> str | None:
    if _DATE_RE.search(lower):
        return lower  # отдаём как есть — мастер сам поймёт
    return None


def _detect_phone(text: str) -> str | None:
    """
    Распознать российский номер в любом формате:
      +7 999 123 45 67, 8(999)123-45-67, 79991234567, 89991234567,
      9991234567 (без префикса), и любые комбинации скобок/пробелов/дефисов/точек.
    Возвращает в едином формате: +7XXXXXXXXXX.

    Стратегия: ищем телефонный паттерн как «слово» — последовательность из
    цифр и обычных телефонных разделителей (пробел/дефис/точка/скобки)
    длиной 10+ символов. Это отделяет реальный телефон от случайных
    цифр вроде времени «14:00» в той же строке.
    """
    if not text:
        return None
    # Регулярка для телефонного «слова» — начинается опционально с +, потом
    # цифры и/или (), -, . пробелы. Должна содержать минимум 10 цифр.
    # \D|\b — границы: пробел, пунктуация, начало/конец.
    phone_re = re.compile(
        r"(?:^|[\s,;:!?])(\+?\d[\d\s\-().]{8,}\d)(?=[\s,;:!?]|$)"
    )
    candidates = []
    for m in phone_re.finditer(text):
        token = m.group(1)
        digits = re.sub(r"\D", "", token)
        if 10 <= len(digits) <= 11:
            candidates.append(digits)
    # Если паттерн не сработал — пробуем fallback: ищем 10-11 цифр подряд
    # (на случай если телефон без разделителей и без границ — «+79991234567»).
    if not candidates:
        for m in re.finditer(r"\+?(\d{10,11})\b", text):
            d = m.group(1)
            if 10 <= len(d) <= 11:
                candidates.append(d)
    for digits in candidates:
        # 10 цифр без префикса — добавляем +7
        if len(digits) == 10 and digits[0] == "9":
            return "+7" + digits
        # 11 цифр начинающихся на 7 или 8 — нормализуем в +7
        if len(digits) == 11 and digits[0] in ("7", "8") and digits[1] == "9":
            return "+7" + digits[1:]
    return None
