#!/usr/bin/env python3
"""
Export legacy data from Redis to a portable JSON file.

This script is part of the data migration effort. Its purpose is to create
a safe, portable snapshot of all important data currently stored in Redis
(user states, vacations, etc.) so it can be:
  - Backed up safely
  - Used for testing the migration script via --simulate --data-file
  - Used as input for the actual one-time migration

Usage examples:
    python scripts/export_legacy_data.py --output backups/legacy_export.json
    python scripts/export_legacy_data.py --output backups/legacy_export.json.gz --gzip
    python scripts/export_legacy_data.py --users 123456,789012 --output user_123456.json
"""

import argparse
import gzip
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import Config, StateStore

log = logging.getLogger("export_legacy")


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def export_legacy_data(
    output_path: str,
    specific_users: list[int] | None = None,
    use_gzip: bool = False,
    include_extras: bool = True,
) -> None:
    """
    Main export function.
    """
    config = Config.from_env()
    if not config.redis_url:
        raise RuntimeError("REDIS_URL is required for export")

    state_store = StateStore(redis_url=config.redis_url)

    if not state_store.redis:
        raise RuntimeError("Could not connect to Redis")

    log.info("Starting legacy data export...")

    # 1. Get list of users to export
    if specific_users:
        user_ids = specific_users
        log.info(f"Exporting specific users: {user_ids}")
    else:
        user_ids = state_store.all_users()
        log.info(f"Found {len(user_ids)} users in legacy state store")

    # 2. Export user states
    clients_data: dict[str, Any] = {}
    for uid in user_ids:
        try:
            state = state_store.get(uid) or {}
            if state:  # Only export users who have actual data
                clients_data[str(uid)] = state
        except Exception as e:
            log.warning(f"Failed to export user {uid}: {e}")

    log.info(f"Exported {len(clients_data)} user states")

    # 3. Export important global keys (vacations, etc.)
    extras: dict[str, Any] = {}
    if include_extras:
        # Vacation data
        try:
            for key in state_store.redis.scan_iter("nailbot:vacation:*"):
                if isinstance(key, bytes):
                    key = key.decode()
                val = state_store.redis.get(key)
                if isinstance(val, bytes):
                    val = val.decode()
                extras[key] = val
        except Exception as e:
            log.warning(f"Failed to export vacation keys: {e}")

        # Psychologist / Oracle related data (if any)
        try:
            for key in state_store.redis.scan_iter("nailbot:psy:*"):
                if isinstance(key, bytes):
                    key = key.decode()
                val = state_store.redis.get(key)
                if isinstance(val, bytes):
                    val = val.decode()
                extras[key] = val
        except Exception as e:
            log.warning(f"Failed to export psy keys: {e}")

        # News pack cache
        try:
            news_pack = state_store.redis.get("nailbot:news:pack")
            if news_pack:
                if isinstance(news_pack, bytes):
                    news_pack = news_pack.decode()
                extras["nailbot:news:pack"] = news_pack
        except Exception:
            pass

    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "source": "redis_legacy_export",
        "clients_count": len(clients_data),
        "clients": clients_data,
        "redis_extras": extras,
    }

    # 4. Write to file (with optional gzip)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if use_gzip or str(output_file).endswith(".gz"):
        if not str(output_file).endswith(".gz"):
            output_file = output_file.with_suffix(output_file.suffix + ".gz")

        with gzip.open(output_file, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    else:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    size_mb = output_file.stat().st_size / (1024 * 1024)
    log.info(f"Export completed successfully.")
    log.info(f"File: {output_file}")
    log.info(f"Size: {size_mb:.2f} MB")
    log.info(f"Users exported: {len(clients_data)}")


def main():
    parser = argparse.ArgumentParser(description="Export legacy Redis data to JSON for migration")
    parser.add_argument("--output", "-o", required=True, help="Output file path (use .gz extension for compression)")
    parser.add_argument("--users", help="Comma-separated list of user_ids to export (default: all)")
    parser.add_argument("--no-extras", action="store_true", help="Do not export additional keys (vacations, psy, etc.)")
    parser.add_argument("--log-level", default="INFO", help="Logging level")

    args = parser.parse_args()

    setup_logging(args.log_level)

    specific_users = None
    if args.users:
        try:
            specific_users = [int(u.strip()) for u in args.users.split(",")]
        except ValueError:
            print("Error: --users must be a comma-separated list of integers")
            sys.exit(1)

    use_gzip = str(args.output).endswith(".gz")

    try:
        export_legacy_data(
            output_path=args.output,
            specific_users=specific_users,
            use_gzip=use_gzip,
            include_extras=not args.no_extras,
        )
    except Exception as e:
        log.exception("Export failed")
        sys.exit(1)


if __name__ == "__main__":
    main()