# NRI v1: Nix Store Injection via Node Resource Interface

## Architecture Overview

Use NRI's `CreateContainer` hook to inject mounts that deliver built Nix stores into containers, leveraging the existing `/var/lib/nix-csi` hostPath mount for storage.

### Component Layout

```
Host filesystem:
  /var/lib/nix-csi/
    ├─ zmq-pub.sock                      ← ZeroMQ PUB socket (broadcasts build completion)
    ├─ zmq-query.sock                    ← ZeroMQ REP socket (answers "is build done?" queries)
    ├─ nix/var/volumes/
    │  └─ {container-id}/                ← build output directory per container
    │     └─ nix/store/...               ← hardlinked Nix paths
    └─ /opt/nri/                         ← mounted into DaemonSet
       └─ wait                           ← Rust binary for createRuntime hook

nix-nri DaemonSet pod:
  ├─ volumeMounts:
  │  ├─ /var/lib/nix-csi (hostPath bind)
  │  ├─ /opt/nri (hostPath bind)
  │  └─ /run/containerd/containerd.sock
  └─ nix-nri Python process:
     ├─ NRI plugin server (communicates with containerd)
     ├─ ZeroMQ PUB socket at /var/lib/nix-csi/zmq-pub.sock (broadcast completion)
     ├─ ZeroMQ REP socket at /var/lib/nix-csi/zmq-query.sock (query handler)
     ├─ Build status cache (container_id → {status, timestamp})
     └─ Background build tasks (Python async)

User container:
  └─ Mounts injected by NRI CreateContainer:
     ├─ /nix (bind mount, read-only, source: /var/lib/nix-csi/nix/var/volumes/{container-id}/nix)
     └─ (Optional) overlay mount for RW semantics
```

### Lifecycle

1. **NRI CreateContainer hook (≤2s timeout)**
   - Filter by pod annotation `nix-nri/test: "true"` (Phase 1), later expand to `nix-nri/store-paths`
   - Extract container ID from request
   - Create `/var/lib/nix-csi/nix/var/volumes/{container-id}` directory on host
   - Spawn background build task (returns immediately)
   - Inject mounts into `ContainerAdjustment.Mounts`:
     - Source: `/var/lib/nix-csi/nix/var/volumes/{container-id}/nix`
     - Destination: `/nix`
     - Options: `["ro", "bind"]` (read-only, hardlinks only)
   - Inject createRuntime hook into `ContainerAdjustment.Hooks.createRuntime`:
     - Path: `/opt/nri/wait`
     - Args: `["--container-id={container-id}"]`
   - Return (NRI completes within timeout)

2. **Background build task (async, in nix-nri pod)**
   - Receives build request for `{container-id}`
   - Builds Nix paths to `/var/lib/nix-csi/nix/var/volumes/{container-id}/nix/store/...`
   - Updates build status cache: `{container_id: {status: "done", timestamp: ...}}`
   - Publishes via ZeroMQ PUB socket: `{"container_id": "{container-id}", "status": "done"}`

3. **CRI container creation + createRuntime hook (host namespace)**
   - `/opt/nri/wait --container-id={container-id}`:
     - **Query phase**: Connect to REP socket at `/var/lib/nix-csi/zmq-query.sock`
       - Send: `{"container_id": "{container-id}"}`
       - Receive: `{"status": "done"}` or `{"status": "pending"}`
       - If status is "done", exit immediately (build already finished)
     - **Wait phase** (only if build pending): Subscribe to PUB socket at `/var/lib/nix-csi/zmq-pub.sock`
       - Wait for `{"container_id": "{container-id}", "status": "done"}` message
       - Unsubscribe and exit
   - Either way, exit allows container to proceed

4. **Container starts**
   - `/nix` is already mounted with hardlinked store paths
   - User process sees `/nix/store/...` ready to use

### Communication: ZeroMQ PUB-SUB

- **Socket**: Unix IPC at `/var/lib/nix-csi/zmq.sock`
- **Mode**: PUB (nix-nri) → SUB (wait binary instances)
- **Message format** (JSON):
  ```json
  {"container_id": "abc123...", "status": "done"}
  ```
- **Deduplication**: Multiple containers requesting same Nix path share output (Nix content addressing handles this)

### Storage Layout

```
/var/lib/nix-csi/nix/var/volumes/
├─ container-id-1/
│  └─ nix/
│     └─ store/
│        ├─ 0000-dep1/
│        ├─ 1111-dep2/
│        └─ 2222-main/
├─ container-id-2/
│  └─ nix/
│     └─ store/  (hardlinks to container-id-1's files)
└─ ...
```

All files within each container's directory are hardlinked, leveraging Nix's content-addressable store for natural deduplication.

### Constraints & Design Decisions

- **Read-only mounts**: Bind mounts with hardlinks prevent accidental modifications
- **OverlayFS not used initially**: Overlay's startup scan doesn't see files added after mount; bind mounts see new files immediately
- **No host namespace dependency**: All tooling is self-contained (Rust binary, Python)
- **No hook cleanup needed**: OCI Runtime cleans up mounts when container terminates
- **2-second timeout workaround**: Build spawned asynchronously; hook waits outside NRI timeout constraint

---

## Implementation Phases

### Phase 1: NRI Mount Injection (Testing) ✅ COMPLETE

**Goal**: Verify NRI protocol and mount injection mechanism.

**Completed**:
- ✅ Filter by `nix-nri/test` annotation
- ✅ Volume directory creation on host
- ✅ Mount injection via ContainerAdjustment
- ✅ Test file creation with pod/container metadata
- ✅ Proper cleanup in StopContainer
- ✅ Pod-side vs host-side path handling

### Phase 2: Full Build System - COMPLETE & TESTED ✅

**Completed**:
1. ✅ Implemented ZeroMQ server (PUB/REP sockets) in nri-nri plugin
2. ✅ Created nri-wait Python application (query + subscribe pattern)
3. ✅ Injected OCI createRuntime hooks with proper environment variables
4. ✅ Implemented async background LARP build task scheduling
5. ✅ Socket communication verified (REP query, PUB broadcast)
6. ✅ End-to-end testing with real Kubernetes cluster
7. ✅ Hook chroot isolation working correctly
8. ✅ **Mount timing validated**: OCI runtime applies mounts immediately during container creation (before hooks run)
9. ✅ **Backfilling architecture verified**: Pre-create empty mount directories in CreateContainer, populate with files during LARP build, files appear immediately in running container

**Key Architectural Findings (Mount Timing)**:

The OCI runtime applies all mounts from the container spec during `CreateContainer`, **before** any hooks execute. Initial approach of deferring mount creation until after build fails:
- Container creation fails immediately if mount sources don't exist
- createRuntime hook never runs if container creation fails

**Solution: Backfilling Pattern**:
1. `CreateContainer` (NRI) → Pre-create **empty** mount directories
2. Inject mounts pointing to these empty directories (mount succeeds)
3. `createRuntime` hook (OCI) → Queries ZeroMQ, waits for build if pending
4. Background build task → Sleeps, then hardlinks/populates files into already-mounted directories
5. Files appear immediately in running container (bind mount sees new files)
6. Hook receives build-complete signal, exits, container process starts

**Backfilling Test Results**:
- ✅ Empty directories mount successfully (no errors)
- ✅ Files added to mounted directory visible in container immediately
- ✅ No container restart required
- ✅ Clean coordination via ZeroMQ query+subscribe pattern

---

### Phase 3: Extract Build Args + Real Nix Builds ✅ COMPLETE

**Goal**: Replace mock LARP builds with real Nix builds using the same build coordination logic as CSI.

**Completed**:
1. ✅ Extracted `get_build_args()` from NodeServicer into shared `nix.get_build_args()` utility
2. ✅ Enables builder pod discovery and cache substitution in NRI builds
3. ✅ NRI plugin now performs real Nix builds instead of mock LARP builds
4. ✅ Both CSI and NRI code paths share build coordination logic (DRY principle)
5. ✅ `_spawn_build_task` calls `get_build_args()` and passes `extra_args` to `build_packages()`

**Implementation**:
- Moved build args discovery to `nix.py` as reusable function
- Removed duplication between `service.py` and `nriplugin.py`
- NRI containers now build using same builder pod discovery and cache substitution as CSI volumes

**Result**: Building /nix stores into containers via NRI now works with full build coordination.

**Next Phase** (Phase 4):
1. Parse FHS annotations from pod metadata
2. Extract store paths to FHS locations (/etc/ssl, /etc/nsswitch, etc)
3. Handle symlinks: error if directory structure has symlinks where directories expected
4. Update annotation filter: change from `nix-nri/test` to `nix-nri/store-paths`
5. Parse multiple mount paths from annotation, trigger real builds with FHS path extraction
6. Implement cache coordination with nix-cache StatefulSet

---

## Implementation Details (Phase 2)

### OCI Hook Mechanism

Hook invocation (injected by NRI CreateContainer):
```
path: /usr/bin/env
args: ["chroot", "/var/lib/nix-csi", "/nix/store/xyz-nri-wait-0.1.0/bin/wait"]
env: {
  NRI_CONTAINER_ID: "{container-id}",
  NRI_QUERY_SOCKET: "/nix/var/nix-csi/wait-req.sock",
  NRI_PUB_SOCKET: "/nix/var/nix-csi/wait-pub.sock",
  NRI_TIMEOUT: "30"
}
```

- **path**: `/usr/bin/env` - POSIX standard tool, absolute path, lets system find chroot via PATH
- **args**: Chroot path + wrapper script path (buildPythonApplication creates wrapper at `bin/wait`)
- **env**: Configuration passed to nri-wait process

The hook:
1. Invokes `/usr/bin/env chroot /var/lib/nix-csi /nix/store/xyz-nri-wait-0.1.0/bin/wait`
2. Chroot into `/var/lib/nix-csi` (full Nix closure with dependencies)
3. Python wrapper script invokes nri_wait module
4. Module queries REP socket at `/nix/var/nix-csi/wait-req.sock`, waits for build completion via PUB socket at `/nix/var/nix-csi/wait-pub.sock`
5. Exits (0 on success, 1 on error)

### ZeroMQ Server Implementation (Phase 2)

**Architecture**:
- Store sockets and build status in `NriPlugin` class (not as global state)
- Build status tracked with `cachetools.TTLCache(maxsize=10000, ttl=3600)` - auto-expires old entries
- Use `aiozmq` for async socket I/O (will package if not in nixpkgs)
- Socket locations:
  - **Host**: `/var/lib/nix-csi/nix/var/nix-csi/wait-req.sock` (REP) and `/var/lib/nix-csi/nix/var/nix-csi/wait-pub.sock` (PUB)
  - **Pod**: `/nix/var/nix-csi/wait-req.sock` (REP) and `/nix/var/nix-csi/wait-pub.sock` (PUB)

**Phase 2 Approach (LARP for now)**:
1. Keep using `nix-nri/test` annotation (same as Phase 1 for now)
2. When CreateContainer fires, spawn async "build" task (just sleeps for demo)
3. REP socket answers build status queries
4. PUB socket broadcasts build completion after simulated delay
5. Later: Replace LARP with actual Nix builds when socket communication verified

**Dependencies**:
- Add `cachetools` to `python/pyproject.toml` (for TTLCache)
- Add `pyzmq` to `python/pyproject.toml` (for ZeroMQ)
- Package `aiozmq` in nixpkgs if needed (currently not packaged)

## Files to Create/Modify (Phase 2)

**nri-wait package (separate Python application):** ✅ COMPLETE
- ✅ `pkgs/nri-wait/pyproject.toml`: hatchling build, pyzmq dependency, console_scripts
- ✅ `pkgs/nri-wait/nri_wait/__init__.py`: ZeroMQ query + wait logic
- ✅ `pkgs/nri-wait/nri_wait/__main__.py`: Entry point
- ✅ `pkgs/nri-wait/default.nix`: buildPythonApplication with cachetools
- ✅ Update `pkgs/default.nix`: Add nri-wait

**NRI plugin ZeroMQ server (Phase 2 - IN PROGRESS):**
- `python/nix_csi/nriplugin.py`:
  - Add ZeroMQ sockets to NriPlugin class (REP for queries, PUB for broadcasts)
  - Add build status cache (TTLCache) to NriPlugin
  - Implement async socket servers in nri_serve()
  - Add methods: query_build_status(), publish_build_complete()
  - Modify CreateContainer to spawn "build" task (LARP for Phase 2)
- `python/pyproject.toml`: Add cachetools, pyzmq, aiozmq dependencies
- `pkgs/aiozmq/default.nix`: Package aiozmq if not in nixpkgs (TBD)

---

## Phase 4: FHS Path Mounting (Planned)

**Goal**: Support mounting specific package contents at FHS (Filesystem Hierarchy Standard) locations.

### FHS Mounting Use Case

Example: User wants `/nix/store/cacert-1.0/etc/ssl` available at `/etc/ssl` in container.

```yaml
metadata:
  annotations:
    nix-nri.io/my-container/etc-ssl: /nix/store/xyz-cacert-1.0/etc/ssl
    nix-nri.io/my-container/etc-nsswitch: /nix/store/abc-glibc-2.37/etc/nsswitch.conf
```

### Implementation Strategy

1. **Parse FHS annotations**: Extract from pod annotations using `nix-nri.io/{container-name}/{path-in-container}` format
2. **Pre-create mount directories**: During CreateContainer, create target directories (`/etc/ssl`, `/etc/nsswitch.conf`, etc.)
3. **Inject mounts**: Add mounts for each FHS path to ContainerAdjustment
4. **Backfill during build**: During build phase, extract/hardlink source paths into mount directories
5. **Error on symlinks**: If encountering symlink where directory expected, fail fast (indicates structural mismatch)

### Volume Layout Example

```
/var/lib/nix-csi/nix/var/volumes/{container-id}/
├── nix/store/xyz-cacert-1.0/...       (all storepaths hardlinked)
├── nix/store/abc-glibc-2.37/...
├── etc/
│   ├── ssl/                            (hardlinks from cacert)
│   └── nsswitch.conf                   (hardlink from glibc)
└── lib64/                              (hardlinks from glibc if requested)
```

### Mount Injection

Multiple mounts injected into ContainerAdjustment:
- `/var/lib/nix-csi/nix/var/volumes/{container-id}/nix` → `/nix`
- `/var/lib/nix-csi/nix/var/volumes/{container-id}/etc/ssl` → `/etc/ssl`
- `/var/lib/nix-csi/nix/var/volumes/{container-id}/etc/nsswitch.conf` → `/etc/nsswitch.conf`
- (etc. for each annotated path)

---

## Open Questions

- How to handle multiple pods on same node (cleanup, deduplication)?
- Should we implement garbage collection for old container directories?
- Do we want to support RW semantics later (overlayfs or copy-on-write)?
- Should wait binary have a timeout, or wait indefinitely?
