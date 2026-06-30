import asyncio
import json
import os
import socket
import struct
import functools
import hashlib
import secrets
import csv
import io
from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime, timezone, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware

import protocols as proto
from db import (
    init_db, get_db_stats, get_latest_poll, get_recent_polls,
    get_machine_timeline, get_machine_stats,
    get_recent_events, get_recent_deliveries,
    get_cycle_report,
    get_alarm_catalog,
    get_current_cycle_start,
    get_last_cycle_completed,
)
from poller import start_poller, stop_poller, get_poller_status
import webhooks as wh
import sheets

# ─────────────────────────────────────────────────────────────────────────────
# Config paths
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR      = Path("/opt/cnc-probe")
CONFIG_DIR    = BASE_DIR / "config"
AUTH_FILE     = CONFIG_DIR / "auth.json"
MACHINES_FILE = CONFIG_DIR / "machines.json"
SECRET_FILE   = CONFIG_DIR / "secret_key"
ORDER_FILE    = CONFIG_DIR / "machine_order.json"

def _ensure_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not AUTH_FILE.exists():
        AUTH_FILE.write_text(json.dumps({
            "username": "admin",
            "password_hash": _hash_pw(os.environ.get("CNC_DEFAULT_ADMIN_PASSWORD", "changeme"))
        }, indent=2))
    if not MACHINES_FILE.exists():
        MACHINES_FILE.write_text(json.dumps([], indent=2))
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32))
    if not ORDER_FILE.exists():
        ORDER_FILE.write_text(json.dumps([], indent=2))

def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def _get_secret() -> str:
    return SECRET_FILE.read_text().strip()

def _load_auth() -> dict:
    return json.loads(AUTH_FILE.read_text())

def _save_auth(data: dict):
    AUTH_FILE.write_text(json.dumps(data, indent=2))

def _load_machines() -> list:
    return json.loads(MACHINES_FILE.read_text())

def _save_machines(data: list):
    MACHINES_FILE.write_text(json.dumps(data, indent=2))

def _load_order() -> list:
    try:
        return json.loads(ORDER_FILE.read_text())
    except Exception:
        return []

def _save_order(order: list):
    ORDER_FILE.write_text(json.dumps(order, indent=2))

def _apply_order(machines: list) -> list:
    """Return machines sorted by saved order. New/unordered machines appended at end."""
    order = _load_order()
    order_index = {mid: i for i, mid in enumerate(order)}
    known   = [m for m in machines if m["id"] in order_index]
    unknown = [m for m in machines if m["id"] not in order_index]
    known.sort(key=lambda m: order_index[m["id"]])
    return known + unknown

def _check_login(username: str, password: str) -> bool:
    auth = _load_auth()
    return auth["username"] == username and auth["password_hash"] == _hash_pw(password)

def _is_admin(request: Request) -> bool:
    return request.session.get("admin") is True

_ensure_config()

# ─────────────────────────────────────────────────────────────────────────────
# Helper: pick out the CONFIG_FIELDS values a plugin declares, from a request body
# ─────────────────────────────────────────────────────────────────────────────
def _extract_protocol_fields(protocol_id: str, body: dict) -> dict:
    """Return a dict of {name: value} for every CONFIG_FIELDS entry the plugin
    declares, read from body. Missing fields are skipped."""
    mod = proto.get_protocol(protocol_id)
    if not mod:
        return {}
    out = {}
    for fld in getattr(mod, "CONFIG_FIELDS", []):
        name = fld.get("name")
        if not name:
            continue
        if name in body:
            val = body[name]
            # Normalize blank → None for numeric fields
            if fld.get("type") == "number":
                if val == "" or val is None:
                    out[name] = None
                else:
                    try:
                        out[name] = int(val)
                    except (TypeError, ValueError):
                        try:
                            out[name] = float(val)
                        except (TypeError, ValueError):
                            out[name] = None
            else:
                out[name] = val if val not in ("", None) else None
    return out

# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — start/stop background poller and Sheets worker
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sheets.start_worker()
    await start_poller()
    yield
    stop_poller()
    sheets.stop_worker()

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="CNC Shop Monitor", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=_get_secret())
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─────────────────────────────────────────────────────────────────────────────
# Auth routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_admin(request):
        return RedirectResponse("/admin", status_code=302)
    error = request.query_params.get("error")
    with open("static/login.html") as f:
        html = f.read()
    if error:
        html = html.replace('id="error-msg" style="display:none"', 'id="error-msg"')
    return HTMLResponse(html)

@app.post("/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if _check_login(username, password):
        request.session["admin"] = True
        return RedirectResponse("/admin", status_code=302)
    return RedirectResponse("/login?error=1", status_code=302)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)

# ─────────────────────────────────────────────────────────────────────────────
# Public routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def public_home():
    with open("static/public.html") as f:
        return f.read()

@app.get("/machine/{machine_id}", response_class=HTMLResponse)
async def machine_view(machine_id: str):
    with open("static/machine_detail.html") as f:
        return f.read()

@app.get("/machine/{machine_id}/live", response_class=HTMLResponse)
async def machine_live_view(machine_id: str):
    """Original live WebSocket view - kept for backward compatibility"""
    with open("static/machine_view.html") as f:
        return f.read()

@app.get("/api/machines")
async def api_machines():
    """Returns machines in saved display order. Public endpoint — only
    includes fields needed for the dashboard, no credentials."""
    machines = _apply_order(_load_machines())
    return [{
        "id":            m["id"],
        "name":          m["name"],
        "protocol":      m["protocol"],
        "utc_offset":    m.get("utc_offset", 0),
        "pallet_count":  m.get("pallet_count"),
    } for m in machines]

@app.get("/api/latest/{machine_id}")
async def public_latest_poll(machine_id: str):
    """Public endpoint — returns latest poll data for a machine. No credentials exposed."""
    row = get_latest_poll(machine_id)
    if row is None:
        return JSONResponse({"error": "No data yet"}, status_code=404)
    return row

@app.get("/api/machine/{machine_id}/stats")
async def machine_stats(machine_id: str, hours: int = 24):
    """Return statistics for a machine over the specified time window."""
    stats = get_machine_stats(machine_id, hours)
    return stats

@app.get("/api/machine/{machine_id}/timeline")
async def machine_timeline(machine_id: str, hours: int = 24):
    """Return timeline of events for a machine."""
    timeline = get_machine_timeline(machine_id, hours)
    return timeline

@app.get("/api/machine/{machine_id}/history")
async def machine_history(machine_id: str, hours: int = 24, limit: int = 100):
    """Return recent poll history for a machine."""
    polls = get_recent_polls(machine_id, hours, limit)
    return {"polls": polls, "count": len(polls)}

@app.get("/api/machine/{machine_id}/current-cycle")
async def machine_current_cycle(machine_id: str):
    """
    Public endpoint — returns info about the currently in-progress cycle
    for a machine, used by the dashboard so the cycle-time counter starts
    from the actual cycle start time (not from page load).
 
    Returns:
      { "running": true,  "started_at": "<iso>", "program": "..." }
        when the most recent cycle event is a cycle.started, OR
      { "running": false }
        when the machine has no cycle in progress (or no events yet)
    """
    info = get_current_cycle_start(machine_id)
    if info is None:
        return {"running": False}
    return {
        "running":    True,
        "started_at": info["ts"],
        "program":    info.get("program", ""),
    }


@app.get("/api/machine/{machine_id}/last-completed-cycle")
async def machine_last_completed_cycle(machine_id: str):
    """
    Return info about the most recent cycle.completed event for a machine.
    Used by the public dashboard to populate the 'Last Cycle End' cell on
    cards whose protocol does not surface that field directly in poll data
    (currently: FOCAS).

    Response shapes:
      { "ended_at": "<iso>", "program": "..." }
        when at least one cycle.completed event exists, OR
      { "ended_at": null }
        when the machine has no completed cycles yet
    """
    info = get_last_cycle_completed(machine_id)
    if info is None:
        return {"ended_at": None}
    return {
        "ended_at": info["ts"],
        "program":  info.get("program", ""),
    }
 


# ─────────────────────────────────────────────────────────────────────────────
# Protocols registry — public-ish (admin panel needs it to render the dropdown)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/protocols")
async def api_protocols():
    """Return metadata for every registered protocol plugin. Used by the
    admin panel and tester page to render the protocol dropdown and any
    plugin-specific configuration fields."""
    return proto.list_protocols()

# ─────────────────────────────────────────────────────────────────────────────
# Admin routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/admin/check")
async def admin_check(request: Request):
    """Returns whether the current session is an admin. Used by public dashboard."""
    return {"admin": _is_admin(request)}

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    with open("static/admin.html") as f:
        return f.read()

@app.get("/admin/integrations", response_class=HTMLResponse)
async def admin_integrations(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    with open("static/integrations.html") as f:
        return f.read()

@app.get("/admin/reports", response_class=HTMLResponse)
async def admin_reports(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    with open("static/reports.html") as f:
        return f.read()

@app.get("/tester", response_class=HTMLResponse)
async def tester(request: Request):
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    with open("static/index.html") as f:
        return f.read()

@app.get("/api/admin/machines")
async def admin_machines_full(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return _load_machines()

@app.post("/api/admin/machines")
async def add_machine(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    protocol_id = body.get("protocol", "http_brother")
    if not proto.is_known(protocol_id):
        return JSONResponse(
            {"error": f"Unknown protocol: {protocol_id!r}"},
            status_code=400,
        )

    machines = _load_machines()
    machine = {
        "id":            secrets.token_hex(4),
        "name":          body.get("name", "Unnamed Machine"),
        "ip":            body["ip"],
        "protocol":      protocol_id,
        "port":          body.get("port") or None,
        "username":      body.get("username") or None,
        "password":      body.get("password") or None,
        "poll_interval": float(body.get("poll_interval", 2.0)),
        "utc_offset":    float(body.get("utc_offset", 0)),
        "pallet_count":  int(body.get("pallet_count", 2)),
        "added":         datetime.now().isoformat(),
    }
    # Merge any plugin-specific CONFIG_FIELDS declared by this protocol.
    machine.update(_extract_protocol_fields(protocol_id, body))

    machines.append(machine)
    _save_machines(machines)
    return {"ok": True, "id": machine["id"], "name": machine["name"]}

@app.put("/api/admin/machines/{machine_id}")
async def update_machine(machine_id: str, request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    protocol_id = body.get("protocol")
    if protocol_id is not None and not proto.is_known(protocol_id):
        return JSONResponse(
            {"error": f"Unknown protocol: {protocol_id!r}"},
            status_code=400,
        )

    machines = _load_machines()
    for i, m in enumerate(machines):
        if m["id"] == machine_id:
            effective_protocol = protocol_id or m["protocol"]
            new_plugin_fields = _extract_protocol_fields(effective_protocol, body)

            # If the protocol changed, strip stale plugin fields from the old
            # protocol so they don't clutter machines.json.
            if protocol_id and protocol_id != m.get("protocol"):
                old_mod = proto.get_protocol(m["protocol"])
                if old_mod:
                    for fld in getattr(old_mod, "CONFIG_FIELDS", []):
                        m.pop(fld.get("name"), None)

            m.update({
                "name":          body.get("name", m["name"]),
                "ip":            body.get("ip", m["ip"]),
                "protocol":      effective_protocol,
                "port":          body.get("port") or None,
                "username":      body.get("username") or None,
                "password":      body.get("password") or None,
                "poll_interval": float(body.get("poll_interval", m["poll_interval"])),
                "utc_offset":    float(body.get("utc_offset", m.get("utc_offset", 0))),
                "pallet_count":  int(body.get("pallet_count", m.get("pallet_count", 2))),
            })
            m.update(new_plugin_fields)
            machines[i] = m
            _save_machines(machines)
            return {"ok": True}
    return JSONResponse({"error": "Not found"}, status_code=404)

@app.delete("/api/admin/machines/{machine_id}")
async def delete_machine(machine_id: str, request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    machines = _load_machines()
    machines = [m for m in machines if m["id"] != machine_id]
    _save_machines(machines)
    return {"ok": True}

@app.put("/api/admin/machine-order")
async def save_machine_order(request: Request):
    """Save display order for the dashboard. Body: {"order": ["id1", "id2", ...]}"""
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    order = body.get("order", [])
    if not isinstance(order, list):
        return JSONResponse({"error": "order must be a list"}, status_code=400)
    valid_ids = {m["id"] for m in _load_machines()}
    order = [mid for mid in order if mid in valid_ids]
    _save_order(order)
    return {"ok": True}

@app.post("/api/admin/change-password")
async def change_password(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    current = body.get("current_password", "")
    new_pw  = body.get("new_password", "")
    if len(new_pw) < 6:
        return JSONResponse({"error": "New password must be at least 6 characters"}, status_code=400)
    auth = _load_auth()
    if auth["password_hash"] != _hash_pw(current):
        return JSONResponse({"error": "Current password is incorrect"}, status_code=400)
    auth["password_hash"] = _hash_pw(new_pw)
    _save_auth(auth)
    return {"ok": True}

# ─────────────────────────────────────────────────────────────────────────────
# Admin — database status
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/admin/db-status")
async def db_status(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    stats   = get_db_stats()
    poller  = get_poller_status()
    return {"db": stats, "poller": poller}

@app.get("/api/admin/latest/{machine_id}")
async def latest_poll(machine_id: str, request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    row = get_latest_poll(machine_id)
    if row is None:
        return JSONResponse({"error": "No data yet"}, status_code=404)
    return row

# ─────────────────────────────────────────────────────────────────────────────
# Admin — webhooks (Thread 4.2, extended in Thread 7)
# ─────────────────────────────────────────────────────────────────────────────
_CYCLE_EVENT_TYPES = {"cycle.started", "cycle.completed"}

def _is_valid_event_type(et) -> bool:
    """
    Accept:
      cycle.started, cycle.completed
      alarm.any               — matches any alarm.*
      alarm.<CODE>            — where <CODE> is alnum/./_/- only
    """
    if not isinstance(et, str) or not et:
        return False
    if et in _CYCLE_EVENT_TYPES:
        return True
    if et == "alarm.any":
        return True
    if et.startswith("alarm."):
        code = et[len("alarm."):]
        if not code:
            return False
        return all(c.isalnum() or c in "._-" for c in code)
    return False

def _sanitize_sub_for_client(sub: dict, include_secret: bool = False) -> dict:
    """Strip secret by default. Include it only on creation or rotation."""
    out = {k: v for k, v in sub.items() if k != "secret"}
    if include_secret:
        out["secret"] = sub["secret"]
    return out

@app.get("/api/admin/webhooks")
async def list_webhooks(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    subs = wh.load_subscriptions()
    return [_sanitize_sub_for_client(s) for s in subs]

@app.post("/api/admin/webhooks")
async def create_webhook(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()

    name        = (body.get("name") or "").strip()
    target_url  = (body.get("target_url") or "").strip()
    events      = body.get("events") or []
    machine_ids = body.get("machine_ids") or []
    enabled     = bool(body.get("enabled", True))

    if not target_url.startswith(("http://", "https://")):
        return JSONResponse({"error": "target_url must start with http:// or https://"}, status_code=400)
    if not isinstance(events, list) or not events:
        return JSONResponse({"error": "events must be a non-empty list"}, status_code=400)
    invalid = [e for e in events if not _is_valid_event_type(e)]
    if invalid:
        return JSONResponse({"error": f"Invalid event types: {invalid}"}, status_code=400)
    if not isinstance(machine_ids, list):
        return JSONResponse({"error": "machine_ids must be a list"}, status_code=400)

    sub = wh.add_subscription(name, target_url, events, machine_ids, enabled)
    return _sanitize_sub_for_client(sub, include_secret=True)

@app.put("/api/admin/webhooks/{sub_id}")
async def edit_webhook(sub_id: str, request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    updates = {}

    if "name" in body:
        updates["name"] = (body["name"] or "").strip()
    if "target_url" in body:
        url = (body["target_url"] or "").strip()
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"error": "target_url must start with http:// or https://"}, status_code=400)
        updates["target_url"] = url
    if "events" in body:
        events = body["events"] or []
        invalid = [e for e in events if not _is_valid_event_type(e)]
        if invalid:
            return JSONResponse({"error": f"Invalid event types: {invalid}"}, status_code=400)
        updates["events"] = events
    if "machine_ids" in body:
        if not isinstance(body["machine_ids"], list):
            return JSONResponse({"error": "machine_ids must be a list"}, status_code=400)
        updates["machine_ids"] = body["machine_ids"]
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])

    sub = wh.update_subscription(sub_id, updates)
    if sub is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return _sanitize_sub_for_client(sub)

@app.delete("/api/admin/webhooks/{sub_id}")
async def remove_webhook(sub_id: str, request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ok = wh.delete_subscription(sub_id)
    if not ok:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"ok": True}

@app.post("/api/admin/webhooks/{sub_id}/rotate-secret")
async def rotate_webhook_secret(sub_id: str, request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    sub = wh.rotate_secret(sub_id)
    if sub is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return _sanitize_sub_for_client(sub, include_secret=True)

@app.post("/api/admin/webhooks/{sub_id}/test")
async def test_webhook(sub_id: str, request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    result = await wh.test_fire(sub_id)
    return result

@app.get("/api/admin/events")
async def list_events(request: Request, limit: int = 100,
                      machine_id: Optional[str] = None,
                      event_type: Optional[str] = None):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    limit = max(1, min(int(limit), 500))
    return get_recent_events(limit=limit, machine_id=machine_id, event_type=event_type)

@app.get("/api/admin/deliveries")
async def list_deliveries(request: Request, limit: int = 100,
                          subscription_id: Optional[str] = None):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    limit = max(1, min(int(limit), 500))
    return get_recent_deliveries(limit=limit, subscription_id=subscription_id)

# ─────────────────────────────────────────────────────────────────────────────
# Admin — alarm catalog (Thread 7)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/admin/alarm-catalog")
async def list_alarm_catalog(request: Request):
    """
    Return every alarm code seen across all machines, for the webhook
    subscription dropdown.
    """
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return get_alarm_catalog()

# ─────────────────────────────────────────────────────────────────────────────
# Admin — Google Sheets config (Thread 5.1)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/admin/sheets/config")
async def sheets_get_config(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    cfg = sheets.load_config()
    cfg["queue_depth"] = sheets.queue_depth()
    cfg["credentials_present"] = Path(cfg.get("credentials_path", "")).exists()
    return sheets.sanitize_for_client(cfg)

@app.put("/api/admin/sheets/config")
async def sheets_put_config(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    body = await request.json()
    updates = {}
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])
    if "sheet_id" in body:
        updates["sheet_id"] = (body["sheet_id"] or "").strip()
    if "tab_name" in body:
        updates["tab_name"] = (body["tab_name"] or "Sheet1").strip() or "Sheet1"
    if "credentials_path" in body:
        updates["credentials_path"] = (body["credentials_path"] or "").strip()
    current = sheets.load_config()
    current.update(updates)
    saved = sheets.save_config(current)
    return sheets.sanitize_for_client(saved)

@app.post("/api/admin/sheets/test")
async def sheets_test(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, sheets.test_connection)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# Admin — cycle reports (Thread 5)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_report_params(request: Request) -> dict:
    """Parse and validate query params shared by JSON and CSV cycle-report endpoints."""
    qp = request.query_params
    start = (qp.get("start") or "").strip()
    end   = (qp.get("end") or "").strip()

    if not start or not end:
        now = datetime.now(timezone.utc)
        end_dt = now
        start_dt = now - timedelta(days=7)
        start = start_dt.isoformat()
        end = end_dt.isoformat()
    else:
        start = _normalize_to_iso(start, end_of_day=False)
        end   = _normalize_to_iso(end,   end_of_day=True)

    machine_id = qp.get("machine_id") or None
    if machine_id in ("", "all"):
        machine_id = None

    pallet_raw = qp.get("pallet")
    pallet = None
    if pallet_raw and pallet_raw not in ("", "all"):
        try:
            p = int(pallet_raw)
            if p in (1, 2):
                pallet = p
        except ValueError:
            pallet = None

    program = (qp.get("program") or "").strip() or None

    return {
        "start": start, "end": end,
        "machine_id": machine_id, "pallet": pallet, "program": program,
    }


def _normalize_to_iso(s: str, end_of_day: bool) -> str:
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        dt = datetime.fromisoformat(s)
        dt = dt.replace(tzinfo=timezone.utc)
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
        return dt.isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


@app.get("/api/admin/cycle-report")
async def cycle_report_json(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    p = _parse_report_params(request)
    rows = get_cycle_report(
        start_ts=p["start"], end_ts=p["end"],
        machine_id=p["machine_id"], pallet=p["pallet"], program=p["program"],
    )

    durations = [r["duration_sec"] for r in rows if r["duration_sec"] > 0]
    summary = {
        "count": len(rows),
        "total_duration_sec": round(sum(durations), 1) if durations else 0,
        "avg_duration_sec":   round(sum(durations) / len(durations), 1) if durations else 0,
        "min_duration_sec":   round(min(durations), 1) if durations else 0,
        "max_duration_sec":   round(max(durations), 1) if durations else 0,
    }

    per_machine: dict[str, dict] = {}
    for r in rows:
        mid = r["machine_id"]
        if mid not in per_machine:
            per_machine[mid] = {
                "machine_id":   mid,
                "machine_name": r["machine_name"],
                "count":        0,
                "total_sec":    0.0,
            }
        per_machine[mid]["count"] += 1
        per_machine[mid]["total_sec"] += r["duration_sec"]
    for v in per_machine.values():
        v["avg_sec"]   = round(v["total_sec"] / v["count"], 1) if v["count"] else 0
        v["total_sec"] = round(v["total_sec"], 1)

    return {
        "filters": p,
        "summary": summary,
        "per_machine": sorted(per_machine.values(), key=lambda x: x["machine_name"]),
        "rows": rows,
    }


@app.get("/api/admin/cycle-report.csv")
async def cycle_report_csv(request: Request):
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    p = _parse_report_params(request)
    rows = get_cycle_report(
        start_ts=p["start"], end_ts=p["end"],
        machine_id=p["machine_id"], pallet=p["pallet"], program=p["program"],
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "machine_id", "machine_name", "pallet", "program",
        "cycle_start_utc", "cycle_end_utc",
        "duration_sec", "duration_hms",
    ])
    for r in rows:
        writer.writerow([
            r["machine_id"],
            r["machine_name"],
            r["pallet"],
            r["program"],
            r["cycle_start"],
            r["cycle_end"],
            f"{r['duration_sec']:.1f}",
            _sec_to_hms(r["duration_sec"]),
        ])

    try:
        start_short = p["start"][:10]
        end_short   = p["end"][:10]
    except Exception:
        start_short = "start"
        end_short   = "end"
    fname = f"cycle-report_{start_short}_to_{end_short}.csv"

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _sec_to_hms(seconds: float) -> str:
    try:
        s = float(seconds)
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s - (h * 3600) - (m * 60)
        return f"{h:02d}:{m:02d}:{sec:04.1f}"
    except Exception:
        return "00:00:00.0"

# ─────────────────────────────────────────────────────────────────────────────
# /ping — dispatch to protocol plugin
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/ping")
async def ping_machine(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    if "ip" not in body or "protocol" not in body:
        return JSONResponse({"error": "ip and protocol required"}, status_code=400)
    return await proto.dispatch_ping(body)

# ─────────────────────────────────────────────────────────────────────────────
# WebSocket streams — dispatch to protocol plugin
# ─────────────────────────────────────────────────────────────────────────────
@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    try:
        raw = await ws.receive_text()
        config = json.loads(raw)
        if not isinstance(config, dict) or "ip" not in config or "protocol" not in config:
            await ws.send_json({"type": "error", "msg": "Invalid config — ip and protocol required"})
            return
        await proto.dispatch_live_stream(config, ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "msg": str(e)})
        except Exception:
            pass

@app.websocket("/stream/{machine_id}")
async def stream_by_id(ws: WebSocket, machine_id: str):
    await ws.accept()
    try:
        machines = _load_machines()
        machine = next((m for m in machines if m["id"] == machine_id), None)
        if not machine:
            await ws.send_json({"type": "error", "msg": "Machine not found"})
            return
        await proto.dispatch_live_stream(machine, ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "msg": str(e)})
        except Exception:
            pass

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8765, reload=False)
