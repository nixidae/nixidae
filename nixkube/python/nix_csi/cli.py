import asyncio
import logging
import argparse
from . import service


def parse_args():
    parser = argparse.ArgumentParser(description="nix CSI driver")
    parser.add_argument(
        "--loglevel",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)",
    )
    return parser.parse_args()


async def async_main():
    args = parse_args()
    # Root logger at WARN to suppress noise from libraries (grpclib, kr8s, etc.)
    # Only nix-csi logger uses user-specified level to avoid log spam from dependencies
    # TODO: Consider config file based logging (e.g., logging.yaml) for more granular
    # per-logger control instead of command-line args. Would be more flexible and
    # serializable for deployment configs.
    logging.basicConfig(
        level=logging.WARN,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger = logging.getLogger("nix-csi")
    loglevel_str = logging.getLevelName(logger.getEffectiveLevel())
    logger.info(f"Current log level: {loglevel_str}")

    logging.getLogger("nix-csi").setLevel(getattr(logging, args.loglevel))

    await service.serve()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
