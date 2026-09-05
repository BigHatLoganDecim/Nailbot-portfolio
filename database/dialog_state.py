"""
DialogStateStore — short-lived conversational state only.

This replaces the old general-purpose StateStore for dialog purposes.

Characteristics:
- Redis is MANDATORY (no silent fallback for critical flows).
- Short TTL (default 36 hours).
- Used only for active booking dialogs, oracle sessions, etc.
- Long-term data (bookings, client cards) lives in SQLite via repositories.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("nailbot.dialog_state")


class DialogStateStore:
    """
    Strict Redis-backed store for dialog/session state.

    If Redis is unavailable, critical operations should fail fast
    (as per project requirements).
    """

    def __init__(self, redis_client, ttl_seconds: int = 36 * 3600):
        if redis_client is None:
            raise RuntimeError(
                "DialogStateStore requires a working Redis client. "
                "Redis is mandatory for dialog state."
            )
        self._redis = redis_client
        self.ttl = ttl_seconds
        self._prefix = "nailbot:dialog:"

    def _key(self, user_id: int) -> str:
        return f"{self._prefix}{user_id}"

    def get(self, user_id: int) -> dict[str, Any]:
        try:
            raw = self._redis.get(self._key(user_id))
            if not raw:
                return {}
            return json.loads(raw)
        except Exception as e:
            log.error("DialogStateStore.get failed for %s: %s", user_id, e)
            # Fail fast for dialog state — do not silently return {}
            raise

    def set(self, user_id: int, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            log.error("DialogStateStore.set: non-dict value for %s", user_id)
            return

        try:
            payload = json.dumps(state, ensure_ascii=False)
            self._redis.setex(self._key(user_id), self.ttl, payload)
        except Exception as e:
            log.error("DialogStateStore.set failed for %s: %s", user_id, e)
            raise

    def clear(self, user_id: int) -> None:
        try:
            self._redis.delete(self._key(user_id))
        except Exception as e:
            log.warning("DialogStateStore.clear failed: %s", e)

    def track_user(self, user_id: int) -> None:
        """Track user for analytics (all_users equivalent)."""
        try:
            self._redis.sadd("nailbot:known_users", user_id)
        except Exception as e:
            log.warning("DialogStateStore.track_user failed: %s", e)
