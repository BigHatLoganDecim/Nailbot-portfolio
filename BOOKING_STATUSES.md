# Booking Statuses Reference

This document describes all possible statuses for bookings in the long-term database and their impact on business counters.

## Possible Statuses

| Status        | Meaning                                      | Counts as Visit? | Design Bonus Restored on Cancel? | Typical Transitions From/To |
|---------------|----------------------------------------------|------------------|----------------------------------|-------------------------------|
| `confirmed`   | Booking is active and scheduled in the future | Yes (when completed) | Usually yes (if cancelled by client) | Created via `finalize_booking` → `completed`, `cancelled`, `rescheduled`, `no_show` |
| `completed`   | Visit successfully took place                | Yes              | N/A (already happened)           | From `confirmed`              |
| `cancelled`   | Booking was cancelled (by client or system)  | No               | Yes (in client cancel, master cancel, mass cancels) | From `confirmed`              |
| `rescheduled` | Client moved the booking to a different time | No (old record)  | **No** (intentional)             | From `confirmed` → new `confirmed` |
| `no_show`     | Client did not show up for the appointment   | Usually No       | Case-by-case                     | From `confirmed`              |

## Counter Impact Rules

### `total_visits`
- Only bookings with status `confirmed` or `completed` should contribute to a client's `total_visits`.
- When a booking moves to `cancelled`, `rescheduled`, or `no_show`, `total_visits` must be decremented (except in specific business cases).
- The `repair_visits()` method recalculates this value from actual confirmed/completed records.

### `design_bonuses` (referral bonuses)
- A design bonus is consumed when a new client books using a referral link (`bonus_used = true` at confirmation).
- **On client cancellation**: The bonus is restored to the client (see `_safe_cancel_booking` and cancel handlers).
- **On master-initiated cancellation** (vacation, sick, block): The bonus is restored.
- **On reschedule**: The bonus is **not** restored (the visit is still happening, just at another time).
- The `repair_client_visits(..., repair_bonuses=True)` command can recalculate correct bonus balances.

### `referrals_brought`
- Incremented for the referrer when their referral successfully books for the first time.
- Not decremented on cancellation or reschedule of the referred client's booking.

## Recommended Queries

```sql
-- Clients with inconsistent visit counts
SELECT c.user_id, c.total_visits as redis_like, 
       COUNT(b.id) as actual_confirmed
FROM clients c
LEFT JOIN bookings b ON b.user_id = c.user_id AND b.status IN ('confirmed', 'completed')
GROUP BY c.user_id
HAVING c.total_visits != COUNT(b.id);

-- Bookings that used design bonuses
SELECT * FROM bookings 
WHERE notes LIKE '%дизайн-бонус%' 
   OR notes LIKE '%referrer_bonus%';
```

## Status Lifecycle Notes

- A booking should almost never go directly from `confirmed` to `completed` without going through the visit.
- `rescheduled` is a terminal state for the *old* booking record. The new time creates a fresh `confirmed` record.
- Mass cancellations (vacation/sick) use status `cancelled` and trigger bonus restoration + visit decrement.
- Admin can force-reconcile a user's visible bookings (remove ghosts from abandoned reschedules etc.) with `/repair <user_id> bookings` (or `full`). "Мои записи" also auto-reconciles on view and shows "🔄 (переносится)" marker while a transfer is in progress.

---
Last updated: 2026-05 (reschedule display + repair bookings reconcile added)
