#!/usr/bin/env bash
#
# CNC Probe uninstaller
# ---------------------
# Stops and removes the service, the mDNS alias, and the avahi-alias helper.
# Prompts before deleting /opt/cnc-probe/ itself (which still contains
# config/ — credentials, machine configs, webhooks, Sheets — and data/ — the
# SQLite history database).
#
# Usage:
#   cd /opt/cnc-probe
#   sudo bash uninstall.sh
#

set -euo pipefail

INSTALL_DIR="/opt/cnc-probe"
SERVICE_NAME="cnc-probe"
MDNS_ALIAS="cnc-probe.local"
APP_PORT=8765

info()  { printf "\033[1;36m[UNINSTALL]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[UNINSTALL]\033[0m WARN: %s\n" "$*" >&2; }
die()   { printf "\033[1;31m[UNINSTALL]\033[0m ERROR: %s\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Must be run as root. Try: sudo bash uninstall.sh"

info "Stopping and disabling $SERVICE_NAME..."
systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true

info "Stopping and disabling avahi-alias@$MDNS_ALIAS..."
systemctl disable --now "avahi-alias@$MDNS_ALIAS" 2>/dev/null || true

info "Stopping and disabling database maintenance timer..."
systemctl disable --now cnc-probe-maintenance.timer 2>/dev/null || true

info "Removing systemd unit files..."
rm -f "/etc/systemd/system/$SERVICE_NAME.service"
rm -f "/etc/systemd/system/avahi-alias@.service"
rm -f "/etc/systemd/system/cnc-probe-maintenance.service"
rm -f "/etc/systemd/system/cnc-probe-maintenance.timer"

info "Removing mDNS publisher: /usr/local/bin/avahi-alias"
rm -f "/usr/local/bin/avahi-alias"

systemctl daemon-reload

# Optional ufw cleanup
if command -v ufw >/dev/null 2>&1; then
    if ufw status 2>/dev/null | grep -q "Status: active"; then
        info "Removing ufw rules for port $APP_PORT/tcp and 5353/udp..."
        ufw delete allow "$APP_PORT/tcp" >/dev/null 2>&1 || true
        ufw delete allow 5353/udp >/dev/null 2>&1 || true
    fi
fi

echo ""
echo "----------------------------------------------------------------"
echo "  Service removed. The install directory remains:"
echo ""
echo "    $INSTALL_DIR"
echo ""
echo "  It contains:"
echo "    - venv/      (Python environment)"
echo "    - config/    (admin credentials, machines, webhooks, Sheets config)"
echo "    - data/      (SQLite history database)"
echo "    - Python source + static assets"
echo ""
echo "  Deleting it is PERMANENT — all machine configs, webhook subscriptions,"
echo "  Google Sheets settings, cycle history, and admin credentials will be lost."
echo "----------------------------------------------------------------"
echo ""
read -r -p "Delete $INSTALL_DIR and everything inside it? [yes/NO] " REPLY
case "$REPLY" in
    yes|YES)
        info "Removing $INSTALL_DIR..."
        # Be defensive — must match exactly
        if [ "$INSTALL_DIR" = "/opt/cnc-probe" ]; then
            rm -rf "$INSTALL_DIR"
            info "Removed $INSTALL_DIR"
        else
            die "Refusing to rm -rf '$INSTALL_DIR' — unexpected path"
        fi
        ;;
    *)
        info "$INSTALL_DIR preserved"
        info "To remove later:  sudo rm -rf $INSTALL_DIR"
        ;;
esac

echo ""
info "Uninstall complete"
