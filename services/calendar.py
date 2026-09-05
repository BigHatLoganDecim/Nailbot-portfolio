"""
Чистая обёртка над Google Calendar API.

В этом модуле — только функции работы с календарём. Никаких bot/handlers/state.
Использует Service Account JSON из переменной окружения GOOGLE_SERVICE_ACCOUNT_JSON.

Любая ошибка не пробрасывается наверх — функции возвращают None / пустой список
с записью в лог. Это сознательное решение: календарь — вспомогательный сервис,
если он отвалится — бот должен продолжать работать.

Несколько мастеров могут писать в ОДИН shared-календарь. Тогда:
  - summary: «[Мария] 💅 Маникюр…»
  - description: «Мастер: Мария»
  - freebusy/busy учитывает только события «своего» мастера (+ безымянные блоки салона)
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import logging
import os
import re
from typing import Any

log = logging.getLogger("nailbot.services.calendar")

_MASTER_TAG_RE = re.compile(r"^\[([^\]]+)\]")
_MASTER_LINE_RE = re.compile(r"Мастер:\s*(.+)", re.MULTILINE)


def get_service():
    """
    Возвращает аутентифицированный объект Google Calendar API service или None.
    Кэшировать снаружи — этот вызов делает HTTP-init.
    """
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        return None
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        creds = service_account.Credentials.from_service_account_info(
            _json.loads(sa_json),
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        log.warning("calendar: не удалось подключиться: %s", e)
        return None


def event_master_tag(summary: str, description: str = "") -> str | None:
    """Имя мастера из [Тег] в начале summary или строки «Мастер: …» в description."""
    m = _MASTER_TAG_RE.match(summary or "")
    if m:
        return m.group(1).strip()
    m2 = _MASTER_LINE_RE.search(description or "")
    if m2:
        return m2.group(1).strip().split("\n")[0].strip()
    return None


def event_belongs_to_master(event: dict, master_name: str) -> bool:
    """Событие относится к этому мастеру (для /today, sync, дайджестов)."""
    if event.get("status") == "cancelled":
        return False
    summary = event.get("summary") or ""
    desc = event.get("description") or ""
    tag = event_master_tag(summary, desc)
    if tag is not None:
        return tag == master_name
    return False


def _is_all_day_event(event: dict) -> bool:
    start = event.get("start") or {}
    return "date" in start and "dateTime" not in start


_SALON_WIDE_RE = re.compile(
    r"🔒|закрыт|closed|весь\s*салон|\[салон\]|salon\s*block",
    re.IGNORECASE,
)


def _tag_matches_master(tag: str, master_name: str) -> bool:
    """Сравнение тега события с именем мастера (без регистра, обрезка пробелов)."""
    return (tag or "").strip().casefold() == (master_name or "").strip().casefold()


def _is_salon_wide_block(summary: str, description: str = "") -> bool:
    """Явная блокировка всего салона (без тега мастера)."""
    text = f"{summary or ''}\n{description or ''}"
    return bool(_SALON_WIDE_RE.search(text))


def event_blocks_master(event: dict, master_name: str) -> bool:
    """Событие занимает слот для мастера (busy-check).

    Shared-календарь нескольких мастеров:
    - [Мария] / «Мастер: Мария» → только Мария
    - без тега, **весь день** → НЕ блокируем (праздники и т.п.)
    - без тега, **почасовое** → блокируем всех ТОЛЬКО если явно «🔒/закрыто/салон»
      (иначе личные события Gmail убивают запись при показанных open_slots)
    """
    if event.get("status") == "cancelled":
        return False
    if event.get("transparency") == "transparent":
        return False
    summary = event.get("summary") or ""
    desc = event.get("description") or ""
    tag = event_master_tag(summary, desc)
    if tag is not None:
        return _tag_matches_master(tag, master_name)
    if _is_all_day_event(event):
        return False
    return _is_salon_wide_block(summary, desc)


def _event_interval(event: dict) -> tuple[_dt.datetime, _dt.datetime] | None:
    start = (event.get("start") or {})
    end = (event.get("end") or {})
    start_s = start.get("dateTime") or start.get("date")
    end_s = end.get("dateTime") or end.get("date")
    if not start_s or not end_s:
        return None
    msk = _dt.timezone(_dt.timedelta(hours=3))
    try:
        # all-day: date only
        if "T" not in start_s:
            s = _dt.datetime.fromisoformat(start_s).replace(tzinfo=msk)
            e = _dt.datetime.fromisoformat(end_s).replace(tzinfo=msk)
            return s, e
        s = _dt.datetime.fromisoformat(start_s.replace("Z", "+00:00"))
        e = _dt.datetime.fromisoformat(end_s.replace("Z", "+00:00"))
        return s, e
    except Exception:
        return None


def _to_aware(dt: _dt.datetime, ref: _dt.datetime) -> _dt.datetime:
    """Привести busy-границы к той же tz, что у слота (избегаем ложных пересечений)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ref.tzinfo or _dt.timezone(_dt.timedelta(hours=3)))
    if ref.tzinfo is not None:
        return dt.astimezone(ref.tzinfo)
    return dt


def get_busy_intervals(svc, calendar_id: str,
                       day_start: _dt.datetime, day_end: _dt.datetime,
                       master_name: str | None = None) -> list[tuple]:
    """
    Занятые интервалы (start, end).

    Если master_name задан — через events.list + фильтр по мастеру
    (нужно для shared-календаря нескольких мастеров).
    Иначе — freebusy (как раньше).
    """
    if not svc or not calendar_id:
        return []
    if master_name:
        events = list_events(svc, calendar_id, day_start, day_end)
        busy: list[tuple] = []
        for ev in events:
            if not event_blocks_master(ev, master_name):
                continue
            iv = _event_interval(ev)
            if iv:
                busy.append(iv)
        return busy
    try:
        body = {
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "timeZone": "Europe/Moscow",
            "items": [{"id": calendar_id}],
        }
        result = svc.freebusy().query(body=body).execute()
        busy = result.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        return [(_dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                 _dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                for b in busy]
    except Exception as e:
        log.warning("calendar: freebusy ошибка: %s", e)
        return []


def slot_is_free(busy: list[tuple], start: _dt.datetime, end: _dt.datetime) -> bool:
    """True если интервал [start, end) не пересекается ни с одним из busy."""
    for b_start, b_end in busy:
        try:
            bs = _to_aware(b_start, start)
            be = _to_aware(b_end, start)
            if start < be and end > bs:
                return False
        except TypeError:
            # разные tz / naive — не роняем запись, считаем пересечением
            log.warning("slot_is_free: tz mismatch start=%s busy=%s..%s", start, b_start, b_end)
            continue
    return True


def list_events(svc, calendar_id: str,
                 start: _dt.datetime, end: _dt.datetime) -> list[dict]:
    """Получить все события за период через events().list()."""
    if not svc or not calendar_id:
        return []
    if "заполнить" in (calendar_id or "").lower():
        return []
    try:
        result = svc.events().list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])
    except Exception as e:
        log.warning("calendar: list events: %s", e)
        return []


def create_event(svc, calendar_id: str, event: dict[str, Any]) -> dict | None:
    """Создать событие. Возвращает результат API (с id и htmlLink) или None."""
    if not svc or not calendar_id:
        return None
    try:
        return svc.events().insert(calendarId=calendar_id, body=event).execute()
    except Exception as e:
        log.warning("calendar: insert event: %s", e)
        return None


def update_event(svc, calendar_id: str, event_id: str, event: dict[str, Any]) -> dict | None:
    """Patch event. None on failure."""
    if not svc or not calendar_id or not event_id:
        return None
    try:
        return svc.events().patch(
            calendarId=calendar_id, eventId=event_id, body=event,
        ).execute()
    except Exception as e:
        log.warning("calendar: patch event %s: %s", event_id, e)
        return None


def delete_event(svc, calendar_id: str, event_id: str) -> bool:
    """Удалить событие. True если успешно."""
    if not svc or not calendar_id or not event_id:
        return False
    try:
        svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return True
    except Exception as e:
        log.warning("calendar: delete event %s: %s", event_id, e)
        return False
