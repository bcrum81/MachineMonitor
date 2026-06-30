#!/usr/bin/env python3
"""
cleanup_events.py — One-time database cleanup for the events table.

Deletes bogus cycle events accumulated before the offline-watchdog and
O9xxx-subprogram filter were added. Does NOT touch the polls table —
only events.

Bad-data criteria (deleted automatically):
  1. Orphaned cycle.started events with no matching cycle.completed within
     a reasonable window (default 4h). These are leftovers from machine
     outages where the machine went offline mid-cycle.
  2. cycle.completed events whose program payload is junk:
       - empty / blank / "0"
       - "O0", "O1" (Matsuura subprogram-call artifacts)
       - "O9xxx" range (Fanuc-builder macros)
  3. Cycle pairs whose duration exceeds the hard maximum (default 4 hours).
     No real machine cycle in your shop runs that long.
  4. Cycle pairs that have a "polling gap" between start and end — i.e.
     no successful polls were recorded for that machine in a window of
     more than 5 minutes during the cycle. This catches stale-pallet
     events that span weekends / outages.

Usage:
  Dry-run (default — shows what would be deleted, deletes nothing):
      sudo /opt/cnc-probe/venv/bin/python3 /opt/cnc-probe/cleanup_events.py

  Apply the cleanup:
      sudo /opt/cnc-probe/venv/bin/python3 /opt/cnc-probe/cleanup_events.py --apply

Recommended workflow:
  1. Stop the cnc-probe service first to avoid races:
        sudo systemctl stop cnc-probe
  2. Dry-run, review the summary
  3. Apply
  4. Restart:
        sudo systemctl start cnc-probe
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/opt/cnc-probe/data/cnc_data.db")

# ── Tunable thresholds ──────────────────────────────────────────────────────
MAX_CYCLE_DURATION_SEC   = 4 * 3600     # 4 hours — anything longer is bogus
ORPHAN_START_TIMEOUT_SEC = 4 * 3600     # cycle.started with no matching end after this
MAX_POLL_GAP_SEC         = 5 * 60       # 5 min gap in polls during a cycle = bogus
SUBPROGRAM_RE = re.compile(r"^[89]\d{3}$")  # 8000-9999 with leading O already stripped

JUNK_PROGRAMS = {"", "0", "O0", "o0", "O1", "o1"}


def is_junk_program(prog: str) -> bool:
    """Return True if the program string looks like a subprogram artifact."""
    if not prog:
        return True
    p = prog.strip()
    if p in JUNK_PROGRAMS:
        return True
    # Strip leading O for the 9xxx check (handles both old O9001 and new 9001)
    if p[0] in ("O", "o"):
        p_no_o = p[1:]
    else:
        p_no_o = p
    if SUBPROGRAM_RE.match(p_no_o):
        return True
    return False


def parse_iso(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def has_poll_gap(conn, machine_id: str, start_ts: str, end_ts: str,
                 max_gap_sec: int) -> bool:
    """
    Return True if there's a gap between any two consecutive polls for this
    machine within [start_ts, end_ts] longer than max_gap_sec, OR if no polls
    exist in that window at all.
    """
    rows = conn.execute("""
        SELECT ts FROM polls
        WHERE machine_id = ? AND ts >= ? AND ts <= ?
        ORDER BY ts ASC
    """, (machine_id, start_ts, end_ts)).fetchall()
    if not rows:
        return True  # zero polls during the cycle = obvious gap
    times = [parse_iso(r[0]) for r in rows]
    times = [t for t in times if t is not None]
    if not times:
        return True
    # Check gap between cycle start and first observed poll
    s = parse_iso(start_ts)
    e = parse_iso(end_ts)
    if s is None or e is None:
        return False
    if (times[0] - s).total_seconds() > max_gap_sec:
        return True
    if (e - times[-1]).total_seconds() > max_gap_sec:
        return True
    # Inter-poll gaps
    for i in range(1, len(times)):
        if (times[i] - times[i - 1]).total_seconds() > max_gap_sec:
            return True
    return False


def find_bad_events(conn) -> tuple[list, list, dict]:
    """
    Walk the events table, identify bad events, return:
      - to_delete: list of event ids to delete
      - reasons:   list of (id, reason) tuples for the report
      - stats:     dict of counts per reason
    """
    rows = conn.execute("""
        SELECT id, machine_id, machine_name, event_type, ts, payload
        FROM events
        WHERE event_type IN ('cycle.started', 'cycle.completed')
        ORDER BY ts ASC, id ASC
    """).fetchall()

    to_delete: list[int] = []
    reasons: list[tuple] = []
    stats = {
        "junk_program_completed":     0,
        "junk_program_started":       0,
        "orphaned_started":           0,
        "duration_exceeds_max":       0,
        "spans_poll_gap":             0,
    }

    open_starts: dict[tuple, dict] = {}  # (machine_id, pallet) -> event row

    for r in rows:
        eid          = r["id"]
        machine_id   = r["machine_id"]
        machine_name = r["machine_name"]
        event_type   = r["event_type"]
        ts           = r["ts"]
        try:
            payload = json.loads(r["payload"])
        except Exception:
            payload = {}
        pallet = payload.get("pallet", 0)
        program = (payload.get("program") or "").strip()

        # Junk program → delete immediately, regardless of pairing
        if is_junk_program(program):
            to_delete.append(eid)
            if event_type == "cycle.completed":
                stats["junk_program_completed"] += 1
                reasons.append((eid, f"{event_type} {machine_name} pallet={pallet} program={program!r} → junk"))
            else:
                stats["junk_program_started"] += 1
                reasons.append((eid, f"{event_type} {machine_name} pallet={pallet} program={program!r} → junk"))
            # If this was a cycle.started we were tracking, drop it from open_starts too
            key = (machine_id, int(pallet) if isinstance(pallet, (int, float)) else 0)
            if event_type == "cycle.started":
                open_starts.pop(key, None)
            continue

        key = (machine_id, int(pallet) if isinstance(pallet, (int, float)) else 0)

        if event_type == "cycle.started":
            # If there was already an open start for this pallet, the previous
            # one is orphaned (no completed ever showed up). Mark it for deletion.
            existing = open_starts.get(key)
            if existing is not None:
                prev_ts = existing["ts"]
                pts = parse_iso(prev_ts)
                cts = parse_iso(ts)
                if pts and cts and (cts - pts).total_seconds() > ORPHAN_START_TIMEOUT_SEC:
                    to_delete.append(existing["id"])
                    stats["orphaned_started"] += 1
                    reasons.append((
                        existing["id"],
                        f"cycle.started orphaned ({existing['machine_name']} pallet={existing['pallet']}) "
                        f"— never paired, replaced after {(cts - pts).total_seconds() / 60:.0f} min"
                    ))
            open_starts[key] = {
                "id": eid, "ts": ts, "machine_name": machine_name, "pallet": pallet,
                "program": program,
            }

        elif event_type == "cycle.completed":
            start = open_starts.pop(key, None)
            if start is None:
                # Orphaned completed (no matching started) — leave it alone
                # since it could pair with a started outside our window
                continue
            sts = parse_iso(start["ts"])
            ets = parse_iso(ts)
            if sts is None or ets is None:
                continue
            duration = (ets - sts).total_seconds()
            if duration > MAX_CYCLE_DURATION_SEC:
                to_delete.append(start["id"])
                to_delete.append(eid)
                stats["duration_exceeds_max"] += 1
                reasons.append((
                    eid,
                    f"PAIR {machine_name} pallet={pallet} program={program!r} "
                    f"duration={duration / 60:.1f}min EXCEEDS max — both events deleted"
                ))
                continue
            if has_poll_gap(conn, machine_id, start["ts"], ts, MAX_POLL_GAP_SEC):
                to_delete.append(start["id"])
                to_delete.append(eid)
                stats["spans_poll_gap"] += 1
                reasons.append((
                    eid,
                    f"PAIR {machine_name} pallet={pallet} program={program!r} "
                    f"duration={duration / 60:.1f}min — poll gap during cycle, both events deleted"
                ))

    # Any remaining open_starts that are old enough → orphaned
    if rows:
        last_ts = parse_iso(rows[-1]["ts"])
        if last_ts:
            for key, start in open_starts.items():
                sts = parse_iso(start["ts"])
                if sts and (last_ts - sts).total_seconds() > ORPHAN_START_TIMEOUT_SEC:
                    to_delete.append(start["id"])
                    stats["orphaned_started"] += 1
                    reasons.append((
                        start["id"],
                        f"cycle.started orphaned ({start['machine_name']} pallet={start['pallet']}) "
                        f"— never paired"
                    ))

    # Deduplicate the to_delete list (a pair may add the same id twice)
    seen = set()
    deduped = []
    for eid in to_delete:
        if eid not in seen:
            seen.add(eid)
            deduped.append(eid)
    return deduped, reasons, stats


def main():
    parser = argparse.ArgumentParser(description="Clean bogus cycle events.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete. Without this, dry-run only.")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"FATAL: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print(f"  CNC events cleanup — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 70)
    print(f"Database:                  {DB_PATH}")
    print(f"Mode:                      {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Max cycle duration:        {MAX_CYCLE_DURATION_SEC / 60:.0f} min")
    print(f"Max poll gap during cycle: {MAX_POLL_GAP_SEC / 60:.0f} min")
    print(f"Orphan start timeout:      {ORPHAN_START_TIMEOUT_SEC / 60:.0f} min")
    print()

    total_events_before = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type LIKE 'cycle.%'"
    ).fetchone()[0]
    print(f"Total cycle.* events in DB: {total_events_before}")
    print()
    print("Scanning...")
    print()

    to_delete, reasons, stats = find_bad_events(conn)

    print("─" * 70)
    print("  Findings:")
    print("─" * 70)
    print(f"  cycle.completed with junk program: {stats['junk_program_completed']:>4d}")
    print(f"  cycle.started with junk program:   {stats['junk_program_started']:>4d}")
    print(f"  Orphaned cycle.started events:     {stats['orphaned_started']:>4d}")
    print(f"  Cycle pairs over max duration:     {stats['duration_exceeds_max']:>4d}")
    print(f"  Cycle pairs spanning poll gaps:    {stats['spans_poll_gap']:>4d}")
    print(f"  ─────────────────────────────────────")
    print(f"  Total events to delete:            {len(to_delete):>4d}")
    print(f"  Total events that survive:         {total_events_before - len(to_delete):>4d}")
    print()

    if reasons:
        print("─" * 70)
        print("  Per-event detail (most recent first):")
        print("─" * 70)
        # Print reasons in reverse so newest are shown first
        for eid, msg in reversed(reasons[:200]):
            print(f"  [{eid:>5d}] {msg}")
        if len(reasons) > 200:
            print(f"  ... and {len(reasons) - 200} more")
        print()

    if not to_delete:
        print("Nothing to delete. Database is clean.")
        return

    if not args.apply:
        print("─" * 70)
        print("  DRY-RUN COMPLETE — no changes made.")
        print("  Run again with --apply to delete the events listed above.")
        print("─" * 70)
        return

    # Apply mode
    print("─" * 70)
    print(f"  APPLYING — deleting {len(to_delete)} events...")
    print("─" * 70)
    chunk = 500
    deleted = 0
    for i in range(0, len(to_delete), chunk):
        batch = to_delete[i:i + chunk]
        placeholders = ",".join("?" * len(batch))
        cur = conn.execute(
            f"DELETE FROM events WHERE id IN ({placeholders})",
            batch,
        )
        deleted += cur.rowcount
    conn.commit()
    print(f"Deleted {deleted} rows.")

    total_events_after = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type LIKE 'cycle.%'"
    ).fetchone()[0]
    print(f"Cycle events remaining:    {total_events_after}")
    print()
    print("Done. You can now restart the service:")
    print("  sudo systemctl start cnc-probe")


if __name__ == "__main__":
    main()
