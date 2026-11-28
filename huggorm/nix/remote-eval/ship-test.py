"""A host with no huggorm gets the client's closure over SSH (nixidae/nixidae#738).

`bare` has Nix and sshd and nothing of huggorm. The client starts a cold
server there with `ssh.provisioned`: the guard answers that the
interpreter is absent, the client copies its closure to bare's daemon
over the SSH link, and the second start runs the server.

A user outside `trusted-users` gets Nix's own refusal of the unsigned
paths, and the path stays absent. Root, trusted by default, gets the copy.
"""

from __future__ import annotations

import json
from typing import Any

from vivarium_runner import Machine, Machines
from vivarium_runner.control import CommandResult

ROOT = "/root/ship-project"
KEY = "/root/.ssh/ship_ed25519"
UNTRUSTED = "plain"


async def evaluate(client: Machine, settings: dict[str, Any], user: str) -> CommandResult:
    return await client.execute(
        " ".join(
            [
                settings["python"],
                settings["client"],
                f"--server {settings['bare']} --user {user} --key {KEY} --python {settings['python']}",
                f"--root {ROOT} --provision --store daemon --expr '1 + 1'",
            ]
        ),
        timeout=900,
    )


async def absent(bare: Machine, path: str) -> None:
    await bare.fail(f"test -e {path}")
    await bare.fail(f"nix-store --check-validity {path}")


async def test(vms: Machines) -> None:
    bare, client = vms.bare, vms.client
    settings = vms.settings
    venv = settings["venv"]

    await client.succeed(f"mkdir -p /root/.ssh && ssh-keygen -q -t ed25519 -N '' -f {KEY}")
    public = (await client.succeed(f"cat {KEY}.pub")).stdout.strip()
    for home in ("/root", f"/home/{UNTRUSTED}"):
        await bare.succeed(f"mkdir -p {home}/.ssh && echo '{public}' >> {home}/.ssh/authorized_keys")
    await bare.succeed(f"chown -R {UNTRUSTED} /home/{UNTRUSTED}/.ssh")
    await bare.wait_for_unit("sshd.service")
    await client.succeed(f"cp -r --no-preserve=mode {settings['project']} {ROOT}")

    # The negative control: bare cannot pass by holding the closure already.
    await absent(bare, venv)

    refused = await evaluate(client, settings, UNTRUSTED)
    print(f"[ship] untrusted: exit {refused.returncode}\n{refused.stderr[-2000:]}")
    if refused.returncode == 0:
        raise AssertionError("a user outside trusted-users copied unsigned paths")
    if "lacks a signature by a trusted key" not in refused.stderr:
        raise AssertionError(f"the refusal is not Nix's: {refused.stderr[-2000:]}")
    await absent(bare, venv)

    shipped = await evaluate(client, settings, "root")
    if shipped.returncode != 0:
        raise AssertionError(f"the copy as root failed: {shipped.stderr[-2000:]}")
    report = json.loads(shipped.stdout)
    print(f"[ship] root: {json.dumps(report['seconds'])}")
    if report["value"] != 2:
        raise AssertionError(f"wrong value: {report['value']}")
    await bare.succeed(f"nix-store --check-validity {venv}")

    # Present now: the guard answers so, and nothing is copied.
    again = await evaluate(client, settings, "root")
    if again.returncode != 0:
        raise AssertionError(f"the second start failed: {again.stderr[-2000:]}")
    print(f"[ship] root again: {json.dumps(json.loads(again.stdout)['seconds'])}")
