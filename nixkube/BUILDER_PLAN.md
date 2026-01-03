# Builder Pods Implementation Plan

## Overview

Add dedicated builder pods to nix-csi for handling builds, moving build workload out of privileged CSI pods.

**Goal**: CSI pods run with `--max-jobs 0`, delegating all builds to unprivileged builder pods via SSH.

## Architecture

```
Pod requests volume
  → CSI pod (privileged, --max-jobs 0)
  → Query k8s API for builder pods
  → nix build --builders "ssh://builder-1, ssh://builder-2" --builders-use-substitutes
  → Builder pod (unprivileged, emptyDir storage)
      → Builds derivation
      → ValidPaths monitoring service detects new path
      → Runs cache push script (push to cache/attic/cachix/s3)
  → CSI fetches from builder via ssh-ng://
  → CSI mounts path
```

## Stage 1: Get Builders Working

### 1.1 Create Builder Environment

**File**: `environments/builder/default.nix`

**Services** (dinit):
- `shared-setup` - SSH keys, Nix config (reuse from shared/)
- `openssh` - Accept SSH connections from CSI pods
- `nix-daemon` - May not be needed (SSH spawns on-demand), but include for safety
- `validpaths-monitor` - Watch ValidPaths table, trigger cache push script
- `nix-timegc` - Daily GC (default: 86400s / 24h)
- `builder` - Umbrella service

**Nix config** (`nix.conf`):
- `max-jobs = auto` (or configurable)
- Substituters: use cache pod if enabled
- `trusted-users = root`
- Other settings from shared config

**ValidPaths Monitoring Service**:
- Polls `ValidPaths` table for new IDs (or use inotify on DB?)
- When new path detected: run cache push script
- Script gets `$storePath` as argument
- Exponential backoff on failures
- Log all pushes

### 1.2 Add Builder Options

**File**: `kubenix/options.nix`

```nix
nix-csi.builders = {
  enable = lib.mkEnableOption "builder pods";

  replicas = lib.mkOption {
    description = "Number of builder pod replicas";
    type = lib.types.ints.positive;
    default = 1;
  };

  emptyDirSize = lib.mkOption {
    description = "Size limit for builder emptyDir storage";
    type = lib.types.str;
    default = "50Gi";
  };

  cachePushScript = lib.mkOption {
    description = ''
      Script to run when new store paths are built.
      Receives store path as $1.
      Default: push to in-cluster cache if enabled.
    '';
    type = lib.types.lines;
    default = '''
      #!/bin/bash
      storePath="$1"
      # Push to in-cluster cache if enabled
      if [ -n "$CACHE_ENABLED" ]; then
        nix copy --to ssh-ng://nix@nix-cache "$storePath"
      fi
    ''';
  };

  resources = lib.mkOption {
    description = "Resource requests/limits for builder pods";
    type = lib.types.attrs;
    default = {
      requests = {
        cpu = "1";
        memory = "2Gi";
        ephemeral-storage = "50Gi";
      };
      limits = {
        memory = "4Gi";
        ephemeral-storage = "50Gi";
      };
    };
  };

  nixBuildTimeout = lib.mkOption {
    description = "Timeout for builds on builder pods (seconds)";
    type = lib.types.ints.positive;
    default = 3600; # 1 hour
  };
};

# Internal options
nix-csi.builderPackage = lib.mkOption {
  type = lib.types.attrsOf lib.types.package;
  internal = true;
};
```

**Build builder packages in config section** (similar to cache/node):
```nix
builderPackage = {
  x86_64-linux = x86Pkgs.nix-csi-builder-env;
  aarch64-linux = armPkgs.nix-csi-builder-env;
};
```

### 1.3 Create Builder Deployment

**File**: `kubenix/builder.nix`

**Resources**:
- `Deployment.nix-builder`
  - replicas = cfg.builders.replicas
  - emptyDir volume for /nix
  - initContainer: copy builder environment to emptyDir (same pattern as cache)
  - main container: run dinit builder service
  - resources: cfg.builders.resources

- `Service.nix-builders` (headless)
  - clusterIP = None
  - selector: builder pod labels
  - Used for DNS discovery of individual builder pods

- `ConfigMap.nix-builder`
  - nix.conf
  - cache-push.sh (from cfg.builders.cachePushScript)

**Labels**: `app.kubernetes.io/name=builder`, `app.kubernetes.io/part-of=nix-csi`

**Environment variables**:
- `CACHE_ENABLED` - whether cache pod is enabled
- `NIX_BUILD_TIMEOUT` - cfg.builders.nixBuildTimeout
- SSH keys (same as cache/nodes)

**Volumes**:
- emptyDir for /nix (size limit: cfg.builders.emptyDirSize)
- ConfigMap for /etc/nix
- Secret for SSH keys

### 1.4 CSI Pod Discovery

**File**: `python/nix_csi/service.py`

**Add builder discovery function**:
```python
async def get_builder_uris():
    """Query k8s API for builder pods, return list of SSH URIs."""
    if not BUILDERS_ENABLED:
        return []

    # Use kr8s to query pods with label selector
    # app.kubernetes.io/name=builder
    pods = await kr8s.asyncio.get("pods",
                                   namespace=NAMESPACE,
                                   label_selector="app.kubernetes.io/name=builder")

    # Build SSH URIs: ssh://pod-name.nix-builders.namespace.svc.cluster.local
    uris = []
    for pod in pods:
        if pod.status.phase == "Running":
            pod_name = pod.metadata.name
            uri = f"ssh://nix@{pod_name}.{BUILDERS_SERVICE}.{NAMESPACE}.svc.cluster.local"
            uris.append(uri)

    return uris
```

**Environment variables for service.py**:
- `BUILDERS_ENABLED` - from kubenix
- `BUILDERS_SERVICE` - "nix-builders"
- `NAMESPACE` - from fieldRef

### 1.5 Modify CSI Build Logic

**File**: `python/nix_csi/service.py` (NodePublishVolume)

**Changes**:
1. Get builder URIs at start of publish
2. If builders exist:
   - Set `--max-jobs 0`
   - Add `--builders <uri1> <uri2> ...`
   - Add `--builders-use-substitutes`
3. Remove `copyToCache` call when builders enabled (builders handle caching)

**Example**:
```python
async def NodePublishVolume(self, stream):
    # ... existing code ...

    # Get builder URIs
    builder_uris = await get_builder_uris()

    # Build command
    build_args = ["nix", "build", "--out-link", gcPath]

    if builder_uris:
        # Delegate to builders
        build_args.extend(["--max-jobs", "0"])
        build_args.extend(["--builders", " ".join(builder_uris)])
        build_args.append("--builders-use-substitutes")

    build_args.append(packagePath)

    await try_captured(*build_args, timeout=NIX_BUILD_TIMEOUT)

    # Don't call copyToCache if builders enabled
    # (builders handle cache pushing via validpaths-monitor)
    if not builder_uris:
        await copyToCache(packagePath)
```

### 1.6 ValidPaths Monitoring Service

**File**: `environments/builder/validpaths-monitor.nix` (or inline in default.nix)

**Implementation**: Python script (or bash with sqlite3)

**Logic**:
```python
import sqlite3
import subprocess
import time

DB = "/nix/var/nix/db/db.sqlite"
SCRIPT = "/etc/nix-csi/cache-push.sh"
SEEN_IDS_FILE = "/nix/var/nix-csi/seen-validpath-ids"

# Load seen IDs
seen = set()
if Path(SEEN_IDS_FILE).exists():
    seen = set(int(x) for x in Path(SEEN_IDS_FILE).read_text().splitlines())

while True:
    conn = sqlite3.connect(DB)
    cursor = conn.execute("SELECT id, path FROM ValidPaths WHERE id > ?", (max(seen or [0]),))

    for row in cursor:
        id, path = row
        if id not in seen:
            # New path! Run cache push script
            logger.info(f"New path detected: {path}")
            try:
                subprocess.run([SCRIPT, path], check=True)
                seen.add(id)
            except Exception as e:
                logger.error(f"Cache push failed for {path}: {e}")
                # Retry logic here

    conn.close()

    # Save seen IDs
    Path(SEEN_IDS_FILE).write_text("\n".join(str(x) for x in seen))

    time.sleep(10)  # Poll every 10 seconds
```

**Dinit service**:
- Type: process
- Depends: nix-daemon (maybe), shared-setup
- Restart: always

### 1.7 Testing Checklist

- [ ] Builder Deployment creates pods successfully
- [ ] Builder pods have emptyDir mounted at /nix
- [ ] InitContainer copies builder environment to emptyDir
- [ ] openssh is running and accepting connections
- [ ] CSI pod can query k8s API for builder pods
- [ ] CSI pod can build with `--builders` flag
- [ ] Build happens on builder pod (check logs)
- [ ] ValidPaths monitoring service detects new paths
- [ ] Cache push script runs successfully
- [ ] CSI pod can fetch built path from builder
- [ ] Volume mount succeeds with path built by builder

## Stage 2: Controller Pod (Future)

**Scope**:
- Move non-cache functionality from cache StatefulSet to new controller Deployment
- Controller handles:
  - Watching k8s for nodes/builders (maintaining machines file)
  - SSH jump-box for external builds
  - Proxying external builder connections
  - Serving machine config for external builders
- Cache StatefulSet becomes pure cache (just nix-daemon + storage)
- External building support

**Not implementing in Stage 1** - focus on CSI → builder flow first.

## Questions / Decisions

1. **ValidPaths monitoring**: Poll SQLite vs inotify? → Start with polling (simple)
2. **Cache push retries**: Built into monitor or in script? → Built into monitor (reusable)
3. **Builder nix-daemon**: Needed or SSH spawns on-demand? → Include for safety, investigate later
4. **Builder discovery**: Cache on CSI pod or query every time? → Query every time (fresh list)
5. **emptyDir GC**: Daily nix-timegc sufficient? → Yes for now, monitor in production

## Implementation Order

1. Create `environments/builder/default.nix` (base structure)
2. Add `nix-csi.builders` options to `kubenix/options.nix`
3. Create `kubenix/builder.nix` (Deployment + Service)
4. Build `builderPackage` in options.nix config
5. Add builder discovery to `python/nix_csi/service.py`
6. Modify CSI build logic to use `--builders`
7. Implement validpaths-monitor service
8. Test end-to-end: CSI → builder → cache → mount

## Success Criteria

- CSI pods run with `--max-jobs 0` when builders exist
- Builds execute on builder pods, not CSI pods
- Built paths automatically pushed to cache
- CSI pods can mount paths built by builders
- Builder pods can be scaled (manually for now)
- Logs show clear build → push → fetch flow
