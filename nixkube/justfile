# nixkube justfile

# Default recipe
default:
    @just --list

# Format all code (Nix, Python, YAML, etc.)
fmt:
    direnv exec . treefmt

# Run Python tests
test:
    direnv exec . python -m pytest pkgs/nixkube/tests -v

# Build manifests only (local, fast)
build-manifests:
    nix build --file . kubenixApply.manifestJSONFile

# Build all outputs for both architectures (local, uses cache)
build-local:
    nix build --file . push --no-link

# Build all outputs for both architectures (remote nixbuild builders, disabled)
build-nixbuild:
    nix build --file . push --no-link

# Alias for nixbuild (legacy)
build-all: build-nixbuild

# Build development environment
build-dev:
    nix build --file . repoenv

# Push to cachix and registry (local builds, uses cache)
push-local:
    nix run --file . push

# Push to cachix and registry (remote nixbuild builders, disabled)
push-nixbuild:
    nix run --file . push

# Alias for nixbuild (legacy)
push: push-nixbuild

# Deploy to Kubernetes cluster
deploy:
    nix run --file . kubenixEval.deploymentScript -- --yes --prune

# Run linter/type checker on Python code
lint:
    direnv exec . pyright pkgs/nixkube/src

# Check formatted code without changes
check-fmt:
    direnv exec . treefmt --fail-on-change

# Deploy to Hetzkube
hetzkube:
    direnv exec ~/Code/hetzkube nix run --show-trace --file ~/Code/hetzkube kubenix.deploymentScript --argstr stage full -- --write-command-result=false --prune --yes --force-replace-on-error

# Deploy test pod
testpod:
    nix run --file tmp/testpod.nix deploymentScript -- --prune --yes --force-replace-on-error

# Run NixOS integration test (requires KVM)
nixos-test:
    nix build --file . nixosTests.containerd --no-link --print-build-logs

# Run NixOS integration test interactively (opens test driver shell)
nixos-test-interactive:
    nix build --file . nixosTests.containerd.driverInteractive && ./result/bin/nixos-test-driver
