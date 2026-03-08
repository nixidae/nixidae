# nixkube Improvement Plan v2

Second round of improvements following the [v1 code review](improvements.md) (33/36 items done).
Organized by theme, prioritized within each section.

> **AI instructions**: When completing an item, mark it with ✅ DONE and add a short note
> about what was changed. When discovering new improvement opportunities during implementation,
> add them to the appropriate section with the next available sub-number. Keep this file
> as the single source of truth for improvement tracking.

Each item includes a **Model** recommendation for cost-efficient AI implementation:
- **Haiku**: Mechanical changes — adding docstrings, fixing typos, moving constants, tiny refactors
- **Sonnet**: Moderate reasoning — deduplication, test writing, small features, config validation
- **Opus**: Complex design — architecture docs, security model, health checks, large refactors

---

## 1. Inline Documentation & Docstrings

The codebase has good comments in complex areas (NRI lifecycle, mount.py) but inconsistent
docstring coverage elsewhere. Inline docs are the primary developer documentation strategy
since developers (and AI) discover code through their editor.

### 1.1 Missing docstrings on public functions ✅ DONE

Added docstrings to all public functions: cli.py (main, async_main), subprocessing.py (log_command, run_console, run_captured, try_captured, try_console), store.py (extract_store_name), nri/server.py (NriPlugin.__init__), csi/server.py (csi_error_handler), constants.py (module docstring).

| File | Function | Notes |
|------|----------|-------|
| `cli.py` | `main()`, `async_main()` | Entry point — document what servers start and config sources |
| `subprocessing.py` | `log_command()` | Trivial but public |
| `subprocessing.py` | `run_console()`, `run_captured()` | Core subprocess API — document timeout behavior, return semantics |
| `store.py` | `extract_store_name()` | Regex-based — document what it extracts and edge cases |
| `cache.py` | `copy_lock` module-level | Document the locking strategy (frozenset key, why Semaphore) |
| `nri/server.py` | `NriPlugin.__init__()` | Document zmq_server and cri_socket roles |
| `csi/server.py` | `csi_error_handler()` | Document the decorator's event emission + gRPC re-raise behavior |
| `nri/server.py` | `nri_error_handler()` | Same as above for NRI variant |
| `constants.py` | Module docstring | Document that this is the single source of truth for all config |

**Effort**: Small per item, ~1 hour total
**Priority**: High — immediate developer experience improvement
**Model**: Haiku

### 1.2 Type annotations on remaining untyped code ✅ DONE

Added `func: Any) -> Any` signatures to both `csi_error_handler` and `nri_error_handler`
with inline comment explaining the Any usage. Typed `_build_args_cache` as
`TTLCache[str, list[str]]` and `build_status` as `TTLCache[str, dict[str, str]]`.

**Effort**: Small
**Priority**: Medium — helps pyright catch more issues
**Model**: Sonnet

### 1.3 Add module-level docstrings to all `__init__.py` files ✅ DONE

Added "nixkube: Kubernetes plugin for injecting Nix stores into pods." to src/__init__.py. Other packages (nix, csi, nri) already had docstrings.

**Effort**: Tiny
**Priority**: Low
**Model**: Haiku

---

## 2. Error Handling & Resilience

### 2.1 Graceful handling of Kubernetes API unavailability ✅ DONE

Added `_nixkube_pod_fetch_failed: TTLCache[str, bool]` with 15s TTL so failures retry
after a short delay instead of either hammering the API or blocking forever.

**Effort**: Small
**Priority**: High — silent event loss in production
**Model**: Sonnet

### 2.2 Structured error context in SubprocessError ✅ DONE

Added __str__() and __repr__() methods to SubprocessError to include command and return code (truncated to 200/150 chars respectively).

**Effort**: Tiny
**Priority**: Medium — improves debuggability
**Model**: Haiku

### 2.3 ZeroMQ socket cleanup on initialization failure ✅ DONE

Combined socket creation into a single try/except that calls `self.shutdown()` on failure,
ensuring the ZMQ context and any partial sockets are cleaned up.

**Effort**: Small
**Priority**: Medium — resource leak on startup failure
**Model**: Sonnet

### 2.4 `events.py` duplicate comment on line 139 ✅ DONE

Removed duplicate comment leftover from refactor.

**Effort**: Tiny
**Priority**: Low — cosmetic
**Model**: Haiku

---

## 3. Code Simplification & Cleanup

### 3.1 Deduplicate build functions in `nix/build.py` ✅ DONE

Extracted `_run_nix_build(args, timeout, timeout_msg, error_msg)` helper. The three
public functions are now thin wrappers that construct their specific args.

**Effort**: Small
**Priority**: Medium — reduces ~40 lines of duplicated error handling
**Model**: Sonnet

### 3.2 Remove mutable default argument in `fetch_packages` ✅ DONE

Changed to `extra_args: list[str] | None = None` with `extra_args or []` pattern.

**Effort**: Tiny
**Priority**: Low — correctness/lint
**Model**: Haiku

### 3.3 Consolidate `ENABLE_COMPAT_DRIVER` and `NRI_ENABLED` into constants.py ✅ DONE

Moved both from cli.py to constants.py. Updated NRI_ENABLED default from false to true per user preference.

**Effort**: Tiny
**Priority**: Low — consistency
**Model**: Haiku

---

## 4. Observability & Debugging

### 4.1 Structured logging preparation

The codebase uses f-string logging consistently, which is good for readability. However,
for production debugging at scale, structured logging (JSON) would be valuable. The
logging config already supports dictConfig — adding a JSON formatter option would let
operators switch to structured output without code changes.

**Fix**: Add a `json_formatter` example to the kubenix `loggingConfig` option documentation,
and optionally ship a default JSON logging config as an alternative ConfigMap.

**Effort**: Small
**Priority**: Medium — helps operators with log aggregation (Loki, ELK, etc.)
**Model**: Sonnet

### 4.2 Startup configuration summary ✅ DONE

Added `log_effective_app_config()` using `f"{var=}"` style. Also renamed
`log_effective_config()` → `log_effective_log_config()` for clarity. Both are called
from `async_main()`. Added `RSYNC_CONCURRENCY` (int) to constants; renamed old semaphore
to `RSYNC_SEM`.

**Effort**: Small
**Priority**: High — essential for production debugging
**Model**: Sonnet

### 4.3 Health check / readiness probe

The daemon has no health check endpoint. If the gRPC server is listening but the
nix-daemon subprocess is dead, or the ZeroMQ sockets are wedged, kubelet has no way
to detect the failure.

**Fix**: Add a simple HTTP health endpoint (or use the CSI `Probe` RPC which is
already part of the spec but returns a stub). Check nix-daemon connectivity and
ZeroMQ socket state.

**Effort**: Medium
**Priority**: Medium — improves operational reliability
**Model**: Opus

---

## 5. Testing

### 5.1 Add unit tests for `cache.py` retry logic ✅ DONE

Added `tests/test_cache.py` with 6 tests covering: empty paths early return, first-attempt
success (no sleep), all-6-attempts failure (5 sleeps), mid-retry success, backoff sequence
(5, 10, 20, 40, 60s), and sign failure not aborting copy.

**Effort**: Small
**Priority**: Medium
**Model**: Sonnet

### 5.2 Add unit tests for `volume.py:is_mount()` ✅ DONE

Added `tests/test_volume.py` with 7 tests covering: path present, path absent, empty file,
malformed line skipped, OSError returns False, multiple mounts, partial path not matched.

**Effort**: Small — already accepts `mounts_file` parameter for testing
**Priority**: Medium
**Model**: Sonnet

### 5.3 Property-based tests for `_format_event_note` ✅ DONE

Added `hypothesis` to test deps (pyproject.toml, default.nix, shell.nix) and added
`TestFormatEventNoteProperties` with two `@given` tests: byte limit always holds and
message always preserved verbatim. All 91 tests passing.

**Effort**: Small
**Priority**: Low — existing tests are good, this is belt-and-suspenders
**Model**: Sonnet

---

## 6. Operator Documentation (External)

These are user-facing docs aimed at operators deploying nixkube. They should live in
`doc/` or a future docs site, not inline.

### 6.1 Quickstart guide

The README has deployment commands but no guided walkthrough. A quickstart should cover:
1. Prerequisites (Kubernetes cluster, containerd with NRI enabled, Nix)
2. Clone + configure SSH keys
3. Deploy with `nix run`
4. Deploy a test workload
5. Verify it works (check events, logs)

**Effort**: Medium
**Priority**: High — biggest barrier to adoption
**Model**: Opus

### 6.2 Configuration reference ✅ DONE

Enhanced option descriptions across all four kubenix option files:
- `options.nix`: descriptions for `undeploy`, `deploySecrets`; `version` marked `internal`;
  examples for `authorizedKeys`, `knownHosts`, `loggingConfig`, `systems`
- `daemonset.nix`: fixed copy-paste bug (`node.enable` said "cache")
- `cache.nix`: improved `cache.enable`, `storageClassName`, `loadBalancerPort` descriptions
- `builder.nix`: added descriptions + examples for `deployments`/`daemonsets`; rewrote
  `privilegedSandboxedBuilds`; improved `loadBalancerPort`
- `doc/options.md`: regenerated via new `just gendoc` recipe
- `justfile`: added `just gendoc` and `just precommit` (fmt+lint+test+gendoc)

**Effort**: Medium
**Priority**: High — options.nix is 7K+ lines, but most are already documented
**Model**: Sonnet

### 6.3 Architecture overview

A visual diagram showing the communication flow (cache ↔ nodes, CSI/NRI protocols,
ZeroMQ coordination) would significantly help new contributors and operators.

**Fix**: ASCII or Mermaid diagram in README or doc/architecture.md.

**Effort**: Small
**Priority**: Medium
**Model**: Opus

### 6.4 Troubleshooting guide

Common failure modes and their solutions:
- Pods stuck in ContainerCreating (CSI volume not ready)
- Pods stuck in Terminating (mount cleanup failure)
- NRI plugin not registering (containerd NRI not enabled)
- Build timeouts (builders unreachable, cache down)
- Store path verification failures
- Events to look for (`kubectl get events --field-selector reason=NixBuildFailed`)

**Effort**: Medium
**Priority**: High — reduces support burden, enables self-service debugging
**Model**: Opus

### 6.5 NRI vs CSI decision guide

Operators need to understand when to use CSI (explicit volumes) vs NRI (annotations).
Trade-offs:
- CSI: explicit, works without NRI, but verbose pod specs
- NRI: automatic, cleaner pods, but requires containerd NRI support + Linux 5.2+
- Mixing: CSI takes precedence when both are configured

**Effort**: Small
**Priority**: Medium
**Model**: Sonnet

---

## 7. Architecture & Design

### 7.1 Extract NRI wire protocol from server.py ✅ DONE

Added `NriPlugin` base class to `grpclib_nri/plugin.py` handling Configure/Synchronize/Shutdown
and all stub handlers. `nixkube/nri/server.py` now only implements `CreateContainer` and
`StateChange`, mirroring `csi/server.py` structure.

**Effort**: Medium
**Priority**: Low — the current structure works fine, this is a clarity improvement
**Model**: Sonnet

### 7.2 Consider replacing ZeroMQ with asyncio primitives ❌ WONTFIX

ZeroMQ stays. pyzmq is battle-tested, and the PUB/SUB pattern across Unix domain
sockets (including across Linux network namespaces) is genuinely the right tool here —
reimplementing that with raw asyncio streams would be reinventing the wheel badly.
ZeroMQ is also worth having experience with as a library.

**Effort**: Large
**Priority**: N/A
**Model**: Opus

### 7.3 Make cache destination configurable

From `cache.py` TODO: the cache copy destination is hardcoded to `ssh-ng://nix@nix-cache`.
Supporting configurable destinations (S3, GCS, custom caches) would make nixkube
more flexible.

**Fix**: Add a `cacheDestination` kubenix option that generates an env var. `copy_to_cache`
reads from env instead of hardcoding. Start with just the SSH destination being
configurable; exotic destinations can come later.

**Effort**: Small (env var) to Large (full plugin system)
**Priority**: Medium — enables more deployment scenarios
**Model**: Sonnet (env var), Opus (plugin system)

---

## 8. Build System & CI

### 8.1 Add `just lint` to CI pipeline ✅ DONE

Added `check` CI job with `nix-shell --run "pyright pkgs/nixkube/src"`. Release job
now requires `check` to pass.

**Effort**: Small
**Priority**: High — prevents type regressions
**Model**: Sonnet

### 8.2 Add `just check-fmt` to CI pipeline ✅ DONE

Added `nix-shell --run "treefmt --fail-on-change"` step to the same `check` CI job.

**Effort**: Small
**Priority**: High — prevents formatting regressions
**Model**: Sonnet

### 8.3 Pin GitHub Actions versions ⏭️ DEFERRED

Skipped per user decision: keeping main branch references for convenience; planning to minimize GHA usage in favor of NixOS tests.

**Effort**: Small
**Priority**: Medium — security hygiene
**Model**: Haiku

---

## 9. Security

### 9.1 Document security model

The project makes several security-sensitive decisions that should be documented:
- Why `setns(2)` + `chroot(2)` is used (and what privileges are required)
- SSH key management (no rotation, ConfigMap-based)
- Store path trust model (no cryptographic verification by default)
- ZeroMQ socket permissions (IPC on shared filesystem)
- RBAC requirements (ClusterRole scope)

**Fix**: Add a `doc/security.md` or a "Security Model" section to README.

**Effort**: Medium
**Priority**: Medium — important for enterprise adoption
**Model**: Opus

### 9.2 Validate environment variable types at startup ✅ DONE

Added `_parse_int_env()` and `_parse_float_env()` helpers in `constants.py` that exit
with a clear error message on invalid input. Used for `RSYNC_CONCURRENCY` and
`NIX_BUILD_TIMEOUT`.

**Effort**: Small
**Priority**: Medium — prevents confusing runtime errors
**Model**: Sonnet

---

## 10. Performance

### 10.1 Parallel hardlinking for large closures

From improvements v1 item 4.1 (NOT STARTED). `hardlinks.py` uses `os.walk()` sequentially.
For large closures (1000+ paths), this could be slow.

**Fix**: Measure first with realistic closure sizes. If >1s, consider `concurrent.futures`
thread pool for the I/O-bound hardlink operations.

**Effort**: Medium (measurement + implementation)
**Priority**: Low — needs measurement to justify
**Model**: Sonnet

### 10.2 Batch nix path-info calls in copy_to_cache ✅ DONE

Use asyncio.gather() to run regular and derivation path-info calls concurrently.

**Effort**: Tiny
**Priority**: Low — small optimization
**Model**: Haiku

---

## Summary

| Priority | Count | Themes |
|----------|-------|--------|
| High     | 7     | Docstrings, event resilience, startup logging, quickstart, config ref, troubleshooting, CI checks |
| Medium   | 12    | Types, error context, ZMQ cleanup, build dedup, structured logging, health check, tests, architecture diagram, cache config, security, env validation |
| Low      | 8     | Init docstrings, mutable default, constant consolidation, property tests, protocol extraction, ZMQ replacement, hardlink perf, batch path-info |

### Model cost breakdown

| Model  | Count | Items |
|--------|-------|-------|
| Haiku  | 7     | 1.1, 1.3, 2.2, 2.4, 3.2, 3.3, 8.3, 10.2 |
| Sonnet | 13    | 1.2, 2.1, 2.3, 3.1, 4.1, 4.2, 5.1, 5.2, 5.3, 6.2, 6.5, 7.1, 7.3 (env var), 8.1, 8.2, 9.2, 10.1 |
| Opus   | 7     | 4.3, 6.1, 6.3, 6.4, 7.2, 7.3 (plugin), 9.1 |

### Recommended execution order

1. **Quick wins** (1.1, 2.4, 3.2, 3.3, 2.2) — immediate code quality, <2 hours, mostly Haiku
2. **Production reliability** (2.1, 4.2, 9.2) — prevent silent failures, ~2 hours, Sonnet
3. **CI hardening** (8.1, 8.2) — prevent regressions, ~1 hour, Sonnet
4. **Operator docs** (6.1, 6.4, 6.2) — unlock adoption, ~4 hours, Opus for guides, Sonnet for options
5. **Code improvements** (3.1, 1.2, 2.3, 4.1) — maintainability, ~3 hours, Sonnet
6. **Everything else** — as time and motivation permit
