"""Live service catalog: Postgres overlay on knowledge.md.

knowledge.md is the template (names, categories, which masters).
Postgres `services` is the price list the master edits in the bot.
Client /prices and booking must read Postgres when it is available.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from utils import Service

log = logging.getLogger("nailbot.catalog")

PREFERRED_MASTER = "Анна"


def preferred_master_name(kb: Any) -> str:
    masters = getattr(kb, "masters", None) or {}
    if PREFERRED_MASTER in masters:
        return PREFERRED_MASTER
    try:
        return next(iter(masters))
    except StopIteration:
        return PREFERRED_MASTER


def pg_rows_by_title(master_name: str) -> dict[str, dict[str, Any]]:
    if not master_name:
        return {}
    try:
        from database.pg import pg_enabled
        from database.appointments import list_services

        if not pg_enabled():
            return {}
        return {r["title"]: r for r in list_services(master_name)}
    except Exception as e:
        log.warning("pg catalog %s: %s", master_name, e)
        return {}


def format_price_rub(price_minor: int) -> str:
    rub = int(price_minor or 0) // 100
    return f"{rub} ₽"


def format_duration_min(duration_min: int, fallback: str = "") -> str:
    n = int(duration_min or 0)
    if n <= 0:
        return fallback or "—"
    return f"{n} мин"


def overlay_services(
    kb_services: list[Service],
    master_name: str | None = None,
    *,
    active_only: bool = True,
    kb: Any = None,
) -> list[Service]:
    name = master_name or (preferred_master_name(kb) if kb is not None else PREFERRED_MASTER)
    rows = pg_rows_by_title(name)
    if not rows:
        return list(kb_services)
    out: list[Service] = []
    for svc in kb_services:
        row = rows.get(svc.name)
        if row is None:
            out.append(svc)
            continue
        if active_only and not row.get("is_active", True):
            continue
        out.append(replace(
            svc,
            price=format_price_rub(row.get("price_minor") or 0),
            duration=format_duration_min(row.get("duration_min") or 0, fallback=svc.duration),
        ))
    return out


def duration_minutes(
    service_names: list[str],
    kb_services: list[Service],
    master_name: str | None = None,
    *,
    default: int = 60,
) -> int:
    rows = pg_rows_by_title(master_name or PREFERRED_MASTER)
    total = 0
    any_main = False
    for name in service_names or []:
        row = rows.get(name)
        if row is not None:
            n = int(row.get("duration_min") or 0)
            if n > 0:
                total += n
                any_main = True
                continue
        svc = next((s for s in kb_services if s.name == name), None)
        if svc:
            m = re.search(r"(\d+)", svc.duration or "")
            if m:
                total += int(m.group(1))
                any_main = True
                continue
        total += default
        any_main = True
    if not any_main:
        return default
    return total


def price_minor_total(
    service_names: list[str],
    master_name: str | None,
    kb_services: list[Service],
) -> int:
    rows = pg_rows_by_title(master_name or PREFERRED_MASTER)
    total = 0
    for name in service_names or []:
        row = rows.get(name)
        if row is not None:
            total += int(row.get("price_minor") or 0)
            continue
        svc = next((s for s in kb_services if s.name == name), None)
        if not svc:
            continue
        m = re.search(r"(\d[\d\s]*)", (svc.price or "").replace("\xa0", " "))
        if not m:
            continue
        try:
            total += int(m.group(1).replace(" ", "")) * 100
        except ValueError:
            pass
    return total
