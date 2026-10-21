"""The services guest as the OOB gateway (nixidae/nixidae#958).

The host reaches an out-of-band guest through a single forwarding point.
`sw` has no passt uplink of its own -- its uplink is on the management
segment -- so the only way in is one passt forward on `svc`, which DNATs
to `sw`. The reply returns only because Kea gives `sw` its default route
back to `svc`, so a working fetch proves the DNAT and the advertised
route together.

Egress to the real world is not proven here: a sandboxed run has no
network (see the `uplink` test). Measured by hand instead, 2026-10-21,
this host, `.driver`: paused at this phase, sw opens a TCP connection to
1.1.1.1:443 through svc, source 10.42.0.10 -- so svc forwards and
masquerades the segment out through its passt uplink.
"""

import urllib.error
import urllib.request

import anyio
import anyio.to_thread

from vivarium_runner import Machines

PAGE = "hello-from-behind-the-gateway"
DOC_ROOT = "/tmp/www"
PORT = 8080
SW_ADDRESS = "10.42.0.10"
"""`sw`'s reserved management address, outside Kea's pool and the DNAT
target. The phase waits for exactly this, so a wrong reservation fails
loudly rather than passing on a pool address."""
GATEWAY = "10.42.0.1"

SETTLED = (
    f"ip -4 -brief addr show vec0 | grep -qF {SW_ADDRESS}"
    f" && ip -4 route show default | grep -qF 'via {GATEWAY}'"
)
"""sw holds its reserved address and its default route from DHCP."""


def _get(where: str) -> str:
    return urllib.request.urlopen(f"http://{where}/probe", timeout=5).read().decode().strip()


async def _fetch(where: str, timeout: float = 30) -> str:
    """GET /probe from the host side, giving the path time to come up."""
    try:
        with anyio.fail_after(timeout):
            while True:
                try:
                    return await anyio.to_thread.run_sync(_get, where)
                except (urllib.error.URLError, OSError):
                    await anyio.sleep(0.5)
    except TimeoutError:
        raise AssertionError(f"nothing answered on {where} within {timeout:.0f}s") from None


async def test(vms: Machines) -> None:
    svc = vms.svc
    sw = vms.sw

    await svc.wait_for_unit("kea-dhcp4-server.service")
    await svc.wait_for_unit("nat.service")
    print("[test] the services guest runs Kea and the NAT gateway")

    await sw.wait_until_succeeds(SETTLED, timeout=60)
    print(f"[test] sw leased its reserved {SW_ADDRESS} and a default route via {GATEWAY}")

    # A service in sw, bound long after passt stopped taking arguments.
    await sw.succeed(f"mkdir -p {DOC_ROOT} && echo {PAGE} > {DOC_ROOT}/probe")
    await sw.succeed(
        f"systemd-run --unit=probe --collect"
        f" --working-directory={DOC_ROOT} python3 -m http.server {PORT}"
    )

    address = svc.forward[0].address
    if address is None:
        raise AssertionError("the services guest booted with an unresolved forward")
    where = svc.reachable(PORT)
    if where != [f"{address}:{PORT}"]:
        raise AssertionError(f"8080 is not forwarded as expected: {where}")
    print(f"[test] the host reaches the gateway on {where[0]}")

    got = await _fetch(where[0])
    if got != PAGE:
        raise AssertionError(f"the gateway returned {got!r}, not sw's page")
    print("[test] and it answers with sw's page, through the single forwarding point")
