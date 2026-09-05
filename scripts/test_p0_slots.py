#!/usr/bin/env python3
"""P0: FSM + (optional) two parallel holds on one slot.

Without DATABASE_URL: only transition table.
With DATABASE_URL: two threads INSERT the same slot → 1 success, 1 SlotConflict.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import ALLOWED_TRANSITIONS


def test_fsm() -> None:
    assert "confirmed" in ALLOWED_TRANSITIONS["held"]
    assert "expired" in ALLOWED_TRANSITIONS["held"]
    assert "superseded" in ALLOWED_TRANSITIONS["confirmed"]
    assert "confirmed" not in ALLOWED_TRANSITIONS["cancelled"]
    assert "held" not in ALLOWED_TRANSITIONS["confirmed"]
    # no jump cancelled → confirmed
    assert not ALLOWED_TRANSITIONS["cancelled"]


def test_race() -> None:
    from database.pg import init_pg, pg_enabled
    from database.appointments import SlotConflict, try_hold

    if not init_pg() or not pg_enabled():
        print("SKIP race: DATABASE_URL not set")
        return

    from database.pg import session_scope
    from database.models import Master
    from sqlalchemy import select

    with session_scope() as s:
        master = s.scalar(select(Master).where(Master.is_active.is_(True)))
        if master is None:
            print("SKIP race: no masters seeded")
            return
        name = master.name

    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=9)
    start = start.replace(hour=10, minute=0, second=0)
    end = start + timedelta(minutes=60)

    def one(client_id: int):
        try:
            appt, _created = try_hold(
                master_name=name,
                start_at=start,
                end_at=end,
                client_tg_id=client_id,
                client_name=f"race-{client_id}",
                actor="client",
                duration_min=60,
            )
            return ("ok", str(appt.id), appt.status)
        except SlotConflict:
            return ("conflict", None, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(one, 900001), pool.submit(one, 900002)]
        results = [f.result() for f in as_completed(futs)]

    oks = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "conflict"]
    assert len(oks) == 1, results
    assert len(conflicts) == 1, results
    assert oks[0][2] in ("held", "confirmed"), oks
    print("OK race:", results)


if __name__ == "__main__":
    test_fsm()
    print("OK: fsm")
    test_race()
    print("OK: test_p0_slots")
