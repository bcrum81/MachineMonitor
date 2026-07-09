#!/usr/bin/env bash
#
# CNC Probe installer
# -------------------
# One-command install on a fresh Ubuntu 22.04+ system.
# Safe to re-run — preserves existing config/ and data/ if present.
#
# Usage:
#   sudo apt install -y git
#   sudo git clone https://github.com/bcrum81/MachineMonitor.git /opt/cnc-probe
#   cd /opt/cnc-probe
#   sudo bash install.sh
#

set -euo pipefail

# ============ configuration ============
INSTALL_DIR="/opt/cnc-probe"
SERVICE_NAME="cnc-probe"
APP_PORT=8765
MDNS_ALIAS="cnc-probe.local"
DEFAULT_ADMIN_USER="admin"
DEFAULT_ADMIN_PASS="admin"

# ============ helpers ============
info()  { printf "\033[1;36m[INSTALL]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[INSTALL]\033[0m WARN: %s\n" "$*" >&2; }
die()   { printf "\033[1;31m[INSTALL]\033[0m ERROR: %s\n" "$*" >&2; exit 1; }

# ============ preflight ============
[ "$(id -u)" -eq 0 ] || die "Must be run as root. Try: sudo bash install.sh"

if [ "$(pwd)" != "$INSTALL_DIR" ]; then
    die "This installer expects the repo to be cloned to $INSTALL_DIR.
       Please run:
           sudo git clone https://github.com/bcrum81/MachineMonitor.git $INSTALL_DIR
           cd $INSTALL_DIR
           sudo bash install.sh"
fi

[ -f "$INSTALL_DIR/app.py" ] || die "app.py not found in $INSTALL_DIR — repo may be incomplete"
[ -f "$INSTALL_DIR/requirements.txt" ] || die "requirements.txt not found in $INSTALL_DIR"

# Ubuntu version check
if ! command -v lsb_release >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq lsb-release
fi
UBUNTU_VERSION="$(lsb_release -rs 2>/dev/null || echo 0)"
UBUNTU_MAJOR="$(echo "$UBUNTU_VERSION" | cut -d. -f1)"
if [ "${UBUNTU_MAJOR:-0}" -lt 22 ]; then
    die "Ubuntu 22.04 or newer is required (detected $UBUNTU_VERSION)"
fi
info "Ubuntu $UBUNTU_VERSION detected"

# ============ apt packages ============
info "Installing system packages (python3-venv, avahi, python3-dbus, ufw, curl)..."
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    avahi-daemon \
    avahi-utils \
    python3-dbus \
    python3-avahi \
    ufw \
    curl \
    ca-certificates

# ============ Python venv + deps ============
if [ ! -d "$INSTALL_DIR/venv" ]; then
    info "Creating Python virtual environment at $INSTALL_DIR/venv..."
    python3 -m venv "$INSTALL_DIR/venv"
else
    info "Python venv already exists — reusing"
fi

info "Upgrading pip inside venv..."
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet

info "Installing Python requirements (this takes a few minutes)..."
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

# ============ runtime directories ============
info "Creating config/ and data/ directories..."
mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/config/google" "$INSTALL_DIR/data"
chmod 700 "$INSTALL_DIR/config/google"

# ============ session secret_key ============
if [ ! -f "$INSTALL_DIR/config/secret_key" ]; then
    info "Generating random session secret_key..."
    head -c 48 /dev/urandom | base64 > "$INSTALL_DIR/config/secret_key"
    chmod 600 "$INSTALL_DIR/config/secret_key"
else
    info "secret_key already exists — preserving"
fi

# ============ auth.json (default admin / admin) ============
if [ ! -f "$INSTALL_DIR/config/auth.json" ]; then
    info "Creating default admin credentials ($DEFAULT_ADMIN_USER / $DEFAULT_ADMIN_PASS)..."
    PWHASH="$(python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$DEFAULT_ADMIN_PASS")"
    printf '{"username": "%s", "password_hash": "%s"}\n' \
        "$DEFAULT_ADMIN_USER" "$PWHASH" \
        > "$INSTALL_DIR/config/auth.json"
    chmod 600 "$INSTALL_DIR/config/auth.json"
    AUTH_CREATED=1
else
    info "auth.json already exists — preserving"
    AUTH_CREATED=0
fi

# ============ systemd units ============
info "Installing systemd unit: $SERVICE_NAME.service"
install -m 644 "$INSTALL_DIR/systemd/$SERVICE_NAME.service" \
    "/etc/systemd/system/$SERVICE_NAME.service"

info "Installing systemd unit: avahi-alias@.service"
install -m 644 "$INSTALL_DIR/systemd/avahi-alias@.service" \
    "/etc/systemd/system/avahi-alias@.service"

info "Installing systemd units: cnc-probe-maintenance.service + .timer"
install -m 644 "$INSTALL_DIR/systemd/cnc-probe-maintenance.service" \
    "/etc/systemd/system/cnc-probe-maintenance.service"
install -m 644 "$INSTALL_DIR/systemd/cnc-probe-maintenance.timer" \
    "/etc/systemd/system/cnc-probe-maintenance.timer"

info "Installing mDNS publisher: /usr/local/bin/avahi-alias"
install -m 755 "$INSTALL_DIR/bin/avahi-alias" "/usr/local/bin/avahi-alias"

systemctl daemon-reload

# ============ firewall ============
if command -v ufw >/dev/null 2>&1; then
    if ufw status 2>/dev/null | grep -q "Status: active"; then
        info "Allowing port $APP_PORT/tcp through ufw..."
        ufw allow "$APP_PORT/tcp" >/dev/null 2>&1 || true
        info "Allowing port 5353/udp through ufw (mDNS)..."
        ufw allow 5353/udp >/dev/null 2>&1 || true
    else
        info "ufw installed but not active — skipping firewall rules"
        info "  (enable later with: sudo ufw allow $APP_PORT/tcp && sudo ufw allow 5353/udp)"
    fi
fi

# ============ enable + start services ============
info "Enabling and starting $SERVICE_NAME..."
systemctl enable --now "$SERVICE_NAME"

info "Enabling and starting mDNS alias ($MDNS_ALIAS)..."
systemctl enable --now "avahi-alias@$MDNS_ALIAS"

info "Enabling daily database maintenance timer..."
systemctl enable --now cnc-probe-maintenance.timer

# ============ summary ============
sleep 2
SRV_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$SRV_IP" ] && SRV_IP="<server-ip>"

echo ""
echo "================================================================"
echo "  CNC Probe install complete"
echo "================================================================"
echo ""
echo "  Dashboard:   http://$MDNS_ALIAS:$APP_PORT/"
echo "               http://$SRV_IP:$APP_PORT/"
echo ""
echo "  Admin:       http://$MDNS_ALIAS:$APP_PORT/admin"
if [ "$AUTH_CREATED" -eq 1 ]; then
    echo "  Login:       $DEFAULT_ADMIN_USER / $DEFAULT_ADMIN_PASS"
    echo "               (change it in the admin panel after first login)"
else
    echo "  Login:       (existing auth.json was preserved)"
fi
echo ""
echo "  Service:     systemctl status $SERVICE_NAME"
echo "  Logs:        journalctl -u $SERVICE_NAME -f"
echo "  Update:      cd $INSTALL_DIR && sudo bash update.sh"
echo "  Uninstall:   cd $INSTALL_DIR && sudo bash uninstall.sh"
echo ""
echo "================================================================"
echo ""

systemctl status "$SERVICE_NAME" --no-pager --lines=5 || true
