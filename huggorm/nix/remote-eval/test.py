"""The client evaluates its own tree on the server (nixidae/nixidae#738).

The tree lives on the client's disk only. The server reads it through
callbacks, so a read that escapes to the server's disk fails.

The client shows the server its tracked files only: `scratch/` is on
the client's disk and never reaches the server. Then the same
evaluation runs with added latency on the link, to show what each
callback costs.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from vivarium_runner import Machine, Machines

ROOT = "/root/project"
KEY = "/root/.ssh/id_ed25519"
FILES = "/root/files"
SCRATCH_FILES = 2000
# One-way delay on each guest's LAN link, so a round trip is twice this.
DELAYS_MS = (0, 5, 25)


async def evaluate(client: Machine, settings: dict[str, Any], *extra: str) -> dict[str, Any]:
    # Any: the client's JSON report, read field by field below.
    result = await client.succeed(
        " ".join(
            [
                settings["python"],
                settings["client"],
                f"--server {settings['server']} --key {KEY} --python {settings['python']}",
                f"--root {ROOT} --files {FILES}",
                *extra,
            ]
        ),
        timeout=600,
    )
    report: dict[str, Any] = json.loads(result.stdout)
    return report


async def test(vms: Machines) -> None:
    server, client = vms.server, vms.client
    settings = vms.settings

    await client.succeed(f"mkdir -p /root/.ssh && ssh-keygen -q -t ed25519 -N '' -f {KEY}")
    public = (await client.succeed(f"cat {KEY}.pub")).stdout.strip()
    await server.succeed(f"mkdir -p /root/.ssh && echo '{public}' >> /root/.ssh/authorized_keys")
    await server.wait_for_unit("sshd.service")

    token = secrets.token_hex(8)
    await client.succeed(f"cp -r --no-preserve=mode {settings['project']} {ROOT}")
    await client.succeed(f"echo {token} > {ROOT}/live.txt")
    await client.succeed(f"cd {ROOT} && find . -type f -printf '%P\\n' > {FILES}")
    await client.succeed(
        f"mkdir {ROOT}/scratch && for i in $(seq {SCRATCH_FILES}); do echo $i > {ROOT}/scratch/$i; done"
    )
    await server.fail(f"test -e {ROOT}")

    report = await evaluate(client, settings, f"--escape 'builtins.readFile {ROOT}/hello.txt'", "--repeat 2")
    print(f"[test] report: {json.dumps(report, indent=2)}")

    value = report["value"]
    if value["imported"] != 42 or value["joined"] != "hello\n" or value["live"] != f"{token}\n":
        raise AssertionError(f"wrong values: {value}")
    if value["listed"] != {"x.nix": "regular"}:
        raise AssertionError(f"wrong readDir: {value['listed']}")
    await server.succeed(f"test -f {value['copied']}/a.txt")
    await server.fail(f"test -e {report['mounted_at']}/scratch")
    if report["calls_mount"]:
        raise AssertionError(f"a mount at a known path read the client: {report['calls_mount']}")
    if report["hidden_asked"]:
        raise AssertionError(f"the server asked for hidden paths: {report['hidden_asked']}")
    error = report["escape_error"]
    if "does not exist" not in error:
        raise AssertionError(f"an absolute path did not fail on the server: {error!r}")
    print(f"[test] the server reads the client's tracked files, and an absolute path fails: {error}")

    rows = []
    for delay in DELAYS_MS:
        for vm in (server, client):
            await vm.succeed(f"tc qdisc replace dev vec1 root netem delay {delay}ms")
        rtt = (await client.succeed(f"ping -c3 -q {settings['server']} | tail -1")).stdout.strip()
        timed = await evaluate(client, settings, "--repeat 2")
        cold, warm = timed["passes"]
        rows.append(
            f"{2 * delay:>4} ms rtt ({rtt}): connect {timed['seconds']['connect']:.2f} s,"
            f" cold {cold['seconds']:.3f} s for {sum(cold['calls'].values())} callbacks,"
            f" warm {warm['seconds']:.3f} s for {sum(warm['calls'].values())} callbacks"
        )
    print("[test] latency sweep:\n" + "\n".join(rows))
