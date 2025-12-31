#! /usr/bin/env python3

import kr8s
import logging

logger = logging.getLogger("nix-csi")


async def get_builder_ips(namespace: str) -> list[str]:
    try:
        candidate_nodes = []
        nodes = kr8s.asyncio.get("nodes")
        # Get all builder tagged nodes
        async for node in nodes:
            try:
                node.metadata["labels"]["nix.csi/builder"]
                candidate_nodes.append(node.name)
            except KeyError:
                logger.debug(f"Node {node.name} missing nix.csi/builder label")

        builder_ips = []
        pods = kr8s.asyncio.get(
            "pods", namespace=namespace, label_selector={"app": "nix-csi-node"}
        )
        async for pod in pods:
            try:
                nodeName = pod.spec["nodeName"]
                if nodeName in candidate_nodes:
                    builder_ips.append(pod.status["podIP"])
            except KeyError:
                logger.debug(f"Pod {pod.name} missing nodeName or podIP")

        return builder_ips
    except Exception as e:
        logger.error(f"Failed to get builder IPs from Kubernetes API: {e}")
        return []
