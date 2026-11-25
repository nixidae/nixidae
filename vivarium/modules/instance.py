"""Apply a template instance's seed: its name, addresses and user data. nixidae/nixidae#1408."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

NETWORK = Path("/run/systemd/network")
OWN = Path("/run/vivarium")


def main(seed_path: str) -> None:
    seed = json.loads(Path(seed_path).read_text())
    Path("/proc/sys/kernel/hostname").write_text(seed["name"])
    for nic, addresses in seed.get("interfaces", {}).items():
        # The drop-in directory of the unit guest.nix declares, `40-<nic>.network`.
        drop_in = NETWORK / f"40-{nic}.network.d"
        drop_in.mkdir(parents=True, exist_ok=True)
        lines = "".join(f"Address={address}\n" for address in addresses)
        (drop_in / "50-instance.conf").write_text(f"[Network]\n{lines}")
    token = seed.get("uplink_token")
    if token is not None:
        drop_in = NETWORK / "40-vec0.network.d"
        drop_in.mkdir(parents=True, exist_ok=True)
        (drop_in / "50-instance-token.conf").write_text(f"[IPv6AcceptRA]\nToken={token}\n")
    OWN.mkdir(mode=0o755, exist_ok=True)
    (OWN / "instance.json").write_text(json.dumps({key: seed[key] for key in ("name", "template", "index")}))
    user_data = seed.get("user_data")
    if user_data is not None:
        fd = os.open(OWN / "user-data", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(user_data)


if __name__ == "__main__":
    main(sys.argv[1])
