# CNC Probe

Self-hosted CNC shop monitor for Ubuntu. Polls Brother Speedio (HTTP) and Fanuc-controlled (FOCAS) machines, displays a live shop-floor dashboard, detects cycle and alarm events, fires webhooks, auto-logs cycles to Google Sheets, and generates cycle-time reports with CSV export.

> **Note:** the GitHub repo is named `MachineMonitor`, but the application is hardcoded to install and run from **`/opt/cnc-probe`** as the **`cnc-probe`** systemd service. Use those paths/names throughout — they are not configurable without editing the source.

## Requirements

- Ubuntu **22.04 LTS** or newer (24.04 LTS is the primary tested target)
- Root / sudo access
- Internet connection during install (apt + pip)
- Port **8765/tcp** open on the shop network
- Port **5353/udp** open for mDNS (`cnc-probe.local`)

## Install

```bash
sudo apt update
sudo apt install -y git
sudo git clone https://github.com/bcrum81/MachineMonitor.git /opt/cnc-probe
cd /opt/cnc-probe
sudo bash install.sh
```

The installer will:

1. Install required system packages (`python3-venv`, `avahi-daemon`, `python3-dbus`, `ufw`, …)
2. Create a Python virtual environment at `/opt/cnc-probe/venv/`
3. Install pinned Python requirements
4. Create runtime directories (`config/`, `config/google/`, `data/`)
5. Generate a random session `secret_key`
6. Generate `config/auth.json` with default admin credentials (`admin / admin`)
7. Install systemd units (`cnc-probe.service`, `avahi-alias@.service`)
8. Install `/usr/local/bin/avahi-alias`
9. Open `8765/tcp` and `5353/udp` in `ufw` (if active)
10. Enable and start both services

### After install

- Dashboard: <http://cnc-probe.local:8765/>
- Admin panel: <http://cnc-probe.local:8765/admin>
- Integrations: <http://cnc-probe.local:8765/admin/integrations>
- Reports: <http://cnc-probe.local:8765/admin/reports>
- Machine tester: <http://cnc-probe.local:8765/tester>
- Default login: `admin / admin` — **change it in the admin panel immediately after first login**

If `cnc-probe.local` does not resolve from a client (some corporate networks filter mDNS), use the server's IP address instead. The installer prints it at the end.

## Update

```bash
cd /opt/cnc-probe
sudo bash update.sh
```

Pulls latest from GitHub, refreshes Python dependencies, reinstalls systemd units if they changed, and restarts the service. `config/` and `data/` are never touched.

## Uninstall

```bash
cd /opt/cnc-probe
sudo bash uninstall.sh
```

Stops and removes the service, the mDNS alias, and the helper script. Prompts before deleting `/opt/cnc-probe/` itself — which still contains `config/` (credentials, machine configs, webhooks, Sheets config) and `data/` (SQLite history).

## Repo Contents vs. Runtime-Generated Files

Everything tracked in git is static — the same on every install. Everything under `config/` and `data/` is specific to the machine and is created fresh on each install.

### Tracked in git (ships with the repo)

| Path | Purpose |
|---|---|
| `app.py` | FastAPI app entry point (serves on `0.0.0.0:8765`) |
| `poller.py` | Background polling manager |
| `db.py` | SQLite schema + query helpers |
| `webhooks.py` | Webhook subscriptions + HMAC dispatch |
| `sheets.py` | Google Sheets background writer |
| `cleanup_events.py` | Maintenance CLI to audit/prune the `events` table |
| `protocols/` | Protocol plugins (`http_brother`, `focas_fanuc`, `opcua_brother`) |
| `static/` | HTML/CSS/JS for all pages |
| `systemd/cnc-probe.service` | Systemd unit — copied to `/etc/systemd/system/` by install |
| `systemd/avahi-alias@.service` | Systemd template unit for mDNS alias |
| `bin/avahi-alias` | Python CNAME publisher — copied to `/usr/local/bin/` by install |
| `install.sh` / `update.sh` / `uninstall.sh` | Lifecycle scripts |
| `requirements.txt` | Pinned Python dependencies |
| `.gitignore` | Keeps runtime files out of the repo |
| `README.md` | This file |

### Runtime-generated (gitignored, never committed)

| Path | Purpose | Created by |
|---|---|---|
| `venv/` | Python virtual environment | `install.sh` |
| `config/auth.json` | Admin username + SHA-256 password hash | `install.sh` (first run) |
| `config/secret_key` | Random session signing key | `install.sh` (first run) |
| `config/machines.json` | Per-machine configs (IP, protocol, poll interval, macros, …) | App, when first machine is added |
| `config/machine_order.json` | Display order for the public dashboard | App, when order is first saved |
| `config/webhooks.json` | Webhook subscriptions | App, when first webhook is created |
| `config/sheets.json` | Google Sheets config | App, when first saved |
| `config/google/credentials.json` | Google service-account key | **Manual install** (see Integrations tab) |
| `data/cnc_data.db` | SQLite history — polls, errors, events, deliveries, alarm catalog | App, on first poll |

> The application reads and writes these at the hardcoded path `/opt/cnc-probe/...`. The first-run bootstrap password can be overridden with the `CNC_DEFAULT_ADMIN_PASSWORD` environment variable; if unset it defaults to `admin`, matching the installer.

## Default Credentials

The installer seeds `config/auth.json` with:

- Username: `admin`
- Password: `admin`

**Change this immediately after first login** via the admin panel's Change Password form. Re-running the installer does not overwrite an existing `auth.json`.

## Services Installed

| Service | Purpose |
|---|---|
| `cnc-probe.service` | Main FastAPI app, runs as root on port 8765 |
| `avahi-alias@cnc-probe.local.service` | Publishes the mDNS CNAME so the host resolves as `cnc-probe.local` in addition to its real hostname |

Common commands:

```bash
sudo systemctl status cnc-probe
sudo systemctl restart cnc-probe
sudo journalctl -u cnc-probe -f
sudo systemctl status avahi-alias@cnc-probe.local
```

## Adding Machines

1. Go to <http://cnc-probe.local:8765/admin>
2. Log in (`admin / admin`)
3. Click **Add Machine**
4. Choose a protocol:
   - `http_brother` — Brother Speedio (HTTP scrape, no auth required by default)
   - `focas_fanuc` — Fanuc 30i/31i via FOCAS port 8193 (Matsuura, Mazak, etc.)
   - `opcua_brother` — scaffold stub only, not yet implemented
5. Fill in IP, poll interval, and any protocol-specific fields (for FOCAS, the tool / part count / program / pallet macro numbers)
6. Save

## Database Maintenance

Cycle/alarm events accumulate in the `events` table. `cleanup_events.py` audits and prunes them (dry-run by default):

```bash
# Preview what would change
sudo /opt/cnc-probe/venv/bin/python3 /opt/cnc-probe/cleanup_events.py

# Apply the changes
sudo /opt/cnc-probe/venv/bin/python3 /opt/cnc-probe/cleanup_events.py --apply
```

> The high-volume `polls` and `poll_errors` tables are **not** pruned by this script and grow unbounded — keep an eye on the size of `data/cnc_data.db` on long-running installs.

## Google Sheets Integration (Optional)

One-time Google Cloud setup before enabling in the admin panel:

1. Create a Google Cloud project at <https://console.cloud.google.com/>
2. Enable the Google Sheets API
3. Create a Service Account (IAM & Admin → Service Accounts)
4. Generate a JSON key, download it
5. Create a Google Sheet with row 1 headers: `Date`, `Machine`, `Pallet`, `Program`, `Operation`, `Total Time (seconds)`
6. Share the sheet with the service account's email (Editor permission)
7. Install the key on the shop PC:
   ```bash
   sudo mv ~/Downloads/your-key.json /opt/cnc-probe/config/google/credentials.json
   sudo chmod 600 /opt/cnc-probe/config/google/credentials.json
   ```
8. In the admin Integrations → Google Sheets tab: enter the Sheet ID (from the URL between `/d/` and `/edit`), the tab name, and the credentials path. Click **Test Connection**, then enable the toggle and Save.

## Troubleshooting

**Service won't start**
```bash
sudo journalctl -u cnc-probe -n 50 --no-pager
```

**mDNS alias not resolving**
```bash
sudo systemctl status avahi-alias@cnc-probe.local
avahi-resolve -n cnc-probe.local
```
If corporate Wi-Fi or a managed switch blocks mDNS, fall back to the server's IP address. mDNS is additive — the app is always reachable on `http://<ip>:8765`.

**Login fails with `admin / admin`**
`auth.json` may have been created by an older version or manually edited. To reset to the default:
```bash
sudo rm /opt/cnc-probe/config/auth.json
sudo bash /opt/cnc-probe/install.sh
```
The installer only writes a new `auth.json` when one does not already exist.

**Python dependency conflict during update**
```bash
sudo rm -rf /opt/cnc-probe/venv
sudo bash /opt/cnc-probe/install.sh
```
This rebuilds the venv from scratch against the current `requirements.txt`. `config/` and `data/` are preserved.

## Supported Protocols

| Protocol | Status | Machines |
|---|---|---|
| `http_brother` | Implemented | Brother Speedio SX1, R450X2, R650X1 (HTTP scrape of `/running_log`, `/work_counter`, `/alarm_log`, `/alarm_list`, `/tool`, `/status_log`, `/mainte_info`, `/measure_result`) |
| `focas_fanuc` | Implemented | Fanuc 30i/31i controllers via FOCAS port 8193 (Matsuura MX420 confirmed) — cycle events via `statinfo.run`, admin-configurable macro reads for tool / part count / program / pallet |
| `opcua_brother` | Scaffold stub | OPC UA reserved for a future release |

## Project Status

The monitor core (polling, dashboard, reports, webhooks, Sheets logging, alarm detection, FOCAS plugin) is feature-complete. Remaining planned work:

- Raspberry Pi shippable provisioning
- Offline license system
- GitHub Releases auto-update mechanism
- Pyarmor obfuscation in the build pipeline

## License

Proprietary — all rights reserved. Contact the project owner for licensing.
