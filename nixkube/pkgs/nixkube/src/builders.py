# SPDX-License-Identifier: MIT

import kr8s
import structlog

from .constants import BUILDERS_ENABLED, BUILDERS_SERVICE, NAMESPACE

logger = structlog.get_logger("nixkube.builders")


async def get_builder_uris() -> list[str]:
    """Query k8s API for builder pods, return list of SSH URIs for --builders flag."""
    if not BUILDERS_ENABLED:
        return []

    try:
        # Use kr8s to query pods with label selector
        # kr8s.asyncio.get() returns an async generator, iterate with async for
        uris = []
        async for pod in kr8s.asyncio.get(
            "pods", namespace=NAMESPACE, label_selector="app.kubernetes.io/name=builder"
        ):
            try:
                if pod["status"]["phase"] == "Running":
                    pod_name = pod["metadata"]["name"]
                    uri = f"ssh-ng://nix@{pod_name}.{BUILDERS_SERVICE}.{NAMESPACE}.svc.cluster.local"
                    uris.append(uri)
            except (KeyError, TypeError):
                # Skip pods with missing or malformed metadata
                continue

        logger.debug("discovered_builders", count=len(uris), uris=uris)
        return uris
    except Exception:
        logger.warning("builder_discovery_failed", exc_info=True)
        return []


def build_builder_args(uris: list[str]) -> list[str]:
    """Build nix command arguments for using builder pods."""
    if not uris:
        return []

    args = ["--max-jobs", "0"]
    for uri in uris:
        args.extend(["--builders", uri])
    args.append("--builders-use-substitutes")
    return args
