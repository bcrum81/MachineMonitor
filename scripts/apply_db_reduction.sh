#!/usr/bin/env bash
#
# One-time cutover to change-only poll storage + retention.
# ---------------------------------------------------------
# Safe to run once, on the live box, as root. It:
#   1. stops cnc-probe (so the new de-duplicating db.py loads on restart and so
#      VACUUM can get the exclusive lock it needs to reclaim disk),
#   2. backfills machine_current, prunes polls/poll_errors older than 30 days,
#      and VACUUMs to shrink the file,
#   3. installs + enables the daily maintenance timer,
#   4. restarts cnc-probe.
#
# Reporting data (events, webhook_deliveries, alarms_catalog) is NOT touched.
#
# Usage:
#   sudo bash /opt/cnc-probe/scripts/apply_db_reduction.sh
#
set -euo pipefail

INSTALL_DIR="/opt/cnc-probe"
RETENTION_DAYS=30

info() { printf "\033[1;36m[cutover]\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m[cutover]\033[0m ERROR: %s\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Must be run as root. Try: sudo bash $0"
[ -f "$INSTALL_DIR/maintenance.py" ] || die "maintenance.py not found in $INSTALL_DIR"

DB="$INSTALL_DIR/data/cnc_data.db"
info "Database size before: $(du -h "$DB" 2>/dev/null | cut -f1)"

info "Stopping cnc-probe (brief polling pause; the new storage code loads on restart)..."
systemctl stop cnc-probe

info "Running one-time maintenance (backfill + prune ${RETENTION_DAYS}d + compact + VACUUM)..."
"$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/maintenance.py" --retention-days "$RETENTION_DAYS" --compact

info "Installing daily maintenance timer..."
install -m 644 "$INSTALL_DIR/systemd/cnc-probe-maintenance.service" \
    "/etc/systemd/system/cnc-probe-maintenance.service"
install -m 644 "$INSTALL_DIR/systemd/cnc-probe-maintenance.timer" \
    "/etc/systemd/system/cnc-probe-maintenance.timer"
systemctl daemon-reload
systemctl enable --now cnc-probe-maintenance.timer

info "Starting cnc-probe..."
systemctl start cnc-probe

sleep 2
info "Database size after:  $(du -h "$DB" 2>/dev/null | cut -f1)"
info "cnc-probe status:"
systemctl --no-pager status cnc-probe --lines=3 || true
info "Next scheduled maintenance:"
systemctl list-timers cnc-probe-maintenance.timer --no-pager || true
info "Done."
