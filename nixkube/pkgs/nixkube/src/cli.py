# SPDX-License-Identifier: MIT

import asyncio
import json
import logging
import logging.config
from pathlib import Path

from .constants import (
    BUILDERS_ENABLED,
    CACHE_ENABLED,
    ENABLE_COMPAT_DRIVER,
    HOST_MOUNT_PATH,
    KUBE_NODE_NAME,
    NAMESPACE,
    NIX_BUILD_TIMEOUT,
    NRI_ENABLED,
    NRI_PLUGIN_IDX,
    NRI_PLUGIN_NAME,
    RSYNC_CONCURRENCY,
    VERIFY_STORE_PATHS,
)
from .csi.server import csi_serve
from .nri.server import nri_serve


def log_effective_log_config() -> None:
    """Print the effective logging configuration for debugging.

    Uses print() instead of logger.info() to ensure output is always visible
    regardless of configured log levels.
    """
    root = logging.getLogger()

    lines = ["Effective logging configuration:"]
    lines.append(f"  Root logger: level={logging.getLevelName(root.level)}")

    # Collect all configured loggers
    for name in sorted(logging.Logger.manager.loggerDict.keys()):
        log = logging.getLogger(name)
        if log.level != logging.NOTSET:
            lines.append(f"  Logger '{name}': level={logging.getLevelName(log.level)}")

    # Document handlers on root logger
    if root.handlers:
        lines.append("  Root handlers:")
        for handler in root.handlers:
            handler_info = f"    {type(handler).__name__}"
            if hasattr(handler, "level"):
                handler_info += f" level={logging.getLevelName(handler.level)}"
            if hasattr(handler, "formatter") and handler.formatter:
                handler_info += f" format='{handler.formatter._fmt}'"
            lines.append(handler_info)

    print("\n".join(lines))


def log_effective_app_config() -> None:
    """Print the effective application configuration for debugging.

    Uses print() instead of logger.info() to ensure output is always visible
    regardless of configured log levels.
    """
    print(
        "Effective application configuration:\n"
        f"  {NIX_BUILD_TIMEOUT=}\n"
        f"  {RSYNC_CONCURRENCY=}\n"
        f"  {CACHE_ENABLED=}\n"
        f"  {BUILDERS_ENABLED=}\n"
        f"  {VERIFY_STORE_PATHS=}\n"
        f"  {HOST_MOUNT_PATH=}\n"
        f"  {NRI_PLUGIN_NAME=}\n"
        f"  {NRI_PLUGIN_IDX=}\n"
        f"  {NAMESPACE=}\n"
        f"  {KUBE_NODE_NAME=}"
    )


async def async_main():
    """Start CSI and NRI servers with configured logging.

    Loads logging configuration from /etc/nix/logging.json (ConfigMap-mounted) and starts
    the CSI gRPC server(s) and optionally the NRI plugin server based on environment flags
    (ENABLE_COMPAT_DRIVER, NRI_ENABLED).
    """
    # Configurable via kubenix option: loggingConfig
    # Mounted to /etc/nix/logging.json via ConfigMap
    logging_config_path = Path("/etc/nix/logging.json")

    if logging_config_path.exists():
        # Load logging config from file
        with open(logging_config_path) as f:
            config_dict = json.load(f)
        logging.config.dictConfig(config_dict)
        logger = logging.getLogger("nixkube")
        logger.info(f"Loaded logging config from {logging_config_path}")
    else:
        # Fallback to basic config if file doesn't exist
        # Root logger at WARN to suppress noise from libraries (grpclib, kr8s, etc.)
        # Only nixkube logger uses INFO level to avoid log spam from dependencies
        logging.basicConfig(
            level=logging.WARN,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
        logger = logging.getLogger("nixkube")
        logger.setLevel(logging.INFO)
        logger.info("Using fallback logging config (nixkube: INFO, root: WARN)")

    log_effective_log_config()
    log_effective_app_config()

    logger.info(f"NRI plugin: {'enabled' if NRI_ENABLED else 'disabled'}")
    logger.info(
        "CSI drivers: nixkube"
        + (" + nix.csi.store (compat)" if ENABLE_COMPAT_DRIVER else "")
    )

    try:
        tasks = [
            csi_serve(plugin_name="nixkube", socket_path=Path("/csi/nixkube/csi.sock"))
        ]
        if ENABLE_COMPAT_DRIVER:
            tasks.append(
                csi_serve(
                    plugin_name="nix.csi.store",
                    socket_path=Path("/csi/nix.csi.store/csi.sock"),
                )
            )
        if NRI_ENABLED:
            tasks.append(nri_serve())
        await asyncio.gather(*tasks)
    except Exception as e:
        logger.critical(f"CSI service failed: {e}", exc_info=True)
        raise


def main():
    """Entry point for the nixkube daemon."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
