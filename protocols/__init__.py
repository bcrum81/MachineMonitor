"""
protocols/__init__.py — Protocol plugin registry.

On import, scans this package for all .py files (not starting with "_"),
imports each as a plugin module, validates required attributes, and
builds a registry keyed by PROTOCOL_ID.

Each plugin module MUST expose:
    PROTOCOL_ID          : str    — unique key, matches machines.json `protocol`
    DISPLAY_NAME         : str    — label shown in the admin dropdown
    DEFAULT_PORT         : int    — used for UI placeholders and default /ping
    REQUIRES_AUTH        : bool   — whether username/password are relevant
    SUPPORTS_PALLETS     : bool   — whether the pallet_count field applies
    DEFAULT_PALLET_COUNT : int    — default pallet_count when this is selected
    IMPLEMENTED          : bool   — False = scaffold stub, poller skips it
    CONFIG_FIELDS        : list   — additional admin form fields (see below)

    async def ping(config: dict) -> dict
    async def poll(config: dict, tick: int) -> dict
        returns { "data": {<field_path>: {value, type, page}, ...},
                  "aux":  <anything>  # passed to detect_events }
    def detect_events(machine_id, machine_name, ts, data, aux, config) -> list[dict]
        each event: {"event_type": str, "payload": dict}
    def reset_state(machine_id: str | None)

Plugins MAY expose:
    async def live_stream(config: dict, ws)   # for /stream WebSocket

CONFIG_FIELDS format — each field is a dict:
    {
        "name":        "tool_macro",
        "label":       "Tool Number Macro",
        "type":        "number",      # "number" | "text" | "password"
        "default":     4120,          # "" or None = empty
        "placeholder": "e.g. 4120",
        "hint":        "Macro that holds the current tool number.",
    }
Values are stored as top-level keys on the machine dict in machines.json.
"""

import importlib
import logging
import pkgutil
from typing import Any, Optional

logger = logging.getLogger("protocols")

_REQUIRED_ATTRS = (
    "PROTOCOL_ID", "DISPLAY_NAME",
    "ping", "poll", "detect_events", "reset_state",
)

_registry: dict[str, Any] = {}


def _check_required(mod) -> list[str]:
    return [a for a in _REQUIRED_ATTRS if not hasattr(mod, a)]


def _discover():
    """Walk this package; import each non-underscore module; validate."""
    _registry.clear()
    pkg = importlib.import_module("protocols")
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        fq_name = f"protocols.{info.name}"
        try:
            mod = importlib.import_module(fq_name)
            missing = _check_required(mod)
            if missing:
                logger.error(
                    f"Protocol '{info.name}' missing required attributes: "
                    f"{missing} — skipped."
                )
                continue
            pid = getattr(mod, "PROTOCOL_ID")
            if pid in _registry:
                logger.error(
                    f"Protocol '{info.name}' declares duplicate PROTOCOL_ID "
                    f"'{pid}' — skipped (already registered by "
                    f"'{_registry[pid].__name__}')."
                )
                continue
            _registry[pid] = mod
            flag = "stub" if not getattr(mod, "IMPLEMENTED", True) else "ready"
            logger.info(f"Registered protocol '{pid}' — {mod.DISPLAY_NAME} [{flag}]")
        except Exception as e:
            logger.error(f"Failed to load protocol module '{fq_name}': {e}")


_discover()


# ── Public API ────────────────────────────────────────────────────────────────
def list_protocols() -> list[dict]:
    """Return metadata for every registered protocol. Used by /api/protocols."""
    out = []
    for pid, mod in _registry.items():
        out.append({
            "id":                   pid,
            "display_name":         getattr(mod, "DISPLAY_NAME", pid),
            "default_port":         getattr(mod, "DEFAULT_PORT", None),
            "requires_auth":        bool(getattr(mod, "REQUIRES_AUTH", False)),
            "supports_pallets":     bool(getattr(mod, "SUPPORTS_PALLETS", False)),
            "default_pallet_count": int(getattr(mod, "DEFAULT_PALLET_COUNT", 0)),
            "implemented":          bool(getattr(mod, "IMPLEMENTED", True)),
            "config_fields":        list(getattr(mod, "CONFIG_FIELDS", [])),
            "supports_live_stream": hasattr(mod, "live_stream"),
        })
    out.sort(key=lambda p: p["display_name"])
    return out


def get_protocol(protocol_id: str):
    """Return the plugin module for a protocol_id, or None."""
    return _registry.get(protocol_id)


def is_known(protocol_id: str) -> bool:
    return protocol_id in _registry


# ── Dispatchers ───────────────────────────────────────────────────────────────
async def dispatch_ping(config: dict) -> dict:
    pid = config.get("protocol", "")
    mod = _registry.get(pid)
    if not mod:
        return {
            "protocol": pid,
            "ip": config.get("ip", ""),
            "reachable": False,
            "detail": f"Unknown protocol: {pid!r}",
        }
    try:
        return await mod.ping(config)
    except Exception as e:
        return {
            "protocol": pid,
            "ip": config.get("ip", ""),
            "reachable": False,
            "detail": f"Ping raised: {e}",
        }


async def dispatch_poll(config: dict, tick: int) -> dict:
    pid = config.get("protocol", "")
    mod = _registry.get(pid)
    if not mod:
        raise ValueError(f"Unknown protocol: {pid!r}")
    if not getattr(mod, "IMPLEMENTED", True):
        raise NotImplementedError(f"Protocol '{pid}' is a scaffold stub")
    return await mod.poll(config, tick)


def dispatch_events(machine_id: str, machine_name: str, ts: str,
                    data: dict, aux, config: dict) -> list[dict]:
    pid = config.get("protocol", "")
    mod = _registry.get(pid)
    if not mod:
        return []
    try:
        return list(mod.detect_events(machine_id, machine_name, ts, data, aux, config))
    except Exception as e:
        logger.error(f"[{machine_name}] Event detection in '{pid}' failed: {e}")
        return []


def dispatch_reset(protocol_id: Optional[str], machine_id: Optional[str]):
    """
    Clear detection state for a machine on a specific protocol.
    If protocol_id is None, clear on every known protocol (used when a
    machine is removed and we don't know which protocol it used).
    """
    targets = [protocol_id] if protocol_id else list(_registry.keys())
    for pid in targets:
        mod = _registry.get(pid)
        if not mod:
            continue
        try:
            mod.reset_state(machine_id)
        except Exception as e:
            logger.error(f"reset_state for '{pid}' failed: {e}")


async def dispatch_live_stream(config: dict, ws) -> bool:
    """
    Returns True if the protocol handled the stream, False if it refused
    (unknown, unimplemented, or doesn't support streaming). The WebSocket
    is left open so the caller can close it after.
    """
    pid = config.get("protocol", "")
    mod = _registry.get(pid)
    if not mod:
        await ws.send_json({"type": "error", "msg": f"Unknown protocol: {pid!r}"})
        return False
    if not getattr(mod, "IMPLEMENTED", True):
        await ws.send_json({"type": "error", "msg": f"Protocol '{pid}' is not yet implemented."})
        return False
    if not hasattr(mod, "live_stream"):
        await ws.send_json({"type": "error", "msg": f"Protocol '{pid}' does not support live streaming."})
        return False
    await mod.live_stream(config, ws)
    return True
