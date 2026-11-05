#!/usr/bin/env python3
"""
pds-sync - two-way Portainer <-> Dockhand compose-stack synchronizer.

Both stores are watched by content hash (polling; robust on docker volumes):
  - Portainer store: portainer:/data/compose/<id>/docker-compose.yml
  - Dockhand  store: /app/data/stacks/<ENV>/<name>/compose.yaml

NEW/CHANGED stack in either store -> canonicalize (relative bind paths ->
PDS_DATA_ROOT/<portainer_id>/...) -> write byte-identical into the OTHER
store (file + companion env files) -> register there (Dockhand DB row or
Portainer REST API) -> deploy once from the shared canonical file so both
tools converge on the SAME containers (same project name + config-hash).

DELETED stack in either store -> mirrored (store file, Dockhand row,
Portainer API record, containers/networks). DATA DIRS ARE NEVER DELETED.

Deep reconcile every DEEP_INTERVAL forces both stores to canonical bytes.
Docker access via mounted /var/run/docker.sock using the vendored CLI +
compose plugin (copied from the host at build time => identical hash).
"""
import base64, fcntl, hashlib, json, logging, os, re, shlex, shutil, signal, subprocess, sys, time
import ssl, urllib.request, urllib.error
from datetime import datetime, timezone

LOG = logging.getLogger("pds")

def load_config():
    cfg = {
        "PORTAINER_URL": "https://127.0.0.1:9000",
        "PORTAINER_API_KEY": "",
        "PORTAINER_ENDPOINT_ID": "1",
        "DOCKHAND_ENV_NAME": "My Environment",
        "PDS_DATA_ROOT": "/data/compose",
        "POLL_INTERVAL": "20",
        "DEEP_INTERVAL": "300",
        "EXCLUDE_STACKS": "",  # leave empty unless a stack is managed out-of-band
        "DEPLOY_ON_SYNC": "1",
        "DRY_RUN": "0",
        "DEFAULT_UID": "1000",
        "DEFAULT_GID": "1000",
        "HEALTH_LIVENESS_SECS": "120",
    }
    path = os.environ.get("PDS_CONFIG", "/etc/pds/config.env")
    if os.path.isfile(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    cfg["POLL_INTERVAL"] = int(cfg["POLL_INTERVAL"])
    cfg["DEEP_INTERVAL"] = int(cfg["DEEP_INTERVAL"])
    cfg["EXCLUDE"] = {x.strip() for x in cfg["EXCLUDE_STACKS"].split(",") if x.strip()}
    cfg["DRY_RUN"] = cfg["DRY_RUN"] not in ("0", "false", "no")
    cfg["DEPLOY"] = cfg["DEPLOY_ON_SYNC"] not in ("0", "false", "no")
    return cfg

CFG = load_config()
WORK = os.environ.get("PDS_WORK", "/work")
STATE_PATH = os.path.join(WORK, "state", "sync-state.json")
LIVENESS = os.path.join(WORK, "state", ".alive")
TRIGGER = os.path.join(WORK, "state", ".trigger")
LOCK_PATH = os.path.join(WORK, ".lock")
EVENT_LOG = os.path.join(WORK, "logs", "sync-events.jsonl")
CANON_DIR = os.path.join(WORK, "state", "canonical")
P_STAGE = os.path.join(WORK, "cache", "portainer-compose")
D_STAGE = os.path.join(WORK, "cache", "dockhand")
DEPLOY_DIR = os.path.join(WORK, "deploy")
PORTAINER_CTR = "portainer"
DOCKHAND_CTR = "dockhand"
PORTAINER_STORE = "portainer:/data/compose"
DOCKHAND_DB = "/app/data/db/dockhand.db"
HELPER_IMAGE = os.environ.get("PDS_HELPER_IMAGE", "local/portainer-dockhand-sync:latest")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------

def sh(args, timeout=120, input_data=None):
    try:
        p = subprocess.run(args, input=input_data, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout: %s" % " ".join(str(a) for a in args[:4])
    except FileNotFoundError as e:
        return 127, "", str(e)

def docker(args, timeout=300, input_data=None):
    return sh(["docker"] + args, timeout=timeout, input_data=input_data)

def dockhand_exec(script, timeout=180):
    return docker(["exec", DOCKHAND_CTR, "sh", "-c", script], timeout=timeout)

def b64e(s):
    if isinstance(s, bytes):
        return base64.b64encode(s).decode()
    return base64.b64encode(s.encode()).decode()

def b64d(s):
    return base64.b64decode(s.encode()).decode()

def cp_in(src, ref):
    return docker(["cp", "--", src, ref])

def cp_out(ref, dst):
    return docker(["cp", "--", ref, dst])

def helper(cmd_script, volumes=(), timeout=900):
    """Root one-shot helper container (own image => bash + coreutils).
    The script is streamed to `bash` on stdin with `set -e`.
    volumes: (host_path, inside_path) pairs."""
    args = ["run", "--rm", "--init", "-i", "--user", "root", "--entrypoint", "bash"]
    for h, i in volumes:
        args += ["-v", "%s:%s" % (h, i)]
    args += ["-e", "PDS_DATA_ROOT=%s" % CFG["PDS_DATA_ROOT"],
             "-e", "PDS_UID=%s" % CFG["DEFAULT_UID"],
             "-e", "PDS_GID=%s" % CFG["DEFAULT_GID"],
             "-e", "PDS_DRUN=%s" % ("1" if CFG["DRY_RUN"] else "0")]
    args.append(HELPER_IMAGE)
    return sh(["docker"] + args, timeout=timeout, input_data="set -e\n" + cmd_script)

def _ssl_ctx():
    # Portainer uses a self-signed cert; we trust it unconditionally.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def p_api(method, path, body=None, expect=(200, 201)):
    """Portainer REST API call (stdlib urllib, no curl). Returns (ok, http_code, payload)."""
    if not CFG["PORTAINER_API_KEY"]:
        return False, 0, {"message": "PORTAINER_API_KEY not set - Portainer-side registration will fail"}
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would call Portainer API %s %s", method, path)
        return True, 200, {"dry_run": True}
    url = CFG["PORTAINER_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", CFG["PORTAINER_API_KEY"])
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as resp:
            code = resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as e:
        code = e.code
        raw = e.read() if e.fp else b""
    except urllib.error.URLError as e:
        return False, 0, {"message": "connection failed: %s" % (e.reason,)}
    except Exception as e:
        return False, 0, {"message": "API error: %s" % e}
    try:
        payload = json.loads(raw.decode()) if raw.strip() else {}
    except Exception:
        payload = {"raw": raw[:300].decode("utf-8", "replace")}
    return (code in expect), code, payload

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)

def stack_state(st, name):
    return st.setdefault("stacks", {}).setdefault(name, {})

# ---------------------------------------------------------------------------
# store I/O (both sides)
# ---------------------------------------------------------------------------

def fetch_portainer_store():
    """Snapshot the portainer compose/ dir. P_STAGE becomes the compose dir
    itself (docker cp semantics: dir -> missing destination)."""
    if os.path.isdir(P_STAGE):
        shutil.rmtree(P_STAGE, ignore_errors=True)
    os.makedirs(os.path.dirname(P_STAGE) or "/", exist_ok=True)
    rc, o, e = cp_out(PORTAINER_STORE, P_STAGE)
    if rc != 0:
        # maybe container stopped -> try start, then retry
        rc2, _, _ = docker(["start", PORTAINER_CTR], timeout=60)
        if rc2 == 0 and not os.path.isdir(P_STAGE):
            rc, o, e = cp_out(PORTAINER_STORE, P_STAGE)
    return rc, e

def portainer_store_ids():
    if not os.path.isdir(P_STAGE):
        return []
    out = []
    for d in sorted(os.listdir(P_STAGE), key=lambda x: (len(x), x)):
        p = os.path.join(P_STAGE, d)
        if d.isdigit() and os.path.isfile(os.path.join(p, "docker-compose.yml")):
            out.append(d)
    return out

def read_portainer_file(id, filename="docker-compose.yml"):
    p = os.path.join(P_STAGE, id, filename)
    if os.path.isfile(p):
        with open(p, "rb") as f:
            return f.read()
    return None

def write_portainer_entry(id, filename, data):
    """Write one file into portainer store dir <id>/ (creates dir if new)."""
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would write portainer store %s/%s (%d bytes)", id, filename, len(data) if isinstance(data, (bytes, str)) else -1)
        return True, ""
    if isinstance(data, str):
        data = data.encode()
    local = os.path.join(DEPLOY_DIR, "_staging", filename)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as f:
        f.write(data)
    ref = PORTAINER_STORE + "/%s/%s" % (id, filename)
    rc, o, e = cp_in(local, ref)
    if rc != 0:
        # new id: create dir first
        dsrc = os.path.join(DEPLOY_DIR, "_stagingdir")
        os.makedirs(dsrc, exist_ok=True)
        rc2, _, _ = cp_in(dsrc, PORTAINER_STORE + "/%s" % id)
        if rc2 == 0:
            rc, o, e = cp_in(local, ref)
    return rc == 0, e   # bool success (callers test `if not ok`), matching write_dh_stack

def delete_portainer_store(id):
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would delete portainer store dir %s", id)
        return True, ""
    vol = portainer_volume_name()
    if not vol:
        return False, "cannot resolve portainer_data volume name"
    host = "/var/lib/docker/volumes/%s/_data" % vol
    script = 'rm -rf "/pv/compose/%s"' % id
    rc, o, e = helper(script, volumes=[(host, "/pv")])
    return (rc == 0), e

def portainer_volume_name():
    rc, o, _ = docker(["inspect", PORTAINER_CTR, "--format",
                       "{{range .Mounts}}{{if eq .Destination \"/data\"}}{{.Name}}{{end}}{{end}}"])
    if rc == 0 and o.strip():
        return o.strip().splitlines()[0].strip()
    return None

def dockhand_volume_name():
    rc, o, _ = docker(["inspect", DOCKHAND_CTR, "--format",
                       "{{range .Mounts}}{{if eq .Destination \"/app/data\"}}{{.Name}}{{end}}{{end}}"])
    if rc == 0 and o.strip():
        return o.strip().splitlines()[0].strip()
    return None

DH_ROOT_FMT = "/app/data/stacks/%s"

def dh_stack_root():
    return DH_ROOT_FMT % CFG["DOCKHAND_ENV_NAME"]

def dh_listing():
    rc, o, e = dockhand_exec("ls -1 '%s' 2>/dev/null | base64 -w0" % dh_stack_root())
    if rc != 0:
        return None, e
    names = b64d(o).splitlines()
    return [n.strip() for n in names if n.strip()], ""

def fetch_dh_stack(name):
    """Return dict filename->bytes for one dockhand store dir (compose + companions)."""
    root = dh_stack_root()
    d = "%s/%s" % (root, name)
    rc, o, e = dockhand_exec("ls -1A '%s' 2>/dev/null | base64 -w0" % d)
    if rc != 0:
        return None, e
    entries = [x.strip() for x in b64d(o).splitlines() if x.strip()]
    out = {}
    for fn in entries:
        if fn.startswith(".") or fn.endswith(".bak") or "/" in fn:
            continue
        if not NAME_RE.match(fn):
            continue
        rc, o, e = dockhand_exec("base64 -w0 '%s/%s/%s' 2>/dev/null" % (root, name, fn))
        if rc == 0 and o:
            out[fn] = base64.b64decode(o.strip())
    return out, ""

def write_dh_stack(name, entries):
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would write dockhand store %s (%d files)", name, len(entries))
        return True, ""
    root = dh_stack_root()
    d = "%s/%s" % (root, name)
    parts = ["mkdir -p '%s'" % d]
    for fn, data in sorted(entries.items()):
        if not NAME_RE.match(fn):
            continue
        parts.append("printf %%s '%s' | base64 -d > '%s/%s'" % (b64e(data), d, fn))
    script = " && ".join(parts)
    rc, o, e = dockhand_exec("set -e; %s" % script, timeout=300)
    return rc == 0, e

def delete_dh_store(name):
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would delete dockhand store dir %s", name)
        return True, ""
    vol = dockhand_volume_name()
    if not vol:
        return False, "cannot resolve dockhand volume name"
    host = "/var/lib/docker/volumes/%s/_data" % vol
    d = "/pv/stacks/%s/%s" % (CFG["DOCKHAND_ENV_NAME"], name)
    script = "rm -rf '%s'" % d
    rc, o, e = helper(script, volumes=[(host, "/pv")])
    return (rc == 0), e

# ---------------------------------------------------------------------------
# canonicalization
# ---------------------------------------------------------------------------

KEY_RE = re.compile(r'^(\s*)([A-Za-z0-9._-]+)\s*:(?:\s+(.*))?\s*$')
ITEM_RE = re.compile(r'^(\s*)-\s+(.*)$')
# key -> (allowed grand-parent context). `dockerfile` is intentionally
# EXCLUDED: it is always relative to `context` and must never be rewritten.
SCALAR_KEYS = {
    "env_file": ("services",),
    "build": ("services",),
    "context": ("build",),
    "source": ("volumes",),
}

def _is_rel(s):
    return s.startswith("./") or s.startswith("../")

def _abs(base, rel):
    return os.path.normpath(os.path.join(base, rel))

def transform_compose(text, name):
    """Rewrite relative paths -> PDS_DATA_ROOT/<name>/... scoped to the right
    YAML contexts. Returns (new_text, {rel: abs}, warnings)."""
    base = CFG["PDS_DATA_ROOT"] + "/" + name
    lines = text.splitlines(True)
    out = []
    keystack = []          # (indent, key, value)
    rewrites = {}
    warns = []

    def rewrite_path(p):
        a = _abs(base, p)
        rewrites[p] = a
        return a

    for line in lines:
        body = line.rstrip("\n")
        indent = len(body) - len(body.lstrip(" "))
        mitem = ITEM_RE.match(body)
        mkey = None if mitem else KEY_RE.match(body)
        comment = ""
        main = body
        if mkey and "#" in (mkey.group(3) or ""):
            # naive comment split (not inside quotes - good enough for these keys)
            v = mkey.group(3) or ""
            ci = v.find(" #")
            if ci >= 0:
                comment = v[ci:]
                mkey = KEY_RE.match(body[:indent + len(mkey.group(2)) + len(mkey.group(3)) - len(v) + ci] )
        if mkey and not mitem:
            key = mkey.group(2)
            value = (mkey.group(3) or "").strip()
            while keystack and keystack[-1][0] >= indent:
                keystack.pop()
            grand = keystack[-2][1] if len(keystack) >= 2 else ""
            parent = keystack[-1][1] if keystack else ""
            ok_ctx = False
            if key == "context" and parent == "build":
                ok_ctx = True
            elif key in ("env_file", "build") and grand == "services":
                ok_ctx = True
            elif key == "file" and keystack and keystack[0][1] == "volumes":
                ok_ctx = True
            elif key == "source" and parent == "volumes" and grand == "services":
                ok_ctx = True
            if value and _is_rel(value) and ok_ctx:
                newval = rewrite_path(value)
                newline, n = re.subn(
                    re.escape(key) + r"(\s*:\s*)" + re.escape(value),
                    lambda m: key + m.group(1) + newval,
                    body, count=1)
                body = newline if n else body
            keystack.append((indent, key, value))
            out.append(body + ("\n" if line.endswith("\n") else ""))
            continue
        if mitem:
            item = mitem.group(2)
            item_indent = indent
            if item.startswith("#"):
                out.append(body + ("\n" if line.endswith("\n") else ""))
                continue
            owner, oidx = "", -1
            for i in range(len(keystack) - 1, -1, -1):
                if keystack[i][0] < item_indent and keystack[i][2] == "":
                    owner, oidx = keystack[i][1], i
                    break
            above  = keystack[oidx - 1][1] if oidx >= 1 else ""
            above2 = keystack[oidx - 2][1] if oidx >= 2 else ""
            head, sep, rest = item.partition(":")
            if owner == "volumes" and above2 == "services" and _is_rel(head):
                item = rewrite_path(head) + (sep + rest if sep else "")
            elif owner in ("env_file", "build") and above2 == "services" and _is_rel(item):
                item = rewrite_path(item)
            elif owner == "volumes" and above2 == "" and above == "" and _is_rel(item):
                item = rewrite_path(item)
            body = mitem.group(1) + "- " + item
        out.append(body + ("\n" if line.endswith("\n") else ""))
    return "".join(out), rewrites, warns

def extract_puid_pgid(text):
    uid = gid = None
    m = re.search(r"PUID[=:]\s*\"?(\d+)", text)
    if m: uid = m.group(1)
    m = re.search(r"PGID[=:]\s*\"?(\d+)", text)
    if m: gid = m.group(1)
    return uid or CFG["DEFAULT_UID"], gid or CFG["DEFAULT_GID"]

def validate_compose(name, canon, companions):
    d = os.path.join(DEPLOY_DIR, "_validate", name)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "docker-compose.yml"), "wb") as f:
        f.write(canon)
    for fn, data in companions.items():
        with open(os.path.join(d, fn), "wb") as f:
            f.write(data)
    rc, o, e = sh(["docker", "compose", "-p", name,
                   "-f", os.path.join(d, "docker-compose.yml"), "config", "--quiet"],
                  timeout=120)
    if rc == 0 and e:
        for l in e.splitlines():
            if "obsolete" not in l and "warning" in l.lower():
                pass  # cosmetic warnings ok
    ok = rc == 0
    return ok, (o + e).strip()[-400:]

# ---------------------------------------------------------------------------
# registration: Dockhand DB + Portainer API
# ---------------------------------------------------------------------------

def dockhand_db():
    """Return (env_id, existing {name: rowid})."""
    rc, o, e = dockhand_exec(
        "sqlite3 /app/data/db/dockhand.db "
        "\"SELECT id,name FROM environments WHERE name='%s'\" 2>/dev/null | base64 -w0"
        % CFG["DOCKHAND_ENV_NAME"].replace("'", "''"))
    env_id = None
    if rc == 0 and o:
        for ln in b64d(o).splitlines():
            if "|" in ln:
                env_id = int(ln.split("|", 1)[0])
                break
    rc, o, e = dockhand_exec(
        "sqlite3 /app/data/db/dockhand.db "
        "\"SELECT id,stack_name FROM stack_sources WHERE environment_id=%d AND source_type='internal'\" 2>/dev/null | base64 -w0"
        % (env_id or -1))
    rows = {}
    if rc == 0 and o:
        for ln in b64d(o).splitlines():
            if "|" in ln:
                rid, nm = ln.split("|", 1)
                rows[nm] = int(rid)
    return env_id, rows

def dockhand_register(name, env_id):
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would register dockhand row for %s", name)
        return True, ""
    root = dh_stack_root() + "/" + name
    path = root + "/compose.yaml"
    env = "''"
    script = (
        "sqlite3 %s "
        "\"INSERT INTO stack_sources (stack_name, environment_id, source_type, "
        "git_repository_id, git_stack_id, compose_path, env_path) "
        "VALUES ('%s', %d, 'internal', 0, 0, '%s', %s) "
        "ON CONFLICT (stack_name, environment_id) DO NOTHING\""
        % (DOCKHAND_DB, name.replace("'", "''"), env_id, path.replace("'", "''"), env)
    )
    rc, o, e = dockhand_exec(script, timeout=120)
    return rc == 0, e

def dockhand_unregister(name, env_id):
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would unregister dockhand row for %s", name)
        return True, ""
    script = (
        "sqlite3 %s "
        "\"DELETE FROM stack_sources WHERE stack_name='%s' AND environment_id=%d\""
        % (DOCKHAND_DB, name.replace("'", "''"), env_id or 0)
    )
    rc, o, e = dockhand_exec(script, timeout=120)
    return rc == 0, e

def portainer_stacks():
    """Return {name: {id, store_id, endpoint_id, project_path}} or None if API unavailable.
    Fields use Portainer's capitalized JSON: Id, Name, EndpointId, ProjectPath."""
    ok, code, payload = p_api("GET", "/api/stacks")
    if not ok:
        LOG.warning("portainer API GET /stacks failed (code=%s): %s", code, payload.get("message", payload))
        return None
    out = {}
    for s in payload if isinstance(payload, list) else []:
        sid = str(s.get("Id", ""))
        pp = s.get("ProjectPath") or ""
        m = re.search(r"/compose/(\d+)", pp)
        if m:
            sid = m.group(1)
        out[s.get("Name")] = {"id": s.get("Id"), "store_id": sid,
                              "endpoint_id": s.get("EndpointId"), "project_path": pp}
    return out

def portainer_endpoint_id():
    """Best-known Portainer endpoint id for docker-stack ops.
    Auto-detects from /api/endpoints (docker endpoint on our socket); falls back to config."""
    global _P_EP
    if _P_EP:
        return _P_EP
    try:
        ok, code, payload = p_api("GET", "/api/endpoints")
        if ok and isinstance(payload, list):
            for e in payload:
                if e.get("Type") == 1 and "docker.sock" in (e.get("URL") or ""):
                    _P_EP = int(e["Id"])
                    LOG.info("auto-detected portainer endpoint id=%s (%s)", _P_EP, e.get("Name"))
                    return _P_EP
    except Exception as ex:
        LOG.debug("endpoint auto-detect failed: %s", ex)
    try:
        return int(CFG["PORTAINER_ENDPOINT_ID"])
    except Exception:
        return 1

_P_EP = 0

def portainer_create(name, canon, endpoint_id=None):
    """Create a compose stack from file content.
    Route (v2.45.x handler.go): POST /stacks/create/{type}/{method}, type=standalone,
    method=string. Body: {Name, Env, StackFileContent}. Returns full Stack object (Id, ProjectPath).
    NOTE: the API does not accept additional/companion files here - write those into the
    store dir afterwards via write_portainer_entry()."""
    ep = int(endpoint_id or portainer_endpoint_id())
    body = {"Name": name, "Env": [], "StackFileContent": canon}
    return p_api("POST", "/api/stacks/create/standalone/string?endpointId=%d" % ep, body,
                 expect=(200, 201))

def portainer_update(stack_id, canon, endpoint_id=None):
    """Re-deploy an existing stack from its (already-updated) store file content.
    Route: PUT /stacks/{id}?endpointId=N, body {StackFileContent, Env, Prune}."""
    ep = int(endpoint_id or portainer_endpoint_id())
    body = {"StackFileContent": canon, "Env": [], "Prune": False}
    return p_api("PUT", "/api/stacks/%d?endpointId=%d" % (stack_id, ep), body,
                 expect=(200,))

def portainer_delete(stack_id, endpoint_id=None):
    """Route: DELETE /stacks/{id}?endpointId=N&external=false (204 on success)."""
    ep = int(endpoint_id or portainer_endpoint_id())
    return p_api("DELETE", "/api/stacks/%d?endpointId=%d&external=false" % (stack_id, ep),
                 expect=(200, 204))

def portainer_store_id(name, stacks_by_name):
    if not stacks_by_name:
        return None
    info = stacks_by_name.get(name)
    if not info:
        return None
    return info.get("store_id") or None

# ---------------------------------------------------------------------------
# deploy / down / data-dir migration
# ---------------------------------------------------------------------------

def deploy_dir(name, canon, companions):
    d = os.path.join(DEPLOY_DIR, name)
    if os.path.isdir(d):
        for f in os.listdir(d):
            try: os.unlink(os.path.join(d, f))
            except OSError: pass
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "docker-compose.yml"), "wb") as f:
        f.write(canon)
    for fn, data in companions.items():
        with open(os.path.join(d, fn), "wb") as f:
            f.write(data)
    return d

def compose_up(name, canon):
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would: compose -p %s up -d --force-recreate", name)
        return True, ""
    ok, err = validate_compose(name, canon, {})
    if not ok:
        return False, "compose config failed: " + err
    d = deploy_dir(name, canon, {})
    rc, o, e = sh(["docker", "compose", "-p", name, "-f",
                   os.path.join(d, "docker-compose.yml"), "up", "-d",
                   "--force-recreate"], timeout=900)
    return rc == 0, (o + "\n" + e).strip()[-600:]

def compose_down(name, canon):
    if CFG["DRY_RUN"]:
        LOG.info("[dry-run] would: compose -p %s down", name)
        return True, ""
    d = deploy_dir(name, canon, {})
    rc, o, e = sh(["docker", "compose", "-p", name, "-f",
                   os.path.join(d, "docker-compose.yml"), "down"], timeout=600)
    return rc == 0, (o + "\n" + e).strip()[-300:]

def migrate_data_dirs(id, name, rewrites):
    """Move data that a relative-path deployment already created into the
    canonical PDS_DATA_ROOT/<id>/... location.

    Candidate old locations for a relative path `./X` (or `../X`):
      * dockhand-store cwd  -> <dockhand volume host root>/stacks/<ENV>/<name>/X
      * portainer-store cwd -> <portainer volume host root>/compose/<id>/X
    (These are exactly where the docker daemon materialized the binds, since
    the resolving client's cwd IS the store dir.) If the stack was already
    deployed from a canonical (absolute) file the data is already at the
    target and nothing happens. Never overwrites an existing target; never
    deletes a non-empty source that could not be moved.
    """
    if CFG["DRY_RUN"]:
        for rel, newp in rewrites.items():
            LOG.info("[dry-run] data-migrate %s -> %s", rel, newp)
        return True, ""
    if not rewrites:
        return True, ""
    dvol_host = None
    pvol_host = None
    vol = dockhand_volume_name()
    if vol:
        dvol_host = "/var/lib/docker/volumes/%s/_data" % vol
    vol = portainer_volume_name()
    if vol:
        pvol_host = "/var/lib/docker/volumes/%s/_data" % vol
    mounts = []
    if dvol_host:
        mounts.append((dvol_host, "/dvol"))
    if pvol_host:
        mounts.append((pvol_host, "/pvol"))
    mounts.append((CFG["PDS_DATA_ROOT"], "/pdsroot"))
    lines = []
    for rel, newp in sorted(rewrites.items()):
        norm = os.path.normpath(rel)          # e.g. X or ../X  (relative)
        cands = []
        if dvol_host:
            cands.append("/dvol/stacks/%s/%s/%s" % (shlex.quote(CFG["DOCKHAND_ENV_NAME"]), shlex.quote(name), norm))
        if pvol_host:
            cands.append("/pvol/compose/%s/%s" % (id, norm))
        target = newp
        for c in cands:
            lines.append(
                """
old='%s'
new='%s'
if [ -e "$old" ] && [ ! -e "$new" ]; then
  mkdir -p "$(dirname "$new")"
  if mv "$old" "$new"; then
    chown -R ${PDS_UID}:${PDS_GID} "$new" 2>/dev/null || true
    echo "MOVED: $old -> $new"
  else
    echo "ERROR: mv failed for $old"
  fi
elif [ -e "$old" ] && [ -e "$new" ]; then
  echo "KEEP: both exist, left source in place: $old"
elif [ ! -e "$old" ]; then
  echo "SKIP: no data at $old"
else
  echo "NOTE: unexpected state at $old / $new"
fi
""" % (c, target))
    rc, o, e = helper("true\n" + "\n".join(lines), volumes=tuple(mounts), timeout=1800)
    return rc == 0, (o + e).strip()

def safe_migrate(old, new):
    """Root helper: mv old->new if old exists and new doesn't. Returns rc."""
    script = """
old='%s'
new='%s'
if [ -e "$old" ]; then
  if [ -e "$new" ]; then
    echo "KEEP: target exists, source left in place: $old"
    exit 0
  fi
  mkdir -p "$(dirname "$new")"
  mv "$old" "$new" && echo "MOVED: $old -> $new" || exit 1
  chown -R ${PDS_UID}:${PDS_GID} "$new" 2>/dev/null || true
else
  echo "SKIP: $old not present"
fi
""" % (old, new)
    rc, o, e = helper(script, timeout=600)
    return rc == 0, (o + e).strip()

def inspect_stack_bindings(name):
    """Return {service: [host bind sources]} for a compose project (running or not)."""
    rc, o, _ = docker(["ps", "-aq", "-f",
                       "label=com.docker.compose.project=%s" % name], timeout=60)
    ids = o.split()
    out = {}
    for cid in ids:
        rc, o, _ = docker(["inspect", cid, "--format",
            "{{index .Config.Labels \"com.docker.compose.service\"}}|{{range .Mounts}}{{if eq .Type \"bind\"}}{{.Source}};{{end}}{{end}}"], timeout=60)
        if rc != 0:
            continue
        svc, _, mounts = o.partition("|")
        srcs = [s for s in mounts.split(";") if s]
        if srcs:
            out[svc.strip()] = srcs
    return out

# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

def event(kind, name, detail=""):
    try:
        with open(EVENT_LOG, "a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "kind": kind, "stack": name, "detail": detail}) + "\n")
        # keep small
        if os.path.getsize(EVENT_LOG) > 2_000_000:
            with open(EVENT_LOG) as f:
                tail = f.readlines()[-500:]
            with open(EVENT_LOG, "w") as f:
                f.writelines(tail)
    except Exception:
        pass

def emit_canon(name, id, canon, companions):
    cdir = os.path.join(CANON_DIR, name)
    if os.path.isdir(cdir):
        shutil.rmtree(cdir, ignore_errors=True)
    os.makedirs(cdir, exist_ok=True)
    with open(os.path.join(cdir, "compose.yaml"), "wb") as f:
        f.write(canon)
    for fn, data in companions.items():
        with open(os.path.join(cdir, fn), "wb") as f:
            f.write(data)

def project_running(name):
    """True if any container for compose project `name` is currently running."""
    rc, o, _ = docker(["ps", "-q", "-f",
                       "label=com.docker.compose.project=%s" % name], timeout=60)
    return bool(o.strip())

def sync_stack(name, origin, store_id, raw, companions, deep=False, no_deploy=False, st=None):
    """Canonicalize + mirror a stack that exists (new or changed) in `origin`
    (portainer|dockhand). Idempotent. Pass the shared outer `st` so commits are
    not clobbered by a later outer save."""
    if st is None:
        st = load_state()
    ss = stack_state(st, name)
    prev_hash = ss.get("hash")
    if ss.get("store_id") != store_id and ss.get("store_id") is not None and not deep:
        # portainer id changed? rebase data root only if data dir exists
        pass
    ss["store_id"] = store_id
    ss["name"] = name
    # --- canonical form -----------------------------------------------------
    canon_text, rewrites, warns = transform_compose(raw.decode("utf-8", "replace"), name)
    for w in warns:
        LOG.warning("%s: %s", name, w)
    canon = canon_text.encode()
    # data migration for relative paths (only when rewrites happened)
    if rewrites and not deep:
        ok, detail = migrate_data_dirs(store_id, name, rewrites)
        if not ok:
            LOG.error("%s: data migration FAILED (%s) - aborting sync for safety", name, detail)
            event("error", name, "migrate: " + detail[:200])
            return False
        if detail:
            LOG.info("%s data-migrate: %s", name, detail)
    # --- validate ------------------------------------------------------------
    ok, err = validate_compose(name, canon, companions)
    if not ok:
        LOG.error("%s: canonical compose invalid (%s) - refusing to sync", name, err)
        event("error", name, "invalid: " + err[:300])
        return False
    emit_canon(name, store_id, canon, companions)
    new_hash = hashlib.sha256(canon).hexdigest()
    # --- write to other store(s) + register ------------------------------------
    if origin == "dockhand":
        if store_id:
            # existing Portainer record: overwrite its store file directly (no API needed)
            ok1, e1 = write_portainer_entry(store_id, "docker-compose.yml", canon)
            ok2, e2 = True, ""
            for fn, data in companions.items():
                okc, ec = write_portainer_entry(store_id, fn, data)
                ok2 = ok2 and okc
                e2 += ec
            if not (ok1 and ok2):
                LOG.error("%s: failed writing to portainer store: %s", name, (e1 + e2)[:300])
                event("error", name, "portainer store write failed")
                return False
            LOG.info("%s: portainer store file updated (id=%s)", name, store_id)
        else:
            # brand-new Dockhand stack: Portainer API creates the record + store file
            ok, code, payload = portainer_create(name, canon.decode())
            if ok:
                new_id = payload.get("Id") if isinstance(payload, dict) else None
                if new_id is not None:
                    store_id = str(new_id)
                    ss["store_id"] = store_id
                    LOG.info("%s: created in Portainer (stack id=%s, path=%s)",
                             name, new_id, payload.get("ProjectPath", ""))
                    # API create does not carry companion files -> write them now
                    for fn, data in sorted(companions.items()):
                        if fn == "docker-compose.yml":
                            continue
                        okc, ec = write_portainer_entry(store_id, fn, data)
                        if not okc:
                            LOG.error("%s: created but companion %s write failed: %s", name, fn, ec[:150])
                else:
                    LOG.warning("%s: Portainer created stack but no Id in response: %s", name, payload)
            else:
                # do NOT commit the hash -> next round re-fires and retries the create
                now_ts = time.time()
                if now_ts - ss.get("last_defer_warn", 0) > 900:
                    ss["last_defer_warn"] = now_ts
                    LOG.warning("%s: Portainer-side registration deferred (code=%s %s) - will retry next round",
                                name, code, str(payload.get("message", payload))[:160])
                    event("deferred", name, "portainer create: %s" % str(payload.get("message", payload))[:160])
                ss["canonical_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                emit_canon(name, store_id, canon, companions)
                save_state(st)
                # still converge the containers locally so the stack RUNS meanwhile
                if CFG["DEPLOY"] and not no_deploy and not project_running(name):
                    ok, detail = compose_up(name, canon)
                    if not ok:
                        LOG.error("%s: compose up failed: %s", name, detail)
                return False
    else:
        # origin == portainer: write the dockhand store file directly + register row (no API needed)
        entries = {"compose.yaml": canon}
        for fn, data in companions.items():
            if fn != "docker-compose.yml":
                entries[fn] = data
        ok, err = write_dh_stack(name, entries)
        if not ok:
            LOG.error("%s: failed writing to dockhand store: %s", name, err[:300])
            event("error", name, "dockhand store write failed")
            return False
        ok, err = dockhand_register(name, _dh_env_id()[0])
        if not ok:
            LOG.warning("%s: dockhand DB registration failed: %s", name, err)
    # --- (optional) nudge Portainer to redeploy via API (best-effort) ----------
    if origin == "portainer":
        stacks = portainer_stacks()
        if stacks:
            info = stacks.get(name)
            if info and info.get("id"):
                ok, code, payload = portainer_update(int(info["id"]), canon.decode())
                if not ok:
                    LOG.warning("%s: portainer API update failed (code=%s %s); will converge locally",
                                name, code, str(payload.get("message", payload))[:120])
            else:
                LOG.info("%s: not yet a Portainer record (API); dockhand store written + will converge locally", name)
        else:
            LOG.info("%s: Portainer API unavailable; dockhand store written + will converge locally", name)
    if origin == "portainer" and store_id is None:
        stacks = portainer_stacks()
        if stacks is not None and name in stacks and stacks[name].get("store_id"):
            store_id = stacks[name]["store_id"]
            ss["store_id"] = store_id
        # --- converge containers --------------------------------------------------
    if CFG["DEPLOY"] and not no_deploy:
        if project_running(name):
            LOG.info("%s: containers already running - leaving as-is (files canonicalized; use a redeploy to converge)", name)
            event("skipped-running", name, "left running containers untouched")
        else:
            ok, detail = compose_up(name, canon)
            if not ok:
                LOG.error("%s: compose up failed: %s", name, detail)
                event("error", name, "compose up: " + detail[:300])
                return False
            LOG.info("%s: converged (compose up) %s", name, detail[:160])
        # --- commit ----------------------------------------------------------------
    ss["hash"] = new_hash
    ss["synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ss["origin"] = origin
    save_state(st)
    event("synced", name, "origin=%s id=%s %s" % (origin, store_id, "deep" if deep else ""))
    LOG.info("%s: SYNCED (origin=%s, id=%s)", name, origin, store_id)
    return True

_DH_ENV = {}
def _dh_env_id():
    if not _DH_ENV:
        env_id, _rows = dockhand_db()
        _DH_ENV["id"] = env_id
    return _DH_ENV["id"], {}

def handle_delete(name, st):
    """A stack known to us vanished from its origin store. Mirror the deletion."""
    LOG.warning("%s: DELETED in origin store - mirroring", name)
    event("deleting", name, "")
    ss = st["stacks"].get(name, {})
    store_id = ss.get("store_id")
    canon = None
    cdir = os.path.join(CANON_DIR, name)
    if os.path.isfile(os.path.join(cdir, "compose.yaml")):
        with open(os.path.join(cdir, "compose.yaml"), "rb") as f:
            canon = f.read()
    # 1) remove from the OTHER store (file)
    if ss.get("origin") == "dockhand" and store_id:
        ok, e = delete_portainer_store(store_id)
        LOG.info("%s: portainer store dir removed: %s", name, "ok" if ok else e)
    else:
        ok, e = delete_dh_store(name)
        LOG.info("%s: dockhand store dir removed: %s", name, "ok" if ok else e)
    # 2) remove registration
    if ss.get("origin") == "dockhand":
        env_id, _ = dockhand_db()
        ok, e = dockhand_unregister(name, env_id)
        LOG.info("%s: dockhand DB row removed: %s", name, "ok" if ok else e)
    else:
        stacks = portainer_stacks()
        if stacks and name in stacks:
            info = stacks[name]
            ok, code, payload = portainer_delete(int(info["id"]), info.get("endpoint_id"))
            LOG.info("%s: portainer record deleted: code=%s", name, code)
    # 3) take down containers
    if canon:
        ok, detail = compose_down(name, canon)
        LOG.info("%s: compose down: %s", name, "ok" if ok else detail)
    # NOTE: data dir under PDS_DATA_ROOT/<id>/ is intentionally KEPT.
    st["stacks"].pop(name, None)
    save_state(st)
    event("deleted", name, "")
    return True

def reconcile(name, st):
    """Deep: force both stores to the canonical bytes, but only if they drift."""
    cdir = os.path.join(CANON_DIR, name)
    cpath = os.path.join(cdir, "compose.yaml")
    if not os.path.isfile(cpath):
        return
    with open(cpath, "rb") as f:
        canon = f.read()
    ss = st["stacks"][name]
    store_id = ss.get("store_id")
    # portainer side
    if store_id:
        cur = read_portainer_file(store_id) if os.path.isdir(P_STAGE) else None
        if cur is None:
            rc, e = fetch_portainer_store()
            cur = read_portainer_file(store_id) if rc == 0 else None
        if cur != canon:
            write_portainer_entry(store_id, "docker-compose.yml", canon)
            LOG.info("%s: deep-reconciled PORTAINER store (was drifted)", name)
    # dockhand side
    files, _ = fetch_dh_stack(name)
    if files and files.get("compose.yaml") != canon:
        write_dh_stack(name, {"compose.yaml": canon})
        LOG.info("%s: deep-reconciled DOCKHAND store (was drifted)", name)



# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def canon_hash_of(raw_text, name):
    canon, _, _ = transform_compose(raw_text, name)
    return hashlib.sha256(canon.encode()).hexdigest()

def scan_and_sync():
    st = load_state()
    first_import = "imported_at" not in st
    # ---- fetch portainer store ----
    rc, e = fetch_portainer_store()
    p_raw = {}          # store_id -> bytes
    if rc == 0:
        for id in portainer_store_ids():
            b = read_portainer_file(id)
            if b is not None:
                p_raw[id] = b
    else:
        LOG.error("cannot fetch portainer store: %s", e[:200])
    # ---- fetch dockhand store ----
    dh_names, dh_err = dh_listing()
    d_raw = {}          # name -> (bytes, companions)
    if dh_names is None:
        LOG.error("cannot list dockhand store: %s", dh_err[:200])
    else:
        for nm in dh_names:
            files, _ = fetch_dh_stack(nm)
            if not files or "compose.yaml" not in files:
                continue
            comp = {k: v for k, v in files.items() if k != "compose.yaml"}
            d_raw[nm] = (files["compose.yaml"], comp)
    # ---- portainer API (names + ids) ----
    pstacks = portainer_stacks() if CFG["PORTAINER_API_KEY"] else None
    if CFG["PORTAINER_API_KEY"] and pstacks is None:
        LOG.warning("Portainer API unavailable this round; using dockhand view only")
    # ---- per-stack decisions ----
    known = dict(st.get("stacks", {}))
    all_names = set()
    if pstacks:
        all_names.update(n for n, i in pstacks.items() if i.get("store_id") in p_raw)
    all_names.update(d_raw.keys())

    for nm in sorted(all_names):
        if nm in CFG["EXCLUDE"]:
            continue
        info = (pstacks or {}).get(nm, {}) if pstacks else {}
        sid = info.get("store_id") if info else None
        p_ok = bool(sid) and sid in p_raw
        d_ok = nm in d_raw
        s = known.get(nm)
        st_hash = s.get("hash") if s else None
        if s and s.get("store_id") and not sid:
            sid = s.get("store_id")
        # canonical hashes of each side's current content
        ph = canon_hash_of(p_raw[sid].decode("utf-8", "replace"), nm) if p_ok else None
        dh = canon_hash_of(d_raw[nm][0].decode("utf-8", "replace"), nm) if d_ok else None

        if p_ok and d_ok:
            if ph == dh and ph == st_hash:
                if s:  # fully in sync
                    continue
                # brand-new-in-both (import): record, no deploy
                s2 = stack_state(st, nm)
                s2["hash"], s2["store_id"], s2["name"] = ph, sid, nm
                s2["synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                s2["origin"] = "import"
                LOG.info("%s: imported (present in both stores, in sync)", nm)
                continue
            # content differs -> determine origin (which side holds the change)
            if st_hash and ph != st_hash and dh != st_hash and ph != dh:
                LOG.error("%s: CONFLICT - both stores changed and differ; adopting dockhand side", nm)
                event("conflict", nm, "both stores differ; adopting dockhand")
            if ph != dh and ph == st_hash:
                origin = "dockhand"
            elif dh != st_hash:
                origin = "dockhand"
            else:
                origin = "portainer"
            raw = d_raw[nm][0] if origin == "dockhand" else p_raw[sid]
            comp = d_raw[nm][1] if origin == "dockhand" else {}
            if origin == "portainer" and sid is None:
                # brand new in both but only visible via dockhand? shouldn't happen
                continue
            sync_stack(nm, origin, sid, raw, comp, first_import, no_deploy=first_import, st=st)
        elif p_ok:
            # portainer store file present, dockhand store file ABSENT
            if s and s.get("hash") == ph:
                # was in sync; dockhand copy vanished. Disambiguate using the
                # Dockhand DB row (the source of truth for "Dockhand deleted it").
                env_id, row = dockhand_db()   # -> (env_id, {name: rowid})
                dh_gone = (env_id is not None) and (nm not in row)
                if dh_gone:
                    # dockhand store file AND db row both gone -> the user deleted
                    # this stack in Dockhand. Mirror the deletion on the Portainer
                    # side (API delete drops the record + store file + containers).
                    # NOTE: this branch is reached only when the dockhand FILE is
                    # absent (a strong per-stack signal), so a transient empty db
                    # read cannot mass-delete stacks that still have files.
                    LOG.warning("%s: deleted in Dockhand (store+row gone) - mirroring delete to Portainer", nm)
                    pstack_api_id = (info or {}).get("id") or (st.get("stacks", {}).get(nm, {}) or {}).get("stack_id")
                    if not pstack_api_id:
                        pstack_api_id = sid   # fall back to store id (usually equal)
                    ok, code, payload = portainer_delete(int(pstack_api_id))
                    if ok:
                        LOG.info("%s: portainer record deleted via API (code=%s)", nm, code)
                    else:
                        LOG.warning("%s: portainer API delete failed (code=%s); stopping containers + removing store locally", nm, code)
                        cpath = os.path.join(CANON_DIR, nm, "compose.yaml")
                        if os.path.isfile(cpath):
                            with open(cpath, "rb") as f:
                                compose_down(nm, f.read())
                        delete_portainer_store(sid)
                    st["stacks"].pop(nm, None)
                    save_state(st)
                    event("mirror-delete", nm, "portainer record+store removed after dockhand delete")
                    continue
                # db row still present (file-only loss) or db unreachable: restore
                LOG.info("%s: dockhand file missing but db row present - restoring from portainer origin", nm)
            # new, changed, or restore
            sync_stack(nm, "portainer", sid, p_raw[sid], {}, first_import, no_deploy=first_import, st=st)
        else:
            # dockhand only (portainer store file absent)
            if s and s.get("hash") == dh:
                # was in sync; portainer copy vanished. Disambiguate:
                stacks = portainer_stacks()
                if stacks is not None and nm not in stacks:
                    # Portainer record ALSO gone -> genuine deletion -> mirror on dockhand side
                    LOG.warning("%s: deleted in Portainer (record+file gone) - removing dockhand store + row", nm)
                    ok, ee = delete_dh_store(nm)
                    env_id, _ = dockhand_db()
                    dockhand_unregister(nm, env_id)
                    cpath = os.path.join(CANON_DIR, nm, "compose.yaml")
                    if os.path.isfile(cpath):
                        with open(cpath, "rb") as f:
                            ok, detail = compose_down(nm, f.read())
                        LOG.info("%s: compose down after remote delete: %s", nm, "ok" if ok else detail)
                    st["stacks"].pop(nm, None)
                    save_state(st)
                    event("mirror-delete", nm, "dockhand store+row removed after remote delete")
                    continue
                # partial / API unknown: restore the portainer side
                LOG.info("%s: portainer copy missing but stack still registered - restoring from dockhand origin", nm)
            # new, changed, or restore
            sync_stack(nm, "dockhand", sid, d_raw[nm][0], d_raw[nm][1], first_import, no_deploy=first_import, st=st)

    # ---- deletions: known but in neither store ----
    for nm in list(known.keys()):
        if nm in CFG["EXCLUDE"] or nm in all_names:
            continue
        handle_delete(nm, st)

    if first_import:
        st["imported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        LOG.info("initial import pass complete (%d stacks tracked)", len(st.get("stacks", {})))
    # ---- periodic deep reconcile: force store bytes back to canonical ----
    if time.time() - st.get("last_deep", 0) > CFG["DEEP_INTERVAL"]:
        for nm in list(st.get("stacks", {}).keys()):
            if nm in CFG["EXCLUDE"]:
                continue
            try:
                reconcile(nm, st)
            except Exception as ex:
                LOG.error("%s: reconcile error: %s", nm, ex)
        st["last_deep"] = time.time()
    save_state(st)

def write_liveness():
    try:
        with open(LIVENESS, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass

def wait_for_trigger(timeout):
    """Sleep in small slices, returning early if the trigger file is new."""
    end = time.time() + timeout
    last = 0
    while time.time() < end:
        write_liveness()
        try:
            m = os.path.getmtime(TRIGGER)
            if m > last:
                last = m
                LOG.debug("trigger touched")
                return
        except OSError:
            pass
        time.sleep(min(2, max(0.2, end - time.time())))

def main():
    os.makedirs(os.path.join(WORK, "state"), exist_ok=True)
    os.makedirs(os.path.join(WORK, "logs"), exist_ok=True)
    os.makedirs(CANON_DIR, exist_ok=True)
    logging.basicConfig(
        level=os.environ.get("PDS_LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)])
    # single-instance lock
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.error("another instance holds the lock; exiting")
        sys.exit(1)
    lock.write(str(os.getpid()))
    lock.flush()
    LOG.info("pds-sync starting (poll=%ss deep=%ss deploy=%s dry_run=%s)",
             CFG["POLL_INTERVAL"], CFG["DEEP_INTERVAL"], CFG["DEPLOY"], CFG["DRY_RUN"])
    if CFG["PORTAINER_API_KEY"]:
        ok, code, payload = p_api("GET", "/api/status")
        LOG.info("portainer API reachable: %s (code=%s)", ok, code)
    else:
        LOG.warning("no PORTAINER_API_KEY: Portainer-side new-stack registration/deploy will be deferred until configured")
    st = load_state()
    st.setdefault("booted_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    save_state(st)
    while True:
        try:
            scan_and_sync()
        except Exception as ex:
            LOG.exception("scan_and_sync failed: %s", ex)
            event("error", "*", "loop: " + str(ex)[:200])
        write_liveness()
        if os.environ.get("PDS_ONCE") == "1":
            LOG.info("PDS_ONCE=1: one cycle complete, exiting")
            break
        wait_for_trigger(CFG["POLL_INTERVAL"])

if __name__ == "__main__":
    main()
