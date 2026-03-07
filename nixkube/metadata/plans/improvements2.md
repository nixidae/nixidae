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

### 1.1 Missing docstrings on public functions

Several public functions lack docstrings. Add concise docstrings explaining *what* and *why*,
not restating obvious parameter names.

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

### 1.2 Type annotations on remaining untyped code

Most code has good type hints but a few spots are lax:

- `csi/server.py`: `csi_error_handler` and `nri_error_handler` lack typed signatures
  (should use `ParamSpec`/`Callable` or at minimum document the expected signature)
- `ZeroMQServer.build_status: TTLCache` — should be `TTLCache[str, dict[str, str]]`
- `_build_args_cache: TTLCache` — should be `TTLCache[str, list[str]]`

**Effort**: Small
**Priority**: Medium — helps pyright catch more issues
**Model**: Sonnet

### 1.3 Add module-level docstrings to all `__init__.py` files

Currently the `__init__.py` files have re-exports but no module docstrings. A one-line
docstring per package helps editors show package purpose on hover.

**Effort**: Tiny
**Priority**: Low
**Model**: Haiku

---

## 2. Error Handling & Resilience

### 2.1 Graceful handling of Kubernetes API unavailability

`events.py:get_nixkube_pod()` caches the pod on first call but has no retry if the
initial fetch fails (returns None forever). If the K8s API is temporarily down at startup,
no events will ever be reported.

**Fix**: Use a TTLCache with short TTL (e.g., 60s) for the failure case so it retries,
while still caching successful results indefinitely.

**Effort**: Small
**Priority**: High — silent event loss in production
**Model**: Sonnet

### 2.2 Structured error context in SubprocessError

`SubprocessError` stores command as `list[str]` but the string representation only shows
the return code. When these bubble up through CSIError/BuildError, the original command
is lost from logs.

**Fix**: Include the command in `__str__`/`__repr__` (truncated to reasonable length).

**Effort**: Tiny
**Priority**: Medium — improves debuggability
**Model**: Haiku

### 2.3 ZeroMQ socket cleanup on initialization failure

In `zmq.py:initialize()`, if the PUB socket bind fails after the REP socket succeeds,
the REP socket is leaked. The sequential try/except blocks don't clean up earlier resources.

**Fix**: Use a single try/except around all socket creation, or add cleanup in each
except block.

**Effort**: Small
**Priority**: Medium — resource leak on startup failure
**Model**: Sonnet

### 2.4 `events.py` duplicate comment on line 139

Line 139 has `# Extract logs from exception if needed (already done above)` — this is
a leftover from a refactor. Remove it.

**Effort**: Tiny
**Priority**: Low — cosmetic
**Model**: Haiku

---

## 3. Code Simplification & Cleanup

### 3.1 Deduplicate build functions in `nix/build.py`

`build_store_path()`, `build_flake_ref()`, and `build_nix_expr()` share identical
error handling patterns (catch CommandTimeoutError → BuildError, catch SubprocessError →
BuildError). Only the nix build arguments differ.

**Fix**: Extract a `_run_nix_build(args, gc_root, label, timeout)` helper that handles
the try/except pattern once. The three public functions become thin wrappers that
construct their specific args.

**Effort**: Small
**Priority**: Medium — reduces ~40 lines of duplicated error handling
**Model**: Sonnet

### 3.2 Remove mutable default argument in `fetch_packages`

`fetch_packages(extra_args: list[str] = [])` uses a mutable default. While not currently
a bug (the list is only read, never mutated), it's a Python anti-pattern that linters flag.

**Fix**: Change to `extra_args: list[str] | None = None` with `extra_args = extra_args or []`.

**Effort**: Tiny
**Priority**: Low — correctness/lint
**Model**: Haiku

### 3.3 Consolidate `ENABLE_COMPAT_DRIVER` and `NRI_ENABLED` into constants.py

These two env var reads live in `cli.py` rather than `constants.py` where all other
environment configuration lives. Moving them centralizes the "single source of truth"
for configuration.

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

### 4.2 Startup configuration summary

`cli.py:log_effective_config()` logs the logging configuration, but doesn't log the
effective *application* configuration (timeouts, enabled features, paths). Operators
debugging issues need to know what config the daemon started with.

**Fix**: Add a `log_effective_app_config()` that logs key constants: `NIX_BUILD_TIMEOUT`,
`CACHE_ENABLED`, `BUILDERS_ENABLED`, `VERIFY_STORE_PATHS`, `RSYNC_CONCURRENCY`,
`HOST_MOUNT_PATH`, `NRI_PLUGIN_IDX`, etc.

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

### 5.1 Add unit tests for `cache.py` retry logic

The exponential backoff retry loop in `copy_to_cache()` is complex (6 attempts, capped
at 60s) but has no unit tests. The retry logic is pure enough to test with a mock
subprocess.

**Effort**: Small
**Priority**: Medium
**Model**: Sonnet

### 5.2 Add unit tests for `volume.py:is_mount()`

`is_mount()` parses `/proc/self/mounts` — this is easily testable with a temp file
containing sample mount entries. Test cases: path present, path absent, malformed
entries, OSError handling.

**Effort**: Small — already accepts `mounts_file` parameter for testing
**Priority**: Medium
**Model**: Sonnet

### 5.3 Property-based tests for `_format_event_note`

The truncation logic in `events.py` handles UTF-8 multi-byte boundaries. Property-based
testing (hypothesis) would stress-test this with random Unicode strings and verify the
1000-byte invariant always holds.

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

### 6.2 Configuration reference

The kubenix options are auto-generated in `doc/options.md` but lack:
- Example values for each option
- Explanation of when you'd change defaults
- Interaction between options (e.g., `cache.enable` + `builders.enable`)

**Fix**: Enhance option descriptions in `kubenix/options.nix` with `example` attributes
and richer `description` text. The auto-generated docs will inherit these improvements.

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

### 7.1 Extract NRI wire protocol from server.py

From improvements v1 item 10.2 (NOT STARTED). `nri/server.py` (647 lines) mixes
NRI protocol handling (registration, event subscription) with business logic (build
task spawning, annotation parsing).

**Fix**: Extract `_register_plugin()` and `_serve_plugin_channel()` equivalents to
`nri/protocol.py`. The `NriPlugin` class focuses on business logic; protocol wire
details are hidden.

**Effort**: Medium
**Priority**: Low — the current structure works fine, this is a clarity improvement
**Model**: Sonnet

### 7.2 Consider replacing ZeroMQ with asyncio primitives

The ZeroMQ dependency adds complexity (socket lifecycle, IPC files on disk, separate
context management) for what is essentially inter-process coordination. Since both
the build task and the OCI hook run on the same node, Unix domain sockets with asyncio
streams could achieve the same result with fewer moving parts.

**Caveat**: ZeroMQ's PUB/SUB pattern is convenient for the heartbeat pump. Evaluate
whether the simplification is worth the migration effort.

**Effort**: Large
**Priority**: Low — only if ZeroMQ causes operational issues
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

### 8.1 Add `just lint` to CI pipeline

The `just lint` recipe (pyright) exists but isn't run in CI. Type errors could regress
silently.

**Fix**: Add a CI job that runs `just lint` and fails on errors.

**Effort**: Small
**Priority**: High — prevents type regressions
**Model**: Sonnet

### 8.2 Add `just check-fmt` to CI pipeline

Similarly, formatting could regress. `just check-fmt` should be a CI check.

**Effort**: Small
**Priority**: High — prevents formatting regressions
**Model**: Sonnet

### 8.3 Pin GitHub Actions versions

The CI workflows should use pinned action versions (SHA or tag) to prevent
supply chain issues from compromised actions.

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

### 9.2 Validate environment variable types at startup

`constants.py` reads environment variables with string defaults but doesn't validate
them. Invalid values (e.g., `RSYNC_CONCURRENCY=abc`) would cause runtime crashes
instead of clear startup errors.

**Fix**: Add validation in `constants.py` with clear error messages, or create a
`validate_config()` function called from `cli.py:async_main()`.

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

### 10.2 Batch nix path-info calls in copy_to_cache

`copy_to_cache()` makes two sequential `nix path-info` calls (regular + derivation).
These could run concurrently with `asyncio.gather()`.

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
