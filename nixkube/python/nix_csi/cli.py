# SPDX-License-Identifier: MIT

import asyncio
import json
import logging
import logging.config
from pathlib import Path

from . import nriplugin, service


def log_effective_config() -> None:
    """Log the effective logging configuration for debugging."""
    logger = logging.getLogger("nix-csi")
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

    logger.info("\n".join(lines))


async def async_main():
    # Configurable via kubenix option: loggingConfig
    # Mounted to /etc/nix/logging.json via ConfigMap
    logging_config_path = Path("/etc/nix/logging.json")

    if logging_config_path.exists():
        # Load logging config from file
        with open(logging_config_path) as f:
            config_dict = json.load(f)
        logging.config.dictConfig(config_dict)
        logger = logging.getLogger("nix-csi")
        logger.info(f"Loaded logging config from {logging_config_path}")
    else:
        # Fallback to basic config if file doesn't exist
        # Root logger at WARN to suppress noise from libraries (grpclib, kr8s, etc.)
        # Only nix-csi logger uses INFO level to avoid log spam from dependencies
        logging.basicConfig(
            level=logging.WARN,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
        logger = logging.getLogger("nix-csi")
        logger.setLevel(logging.INFO)
        logger.info("Using fallback logging config (nix-csi: INFO, root: WARN)")

    log_effective_config()

    try:
        await asyncio.gather(service.serve(), nriplugin.nri_serve())
    except Exception as e:
        logger.critical(f"Service failed: {e}", exc_info=True)
        raise


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
