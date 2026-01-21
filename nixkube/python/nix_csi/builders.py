import kr8s
import logging

from .constants import BUILDERS_ENABLED, BUILDERS_SERVICE, NAMESPACE

logger = logging.getLogger("nix-csi")


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
            if pod.status.phase == "Running":
                pod_name = pod.metadata.name
                uri = f"ssh://nix@{pod_name}.{BUILDERS_SERVICE}.{NAMESPACE}.svc.cluster.local"
                uris.append(uri)

        logger.debug(f"Discovered {len(uris)} builder pods: {uris}")
        return uris
    except Exception as e:
        logger.warning(f"Failed to discover builder pods: {e}")
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
