# CRI Client Implementation Plan

## Overview

Implement a CRI (Container Runtime Interface) client to discover and interact with the container runtime on the node. This enables:
- Container ID discovery for garbage collection of NRI volumes
- Runtime-agnostic queries (works with containerd and CRI-O)
- Sweeping garbage collection of stale NRI containers

## CRI Socket Discovery

### Method 1: Kubelet API Server Proxy (Recommended)

Query the kubelet's configz endpoint through the API server proxy:

```bash
kubectl get --raw /api/v1/nodes/{node-name}/proxy/configz | jq .kubeletconfig.containerRuntimeEndpoint
```

**Example output:**
```
"unix:///var/run/containerd/containerd.sock"
```

**Advantages:**
- Works from any machine with cluster-admin access
- No direct kubelet port exposure needed
- Handles authentication/authorization through API server
- Works across firewalls and network policies
- Supports non-standard paths (e.g., k3s: `/run/k3s/containerd/containerd.sock`)

**From within pod (using kr8s):**
```python
import os
import json
from kr8s.clients import APIClient

async def get_cri_endpoint() -> str:
    """Get CRI endpoint from kubelet via API server proxy."""
    node_name = os.environ["KUBE_NODE_NAME"]
    async with APIClient() as api:
        response = await api.get(
            f"/api/v1/nodes/{node_name}/proxy/configz"
        )
    config = response.json()
    return config["kubeletconfig"]["containerRuntimeEndpoint"]
```

### Method 2: Standard Paths Fallback

If API server access fails, try common CRI socket locations:
- containerd: `/run/containerd/containerd.sock`, `/var/run/containerd/containerd.sock`
- CRI-O: `/var/run/crio/crio.sock`, `/run/crio/crio.sock`
- k3s: `/run/k3s/containerd/containerd.sock`

### Method 3: Environment Variable

Allow manual override via environment variable (for non-standard setups):
```
CRI_ENDPOINT=unix:///path/to/socket
```

## Implementation Strategy

1. ✅ Create `cri-proto-python` package for CRI API definitions
2. Implement CRI socket discovery in nix-csi
3. Build CRI client using grpclib (like NRI plugin)
4. Add `ListContainers()` query for container ID discovery
5. Implement sweeping garbage collection using discovered container IDs

## CRI API Endpoints

Key RPC methods needed:
- `RuntimeService.ListContainers()` - Get all containers on node
- `RuntimeService.ContainerStatus()` - Get container details

These provide the source of truth for active containers (running or stopped).
