"""vivarium-k8s-join ENDPOINT TOKEN CA_CERT_HASH: join this node to a cluster.

The three values `kubeadm token create --print-join-command` prints, as
arguments rather than a command line, so the timeouts and the node's own
registration in the JoinConfiguration apply here too. A join run from the
printed command line gets kubeadm's defaults and gives up on the TLS
bootstrap after five minutes.
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
    if len(argv) != 3:
        print("usage: vivarium-k8s-join <endpoint> <token> <ca-cert-hash>", file=sys.stderr)
        return 1
    endpoint, token, digest = argv
    config = json.loads(TEMPLATE.read_text())
    config["discovery"] = {"bootstrapToken": {"apiServerEndpoint": endpoint, "token": token, "caCertHashes": [digest]}}
    # JSON is YAML, so kubeadm reads it as written.
    with tempfile.NamedTemporaryFile("w", prefix="kubeadm-join-", suffix=".json", delete=False) as file:
        json.dump(config, file)
    try:
        return subprocess.run([KUBEADM, "join", "--config", file.name, "--v=2"], check=False).returncode
    finally:
        Path(file.name).unlink()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
