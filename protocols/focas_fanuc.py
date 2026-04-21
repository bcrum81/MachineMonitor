"""
protocols/focas_fanuc.py — FOCAS (Fanuc) protocol plugin.

Live implementation for Thread 9. Uses pyfocas 0.1 (Moritz Breurather),
a synchronous TCP FOCAS client. All pyfocas calls run inside
loop.run_in_executor() so they do not block the async poller.

What this plugin does every poll:
  - Keeps a persistent TCP socket per machine across polls
  - Calls get_sys_info() on first connect — caches model/series/version
  - Calls get_status_info() every poll — drives cycle.started /
    cycle.completed events from the `run` field
  - Calls read_macro() for each admin-configured macro:
      tool_macro, part_count_macro, program_macro, pallet_macro
  - On any I/O error, closes the socket and raises ConnectionError.
    The poller logs it and retries with exponential backoff. Next
    successful poll reconnects.
  - reset_state(machine_id) tears down the socket and clears the
    cycle-detection baseline.

Cycle event detection:
  Fanuc 30i/31i status.run values:
      0 = **** (reset / none)
      1 = STOP
      2 = HOLD (feed hold — still mid-cycle, part still clamped)
      3 = STRT (executing)
      4 = MSTR (manual movement)
  "Running" is defined as run in {2, 3}. A feed hold does NOT end the
  cycle. Transition false->true fires cycle.started; true->false fires
  cycle.completed. First poll after a (re)start establishes a silent
  baseline — no events until a real state change is observed.

Macros:
  pyfocas does not expose a program-number read directly. On Fanuc
  30i/31i controllers the running program number is typically available
  via macro #4115 (O-number modal info). Configure program_macro = 4115
  in the admin panel and the plugin will include it in the payload and
  on the dashboard.

Not implemented in Thread 9:
  - Per-code alarm events. pyfocas 0.1 only exposes a boolean alarm
    flag in statinfo, not the active alarm code or message. Leaving
    alarm event generation for a future thread.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from pyfocas.protocol.protocol import FOCAS
from pyfocas.protocol.packet import FOCASError

logger = logging.getLogger("protocols.focas_fanuc")

# ═════════════════════════════════════════════════════════════════════════════
# Plugin metadata
# ═════════════════════════════════════════════════════════════════════════════
PROTOCOL_ID          = "focas_fanuc"
DISPLAY_NAME         = "FOCAS — Matsuura / Fanuc"
DEFAULT_PORT         = 8193
REQUIRES_AUTH        = False
SUPPORTS_PALLETS     = False
DEFAULT_PALLET_COUNT = 0
IMPLEMENTED          = True

# Admin-form fields specific to this protocol. Values are persisted to
# machines.json as top-level keys (e.g. machine["tool_macro"] = 4120).
CONFIG_FIELDS = [
    {
        "name":        "tool_macro",
        "label":       "Tool Number Macro",
        "type":        "number",
        "default":     4120,
        "placeholder": "e.g. 4120",
        "hint":        "Macro that holds the currently-loaded tool number. "
                       "Leave blank to skip.",
    },
    {
        "name":        "part_count_macro",
        "label":       "Part Count Macro",
        "type":        "number",
        "default":     3901,
        "placeholder": "e.g. 3901",
        "hint":        "Macro that holds the part count. Leave blank to skip.",
    },
    {
        "name":        "program_macro",
        "label":       "Program Number Macro",
        "type":        "number",
        "default":     4115,
        "placeholder": "e.g. 4115 (O-number)",
        "hint":        "Macro that holds the running program O-number. "
                       "Fanuc 30i/31i controllers typically expose this as "
                       "#4115. Leave blank to skip.",
    },
    {
        "name":        "pallet_macro",
        "label":       "Pallet Number Macro",
        "type":        "number",
        "default":     "",
        "placeholder": "TBD on live machine",
        "hint":        "Macro that holds the active pallet number. Leave "
                       "blank if not used.",
    },
]

# Macro config field name -> (data-dict path, display label)
_MACRO_LABEL_MAP = {
    "tool_macro":       ("tool/number",         "Tool Number"),
    "part_count_macro": ("counters/part_count", "Part Count"),
    "program_macro":    ("program/number",      "Program Number"),
    "pallet_macro":     ("pallet/active",       "Active Pallet"),
}


# ═════════════════════════════════════════════════════════════════════════════
# Fanuc status helpers
# ═════════════════════════════════════════════════════════════════════════════
_RUN_NAMES = {
    0: "Reset",
    1: "Stopped",
    2: "Feed Hold",
    3: "Running",
    4: "Manual",
}


def _is_running(run_value: int) -> bool:
    """A cycle is in progress when run is STRT (3) or HOLD (2)."""
    return run_value in (2, 3)


def _status_string(stat) -> str:
    """Derive a human-readable status from a FOCASStatInfo."""
    if stat.emergency != 0:
        return "Emergency Stop"
    if stat.alarm != 0:
        return "Alarm"
    return _RUN_NAMES.get(stat.run, f"Run={stat.run}")


def _format_macro(value) -> str:
    """Format a macro value for display. Integer-valued floats drop the .0."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f.is_integer():
        return str(int(f))
    return f"{f:g}"


# ═════════════════════════════════════════════════════════════════════════════
# Per-machine runtime state
# ═════════════════════════════════════════════════════════════════════════════
# machine_id -> {
#   "client":   FOCAS or None,
#   "sys_info": FOCASSysInfo or None,
#   "running":  bool or None,   # last observed running state; None = unbaselined
# }
_state: dict[str, dict] = {}


def _get_state(machine_id: str) -> dict:
    st = _state.get(machine_id)
    if st is None:
        st = {"client": None, "sys_info": None, "running": None}
        _state[machine_id] = st
    return st


def _drop_client(machine_id: str):
    """Close and forget the socket for a machine. Never raises.

    Preserves the cycle-detection 'running' baseline on purpose — a
    transient network blip should not cause us to re-baseline and miss
    the next real edge. reset_state() is the way to wipe baseline too.
    """
    st = _state.get(machine_id)
    if not st:
        return
    client = st.get("client")
    st["client"] = None
    st["sys_info"] = None
    if client is not None and client.socket is not None:
        try:
            client.socket.close()
        except Exception:
            pass


def _connect_sync(ip: str, port: int):
    """Synchronous: create+connect a FOCAS client and read sys_info once."""
    client = FOCAS(ip, port)
    ok = client.connect()
    if not ok:
        try:
            if client.socket is not None:
                client.socket.close()
        except Exception:
            pass
        raise ConnectionError(f"FOCAS connect() returned False for {ip}:{port}")
    sys_info = client.get_sys_info()
    return client, sys_info


async def _ensure_connected(machine_id: str, ip: str, port: int):
    """Ensure a live FOCAS client for this machine. Connects if needed."""
    st = _get_state(machine_id)
    if st["client"] is not None:
        return st["client"], st["sys_info"]
    loop = asyncio.get_event_loop()
    client, sys_info = await loop.run_in_executor(
        None, _connect_sync, ip, port
    )
    st["client"] = client
    st["sys_info"] = sys_info
    logger.info(
        f"FOCAS connected {ip}:{port} — series={sys_info.series!r} "
        f"version={sys_info.version!r} axes={sys_info.axes!r}"
    )
    return client, sys_info


def _read_macro_safe(client, number: int) -> Optional[float]:
    """Read a single macro. Returns None on FOCAS-level error. Raises on
    I/O errors so the caller can tear down the socket."""
    try:
        macros = client.read_macro(number, number)
        return macros.get(number)
    except FOCASError as e:
        logger.info(f"read_macro({number}) FOCASError: {e}")
        return None


def _poll_sync(client, config: dict) -> dict:
    """All FOCAS calls for one poll tick, run in a worker thread."""
    stat = client.get_status_info()

    macros_read = {}
    for field_name in _MACRO_LABEL_MAP.keys():
        num = config.get(field_name)
        if num is None or num == "":
            continue
        try:
            num = int(num)
        except (TypeError, ValueError):
            continue
        macros_read[field_name] = _read_macro_safe(client, num)

    return {"stat": stat, "macros": macros_read}


def _build_data_dict(sys_info, result: dict) -> dict:
    """Assemble the {path: {value, type, page}} structure that polls.data
    uses. Shared between poll() and live_stream()."""
    stat = result["stat"]
    macros = result["macros"]

    model = ""
    if sys_info is not None:
        model = f"{sys_info.series} {sys_info.version}".strip()

    data = {
        "machine/model":    {"value": model,                 "type": "str", "page": "header"},
        "machine/status":   {"value": _status_string(stat),  "type": "str", "page": "header"},
        "status/run":       {"value": _RUN_NAMES.get(stat.run, str(stat.run)), "type": "str", "page": "status"},
        "status/aut":       {"value": str(stat.aut),         "type": "str", "page": "status"},
        "status/motion":    {"value": str(stat.motion),      "type": "str", "page": "status"},
        "status/mstb":      {"value": str(stat.mstb),        "type": "str", "page": "status"},
        "status/emergency": {"value": str(stat.emergency),   "type": "str", "page": "status"},
        "status/alarm":     {"value": str(stat.alarm),       "type": "str", "page": "status"},
        "status/edit":      {"value": str(stat.edit),        "type": "str", "page": "status"},
    }
    for field_name, (path, _label) in _MACRO_LABEL_MAP.items():
        if field_name in macros:
            data[path] = {
                "value": _format_macro(macros[field_name]),
                "type":  "str",
                "page":  path.split("/")[0],
            }
    return data


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — ping
# ═════════════════════════════════════════════════════════════════════════════
async def ping(config: dict) -> dict:
    ip = config.get("ip", "")
    port = int(config.get("port") or DEFAULT_PORT)
    result = {
        "protocol":  PROTOCOL_ID,
        "ip":        ip,
        "reachable": False,
        "detail":    "",
    }
    if not ip:
        result["detail"] = "IP address required."
        return result

    def _try():
        client = FOCAS(ip, port)
        try:
            if not client.connect():
                raise ConnectionError("connect() returned False")
            sys_info  = client.get_sys_info()
            stat_info = client.get_status_info()
            return sys_info, stat_info
        finally:
            try:
                if client.socket is not None:
                    client.socket.close()
            except Exception:
                pass

    try:
        loop = asyncio.get_event_loop()
        sys_info, stat_info = await loop.run_in_executor(None, _try)
        model = f"{sys_info.series} {sys_info.version}".strip() or "Fanuc"
        result["reachable"] = True
        result["detail"] = (
            f"FOCAS handshake OK — {model}, axes={sys_info.axes}, "
            f"run={stat_info.run} ({_RUN_NAMES.get(stat_info.run, '?')})"
        )
        result["machine_info"] = {
            "series":   sys_info.series,
            "version":  sys_info.version,
            "cnc_type": sys_info.cnc_type,
            "mt_type":  sys_info.mt_type,
            "axes":     sys_info.axes,
            "max_axis": sys_info.max_axis,
        }
    except Exception as e:
        result["detail"] = f"Could not open FOCAS session on {ip}:{port} — {e}"
    return result


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — poll
# ═════════════════════════════════════════════════════════════════════════════
async def poll(config: dict, tick: int) -> dict:
    machine_id = config.get("id", "")
    ip   = config.get("ip", "")
    port = int(config.get("port") or DEFAULT_PORT)

    if not machine_id:
        raise ValueError("machine id missing in config")
    if not ip:
        raise ValueError("machine ip missing in config")

    try:
        client, sys_info = await _ensure_connected(machine_id, ip, port)
    except Exception as e:
        _drop_client(machine_id)
        raise ConnectionError(f"FOCAS connect failed: {e}") from e

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _poll_sync, client, config)
    except Exception as e:
        _drop_client(machine_id)
        raise ConnectionError(f"FOCAS poll failed: {e}") from e

    data = _build_data_dict(sys_info, result)
    stat = result["stat"]

    aux = {
        "run":       stat.run,
        "alarm":     stat.alarm,
        "emergency": stat.emergency,
        "program":   data.get("program/number", {}).get("value", ""),
    }
    return {"data": data, "aux": aux}


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — detect_events (cycle only; alarm events not implemented)
# ═════════════════════════════════════════════════════════════════════════════
def detect_events(machine_id: str, machine_name: str, ts: str,
                  data: dict, aux, config: dict) -> list[dict]:
    if not isinstance(aux, dict):
        return []
    run = aux.get("run")
    if run is None:
        return []

    st = _get_state(machine_id)
    curr_running = _is_running(run)
    prev_running = st.get("running")

    if prev_running is None:
        st["running"] = curr_running
        logger.info(
            f"[{machine_name}] cycle baseline set "
            f"(run={run}, running={curr_running})"
        )
        return []

    if prev_running == curr_running:
        return []

    program = aux.get("program") or ""
    events = []
    if not prev_running and curr_running:
        events.append({
            "event_type": "cycle.started",
            "payload": {
                "event":        "cycle.started",
                "machine_id":   machine_id,
                "machine_name": machine_name,
                "pallet":       0,   # FOCAS plugin does not distinguish pallets
                "program":      program,
                "ts":           ts,
            },
        })
        logger.info(f"[{machine_name}] cycle.started program={program!r}")
    else:
        events.append({
            "event_type": "cycle.completed",
            "payload": {
                "event":        "cycle.completed",
                "machine_id":   machine_id,
                "machine_name": machine_name,
                "pallet":       0,
                "program":      program,
                "ts":           ts,
            },
        })
        logger.info(f"[{machine_name}] cycle.completed program={program!r}")

    st["running"] = curr_running
    return events


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — reset_state (called on config change, machine remove, shutdown)
# ═════════════════════════════════════════════════════════════════════════════
def reset_state(machine_id: Optional[str] = None):
    if machine_id is None:
        for mid in list(_state.keys()):
            _drop_client(mid)
        _state.clear()
        return
    _drop_client(machine_id)
    _state.pop(machine_id, None)


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — live_stream (tester / machine_view WebSocket)
# ═════════════════════════════════════════════════════════════════════════════
async def live_stream(config: dict, ws):
    from fastapi import WebSocketDisconnect  # local import — avoids circular dep

    ip   = config.get("ip", "")
    port = int(config.get("port") or DEFAULT_PORT)
    poll_interval = float(config.get("poll_interval") or 2.0)

    # Synthetic per-stream ID so we never collide with a saved machine's state
    stream_id = f"_stream_{id(ws)}"

    await ws.send_json({"type": "status",
                        "msg":  f"Connecting to FOCAS {ip}:{port} ..."})
    try:
        client, sys_info = await _ensure_connected(stream_id, ip, port)
    except Exception as e:
        await ws.send_json({"type": "error",
                            "msg":  f"FOCAS connect failed: {e}"})
        _drop_client(stream_id)
        _state.pop(stream_id, None)
        return

    model = f"{sys_info.series} {sys_info.version}".strip()
    await ws.send_json({
        "type": "status",
        "msg":  f"Connected — {model}. Streaming every {poll_interval}s.",
    })

    try:
        tick = 0
        while True:
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, _poll_sync, client, config)
            except Exception as e:
                await ws.send_json({"type": "error",
                                    "msg":  f"Poll error: {e} — dropping connection."})
                return

            data = _build_data_dict(sys_info, result)

            if tick == 0:
                await ws.send_json({
                    "type": "nodes",
                    "data": [
                        {"path": k, "node_id": k,
                         "value": v["value"], "type": v["type"]}
                        for k, v in data.items()
                    ],
                })
                await ws.send_json({
                    "type": "status",
                    "msg":  f"Streaming {len(data)} points every {poll_interval}s...",
                })

            await ws.send_json({
                "type": "poll",
                "ts":   datetime.now().isoformat(),
                "data": data,
            })
            tick += 1
            await asyncio.sleep(poll_interval)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"live_stream error: {e}")
    finally:
        _drop_client(stream_id)
        _state.pop(stream_id, None)
