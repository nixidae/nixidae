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

MINIONS = ("web1", "web2", "suse1")


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


async def orchestrate(master: Machine, pillar: dict[str, Any]) -> tuple[int, str]:
    """`salt-run state.orchestrate orch.deploy` with `pillar`: (exit, output)."""
    return await master.execute(shlex.join(
        ["salt-run", "--out", "json", "state.orchestrate", "orch.deploy",
         f"pillar={json.dumps(pillar)}"]), timeout=900)


async def history(web: Machine) -> list[str]:
    """The systems the system profile's generations point at, oldest first."""
    out = await web.succeed(
        "find /nix/var/nix/profiles -maxdepth 1 -name 'system-*-link' -printf '%f %l\\n'")
    links = [line.split() for line in out.splitlines() if line.strip()]
    return [target for _, target in sorted(links, key=lambda link: int(link[0].split("-")[1]))]


async def bootstrap_suse(master: Machine, suse1: Machine, settings: dict[str, Any],
                         install: dict[str, Any]) -> None:
    """salt-ssh installs the minion on SUSE, as Uyuni bootstraps a client,
    and points havsorm at the master's binary cache."""
    await master.succeed(
        f"install -D -m 0600 {shlex.quote(settings['key'])} /etc/salt/ssh/id_ed25519")
    public_key = (await suse1.succeed(f"cat {shlex.quote(install['publicKey'])}")).strip()
    config = {"havsorm": {"nix_settings": {
        "substituters": install["cache"], "trusted-public-keys": public_key,
        "build-users-group": ""}}}
    pillar = json.dumps({"bootstrap": {"salt": settings["env"], "master": "10.9.0.1",
                                       "config": config}})
    out = await master.succeed(shlex.join(
        ["salt-ssh", "--ignore-host-keys", "--out", "json", "--static", "suse1",
         "state.apply", "bootstrap", f"pillar={pillar}"]), timeout=600)
    ret: dict[str, Any] = json.loads(out)["suse1"]
    failed = {name: r for name, r in ret.items() if r["result"] is not True}
    assert not failed, failed
    assert len(ret) == 3, ret


async def wait_for_minions(master: Machine) -> None:
    with anyio.fail_after(300):
        while True:
            ret = await salt(master, "-L", ",".join(MINIONS), "test.ping", timeout=10)
            if all(ret.get(m) is True for m in MINIONS):
                return
            await anyio.sleep(2)


async def test(vms: Machines) -> None:
    master, web1, suse1 = vms.master, vms.web1, vms.suse1
    await master.wait_for_unit("salt-master.service")
    await suse1.fail("test -e /etc/salt/minion")
    await bootstrap_suse(master, suse1, vms.settings["salt"], vms.settings["install"])
    assert await suse1.unit_state("salt-minion.service") == "active"
    print("[test] salt-ssh bootstrapped a minion on suse1 with a #!nix state")
    await wait_for_minions(master)
    print("[test] web1 and web2 (NixOS) and suse1 (openSUSE) answer the master")

    # A #!nix state from the master's file_roots, rendered on each minion.
    applied = await salt(master, "-L", ",".join(MINIONS), "state.apply", "fleet")
    for minion in MINIONS:
        ret = state(applied, minion)
        assert ret["result"] is True, ret
    for minion in MINIONS:
        machine = getattr(vms, minion)
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

    # Nix on openSUSE, from the master's binary cache.
    nix = vms.settings["install"]["nix"]
    await suse1.fail(f"test -e {nix}")
    installed = state(await salt(
        master, "suse1", "state.single", "havsorm.store_present", "nix", f"path={nix}",
        'settings={"build-users-group": "", "experimental-features": "nix-command"}'), "suse1")
    assert installed["result"] is True, installed
    profile = "/nix/var/nix/profiles/default"
    assert (await suse1.succeed(f"readlink -f {profile}")).strip() == nix
    said = await suse1.succeed(f"{profile}/bin/nix --version")
    assert said.startswith("nix (Nix)"), said
    again = state(await salt(
        master, "suse1", "state.single", "havsorm.store_present", "nix", f"path={nix}",
        'settings={"build-users-group": "", "experimental-features": "nix-command"}'), "suse1")
    assert again["result"] is True and again["changes"] == {}, again
    print(f"[test] havsorm.store_present installed {said.strip()} on openSUSE "
          "from the master's cache, and then changed nothing")

    daemon = state(await salt(master, "suse1", "state.single", "service.running",
                              "nix-daemon.socket"), "suse1")
    assert daemon["result"] is True, daemon
    await suse1.succeed(f"{profile}/bin/nix store info --store daemon")
    print("[test] Nix's own daemon answers on openSUSE, through Nix's own units")

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

    on_next = await salt(master, "-G", f"nixos:system:{nxt}", "test.ping", timeout=20)
    assert on_next == {"web1": True}, on_next
    code, said = await master.execute(shlex.join(
        ["salt", "--out", "json", "--static", "-t", "20", "-G", f"nixos:system:{booted}",
         "test.ping"]))
    assert "web1" not in said, (code, said)
    print("[test] a grain target finds the minions on the next system, and only them")

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

    # A rolling deploy, through a #!nix orchestration.
    webs = {"web1": web1, "web2": vms.web2}
    good = {name: web.spec.configurations["next"] for name, web in webs.items()}
    broken = {name: web.spec.configurations["broken"] for name, web in webs.items()}
    code, out = await orchestrate(master, {"systems": good})
    assert code == 0, out
    for name, web in webs.items():
        assert await current(web) == good[name], name
        assert await generation(web) == "next", name
    print("[test] an orchestration deployed the next system to web1 and web2")

    before = {name: await history(web) for name, web in webs.items()}
    code, out = await orchestrate(master, {"systems": broken, "good": good})
    assert code != 0, out
    for name, web in webs.items():
        assert await current(web) == good[name], (name, out)
        assert await generation(web) == "next", name
    tried = [name for name, web in webs.items()
             if broken[name] in (await history(web))[len(before[name]):]]
    assert len(tried) == 1, (tried, out)
    print(f"[test] a broken system stopped the deploy at {tried[0]}, "
          "the other minion never got it, and both run the good system again")

    journal = await master.journal("salt-master.service", lines=100000)
    assert "Traceback" not in journal, journal[-3000:]
    print("[test] salt-master logged no traceback")
