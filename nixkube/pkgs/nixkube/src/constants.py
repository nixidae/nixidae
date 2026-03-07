# SPDX-License-Identifier: MIT

"""Configuration constants and environment variable parsing.

This module is the single source of truth for all nixkube configuration. All environment
variables are read here with their defaults, and exported as module-level constants for
use throughout the application. Configuration is centralized to prevent scattered env var
reads and ensure consistent defaults across the codebase.
"""

import os
from asyncio import Semaphore
from importlib import metadata
from pathlib import Path

CSI_PLUGIN_NAME = "nixkube"
try:
    CSI_VENDOR_VERSION = metadata.version("nixkube")
except metadata.PackageNotFoundError:
    # When running tests or in development, package may not be installed
    CSI_VENDOR_VERSION = "dev"

# Exit code from mount command when target is already mounted
MOUNT_ALREADY_MOUNTED = 32

# Paths we base everything on.
# Remember that these are CSI pod paths not node paths.
NIX_ROOT = Path("/")
CSI_ROOT = NIX_ROOT / "nix/var/nix-csi"
CSI_VOLUMES = CSI_ROOT / "volumes"
NRI_CONTAINERS = CSI_ROOT / "containers"
CSI_GCROOTS = NIX_ROOT / "nix/var/nix/gcroots/nix-csi"

# Configurable via kubenix option: rsyncConcurrency (default: 1)
# Set via RSYNC_CONCURRENCY environment variable
RSYNC_CONCURRENCY_COUNT: int = max(int(os.environ.get("RSYNC_CONCURRENCY", "1")), 1)
RSYNC_CONCURRENCY: Semaphore = Semaphore(RSYNC_CONCURRENCY_COUNT)

# Configurable via kubenix option: nodeBuildTimeout (default: 300)
# Set via NIX_BUILD_TIMEOUT environment variable
NIX_BUILD_TIMEOUT = float(os.environ.get("NIX_BUILD_TIMEOUT", "300"))

# Builder configuration
# Set via environment variables from kubenix when builders are enabled
BUILDERS_ENABLED = os.environ.get("BUILDERS_ENABLED", "false").lower() == "true"

NAMESPACE = os.environ.get("KUBE_NAMESPACE", "nixkube")
BUILDERS_SERVICE = "nixkube-builders"

# Simple string check is fine - value controlled by easykubenix (always "true" or "false")
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "false") == "true"

# Whether to enable NRI plugin
# Set via NRI_ENABLED environment variable (default: true)
NRI_ENABLED = os.environ.get("NRI_ENABLED", "true") == "true"

# Whether to enable compatibility driver (nix.csi.store alongside nixkube)
# Set via ENABLE_COMPAT_DRIVER environment variable (default: false)
ENABLE_COMPAT_DRIVER = os.environ.get("ENABLE_COMPAT_DRIVER", "false") == "true"

# Verify store paths before mounting to detect corruption early
# Set via VERIFY_STORE_PATHS environment variable
VERIFY_STORE_PATHS = os.environ.get("VERIFY_STORE_PATHS", "false") == "true"

# CSI socket path for gRPC server
CSI_SOCKET_PATH = os.environ.get("CSI_SOCKET_PATH", "/csi/csi.sock")

# NRI runtime socket — containerd's multiplex socket we connect to
NRI_RUNTIME_SOCKET = os.environ.get("NRI_RUNTIME_SOCKET", "/var/run/nri/nri.sock")

# NRI plugin identity — sent in RegisterPlugin; must match the index prefix
# that containerd expects (two-digit zero-padded number, e.g. "69").
# Default "69" is high enough to avoid collision with early-stage plugins (00-50).
NRI_PLUGIN_NAME = os.environ.get("NRI_PLUGIN_NAME", "nixkube")
NRI_PLUGIN_IDX = os.environ.get("NRI_PLUGIN_IDX", "69")

# NRI host mount path for bind mounts (default: /var/lib/nix-csi)
# Set via HOST_MOUNT_PATH environment variable from kubenix
HOST_MOUNT_PATH = Path(os.environ.get("HOST_MOUNT_PATH", "/var/lib/nix-csi"))

# Statically linked chroot binary used to execute OCI hooks from HOST_MOUNT_PATH
COREUTILS_STATIC = Path(os.environ.get("COREUTILS_STATIC", "coreutils"))

# Host root filesystem mounted into the daemonset container.
HOST_ROOT = Path(os.environ.get("HOST_ROOT", "/host"))

# Host /proc mounted into the daemonset for accessing container namespaces.
HOST_PROC_PATH = str(HOST_ROOT / "proc")

# Kubelet pods directory for discovering active volumes
KUBELET_PODS_PATH = Path("/var/lib/kubelet/pods")

# CSI pod metadata for event reporting (from downwardAPI)
# These are required at runtime but may be absent in test environments
KUBE_POD_NAME = os.environ.get("KUBE_POD_NAME", "unknown")
KUBE_POD_UID = os.environ.get("KUBE_POD_UID", "unknown")
KUBE_NODE_NAME = os.environ.get("KUBE_NODE_NAME", "unknown")

# mount(2) flags (from sys/mount.h)
MS_RDONLY = 1
MS_REMOUNT = 32
MS_BIND = 4096
