# SPDX-License-Identifier: MIT

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
RSYNC_CONCURRENCY = Semaphore(max(int(os.environ.get("RSYNC_CONCURRENCY", "1")), 1))

# Configurable via kubenix option: nodeBuildTimeout (default: 300)
# Set via NIX_BUILD_TIMEOUT environment variable
NIX_BUILD_TIMEOUT = float(os.environ.get("NIX_BUILD_TIMEOUT", "300"))

# Builder configuration
# Set via environment variables from kubenix when builders are enabled
BUILDERS_ENABLED = os.environ.get("BUILDERS_ENABLED", "false").lower() == "true"

NAMESPACE = os.environ.get("KUBE_NAMESPACE", "nix-csi")
BUILDERS_SERVICE = "nix-csi-builders"

# Simple string check is fine - value controlled by easykubenix (always "true" or "false")
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "false") == "true"

# Verify store paths before mounting to detect corruption early
# Set via VERIFY_STORE_PATHS environment variable
VERIFY_STORE_PATHS = os.environ.get("VERIFY_STORE_PATHS", "false") == "true"

# CSI socket path for gRPC server
CSI_SOCKET_PATH = os.environ.get("CSI_SOCKET_PATH", "/csi/csi.sock")

# NRI runtime socket — containerd's multiplex socket we connect to
NRI_RUNTIME_SOCKET = os.environ.get("NRI_RUNTIME_SOCKET", "/var/run/nri/nri.sock")

# NRI plugin identity — sent in RegisterPlugin; must match the index prefix
# that containerd expects (two-digit zero-padded number, e.g. "00").
NRI_PLUGIN_NAME = os.environ.get("NRI_PLUGIN_NAME", "nix-csi")
NRI_PLUGIN_IDX = os.environ.get("NRI_PLUGIN_IDX", "00")

# NRI host mount path for bind mounts (default: /var/lib/nix-csi)
# Set via HOST_MOUNT_PATH environment variable from kubenix
HOST_MOUNT_PATH = Path(os.environ.get("HOST_MOUNT_PATH", "/var/lib/nix-csi"))

# Statically linked chroot binary used to execute OCI hooks from HOST_MOUNT_PATH
COREUTILS_STATIC = Path(os.environ.get("COREUTILS_STATIC", "coreutils"))

# Host /proc mounted into the daemonset for accessing container namespaces.
# Set via HOST_PROC_PATH environment variable from kubenix.
HOST_PROC_PATH = os.environ.get("HOST_PROC_PATH", "/host/proc")

# Kubelet pods directory for discovering active volumes
KUBELET_PODS_PATH = Path("/var/lib/kubelet/pods")

# CSI pod metadata for event reporting (from downwardAPI)
# These are required at runtime but may be absent in test environments
KUBE_POD_NAME = os.environ.get("KUBE_POD_NAME", "unknown")
KUBE_POD_UID = os.environ.get("KUBE_POD_UID", "unknown")
KUBE_NODE_NAME = os.environ.get("KUBE_NODE_NAME", "unknown")
