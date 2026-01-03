import asyncio
import logging
import logging.config
import json
from pathlib import Path
from . import service


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

    try:
        await service.serve()
    except Exception as e:
        logger.critical(f"CSI service failed: {e}", exc_info=True)
        raise


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
