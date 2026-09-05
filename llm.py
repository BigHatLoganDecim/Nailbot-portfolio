"""
Универсальный LLM-клиент через OpenAI-совместимый API.

Поддерживает любого провайдера со стандартом OpenAI:
  - Groq (по умолчанию)
  - VseGPT (российский reseller — рубли, DeepSeek/Claude/GPT)
  - OpenRouter (агрегатор зарубежных, USDT)
  - DeepSeek API напрямую
  - YandexGPT через OpenAI-совместимую обёртку
  - Любого другого OpenAI-compat

Переключение — через env-переменные:
  LLM_API_KEY      — ключ (если не задан, fallback на GROQ_API_KEY)
  LLM_BASE_URL     — endpoint (если не задан → https://api.groq.com/openai/v1)
  LLM_MODEL_PARSER — модель для парсера записей (быстрая, цена низкая)
  LLM_MODEL_ORACLE — модель для «Оракула» (нужна сила и эмпатия)
  LLM_MODEL_SUMMARY — модель для саммари мастеру
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("nailbot.llm")


# Запасная модель по умолчанию — если основная (per-feature) вернёт ошибку
# (снята с обслуживания / 400 model not found / rate-limit по конкретной
# модели), _chat автоматически ретраит на ней. Переопределяется через
# конструктор (fallback_model) и env LLM_FALLBACK_MODEL (см. utils.Config).
DEFAULT_FALLBACK_MODEL = "llama-3.3-70b-versatile"

# Подстроки ошибок, при которых fallback БЕСПОЛЕЗЕН — не тратим время/токены
# на повтор (проблема в ключе/доступе, а не в модели).
_NO_RETRY_MARKERS = (
    "401", "invalid_api_key", "authentication", "unauthorized",
    "permission", "no such organization", "insufficient_quota",
)


SYSTEM_PROMPT = """Ты — парсер запросов для бота записи в студию услуг.
Доступные мастера: Анна, Мария.

РАЗРЕШЁННЫЕ ТЕМЫ (только эти):
- запись на процедуру
- цены на услуги
- что входит в услугу
- правила салона
- материалы и покрытия
- график работы мастеров
- адрес и контакты

ВСЁ ОСТАЛЬНОЕ — intent="off_topic": болтовня, шутки, личные вопросы, флирт,
политика, советы по жизни, кулинария, погода, любые темы вне маникюра.

СТРОГИЕ ПРАВИЛА:
1. Ты только извлекаешь структуру — ты НЕ отвечаешь на вопросы и НЕ ведёшь беседу.
2. Если сообщение содержит И болтовню, И запрос на запись — intent="book" (запись важнее).
3. Если клиент флиртует, угрожает, жалуется на жизнь — всегда off_topic.
4. Никогда не угадывай услуги, мастера или время, если они не упомянуты явно.
5. Никакого текста кроме JSON. Никаких пояснений, никаких markdown-блоков.

ОТВЕЧАЙ СТРОГО ОДНИМ JSON-объектом:
{
  "intent": "book" | "cancel" | "my" | "prices" | "rules" | "schedule" | "hello" | "off_topic" | "unknown",
  "master": "Анна" | "Мария" | null,
  "services": [строки — названия процедур, как сказал клиент],
  "datetime_text": строка или null,
  "phone": строка или null
}
"""


SUMMARY_PROMPT = """Ты — помощник мастера маникюра. Сформируй очень короткий отчёт
о клиенте по итогам разговора. Без эмодзи, без воды, по делу: 1–3 предложения.

Формат:
- 1 предложение: чего хотел клиент (мастер, процедура, дата, телефон).
- 1 предложение: чем закончилось (записан / передумал / не дошли до подтверждения).
- При необходимости 1 предложение: что бросилось в глаза (особые пожелания, оговорки).

Никакого приветствия, никаких советов. Только факты. Отвечай ОДНОЙ строкой
русского текста без переносов."""


class LLMClient:
    """
    OpenAI-совместимый LLM-клиент с per-feature моделями.

    Per-feature модели позволяют дёшёвую модель ставить на парсинг записей
    (типовой шаблон), а топовую — на «Оракула» (живые тексты).
    """

    def __init__(self,
                  api_key: str,
                  base_url: str = "https://api.groq.com/openai/v1",
                  model_parser: str = "llama-3.1-8b-instant",
                  model_oracle: str = "llama-3.3-70b-versatile",
                  model_summary: str = "llama-3.1-8b-instant",
                  timeout_sec: float = 15.0,
                  fallback_model: str | None = None):
        self.api_key = api_key
        self.base_url = base_url
        self.model_parser = model_parser
        self.model_oracle = model_oracle
        self.model_summary = model_summary
        self.timeout = timeout_sec
        self.fallback_model = (fallback_model or "").strip() or DEFAULT_FALLBACK_MODEL
        self._client = None
        self._client_failed = False
        self.usage_callback = None
        self.last_usage = {"in": 0, "out": 0, "model": ""}

        if not api_key:
            log.info("LLM: API ключ не задан — LLM отключён, бот работает на парсере")
        else:
            # Lazy init OpenAI client — экономия RAM на free, пока LLM не вызван
            log.info("LLM: lazy-init (ключ есть, клиент создастся при первом вызове)")

    def _ensure_client(self) -> bool:
        """Создать OpenAI-клиент при первом использовании. True если готов."""
        if self._client is not None:
            return True
        if self._client_failed or not self.api_key:
            return False
        try:
            from openai import OpenAI  # type: ignore
            self._client = OpenAI(
                api_key=self.api_key, base_url=self.base_url, timeout=self.timeout,
            )
            log.info("LLM: клиент создан → %s (parser=%s, oracle=%s)",
                     self.base_url, self.model_parser, self.model_oracle)
            return True
        except Exception as e:
            log.warning("LLM: не удалось инициализировать клиент (%s) — отключён", e)
            self._client_failed = True
            return False

    def _track(self, resp, model_used: str) -> None:
        """Извлекает usage из ответа и зовёт колбэк (если задан)."""
        try:
            usage = getattr(resp, "usage", None)
            in_t = int(getattr(usage, "prompt_tokens", 0)) if usage else 0
            out_t = int(getattr(usage, "completion_tokens", 0)) if usage else 0
            self.last_usage = {"in": in_t, "out": out_t, "model": model_used}
            if self.usage_callback:
                self.usage_callback(self.last_usage)
        except Exception as e:
            log.debug("LLM._track: %s", e)

    @property
    def enabled(self) -> bool:
        """True если есть ключ (клиент может быть ещё не создан — lazy)."""
        return bool(self.api_key) and not self._client_failed

    def _chat(self, model: str, messages: list[dict],
               temperature: float = 0.5, max_tokens: int = 600,
               extra: dict | None = None,
               timeout: float | None = None) -> str | None:
        """Универсальный вызов chat completions. Возвращает текст ответа или None."""
        if not self._ensure_client():
            return None
        params: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if extra:
            params.update(extra)
        # Если задан timeout явно — он переопределяет client default
        if timeout is not None:
            params["timeout"] = timeout
        try:
            resp = self._client.chat.completions.create(**params)
            self._track(resp, model)
            return (resp.choices[0].message.content or "").strip() or None
        except Exception as e:
            log.warning("LLM._chat (%s): %s", model, e)
            # Авто-fallback: основная модель сломалась (снята с обслуживания,
            # 400 model not found, rate-limit по конкретной модели) — пробуем
            # запасную. НЕ ретраим при ошибках доступа (ключ/auth/квота аккаунта)
            # и если уже сами на fallback-модели.
            err = str(e).lower()
            if (model != self.fallback_model
                    and not any(m in err for m in _NO_RETRY_MARKERS)):
                log.warning("LLM._chat: пробую запасную модель %s вместо %s",
                            self.fallback_model, model)
                fb_params = dict(params)
                fb_params["model"] = self.fallback_model
                try:
                    resp = self._client.chat.completions.create(**fb_params)
                    self._track(resp, self.fallback_model)
                    return (resp.choices[0].message.content or "").strip() or None
                except Exception as e2:
                    log.warning("LLM._chat fallback (%s): %s", self.fallback_model, e2)
            return None

    def summarize(self, booking: dict[str, Any], outcome: str,
                  transcript: list[str] | None = None) -> str | None:
        if not self.enabled:
            return None
        lines = [
            f"Итог разговора: {outcome}",
            f"Мастер: {booking.get('master') or '—'}",
            f"Процедуры (распознанные): {', '.join(booking.get('services') or []) or '—'}",
            f"Сказал клиент (свободный текст): {booking.get('services_raw') or '—'}",
            f"Когда: {booking.get('datetime') or '—'}",
            f"Телефон: {booking.get('phone') or '—'}",
        ]
        if transcript:
            lines.append("Последние сообщения клиента:")
            lines.extend(f"- {t}" for t in transcript[-5:])
        user_msg = "\n".join(lines)
        return self._chat(
            self.model_summary,
            [{"role": "system", "content": SUMMARY_PROMPT},
             {"role": "user", "content": user_msg[:2000]}],
            temperature=0.2, max_tokens=200,
        )

    def psy_chat(self, messages: list[dict],
                  model_override: str | None = None) -> str | None:
        """
        Многооборотный чат для «Оракула». Температура выше, max_tokens больше —
        чтобы персонажи звучали живо. Timeout 30 сек — модель может думать долго.
        """
        return self._chat(
            model_override or self.model_oracle,
            messages,
            temperature=0.75, max_tokens=900,
            extra={"top_p": 0.95, "presence_penalty": 0.3, "frequency_penalty": 0.2},
            timeout=30.0,
        )

    def parse(self, user_text: str) -> dict[str, Any] | None:
        """Парсинг сообщения клиента — JSON-схема."""
        content = self._chat(
            self.model_parser,
            [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user", "content": user_text[:1000]}],
            temperature=0.0, max_tokens=300,
        )
        if not content:
            return None
        return _extract_json(content)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        text = brace.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("LLM: ответ не парсится как JSON: %r", text[:200])
        return None
