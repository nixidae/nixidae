"""A Salt master drives a NixOS and an openSUSE minion with havsorm; see
nix/fleet.nix.

Each step checks the minion, not only Salt's return: Salt can report a
change that did not land.
"""

import json
import shlex
from typing import Any

import anyio
from vivarium_runner import Machine, Machines

MINIONS = ("web1", "suse1")


async def salt(master: Machine, *argv: str, timeout: int = 300) -> dict[str, Any]:
    """`salt <argv>` on the master, as JSON by minion id."""
    out = await master.succeed(
        shlex.join(["salt", "--out", "json", "--static", "-t", str(timeout), *argv]),
        timeout=timeout + 30)
    result: dict[str, Any] = json.loads(out)
    return result


def state(ret: dict[str, Any], minion: str) -> dict[str, Any]:
    """The one state's result in a `state.single` return."""
    (only,) = ret[minion].values()
    result: dict[str, Any] = only
    return result


async def current(web1: Machine) -> str:
    return (await web1.succeed("readlink -f /run/current-system")).strip()


async def generation(web1: Machine) -> str:
    return (await web1.succeed("cat /etc/havsorm-generation")).strip()


async def start_suse_minion(suse1: Machine, settings: dict[str, Any]) -> None:
    await suse1.add_closure(settings["closure"])
    await suse1.succeed(
        f"mkdir -p /etc/salt && printf '%s' {shlex.quote(settings['config'])} > /etc/salt/minion")
    await suse1.succeed(
        f"systemd-run --unit=salt-minion {shlex.quote(settings['bin'])}/salt-minion")


async def wait_for_minions(master: Machine) -> None:
    with anyio.fail_after(300):
        while True:
            ret = await salt(master, "-L", ",".join(MINIONS), "test.ping", timeout=10)
            if all(ret.get(m) is True for m in MINIONS):
                return
            await anyio.sleep(2)


async def test(vms: Machines) -> None:
    master, web1, suse1 = vms.master, vms.web1, vms.suse1
    await start_suse_minion(suse1, vms.settings["salt"])
    await master.wait_for_unit("salt-master.service")
    await wait_for_minions(master)
    print("[test] web1 (NixOS) and suse1 (openSUSE) answer the master")

    # A #!nix state from the master's file_roots, rendered on each minion.
    applied = await salt(master, "-L", ",".join(MINIONS), "state.apply", "fleet")
    for minion in MINIONS:
        ret = state(applied, minion)
        assert ret["result"] is True, ret
    for minion, machine in (("web1", web1), ("suse1", suse1)):
        os_grain = (await salt(master, minion, "grains.get", "os"))[minion]
        said = (await machine.succeed("cat /etc/havsorm-fleet")).strip()
        assert said == f"{os_grain} {minion}", said
    os_names = {m: (await salt(master, m, "grains.get", "os"))[m] for m in MINIONS}
    assert os_names["web1"] == "NixOS", os_names
    assert os_names["web1"] != os_names["suse1"], os_names
    print(f"[test] one #!nix state rendered per minion: {os_names}")

    evaluated = await salt(master, "suse1", "havsorm.eval", "builtins.currentSystem")
    assert evaluated["suse1"] == "x86_64-linux", evaluated
    print("[test] huggorm evaluates on openSUSE, from the host's store")

    booted = web1.spec.toplevel
    nxt = web1.spec.configurations["next"]
    assert booted is not None
    assert await current(web1) == booted
    assert await generation(web1) == "base"

    dry = state(await salt(master, "web1", "state.single", "havsorm.system", nxt, "test=True"),
                "web1")
    assert dry["result"] is None, dry
    assert dry["changes"]["running"] == {"old": booted, "new": nxt}, dry
    assert await current(web1) == booted, "test=True switched the system"
    assert await generation(web1) == "base"

    done = state(await salt(master, "web1", "state.single", "havsorm.system", nxt), "web1")
    assert done["result"] is True, done
    assert await current(web1) == nxt
    assert await generation(web1) == "next"
    assert (await web1.succeed("readlink -f /nix/var/nix/profiles/system")).strip() == nxt
    again = state(await salt(master, "web1", "state.single", "havsorm.system", nxt), "web1")
    assert again["result"] is True and again["changes"] == {}, again
    print("[test] havsorm.system switched web1 to the next system, and then changed nothing")

    back = state(await salt(master, "web1", "state.single", "havsorm.system", booted), "web1")
    assert back["result"] is True, back
    assert await generation(web1) == "base"
    rolled = state(await salt(master, "web1", "state.single", "havsorm.rolled_back", "rb"),
                   "web1")
    assert rolled["result"] is True, rolled
    assert rolled["changes"]["new"] == nxt, rolled
    assert await current(web1) == nxt
    assert await generation(web1) == "next"
    print("[test] havsorm.rolled_back took web1 back one generation, to the next system")

    # The reactor: an event names a release, and a #!nix reaction deploys it.
    await salt(master, "web1", "state.single", "havsorm.system", booted)
    assert await generation(web1) == "base"
    event = json.dumps({"minion": "web1", "path": nxt})
    await master.succeed(shlex.join(["salt-run", "event.send", "havsorm/release/web", event]))
    with anyio.fail_after(180):
        while await current(web1) != nxt:
            await anyio.sleep(2)
    assert await generation(web1) == "next"
    print("[test] an event on the bus deployed the next system through a #!nix reaction")
