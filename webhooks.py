"""
webhooks.py — Webhook subscription storage and delivery.

Subscriptions are stored in /opt/cnc-probe/config/webhooks.json as a list of:
{
  "id":          "<hex token>",
  "name":        "Zapier - cycle started",
  "target_url":  "https://hooks.zapier.com/hooks/catch/...",
  "events":      ["cycle.started", "cycle.completed"],
  "machine_ids": [],       # empty list = all machines; else filter
  "secret":      "<hex>",  # used for HMAC signing
  "enabled":     true,
  "created":     "2026-04-16T..."
}

Delivery:
  * Each matching event spawns an asyncio task that POSTs JSON to target_url.
  * Retries on network error or non-2xx response:
      attempt 1 → 2s wait → attempt 2 → 8s wait → attempt 3 → give up.
  * Every attempt (success or failure) is logged via db.write_delivery().
  * HMAC signature sent as header: X-CNC-Signature: sha256=<hex>
      computed over the raw JSON body using the subscription's secret.
  * Also sends: X-CNC-Event, X-CNC-Delivery-Id, X-CNC-Subscription-Id.

In addition: every dispatched event is also queued to the Google Sheets
background writer (sheets.enqueue_event). Sheets and webhooks run in
parallel — one does not block or affect the other.
"""

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

import requests as req_lib

from db import write_event, write_delivery
import sheets  # Google Sheets auto-logging

logger = logging.getLogger("webhooks")

WEBHOOKS_FILE = Path("/opt/cnc-probe/config/webhooks.json")

# Retry schedule in seconds — three attempts total, ~10 seconds of backoff
RETRY_DELAYS = [2, 8]
REQUEST_TIMEOUT = 10  # seconds per HTTP call


# ── Subscription persistence ─────────────────────────────────────────────────
def _ensure_file():
    WEBHOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not WEBHOOKS_FILE.exists():
        WEBHOOKS_FILE.write_text(json.dumps([], indent=2))


def load_subscriptions() -> list[dict]:
    _ensure_file()
    try:
        return json.loads(WEBHOOKS_FILE.read_text())
    except Exception as e:
        logger.error(f"Failed to load webhooks.json: {e}")
        return []


def save_subscriptions(subs: list[dict]):
    _ensure_file()
    WEBHOOKS_FILE.write_text(json.dumps(subs, indent=2))


def add_subscription(name: str, target_url: str, events: list[str],
                     machine_ids: list[str], enabled: bool = True) -> dict:
    subs = load_subscriptions()
    sub = {
        "id":          secrets.token_hex(4),
        "name":        name or "Unnamed",
        "target_url":  target_url,
        "events":      events,
        "machine_ids": machine_ids or [],
        "secret":      secrets.token_hex(16),
        "enabled":     bool(enabled),
        "created":     datetime.now(timezone.utc).isoformat(),
    }
    subs.append(sub)
    save_subscriptions(subs)
    return sub


def update_subscription(sub_id: str, updates: dict) -> dict | None:
    subs = load_subscriptions()
    for i, s in enumerate(subs):
        if s["id"] == sub_id:
            # Only allow safe fields to be updated — never overwrite id/secret/created
            for k in ("name", "target_url", "events", "machine_ids", "enabled"):
                if k in updates:
                    subs[i][k] = updates[k]
            save_subscriptions(subs)
            return subs[i]
    return None


def delete_subscription(sub_id: str) -> bool:
    subs = load_subscriptions()
    new_subs = [s for s in subs if s["id"] != sub_id]
    if len(new_subs) == len(subs):
        return False
    save_subscriptions(new_subs)
    return True


def rotate_secret(sub_id: str) -> dict | None:
    subs = load_subscriptions()
    for i, s in enumerate(subs):
        if s["id"] == sub_id:
            subs[i]["secret"] = secrets.token_hex(16)
            save_subscriptions(subs)
            return subs[i]
    return None


# ── Matching logic ───────────────────────────────────────────────────────────
def _matches(sub: dict, event_type: str, machine_id: str) -> bool:
    if not sub.get("enabled", True):
        return False
    subscribed = sub.get("events", []) or []
    # Direct match, or alarm.any wildcard that matches any alarm.* event.
    if event_type in subscribed:
        pass
    elif event_type.startswith("alarm.") and "alarm.any" in subscribed:
        pass
    else:
        return False
    machine_filter = sub.get("machine_ids") or []
    if machine_filter and machine_id not in machine_filter:
        return False
    return True


# ── HTTP delivery ────────────────────────────────────────────────────────────
def _sign(secret: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _sync_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    """Synchronous POST. Returns (status_code, response_text_or_error)."""
    try:
        r = req_lib.post(url, data=body, headers=headers, timeout=REQUEST_TIMEOUT)
        return r.status_code, (r.text or "")[:2000]
    except req_lib.exceptions.Timeout:
        return 0, "timeout"
    except req_lib.exceptions.ConnectionError as e:
        return 0, f"connection error: {e}"
    except Exception as e:
        return 0, f"error: {e}"


async def _deliver_one(event_id: int, sub: dict, event_type: str, payload: dict):
    """Run delivery attempts for a single subscription with retries."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = _sign(sub["secret"], body)
    headers = {
        "Content-Type":          "application/json",
        "User-Agent":            "CNC-Shop-Monitor/1.0",
        "X-CNC-Event":           event_type,
        "X-CNC-Subscription-Id": sub["id"],
        "X-CNC-Signature":       signature,
    }

    loop = asyncio.get_event_loop()
    max_attempts = len(RETRY_DELAYS) + 1

    for attempt in range(1, max_attempts + 1):
        ts = datetime.now(timezone.utc).isoformat()
        status, response_text = await loop.run_in_executor(
            None,
            functools.partial(_sync_post, sub["target_url"], body, headers),
        )
        success = 200 <= status < 300
        write_delivery(event_id, sub["id"], sub["target_url"],
                       attempt, ts, status, success, response_text)

        if success:
            logger.info(f"Webhook delivered → {sub['name']} (event_id={event_id} attempt={attempt})")
            return

        if attempt <= len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt - 1]
            logger.warning(
                f"Webhook delivery failed → {sub['name']} "
                f"(event_id={event_id} attempt={attempt} status={status}) "
                f"retrying in {delay}s"
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        else:
            logger.error(
                f"Webhook delivery gave up → {sub['name']} "
                f"(event_id={event_id} after {attempt} attempts)"
            )


async def dispatch_event(machine_id: str, machine_name: str,
                         event_type: str, ts: str, payload: dict):
    """
    Persist an event row and:
      1) queue the event for Google Sheets logging (always-on if enabled)
      2) spawn webhook delivery tasks for every matching subscription

    Called from poller.py after event detection returns. The two
    destinations run in parallel — failures in one never affect the other.
    """
    # Persist first so we always have a record, even if nothing subscribes.
    event_id = write_event(machine_id, machine_name, event_type, ts, payload)

    # ── Google Sheets — fire-and-forget, never raises ──
    try:
        sheets.enqueue_event(event_type, payload)
    except Exception as e:
        logger.error(f"Sheets enqueue error: {e}")

    # ── Webhooks ──
    subs = load_subscriptions()
    matching = [s for s in subs if _matches(s, event_type, machine_id)]
    if not matching:
        return

    for sub in matching:
        # Fire-and-forget — let each delivery run its own retry loop concurrently.
        asyncio.create_task(
            _deliver_one(event_id, sub, event_type, payload),
            name=f"webhook-{sub['id']}-{event_id}"
        )


async def test_fire(sub_id: str) -> dict:
    """
    Admin-triggered test. Sends a synthetic event to this subscription only,
    regardless of its enabled/filter settings. Used by the "Test" button.
    Returns {"ok": bool, "status": int, "response": str}.
    """
    subs = load_subscriptions()
    sub = next((s for s in subs if s["id"] == sub_id), None)
    if sub is None:
        return {"ok": False, "status": 0, "response": "Subscription not found"}

    ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "event":        "test.ping",
        "machine_id":   "test-machine",
        "machine_name": "Test Machine",
        "pallet":       1,
        "program":      "0000",
        "ts":           ts,
        "note":         "This is a test fire from the CNC Shop Monitor admin panel.",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type":          "application/json",
        "User-Agent":            "CNC-Shop-Monitor/1.0",
        "X-CNC-Event":           "test.ping",
        "X-CNC-Subscription-Id": sub["id"],
        "X-CNC-Signature":       _sign(sub["secret"], body),
    }
    loop = asyncio.get_event_loop()
    status, response_text = await loop.run_in_executor(
        None,
        functools.partial(_sync_post, sub["target_url"], body, headers),
    )
    # Record the test fire as event_id=0 so it shows in the delivery log
    # with a distinct marker.
    write_delivery(0, sub["id"], sub["target_url"], 1, ts, status,
                   200 <= status < 300, response_text)
    return {"ok": 200 <= status < 300, "status": status, "response": response_text[:500]}
