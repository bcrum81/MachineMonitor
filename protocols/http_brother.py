"""
protocols/http_brother.py — Brother Speedio HTTP scraping.

Combines what used to be three files into one self-contained plugin:
  - Brother HTTP page scraping (formerly scraper.py)
  - Cycle event detection — pallet operation-end transitions (formerly events.py)
  - Alarm event detection — /alarm_list idx progression (formerly alarms.py)
  - Live WebSocket streaming for /stream endpoints

All Brother-specific HTML parsing, field names, and state machines live
here and nowhere else.
"""

import asyncio
import functools
import logging
import re
from datetime import datetime
from typing import Optional

import requests as req_lib
from bs4 import BeautifulSoup

from db import register_alarm_code, record_alarm_occurrence

logger = logging.getLogger("protocols.http_brother")

# ═════════════════════════════════════════════════════════════════════════════
# Plugin metadata
# ═════════════════════════════════════════════════════════════════════════════
PROTOCOL_ID          = "http_brother"
DISPLAY_NAME         = "HTTP — Brother Speedio (Web Scrape)"
DEFAULT_PORT         = 80
REQUIRES_AUTH        = True
SUPPORTS_PALLETS     = True
DEFAULT_PALLET_COUNT = 2
IMPLEMENTED          = True
CONFIG_FIELDS: list  = []   # no plugin-specific extras

# ═════════════════════════════════════════════════════════════════════════════
# Brother HTTP page map
# ═════════════════════════════════════════════════════════════════════════════
BROTHER_PAGES = {
    "running_log":    "/running_log",
    "work_counter":   "/work_counter",
    "alarm_log":      "/alarm_log",
    "tool":           "/tool",
    "status_log":     "/status_log",
    "mainte_info":    "/mainte_info",
    "measure_result": "/measure_result",
    "alarm_list":     "/alarm_list?page=0&sort=1",
}

FAST_PAGES = ["running_log", "work_counter", "alarm_log"]
SLOW_PAGES = ["tool", "status_log", "mainte_info"]


# ═════════════════════════════════════════════════════════════════════════════
# Low-level HTTP helpers
# ═════════════════════════════════════════════════════════════════════════════
def _sync_fetch(base_url: str, path: str, auth=None):
    try:
        r = req_lib.get(base_url + path, timeout=5, auth=auth)
        if r.status_code == 200:
            return r.content.decode("ISO-8859-1", errors="replace")
    except Exception:
        pass
    return None


async def _async_fetch(base_url: str, path: str, auth=None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(_sync_fetch, base_url, path, auth))


def _url_and_auth(config: dict):
    port = config.get("port") or DEFAULT_PORT
    base_url = f"http://{config['ip']}:{port}"
    auth = (config["username"], config.get("password") or "") if config.get("username") else None
    return base_url, auth


# ═════════════════════════════════════════════════════════════════════════════
# Brother HTML parsers  (preserved verbatim from former scraper.py)
# ═════════════════════════════════════════════════════════════════════════════
def _parse_header(soup):
    result = {}
    try:
        td = soup.find("td", attrs={"colspan": "2"})
        if td:
            for line in td.get_text(separator="\n").splitlines():
                line = line.strip()
                if "Model" in line and "Status" in line:
                    parts = line.split("Status")
                    if len(parts) == 2:
                        result["model"]  = parts[0].replace("Model :", "").strip()
                        result["status"] = parts[1].replace(":", "").strip()
    except Exception:
        pass
    return result


def _parse_clock(soup):
    try:
        td = soup.find("td", class_="titile_time")
        if td:
            return td.get_text().strip()
    except Exception:
        pass
    return None


def _parse_table(soup, page_name):
    data = {}
    operation_end_times = []
    pallet_context = None
    has_pallet_programs = False

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            label_cell = None
            for cell in cells:
                cls = " ".join(cell.get("class", []))
                if "running_menu_1" in cls or "wk_menu" in cls:
                    label_cell = cell
                    break

            if label_cell:
                label = label_cell.get_text(separator=" ").strip()
                if label and label not in ("\xa0", ""):
                    value_cells = []
                    for cell in cells:
                        cls = " ".join(cell.get("class", []))
                        if "running_value_1" in cls or "wk_value" in cls:
                            value_cells.append(cell)

                    if value_cells:
                        value = value_cells[0].get_text(separator=" ").strip()

                        if "Pallet 1 program" in label:
                            pallet_context = "Pallet 1"
                            has_pallet_programs = True
                        elif "Pallet 2 program" in label:
                            pallet_context = "Pallet 2"
                            has_pallet_programs = True
                        elif label == "Program":
                            pallet_context = None

                        cleaned_label = label.strip()
                        if pallet_context and cleaned_label in [
                            "Cycle time", "Cutting time", "Non cutting time",
                            "Cutting time / cycle time", "Operation end date and time",
                        ]:
                            field_name = f"{pallet_context} {cleaned_label.lower()}"
                        else:
                            field_name = cleaned_label

                        if "Operation end date and time" in field_name and page_name == "running_log":
                            operation_end_times.append({
                                "value": value if value and value != "\xa0" else "--",
                                "pallet_context": pallet_context,
                                "field_name": field_name,
                            })

                        data[f"{page_name}/{field_name}"] = {
                            "value": value if value and value != "\xa0" else "--",
                            "type": "str",
                            "page": page_name,
                        }

    if page_name == "running_log" and operation_end_times:
        if len(operation_end_times) == 1:
            data[f"{page_name}/Operation end date and time"] = {
                "value": operation_end_times[0]["value"],
                "type": "str",
                "page": page_name,
            }
        elif len(operation_end_times) == 2 and has_pallet_programs:
            data[f"{page_name}/Pallet 1 operation end date and time"] = {
                "value": operation_end_times[0]["value"],
                "type": "str",
                "page": page_name,
            }
            data[f"{page_name}/Pallet 2 operation end date and time"] = {
                "value": operation_end_times[1]["value"],
                "type": "str",
                "page": page_name,
            }
            data.pop(f"{page_name}/Operation end date and time", None)
        else:
            data[f"{page_name}/Operation end date and time"] = {
                "value": operation_end_times[0]["value"] if operation_end_times else "--",
                "type": "str",
                "page": page_name,
            }

    if not data:
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if 2 <= len(cells) <= 3:
                    label = cells[0].get_text(separator=" ").strip()
                    value = cells[1].get_text(separator=" ").strip()
                    if (label and value
                            and label not in ("\xa0", "")
                            and value not in ("\xa0", "")):
                        data[f"{page_name}/{label}"] = {
                            "value": value,
                            "type": "str",
                            "page": page_name,
                        }
    return data


async def _scrape_pages(base_url, pages, auth=None):
    all_data = {}
    htmls = await asyncio.gather(
        *[_async_fetch(base_url, BROTHER_PAGES[n], auth) for n in pages],
        return_exceptions=True,
    )
    for name, html in zip(pages, htmls):
        if isinstance(html, str) and html:
            soup = BeautifulSoup(html, "html.parser")
            if name == "running_log":
                for k, v in _parse_header(soup).items():
                    all_data[f"machine/{k}"] = {"value": v, "type": "str", "page": "header"}
                clk = _parse_clock(soup)
                if clk:
                    all_data["machine/clock"] = {"value": clk, "type": "str", "page": "header"}
            all_data.update(_parse_table(soup, name))
        else:
            all_data[f"{name}/_error"] = {"value": "fetch failed", "type": "error", "page": name}
    return all_data


# ═════════════════════════════════════════════════════════════════════════════
# /alarm_list parser  (preserved from former scraper.py)
# ═════════════════════════════════════════════════════════════════════════════
_IDX_RE = re.compile(r"idx=(\d+)")


def _parse_alarm_list(soup):
    entries = []
    for row in soup.find_all("tr"):
        classes = row.get("class", []) or []
        level = None
        for cls in classes:
            if cls.startswith("alarm_level_"):
                try:
                    level = int(cls.replace("alarm_level_", ""))
                except ValueError:
                    level = None
                break
        if level is None:
            continue

        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        onclick = row.get("onclick", "") or ""
        m = _IDX_RE.search(onclick)
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except ValueError:
            continue

        code    = cells[0].get_text(separator=" ").strip()
        message = cells[1].get_text(separator=" ").strip()
        lingo   = cells[2].get_text(separator=" ").strip()
        tm      = cells[3].get_text(separator=" ").strip()
        dt      = cells[4].get_text(separator=" ").strip()

        if not code:
            continue

        entries.append({
            "idx":     idx,
            "code":    code,
            "message": message,
            "lingo":   lingo,
            "time":    tm,
            "date":    dt,
            "level":   level,
        })
    return entries


async def _scrape_alarm_list(base_url, auth=None):
    html = await _async_fetch(base_url, BROTHER_PAGES["alarm_list"], auth)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    return _parse_alarm_list(soup)


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — ping
# ═════════════════════════════════════════════════════════════════════════════
async def ping(config: dict) -> dict:
    base_url, auth = _url_and_auth(config)
    result = {
        "protocol": PROTOCOL_ID,
        "ip": config.get("ip", ""),
        "reachable": False,
        "detail": "",
        "pages": {},
    }
    html = await _async_fetch(base_url, "/running_log", auth)
    if html:
        result["reachable"] = True
        result["detail"] = "HTTP 200 — Brother web server responding"
        soup = BeautifulSoup(html, "html.parser")
        result["machine_info"] = _parse_header(soup)

        # Surface the controller's wall-clock so the admin UI can show
        # both clocks side-by-side and suggest a clock_offset relative
        # to the shop PC's local time.
        clk = _parse_clock(soup)
        if clk:
            # Brother typically formats this as 'YYYY/MM/DD HH:MM:SS'.
            # Normalize to 'YYYY-MM-DD HH:MM:SS' for the admin UI.
            normalized = clk.strip().replace("/", "-")
            result["controller_clock"] = normalized
            from datetime import datetime as _dt
            result["server_clock_local"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            sugg = _suggest_offset_from_clock(normalized)
            if sugg is not None:
                result["suggested_clock_offset"] = sugg

        for name, path in BROTHER_PAGES.items():
            pg = await _async_fetch(base_url, path, auth)
            result["pages"][name] = pg is not None
    else:
        result["detail"] = f"Could not fetch {base_url}/running_log — check IP"
    return result


def _suggest_offset_from_clock(controller_wall_clock: str):
    """Compare the controller's wall-clock to the shop PC's local time
    and return the implied drift in hours (rounded to 0.5), or None if
    parsing fails.

    Result is positive if the controller is BEHIND local time. The
    dashboard uses this offset to add to machine-supplied timestamps
    so they display correctly.
    """
    if not controller_wall_clock:
        return None
    from datetime import datetime as _dt
    fmt_candidates = ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")
    ctrl = None
    for fmt in fmt_candidates:
        try:
            ctrl = _dt.strptime(controller_wall_clock, fmt)
            break
        except Exception:
            continue
    if ctrl is None:
        return None
    now_local = _dt.now()
    delta_hours = (now_local - ctrl).total_seconds() / 3600.0
    return round(delta_hours * 2) / 2


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — poll
# ═════════════════════════════════════════════════════════════════════════════
async def poll(config: dict, tick: int) -> dict:
    """
    One poll tick. Returns:
        {
          "data": { <field_path>: {value, type, page}, ... },
          "aux":  { "alarm_entries": [<alarm_row>, ...] or None },
        }
    Raises ConnectionError if the primary page (/running_log) fails.
    """
    base_url, auth = _url_and_auth(config)
    pages = FAST_PAGES + (SLOW_PAGES if tick % 10 == 0 else [])
    data_task  = _scrape_pages(base_url, pages, auth)
    alarm_task = _scrape_alarm_list(base_url, auth)
    data, alarm_entries = await asyncio.gather(data_task, alarm_task)

    if "running_log/_error" in data:
        raise ConnectionError("running_log fetch failed")

    return {
        "data": data,
        "aux":  {"alarm_entries": alarm_entries},
    }


# ═════════════════════════════════════════════════════════════════════════════
# Cycle event detection
# ═════════════════════════════════════════════════════════════════════════════
# Key = (machine_id, pallet_num)
_cycle_state: dict[tuple, dict] = {}

# Values that mean "no real program is loaded" on a Brother controller.
# The SX1 shows "????" when idle with no program selected; "----" and "--"
# appear on some other models. Any of these means the pallet is NOT in
# a real cycle even if the operation-end timestamp happens to be blank.
_INVALID_PROGRAMS = {"", "????", "----", "---", "--", "-"}


def _is_valid_program(program: str) -> bool:
    """True only when the program field holds a real program number."""
    if not program:
        return False
    return program.strip() not in _INVALID_PROGRAMS


def _is_running(pallet_data: dict) -> bool:
    """
    A pallet is only considered 'running' when BOTH:
      - op_end is blank (no completion timestamp), AND
      - a valid program is loaded.
    The program check is what prevents phantom cycle.started events when
    the machine is in Standby with no program loaded (op_end blank, program '????').
    """
    if pallet_data.get("op_end", "") != "":
        return False
    return _is_valid_program(pallet_data.get("program", ""))


def _get_pallet_data(data: dict, pallet: int) -> dict:
    prog_candidates = [
        f"running_log/Pallet {pallet} program",
        f"running_log/Pallet {pallet} program number",
        f"running_log/Pallet {pallet} Program",
    ]
    end_candidates = [
        f"running_log/Pallet {pallet} operation end date and time",
        f"running_log/Pallet {pallet} Operation end date and time",
    ]
    if pallet == 1:
        prog_candidates += ["running_log/Program", "running_log/program"]
        end_candidates += ["running_log/Operation end date and time"]

    def _first(keys):
        for k in keys:
            if k in data:
                v = data[k].get("value") or ""
                return v.strip() if isinstance(v, str) else str(v).strip()
        return ""

    prog   = _first(prog_candidates)
    op_end = _first(end_candidates)

    if prog in ("--", "\xa0"):
        prog = ""
    if op_end in ("-", "--", "\xa0", ""):
        op_end = ""

    return {"program": prog, "op_end": op_end}


def _cycle_payload(machine_id, machine_name, event_type, ts, pallet, program) -> dict:
    return {
        "event":        event_type,
        "machine_id":   machine_id,
        "machine_name": machine_name,
        "pallet":       pallet,
        "program":      program,
        "ts":           ts,
    }


def _detect_cycle_events(machine_id, machine_name, pallet_count, ts, data):
    events = []
    if pallet_count < 1:
        return events

    pallets_to_check = [1] if pallet_count == 1 else [1, 2]

    for pallet in pallets_to_check:
        key = (machine_id, pallet)
        current = _get_pallet_data(data, pallet)
        prev = _cycle_state.get(key)

        if prev is None or not prev.get("baselined"):
            _cycle_state[key] = {
                "op_end":       current["op_end"],
                "program":      current["program"],
                "last_program": current["program"] if _is_valid_program(current["program"]) else "",
                "baselined":    True,
            }
            logger.info(
                f"[{machine_name}] pallet {pallet} baselined "
                f"(op_end={current['op_end']!r} program={current['program']!r} "
                f"running={_is_running(current)})"
            )
            continue

        # Track the most recently seen VALID program so payloads stay populated
        # across momentary '????' blips inside a real cycle.
        if _is_valid_program(current["program"]):
            prev["last_program"] = current["program"]

        prev_running = _is_running(prev)
        curr_running = _is_running(current)

        # cycle.started: we went from not-running to running
        if not prev_running and curr_running:
            program_for_payload = current["program"] if _is_valid_program(current["program"]) \
                                  else prev.get("last_program", "")
            events.append({
                "event_type": "cycle.started",
                "payload": _cycle_payload(machine_id, machine_name, "cycle.started",
                                          ts, pallet, program_for_payload),
            })
            logger.info(
                f"[{machine_name}] cycle.started pallet={pallet} "
                f"program={program_for_payload!r}"
            )

        # cycle.completed: only when we see a REAL op_end timestamp appear.
        #
        # Two valid completion paths:
        #   1) We were running and now see a new timestamp (normal close)
        #   2) We weren't tracked as running, but a new op_end timestamp has
        #      appeared that we've never seen before (catch-up — we missed
        #      the start, perhaps across a restart)
        #
        # We require op_end to be a non-blank timestamp in both cases, so a
        # program blip to '????' mid-cycle does NOT fire a false completion.
        elif current["op_end"] != "" and current["op_end"] != prev.get("op_end", ""):
            program_for_payload = current["program"] if _is_valid_program(current["program"]) \
                                  else prev.get("last_program", "")
            events.append({
                "event_type": "cycle.completed",
                "payload": _cycle_payload(machine_id, machine_name, "cycle.completed",
                                          ts, pallet, program_for_payload),
            })
            logger.info(
                f"[{machine_name}] cycle.completed pallet={pallet} "
                f"program={program_for_payload!r}"
            )

        prev["op_end"] = current["op_end"]
        prev["program"] = current["program"]
        if _is_valid_program(current["program"]):
            prev["last_program"] = current["program"]

    return events


# ═════════════════════════════════════════════════════════════════════════════
# Alarm event detection  (preserved verbatim from former alarms.py)
# ═════════════════════════════════════════════════════════════════════════════
_alarm_state: dict[str, dict] = {}
REBASELINE_DROP_THRESHOLD = 50


def _catalog_register_safe(machine_name, entry, ts):
    try:
        register_alarm_code(entry["code"], entry.get("message", ""), entry.get("level"), ts)
    except Exception as err:
        logger.error(f"[{machine_name}] Catalog register failed for {entry.get('code')}: {err}")


def _catalog_record_safe(machine_name, entry, ts):
    try:
        record_alarm_occurrence(entry["code"], entry.get("message", ""), entry.get("level"), ts)
    except Exception as err:
        logger.error(f"[{machine_name}] Catalog record failed for {entry.get('code')}: {err}")


def _detect_alarm_events(machine_id, machine_name, ts, entries):
    if not entries:
        return []

    max_idx = max(e["idx"] for e in entries)
    state = _alarm_state.get(machine_id)

    if state is None or not state.get("baselined"):
        _alarm_state[machine_id] = {"last_idx": max_idx, "baselined": True}
        logger.info(
            f"[{machine_name}] Alarm baseline set: last_idx={max_idx} "
            f"({len(entries)} entries in list)"
        )
        for e in entries:
            _catalog_register_safe(machine_name, e, ts)
        return []

    last_idx = state["last_idx"]

    if max_idx < last_idx - REBASELINE_DROP_THRESHOLD:
        logger.warning(
            f"[{machine_name}] Alarm idx dropped {last_idx} -> {max_idx}; "
            f"assuming counter reset, rebaselining."
        )
        _alarm_state[machine_id] = {"last_idx": max_idx, "baselined": True}
        for e in entries:
            _catalog_register_safe(machine_name, e, ts)
        return []

    new_entries = sorted(
        [e for e in entries if e["idx"] > last_idx],
        key=lambda x: x["idx"],
    )

    events = []
    for e in new_entries:
        event_type = f"alarm.{e['code']}"
        payload = {
            "event":        event_type,
            "machine_id":   machine_id,
            "machine_name": machine_name,
            "code":         e["code"],
            "message":      e.get("message", ""),
            "level":        e.get("level"),
            "lingo":        e.get("lingo", ""),
            "machine_time": e.get("time", ""),
            "machine_date": e.get("date", ""),
            "ts":           ts,
        }
        events.append({"event_type": event_type, "payload": payload})
        logger.info(f"[{machine_name}] {event_type} — {e.get('message', '')!r}")
        _catalog_record_safe(machine_name, e, ts)

    if new_entries:
        _alarm_state[machine_id]["last_idx"] = max(e["idx"] for e in new_entries)

    return events


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — detect_events (unified: cycle + alarm)
# ═════════════════════════════════════════════════════════════════════════════
def detect_events(machine_id: str, machine_name: str, ts: str,
                  data: dict, aux, config: dict) -> list[dict]:
    events = []
    pallet_count = int(config.get("pallet_count", DEFAULT_PALLET_COUNT))
    events.extend(_detect_cycle_events(machine_id, machine_name, pallet_count, ts, data))

    alarm_entries = (aux or {}).get("alarm_entries") if isinstance(aux, dict) else None
    if alarm_entries is not None:
        events.extend(_detect_alarm_events(machine_id, machine_name, ts, alarm_entries))

    return events


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — force_close_cycles (offline watchdog hook)
# ═════════════════════════════════════════════════════════════════════════════
def force_close_cycles(machine_id: str, machine_name: str, ts: str,
                       config: dict) -> list[dict]:
    """
    Called by the poller when this machine has been unreachable longer
    than the watchdog threshold. For each pallet we currently believe is
    running (op_end blank + valid program), emit a cycle.completed event
    using the supplied ts. Then mark the pallet as not-running so we
    don't fire again on the same outage.

    Returns the list of cycle.completed events to dispatch (may be empty).
    """
    events = []
    pallet_count = int(config.get("pallet_count", DEFAULT_PALLET_COUNT))
    if pallet_count < 1:
        return events

    pallets_to_check = [1] if pallet_count == 1 else [1, 2]

    for pallet in pallets_to_check:
        key = (machine_id, pallet)
        prev = _cycle_state.get(key)
        if prev is None or not prev.get("baselined"):
            continue
        # Was this pallet considered running (no op_end + valid program)?
        if not _is_running(prev):
            continue

        program_for_payload = prev.get("last_program", "") or prev.get("program", "")
        events.append({
            "event_type": "cycle.completed",
            "payload": _cycle_payload(
                machine_id, machine_name, "cycle.completed",
                ts, pallet, program_for_payload,
            ),
        })
        logger.warning(
            f"[{machine_name}] watchdog cycle.completed pallet={pallet} "
            f"program={program_for_payload!r} (offline-triggered)"
        )

        # Mark this pallet as no-longer-running so we don't fire again.
        # We do NOT clear baselined — when the machine comes back, the
        # next observed real op_end change will look like a fresh cycle.
        # But we set program to empty so _is_running() returns False
        # until the next poll re-establishes state.
        prev["program"] = ""
        # Leave op_end alone so a new timestamp triggers cycle.completed
        # detection cleanly when the machine recovers.

    return events


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — reset_state
# ═════════════════════════════════════════════════════════════════════════════
def reset_state(machine_id: Optional[str] = None):
    if machine_id is None:
        _cycle_state.clear()
        _alarm_state.clear()
        return
    for k in list(_cycle_state.keys()):
        if k[0] == machine_id:
            _cycle_state.pop(k, None)
    _alarm_state.pop(machine_id, None)


# ═════════════════════════════════════════════════════════════════════════════
# Plugin — live_stream  (formerly brother_stream in scraper.py)
# ═════════════════════════════════════════════════════════════════════════════
async def live_stream(config: dict, ws):
    from fastapi import WebSocketDisconnect  # local import to avoid circular dep
    base_url, auth = _url_and_auth(config)
    poll_interval = float(config.get("poll_interval", 2.0))

    await ws.send_json({"type": "status", "msg": f"Connecting to {base_url} ..."})
    html = await _async_fetch(base_url, "/running_log", auth)
    if html is None:
        await ws.send_json({"type": "error", "msg": f"Could not reach {base_url}"})
        return
    await ws.send_json({"type": "status", "msg": "Connected. Scraping pages..."})
    data = await _scrape_pages(base_url, FAST_PAGES + SLOW_PAGES, auth)
    await ws.send_json({
        "type": "nodes",
        "data": [
            {"path": k, "node_id": k, "value": v["value"], "type": v["type"]}
            for k, v in data.items()
        ],
    })
    await ws.send_json({
        "type": "status",
        "msg": f"Streaming {len(data)} points every {poll_interval}s...",
    })
    tick = 0
    while True:
        await asyncio.sleep(poll_interval)
        tick += 1
        pages = FAST_PAGES + (SLOW_PAGES if tick % 10 == 0 else [])
        fresh = await _scrape_pages(base_url, pages, auth)
        await ws.send_json({
            "type": "poll",
            "ts":   datetime.now().isoformat(),
            "data": fresh,
        })
