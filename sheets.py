"""
sheets.py — Google Sheets auto-logging for cycle events.

Config lives in /opt/cnc-probe/config/sheets.json:
  {
    "enabled":          true,
    "sheet_id":         "1ABC...",
    "tab_name":         "Sheet1",
    "credentials_path": "/opt/cnc-probe/config/google/credentials.json"
  }

Each cycle event is appended as one row with six columns:
  [Date, Machine, Pallet, Program, Operation, Total Time (seconds)]

Where:
  Date                 = ISO-8601 UTC timestamp (e.g. 2026-04-16T18:47:18.247596+00:00)
  Machine              = machine_name (not id)
  Pallet               = 1 or 2
  Program              = program number
  Operation            = "Start" or "Complete"
  Total Time (seconds) = duration of the cycle, blank for Start rows,
                         elapsed seconds (one decimal) for Complete rows.

Total Time is computed by pairing each cycle.completed event with the most
recent cycle.started event on the same (machine_id, pallet). Pairing state
is kept in memory only — on a service restart, the first completion after
restart will log with a blank Total Time because no matching start was seen.

Writes are fire-and-forget via a background worker thread. If Sheets is
unreachable, rows queue in memory (up to MAX_QUEUE) and retry on the next
write. Nothing here blocks the event loop or polling.
"""

import json
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sheets")

CONFIG_FILE = Path("/opt/cnc-probe/config/sheets.json")

# Cap the in-memory queue so a long Sheets outage doesn't eat RAM.
MAX_QUEUE = 1000

# Retry delay (seconds) after a write failure, before pulling next job.
RETRY_DELAY = 10

# Map event type → "Operation" column value.
OP_LABEL = {
    "cycle.started":   "Start",
    "cycle.completed": "Complete",
}


# ── Config load/save ──────────────────────────────────────────────────────────
def _default_config() -> dict:
    return {
        "enabled":          False,
        "sheet_id":         "",
        "tab_name":         "Sheet1",
        "credentials_path": "/opt/cnc-probe/config/google/credentials.json",
    }


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(_default_config(), indent=2))
        return _default_config()
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        # Fill in any missing keys from defaults
        base = _default_config()
        base.update(cfg)
        return base
    except Exception as e:
        logger.error(f"Failed to read sheets.json: {e}")
        return _default_config()


def save_config(cfg: dict) -> dict:
    """Persist the given config dict. Returns the saved config."""
    merged = _default_config()
    merged.update(cfg or {})
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(merged, indent=2))
    return merged


def sanitize_for_client(cfg: dict) -> dict:
    """Nothing secret in the config itself, but return a copy for API responses."""
    return {k: v for k, v in cfg.items()}


# ── gspread client (lazy) ─────────────────────────────────────────────────────
_client_lock = threading.Lock()
_cached_client = None     # gspread.Client
_cached_fingerprint = None  # (sheet_id, credentials_path, mtime)


def _get_client(cfg: dict):
    """Build (or reuse) a gspread client. Raises on failure."""
    global _cached_client, _cached_fingerprint
    import gspread
    from google.oauth2.service_account import Credentials

    cred_path = cfg.get("credentials_path") or ""
    if not cred_path or not Path(cred_path).exists():
        raise FileNotFoundError(f"Credentials file not found: {cred_path}")

    mtime = Path(cred_path).stat().st_mtime
    fp = (cfg.get("sheet_id"), cred_path, mtime)

    with _client_lock:
        if _cached_client is not None and _cached_fingerprint == fp:
            return _cached_client

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(cred_path, scopes=scopes)
        _cached_client = gspread.authorize(creds)
        _cached_fingerprint = fp
        logger.info(f"Sheets client initialized (credentials={cred_path})")
        return _cached_client


def _invalidate_client():
    """Call when the config changes, so the next write rebuilds the client."""
    global _cached_client, _cached_fingerprint
    with _client_lock:
        _cached_client = None
        _cached_fingerprint = None


def _get_worksheet(cfg: dict):
    """Resolve to a gspread Worksheet. Raises on failure."""
    client = _get_client(cfg)
    sheet_id = (cfg.get("sheet_id") or "").strip()
    if not sheet_id:
        raise ValueError("sheet_id is not configured")
    tab = (cfg.get("tab_name") or "Sheet1").strip() or "Sheet1"
    ss = client.open_by_key(sheet_id)
    try:
        return ss.worksheet(tab)
    except Exception:
        # Tab not found — fall back to the first worksheet
        return ss.sheet1


# ── Public: test connection (used by admin "Test" button) ─────────────────────
def test_connection(cfg: dict | None = None) -> dict:
    """
    Synchronously probe the configured sheet. Returns:
      {"ok": bool, "message": str, "spreadsheet_title": str|None, "worksheet_title": str|None}
    Safe to call from request handlers — runs in the caller's thread.
    """
    cfg = cfg or load_config()
    try:
        ws = _get_worksheet(cfg)
        return {
            "ok": True,
            "message": "Connected. Worksheet is reachable and writable.",
            "spreadsheet_title": ws.spreadsheet.title,
            "worksheet_title":   ws.title,
        }
    except Exception as e:
        return {
            "ok": False,
            "message": f"{type(e).__name__}: {e}",
            "spreadsheet_title": None,
            "worksheet_title":   None,
        }


# ── Start-time pairing state (for duration on Complete rows) ──────────────────
# Key = (machine_id, pallet). Value = ISO timestamp string of the last-seen
# cycle.started event for that pallet.
_start_lock = threading.Lock()
_pending_starts: dict[tuple, str] = {}


def _record_start(machine_id: str, pallet, ts_iso: str):
    key = (str(machine_id), int(pallet) if pallet is not None else 0)
    with _start_lock:
        _pending_starts[key] = ts_iso


def _consume_start(machine_id: str, pallet) -> Optional[str]:
    """Pop and return the matching start timestamp, or None if unknown."""
    key = (str(machine_id), int(pallet) if pallet is not None else 0)
    with _start_lock:
        return _pending_starts.pop(key, None)


def _seconds_between(start_iso: str, end_iso: str) -> Optional[float]:
    """Return (end - start) in seconds (one-decimal), or None on parse failure."""
    try:
        s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        diff = (e - s).total_seconds()
        if diff < 0:
            return None
        return round(diff, 1)
    except Exception:
        return None


# ── Background writer queue ───────────────────────────────────────────────────
_queue: "queue.Queue[list]" = queue.Queue(maxsize=MAX_QUEUE)
_worker_thread: Optional[threading.Thread] = None
_worker_stop = threading.Event()


def _worker_loop():
    logger.info("Sheets worker thread started.")
    while not _worker_stop.is_set():
        try:
            row = _queue.get(timeout=1.0)
        except queue.Empty:
            continue

        # Re-check config on every write — allows hot reconfiguration without restart.
        cfg = load_config()
        if not cfg.get("enabled"):
            # Logging is disabled — drop the row silently.
            _queue.task_done()
            continue

        success = False
        try:
            ws = _get_worksheet(cfg)
            ws.append_row(row, value_input_option="USER_ENTERED")
            success = True
            logger.info(f"Sheets row appended: {row}")
        except Exception as e:
            logger.error(f"Sheets write failed (row={row}): {e}")
            _invalidate_client()

        _queue.task_done()

        if not success:
            # Back off briefly before processing the next job so we don't
            # burn CPU on a persistent auth failure. Re-queue the failed
            # row at the end of the queue so newer events can still flow.
            try:
                if _queue.qsize() < MAX_QUEUE:
                    _queue.put_nowait(row)
            except queue.Full:
                logger.error("Sheets queue full — dropping row on re-queue.")
            _worker_stop.wait(RETRY_DELAY)

    logger.info("Sheets worker thread stopped.")


def start_worker():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="sheets-worker", daemon=True
    )
    _worker_thread.start()


def stop_worker():
    _worker_stop.set()


# ── Public: enqueue event ─────────────────────────────────────────────────────
def enqueue_event(event_type: str, payload: dict):
    """
    Queue one cycle event for background write. Fire-and-forget.
    Called from webhooks.dispatch_event(). Never blocks or raises.

    Row layout: [Date, Machine, Pallet, Program, Operation, Total Time (seconds)]
    Total Time is blank on Start rows, and computed from the matching Start
    on Complete rows (or blank if no matching Start is known).
    """
    try:
        cfg = load_config()
        if not cfg.get("enabled"):
            return
        op = OP_LABEL.get(event_type)
        if op is None:
            return  # only log cycle.started / cycle.completed

        ts         = payload.get("ts") or ""
        machine    = payload.get("machine_name") or ""
        machine_id = payload.get("machine_id") or ""
        # `pallet` may legitimately be 0 (FOCAS / non-pallet machines).
        # Do NOT use `or ""` here — it would coerce 0 to empty string,
        # break the start/complete pairing, and leave a blank cell in
        # the Pallet column for every FOCAS row.
        pallet_raw = payload.get("pallet")
        program    = payload.get("program") or ""

        # Pallet column shows blank only when the payload truly omitted it;
        # a value of 0 is rendered as 0.
        pallet_cell = "" if pallet_raw is None else pallet_raw

        # Total-time handling
        duration_cell = ""   # blank by default (Start rows and unmatched Completes)

        if event_type == "cycle.started":
            # Remember this start so the next Complete on this pallet can
            # compute duration.
            if machine_id != "" and pallet_raw is not None:
                _record_start(machine_id, pallet_raw, ts)

        elif event_type == "cycle.completed":
            if machine_id != "" and pallet_raw is not None:
                start_ts = _consume_start(machine_id, pallet_raw)
                if start_ts:
                    secs = _seconds_between(start_ts, ts)
                    if secs is not None:
                        duration_cell = secs

        row = [ts, machine, pallet_cell, program, op, duration_cell]
        _queue.put_nowait(row)
    except queue.Full:
        logger.error("Sheets queue full — dropping event.")
    except Exception as e:
        logger.error(f"Sheets enqueue error: {e}")


def queue_depth() -> int:
    return _queue.qsize()
