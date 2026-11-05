#!/bin/bash
# Remove pds-sync. NEVER touches stack data dirs, stores, or containers.
set -u
BASE="${PDS_BASE:-/var/lib/portainer-dockhand-sync}"
systemctl --user disable --now pds-sync.service 2>/dev/null || true
systemctl --user disable --now pds-health.timer 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true
rm -f ~/.config/systemd/user/pds-sync.service ~/.config/systemd/user/pds-health.*
docker rm -f pds-sync 2>/dev/null || true
docker rmi local/portainer-dockhand-sync:latest 2>/dev/null || true
# keep: $BASE (logs+state) and the config file (your key)
echo "removed. state/logs kept under $BASE (delete manually if wanted)"
