#!/bin/bash
# ============================================================================
# pds-sync installer — Portainer <-> Dockhand stack synchronizer
#
# Run as the user that will operate the Docker host (a *user* systemd install,
# so no root/sudo is required — see README "Install").
#
#   bash install.sh
#
# Override the install location if you don't want the default:
#   PDS_BASE=/opt/pds-sync bash install.sh
# ============================================================================
set -euo pipefail

USER_NAME="$(id -un)"
BASE="${PDS_BASE:-/var/lib/portainer-dockhand-sync}"
SRC="${PDS_SRC:-$BASE}"   # where sync.py/Dockerfile/etc. live (this project)
[ -d "$SRC" ] || { echo "ERROR: sources not found at $SRC (place them there first)"; exit 1; }

echo "==> [1/6] building image local/portainer-dockhand-sync:latest"
cd "$SRC"
# vendored docker CLI + compose plugin so the container's `docker compose`
# produces the SAME config-hash as host-side `docker compose` (Compose v5).
cp /usr/bin/docker ./docker
cp /usr/libexec/docker/cli-plugins/docker-compose ./docker-compose
docker build -q -t local/portainer-dockhand-sync:latest .
rm -f ./docker ./docker-compose
echo "    image: $(docker images local/portainer-dockhand-sync:latest --format '{{.ID}} {{.Size}}')"

echo "==> [2/6] working dirs"
mkdir -p "$BASE/state" "$BASE/logs" "$BASE/cache"
chown -R "$USER_NAME":"$USER_NAME" "$BASE"

echo "==> [3/6] config"
CFG=/etc/pds/config.env
if ! mkdir -p /etc/pds 2>/dev/null; then
    CFG="$BASE/config.env"
    echo "    /etc not writable -> config goes to $CFG"
fi
if [ ! -f "$CFG" ]; then
    cat > "$CFG" <<'CFGEOF'
# pds-sync configuration (see README)
# --- Portainer (REST API) ---
# Base URL of the Portainer you sync against (local instance on the host):
PORTAINER_URL=https://127.0.0.1:9000
# Create: Portainer UI -> Settings -> API Keys -> Add API Key (no expiry).
# NOTE: this Portainer build authenticates via the X-API-Key header (sync.py
# sends that automatically) — NOT the X-Http-Auth-Token header in older docs.
PORTAINER_API_KEY=
# Fallback Docker endpoint id. sync.py AUTO-DETECTS it from GET /api/endpoints,
# so leave this as-is unless detection fails for some reason.
PORTAINER_ENDPOINT_ID=1
# --- Dockhand ---
# The Dockhand *environment* name (must match exactly, spaces included).
# Find it in the Dockhand UI / its "stacks/<env>/" volume path.
DOCKHAND_ENV_NAME=My Environment
# --- shared data root for stack data dirs (relative-path canonicalization) ---
# Relative bind paths in a compose file are rewritten to <PDS_DATA_ROOT>/<name>/...
# so Portainer and Dockhand bind to the SAME on-disk data.
PDS_DATA_ROOT=/data/compose
# --- behavior ---
POLL_INTERVAL=20
DEEP_INTERVAL=300
# Comma-separated stack names the sync must NEVER touch. Leave empty unless a
# particular stack is managed out-of-band and would break on a full redeploy.
EXCLUDE_STACKS=
# 1 = converge containers on sync (default); 0 = mirror files+registration only.
DEPLOY_ON_SYNC=1
# --- ownership for migrated data dirs ---
# The uid:gid the service user runs as (id -u / id -g) — used when it moves data.
DEFAULT_UID=1000
DEFAULT_GID=1000
# --- watchdog ---
HEALTH_LIVENESS_SECS=120
CFGEOF
    chmod 600 "$CFG"
    echo "    wrote $CFG"
    echo "    *** EDIT $CFG : set DOCKHAND_ENV_NAME and PORTAINER_API_KEY ***"
else
    echo "    existing $CFG kept"
fi

echo "==> [4/6] systemd user units"
loginctl enable-linger "$USER_NAME" 2>/dev/null || true
mkdir -p ~/.config/systemd/user
# install units, pointing them at THIS install location ($BASE) and the $CFG path
for u in pds-sync.service pds-health.service pds-health.timer; do
    sed "s#/var/lib/portainer-dockhand-sync#$BASE#g" "$SRC/$u" > ~/.config/systemd/user/"$u"
done
if [ "$CFG" != "/etc/pds/config.env" ]; then
    sed -i "s#-e PDS_CONFIG=/etc/pds/config.env#-e PDS_CONFIG=$CFG#" ~/.config/systemd/user/pds-sync.service
    sed -i "s#^Environment=PDS_CFG=.*#Environment=PDS_CFG=$CFG#"         ~/.config/systemd/user/pds-sync.service
fi
chmod +x "$SRC/healthcheck.sh"
if [ "$SRC" != "$BASE" ]; then
    cp -f "$SRC/run-pds.sh" "$BASE/run-pds.sh"
    cp -f "$SRC/healthcheck.sh" "$BASE/healthcheck.sh"
fi
chmod 755 "$BASE/run-pds.sh"
# run-pds.sh + healthcheck.sh hard-code the base/config path; point them at $BASE / $CFG
sed -i "s#/var/lib/portainer-dockhand-sync#$BASE#g; s#/etc/pds/config.env#$CFG#g" "$BASE/run-pds.sh" "$BASE/healthcheck.sh"
systemctl --user daemon-reload
systemctl --user enable --now pds-sync.service
systemctl --user enable --now pds-health.timer
echo "    units installed (user: $USER_NAME)"

echo "==> [5/6] smoke test (DRY_RUN, one cycle — verifies store access, writes NOTHING)"
docker rm -f pds-sync-test 2>/dev/null || true
PDS_CFG="$CFG" PDS_NAME=pds-sync-test PDS_ONCE=1 PDS_LOGLEVEL=INFO DRY_RUN=1 "$BASE/run-pds.sh" 2>&1 | tail -25 || true
echo "    (smoke test is DRY_RUN=1: no files, rows, records, or containers were touched)"

echo "==> [6/6] status"
systemctl --user --no-pager status pds-sync.service | head -6 || true
echo
echo "DONE. Useful commands:"
echo "  journalctl --user -u pds-sync -f          # live log"
echo "  tail -f $BASE/logs/sync-events.jsonl      # action events"
echo "  touch $BASE/state/.trigger                # force immediate resync"
echo "  systemctl --user restart pds-sync         # restart"
echo "  bash $BASE/uninstall.sh                   # remove (keeps your data)"
