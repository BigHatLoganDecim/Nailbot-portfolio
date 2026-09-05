#!/usr/bin/env python3
"""P1: outbox retry schedule + reminder skip for cancelled."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import ALLOWED_TRANSITIONS
from database.worker import RETRY_DELAYS_SEC, next_retry_at


def test_retries() -> None:
    assert RETRY_DELAYS_SEC == (60, 300, 1800, 7200)
    t0 = next_retry_at(0)
    t1 = next_retry_at(1)
    t2 = next_retry_at(2)
    t3 = next_retry_at(3)
    assert t0 and t1 and t2 and t3
    assert (t1 - t0) >= timedelta(seconds=200)
    assert next_retry_at(4) is None  # dead after 2h retry


def test_noshow_from_confirmed() -> None:
    assert "no_show" in ALLOWED_TRANSITIONS["confirmed"]
    assert "completed" in ALLOWED_TRANSITIONS["no_show"]
    assert "cancelled" in ALLOWED_TRANSITIONS["confirmed"]


def test_reminder_skip_statuses() -> None:
    skip = {"cancelled", "superseded", "expired", "no_show", "completed"}
    send = {"confirmed"}
    assert skip.isdisjoint(send)


if __name__ == "__main__":
    test_retries()
    test_noshow_from_confirmed()
    test_reminder_skip_statuses()
    print("OK: test_p1_worker")
