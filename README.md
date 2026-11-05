# pds-sync — Portainer ⇄ Dockhand stack synchronizer

Two-way synchronizer that keeps every **Portainer** stack and every **Dockhand**
stack in lockstep so that **both tools can manage, edit, update, and delete
every stack**, and a stack created in *either* tool automatically appears in the
*other* and runs from a single shared compose file.

Portainer and Dockhand are great on their own, but they keep compose files in
**separate volumes** with no shared discovery — a stack created in one is
invisible to the other. `pds-sync` closes that gap. It runs on the Docker host as
a lightweight long-lived container (started by a **user** systemd unit, so it
needs no root/sudo), and continuously reconciles the two stores.

## What it does

| Event | Reaction |
|---|---|
| New stack created in **Portainer** | canonicalize → write to the Dockhand store + a `stack_sources` DB row → now visible & manageable in Dockhand (containers already up via Portainer) |
| New stack created in **Dockhand** | canonicalize → write to the Portainer store + a Portainer API record → `compose up` → now visible & manageable in Portainer |
| Stack **edited** in either | the changed side is the *origin* → both stores + containers converge |
| Stack **deleted** in either | mirror: remove the other side's store file + its DB row / Portainer record + `compose down`. **Data dirs are never deleted.** |
| Drift (self-heal) | every `DEEP_INTERVAL`, both stores are forced back to the canonical bytes |

## Canonicalization (the data-safety core)
Relative bind/env/build paths (`./data`, `../x`, `./app.yml`) resolve to a
*different physical location depending on which tool deploys them* — a naive file
copy would **split the data**. `pds-sync` rewrites every relative path to an
**absolute** path under `PDS_DATA_ROOT/<stackname>/…` so both tools bind to the
**same** on-disk data. If a relative stack already has data where a tool
previously deployed it, `pds-sync` **moves** it (same device → instant rename;
never overwrites an existing target; never deletes a non-empty source) before
converging. Absolute paths and named volumes are left untouched.

## Convergence
After mirroring, `pds-sync` runs `docker compose -p <name> up -d` from the
canonical file so both tools point at the **same** containers (same
`com.docker.compose.project` + `config-hash` labels → Compose v5 recognizes them).
If a stack is *already running* with a matching config it is left alone (no churn).

## Requirements
- A Docker host running both **Portainer CE** and **Dockhand** (any recent
  Portainer; tested against 2.45.x).
- A user with permission to run `docker` and use a **user systemd instance**
  (no root/sudo needed).
- The host's `docker` CLI + `docker-compose` plugin (vendored into the image at
  build time so `config-hash` behavior matches the host exactly).

## Install
```bash
# place this directory somewhere stable, e.g. /var/lib/portainer-dockhand-sync
cd /var/lib/portainer-dockhand-sync
bash install.sh                 # builds the image, writes a config template, installs user units
```
`install.sh`:
1. builds `local/portainer-dockhand-sync:latest` (vendoring the host's `docker` CLI + compose plugin),
2. writes a config file (`/etc/pds/config.env`, or the project dir if `/etc` isn't writable),
3. installs `pds-sync.service` + `pds-health.*` as **user** units and enables linger,
4. runs a **dry-run** cycle to verify it can read both stores,
5. starts the service.

Then edit the config and fill in the two required values:
```bash
EDITOR=/usr/bin/nano editor /etc/pds/config.env
#   DOCKHAND_ENV_NAME=...        # your Dockhand environment name (exact, spaces included)
#   PORTAINER_API_KEY=...        # Portainer UI -> Settings -> API Keys -> Add API Key
systemctl --user restart pds-sync
```

### Uninstall
```bash
bash uninstall.sh    # removes the units + container; KEEPS config, state, logs, and all data
```

## Portainer API key (required for Portainer-side create/delete)
Create one in the Portainer UI: **Settings → API Keys → Add API Key** (no expiry).
Put it in `config.env`:

```
PORTAINER_API_KEY=ptr_...
PORTAINER_ENDPOINT_ID=1      # fallback only — see auto-detect below
```

### Non-obvious: this build uses the `X-API-Key` header
Recent Portainer builds (2.45.x) want the key in the **`X-API-Key`** request
header — **not** the `X-Http-Auth-Token` header that older docs/UI use. A key
sent via `X-Http-Auth-Token` is silently ignored and every call returns
`401 "A valid authorization token is missing"`. `sync.py` sends `X-API-Key`
automatically; do not "fix" it back.

### Stack routes (verified against 2.45.x)
The legacy `POST /api/stacks` create route no longer exists. `sync.py` targets:

| Op | Method + path |
|---|---|
| List | `GET /api/stacks` |
| Create | `POST /api/stacks/create/standalone/string?endpointId=N`  (body `{Name, Env, StackFileContent}`) |
| Update | `PUT /api/stacks/{id}?endpointId=N`  (body `{StackFileContent, Env, Prune}`) |
| Delete | `DELETE /api/stacks/{id}?endpointId=N&external=false` |
| Get file | `GET /api/stacks/{id}/file?endpointId=N` |

Note the create path segment is **`standalone`** (for Compose stacks) — not
`compose` and not a numeric type. The `400` message "type must be 1 or 2" is
misleading; the handler switches on `swarm|standalone|kubernetes`.

**Endpoint id auto-detect:** `sync.py` reads the correct docker endpoint id from
`GET /api/endpoints` and only falls back to `PORTAINER_ENDPOINT_ID` if it can't.
Leave the config value at its default.

Without a key, the sync still mirrors Dockhand→Portainer *files* and deploys, but
defers the Portainer *record* create (retried each round) and skips
Portainer-driven create/delete.

## Configuration (`config.env`)
| Key | Meaning |
|---|---|
| `PORTAINER_URL` | Portainer base URL (default `https://127.0.0.1:9000`) |
| `PORTAINER_API_KEY` | **Required** for Portainer-side create/delete (see above) |
| `PORTAINER_ENDPOINT_ID` | fallback docker endpoint id (auto-detected) |
| `DOCKHAND_ENV_NAME` | **Required** — the Dockhand environment name (exact) |
| `PDS_DATA_ROOT` | shared data root for canonicalized relative paths (default `/data/compose`) |
| `POLL_INTERVAL` | seconds between store scans (default 20) |
| `DEEP_INTERVAL` | seconds between full store re-canonicalization (default 300) |
| `EXCLUDE_STACKS` | comma-separated names the sync never touches (default empty) |
| `DEPLOY_ON_SYNC` | `1` = converge containers (default); `0` = mirror files + registration only |
| `DEFAULT_UID` / `DEFAULT_GID` | ownership for migrated data dirs (your uid/gid) |
| `HEALTH_LIVENESS_SECS` | liveness threshold for the watchdog (default 120) |
| `DRY_RUN` | `1` = log intended actions without writing (for testing) |

## Operations
```bash
journalctl --user -u pds-sync -f                 # live log
tail -f /var/lib/portainer-dockhand-sync/logs/sync-events.jsonl   # action events
touch /var/lib/portainer-dockhand-sync/state/.trigger   # force an immediate resync
systemctl --user restart pds-sync               # restart
systemctl --user is-active pds-sync pds-health.timer
docker logs pds-sync                            # container log
```

## Safety model
- **Never deletes data dirs** — only store files, DB rows, API records, containers.
- Data migration is a same-device rename; it refuses to overwrite an existing
  target and aborts the sync (not the data) if a move fails.
- A new stack is only registered/deployed after `docker compose config` validates
  the canonical file.
- Deletion is mirrored only on a strong per-stack signal (that side's store file
  **and** DB row / Portainer record both gone), so a transient empty DB read
  cannot mass-delete stacks that still have files.
- Single-instance lock + a liveness watchdog (the user timer restarts the unit if
  the container goes stale).
- The `pds-sync` container runs as **root** with the Docker socket mounted
  read-only and uses short-lived `docker run`/`exec`/`cp` helpers for the few
  root-only writes (Dockhand DB, store files).

## Layout
```
/var/lib/portainer-dockhand-sync/
├── sync.py          # the synchronizer (what actually runs)
├── Dockerfile       # runtime image (python3 + vendored docker CLI)
├── run-pds.sh       # docker-run wrapper (host config -> /etc/pds/config.env)
├── install.sh / uninstall.sh / healthcheck.sh
├── pds-sync.service / pds-health.service / pds-health.timer   # user units
├── config.env       # host config, mode 600, incl. PORTAINER_API_KEY (not committed)
├── state/           # sync-state.json + canonical/ (per-stack canonical bytes)
├── logs/            # sync-events.jsonl (append-only action log)
├── cache/           # ephemeral store snapshots (safe to delete)
└── deploy/          # transient workdir for compose up/validate
```

## How a create flows (Dockhand → Portainer)
1. `pds-sync` sees a new dir in `dockhand:/app/data/stacks/<env>/<name>/` (or a
   new `stack_sources` row).
2. Canonicalize `compose.yaml` (relative → `PDS_DATA_ROOT/<name>/…`).
3. Migrate any existing relative-path data into the canonical location.
4. `POST /api/stacks/create/standalone/string?endpointId=N` (Portainer API) →
   creates `portainer:/data/compose/<newid>/` and the Portainer record; the store
   file is also written directly as a fallback.
5. `docker compose -p <name> up -d` → containers converge.
6. State committed; the next poll sees both stores equal → no-op.

## Troubleshooting
- **`401 token missing`** → you're (or a client is) sending the key via the wrong
  header. `pds-sync` uses `X-API-Key`; verify `PORTAINER_API_KEY` is correct.
- **`endpoint not found` / create 400** → the endpoint id didn't resolve; check
  `GET /api/endpoints` and set `PORTAINER_ENDPOINT_ID` explicitly.
- **Stack not appearing** → confirm `DOCKHAND_ENV_NAME` matches exactly (spaces
  included) and the stack isn't in `EXCLUDE_STACKS`.
- **Want to test without touching anything** → set `DRY_RUN=1` and watch the log.

## Scope / notes
- Stacks listed in `EXCLUDE_STACKS` are intentionally left out-of-band (e.g. a
  stack whose database was upgraded standalone and would break on a full
  `compose up`). Manage those in their own tool as before.
- Standalone (non-stack) containers are out of scope — they aren't Portainer or
  Dockhand stacks.

## License
MIT — see [LICENSE](LICENSE).
