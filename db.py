"""
db.py — SQLite database setup and write functions.
Database location: /opt/cnc-probe/data/cnc_data.db

Schema:
  polls table — one row per poll cycle per machine
  poll_errors table — one row per failed poll attempt
  events table — one row per detected machine event (cycle.started, cycle.completed, alarm.<CODE>)
  webhook_deliveries table — one row per webhook delivery attempt
  alarms_catalog table — one row per unique alarm code seen across all machines
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB_DIR  = Path("/opt/cnc-probe/data")
DB_PATH = DB_DIR / "cnc_data.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create database directory and tables if they don't exist. Safe to call on every startup."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id   TEXT    NOT NULL,
                machine_name TEXT    NOT NULL,
                ts           TEXT    NOT NULL,
                data         TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_polls_machine_ts ON polls (machine_id, ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS poll_errors (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id   TEXT    NOT NULL,
                machine_name TEXT    NOT NULL,
                ts           TEXT    NOT NULL,
                error        TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_errors_machine_ts ON poll_errors (machine_id, ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                machine_id   TEXT    NOT NULL,
                machine_name TEXT    NOT NULL,
                event_type   TEXT    NOT NULL,
                ts           TEXT    NOT NULL,
                payload      TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_machine_ts ON events (machine_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events (event_type, ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id        INTEGER NOT NULL,
                subscription_id TEXT    NOT NULL,
                target_url      TEXT    NOT NULL,
                attempt         INTEGER NOT NULL,
                ts              TEXT    NOT NULL,
                status_code     INTEGER NOT NULL,
                success         INTEGER NOT NULL,
                response        TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_ts ON webhook_deliveries (ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_sub ON webhook_deliveries (subscription_id, ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alarms_catalog (
                code             TEXT    PRIMARY KEY,
                message          TEXT    NOT NULL,
                level            INTEGER,
                first_seen       TEXT    NOT NULL,
                last_seen        TEXT    NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()


def write_poll(machine_id: str, machine_name: str, ts: str, data: dict):
    """Write one poll result to the polls table."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO polls (machine_id, machine_name, ts, data) VALUES (?, ?, ?, ?)",
            (machine_id, machine_name, ts, json.dumps(data)),
        )
        conn.commit()


def write_error(machine_id: str, machine_name: str, ts: str, error: str):
    """Write one poll error to the poll_errors table."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO poll_errors (machine_id, machine_name, ts, error) VALUES (?, ?, ?, ?)",
            (machine_id, machine_name, ts, error),
        )
        conn.commit()


def write_event(machine_id: str, machine_name: str, event_type: str, ts: str, payload: dict) -> int:
    """Write one detected event to the events table. Returns the new event's id."""
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (machine_id, machine_name, event_type, ts, payload) VALUES (?, ?, ?, ?, ?)",
            (machine_id, machine_name, event_type, ts, json.dumps(payload)),
        )
        conn.commit()
        return cur.lastrowid


def write_delivery(event_id: int, subscription_id: str, target_url: str,
                   attempt: int, ts: str, status_code: int, success: bool, response: str):
    """Record one webhook delivery attempt."""
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO webhook_deliveries
                (event_id, subscription_id, target_url, attempt, ts, status_code, success, response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (event_id, subscription_id, target_url, attempt, ts, status_code,
              1 if success else 0, (response or "")[:2000]))
        conn.commit()


def get_recent_events(limit: int = 100, machine_id: str | None = None,
                      event_type: str | None = None) -> list[dict]:
    """Return recent events, newest first, optionally filtered by machine or type."""
    q = "SELECT * FROM events"
    conds = []
    args = []
    if machine_id:
        conds.append("machine_id = ?")
        args.append(machine_id)
    if event_type:
        conds.append("event_type = ?")
        args.append(event_type)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [
        {
            "id":           r["id"],
            "machine_id":   r["machine_id"],
            "machine_name": r["machine_name"],
            "event_type":   r["event_type"],
            "ts":           r["ts"],
            "payload":      json.loads(r["payload"]),
        }
        for r in rows
    ]


def get_recent_deliveries(limit: int = 100, subscription_id: str | None = None) -> list[dict]:
    """Return recent delivery attempts, newest first, optionally filtered by subscription."""
    q = """
        SELECT d.*, e.event_type, e.machine_name, e.machine_id
        FROM webhook_deliveries d
        LEFT JOIN events e ON e.id = d.event_id
    """
    args = []
    if subscription_id:
        q += " WHERE d.subscription_id = ?"
        args.append(subscription_id)
    q += " ORDER BY d.id DESC LIMIT ?"
    args.append(limit)
    with _get_conn() as conn:
        rows = conn.execute(q, args).fetchall()
    return [
        {
            "id":              r["id"],
            "event_id":        r["event_id"],
            "subscription_id": r["subscription_id"],
            "target_url":      r["target_url"],
            "attempt":         r["attempt"],
            "ts":              r["ts"],
            "status_code":     r["status_code"],
            "success":         bool(r["success"]),
            "response":        r["response"],
            "event_type":      r["event_type"],
            "machine_name":    r["machine_name"],
            "machine_id":      r["machine_id"],
        }
        for r in rows
    ]


def get_latest_poll(machine_id: str) -> dict | None:
    """Return the most recent poll row for a machine, or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM polls WHERE machine_id = ? ORDER BY ts DESC LIMIT 1",
            (machine_id,),
        ).fetchone()
    if row:
        return {
            "machine_id":   row["machine_id"],
            "machine_name": row["machine_name"],
            "ts":           row["ts"],
            "data":         json.loads(row["data"]),
        }
    return None


def get_recent_polls(machine_id: str, hours: int = 24, limit: int = 100) -> list[dict]:
    """Return recent poll data for a machine within the specified time window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM polls 
            WHERE machine_id = ? AND ts >= ?
            ORDER BY ts DESC 
            LIMIT ?
        """, (machine_id, cutoff, limit)).fetchall()
    
    return [
        {
            "machine_id":   row["machine_id"],
            "machine_name": row["machine_name"],
            "ts":           row["ts"],
            "data":         json.loads(row["data"]),
        }
        for row in rows
    ]


def get_machine_timeline(machine_id: str, hours: int = 24) -> dict:
    """
    Return timeline data for a machine showing key events and status changes.
    Includes cycle completions, program changes, status changes, and errors.
    """
    polls = get_recent_polls(machine_id, hours, limit=500)
    errors = get_recent_errors(machine_id, hours)
    
    timeline = []
    prev_data = None
    
    # Process polls for significant events
    for poll in reversed(polls):  # Process chronologically
        data = poll["data"]
        ts = poll["ts"]
        
        # Check for program changes
        p1_prog = data.get("running_log/Pallet 1 program number", {}).get("value", "")
        p2_prog = data.get("running_log/Pallet 2 program number", {}).get("value", "")
        
        if prev_data:
            prev_p1 = prev_data.get("running_log/Pallet 1 program number", {}).get("value", "")
            prev_p2 = prev_data.get("running_log/Pallet 2 program number", {}).get("value", "")
            
            if p1_prog != prev_p1 and p1_prog not in ("", "--"):
                timeline.append({
                    "ts": ts,
                    "type": "program_change",
                    "message": f"Pallet 1 started program {p1_prog}",
                    "data": {"pallet": 1, "program": p1_prog}
                })
            
            if p2_prog != prev_p2 and p2_prog not in ("", "--"):
                timeline.append({
                    "ts": ts,
                    "type": "program_change", 
                    "message": f"Pallet 2 started program {p2_prog}",
                    "data": {"pallet": 2, "program": p2_prog}
                })
        
        prev_data = data
    
    # Add errors to timeline
    for error in errors:
        timeline.append({
            "ts": error["ts"],
            "type": "error",
            "message": f"Connection error: {error['error']}",
            "data": {"error": error["error"]}
        })
    
    # Sort by timestamp (most recent first)
    timeline.sort(key=lambda x: x["ts"], reverse=True)
    
    return {
        "timeline": timeline[:50],  # Limit to 50 most recent events
        "summary": {
            "total_polls": len(polls),
            "total_errors": len(errors),
            "time_window_hours": hours,
            "oldest_data": polls[-1]["ts"] if polls else None,
            "newest_data": polls[0]["ts"] if polls else None
        }
    }


def get_recent_errors(machine_id: str, hours: int = 24) -> list[dict]:
    """Return recent poll errors for a machine within the specified time window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM poll_errors 
            WHERE machine_id = ? AND ts >= ?
            ORDER BY ts DESC
        """, (machine_id, cutoff)).fetchall()
    
    return [
        {
            "machine_id":   row["machine_id"],
            "machine_name": row["machine_name"],
            "ts":           row["ts"],
            "error":        row["error"],
        }
        for row in rows
    ]


def get_machine_stats(machine_id: str, hours: int = 24) -> dict:
    """Calculate statistics for a machine over the specified time window."""
    polls = get_recent_polls(machine_id, hours)
    errors = get_recent_errors(machine_id, hours)
    
    if not polls:
        return {
            "status": "no_data",
            "polls_count": 0,
            "errors_count": len(errors),
            "uptime_percent": 0,
            "programs": [],
            "cycle_times": [],
            "efficiency": []
        }
    
    # Calculate basic stats
    total_polls = len(polls)
    error_count = len(errors)
    uptime_percent = (total_polls / (total_polls + error_count)) * 100 if (total_polls + error_count) > 0 else 0
    
    # Extract program information
    programs = set()
    cycle_times = []
    efficiency_values = []
    
    for poll in polls:
        data = poll["data"]
        
        # Collect program numbers
        for pallet in [1, 2]:
            prog = data.get(f"running_log/Pallet {pallet} program number", {}).get("value", "")
            if prog and prog not in ("", "--"):
                programs.add(prog)
        
        # Collect cycle times (Pallet 1)
        cycle_time = data.get("running_log/Pallet 1 cycle time", {}).get("value", "")
        if cycle_time and cycle_time not in ("", "--"):
            cycle_times.append(cycle_time)
        
        # Collect efficiency percentages
        efficiency = data.get("running_log/Pallet 1 cutting/cycle efficiency", {}).get("value", "")
        if efficiency and efficiency not in ("", "--", "%"):
            try:
                eff_val = float(efficiency.replace("%", "").strip())
                efficiency_values.append(eff_val)
            except ValueError:
                pass
    
    # Calculate average efficiency
    avg_efficiency = sum(efficiency_values) / len(efficiency_values) if efficiency_values else 0
    
    return {
        "status": "running" if total_polls > 0 else "offline",
        "polls_count": total_polls,
        "errors_count": error_count,
        "uptime_percent": round(uptime_percent, 1),
        "programs": sorted(list(programs)),
        "unique_programs": len(programs),
        "avg_efficiency": round(avg_efficiency, 1),
        "time_window_hours": hours,
        "last_poll": polls[0]["ts"] if polls else None
    }


def get_db_stats() -> dict:
    """Return row counts and latest poll timestamp per machine."""
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM polls").fetchone()[0]
        errors = conn.execute("SELECT COUNT(*) FROM poll_errors").fetchone()[0]
        per_machine = conn.execute("""
            SELECT machine_id, machine_name,
                   COUNT(*) as poll_count,
                   MAX(ts)  as last_poll
            FROM polls
            GROUP BY machine_id
        """).fetchall()
    return {
        "total_polls":  total,
        "total_errors": errors,
        "machines": [
            {
                "machine_id":   r["machine_id"],
                "machine_name": r["machine_name"],
                "poll_count":   r["poll_count"],
                "last_poll":    r["last_poll"],
            }
            for r in per_machine
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Thread 5 — Cycle time reports
# ─────────────────────────────────────────────────────────────────────────────
def get_cycle_report(start_ts: str, end_ts: str,
                     machine_id: str | None = None,
                     pallet: int | None = None,
                     program: str | None = None) -> list[dict]:
    """
    Return one row per completed cycle in the date range, by pairing each
    cycle.started event with the next cycle.completed event on the same
    (machine_id, pallet).

    start_ts / end_ts : ISO-8601 UTC strings. The filter matches cycles whose
                        cycle.completed timestamp falls within this window.
                        (This keeps finished cycles that *started* before the
                        window but *ended* inside it, which is usually what
                        reporting users want.)
    machine_id : optional filter
    pallet     : optional filter (1 or 2)
    program    : optional filter — matches payload.program exactly

    Returns list of dicts sorted by cycle_end descending:
      {
        "machine_id":   "...",
        "machine_name": "...",
        "pallet":       1,
        "program":      "1260",
        "cycle_start":  "2026-04-16T19:00:12.1Z",
        "cycle_end":    "2026-04-16T19:09:45.9Z",
        "duration_sec": 573.8,
      }
    Incomplete cycles (start with no matching end) are skipped.
    """
    # Pull all events that could be relevant. We pull a bit wider than the
    # window so we can match a completed-in-window event to its start that
    # happened slightly before the window start.
    q = """
        SELECT id, machine_id, machine_name, event_type, ts, payload
        FROM events
        WHERE event_type IN ('cycle.started', 'cycle.completed')
          AND ts <= ?
        ORDER BY ts ASC, id ASC
    """
    args = [end_ts]
    with _get_conn() as conn:
        rows = conn.execute(q, args).fetchall()

    # Walk chronologically. For each (machine_id, pallet), hold the last
    # seen cycle.started until a matching cycle.completed arrives.
    open_starts: dict[tuple, dict] = {}
    cycles: list[dict] = []

    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except Exception:
            continue

        pl = payload.get("pallet")
        if pl is None:
            continue

        key = (r["machine_id"], int(pl))

        if r["event_type"] == "cycle.started":
            open_starts[key] = {
                "ts":      r["ts"],
                "program": payload.get("program") or "",
                "machine_name": r["machine_name"],
            }
        elif r["event_type"] == "cycle.completed":
            start = open_starts.pop(key, None)
            if start is None:
                # No matching start — skip (incomplete cycle)
                continue
            # Only emit if cycle.completed falls in the requested window
            if r["ts"] < start_ts or r["ts"] > end_ts:
                continue
            # Apply filters
            if machine_id and r["machine_id"] != machine_id:
                continue
            if pallet is not None and int(pl) != int(pallet):
                continue
            # Program filter: prefer completed event's program, fall back to start's program
            prog_used = payload.get("program") or start["program"] or ""
            if program and prog_used != program:
                continue

            duration = _iso_seconds_between(start["ts"], r["ts"])
            cycles.append({
                "machine_id":   r["machine_id"],
                "machine_name": r["machine_name"] or start["machine_name"],
                "pallet":       int(pl),
                "program":      prog_used,
                "cycle_start":  start["ts"],
                "cycle_end":    r["ts"],
                "duration_sec": duration,
            })

    cycles.sort(key=lambda c: c["cycle_end"], reverse=True)
    return cycles


def _iso_seconds_between(start_iso: str, end_iso: str) -> float:
    """Parse two ISO-8601 strings and return (end - start) in seconds, rounded to 0.1s.
    Returns 0.0 if either parse fails."""
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return round((e - s).total_seconds(), 1)
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Thread 7 — Alarm catalog
# ─────────────────────────────────────────────────────────────────────────────
def register_alarm_code(code: str, message: str, level: int | None, ts: str):
    """
    Ensure a code is present in the alarms_catalog.
    Does NOT increment occurrence_count. Used by the baseline path so
    that restarting the service doesn't inflate counts.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT code FROM alarms_catalog WHERE code = ?",
            (code,),
        ).fetchone()
        if row:
            conn.execute("""
                UPDATE alarms_catalog
                SET message = ?, level = ?, last_seen = ?
                WHERE code = ?
            """, (message, level, ts, code))
        else:
            conn.execute("""
                INSERT INTO alarms_catalog
                    (code, message, level, first_seen, last_seen, occurrence_count)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (code, message, level, ts, ts))
        conn.commit()


def record_alarm_occurrence(code: str, message: str, level: int | None, ts: str):
    """
    Insert or update a catalog row AND increment occurrence_count by 1.
    Used when a brand-new alarm (idx > last seen) is detected.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT code FROM alarms_catalog WHERE code = ?",
            (code,),
        ).fetchone()
        if row:
            conn.execute("""
                UPDATE alarms_catalog
                SET message = ?, level = ?, last_seen = ?,
                    occurrence_count = occurrence_count + 1
                WHERE code = ?
            """, (message, level, ts, code))
        else:
            conn.execute("""
                INSERT INTO alarms_catalog
                    (code, message, level, first_seen, last_seen, occurrence_count)
                VALUES (?, ?, ?, ?, ?, 1)
            """, (code, message, level, ts, ts))
        conn.commit()


def get_alarm_catalog() -> list[dict]:
    """Return all known alarm codes, sorted by code."""
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT code, message, level, first_seen, last_seen, occurrence_count
            FROM alarms_catalog
            ORDER BY code
        """).fetchall()
    return [
        {
            "code":             r["code"],
            "message":          r["message"] or "",
            "level":            r["level"],
            "first_seen":       r["first_seen"],
            "last_seen":        r["last_seen"],
            "occurrence_count": r["occurrence_count"],
        }
        for r in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Current-cycle helpers — added for accurate dashboard cycle time on FOCAS
# ─────────────────────────────────────────────────────────────────────────────
def get_current_cycle_start(machine_id: str) -> dict | None:
    """
    Return info about the most recent UNMATCHED cycle.started event for a
    machine — i.e. a cycle.started that has not yet been followed by a
    cycle.completed. Used by the public dashboard so the FOCAS card's
    cycle-time counter starts from the actual cycle start time, not from
    the moment the browser loaded the page.

    Returns:
        {"ts": "<iso-utc>", "program": "<str>"} if a current cycle is in
        progress, or None if the most recent cycle event is a completion
        (i.e. nothing currently running).

    Logic: look at the last cycle.* event for this machine. If it's
    cycle.started -> we're mid-cycle, return its timestamp. If it's
    cycle.completed -> not running, return None.
    """
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT event_type, ts, payload
              FROM events
             WHERE machine_id = ?
               AND event_type IN ('cycle.started', 'cycle.completed')
             ORDER BY id DESC
             LIMIT 1
        """, (machine_id,)).fetchone()
    if row is None:
        return None
    if row["event_type"] != "cycle.started":
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        payload = {}
    return {
        "ts":      row["ts"],
        "program": payload.get("program", ""),
    }


def get_last_cycle_completed(machine_id: str) -> dict | None:
    """
    Return info about the most recent cycle.completed event for a machine.
    Used by the FOCAS dashboard card to show 'Last Cycle End' since the
    FOCAS plugin doesn't expose that directly in poll data.

    Returns {"ts": "<iso-utc>", "program": "<str>"} or None if the
    machine has no completed cycles yet.
    """
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT ts, payload
              FROM events
             WHERE machine_id = ?
               AND event_type = 'cycle.completed'
             ORDER BY id DESC
             LIMIT 1
        """, (machine_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        payload = {}
    return {
        "ts":      row["ts"],
        "program": payload.get("program", ""),
    }
