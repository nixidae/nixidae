# nixkube Python Code Review — Suggested Improvements

Professional code review of `pkgs/nixkube/src/` (~3,900 lines).
Organized by priority and effort. Status tracked per item.

---

## 1. Dead Code & Unused Abstractions

### 1.1 `models.py` — Unused `PodInfo` dataclass ✅ DONE
Deleted `models.py` entirely.

### 1.2 Backwards-compatibility aliases in `errors.py` ✅ DONE
Removed `PathBuildError`, `FlakeBuildError`, `ExprBuildError` aliases.

### 1.3 `RemoveVolumeDirError` — Never raised ✅ DONE
Removed unused exception class.

---

## 2. Correctness & Robustness

### 2.1 `volume.py` — Bind mount doesn't actually get RO on first call ✅ DONE
Added two-step mount+remount for both CSI and NRI paths. Unmount on remount failure to never leave a writable mount exposed.

### 2.2 `cache.py` — Locking uses only first path, not full set ✅ DONE
Changed lock key to `frozenset(package_paths)`.

### 2.3 `nri/server.py` — Build task exception swallowed silently ✅ DONE
Made `_spawn_build_task` re-raise after reporting event. Fixed done_callback to distinguish success/failure/cancel.

### 2.4 `nri/server.py` — `_spawn_build_task` resolves store paths before mount ✅ INVESTIGATED
Investigated — the current behavior is correct. Store paths are resolved in the daemonset namespace where the hardlink farm is built, then passed as bind-mount sources to the mount worker. No change needed.

### 2.5 `cri.py` — gRPC channel not closed on error ✅ DONE
Created `cri_channel` async context manager for safe channel cleanup.

### 2.6 `subprocessing.py` — Logger name mismatch ✅ DONE
Renamed from `"nix-csi.subprocessing"` to `"nixkube.subprocessing"`.

### 2.7 `volume.py` — `unmount()` doesn't verify mount is gone ✅ DONE (added post-review)
`unmount()` now always verifies the mount is actually gone after `umount2`, raising `UnmountError` if it persists. Added critical comment to `NodeUnpublishVolume` documenting that it must never return success while the mount exists.

---

## 3. Design & Architecture

### 3.1 Duplicated mount flag constants ✅ DONE
Moved `MS_BIND`, `MS_RDONLY`, `MS_REMOUNT` to `constants.py`. Both `volume.py` and `nri/mount.py` now import from there.

### 3.2 Duplicated `copy_to_cache` fire-and-forget pattern ✅ DONE
Extracted `schedule_copy_to_cache()` helper in `cache.py`.

### 3.3 `volume.py:56` — Redundant variable assignment ✅ DONE
Renamed parameter to `volume_root` directly.

### 3.4 CSI vs NRI overlay dir naming ✅ DONE
Unified to `upper/` and `work/` in both CSI and NRI paths. `prepare_volume` pre-creates them, both protocols use the same dirs.

### 3.5 `events.py` — `_format_event_note` called with double-applied logs ✅ DONE
Fixed to pass `message` and `logs` separately.

---

## 4. Performance

### 4.1 `hardlinks.py` — Recursive Python walks for large closures
- **Status**: NOT STARTED — Low priority, needs measurement before optimizing.
- **Suggestion**: Consider `os.walk()` or compiled helper for truly large closures.

### 4.2 `nix/build.py` — `get_build_args()` called on every request
- **Status**: NOT STARTED
- **Suggestion**: Cache with 30s TTL. It queries k8s API and pings cache on every volume request.

### 4.3 `nri/server.py` — `get_current_system()` called per-container
- **Status**: WON'T FIX — `get_current_system()` is `@cache`-decorated so it's effectively free after first call. The double call per `CreateContainer` has zero cost.

---

## 5. Error Handling

### 5.1 `nri/server.py` — No error handler decorator ✅ DONE
Created `nri_error_handler` decorator applied to all NRI handlers.

### 5.2 `nix/database.py` — Double exception wrapping ✅ DONE
Flattened to single try/except.

### 5.3 `hardlinks.py:60-62` — Bare re-raise of `HardlinkClosureError`
- **Status**: NOT STARTED — Low priority.
- **Note**: The bare re-raise IS needed because `HardlinkClosureError` inherits from `Exception` via `CSIError` → `GRPCError`. Without it, the general `except Exception` would double-wrap it. Consider adding a brief comment.

---

## 6. Type Safety

### 6.1 `nri/server.py` — `Optional` → `X | None` ✅ DONE
Replaced all `Optional[X]` with `X | None`.

### 6.2 `annotations.py` — Untyped `pod_annotations` parameter ✅ DONE
Added `Annotations = Mapping[str, str]` type alias and annotations to all functions.

### 6.3 `nri/zmq.py` — `Optional` → `X | None` ✅ DONE
Replaced during ZeroMQ TTLCache consolidation.

---

## 7. Testing Opportunities

### 7.1 `store.py` — Pure logic unit tests
- **Status**: NOT STARTED

### 7.2 `events.py:_format_event_note` — Truncation logic tests
- **Status**: NOT STARTED

### 7.3 `hardlinks.py` — Symlink handling edge case tests
- **Status**: NOT STARTED

### 7.4 `annotations.py` — Already has good coverage ✅ EXISTING
38 tests covering annotation parsing. Could add edge cases.

---

## 8. Security

### 8.1 `nix/build.py` — `nix_expr` security boundary
- **Status**: DOCS ONLY — No code change needed. Security boundary is RBAC.

### 8.2 `nri/mount.py` — `setns` + `chroot` privileges
- **Status**: DOCS ONLY — No code change. Operational concern for deployment docs.

---

## 9. Code Clarity

### 9.1 %-formatting log lines → f-strings ✅ DONE
Converted all remaining %-formatting to f-strings.

### 9.2 `cri.py` %-formatting ✅ DONE
Converted to f-string.

### 9.3 `nri/server.py` — Unnecessary dict + loop ✅ DONE
Simplified to `mapping = plugin.__mapping__()`.

### 9.4 `nix/system.py` — Dead `logger = None` ✅ DONE
Removed.

---

## 10. Dependency & Import Hygiene

### 10.1 `volume.py` — `aiofiles` dependency ✅ DONE
Replaced with synchronous `Path.read_text()` since `/proc/self/mounts` is virtual and never blocks. Removed `aiofiles` from `pyproject.toml` and `default.nix`. Made `is_mount()` a sync function.

### 10.2 `nri/server.py` — Heavy import surface
- **Status**: NOT STARTED — Low priority.
- **Suggestion**: Extract `_register_plugin` and `_serve_plugin_channel` to `nri/protocol.py`.

---

## Additional fixes (discovered during review)

### A.1 NRI plugin mapping initialization ✅ DONE
Simplified from dict+loop to direct assignment.

### A.2 `_stream_id`/`_flags` pyright warnings ✅ DONE
Fixed unused variable warnings in `struct.unpack_from`.

### A.3 ZeroMQ server unbounded state ✅ DONE
Consolidated `container_pids`, `container_bundles`, `_pid_events` into single `TTLCache[str, ContainerInfo]`.

### A.4 `build_packages` renamed to `fetch_packages` ✅ DONE
Renamed and changed return type to `None` since callers already have the paths.

### A.5 `HOST_ROOT` constant ✅ DONE
Added to `constants.py`, derived `HOST_PROC_PATH` from it. Updated `nri/cleanup.py` and `nri/server.py`.

### A.6 Double closure resolution in NRI ✅ DONE
Removed redundant `get_closure_paths()` call in NRI server; `prepare_volume` handles it.

### A.7 CRI connectivity check at NRI startup ✅ DONE
NRI server now calls `list_container_ids` at startup and fails fast if CRI is unreachable.

---

## Summary

| Status | Count |
|--------|-------|
| ✅ DONE | 28 |
| NOT STARTED | 6 |
| WON'T FIX | 1 |
| DOCS ONLY | 2 |

### Remaining items (by priority)
1. **4.2** Cache `get_build_args()` with 30s TTL
2. **5.3** Add comment to hardlinks.py bare re-raise
3. **7.1-7.3** Unit tests for pure functions (store.py, events.py, hardlinks.py)
4. **10.2** Extract NRI protocol module
5. **4.1** Hardlinks performance (measure first)
