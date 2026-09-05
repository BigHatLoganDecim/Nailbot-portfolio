#!/usr/bin/env python3
"""PG overlay for /prices and booking: inactive hidden, live price/duration win."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import Service
from services import catalog


def _svc(name: str, price: str, duration: str) -> Service:
    return Service(name=name, price=price, duration=duration, masters=["Анна"], category="Маникюр")


def test_overlay_uses_pg_and_hides_inactive() -> None:
    kb_svcs = [
        _svc("Маникюр с покрытием", "2800 ₽", "120 мин"),
        _svc("Педикюр", "3300 ₽", "120 мин"),
        _svc("Ремонт 1 ногтя", "150 ₽", "—"),
    ]
    catalog.pg_rows_by_title = lambda name: {  # type: ignore[method-assign]
        "Маникюр с покрытием": {
            "title": "Маникюр с покрытием",
            "price_minor": 300_000,
            "duration_min": 90,
            "is_active": True,
        },
        "Педикюр": {
            "title": "Педикюр",
            "price_minor": 330_000,
            "duration_min": 120,
            "is_active": False,
        },
        "Ремонт 1 ногтя": {
            "title": "Ремонт 1 ногтя",
            "price_minor": 200_00,
            "duration_min": 0,
            "is_active": True,
        },
    }
    live = catalog.overlay_services(kb_svcs, "Анна")
    names = [s.name for s in live]
    assert "Педикюр" not in names, names
    man = next(s for s in live if s.name == "Маникюр с покрытием")
    assert man.price == "3000 ₽", man.price
    assert man.duration == "90 мин", man.duration
    repair = next(s for s in live if s.name == "Ремонт 1 ногтя")
    assert repair.price == "200 ₽", repair.price
    assert repair.duration == "—", repair.duration


def test_duration_minutes_prefers_pg() -> None:
    kb_svcs = [_svc("Маникюр с покрытием", "2800 ₽", "120 мин")]
    catalog.pg_rows_by_title = lambda name: {  # type: ignore[method-assign]
        "Маникюр с покрытием": {
            "title": "Маникюр с покрытием",
            "price_minor": 300_000,
            "duration_min": 90,
            "is_active": True,
        },
    }
    n = catalog.duration_minutes(["Маникюр с покрытием"], kb_svcs, "Анна", default=60)
    assert n == 90, n
    total = catalog.price_minor_total(["Маникюр с покрытием"], "Анна", kb_svcs)
    assert total == 300_000, total


def test_overlay_falls_back_when_pg_empty() -> None:
    kb_svcs = [_svc("Маникюр с покрытием", "2800 ₽", "120 мин")]
    catalog.pg_rows_by_title = lambda name: {}  # type: ignore[method-assign]
    live = catalog.overlay_services(kb_svcs, "Анна")
    assert live[0].price == "2800 ₽"


def test_seed_does_not_assign_existing_price() -> None:
    """Guard: seed loop must not write price/duration/active on existing rows."""
    src = (Path(__file__).resolve().parents[1] / "database" / "appointments.py").read_text(encoding="utf-8")
    assert "existing.duration_min = duration" not in src
    assert "existing.price_minor = price" not in src
    assert "existing.is_active = True" not in src
    assert "do not touch price_minor" in src


if __name__ == "__main__":
    test_overlay_uses_pg_and_hides_inactive()
    test_duration_minutes_prefers_pg()
    test_overlay_falls_back_when_pg_empty()
    test_seed_does_not_assign_existing_price()
    print("OK price catalog")
