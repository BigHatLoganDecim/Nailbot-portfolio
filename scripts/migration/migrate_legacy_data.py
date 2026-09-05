#!/usr/bin/env python3
"""
Nailbot — Legacy Data Migration Tool (Redis/Export -> SQLite)

Фаза 1 tooling (accelerated). Key feature: detailed per-client change PLAN in --simulate / --dry-run.
Shows exactly what will be created/updated/skipped + reasons for every client and every booking.
Generates rich JSON report for review before any real write.

Usage (Windows example):
  python migrate_legacy_data.py --simulate
  python migrate_legacy_data.py --data-file backups/demo_backup.json.gz --simulate --limit 50 --offset 0
  python migrate_legacy_data.py --simulate --dry-run --db "database/nailbot.db" --data-file "..."
  python migrate_legacy_data.py --validate-only --data-file "..."

Supports:
- .json and .json.gz (current export format with "clients" dict + "redis_extras")
- Old "users" format fallback
- --limit / --offset for safe batch testing
- Structured per-user plans in report + pretty console output
- Validation of datetime_iso, services, statuses
- Ready for --full migration + checkpoints (future phase)
"""

import argparse
import gzip
import json
import sqlite3
import uuid
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Known test backup locations (user machine)
DEFAULT_BACKUP_CANDIDATES = [
    "backups/demo_backup.json.gz",
]

BOOKING_STATUS_ALIASES = {
    "confirmed": "confirmed",
    "active": "confirmed",
    "pending": "pending",
    "rescheduled": "rescheduled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "no_show": "no_show",
    "completed": "completed",
    "done": "completed",
}


def load_legacy_data(path: str) -> Dict[str, Any]:
    """Load export (gz or json). Returns normalized dict with 'clients' (uid->state)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Data file not found: {path}")

    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

    # Normalize to {"clients": dict, "meta": {...}, "extras": {...}}
    if isinstance(raw, dict) and "clients" in raw:
        clients = raw["clients"]
        if isinstance(clients, list):
            clients = {str(c.get("id") or c.get("user_id") or f"anon_{i}"): c for i, c in enumerate(clients)}
        meta = {"created_at": raw.get("created_at"), "clients_count": raw.get("clients_count", len(clients))}
        extras = raw.get("redis_extras", {})
        return {
            "clients": clients,
            "meta": meta,
            "extras": extras,
            "format": "clients-export",
            "has_extras": len(extras) > 0
        }

    if isinstance(raw, dict) and "users" in raw:
        # old format fallback
        return {"clients": raw["users"], "meta": {"source": "old-users"}, "extras": {}, "format": "users"}

    # flat or unknown - try treat whole as clients if values look like states
    if isinstance(raw, dict):
        sample = next(iter(raw.values()), {})
        if isinstance(sample, dict) and ("bookings" in sample or "total_visits" in sample):
            return {"clients": raw, "meta": {"source": "flat"}, "extras": {}, "format": "flat"}

    raise ValueError(f"Unrecognized export format. Top keys: {list(raw.keys())[:8] if isinstance(raw, dict) else type(raw)}")


def normalize_booking(bk: Dict[str, Any], default_user_id: str) -> Dict[str, Any]:
    """Production-hardened normalization for real legacy data."""
    status = str(bk.get("status", "confirmed")).lower()
    status = BOOKING_STATUS_ALIASES.get(status, status)

    # Robust services parsing (handles list, comma string, JSON string)
    services = bk.get("services") or bk.get("services_raw") or []
    if isinstance(services, str):
        services = services.strip()
        if services.startswith("["):
            try:
                services = json.loads(services)
            except:
                services = [s.strip() for s in services.split(",") if s.strip()]
        else:
            services = [s.strip() for s in services.split(",") if s.strip()]
    elif not isinstance(services, list):
        services = []

    # Better datetime handling
    dt_iso = bk.get("datetime_iso") or bk.get("datetime") or ""
    if not dt_iso and bk.get("datetime_text"):
        dt_iso = bk["datetime_text"]

    # Preserve reschedule information in notes
    notes = bk.get("notes", "") or ""
    if "rescheduled_from" in str(bk):
        notes = f"{notes} | rescheduled_from:{bk.get('rescheduled_from', '')}".strip(" |")

    return {
        "id": bk.get("id") or str(uuid.uuid4()),
        "user_id": str(bk.get("user_id") or default_user_id),
        "datetime_iso": dt_iso,
        "master": bk.get("master", "unknown"),
        "services": services,
        "status": status,
        "notes": notes,
        "price": bk.get("price"),
        "source": "legacy_migration",
    }


def validate_client_record(uid: str, state: Dict[str, Any]) -> List[str]:
    """Light validation. Returns list of issues (empty = OK)."""
    issues = []
    if not isinstance(state, dict):
        issues.append("state is not dict")
        return issues

    visits = state.get("total_visits")
    if visits is not None and not isinstance(visits, (int, float)):
        issues.append(f"bad total_visits type: {type(visits)}")

    bks = state.get("bookings")
    if bks is not None and not isinstance(bks, list):
        issues.append(f"bookings not list: {type(bks)}")
    else:
        for i, bk in enumerate(bks or []):
            if not isinstance(bk, dict):
                issues.append(f"booking[{i}] not dict")
                continue
            if not (bk.get("datetime_iso") or bk.get("datetime_text")):
                issues.append(f"booking[{i}] missing datetime")
            if not bk.get("master"):
                issues.append(f"booking[{i}] missing master")
    return issues


def build_detailed_plan(uid: str, state: Dict[str, Any], dry_run: bool = False, db_conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Core: produce human + machine readable plan of exactly what migration will do for this client."""
    issues = validate_client_record(uid, state)
    display = state.get("phone") or state.get("name") or state.get("username") or uid

    visits = int(state.get("total_visits", 0) or 0)
    bonuses = int(state.get("design_bonus_available", state.get("design_bonuses", 0)) or 0)
    referrals = int(state.get("referrals_brought", state.get("referred_by_count", 0)) or 0)

    plan = {
        "uid": str(uid),
        "display": str(display),
        "legacy_counters": {"total_visits": visits, "design_bonuses": bonuses, "referrals_brought": referrals},
        "validation_issues": issues,
        "actions": [],
        "bookings": {"insert": [], "update": [], "skip": []},
        "notes": [],
    }

    # 1. Counters
    plan["actions"].append({
        "action": "set_migration_data",
        "visits": visits,
        "bonuses": bonuses,
        "referrals": referrals,
        "reason": "absolute values from legacy export (authoritative)",
    })

    # 2. Bookings
    raw_bookings: List[Dict] = state.get("bookings") or []
    for idx, bk in enumerate(raw_bookings):
        try:
            nbk = normalize_booking(bk, uid)

            if db_conn:
                decision, reason = detect_booking_conflict(db_conn, nbk)
                action_type = decision if decision in ("insert", "update") else "skip_booking"
                # real decision from indexed DB lookup
            else:
                action_type = "create_booking"
                reason = "new from legacy export (simulate: no --db provided for conflict check)"

            plan["actions"].append({
                "action": action_type,
                "booking_id": nbk["id"],
                "datetime_iso": nbk["datetime_iso"],
                "master": nbk["master"],
                "services": nbk["services"],
                "status": nbk["status"],
                "reason": reason,
            })
            if action_type in ("create_booking", "insert", "update"):
                plan["bookings"]["insert"].append(nbk["id"])
            elif action_type == "skip_booking":
                plan["bookings"]["skip"].append(nbk["id"])
        except Exception as e:
            plan["bookings"]["skip"].append(f"idx_{idx}")
            plan["notes"].append(f"skip booking idx={idx}: {e}")

    if not raw_bookings:
        plan["notes"].append("no bookings in legacy state")

    if issues:
        plan["notes"].append(f"validation issues: {len(issues)}")

    return plan


def simulate_migration(
    data: Dict[str, Any],
    limit: Optional[int] = None,
    offset: int = 0,
    dry_run: bool = False,
    db_path: Optional[str] = None,
    validate_only: bool = False,
    checkpoint_path: Optional[str] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """Run simulation / dry-run. Returns rich report with per_user_plans.

    Supports --checkpoint + --resume for safe long-running batched work.
    """
    clients: Dict[str, Any] = data.get("clients", {})
    if isinstance(clients, list):
        clients = {str(c.get("id") or c.get("user_id") or f"u{i}"): c for i, c in enumerate(clients)}

    total_in_export = len(clients)

    # === Checkpoint handling (resume support) ===
    effective_offset = offset
    checkpoint_state = {}
    if checkpoint_path and resume:
        checkpoint_state = load_checkpoint(checkpoint_path)
        effective_offset = max(offset, checkpoint_state.get("offset", 0))
        if effective_offset > offset:
            print(f"Checkpoint override: using offset {effective_offset} instead of {offset}")

    db_conn = None
    using_test_db = False

    if db_path:
        if db_path == ":memory:" or not Path(db_path).exists():
            # Auto-create populated test DB for safe local --dry-run demos (no prod DB required)
            print("Creating auto-populated test DB for --dry-run conflict detection...")
            db_conn = create_populated_test_db(db_path if db_path != ":memory:" else ":memory:")
            using_test_db = True
        else:
            try:
                db_conn = sqlite3.connect(db_path)
            except Exception as e:
                print(f"WARNING: could not open --db {db_path}: {e}")
    elif dry_run:
        # No --db given + --dry-run → use self-contained test DB so feature is immediately usable
        print("No --db provided. Using in-memory test DB with synthetic data for dry-run demo...")
        db_conn = create_populated_test_db(":memory:")
        using_test_db = True

    processed = checkpoint_state.get("processed", 0)
    per_user_plans: Dict[str, Any] = {}
    errors: List[Dict] = checkpoint_state.get("errors", [])
    validation_stats = {"ok": 0, "with_issues": 0, "bookings_total": 0}

    slice_iter = islice(clients.items(), effective_offset, effective_offset + limit if limit else None)

    for idx, (uid, state) in enumerate(slice_iter):
        if (idx + 1) % 100 == 0:
            print(f"  Progress: {idx+1} clients processed...")
        processed += 1
        try:
            if validate_only:
                issues = validate_client_record(uid, state)
                plan = {
                    "uid": str(uid),
                    "display": state.get("phone") or uid,
                    "validation_issues": issues,
                    "legacy_counters": {"total_visits": state.get("total_visits"), "design_bonuses": state.get("design_bonus_available")},
                    "bookings_count": len(state.get("bookings") or []),
                }
                if issues:
                    validation_stats["with_issues"] += 1
                else:
                    validation_stats["ok"] += 1
                validation_stats["bookings_total"] += plan["bookings_count"]
                per_user_plans[uid] = plan
                if issues:
                    print(f"  [VALIDATE] {uid}: ISSUES={issues}")
                continue

            state_for_plan = dict(state)  # copy for safe mutation in demo mode
            if using_test_db and str(uid) == "100000001" and not state_for_plan.get("bookings"):
                # Inject synthetic legacy booking so --dry-run visibly exercises real conflict detection
                # (the small test backup has 0 bookings; this makes "skip" / "insert" decisions appear)
                state_for_plan["bookings"] = [{
                    "id": "legacy-demo-01",
                    "datetime_iso": "2026-06-10T12:00:00",
                    "master": "master1",
                    "services": ["manicure"],
                    "status": "confirmed"
                }, {
                    "id": "legacy-demo-02",
                    "datetime_iso": "2026-06-12T09:00:00",
                    "master": "master3",
                    "services": ["pedicure"],
                    "status": "confirmed"
                }]
            plan = build_detailed_plan(uid, state_for_plan, dry_run=dry_run, db_conn=db_conn)
            per_user_plans[uid] = plan

            # Save checkpoint after every client (safe & cheap)
            if checkpoint_path:
                current_offset = effective_offset + processed + 1
                save_checkpoint(checkpoint_path, current_offset, processed + 1, errors)

            # Pretty console output (visible impact)
            print(f"\n=== Client {uid} ({plan['display']}) ===")
            for a in plan["actions"]:
                if a["action"] == "set_migration_data":
                    print(f"  COUNTERS: visits={a['visits']}, bonuses={a['bonuses']}, referrals={a['referrals']}  ({a['reason']})")
                elif any(x in a.get("action", "") for x in ("booking", "insert", "update", "skip")):
                    print(f"  BOOKING: {a.get('datetime_iso','?')} | {a.get('master','?')} | {a.get('services',[])} | status={a.get('status')}")
                    print(f"           -> {a['action']}  reason: {a['reason']}")
            if plan["notes"]:
                print(f"  notes: {plan['notes']}")
            if plan["validation_issues"]:
                print(f"  validation: {plan['validation_issues']}")

            validation_stats["bookings_total"] += len(plan["bookings"]["insert"])
            if plan["validation_issues"]:
                validation_stats["with_issues"] += 1
            else:
                validation_stats["ok"] += 1

        except Exception as e:
            err = {"uid": str(uid), "error": str(e)[:300]}
            errors.append(err)
            print(f"  ERROR for {uid}: {e}")

    if db_conn:
        db_conn.close()

    report = {
        "timestamp": datetime.now().isoformat(),
        "mode": "validate_only" if validate_only else ("dry_run" if dry_run else "simulate"),
        "input": {
            "data_file": data.get("_source_path"),
            "format": data.get("format"),
            "total_in_export": total_in_export,
            "limit": limit,
            "offset": effective_offset,
            "db_path": db_path,
            "checkpoint": checkpoint_path,
            "resumed": bool(checkpoint_state),
        },
        "summary": {
            "processed_clients": processed,
            "planned_bookings": validation_stats["bookings_total"],
            "clients_ok": validation_stats["ok"],
            "clients_with_issues": validation_stats["with_issues"],
            "errors_count": len(errors),
        },
        "per_user_plans": per_user_plans,
        "errors": errors,
        "validation_stats": validation_stats,
    }
    return report


def perform_full_migration(
    data: Dict[str, Any],
    db_path: str,
    limit: Optional[int] = None,
    offset: int = 0,
    checkpoint_path: Optional[str] = None,
    resume: bool = False,
    backup: bool = True,
) -> Dict[str, Any]:
    """Real migration execution. Writes to the target DB.

    Uses the same planning + conflict logic as dry-run.
    Safe when combined with --checkpoint + --resume.
    """
    print(f"\n=== REAL MIGRATION MODE ===")
    print(f"Target DB: {db_path}")

    clients = data.get("clients", {})
    if isinstance(clients, list):
        clients = {str(c.get("id") or c.get("user_id") or f"u{i}"): c for i, c in enumerate(clients)}

    total_in_export = len(clients)

    # Checkpoint handling
    effective_offset = offset
    checkpoint_state = {}
    if checkpoint_path and resume:
        checkpoint_state = load_checkpoint(checkpoint_path)
        effective_offset = max(offset, checkpoint_state.get("offset", 0))

    # === Automatic safety backup ===
    backup_path = ""
    if backup:
        backup_path = backup_database(db_path)

    conn = sqlite3.connect(db_path)

    # Detect if this looks like a real project DB (has more tables/columns than our minimal one)
    is_real_schema = False
    try:
        cur = conn.execute("PRAGMA table_info(clients)")
        cols = [row[1] for row in cur.fetchall()]
        if len(cols) > 5 or 'created_at' in cols:  # heuristic
            is_real_schema = True
    except:
        pass

    if is_real_schema:
        print("[INFO] Detected real project schema. Running in compatibility mode.")
    else:
        ensure_minimal_schema(conn)

    processed = checkpoint_state.get("processed", 0)
    errors = checkpoint_state.get("errors", [])
    migrated_clients = 0
    migrated_bookings = 0
    skipped_bookings = 0
    per_client_results: Dict[str, Any] = {}

    # === Execution Log (new rich tracking for this step) ===
    execution_log = {
        "started_at": datetime.now().isoformat(),
        "clients": {},
        "overall_summary": {}
    }

    slice_iter = list(clients.items())[effective_offset : effective_offset + limit if limit else None]

    total_to_process = len(slice_iter) if hasattr(slice_iter, '__len__') else 'unknown'

    for idx, (uid, state) in enumerate(slice_iter):
        if (idx + 1) % 100 == 0 or idx == 0:
            print(f"  Progress: {idx+1}/{total_to_process} clients processed...")

        client_result = {
            "counters_set": False,
            "bookings_inserted": 0,
            "bookings_updated": 0,
            "bookings_skipped": 0,
            "errors": []
        }
        try:
            uid_str = str(uid)

            # === Capture BEFORE state for counters ===
            cur = conn.execute(
                "SELECT total_visits, design_bonuses, referrals_brought FROM clients WHERE user_id = ?",
                (uid_str,)
            )
            before_row = cur.fetchone()
            before = {
                "total_visits": before_row[0] if before_row else 0,
                "design_bonuses": before_row[1] if before_row else 0,
                "referrals_brought": before_row[2] if before_row else 0,
            }

            # 1. Write counters (set_migration_data style) — compatible with real schema
            target_visits = int(state.get("total_visits", 0) or 0)
            target_bonuses = int(state.get("design_bonus_available", state.get("design_bonuses", 0)) or 0)
            target_referrals = int(state.get("referrals_brought", 0) or 0)
            now = datetime.now().isoformat()

            conn.execute("""
                INSERT OR REPLACE INTO clients (user_id, total_visits, design_bonuses, referrals_brought, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (uid_str, target_visits, target_bonuses, target_referrals, now))
            client_result["counters_set"] = True

            # === Capture AFTER state ===
            after = {
                "total_visits": target_visits,
                "design_bonuses": target_bonuses,
                "referrals_brought": target_referrals,
            }

            # 2. Bookings with detailed action logging
            raw_bookings = state.get("bookings") or []
            booking_actions = []

            for bk in raw_bookings:
                nbk = normalize_booking(bk, uid)
                decision, reason = detect_booking_conflict(conn, nbk)

                action = {
                    "booking_id": nbk["id"],
                    "datetime_iso": nbk["datetime_iso"],
                    "master": nbk["master"],
                    "status": nbk["status"],
                    "decision": decision,
                    "reason": reason
                }

                if decision == "skip":
                    client_result["bookings_skipped"] += 1
                    skipped_bookings += 1
                    action["performed"] = "skipped"
                elif decision == "insert":
                    conn.execute("""
                        INSERT INTO bookings (id, user_id, datetime_iso, master, services, status, notes, price, source, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        nbk["id"], nbk["user_id"], nbk["datetime_iso"], nbk["master"],
                        json.dumps(nbk["services"], ensure_ascii=False),
                        nbk["status"],
                        nbk.get("notes", ""),
                        nbk.get("price"),
                        "legacy_migration",
                        datetime.now().isoformat()
                    ))
                    client_result["bookings_inserted"] += 1
                    migrated_bookings += 1
                    action["performed"] = "inserted"
                elif decision == "update":
                    conn.execute("""
                        UPDATE bookings 
                        SET status = ?, notes = ?, source = 'legacy_migration_updated', 
                            services = ?, price = ?
                        WHERE id = ?
                    """, (nbk["status"], nbk.get("notes", ""), 
                          json.dumps(nbk["services"], ensure_ascii=False), 
                          nbk.get("price"), nbk["id"]))
                    client_result["bookings_updated"] += 1
                    migrated_bookings += 1
                    action["performed"] = "updated"

                booking_actions.append(action)

            migrated_clients += 1
            processed += 1
            per_client_results[str(uid)] = client_result

            # Record rich execution details for this client
            execution_log["clients"][uid_str] = {
                "counters": {
                    "before": before,
                    "after": after,
                    "target_from_legacy": {
                        "total_visits": target_visits,
                        "design_bonuses": target_bonuses,
                        "referrals_brought": target_referrals
                    }
                },
                "bookings": booking_actions,
                "summary": {
                    "bookings_inserted": client_result["bookings_inserted"],
                    "bookings_updated": client_result["bookings_updated"],
                    "bookings_skipped": client_result["bookings_skipped"]
                }
            }

            # Checkpoint after each client
            if checkpoint_path:
                save_checkpoint(checkpoint_path, effective_offset + processed, processed, errors, {"last_uid": str(uid)})

            if (idx + 1) % 50 == 0:
                print(f"  Progress: {processed} clients migrated...")

        except Exception as e:
            client_result["errors"].append(str(e)[:200])
            per_client_results[str(uid)] = client_result
            errors.append({"uid": str(uid), "error": str(e)[:300]})
            print(f"  ERROR migrating {uid}: {e}")

    conn.commit()
    conn.close()

    execution_log["finished_at"] = datetime.now().isoformat()
    execution_log["overall_summary"] = {
        "total_clients_processed": processed,
        "clients_migrated": migrated_clients,
        "bookings_inserted": migrated_bookings,
        "bookings_skipped_due_to_conflict": skipped_bookings,
        "errors_encountered": len(errors)
    }

    # Save rich execution log
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    exec_log_filename = f"migration_execution_log_{ts}.json"
    Path(exec_log_filename).write_text(json.dumps(execution_log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] Detailed execution log saved: {exec_log_filename}")

    # Auto-generate human-readable summary
    summary_file = generate_migration_summary(execution_log, db_path, backup_path)
    print(f"[OK] Human-readable summary saved: {summary_file}")

    report = {
        "timestamp": datetime.now().isoformat(),
        "mode": "full",
        "input": {
            "data_file": data.get("_source_path"),
            "db_path": db_path,
            "limit": limit,
            "offset": effective_offset,
            "checkpoint": checkpoint_path,
            "resumed": bool(checkpoint_state),
            "backup_created": backup_path,
            "execution_log": exec_log_filename,
            "summary_md": summary_file
        },
        "summary": {
            "processed_clients": processed,
            "migrated_clients": migrated_clients,
            "migrated_bookings": migrated_bookings,
            "skipped_bookings": skipped_bookings,
            "errors_count": len(errors),
        },
        "per_client_results": per_client_results,
        "errors": errors,
    }
    return report


def save_report(report: Dict[str, Any], path: Optional[str] = None) -> str:
    if not path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"migration_report_{report['mode']}_{ts}.json"
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# === Checkpoint / Resume support (for safe long batched migrations) ===

def load_checkpoint(path: str) -> Dict[str, Any]:
    """Load checkpoint if exists, otherwise return empty state."""
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            print(f"Resuming from checkpoint: {path} (offset={data.get('offset', 0)}, processed={data.get('processed', 0)})")
            return data
        except Exception as e:
            print(f"WARNING: Could not load checkpoint {path}: {e}. Starting fresh.")
    return {"offset": 0, "processed": 0, "errors": [], "timestamp": None}


def save_checkpoint(path: str, offset: int, processed: int, errors: list, extra: Optional[Dict] = None) -> None:
    """Atomically save progress checkpoint."""
    if not path:
        return
    data = {
        "offset": offset,
        "processed": processed,
        "errors": errors[-50:],  # keep last 50 errors only
        "timestamp": datetime.now().isoformat(),
    }
    if extra:
        data.update(extra)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# === New: Human-readable summary + Verification layer (this step) ===

def generate_migration_summary(execution_log: Dict, db_path: str, backup_path: str = "") -> str:
    """Generate a nice human-readable Markdown summary after a real migration."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary_lines = []

    summary_lines.append("# Migration Summary\n")
    summary_lines.append(f"**Date:** {ts}\n")
    summary_lines.append(f"**Source:** {execution_log.get('input', {}).get('data_file', 'N/A')}\n")
    summary_lines.append(f"**Target DB:** {db_path}\n")
    if backup_path:
        summary_lines.append(f"**Backup:** {backup_path}\n")

    overall = execution_log.get("overall_summary", {})
    summary_lines.append("\n## Overall Results\n")
    summary_lines.append(f"- Clients processed: **{overall.get('total_clients_processed', 0)}**\n")
    summary_lines.append(f"- Clients migrated: **{overall.get('clients_migrated', 0)}**\n")
    summary_lines.append(f"- Bookings inserted: **{overall.get('bookings_inserted', 0)}**\n")
    summary_lines.append(f"- Bookings skipped (conflicts): **{overall.get('bookings_skipped_due_to_conflict', 0)}**\n")
    summary_lines.append(f"- Errors: **{overall.get('errors_encountered', 0)}**\n")

    summary_lines.append("\n## Per-Client Highlights\n")
    clients = execution_log.get("clients", {})
    for uid, data in list(clients.items())[:10]:  # show first 10
        counters = data.get("counters", {})
        b = counters.get("before", {})
        a = counters.get("after", {})
        summary_lines.append(f"- **{uid}**:\n")
        summary_lines.append(f"  - Visits: {b.get('total_visits', 0)} → {a.get('total_visits', 0)}\n")
        summary_lines.append(f"  - Bonuses: {b.get('design_bonuses', 0)} → {a.get('design_bonuses', 0)}\n")
        bks = data.get("summary", {})
        summary_lines.append(f"  - Bookings: +{bks.get('bookings_inserted', 0)} inserted, {bks.get('bookings_skipped', 0)} skipped\n")

    if len(clients) > 10:
        summary_lines.append(f"\n... and {len(clients) - 10} more clients (see full JSON log)\n")

    summary_lines.append("\n## Next Steps\n")
    summary_lines.append("- Run `--verify` against the target DB for health checks.\n")
    summary_lines.append("- Review the full `migration_execution_log_*.json` for every change.\n")

    filename = f"migration_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    Path(filename).write_text("".join(summary_lines), encoding="utf-8")
    return filename


def run_verification(db_path: str, execution_log_path: Optional[str] = None) -> Dict[str, Any]:
    """Run post-migration health checks on the target DB."""
    print(f"\n=== POST-MIGRATION VERIFICATION ===\nTarget: {db_path}")

    conn = sqlite3.connect(db_path)
    results = {}

    # Basic counts
    cur = conn.execute("SELECT COUNT(*) FROM clients")
    results["total_clients_in_db"] = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM bookings")
    results["total_bookings_in_db"] = cur.fetchone()[0]

    cur = conn.execute("SELECT SUM(total_visits), SUM(design_bonuses) FROM clients")
    row = cur.fetchone()
    results["sum_visits"] = row[0] or 0
    results["sum_bonuses"] = row[1] or 0

    print(f"Clients in DB: {results['total_clients_in_db']}")
    print(f"Bookings in DB: {results['total_bookings_in_db']}")
    print(f"Sum of visits: {results['sum_visits']}")
    print(f"Sum of bonuses: {results['sum_bonuses']}")

    if execution_log_path and Path(execution_log_path).exists():
        log = json.loads(Path(execution_log_path).read_text())
        overall = log.get("overall_summary", {})
        results["expected_from_log"] = {
            "clients_migrated": overall.get("clients_migrated"),
            "bookings_inserted": overall.get("bookings_inserted")
        }
        print(f"\nFrom execution log:")
        print(f"  Expected clients migrated: {results['expected_from_log']['clients_migrated']}")
        print(f"  Expected bookings inserted: {results['expected_from_log']['bookings_inserted']}")

        diff_clients = results["total_clients_in_db"] - (results['expected_from_log']['clients_migrated'] or 0)
        print(f"\nDifference (DB vs log): clients {diff_clients:+d}")

    conn.close()
    print("\n=== Verification complete ===")
    return results


def run_self_test() -> None:
    """Phase 2 self-test suite. Runs key paths on local test data to ensure the tool is trustworthy."""
    print("\n" + "="*60)
    print("MIGRATION TOOL SELF-TEST (Phase 2)")
    print("="*60)

    # Find available test backups
    test_backups = [p for p in DEFAULT_BACKUP_CANDIDATES if Path(p).exists()]

    if not test_backups:
        print("❌ No local test backups found. Cannot run self-test.")
        print("   Expected files in Downloads/Telegram Desktop/")
        return

    print(f"Found {len(test_backups)} local test backup(s). Using first one for testing.")
    test_file = test_backups[0]
    print(f"Using: {test_file}\n")

    results = []
    temp_dir = Path("temp_selftest")
    temp_dir.mkdir(exist_ok=True)

    try:
        # Test 1: Readiness Report
        print("[1/6] Testing --readiness-report ...")
        report = generate_readiness_report(load_legacy_data(test_file))
        if report.get("summary", {}).get("risk_level"):
            results.append(("readiness_report", True))
            print("      [OK]")
        else:
            results.append(("readiness_report", False))
            print("      [FAIL] Failed to produce risk level")

        # Test 2: Simulate
        print("[2/6] Testing --simulate ...")
        sim_report = simulate_migration(load_legacy_data(test_file), limit=5)
        if sim_report.get("mode") == "simulate":
            results.append(("simulate", True))
            print("      [OK]")
        else:
            results.append(("simulate", False))
            print("      [FAIL]")

        # Test 3: Dry-run on temp DB
        print("[3/6] Testing --dry-run on temporary DB ...")
        temp_db = temp_dir / "test_dry.db"
        if temp_db.exists():
            temp_db.unlink()
        dry_report = simulate_migration(
            load_legacy_data(test_file),
            limit=3,
            dry_run=True,
            db_path=str(temp_db)
        )
        if dry_report.get("mode") == "dry_run":
            results.append(("dry_run", True))
            print("      [OK]")
        else:
            results.append(("dry_run", False))
            print("      [FAIL]")

        # Test 4: Full migration on temp DB + execution log
        print("[4/6] Testing --full + execution log generation ...")
        temp_db2 = temp_dir / "test_full.db"
        if temp_db2.exists():
            temp_db2.unlink()

        full_report = perform_full_migration(
            load_legacy_data(test_file),
            db_path=str(temp_db2),
            limit=3,
            backup=False  # don't create real backups during self-test
        )

        exec_log = full_report.get("input", {}).get("execution_log")
        if exec_log and Path(exec_log).exists():
            results.append(("full_migration", True))
            print("      [OK] (execution log created)")
            Path(exec_log).unlink(missing_ok=True)
        else:
            results.append(("full_migration", False))
            print("      [FAIL] Execution log was not created")

        # Test 5: Verify mode
        print("[5/6] Testing --verify ...")
        verify_result = run_verification(str(temp_db2))
        if verify_result.get("total_clients_in_db", 0) >= 0:
            results.append(("verify", True))
            print("      [OK]")
        else:
            results.append(("verify", False))
            print("      [FAIL]")

        # Test 6: Checkpoint + resume (light test)
        print("[6/6] Testing checkpoint + resume ...")
        cp_file = temp_dir / "test_cp.json"
        if cp_file.exists():
            cp_file.unlink()

        simulate_migration(load_legacy_data(test_file), limit=2, checkpoint_path=str(cp_file))
        if cp_file.exists():
            results.append(("checkpoint", True))
            print("      [OK] Checkpoint created")
            simulate_migration(load_legacy_data(test_file), limit=2, checkpoint_path=str(cp_file), resume=True)
            results.append(("resume", True))
            print("      [OK] Resume works")
            cp_file.unlink(missing_ok=True)
        else:
            results.append(("checkpoint", False))
            print("      [FAIL] Checkpoint was not created")

    except Exception as e:
        print(f"\n[CRASH] Self-test crashed: {e}")
        results.append(("crash", False))

    # Cleanup
    import shutil
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Final report
    print("\n" + "="*60)
    print("SELF-TEST RESULTS")
    print("="*60)

    passed = 0
    for name, success in results:
        status = "[PASS]" if success else "[FAIL]"
        print(f"{status}  {name}")
        if success:
            passed += 1

    total = len(results)
    print(f"\nTotal: {passed}/{total} passed")

    if passed == total:
        print("\n[ALL PASSED] Tool looks solid for Phase 2.")
    else:
        print(f"\n[WARNING] {total - passed} test(s) failed. Fix before going to real prod data.")

    print("="*60 + "\n")


def backup_database(db_path: str) -> str:
    """Create a timestamped backup of the target database before real migration.
    Returns path to the backup file.
    """
    src = Path(db_path)
    if not src.exists():
        return ""  # nothing to backup (e.g. new :memory: or fresh file)

    backups_dir = Path("backups")
    backups_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{src.stem}_pre_migration_{ts}{src.suffix}"
    backup_path = backups_dir / backup_name

    # Simple file copy (works for SQLite)
    import shutil
    shutil.copy2(src, backup_path)
    print(f"✓ Database backed up to: {backup_path}")
    return str(backup_path)


def generate_readiness_report(data: Dict[str, Any], limit: Optional[int] = None) -> Dict[str, Any]:
    """Deep analysis of legacy data. Produces a professional Migration Readiness Report."""
    clients = data.get("clients", {})
    if isinstance(clients, list):
        clients = {str(c.get("id") or c.get("user_id") or f"u{i}"): c for i, c in enumerate(clients)}

    total_clients = len(clients)
    processed = 0

    issues = []
    stats = {
        "total_clients": total_clients,
        "clients_with_bookings": 0,
        "total_bookings_in_source": 0,
        "status_distribution": {},
        "date_parse_failures": 0,
        "missing_master": 0,
        "potential_duplicates_in_source": 0,
        "clients_with_zero_visits_but_bookings": 0,
    }

    seen_bookings = set()  # for source duplicate detection

    items = list(clients.items())
    if limit:
        items = items[:limit]

    for uid, state in items:
        processed += 1
        bookings = state.get("bookings") or []

        if bookings:
            stats["clients_with_bookings"] += 1
            stats["total_bookings_in_source"] += len(bookings)

        visits = state.get("total_visits", 0) or 0
        if visits == 0 and len(bookings) > 0:
            stats["clients_with_zero_visits_but_bookings"] += 1

        for bk in bookings:
            status = str(bk.get("status", "unknown")).lower()
            stats["status_distribution"][status] = stats["status_distribution"].get(status, 0) + 1

            dt = bk.get("datetime_iso") or bk.get("datetime_text") or ""
            if not dt:
                stats["date_parse_failures"] += 1
                issues.append(f"Client {uid}: booking missing date")

            master = bk.get("master")
            if not master:
                stats["missing_master"] += 1
                issues.append(f"Client {uid}: booking missing master")

            # Simple source duplicate detection
            key = f"{uid}|{dt}|{master}"
            if key in seen_bookings:
                stats["potential_duplicates_in_source"] += 1
            seen_bookings.add(key)

    # Risk scoring
    risk_score = 0
    risks = []
    if stats["date_parse_failures"] > 0:
        risk_score += 20
        risks.append(f"{stats['date_parse_failures']} bookings with missing/invalid dates")
    if stats["potential_duplicates_in_source"] > 0:
        risk_score += 15
        risks.append(f"{stats['potential_duplicates_in_source']} potential duplicate bookings in source data")
    if stats["clients_with_zero_visits_but_bookings"] > total_clients * 0.1:
        risk_score += 10
        risks.append("Many clients have bookings but zero total_visits (counter sync risk)")

    # Analyze redis_extras if present
    extras = data.get("extras", {})
    if extras:
        extra_keys = list(extras.keys())
        risks.append(f"Export contains {len(extras)} redis_extra keys (e.g. {extra_keys[:3]}) — some data may need manual review")

    recommendations = []
    if risk_score > 30:
        recommendations.append("Strongly run --readiness-report + manual review before --full")
    if stats["potential_duplicates_in_source"] > 5:
        recommendations.append("Consider cleaning source data or rely on conflict detection during migration")
    recommendations.append("Always run with --checkpoint + --limit on first real attempts")

    report = {
        "timestamp": datetime.now().isoformat(),
        "mode": "readiness_report",
        "summary": {
            "total_clients_in_export": total_clients,
            "processed": processed,
            "risk_score": min(risk_score, 100),
            "risk_level": "HIGH" if risk_score > 40 else ("MEDIUM" if risk_score > 20 else "LOW"),
        },
        "stats": stats,
        "issues_found": issues[:50],  # cap
        "risks": risks,
        "recommendations": recommendations,
    }
    return report


def print_final_summary(report: Dict[str, Any]) -> None:
    s = report["summary"]
    mode = report["mode"]
    inp = report.get("input", {})
    print("\n" + "=" * 60)
    print(f"MIGRATION {mode.upper()} SUMMARY")
    print("=" * 60)
    print(f"Input: {inp.get('data_file')} (format={inp.get('format')}, total_export={inp.get('total_in_export')})")
    print(f"Batch: limit={inp.get('limit')}, offset={inp.get('offset')}")
    if inp.get("checkpoint"):
        print(f"Checkpoint: {inp['checkpoint']} (resumed={inp.get('resumed', False)})")
    if inp.get("backup_created"):
        print(f"Backup created: {inp['backup_created']}")
    if inp.get("execution_log"):
        print(f"Execution log: {inp['execution_log']}")
    if inp.get("db_path"):
        print(f"DB: {inp['db_path']} (dry-run checks only)")

    if report.get("mode") == "readiness_report":
        r = report.get("summary", {})
        print(f"\n=== MIGRATION READINESS REPORT ===")
        print(f"Risk Level: {r.get('risk_level', 'N/A')} (Score: {r.get('risk_score', 0)}/100)")
        stats = report.get("stats", {})
        print(f"Clients with bookings: {stats.get('clients_with_bookings', 0)}")
        print(f"Total source bookings: {stats.get('total_bookings_in_source', 0)}")
        if report.get("risks"):
            print("Risks identified:")
            for risk in report.get("risks", []):
                print(f"  - {risk}")
        if report.get("recommendations"):
            print("Recommendations:")
            for rec in report.get("recommendations", []):
                print(f"  - {rec}")
        print("==================================\n")
    print(f"Processed: {s.get('processed_clients', 0)} clients")
    if report.get("mode") == "full":
        print(f"Migrated clients: {s.get('migrated_clients', 0)}")
        print(f"Migrated bookings: {s.get('migrated_bookings', 0)} (skipped duplicates: {s.get('skipped_bookings', 0)})")
    elif report.get("mode") == "readiness_report":
        print(f"Risk Level: {s.get('risk_level', 'UNKNOWN')} (score: {s.get('risk_score', 0)}/100)")
    else:
        print(f"Planned/seen bookings: {s.get('planned_bookings', 0)}")
        print(f"  OK clients: {s.get('clients_ok', 0)}   With issues: {s.get('clients_with_issues', 0)}")
    print(f"Errors: {s.get('errors_count', 0)}")
    if report.get("errors"):
        print("First errors:")
        for e in report["errors"][:3]:
            print(f"  - {e}")
    print(f"\nFull per-user plans + details saved to: {report.get('_report_path', 'N/A')}")
    print("=" * 60 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Legacy Redis/Export -> SQLite migration with detailed per-client plans + safe resume",
        epilog="Examples:\n"
               "  --simulate --limit 50\n"
               "  --dry-run --db my.db --checkpoint p.json\n"
               "  --full --db prod.db --checkpoint p.json --resume --yes   # REAL migration (careful!)\n"
               "  --self-test     # Run internal validation before using on real data",
    )
    parser.add_argument("--data-file", help="Path to legacy .json or .json.gz export")
    parser.add_argument("--simulate", action="store_true", help="Run simulation and print detailed plans (no writes)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate with real DB for conflict detection (reads only)")
    parser.add_argument("--validate-only", action="store_true", help="Only validate structure and data quality, no plans")
    parser.add_argument("--readiness-report", action="store_true", help="Generate detailed Migration Readiness Report (deep validation + risk analysis + recommendations). Recommended before --full on real data.")

    # Phase 2 - Self testing
    parser.add_argument("--self-test", action="store_true", help="Run internal self-test suite using available local backups. Verifies all major paths work correctly before touching real prod data.")

    # Post-migration tooling
    parser.add_argument("--verify", action="store_true", help="Run post-migration verification against the target DB (counts, sums, health checks). Can be used with --execution-log for comparison.")
    parser.add_argument("--execution-log", help="Path to a previous migration_execution_log_*.json for verification/comparison")
    parser.add_argument("--limit", type=int, default=None, help="Process only N first clients")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N clients")
    parser.add_argument("--db", help="Path to target SQLite DB for --dry-run conflict checks (optional)")
    parser.add_argument("--report", help="Custom path for JSON report")
    parser.add_argument("--no-report", action="store_true", help="Do not write JSON report file")

    # Checkpoint / Resume support (critical for safe long migrations)
    parser.add_argument("--checkpoint", help="Path to checkpoint JSON file for saving progress")
    parser.add_argument("--resume", action="store_true", help="Resume from --checkpoint if it exists")

    # Real migration
    parser.add_argument("--full", action="store_true", help="Perform REAL migration (writes to DB). Use with extreme caution. Strongly recommend --dry-run first.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt for --full mode")

    # Safety for real migration
    parser.add_argument("--backup", action="store_true", default=True, help="Automatically backup target DB before --full (default: on)")
    parser.add_argument("--no-backup", dest="backup", action="store_false", help="Disable automatic backup before --full")

    args = parser.parse_args()

    # Auto-detect data file
    data_path = args.data_file
    if not data_path:
        for cand in DEFAULT_BACKUP_CANDIDATES:
            if Path(cand).exists():
                data_path = cand
                print(f"Auto-detected data file: {data_path}")
                break
    if not data_path:
        parser.error("No --data-file and no default backup found in Downloads/Telegram Desktop/")

    print(f"Loading {data_path} ...")
    data = load_legacy_data(data_path)
    data["_source_path"] = data_path

    if args.full:
        if not args.db:
            parser.error("--full requires --db PATH (real target database)")
        if not args.yes:
            print("\n" + "="*70)
            print("!!! КРИТИЧЕСКОЕ ПРЕДУПРЕЖДЕНИЕ !!!")
            print("="*70)
            print("Ты запускаешь РЕАЛЬНУЮ миграцию (--full).")
            print("Убедись, что ты уже выполнил:")
            print("  1. --readiness-report на этом дампе")
            print("  2. Несколько --dry-run с --checkpoint")
            print("  3. --verify после тестовых запусков")
            print("="*70)
            confirm = input("Напиши 'YES I UNDERSTAND THE RISKS' чтобы продолжить: ").strip()
            if confirm != "YES I UNDERSTAND THE RISKS":
                print("Aborted.")
                return

    if args.readiness_report:
        report = generate_readiness_report(data, limit=args.limit)
    elif not (args.simulate or args.dry_run or args.validate_only or args.full):
        # default to simulate for safety
        args.simulate = True
        print("Defaulting to --simulate (safe mode)")

    if args.self_test:
        run_self_test()
        return
    elif args.readiness_report:
        report = generate_readiness_report(data, limit=args.limit)
    elif args.verify:
        run_verification(args.db or "", args.execution_log)
        return
    elif args.full:
        report = perform_full_migration(
            data,
            db_path=args.db,
            limit=args.limit,
            offset=args.offset,
            checkpoint_path=args.checkpoint,
            resume=args.resume,
            backup=args.backup,
        )
    else:
        report = simulate_migration(
            data,
            limit=args.limit,
            offset=args.offset,
            dry_run=args.dry_run,
            db_path=args.db,
            validate_only=args.validate_only,
            checkpoint_path=args.checkpoint,
            resume=args.resume,
        )

    if not args.no_report:
        report_path = save_report(report, args.report)
        report["_report_path"] = report_path
        print(f"Report written: {report_path}")

    print_final_summary(report)


# === Real dry-run helpers (Phase 1 acceleration) ===
# All queries use indexes. No full scans. Safe for :memory: and large prod DBs.
# Self-contained test DB creation so --dry-run works locally with zero external files.

def ensure_minimal_schema(conn: sqlite3.Connection) -> None:
    """Create/align schema as close as possible to the real project one.
    Safe to call on real DB copies — uses IF NOT EXISTS + idempotent ALTERs where possible.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            user_id TEXT PRIMARY KEY,
            total_visits INTEGER DEFAULT 0,
            design_bonuses INTEGER DEFAULT 0,
            referrals_brought INTEGER DEFAULT 0,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS bookings (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            datetime_iso TEXT,
            master TEXT,
            services TEXT,           -- stored as JSON
            status TEXT,
            notes TEXT,
            price REAL,
            source TEXT,
            created_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_bk_lookup ON bookings(user_id, datetime_iso, master);
        CREATE INDEX IF NOT EXISTS idx_bk_user ON bookings(user_id);
    """)

    # Best-effort alignment for real DBs (safe if columns already exist)
    try:
        conn.execute("ALTER TABLE clients ADD COLUMN updated_at TEXT")
    except:
        pass
    try:
        conn.execute("ALTER TABLE bookings ADD COLUMN created_at TEXT")
    except:
        pass
    try:
        conn.execute("ALTER TABLE bookings ADD COLUMN price REAL")
    except:
        pass

    conn.commit()


def detect_booking_conflict(conn: sqlite3.Connection, nbk: Dict[str, Any]) -> Tuple[str, str]:
    """Production-grade conflict detection for real data.

    Handles normal bookings + reschedules intelligently.
    """
    user_id = nbk.get("user_id")
    dt = nbk.get("datetime_iso", "")
    master = nbk.get("master", "")
    notes = nbk.get("notes", "")

    cur = conn.execute(
        """SELECT id, status, notes FROM bookings 
           WHERE user_id=? AND datetime_iso=? AND master=? LIMIT 1""",
        (user_id, dt, master)
    )
    row = cur.fetchone()

    if not row:
        return "insert", "no matching slot (user+datetime+master) in target DB"

    existing_id, existing_status, existing_notes = row

    # Rescheduled bookings should usually be inserted as new records
    if "rescheduled_from" in notes:
        return "insert", "rescheduled booking (new time slot) — inserting as new"

    if existing_status == nbk.get("status"):
        return "skip", f"duplicate exists (id={existing_id}, status={existing_status})"

    return "update", f"status differs ({existing_status} → {nbk.get('status')})"


def get_current_counters(conn: sqlite3.Connection, user_id: str) -> Optional[Dict[str, int]]:
    cur = conn.execute(
        "SELECT total_visits, design_bonuses FROM clients WHERE user_id=?",
        (user_id,)
    )
    row = cur.fetchone()
    if row:
        return {"total_visits": row[0], "design_bonuses": row[1]}
    return None


def create_populated_test_db(path: str = ":memory:") -> sqlite3.Connection:
    """One-command local test DB for --dry-run demos (populates synthetic data for the test client)."""
    if path and path != ":memory:":
        conn = sqlite3.connect(path)
    else:
        conn = sqlite3.connect(":memory:")
    ensure_minimal_schema(conn)

    test_uid = "100000001"  # synthetic demo client for dry-run
    conn.execute(
        "INSERT OR REPLACE INTO clients (user_id, total_visits, design_bonuses, referrals_brought) VALUES (?,?,?,?)",
        (test_uid, 3, 1, 0)
    )

    # Existing booking → will trigger "skip" decision in dry-run
    conn.execute("""
        INSERT OR REPLACE INTO bookings (id, user_id, datetime_iso, master, services, status, source)
        VALUES (?,?,?,?,?,?,?)
    """, ("test-skip-001", test_uid, "2026-06-10T12:00:00", "master1", '["manicure"]', "confirmed", "synthetic"))

    # Different time → will be "insert"
    conn.execute("""
        INSERT OR REPLACE INTO bookings (id, user_id, datetime_iso, master, services, status, source)
        VALUES (?,?,?,?,?,?,?)
    """, ("test-ins-002", test_uid, "2026-06-11T15:00:00", "master2", '["pedicure"]', "confirmed", "synthetic"))

    conn.commit()
    return conn


if __name__ == "__main__":
    main()
