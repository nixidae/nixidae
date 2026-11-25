"""vivarium-k8s-join ENDPOINT TOKEN CA_CERT_HASH [CERTIFICATE_KEY]: join this node to a cluster.

The three values `kubeadm token create --print-join-command` prints, as
arguments rather than a command line, so the timeouts and the node's own
registration in the JoinConfiguration apply here too. A join run from the
printed command line gets kubeadm's defaults and gives up on the TLS
bootstrap after five minutes.

A control plane also takes the key of the certificates the cluster
uploaded (`kubeadm init phase upload-certs`).
"""

from __future__ import annotations

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
    if len(argv) != (4 if control_plane else 3):
        key = " <certificate-key>" if control_plane else ""
        sys.stderr.write(f"usage: vivarium-k8s-join <endpoint> <token> <ca-cert-hash>{key}\n")
        return 1
    endpoint, token, digest = argv[:3]
    config["discovery"] = {"bootstrapToken": {"apiServerEndpoint": endpoint, "token": token, "caCertHashes": [digest]}}
    if control_plane:
        config["controlPlane"]["certificateKey"] = argv[3]
    # JSON is YAML, so kubeadm reads it as written.
    with tempfile.NamedTemporaryFile("w", prefix="kubeadm-join-", suffix=".json", delete=False) as file:
        json.dump(config, file)
    try:
        return subprocess.run([KUBEADM, "join", "--config", file.name, "--v=2"], check=False).returncode
    finally:
        Path(file.name).unlink()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
