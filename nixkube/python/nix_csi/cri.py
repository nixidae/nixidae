# SPDX-License-Identifier: MIT

import logging
import os
from pathlib import Path

import kr8s

logger = logging.getLogger("nix-csi")


async def get_cri_socket() -> Path:
    """
    Get the CRI socket path for the node via kubelet configz.

    Queries the kubelet's containerRuntimeEndpoint through the Kubernetes API server
    proxy (/api/v1/nodes/{node-name}/proxy/configz). This is the authoritative source
    of the container runtime configuration.

    Returns the CRI socket as a Path object (e.g., Path("unix:///var/run/containerd/containerd.sock"))

    Raises RuntimeError if unable to query the API server or parse the response.
    """
    node_name = os.environ.get("KUBE_NODE_NAME")
    if not node_name:
        raise RuntimeError("KUBE_NODE_NAME environment variable not set")

    try:
        api = kr8s.asyncio.Api()
        async with api.call_api(
            "GET",
            url=f"/api/v1/nodes/{node_name}/proxy/configz",
        ) as response:
            config = response.json()
            endpoint = config.get("kubeletconfig", {}).get("containerRuntimeEndpoint")

            if not endpoint:
                raise RuntimeError("containerRuntimeEndpoint not found in kubelet configz")

            logger.info("Discovered CRI socket: %s", endpoint)
            return Path(endpoint)

    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Failed to query kubelet configz for node {node_name}: {e}"
        ) from e
