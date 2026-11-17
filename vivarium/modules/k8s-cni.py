"""vivarium-k8s-cni POD_CIDR: the bridge network for this node's pods.

Not baked into the image because the subnet is not known until the
cluster exists: kube-controller-manager carves a /24 out of the pod
subnet per node and publishes it as the Node's spec.podCIDR, so the test
reads it back and calls this. Until then the node stays NotReady with
"cni plugin not initialized", which is correct -- it is not.

`ipMasq` always. Pods live on `cni0` in the node's podCIDR, and without
SNAT nothing they send past the node is ever answered: not the registry
under `images = "pull"`, and not the cache guest of
`vivarium.recipes.cache` under either mode. Measured: with it off,
nixkube's init container spends its life on "Resolving timed out after
15000 milliseconds" and the DaemonSet never rolls out -- with no error
anywhere naming a route.
"""

from __future__ import annotations

import ipaddress
import json
import sys
from pathlib import Path

CONFIG = Path("/etc/cni/net.d/10-vivarium.conflist")


def conflist(subnet: str) -> dict[str, object]:
    return {
        "cniVersion": "1.0.0",
        "name": "uml",
        "plugins": [
            {
                "type": "bridge",
                "bridge": "cni0",
                # It implies isGateway, and the plugin adds the default route
                # itself. Naming 0.0.0.0/0 in ipam.routes as well, as most
                # published conflists do, adds it twice: the plugin owns an
                # existing default route only if it names a gateway, and an
                # ipam route does not. The second add is EEXIST, kubelet says
                # `failed to add route ...: file exists`, and nothing that
                # needs CNI gets a sandbox -- CoreDNS here, long after the
                # nodes all went Ready.
                "isDefaultGateway": True,
                "hairpinMode": True,
                "ipMasq": True,
                "ipam": {"type": "host-local", "ranges": [[{"subnet": subnet}]]},
            },
            {"type": "portmap", "capabilities": {"portMappings": True}},
        ],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: vivarium-k8s-cni <pod-cidr>", file=sys.stderr)
        return 1
    try:
        subnet = str(ipaddress.ip_network(argv[0]))
    except ValueError as error:
        print(f"vivarium-k8s-cni: {error}", file=sys.stderr)
        return 1
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(conflist(subnet), indent=2) + "\n")
    print(f"vivarium-k8s-cni: {subnet} on cni0")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
