#!/bin/sh
# Restart pds-sync (user unit) if its liveness file is stale (>HEALTH_LIVENESS_SECS)
# or the container is not running. Invoked by pds-health.timer (every minute).
set -u
SECS=120
if [ -f /etc/pds/config.env ]; then
    v=$(grep -E '^HEALTH_LIVENESS_SECS=' /etc/pds/config.env | head -1 | cut -d= -f2)
    [ -n "$v" ] && SECS=$v
fi
LIVE=/var/lib/portainer-dockhand-sync/state/.alive
if ! docker ps -q --filter name=^/pds-sync$ 2>/dev/null | grep -q .; then
    echo "$(date -Is) pds-sync container not running -> restarting unit"
    systemctl --user restart pds-sync.service 2>/dev/null || true
    exit 0
fi
if [ -f "$LIVE" ]; then
    now=$(date +%s); m=$(cat "$LIVE" 2>/dev/null || echo 0)
    case "$m" in (*[!0-9]*|"") m=0;; esac
    age=$((now - m))
    if [ "$age" -gt "$SECS" ]; then
        echo "$(date -Is) liveness stale (age=${age}s > ${SECS}s) -> restarting unit"
        systemctl --user restart pds-sync.service 2>/dev/null || true
        exit 0
    fi
fi
exit 0
