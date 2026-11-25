"""vivarium-k8s-join [--node-ip IPS] ENDPOINT TOKEN CA_CERT_HASH [CERTIFICATE_KEY]: join this node to a cluster.

The three values `kubeadm token create --print-join-command` prints, as
arguments rather than a command line, so the timeouts and the node's own
registration in the JoinConfiguration apply here too. A join run from the
printed command line gets kubeadm's defaults and gives up on the TLS
bootstrap after five minutes.

A control plane also takes the key of the certificates the cluster
uploaded (`kubeadm init phase upload-certs`).

`--node-ip` is kubelet's, for a template's instance: Nix does not know
its address. kubeadm writes it to kubeadm-flags.env, which kubelet reads
on every start.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

TEMPLATE = Path("@template@")
"""The JoinConfiguration from Nix, as JSON, without its `discovery`."""
KUBEADM = "@kubeadm@"


def main(argv: list[str]) -> int:
    config = json.loads(TEMPLATE.read_text())
    control_plane = "controlPlane" in config
    parser = argparse.ArgumentParser(prog="vivarium-k8s-join")
    parser.add_argument("--node-ip", help="kubelet's --node-ip, comma-separated")
    parser.add_argument("endpoint")
    parser.add_argument("token")
    parser.add_argument("digest", metavar="ca-cert-hash")
    if control_plane:
        parser.add_argument("certificate_key", metavar="certificate-key")
    args = parser.parse_args(argv)
    config["discovery"] = {
        "bootstrapToken": {"apiServerEndpoint": args.endpoint, "token": args.token, "caCertHashes": [args.digest]}
    }
    if control_plane:
        config["controlPlane"]["certificateKey"] = args.certificate_key
    if args.node_ip is not None:
        registration = config.setdefault("nodeRegistration", {})
        extra = [arg for arg in registration.get("kubeletExtraArgs", []) if arg["name"] != "node-ip"]
        registration["kubeletExtraArgs"] = [*extra, {"name": "node-ip", "value": args.node_ip}]
    # JSON is YAML, so kubeadm reads it as written.
    with tempfile.NamedTemporaryFile("w", prefix="kubeadm-join-", suffix=".json", delete=False) as file:
        json.dump(config, file)
    try:
        return subprocess.run([KUBEADM, "join", "--config", file.name, "--v=2"], check=False).returncode
    finally:
        Path(file.name).unlink()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
