"""
protocols/opcua_brother.py — OPC UA scaffold for Brother D00.

Scaffold stub. Appears in the admin protocol dropdown so a machine
row can be created, but ping() reports "not implemented" and the
poller refuses to start a task for this protocol until IMPLEMENTED
is flipped to True.

When ready to implement:
  1. pip install asyncua (already in requirements)
  2. Replace ping/poll/live_stream bodies with real OPC UA calls
  3. Set IMPLEMENTED = True
  4. Populate CONFIG_FIELDS with anything the user needs to configure
     (e.g. namespace index, node IDs, etc.)
"""

import logging
from typing import Optional

logger = logging.getLogger("protocols.opcua_brother")

# ── Plugin metadata ───────────────────────────────────────────────────────────
PROTOCOL_ID          = "opcua_brother"
DISPLAY_NAME         = "OPC UA — Brother D00 (not yet implemented)"
DEFAULT_PORT         = 4840
REQUIRES_AUTH        = True
SUPPORTS_PALLETS     = True
DEFAULT_PALLET_COUNT = 2
IMPLEMENTED          = False
CONFIG_FIELDS: list  = []


# ── Plugin API ────────────────────────────────────────────────────────────────
async def ping(config: dict) -> dict:
    """TCP port check only — real OPC UA handshake not implemented yet."""
    import socket
    ip = config.get("ip", "")
    port = config.get("port") or DEFAULT_PORT
    result = {
        "protocol":  PROTOCOL_ID,
        "ip":        ip,
        "reachable": False,
        "detail":    "",
    }
    if not ip:
        result["detail"] = "IP address required."
        return result
    try:
        s = socket.create_connection((ip, port), timeout=3)
        s.close()
        result["reachable"] = True
        result["detail"] = (
            f"TCP port {port} is open. Note: OPC UA handshake is not yet "
            f"implemented — this protocol is a scaffold stub and will not "
            f"produce polling data in this build."
        )
    except Exception as e:
        result["detail"] = f"Could not reach {ip}:{port} — {e}"
    return result


async def poll(config: dict, tick: int) -> dict:
    raise NotImplementedError("OPC UA polling not yet implemented")


def detect_events(machine_id, machine_name, ts, data, aux, config) -> list[dict]:
    return []


def reset_state(machine_id: Optional[str] = None):
    pass
