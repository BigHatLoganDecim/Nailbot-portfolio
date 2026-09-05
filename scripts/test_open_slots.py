#!/usr/bin/env python3
"""Smoke: open_slots демо-мастеров из knowledge.md."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import load_knowledge, _parse_open_slots


def test_parse():
    d = _parse_open_slots("2026-09-08:14,16; 2026-09-09:18")
    assert d["2026-09-08"] == (14, 16)
    assert d["2026-09-09"] == (18,)


def test_knowledge_maria():
    kb = load_knowledge("knowledge.md")
    y = kb.masters["Мария"]
    assert "2026-09-08" in y.open_slots
    assert set(y.open_slots["2026-09-08"]) == {14, 16}
    assert set(y.open_slots["2026-09-09"]) == {18}
    assert 10 not in y.open_slots["2026-09-08"]


def test_knowledge_anna():
    kb = load_knowledge("knowledge.md")
    d = kb.masters["Анна"]
    assert set(d.open_slots["2026-09-07"]) == {10, 18, 20}
    assert 12 not in d.open_slots["2026-09-07"]


if __name__ == "__main__":
    test_parse()
    test_knowledge_maria()
    test_knowledge_anna()
    print("OK: open_slots")
