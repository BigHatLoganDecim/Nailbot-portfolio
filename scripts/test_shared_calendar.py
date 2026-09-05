#!/usr/bin/env python3
"""Smoke: shared-календарь — теги мастеров и busy-filter."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.calendar import event_belongs_to_master, event_blocks_master, event_master_tag


def test_tags():
    assert event_master_tag("[Мария] 💅 маникюр", "") == "Мария"
    assert event_master_tag("💅 x", "Мастер: Анна\nТелефон: 1") == "Анна"
    assert event_master_tag("🔒 Закрыто", "") is None


def test_belongs():
    y = {"summary": "[Мария] 💅 pedi", "description": "Мастер: Мария"}
    d = {"summary": "[Анна] 💅 art", "description": "Мастер: Анна"}
    assert event_belongs_to_master(y, "Мария")
    assert not event_belongs_to_master(y, "Анна")
    assert event_belongs_to_master(d, "Анна")


def test_blocks():
    y = {"summary": "[Мария] 🔒 Закрыто", "description": "Мастер: Мария",
         "start": {"dateTime": "2026-09-08T14:00:00+03:00"}}
    salon = {"summary": "Салон закрыт 2ч", "description": "",
             "start": {"dateTime": "2026-09-08T12:00:00+03:00"},
             "end": {"dateTime": "2026-09-08T14:00:00+03:00"}}
    personal = {"summary": "Врач / личное", "description": "",
                "start": {"dateTime": "2026-09-08T14:00:00+03:00"},
                "end": {"dateTime": "2026-09-08T15:00:00+03:00"}}
    allday = {"summary": "Праздник", "description": "",
              "start": {"date": "2026-09-08"}, "end": {"date": "2026-09-09"}}
    assert event_blocks_master(y, "Мария")
    assert not event_blocks_master(y, "Анна")
    # явный блок салона без тега — для всех
    assert event_blocks_master(salon, "Анна")
    assert event_blocks_master(salon, "Мария")
    # личное без тега — НЕ блокирует (иначе open_slots «есть», запись «нельзя»)
    assert not event_blocks_master(personal, "Мария")
    assert not event_blocks_master(personal, "Анна")
    # all-day без тега — НЕ блокирует (shared calendar)
    assert not event_blocks_master(allday, "Мария")
    assert not event_blocks_master(allday, "Анна")
    # регистр тега
    assert event_blocks_master(
        {"summary": "[мария] 💅 x", "start": {"dateTime": "2026-09-08T10:00:00+03:00"}},
        "Мария",
    )


if __name__ == "__main__":
    test_tags()
    test_belongs()
    test_blocks()
    print("OK: shared calendar filters")
