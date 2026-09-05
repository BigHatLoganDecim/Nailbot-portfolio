#!/usr/bin/env python3
"""Быстрая проверка инстанса. Usage: python scripts/ops_health_check.py [url]"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

DEFAULT_URL = os.environ.get("HEALTH_URL", "http://127.0.0.1:8080/health")


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL).rstrip("/")
    if not url.endswith("/health"):
        url += "/health"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"FAIL: {url} — {e}")
        return 1

    ok = data.get("status") == "ok"
    print(f"{'OK' if ok else 'WARN'}  {url}")
    for key in ("database", "redis", "llm", "scheduler"):
        print(f"  {key}: {data.get(key, '?')}")
    kb = data.get("knowledge") or {}
    print(f"  masters: {kb.get('masters')}  services: {kb.get('services')}  promos: {kb.get('promo_codes')}")
    print(f"  scheduled_jobs: {data.get('scheduled_jobs')}  uptime_sec: {data.get('uptime_sec')}")
    if not ok:
        print("  raw:", json.dumps(data, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())