#!/usr/bin/env python3
"""
maintenance.py — CNC Probe database maintenance.

Keeps cnc_data.db small by enforcing a retention window on the high-volume
`polls` and `poll_errors` tables and reclaiming freed disk space. Reporting
data (events, webhook_deliveries, alarms_catalog) is never touched.

Run by the cnc-probe-maintenance systemd timer (daily), and usable by hand:

    # Default: prune to 30 days, backfill live snapshots, VACUUM
    sudo /opt/cnc-probe/venv/bin/python3 /opt/cnc-probe/maintenance.py

    # Preview only — report what would be deleted, change nothing
    sudo /opt/cnc-probe/venv/bin/python3 /opt/cnc-probe/maintenance.py --dry-run

    # Custom retention, skip the (slow) VACUUM
    sudo /opt/cnc-probe/venv/bin/python3 /opt/cnc-probe/maintenance.py --retention-days 60 --no-vacuum

Note: VACUUM needs exclusive access. If the cnc-probe service is writing
heavily it may briefly contend; running with the service stopped is cleanest
for the initial reclaim, but routine daily runs are fine while live (WAL mode).
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

import db

DEFAULT_RETENTION_DAYS = 30


def _db_size_mb() -> float:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = db.DB_PATH.with_name(db.DB_PATH.name + suffix)
        if p.exists():
            total += p.stat().st_size
    return total / 1e6


def _count_older_than(days: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = db._get_conn()
    try:
        p = conn.execute("SELECT COUNT(*) FROM polls WHERE ts < ?", (cutoff,)).fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM poll_errors WHERE ts < ?", (cutoff,)).fetchone()[0]
    finally:
        conn.close()
    return {"cutoff": cutoff, "polls": p, "poll_errors": e}


def main() -> int:
    ap = argparse.ArgumentParser(description="CNC Probe database maintenance")
    ap.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                    help=f"keep polls/poll_errors newer than this many days (default {DEFAULT_RETENTION_DAYS})")
    ap.add_argument("--no-vacuum", action="store_true", help="skip VACUUM (no disk reclaim)")
    ap.add_argument("--no-backfill", action="store_true", help="skip machine_current backfill")
    ap.add_argument("--dry-run", action="store_true", help="report only; make no changes")
    args = ap.parse_args()

    if args.retention_days < 1:
        print("FATAL: --retention-days must be >= 1", file=sys.stderr)
        return 2
    if not db.DB_PATH.exists():
        print(f"FATAL: database not found at {db.DB_PATH}", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[maintenance] {stamp}")
    print(f"[maintenance] db: {db.DB_PATH}  size: {_db_size_mb():,.1f} MB")

    # Ensure schema/WAL and the machine_current table exist.
    db.init_db()

    pending = _count_older_than(args.retention_days)
    print(f"[maintenance] retention: {args.retention_days} days (cutoff {pending['cutoff']})")
    print(f"[maintenance] eligible to delete: "
          f"{pending['polls']:,} polls, {pending['poll_errors']:,} poll_errors")

    if args.dry_run:
        print("[maintenance] --dry-run: no changes made.")
        return 0

    if not args.no_backfill:
        n = db.backfill_current()
        print(f"[maintenance] backfilled machine_current: {n} row(s) written")

    result = db.prune_history(args.retention_days)
    print(f"[maintenance] deleted: {result['polls_deleted']:,} polls, "
          f"{result['poll_errors_deleted']:,} poll_errors")

    if args.no_vacuum:
        print("[maintenance] VACUUM skipped (--no-vacuum). "
              f"Size now: {_db_size_mb():,.1f} MB (freed pages reused, file not shrunk)")
    else:
        print("[maintenance] VACUUM (reclaiming disk — may take a while)...")
        try:
            db.vacuum()
            print(f"[maintenance] VACUUM done. Size now: {_db_size_mb():,.1f} MB")
        except sqlite3.OperationalError as e:
            print(f"[maintenance] WARN: VACUUM skipped ({e}). "
                  "Old rows are deleted; disk will reclaim on the next successful VACUUM.",
                  file=sys.stderr)

    print("[maintenance] complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
