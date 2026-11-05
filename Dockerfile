# pds-sync - Portainer <-> Dockhand stack synchronizer
# Base: debian:trixie (same libc family as the host => vendored docker CLI works)
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*

# vendored docker CLI + compose plugin (copied from the host at build time =>
# identical config-hash behavior to host-side `docker compose`)
RUN mkdir -p /usr/lib/docker/cli-plugins
COPY docker /usr/bin/docker
COPY docker-compose /usr/lib/docker/cli-plugins/docker-compose
RUN chmod 755 /usr/bin/docker /usr/lib/docker/cli-plugins/docker-compose

RUN mkdir -p /opt/pds /etc/pds /home/pds && chown 1000:1000 /home/pds
COPY sync.py /opt/pds/sync.py
ENV HOME=/home/pds \
    PDS_WORK=/work \
    PDS_CONFIG=/etc/pds/config.env \
    CLI_PLUGIN_ROOT=/usr/lib/docker/cli-plugins
WORKDIR /opt/pds
USER 1000:1000
ENTRYPOINT ["python3", "/opt/pds/sync.py"]
