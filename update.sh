#!/usr/bin/env bash
#
# CNC Probe updater
# -----------------
# Pulls latest from GitHub, refreshes Python deps, reinstalls systemd units
# if they have changed, and restarts the service. Leaves config/ and data/
# completely untouched.
#
# Usage:
#   cd /opt/cnc-probe
#   sudo bash update.sh
#

set -euo pipefail

INSTALL_DIR="/opt/cnc-probe"
SERVICE_NAME="cnc-probe"
MDNS_ALIAS="cnc-probe.local"

info()  { printf "\033[1;36m[UPDATE]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[UPDATE]\033[0m WARN: %s\n" "$*" >&2; }
die()   { printf "\033[1;31m[UPDATE]\033[0m ERROR: %s\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Must be run as root. Try: sudo bash update.sh"
[ -d "$INSTALL_DIR/.git" ] || die "$INSTALL_DIR is not a git checkout — run install.sh first"

cd "$INSTALL_DIR"

# Stash any local edits (shouldn't happen on a clean install, but be safe)
if ! git diff --quiet || ! git diff --cached --quiet; then
    STASH_NAME="cnc-probe-pre-update-$(date +%Y%m%d-%H%M%S)"
    warn "Local changes detected — stashing as '$STASH_NAME'"
    git stash push -u -m "$STASH_NAME" || true
fi

info "Fetching latest from GitHub..."
git fetch --all --quiet

info "Fast-forwarding to origin/$(git rev-parse --abbrev-ref HEAD)..."
git pull --ff-only

info "Updating Python requirements..."
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

# Re-install systemd units if they changed in the repo
UNITS_CHANGED=0

if [ -f "$INSTALL_DIR/systemd/$SERVICE_NAME.service" ]; then
    if ! cmp -s "$INSTALL_DIR/systemd/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"; then
        info "Service unit changed — reinstalling /etc/systemd/system/$SERVICE_NAME.service"
        install -m 644 "$INSTALL_DIR/systemd/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
        UNITS_CHANGED=1
    fi
fi

if [ -f "$INSTALL_DIR/systemd/avahi-alias@.service" ]; then
    if ! cmp -s "$INSTALL_DIR/systemd/avahi-alias@.service" "/etc/systemd/system/avahi-alias@.service"; then
        info "avahi-alias@.service changed — reinstalling"
        install -m 644 "$INSTALL_DIR/systemd/avahi-alias@.service" "/etc/systemd/system/avahi-alias@.service"
        UNITS_CHANGED=1
    fi
fi

AVAHI_SCRIPT_CHANGED=0
if [ -f "$INSTALL_DIR/bin/avahi-alias" ]; then
    if ! cmp -s "$INSTALL_DIR/bin/avahi-alias" "/usr/local/bin/avahi-alias"; then
        info "avahi-alias script changed — reinstalling /usr/local/bin/avahi-alias"
        install -m 755 "$INSTALL_DIR/bin/avahi-alias" "/usr/local/bin/avahi-alias"
        AVAHI_SCRIPT_CHANGED=1
    fi
fi

if [ "$UNITS_CHANGED" -eq 1 ] || [ "$AVAHI_SCRIPT_CHANGED" -eq 1 ]; then
    systemctl daemon-reload
fi

if [ "$AVAHI_SCRIPT_CHANGED" -eq 1 ]; then
    info "Restarting mDNS alias..."
    systemctl restart "avahi-alias@$MDNS_ALIAS" || true
fi

info "Restarting $SERVICE_NAME..."
systemctl restart "$SERVICE_NAME"

sleep 2
echo ""
systemctl status "$SERVICE_NAME" --no-pager --lines=5 || true

echo ""
info "Update complete"
