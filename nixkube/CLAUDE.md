# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Version Control

**Always use Jujutsu (jj) instead of Git** for all version control operations in this repository.

**Format code before committing**: Run `just fmt` before any `jj describe` or `jj commit` to ensure all code (Nix, Python, YAML, etc.) is properly formatted according to project standards.

## Project Overview

nix-csi is a Kubernetes CSI (Container Storage Interface) driver that mounts `/nix` stores into pods using ephemeral volumes. The system consists of:

1. **Node DaemonSet** - Runs on each Kubernetes node, implements the CSI driver protocol to mount Nix stores into pods
2. **Cache StatefulSet** - Central cache/coordinator that manages distributed builds and binary substitution
3. **Python Services** - Three main services packaged together:
   - `nix-csi`: CSI driver implementation (gRPC server)

## Architecture

### Build System

The project uses Nix with flake-compatish for backwards compatibility. Key build outputs are defined in `default.nix`:

- **Environments** (`environments/`): Separate Nix environments for cache and node, built using dinix (a service manager). Each environment:
  - Shares common services (openssh, nix-daemon, shared-setup)
  - Has role-specific services defined in separate modules
  - Builds for both x86_64-linux and aarch64-linux architectures
  - Is deployed as a minimal container with services managed by dinit

- **Kubernetes Deployment** (`kubenix/`): Uses easykubenix to generate Kubernetes manifests
  - `options.nix`: Defines module options and builds separate `cachePackage` and `nodePackage`
  - `cache.nix`: Cache StatefulSet with initContainer that copies environment artifacts
  - `daemonset.nix`: Node DaemonSet with CSI driver registration
  - SSH keys from `./keys/*.pub` are automatically imported as authorized keys

### Communication Flow

**Cache → Nodes**: The cache service watches for pods labeled `app=nix-csi-node` and updates `/etc/machines` with builder DNS names (`pod.name.nix-builders.namespace.svc.cluster.local`). This enables distributed builds.

**Nodes → Cache**: Node pods use the cache as a binary substitute via `ssh-ng://nix@nix-cache?trusted=1` configured in `kubenix/config.nix`.

**CSI Protocol**: When a pod requests a volume with `storePath` or `nixExpr` or `flakeRef` in volumeAttributes, the node CSI driver:
1. Builds/fetches the requested Nix store path
2. Copies artifacts to the cache (if configured)
3. Mounts the store path into the pod using bind mounts

### Python Service Architecture

The Python services use:
- `grpclib` for async gRPC (CSI protocol implementation)
- `kr8s` for Kubernetes API interactions
- `csi-proto-python` for CSI protobuf definitions (generated from upstream spec)

All three services are packaged together in `python/` with a single `pyproject.toml`.

## Common Commands

Use the `justfile` for common development tasks. Run `just --list` to see all available recipes.

### Code Quality

**Format code before committing:**
```bash
just fmt
```

**Check formatting without changes:**
```bash
just check-fmt
```

**Run Python type checker:**
```bash
just lint
```

### Testing

**Run Python tests:**
```bash
just test
```

The integration test (runs in CI via `.github/workflows/integration-test.yaml`):
- Checks `/nix/store` is accessible in test pods
- Validates CSI driver registration
- Confirms cache and node pods are operational

**Build job** (runs once, pushes to cachix and container registry):
1. Builds and pushes Lix image
2. Builds and pushes cache/node environments
3. Builds and pushes scratch image

**Test jobs** (can run in parallel, pull from caches):
- `test-kind`: Tests deployment on Kind cluster using `kubenixApply` with `local="true"`
- Future test jobs can be added for different deployment scenarios (e.g., different K8s versions, configurations)

### Building

**Build Kubernetes manifests:**
```bash
just build-manifests
```

**Build development environment:**
```bash
just build-dev
```

**Build all outputs for both architectures (requires builders):**
```bash
just build-all
```

### Deployment

**Deploy to Kubernetes cluster** (reads SSH keys from `./keys/*.pub`):
```bash
just deploy
```

**Push to cachix and container registry:**
```bash
just push
```

### Development Environment

The development environment includes:
- Python with nix-csi, csi-proto-python, kr8s
- xonsh shell
- ruff, pyright (linting/type checking)
- kluctl, stern, kubectx (Kubernetes tools)
- buildah, skopeo, regctl (container tools)
- just (for running recipes)

Enter the environment with `direnv allow && direnv reload` (or open a new terminal).

### Python Development

The Python code is in `python/` with three packages:
- `python/nix_csi/` - CSI driver (main entry: `service.py`)
- `python/nix_cache/` - Cache manager (main entry: `cli.py`)
- `python/nix_timegc/` - Garbage collector (main entry: `cli.py`)

Version is managed in `python/pyproject.toml` and automatically imported into the Nix build.

## Key Configuration Points

### Volume Attributes

Pods request Nix stores via CSI volumeAttributes (see README for examples):
- `storePath`: Direct Nix store path to mount
- `nixExpr`: Nix expression to evaluate and mount
- `flakeRef`: Flake reference to build and mount

### Service Dependencies (dinit)

Both environments use dinit for service management with dependency chains:
- Cache: `shared-setup` → `nix-daemon` → `cache-daemon` → `cache-logger` → `cache` (umbrella)
- Node: `shared-setup` → `nix-daemon` → `csi-gc` → `csi-daemon` → `csi-logger` → `csi` (umbrella)

The `config-reconciler` service runs continuously to sync SSH keys and Nix config from mounted volumes to runtime locations.

## Important Files

- `default.nix`: Main entry point, defines all build outputs
- `environments/cache/default.nix`: Cache environment with shared + cache services
- `environments/node/default.nix`: Node environment with shared + CSI services
- `kubenix/options.nix`: Kubernetes module options and package builds
- `python/nix_csi/service.py`: CSI NodeServicer gRPC implementation
- `python/nix_cache/cli.py`: Cache service that maintains Nix machines file
- `liximage.nix`: Builds the Lix container used by initContainers

## Code Review Standards

**The code should be approachable for both AI and beginners.** When reviewing or modifying code, follow these guidelines for comments:

### When Comments Are Useful
- ✅ **Non-obvious design decisions**: Why a particular approach was chosen over alternatives
- ✅ **Complex algorithms**: Explain the logic when it's not immediately clear from the code
- ✅ **Protocol requirements**: CSI interface requirements, Kubernetes API quirks, Nix behavior
- ✅ **Error handling philosophy**: Why fail-fast vs. graceful degradation (see `NodeUnpublishVolume` for good example)
- ✅ **Performance implications**: Why bind mounts vs. overlayfs, concurrency limits, retry strategies
- ✅ **Footguns and gotchas**: Things that could easily be misunderstood or break

### When Comments Are Noise (Avoid These)
- ❌ **Restating what's already obvious**: Variable names, function names, and code structure should be self-documenting
- ❌ **Duplicating log messages**: Log messages serve as inline documentation - don't repeat them in comments
- ❌ **Explaining standard patterns**: `exp_backoff`, `async with lock`, walrus operators, set comprehensions - trust the reader
- ❌ **Obvious control flow**: for-else with clear error logging, early returns, simple conditionals

### Testing Philosophy
- **Integration tests over unit tests**: This project orchestrates subprocess calls to Nix, rsync, mount, etc. Unit testing these would require excessive mocking and wouldn't catch real issues.
- **Test in real environments**: Use the GitHub Actions integration tests on actual Kind clusters to validate behavior.
- Unit tests are only valuable for pure business logic isolated from subprocess orchestration.
