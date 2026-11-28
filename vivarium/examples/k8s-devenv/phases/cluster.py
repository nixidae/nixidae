"""The cluster: kubeadm, the LoadBalancer controller, cert-manager and its issuer.

`bring_up` does all of it, and writes `access/kubeconfig` for the host. On
a second `vivarium up` it resumes the kept cluster instead.
"""

from __future__ import annotations

from vivarium_k8s import bring_up
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    await bring_up(vms)
