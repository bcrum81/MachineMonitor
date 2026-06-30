"""
protocols/focas_fanuc.py — FOCAS (Fanuc) protocol plugin.

Live implementation:
  - Persistent TCP socket per machine across polls
  - get_sys_info() at first connect — model/series/version
  - get_status_info() every poll — drives cycle.started / cycle.completed
  - FOCAS function 0xB9 every poll — returns the MAIN program path
    (e.g. //DATA_SV/O0146.NC) which is rock-solid stable during O8xxx/
    O9xxx subprogram excursions. This is the source of truth for
    cycle event payloads and dashboard display.
  - FOCAS function 0xCF every poll — returns the EXECUTING program
    name (the subprogram if one is running). Surfaced as a diagnostic
    field but NEVER drives cycle events. Helps debugging.
  - read_macro() for admin-configured macros (tool, part-count, etc.)
  - In ping(): also reads #3011 (date) and #3012 (time) to surface the
    controller's wall-clock for the admin "test connection" workflow.
    Returns the implied utc_offset to help operators set machine config
    correctly. Not read on every poll — too costly per tick.
  - On any I/O error, drops the socket and raises ConnectionError.

Cycle event detection:
  Sticky-program logic. The sticky is updated ONLY from 0xB9 (main
  program). Subprogram excursions never pollute the sticky.
    - cycle.started fires when run transitions to {2,3} and we have
      ANY sticky program (lenient — could be the previous job's main
      program if 0xB9 is briefly empty, but in practice 0xB9 is always
      populated once a real job has been observed).
    - cycle.completed fires when run leaves {2,3} and a sticky was set.
    - Subprogram artifacts (O8xxx/O9xxx) never appear in event payloads.

Offline watchdog hook:
  force_close_cycles() is called by the poller after the machine has
  been unreachable for the watchdog timeout. Emits cycle.completed
  using the sticky program and the supplied ts.
"""

import asyncio
import logging
import re
import struct
from datetime import datetime, timezone
from typing import Optional

from pyfocas.protocol.protocol import FOCAS
from pyfocas.protocol.packet import (
    ControlDevice,
    PacketOrigin,
    PacketType,
    RESPONSE_BUFFER_SIZE,
    FOCASError,
    create_packet,
    extract_focas_packet,
)

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
        "label":       "Program Number Macro (fallback)",
        "type":        "number",
        "default":     "",
        "placeholder": "Leave blank — direct read used",
        "hint":        "Macro fallback for program number. Most Fanuc 30i/31i "
                       "machines now use the direct main-program read (FOCAS "
                       "0xB9) and don't need this. Set 4115 only if the "
                       "direct read fails on your controller.",
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
    {
        "name":        "ignored_programs",
        "label":       "Ignored Programs",
        "type":        "text",
        "default":     "",
        "placeholder": "e.g. 1, 0001",
        "hint":        "Comma-separated list of program numbers to ignore. "
                       "Programs in this list will not display on the dashboard "
                       "and will not fire cycle events. Useful for setup or "
                       "test programs that aren't real production cycles. "
                       "Leading zeros are optional — '1' and '0001' both match.",
    },
]

_MACRO_LABEL_MAP = {
    "tool_macro":       ("tool/number",         "Tool Number"),
    "part_count_macro": ("counters/part_count", "Part Count"),
    "program_macro":    ("program/macro",       "Program Macro Value"),
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
    return run_value in (2, 3)


def _status_string(stat) -> str:
    if stat.emergency != 0:
        return "Emergency Stop"
    if stat.alarm != 0:
        return "Alarm"
    return _RUN_NAMES.get(stat.run, f"Run={stat.run}")


def _format_macro(value) -> str:
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f.is_integer():
        return str(int(f))
    return f"{f:g}"


def _normalize_program_name(name: str) -> str:
    """Strip path, .NC extension, and leading O/o.
    Examples:
        '//DATA_SV/O0146.NC' -> '0146'
        'O0146.NC'           -> '0146'
        'O0146'              -> '0146'
    """
    if not name:
        return ""
    # Strip path
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    # Strip extension
    if "." in name:
        name = name.rsplit(".", 1)[0]
    # Strip leading O/o
    if name and name[0] in ("O", "o"):
        name = name[1:]
    return name


_SUBPROGRAM_RE = re.compile(r"^[89]\d{3}$")


def _is_subprogram(normalized_name: str) -> bool:
    """True for O8000-O9999 builder/tool-change macros."""
    if not normalized_name:
        return False
    return bool(_SUBPROGRAM_RE.match(normalized_name))


def _is_ignored_program(program: str, config: dict) -> bool:
    """True if `program` (already normalized — no path, no .NC, no leading
    O) matches any entry in the machine's `ignored_programs` config list.

    Comparison is leading-zero-insensitive so '1' and '0001' both match
    a single config entry of either form.
    """
    if not program:
        return False
    raw = (config.get("ignored_programs") or "").strip()
    if not raw:
        return False
    program_stripped = program.lstrip("0") or "0"
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        entry_stripped = entry.lstrip("0") or "0"
        if program == entry or program_stripped == entry_stripped:
            return True
    return False


# ═════════════════════════════════════════════════════════════════════════════
# Raw FOCAS function callers (functions pyfocas doesn't expose)
# ═════════════════════════════════════════════════════════════════════════════
def _build_raw_packet(fn_code: int) -> bytes:
    cmd = struct.pack(">HHH",
                      int(ControlDevice.CNC),
                      (fn_code >> 8) & 0xFF,
                      fn_code & 0xFF)
    return create_packet(
        PacketOrigin.CLIENT,
        PacketType.GENERIC_REQUEST,
        cmd + struct.pack(">iiiii", 0, 0, 0, 0, 0),
    )


def _send_and_extract_body(client, fn_code: int) -> Optional[bytes]:
    """Fire a 0-payload FOCAS request, return the parsed subpacket body."""
    if client.socket is None:
        return None
    try:
        client.socket.sendall(_build_raw_packet(fn_code))
        raw = client.socket.recv(RESPONSE_BUFFER_SIZE)
    except (OSError, TimeoutError):
        raise
    try:
        parsed = extract_focas_packet(raw)
    except Exception as e:
        logger.info(f"FOCAS fn 0x{fn_code:02X} extract failed: {e}")
        return None
    if not parsed.data or not isinstance(parsed.data, list):
        return None
    sub = parsed.data[0]
    if not isinstance(sub, (bytes, bytearray)) or len(sub) < 14:
        return None
    return bytes(sub)


def _extract_first_string(body: bytes, offset: int = 14, min_len: int = 3) -> str:
    """Pull the first run of printable ASCII >= min_len from `body`
    starting at `offset`. Returns '' if none found."""
    if not body:
        return ""
    start = -1
    for i in range(offset, len(body)):
        b = body[i]
        printable = 0x20 <= b <= 0x7E
        if printable and start < 0:
            start = i
        elif not printable and start >= 0:
            if i - start >= min_len:
                return body[start:i].decode("ascii", errors="replace").strip()
            start = -1
    if start >= 0 and len(body) - start >= min_len:
        return body[start:].decode("ascii", errors="replace").strip()
    return ""


def _read_main_program_path_sync(client) -> Optional[str]:
    """
    FOCAS function 0xB9 — returns the MAIN program path/name.
    Stable during subprogram excursions; this is the canonical source
    for "what part program is the operator running right now."

    Response body layout (from live-machine inspection):
      [0:6]  command echo (00 01 00 00 00 B9)
      [6:14] standard reply prefix
      [14:?] ASCII null-terminated main program path (e.g. '//DATA_SV/O0146.NC\0')
      [?: ]  ASCII null-terminated scheduled-program slot
             (e.g. 'ET-SCHDL-PROG/O1\0')

    We take ONLY the first ASCII run starting at offset 14. The scheduled-
    program slot is ignored on purpose — that's where bogus "O1" came from.
    """
    body = _send_and_extract_body(client, 0xB9)
    if body is None:
        return None
    first = _extract_first_string(body, offset=14, min_len=3)
    return first or None


def _read_exe_program_name_sync(client) -> Optional[str]:
    """
    FOCAS function 0xCF — returns the EXECUTING program name. Flips to
    O8xxx/O9xxx during subprograms. Surfaced as a diagnostic only;
    never drives cycle events or sticky updates.
    """
    body = _send_and_extract_body(client, 0xCF)
    if body is None:
        return None
    # On 0xCF, the name often appears at offset 18 (4 extra header bytes).
    # Try 18 first, then 14.
    for off in (18, 14):
        s = _extract_first_string(body, offset=off, min_len=3)
        if s and (s[0] in "ON" or any(c.isdigit() for c in s) or "#" in s):
            return s
    return None


# ═════════════════════════════════════════════════════════════════════════════
# Per-machine runtime state
# ═════════════════════════════════════════════════════════════════════════════
# machine_id -> {
#   "client":          FOCAS or None,
#   "sys_info":        FOCASSysInfo or None,
#   "running":         bool or None,    # last observed; None = unbaselined
#   "sticky_program":  str,             # last main-program seen (from 0xB9)
# }
_state: dict[str, dict] = {}


def _get_state(machine_id: str) -> dict:
    st = _state.get(machine_id)
    if st is None:
        st = {
            "client":         None,
            "sys_info":       None,
            "running":        None,
            "sticky_program": "",
        }
        _state[machine_id] = st
    return st


def _drop_client(machine_id: str):
    """Close and forget the socket. Preserves sticky + running-baseline."""
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
    st = _get_state(machine_id)
    if st["client"] is not None:
        return st["client"], st["sys_info"]
    loop = asyncio.get_event_loop()
    client, sys_info = await loop.run_in_executor(None, _connect_sync, ip, port)
    st["client"] = client
    st["sys_info"] = sys_info
    logger.info(
        f"FOCAS connected {ip}:{port} — series={sys_info.series!r} "
        f"version={sys_info.version!r} axes={sys_info.axes!r}"
    )
    return client, sys_info


def _read_macro_safe(client, number: int) -> Optional[float]:
    try:
        macros = client.read_macro(number, number)
        return macros.get(number)
    except FOCASError as e:
        logger.info(f"read_macro({number}) FOCASError: {e}")
        return None


def _poll_sync(client, config: dict) -> dict:
    stat = client.get_status_info()

    # Main program path (0xB9) — primary source for sticky/display
    try:
        main_program = _read_main_program_path_sync(client)
    except (OSError, TimeoutError):
        raise
    except Exception as e:
        logger.info(f"main_program (0xB9) read failed: {e}")
        main_program = None

    # Executing program (0xCF) — diagnostic only
    try:
        exe_program = _read_exe_program_name_sync(client)
    except (OSError, TimeoutError):
        raise
    except Exception as e:
        logger.info(f"exe_program (0xCF) read failed: {e}")
        exe_program = None

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

    return {
        "stat":         stat,
        "main_program": main_program,   # from 0xB9
        "exe_program":  exe_program,    # from 0xCF
        "macros":       macros_read,
    }


def _resolve_main_program(main_program: Optional[str], macros: dict) -> str:
    """Pick the canonical main program for sticky/display.
    Priority:
      1. Direct main-program read from 0xB9 (normalized)
      2. program_macro fallback (only useful for in-CNC-memory programs)
    Subprograms returned by these sources are filtered by the caller.
    """
    if main_program:
        return _normalize_program_name(main_program)
    macro_val = macros.get("program_macro")
    if macro_val is not None:
        formatted = _format_macro(macro_val)
        if formatted and formatted != "0":
            return formatted.zfill(4) if formatted.isdigit() else formatted
    return ""


def _build_data_dict(sys_info, result: dict, sticky_program: str,
                     config: Optional[dict] = None) -> dict:
    """Build the {path: {value,type,page}} dict that polls.data uses."""
    config = config or {}
    stat = result["stat"]
    macros = result["macros"]
    main_program = result.get("main_program")
    exe_program = result.get("exe_program")

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

    # Display priority:
    #   1. Sticky (always populated once a real main program is observed)
    #   2. Fresh main-program read (if sticky not yet set), but never if
    #      it's a subprogram or an admin-ignored setup program
    #   3. Empty — never display the executing program when it's a subprogram
    raw_main = _resolve_main_program(main_program, macros)
    if sticky_program:
        display_program = sticky_program
    elif (raw_main
          and not _is_subprogram(raw_main)
          and not _is_ignored_program(raw_main, config)):
        display_program = raw_main
    else:
        display_program = ""

    data["program/number"] = {
        "value": display_program,
        "type":  "str",
        "page":  "program",
    }

    # Diagnostic fields — useful in machine detail view to verify the
    # sticky logic is doing the right thing.
    if main_program:
        data["program/main_path"] = {
            "value": main_program,
            "type":  "str",
            "page":  "program",
        }
    if exe_program:
        data["program/executing"] = {
            "value": exe_program,
            "type":  "str",
            "page":  "program",
        }
    if raw_main:
        data["program/raw"] = {
            "value": raw_main,
            "type":  "str",
            "page":  "program",
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
# Clock helper for ping() — reads #3011 and #3012 to get controller wall-clock
# ═════════════════════════════════════════════════════════════════════════════
def _read_machine_clock_sync(client) -> Optional[str]:
    """
    Reads macros #3011 (date YYYYMMDD) and #3012 (time HHMMSS) and
    returns an ISO-8601-ish string 'YYYY-MM-DD HH:MM:SS' representing
    the controller's wall-clock. Returns None on read failure.
    Used only by ping(), not the per-poll path.
    """
    try:
        d_val = _read_macro_safe(client, 3011)
        t_val = _read_macro_safe(client, 3012)
    except Exception as e:
        logger.info(f"clock-read macros failed: {e}")
        return None
    if d_val is None or t_val is None:
        return None
    try:
        d_int = int(d_val)
        t_int = int(t_val)
    except (TypeError, ValueError):
        return None
    if d_int <= 0 or t_int < 0:
        return None
    yyyy = d_int // 10000
    mm   = (d_int // 100) % 100
    dd   = d_int % 100
    hh   = t_int // 10000
    mi   = (t_int // 100) % 100
    ss   = t_int % 100
    if not (1 <= mm <= 12 and 1 <= dd <= 31 and 0 <= hh <= 23
            and 0 <= mi <= 59 and 0 <= ss <= 59):
        return None
    return f"{yyyy:04d}-{mm:02d}-{dd:02d} {hh:02d}:{mi:02d}:{ss:02d}"


def _suggest_offset(controller_wall_clock: str) -> Optional[float]:
    """
    Given the controller's wall-clock as 'YYYY-MM-DD HH:MM:SS', return the
    drift between that clock and the SHOP PC'S local time, in hours,
    rounded to 0.5.

    Result is negative if the controller is behind local, positive if
    ahead. Stored in machines.json as `clock_offset` and used by the
    dashboard to correct machine-supplied timestamps for display.
    """
    if not controller_wall_clock:
        return None
    try:
        ctrl = datetime.strptime(controller_wall_clock, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    # datetime.now() with no tz returns the shop PC's local time, which is
    # exactly what we want to compare against — operators care about local
    # wall-clock, not UTC.
    now_local = datetime.now()
    delta_hours = (now_local - ctrl).total_seconds() / 3600.0
    return round(delta_hours * 2) / 2


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
            main = None
            try:
                main = _read_main_program_path_sync(client)
            except Exception:
                pass
            ctrl_clock = None
            try:
                ctrl_clock = _read_machine_clock_sync(client)
            except Exception:
                pass
            return sys_info, stat_info, main, ctrl_clock
        finally:
            try:
                if client.socket is not None:
                    client.socket.close()
            except Exception:
                pass

    try:
        loop = asyncio.get_event_loop()
        sys_info, stat_info, main, ctrl_clock = await loop.run_in_executor(None, _try)
        model = f"{sys_info.series} {sys_info.version}".strip() or "Fanuc"
        program_bit = f", program={main}" if main else ""
        result["reachable"] = True
        result["detail"] = (
            f"FOCAS handshake OK — {model}, axes={sys_info.axes}, "
            f"run={stat_info.run} ({_RUN_NAMES.get(stat_info.run, '?')})"
            f"{program_bit}"
        )
        result["machine_info"] = {
            "series":   sys_info.series,
            "version":  sys_info.version,
            "cnc_type": sys_info.cnc_type,
            "mt_type":  sys_info.mt_type,
            "axes":     sys_info.axes,
            "max_axis": sys_info.max_axis,
            "program":  main or "",
        }
        if ctrl_clock:
            result["controller_clock"]    = ctrl_clock
            result["server_clock_local"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sugg = _suggest_offset(ctrl_clock)
            if sugg is not None:
                result["suggested_clock_offset"] = sugg
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

    stat = result["stat"]
    st = _get_state(machine_id)
    currently_running = _is_running(stat.run)

    raw_main = _resolve_main_program(result.get("main_program"), result["macros"])

    # Sticky update: update from a non-subprogram main-program ANYTIME
    # (running or not). This is safe because raw_main is from 0xB9, which
    # rolls over to the new job only when the operator actually loads a
    # different program — never during subprogram excursions.
    #
    # Programs in the machine's `ignored_programs` config (e.g. setup
    # programs like O0001) clear the sticky instead of updating it, so
    # they never display on the dashboard and never drive cycle events.
    if raw_main and not _is_subprogram(raw_main):
        if _is_ignored_program(raw_main, config):
            if st["sticky_program"]:
                logger.info(
                    f"[{config.get('name', machine_id)}] ignored program "
                    f"{raw_main!r} loaded — clearing sticky "
                    f"(was {st['sticky_program']!r})"
                )
            st["sticky_program"] = ""
        else:
            if st["sticky_program"] != raw_main:
                logger.info(
                    f"[{config.get('name', machine_id)}] main program -> {raw_main} "
                    f"(was {st['sticky_program']!r})"
                )
            st["sticky_program"] = raw_main

    sticky = st["sticky_program"]
    data = _build_data_dict(sys_info, result, sticky, config)

    aux = {
        "run":       stat.run,
        "alarm":     stat.alarm,
        "emergency": stat.emergency,
        "raw_main":  raw_main,
        "program":   sticky,
    }
    return {"data": data, "aux": aux}


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — detect_events (cycle only)
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
    sticky = st.get("sticky_program") or ""

    if prev_running is None:
        st["running"] = curr_running
        logger.info(
            f"[{machine_name}] cycle baseline set "
            f"(run={run}, running={curr_running}, sticky={sticky!r})"
        )
        return []

    if prev_running == curr_running:
        return []

    events = []

    # Running -> Not running: cycle ended
    if prev_running and not curr_running:
        st["running"] = curr_running
        if sticky:
            events.append({
                "event_type": "cycle.completed",
                "payload": {
                    "event":        "cycle.completed",
                    "machine_id":   machine_id,
                    "machine_name": machine_name,
                    "pallet":       0,
                    "program":      sticky,
                    "ts":           ts,
                },
            })
            logger.info(
                f"[{machine_name}] cycle.completed program={sticky!r} (run={run})"
            )
            # Don't clear sticky here. The operator may rerun the same
            # program; the next cycle.started will fire with the same
            # name from sticky, and a different program loading would
            # update the sticky via poll() before that happens.
        else:
            logger.info(
                f"[{machine_name}] not-running with no sticky program "
                f"(run={run}) — no event"
            )
        return events

    # Not running -> Running: cycle starting
    # Lenient: fire if we have any sticky (even from a prior job).
    # poll() updates sticky synchronously when raw_main changes, so by
    # the time we get here the sticky should reflect the current job.
    if not prev_running and curr_running:
        st["running"] = curr_running
        if sticky:
            events.append({
                "event_type": "cycle.started",
                "payload": {
                    "event":        "cycle.started",
                    "machine_id":   machine_id,
                    "machine_name": machine_name,
                    "pallet":       0,
                    "program":      sticky,
                    "ts":           ts,
                },
            })
            logger.info(
                f"[{machine_name}] cycle.started program={sticky!r} (run={run})"
            )
        else:
            logger.info(
                f"[{machine_name}] running but no sticky program yet, deferring "
                f"(run={run})"
            )
        return events

    return events


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — force_close_cycles (offline watchdog hook)
# ═════════════════════════════════════════════════════════════════════════════
def force_close_cycles(machine_id: str, machine_name: str, ts: str,
                       config: dict) -> list[dict]:
    """Called by the poller when this machine has been unreachable longer
    than the watchdog threshold."""
    events = []
    st = _state.get(machine_id)
    if st is None:
        return events
    if not st.get("running"):
        return events

    sticky = st.get("sticky_program") or ""
    if not sticky:
        st["running"] = False
        return events

    events.append({
        "event_type": "cycle.completed",
        "payload": {
            "event":        "cycle.completed",
            "machine_id":   machine_id,
            "machine_name": machine_name,
            "pallet":       0,
            "program":      sticky,
            "ts":           ts,
        },
    })
    logger.warning(
        f"[{machine_name}] watchdog cycle.completed program={sticky!r} (offline-triggered)"
    )

    st["running"] = False
    # Keep sticky as-is; the machine is presumably running the same
    # program when it comes back. If a different program is loaded
    # during the outage, poll() will update sticky on first reconnect.
    return events


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — reset_state
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
# Plugin — live_stream
# ═════════════════════════════════════════════════════════════════════════════
async def live_stream(config: dict, ws):
    from fastapi import WebSocketDisconnect

    ip   = config.get("ip", "")
    port = int(config.get("port") or DEFAULT_PORT)
    poll_interval = float(config.get("poll_interval") or 2.0)

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

            st = _get_state(stream_id)
            raw_main = _resolve_main_program(result.get("main_program"), result["macros"])
            if raw_main and not _is_subprogram(raw_main):
                if _is_ignored_program(raw_main, config):
                    st["sticky_program"] = ""
                else:
                    st["sticky_program"] = raw_main

            data = _build_data_dict(sys_info, result, st["sticky_program"], config)

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
