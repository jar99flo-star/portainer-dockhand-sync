# Changelog

All notable changes to **pds-sync** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions use
[Semantic Versioning](https://semver.org/).

## [Unreleased] — first public release

### Added
- Two-way Portainer ⇄ Dockhand stack synchronizer.
- **Canonicalization** of relative bind/env/build paths to a shared
  `PDS_DATA_ROOT/<name>/…` so both tools bind the *same* on-disk data
  (prevents data-splitting). Same-device rename migration; never overwrites a
  target, never deletes a non-empty source.
- **Create mirroring** in both directions:
  - Portainer → Dockhand (store file + `stack_sources` DB row).
  - Dockhand → Portainer (REST create + store file fallback + `compose up`).
- **Edit mirroring**: the changed side is treated as the origin; both stores and
  containers converge.
- **Delete mirroring** in both directions with a strong per-stack signal so a
  transient empty DB/API read cannot mass-delete. Data dirs are never deleted.
- **Drift self-heal**: every `DEEP_INTERVAL`, both stores are forced back to
  canonical bytes.
- **Convergence**: `docker compose -p <name> up -d` from the canonical file so
  both tools share the same containers (same project + config-hash labels).
- Vendored `docker` CLI + compose plugin at build time → identical
  `config-hash` to host-side Compose v5 (no spurious recreates).
- Runs as a **user systemd unit** (no root/sudo), long-lived container on the
  host, with a liveness **watchdog** timer.
- **DRY_RUN** mode for safe testing; **EXCLUDE_STACKS** for out-of-band stacks;
  **DEPLOY_ON_SYNC** toggle (mirror files+registration only).
- Single-instance lock; `state/`, `logs/`, `cache/`, `deploy/` layout;
  append-only `logs/sync-events.jsonl` action log.
- `install.sh` (idempotent, DRY_RUN smoke test), `uninstall.sh` (keeps data),
  `healthcheck.sh`, MIT `LICENSE`, `.gitignore`.

### Notes (important for operators)
- **Portainer 2.45.x** authenticates via the **`X-API-Key`** header (not the
  `X-Http-Auth-Token` header in older docs/UI). `sync.py` sends `X-API-Key`.
- The legacy `POST /api/stacks` create route no longer exists; create is
  `POST /api/stacks/create/standalone/string?endpointId=N`. The docker endpoint
  id is **auto-detected** from `GET /api/endpoints` (config value is a fallback).
- The container runs as **root** with the Docker socket mounted read-only and
  uses short-lived `docker run`/`exec`/`cp` helpers for the few root-only
  writes (Dockhand DB, store files).

### Fixed (bugs found during live validation on a 2.45.x instance)
- **Wrong auth header** → every Portainer call 401'd (`token missing`). Now uses
  `X-API-Key`.
- **`write_portainer_entry` returned an int (`rc`)** where callers expected a
  bool, so a *successful* write was misread as a failure (`0` is falsy) — hashes
  never committed and the service re-synced every stack each poll. Now returns
  `rc == 0`.
- **Crash on Dockhand-side delete**: `row, _ = dockhand_db()` unpacked the int
  `env_id` into `row`, so `nm not in row` raised `TypeError` and killed the whole
  scan each cycle. Correctly unpacks `env_id, row`.
- **Dockhand-side delete restored instead of mirrored**: the branch checked the
  *Portainer API record* (wrong signal). Now checks the **Dockhand DB row** as
  the source of truth and mirror-deletes to Portainer only when the Dockhand
  store file *and* DB row are both gone.
- **Wrong id for Portainer delete**: used the store dir id; now uses the
  Portainer **stack id** (`info["id"]`) for the API delete, with local
  store/`compose down` cleanup only as a fallback.
