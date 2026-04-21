"""
poller.py — Background polling manager.

Protocol-agnostic. Dispatches ping, poll, and event detection through
the protocols registry (protocols/__init__.py). Any Brother-specific
logic now lives in protocols/http_brother.py; this module no longer
knows about pallets, alarms, or HTTP scraping.

Starts one async task per configured machine on app startup.
Watches machines.json every 10 seconds; restarts tasks when config
changes. Writes every poll result to SQLite via db.py. After each
successful poll, runs event detection via the plugin and dispatches
any returned events through the webhook system.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import protocols as proto
from db import write_poll, write_error
from webhooks import dispatch_event

logger = logging.getLogger("poller")

MACHINES_FILE = Path("/opt/cnc-probe/config/machines.json")
CONFIG_CHECK_INTERVAL = 10   # seconds between config-change checks

# ── internal state ────────────────────────────────────────────────────────────
_tasks: dict[str, asyncio.Task] = {}   # machine_id → Task
_configs: dict[str, dict]       = {}   # machine_id → last-known config fingerprint + protocol
# _configs stores {"fp": str, "protocol": str} so we know which protocol owned
# a task when we stop it.


def _load_machines() -> list[dict]:
    try:
        return json.loads(MACHINES_FILE.read_text())
    except Exception as e:
        logger.error(f"Failed to load machines.json: {e}")
        return []


def _config_fingerprint(m: dict) -> str:
    """
    Return a string that changes if any polling-relevant field changes.
    Includes every top-level machine key EXCEPT ones that only affect
    display (name, utc_offset, added, id). This way, adding new plugin
    config fields (e.g. tool_macro for FOCAS) automatically triggers a
    task restart when changed.
    """
    relevant = {k: v for k, v in m.items()
                if k not in ("name", "utc_offset", "added", "id")}
    return json.dumps(relevant, sort_keys=True, default=str)


# ── per-machine polling loop ──────────────────────────────────────────────────
async def _poll_machine(machine: dict):
    machine_id    = machine["id"]
    machine_name  = machine["name"]
    protocol_id   = machine.get("protocol", "")
    poll_interval = float(machine.get("poll_interval", 2.0))

    mod = proto.get_protocol(protocol_id)
    if not mod:
        logger.warning(
            f"[{machine_name}] Unknown protocol '{protocol_id}' — task will not poll."
        )
        return
    if not getattr(mod, "IMPLEMENTED", True):
        logger.warning(
            f"[{machine_name}] Protocol '{protocol_id}' is a scaffold stub — "
            f"task will not poll."
        )
        return

    logger.info(
        f"[{machine_name}] Polling started via '{protocol_id}' every {poll_interval}s"
    )

    tick            = 0
    consecutive_err = 0
    MAX_BACKOFF     = 60

    while True:
        try:
            tick += 1
            result = await proto.dispatch_poll(machine, tick)
            ts = datetime.now(timezone.utc).isoformat()
            data = result.get("data", {}) if isinstance(result, dict) else {}
            aux  = result.get("aux")      if isinstance(result, dict) else None

            write_poll(machine_id, machine_name, ts, data)
            consecutive_err = 0

            # ── Event detection + webhook dispatch ──
            try:
                events = proto.dispatch_events(
                    machine_id, machine_name, ts, data, aux, machine
                )
                for ev in events:
                    await dispatch_event(
                        machine_id, machine_name,
                        ev["event_type"], ts, ev["payload"]
                    )
            except Exception as ev_err:
                logger.error(f"[{machine_name}] Event dispatch error: {ev_err}")

            await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.info(f"[{machine_name}] Polling task cancelled.")
            return

        except Exception as e:
            ts = datetime.now(timezone.utc).isoformat()
            write_error(machine_id, machine_name, ts, str(e))
            consecutive_err += 1
            backoff = min(poll_interval * (2 ** min(consecutive_err, 6)), MAX_BACKOFF)
            logger.warning(
                f"[{machine_name}] Poll error #{consecutive_err}: {e} — "
                f"retrying in {backoff:.0f}s"
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                logger.info(f"[{machine_name}] Polling task cancelled during backoff.")
                return


# ── task management ───────────────────────────────────────────────────────────
def _start_task(machine: dict):
    machine_id = machine["id"]
    task = asyncio.create_task(_poll_machine(machine), name=f"poll-{machine_id}")
    _tasks[machine_id] = task
    _configs[machine_id] = {
        "fp":       _config_fingerprint(machine),
        "protocol": machine.get("protocol", ""),
    }
    logger.info(f"Started polling task for machine: {machine['name']} ({machine_id})")


def _stop_task(machine_id: str):
    task = _tasks.pop(machine_id, None)
    meta = _configs.pop(machine_id, None)
    if task and not task.done():
        task.cancel()
    # Clear detection state for this machine on whichever protocol owned it.
    # If meta is missing for any reason, clear on every protocol to be safe.
    proto.dispatch_reset(meta["protocol"] if meta else None, machine_id)
    logger.info(f"Stopped polling task for machine_id: {machine_id}")


# ── config watcher ────────────────────────────────────────────────────────────
async def _watch_config():
    """
    Runs forever. Every CONFIG_CHECK_INTERVAL seconds:
      - Detects new machines     → start task
      - Detects removed machines → cancel task
      - Detects changed configs  → cancel and restart task
      - Detects dead tasks       → restart
    """
    while True:
        try:
            await asyncio.sleep(CONFIG_CHECK_INTERVAL)
            machines     = _load_machines()
            current_ids  = {m["id"] for m in machines}
            existing_ids = set(_tasks.keys())

            # Stop tasks for removed machines
            for mid in existing_ids - current_ids:
                logger.info(f"Machine {mid} removed from config — stopping task.")
                _stop_task(mid)

            for machine in machines:
                mid = machine["id"]
                new_fp = _config_fingerprint(machine)

                if mid not in _tasks:
                    _start_task(machine)
                elif _configs.get(mid, {}).get("fp") != new_fp:
                    logger.info(
                        f"Config changed for {machine['name']} ({mid}) — restarting task."
                    )
                    _stop_task(mid)
                    _start_task(machine)
                else:
                    task = _tasks[mid]
                    if task.done():
                        logger.warning(
                            f"Task for {machine['name']} ({mid}) died — restarting."
                        )
                        _stop_task(mid)
                        _start_task(machine)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Config watcher error: {e}")


# ── public API ────────────────────────────────────────────────────────────────
async def start_poller():
    """
    Call from FastAPI lifespan on startup.
    Launches one task per configured machine, then starts the config watcher.
    """
    registered = [p["id"] for p in proto.list_protocols()]
    logger.info(f"Protocols registered: {registered}")

    machines = _load_machines()
    if not machines:
        logger.info("No machines configured yet — poller standing by.")
    for machine in machines:
        _start_task(machine)
    asyncio.create_task(_watch_config(), name="poller-config-watcher")
    logger.info(f"Poller started — {len(machines)} machine(s) active.")


def stop_poller():
    """Call from FastAPI lifespan on shutdown."""
    for mid in list(_tasks.keys()):
        _stop_task(mid)
    logger.info("Poller stopped.")


def get_poller_status() -> dict:
    """Return current task states for the admin status endpoint."""
    return {
        mid: {
            "running":   not task.done(),
            "cancelled": task.cancelled() if task.done() else False,
        }
        for mid, task in _tasks.items()
    }
