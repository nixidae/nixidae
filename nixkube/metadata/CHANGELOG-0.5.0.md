# nixkube 0.5.0 Release Changelog

## Summary
Release 0.5.0 introduces a revolutionary new **NRI (Node Resource Interface) plugin system** for automatic Nix store injection alongside the existing CSI driver. This release also includes a complete project rebranding (nix-csi → nixkube), major code refactoring for maintainability, comprehensive testing infrastructure, and hardened deployment reliability.

## 🆕 NEW: NRI (Node Resource Interface) Plugin System

**What is NRI?** The NRI plugin system is a containerd-native mechanism for injecting resource initialization hooks into containers at the container runtime level. Unlike CSI (which requires explicit pod volume requests), NRI plugins automatically intercept container creation and can inject resources based on pod annotations or other criteria.

### Why NRI Matters for nixkube
Previously, nixkube only offered CSI—requiring developers to explicitly request Nix store mounts via volumeAttributes. With NRI, you can:
- **Automatic injection**: Annotate pods to automatically get Nix store access without explicit volume requests
- **Runtime-level control**: Inject mounts directly at container creation time (before the container filesystem is finalized)
- **Simpler pod specs**: No need to configure volumeAttributes if using NRI annotations
- **Better integration**: Works seamlessly with containerd's lifecycle management

### NRI Implementation (NEW in 0.5.0)

#### Core NRI Plugin
- **Separate NRI protocol layer into grpclib-nri package** - Standalone, reusable NRI/ttrpc implementation
- **Add pytest integration tests for grpclib-nri NRI plugin** - Protocol validation with test coverage
- **Implement NRI container garbage collection in StopContainer** - Clean up stale volumes when containers exit
- **Move NRI cleanup from StopContainer to StateChange REMOVE_CONTAINER event** - Proper lifecycle management

#### Advanced Mount Operations (NEW)
- **Add RW overlayfs support for /nix via fsopen/fsconfig/fsmount** - Modern kernel API for writable mounts (Linux 5.13+)
- **Replace OCI bind mount injection with open_tree/move_mount for /nix** - Efficient mount tree operations
- **Implement in-container bind mounts via setns+chroot** - Container namespace isolation for mounts
- **Add file:/dir: type prefix to FHS mount annotations** - Fix EEXIST on file-typed bind mounts

#### Container Runtime Integration (NEW)
- **Implement CRI socket discovery via kubelet API server proxy** - Dynamic CRI endpoint discovery
- **Implement CRI ListContainers for garbage collection** - Discover running containers for cleanup
- **Verify CRI connectivity at NRI startup** - Early error detection for misconfiguration
- **Implement kernel capability detection with @cache** - Validate system capabilities at startup
- **Add kernel version checks for NRI mount operations** - Compatibility checking for new syscalls

#### NRI Pod Annotation Parsing (NEW)
- **Extract NRI annotation parsing into dedicated module** - Cleaner annotation handling
- **Implement FHS mount annotation parsing and filtering** - Extract mounts from pod annotations
- **Extract build args into shared utility** - Share build logic between CSI and NRI
- **Extract store paths from container environment and args** - Discover requested Nix paths

#### NRI Build Coordination (NEW)
- **Implement Phase 2: ZeroMQ-based NRI build coordination** - Async build task tracking via ZeroMQ
- **Refactor ZeroMQ server into separate module** - Dedicated coordination service
- **Implement build progress keep-alive with rolling timeout** - Prevent timeout during long builds
- **Implement event reporting for NRI plugin** - Kubernetes event integration for build status
- **Implement NRI multi-system support with comprehensive unit tests** - ARM64 and x86_64 support

#### NRI Error Handling & Logging
- **Add nri_error_handler decorator for consistent error handling** - Unified error handling across NRI
- **Convert NRI server logging to f-strings and remove function prefixes** - Clean, fast logging
- **Enhance StateChange logging with pod and container context** - Better observability
- **Add namespace to NRI CreateContainer debug log** - Improved debugging
- **Move ZeroMQ server to NRI package and refactor logging** - Better module organization

### How to Use NRI in 0.5.0
Add the `nixkube/storepaths` annotation to your pod to automatically inject Nix store access:

```yaml
apiVersion: v1
kind: Pod
metadata:
  annotations:
    nixkube/storepaths: |
      /nix/store/abc123.../hello
      /nix/store/xyz789.../python3
```

The NRI plugin will:
1. Intercept container creation
2. Parse the storepaths annotation
3. Prepare/build the requested packages
4. Mount them into the container's `/nix` namespace
5. Report events on build success/failure

## ⚙️ Infrastructure & Deployment

### Kubernetes DaemonSet Improvements
- **Single DaemonSet with JSON NODE_ENV** - Consolidated per-system deployments into single DaemonSet with nodeSelector and direct store paths
- **Per-system DaemonSets with direct store paths** - Direct NODE_ENV store path instead of fragile `${!ARCH}` indirect expansion
- **Split CI per-arch builds** - Optimized architecture-specific builds to avoid cross-compilation overhead

### Binary Cache & Substitution
- **Sign store paths before copying to cache** - Added cryptographic signing to ensure cache integrity
- **Limit kubenixPush to currentSystem** - Avoid unnecessary cross-compilation when pushing to cache
- **Remove ConfigMap.push** - Transformer already preserves context when push=true, simplified configuration

### Nix Builder Management
- **Disable nixbuild.net builders** - Removed external builder dependency for more reliable builds

### Configuration Management
- **Move nixkube/discard annotation to resource level** - Allows transformer to properly strip string context
- **Resolve NODE_ENV key using system at image-build time** - Fixed shellcheck errors and improved build determinism

## 🔧 Code Quality & Refactoring

### Code Organization
- **Organize CSI code into dedicated csi/ package** - Separated CSI protocol handlers into structured package
- **Split nix.py into focused modules in nix/ package** - Broke up monolithic 416-line nix.py:
  - `build.py` - Package building
  - `closure.py` - Store path closures
  - `database.py` - Nix DB initialization
  - `gc.py` - Garbage collection
  - `system.py` - System detection (cached)
  - `verify.py` - Store path verification
- **Move ns_mount.py into nri/ package** - NRI-specific namespace mounting utilities

### Subprocess Management
- **Refactor subprocessing.py to use shellous** - Modern async subprocess handling with comprehensive test coverage
- **Refactor init_database to use shellous** - Cleaner piped command execution
- **Replace nix_init_db bash script with async subprocess piping** - Python-native implementation

### System Calls & Performance
- **Replace mount/umount subprocess calls with direct syscalls** - Eliminated subprocess overhead for mount operations
- **Drop is_mount async: /proc/self/mounts is virtual, sync read is correct** - Fixed unnecessary async I/O
- **Replace aiofiles with synchronous reads and remove dependency** - Simplified I/O handling

### Logging & Error Handling
- **Convert remaining %-formatting log lines to f-strings** - Consistency across codebase (f-strings are faster and cleaner)
- **Add module-specific loggers for all utility modules** - Better debugging granularity
- **Rename zmq_server.py to zmq.py and update imports** - Simplified module naming

### Type System
- **Replace Optional[X] with X | None** - Modern Python type hints (3.10+)
- **Improved type annotations throughout** - Better type safety and IDE support
- **Add cri_channel context manager for safe gRPC channel cleanup** - Proper resource management

### Code Cleanup
- **Remove unused BuildError aliases** (PathBuildError, FlakeBuildError, ExprBuildError)
- **Remove unused RemoveVolumeDirError** - Dead code removal
- **Remove unused models.py** (PodInfo dataclass)
- **Remove unused nix_cache package**
- **Remove unused nix-csi-validpaths-monitor**
- **Flatten double exception wrapping in init_database** - Simplified error handling
- **Remove dead logger = None in nix/system.py** - Code cleanup
- **Fix logger name mismatch in subprocessing.py** - Consistency

## 🔧 CSI Driver Improvements & Hardening

### CSI Mount Operations
- **Harden unmount: always verify mount is gone, fail NodeUnpublishVolume if not** - Robust cleanup verification
- **Fix CSI bind mount not enforcing read-only flag** - Proper read-only enforcement
- **Unify overlayfs volume subpaths** - Consistent overlay structure
- **Fix CSI NodeUnpublishVolume to not block on kubelet directory removal** - Non-blocking cleanup
- **Add path existence checks and diagnostic logging to CSI mount operations** - Better error diagnosis
- **Enhance NodePublishVolume log message with pod context** - More informative CSI logs

### CSI Security & Robustness
- **Use statically linked coreutils for OCI hook chroot execution** - Minimal dependencies
- **Fix runtime TypeError: memoryview is not subscriptable in Python 3.13** - Python 3.13 compatibility

## 🏗️ Build Infrastructure

### Multi-System Support
- **Implement NRI multi-system support with comprehensive unit tests** - ARM64 and x86_64 support
- **Add configurable systems option for conditional cross-builds** - Flexible architecture selection

### Testing Infrastructure
- **Add NixOS integration test infrastructure for kubeadm + containerd** - Real cluster testing
- **Add nixos tests evaluation** - Test framework setup
- **Disable aarch64-linux in nixos tests evaluation** - x86_64 focus for CI
- **Add Quad9 DNS resolver to nixos test VM** - Reliable DNS for tests
- **Configure workflows for cidev branch isolation** - CI workflow organization
- **Enable networking for nixos test VM** - Network-dependent test support
- **Add networking debug checks to nixos test** - Diagnostic logging
- **Use macvtap for direct external network access in nixos tests** - Network isolation
- **Add iptables NAT wrapper for nixos tests** - Network bridging
- **Add SSH port-forwarded test driver wrapper via QEMU_NET_OPTS** - SSH debugging
- **Add comprehensive debug output for GHA network state and VM networking** - GitHub Actions debugging
- **Enable OpenSSH in nixos test VM and fix debug output visibility** - SSH debugging
- **Run test in background with upterm for parallel debugging** - Interactive debugging
- **Simplify nixosTest networking and add sshBackdoor for interactive debugging** - Better test UX
- **chmod /dev/vhost-vsock in test wrapper for sshBackdoor access** - vsock permissions

### Build Tools & Dependencies
- **Fix grpclib-ttrpc AF_UNIX socket path too long error in Nix builds** - Path length compatibility
- **Fix buildGoModule for grpclib-ttrpc and grpclib-nri test servers** - Go tooling integration
- **Fix grpclib-ttrpc test suite compatibility with ttrpc proto** - Proto compatibility

## 🧪 Testing

### New Test Coverage
- **Add comprehensive test coverage for grpclib-ttrpc**:
  - Streaming integration tests
  - Error handling tests
  - Empty/null message handling
  - Timeout/deadline tests
  - Go test server fixtures
- **Add pytest integration tests for grpclib-nri NRI plugin** - NRI protocol validation
- **Add unit tests for store.py pure functions** - Store path utilities
- **Add unit tests for events.py truncation logic** - Event handling verification
- **Add unit tests for hardlinks.py symlink handling** - Filesystem utilities
- **Add NRI CI tests for hello RO and RW** - Integration test coverage

### Test Infrastructure
- **Add pytest to dev shell and fix final type checker issues** - Test environment setup
- **Implement streaming Client API for grpclib-ttrpc with full test coverage** - Protocol testing

## 📦 Project Renaming & Branding

### nix-csi → nixkube Migration
- **Rename project to nixkube** - Complete rebranding:
  - pkgs/nix-csi → pkgs/nixkube
  - nix_csi Python package → src
  - All documentation and annotations updated
- **Update all references from nix-csi to nixkube** - Consistent naming throughout
- **Rename CSI driver from nix.csi.store to nixkube** - New primary driver name
- **Support both nixkube and nix.csi.store CSI drivers in single Python process** - Backwards compatibility
- **Add backwards compatibility with mkRenamedOptionModule** - Seamless module migration
- **Change default namespace from nix-csi to nixkube** - Clean defaults
- **Rename environment names from nix-csi-* to nixkube-*** - Consistent naming
- **Rename nix-csi/discard annotation to nixkube/discard** - Updated annotations
- **Move NRI wait sockets to /nix/var/nixkube** - Consistent path organization
- **Enhance README with NRI examples** - Improved documentation

## 🛠️ Developer Experience

### Just Recipes
- **Add justfile with common development recipes** - Quick access to common tasks:
  - `just fmt` - Format all code
  - `just lint` - Type checking
  - `just test` - Run tests
  - `just build-*` - Build targets
  - `just deploy` - Kubernetes deployment
  - `just hetzkube` - Deploy to Hetzkube
  - `just testpod` - Test pod deployment

### Code Formatting
- **Add typos formatter** - Spell checking
- **Add yamlfmt formatter** - YAML formatting
- Complete treefmt configuration with 8 formatters

### CI/CD
- **Update CI to use nixkube namespace and document Cachix migration** - CI improvements
- **Fix CI test job volumeMounts to reference nixkube volume** - CI fixes
- **Fix serviceAccountName in cache and init job** - RBAC corrections
- **Add host root mount and CRI socket discovery permissions** - Pod security
- **Add separate just entries for local vs nixbuild** - Build flexibility
- **Update meta.mainProgram from nix-csi to nixkube** - Package metadata
- **Print logging config to stdout instead of logger** - Better logging visibility
- **Enable CI on all branches** - Broader testing

## 📊 Performance & Optimization

### Build Caching & Coordination
- **Cache get_build_args with 30s TTL to avoid redundant API calls** - Reduced API pressure and faster builds
- **Consolidate ZeroMQ container state into single TTLCache** - Simplified state management for build tracking
- **Rename build_packages to fetch_packages and remove misleading return value** - Clearer API semantics
- **Fix cache copy lock to use frozenset key instead of first path** - Correct locking for concurrent builds
- **Remove double closure resolution in NRI build task** - Eliminated redundant computation

### Error Handling & Cleanup
- **Extract schedule_copy_to_cache helper to deduplicate fire-and-forget pattern** - Code reuse and maintainability
- **Extract nri_error_handler decorator for consistent error handling** - Unified error handling across NRI
- **Fix build task done_callback to distinguish success/failure** - Better state tracking for async operations
- **Fix unused variable warnings in struct.unpack_from** - Code cleanup

## 🔐 Security & Robustness

### Hardening
- **Use statically linked coreutils for OCI hook chroot execution** - Minimal dependencies and better isolation
- **Add kernel version checking for new mount API syscalls** - Compatibility validation for Linux 5.13+ features

## 🐛 Bug Fixes & Stability

### CSI Driver
- **Fix runtime TypeError: memoryview is not subscriptable in Python 3.13** - Python 3.13 compatibility
- **Fix selector.matchLabels to not include version label** - Kubernetes selector fixes

### Error Handling & Logging
- **Make loggingConfig mergeable with defaults** - Flexible logging configuration
- **Guard hex formatting in debug logging behind isEnabledFor check** - Performance optimization
- **Replace assertions and clean up error handling in grpclib_ttrpc** - Proper error handling
- **Remove cleanup_container_volume, rely on garbage_collect_stale_volumes** - Unified cleanup
- **Remove emit_event_for_exception, redundant with csi_error_handler decorator** - Eliminated duplication
- **Enhance StateChange logging with pod and container context** - Better observability
- **Remove redundant CRI container discovery log message** - Reduced noise

### Code Cleanup
- **Move NRI cleanup from StopContainer to StateChange REMOVE_CONTAINER event** - Proper lifecycle
- **Flatten CSI compatibility option to nixkube.node.compat** - Simplified configuration
- **Document NRI pod creation lifecycle in server.py** - Better documentation
- **Clean up unused NRI handler skeletons with explanatory comments** - Code clarity
- **Simplify NRI plugin mapping initialization** - Cleaner initialization
- **Rename ZeroMQ server and refactor logging** - Better module organization
- **More info in "findstuckpods"** - Enhanced debugging
- **Deduplicate mount flag constants into constants.py** - Code organization
- **Simplify nixosTest networking** - Cleaner test code
- **Remove mkNCSI and apply labels explicitly** - Explicit resource labeling
- **Reword cache copy log message for clarity** - Better messaging
- **Fix stream buffer error/EOF ordering race condition** - Race condition fix
- **Use stream IDs instead of stream objects as task dict keys** - Cleaner data structures
- **Fix proto imports and type checking** - Proto compatibility
- **Use relative imports for test modules** - Proper module structure
- **Extract ttrpc protos into standalone ttrpc-proto-python package** - Reusable components
- **Move ttRPC into standalone grpclib-ttrpc package** - Modular design
- **Switch NRI plugin to pre-connected mux registration** - Improved registration
- **Use single local grpclib override for both csi-proto-python and nri-proto-python** - Reduced duplication
- **Move system detection to startup via cached get_current_system** - Cached initialization
- **Add HOST_ROOT constant and use it consistently** - Consistent path handling

## 📝 Documentation

### README Updates
- **Update README from nix-csi to nixkube** - Renamed examples
- **Enhance README with NRI examples and lib.getExe tip** - Better documentation
- **Update nri-wait pyproject.toml authors** - Attribution updates
- **Update nix-csi references to nixkube in options.nix descriptions** - Consistent naming
- **Update remaining nix-csi references** - Complete migration

## Version
- **Update nixkube to version 0.5.0**
- **Add typos and yamlfmt to treefmt** - Enhanced code quality

---

## Migration Guide

### For Existing Users
If you're upgrading from 0.4.3:

1. **Namespace Change**: Default namespace changed from `nix-csi` to `nixkube`
   - Old configs still work via backwards-compatible module rename
   - Update your kubenix configs to use `nixkube` instead of `nix-csi`

2. **CSI Driver Name**: Primary driver changed from `nix.csi.store` to `nixkube`
   - Old driver name still supported for backwards compatibility
   - Update volumeAttributes to use new driver name

3. **Annotations**: Pod annotations renamed from `nix-csi/*` to `nixkube/*`
   - Enable compatibility mode with `nixkube.node.compat` if needed

### No Breaking Changes
- All functionality is backwards compatible
- Existing deployments will continue to work
- Upgrade at your convenience

---

**Release Date**: March 6, 2026
