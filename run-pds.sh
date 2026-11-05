#!/bin/sh
# pds-sync runner (invoked by the user systemd unit)
#   PDS_CFG  : host path of the config file (default: /var/lib/portainer-dockhand-sync/config.env)
#   PDS_NAME : container name (default: pds-sync)
# --network=host: the service talks to 127.0.0.1:9000 (Portainer) on the host loopback.
# Runs as root: needs full docker-socket control + spawns root helper containers.
exec docker run --rm --name "${PDS_NAME:-pds-sync}" \
    --network=host \
    --user 0:0 \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v /var/lib/portainer-dockhand-sync:/work \
    -v "${PDS_CFG:-/var/lib/portainer-dockhand-sync/config.env}":/etc/pds/config.env:ro \
    -e PDS_CONFIG=/etc/pds/config.env \
    local/portainer-dockhand-sync:latest
