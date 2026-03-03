# SPDX-License-Identifier: MIT

import logging
import os
from pathlib import Path

import grpclib.client
import kr8s
from cri import cri_grpc, cri_pb2

logger = logging.getLogger("nixkube.cri")


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
        api = await kr8s.asyncio.api()
        async with api.call_api(
            "GET",
            url=f"nodes/{node_name}/proxy/configz",
        ) as response:
            config = response.json()
            endpoint = config.get("kubeletconfig", {}).get("containerRuntimeEndpoint")

            if not endpoint:
                raise RuntimeError(
                    "containerRuntimeEndpoint not found in kubelet configz"
                )

            # Strip unix:// prefix if present
            endpoint = endpoint.removeprefix("unix://")

            logger.info("Discovered CRI socket: %s", endpoint)
            return Path(endpoint)

    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Failed to query kubelet configz for node {node_name}: {e}"
        ) from e


async def list_container_ids(cri_socket: Path) -> set[str]:
    """
    List all container IDs from the container runtime via CRI API.

    Connects to the CRI socket and calls RuntimeService.ListContainers()
    to get all containers (running or stopped).

    Args:
        cri_socket: Path to the CRI socket (e.g., Path("/var/run/containerd/containerd.sock"))

    Returns:
        Set of container IDs as strings.

    Raises:
        RuntimeError if unable to connect to CRI or query containers.
    """
    try:
        # Create gRPC channel to CRI socket
        channel = grpclib.client.Channel(path=str(cri_socket))
        stub = cri_grpc.RuntimeServiceStub(channel)

        # Call ListContainers to get all containers
        request = cri_pb2.ListContainersRequest()
        response = await stub.ListContainers(request)

        # Extract container IDs from response
        container_ids = {container.id for container in response.containers}
        logger.debug("Discovered %d containers via CRI", len(container_ids))

        channel.close()
        return container_ids

    except Exception as e:
        raise RuntimeError(
            f"Failed to list containers from CRI socket {cri_socket}: {e}"
        ) from e
