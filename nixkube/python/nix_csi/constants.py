import os
from asyncio import Semaphore
from importlib import metadata
from pathlib import Path

CSI_PLUGIN_NAME = "nix.csi.store"
CSI_VENDOR_VERSION = metadata.version("nix-csi")

# Exit code from mount command when target is already mounted
MOUNT_ALREADY_MOUNTED = 32

# Paths we base everything on.
# Remember that these are CSI pod paths not node paths.
NIX_ROOT = Path("/")
CSI_ROOT = NIX_ROOT / "nix/var/nix-csi"
CSI_VOLUMES = CSI_ROOT / "volumes"
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
