# nix-csi justfile

# Default recipe
default:
    @just --list

# Format all code (Nix, Python, YAML, etc.)
fmt:
    direnv exec . treefmt

# Run Python tests
test:
    direnv exec . python -m pytest python/tests -v

# Build all outputs for both architectures (requires builders)
build-all:
    nix build --builders "eu.nixbuild.net aarch64-linux; eu.nixbuild.net x86_64-linux" --file . push --no-link

# Build manifests only
build-manifests:
    nix build --file . kubenixApply.manifestJSONFile

# Build development environment
build-dev:
    nix build --file . repoenv

# Push to cachix and registry (builds all architectures)
push:
    nix run --builders "eu.nixbuild.net aarch64-linux; eu.nixbuild.net x86_64-linux" --file . push

# Deploy to Kubernetes cluster
deploy:
    nix run --file . kubenixEval.deploymentScript -- --yes --prune

# Run linter/type checker on Python code
lint:
    direnv exec . pyright python/nix_csi

# Check formatted code without changes
check-fmt:
    direnv exec . treefmt --fail-on-change

# Deploy to Hetzkube
hetzkube:
    direnv exec ~/Code/hetzkube nix run --show-trace --file ~/Code/hetzkube kubenix.deploymentScript --argstr stage full -- --write-command-result=false --prune --yes

# Deploy test pod
testpod:
    nix run --file tmp/testpod.nix deploymentScript -- --prune --yes --force-replace-on-error
