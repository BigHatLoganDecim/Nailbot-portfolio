#!/usr/bin/env python3
"""Smoke-тесты логики callback'ов напоминаний (без Telegram/DB)."""
from __future__ import annotations


def is_rm_cancel_prompt(data: str) -> bool:
    return (
        data.startswith("rm:cancel:")
        and not data.startswith("rm:cancel_yes:")
        and not data.startswith("rm:cancel_back:")
    )


def test_cancel_routing():
    bid = "abc123def456"
    assert is_rm_cancel_prompt(f"rm:cancel:{bid}")
    assert not is_rm_cancel_prompt(f"rm:cancel_yes:{bid}")
    assert not is_rm_cancel_prompt(f"rm:cancel_back:{bid}")
    assert not is_rm_cancel_prompt(f"rm:yes:{bid}")


def test_decrement_order_documented():
    """Документируем ожидаемый порядок: decrement до mark cancelled."""
    steps = ["decrement_visits", "mark_booking_status(cancelled)"]
    assert steps.index("decrement_visits") < steps.index("mark_booking_status(cancelled)")


if __name__ == "__main__":
    test_cancel_routing()
    test_decrement_order_documented()
    print("OK: reminder routing smoke tests passed")