"""A Salt master drives a fleet with havsorm; see nix/fleet.nix.

One script, one function per phase, told apart by `vms.phase`. Each step
checks the minion, not only Salt's return: Salt can report a change that
did not land.
"""

import json
import shlex
from collections import Counter
from typing import Any

import anyio
from vivarium_runner import Machine, Machines

MINIONS = ("web1", "web2", "suse1", "lb")
WEBS = {"web1": "10.9.0.2", "web2": "10.9.0.4", "suse1": "10.9.0.3"}


async def salt(master: Machine, *argv: str, timeout: int = 300) -> dict[str, Any]:
    """`salt <argv>` on the master, as JSON by minion id."""
    out = await master.succeed(
        shlex.join(["salt", "--out", "json", "--static", "-t", str(timeout), *argv]), timeout=timeout + 30
    )
    result: dict[str, Any] = json.loads(out)
    return result


def state(ret: dict[str, Any], minion: str) -> dict[str, Any]:
    """The one state's result in a `state.single` return."""
    (only,) = ret[minion].values()
    result: dict[str, Any] = only
    return result


def applied(ret: dict[str, Any], minion: str) -> dict[str, Any]:
    """Every state of a `state.apply` return, by id, all of them passed."""
    assert isinstance(ret[minion], dict), (minion, "did not render", ret[minion])
    states = {r["__id__"]: r for r in ret[minion].values()}
    failed = {i: r["comment"] for i, r in states.items() if r["result"] is not True}
    assert not failed, (minion, failed)
    return states


async def current(web: Machine) -> str:
    return (await web.succeed("readlink -f /run/current-system")).strip()


async def generation(web: Machine) -> str:
    return (await web.succeed("cat /etc/havsorm-generation")).strip()


async def orchestrate(master: Machine, pillar: dict[str, Any]) -> tuple[int, str]:
    """`salt-run state.orchestrate orch.deploy` with `pillar`: (exit, output)."""
    return await master.execute(
        shlex.join(["salt-run", "--out", "json", "state.orchestrate", "orch.deploy", f"pillar={json.dumps(pillar)}"]),
        timeout=900,
    )


async def history(web: Machine) -> list[str]:
    """The systems the system profile's generations point at, oldest first."""
    out = await web.succeed("find /nix/var/nix/profiles -maxdepth 1 -name 'system-*-link' -printf '%f %l\\n'")
    links = [line.split() for line in out.splitlines() if line.strip()]
    return [target for _, target in sorted(links, key=lambda link: int(link[0].split("-")[1]))]


async def through_lb(master: Machine, requests: int = 12) -> Counter[str]:
    """Which backend answered each of `requests` requests to the load
    balancer, counted."""
    out = await master.succeed(f"for i in $(seq {requests}); do curl -sf --max-time 5 http://10.9.0.5/; done")
    return Counter(out.split())


async def until_lb(master: Machine, backends: set[str], timeout: float = 120) -> None:
    with anyio.fail_after(timeout):
        while set(await through_lb(master)) != backends:
            await anyio.sleep(1)


async def bootstrap(vms: Machines) -> None:
    """salt-ssh installs the minion on SUSE, as Uyuni bootstraps a client;
    then a #!nix state renders on every minion."""
    master, suse1 = vms.master, vms.suse1
    settings = vms.settings["salt"]
    await master.wait_for_unit("salt-master.service")
    await suse1.fail("test -e /etc/salt/minion")
    await master.succeed(f"install -D -m 0600 {shlex.quote(settings['key'])} /etc/salt/ssh/id_ed25519")
    pillar = json.dumps({"bootstrap": {"salt": settings["env"], "master": "10.9.0.1", "config": settings["config"]}})
    out = await master.succeed(
        shlex.join(
            [
                "salt-ssh",
                "--ignore-host-keys",
                "--out",
                "json",
                "--static",
                "suse1",
                "state.apply",
                "bootstrap",
                f"pillar={pillar}",
            ]
        ),
        timeout=600,
    )
    ret: dict[str, Any] = json.loads(out)["suse1"]
    failed = {name: r for name, r in ret.items() if r["result"] is not True}
    assert not failed, failed
    assert len(ret) == 3, ret
    assert await suse1.unit_state("salt-minion.service") == "active"
    print("[test] salt-ssh bootstrapped a minion on suse1 with a #!nix state")

    with anyio.fail_after(300):
        while True:
            ret = await salt(master, "-L", ",".join(MINIONS), "test.ping", timeout=10)
            if all(ret.get(m) is True for m in MINIONS):
                break
            await anyio.sleep(2)
    print(f"[test] {', '.join(MINIONS)} answer the master")

    done = await salt(master, "-L", ",".join(MINIONS), "state.apply", "fleet")
    os_names = {}
    for minion in MINIONS:
        applied(done, minion)
        os_names[minion] = (await salt(master, minion, "grains.get", "os"))[minion]
        said = (await getattr(vms, minion).succeed("cat /etc/havsorm-fleet")).strip()
        assert said == f"{os_names[minion]} {minion}", said
    assert os_names["web1"] == "NixOS" and os_names["suse1"] != "NixOS", os_names
    print(f"[test] one #!nix state rendered per minion: {os_names}")


async def nix_on_suse(vms: Machines) -> None:
    """Nix on openSUSE, from the master's binary cache."""
    master, suse1 = vms.master, vms.suse1
    evaluated = await salt(master, "suse1", "havsorm.eval", "builtins.currentSystem")
    assert evaluated["suse1"] == "x86_64-linux", evaluated
    print("[test] huggorm evaluates on openSUSE, from the host's store")

    nix = vms.settings["install"]["nix"]
    await suse1.fail(f"test -e {nix}")
    args = [
        "state.single",
        "havsorm.store_present",
        "nix",
        f"path={nix}",
        'settings={"build-users-group": "", "experimental-features": "nix-command"}',
    ]
    installed = state(await salt(master, "suse1", *args), "suse1")
    assert installed["result"] is True, installed
    profile = "/nix/var/nix/profiles/default"
    assert (await suse1.succeed(f"readlink -f {profile}")).strip() == nix
    said = await suse1.succeed(f"{profile}/bin/nix --version")
    assert said.startswith("nix (Nix)"), said
    again = state(await salt(master, "suse1", *args), "suse1")
    assert again["result"] is True and again["changes"] == {}, again
    print(
        f"[test] havsorm.store_present installed {said.strip()} on openSUSE "
        "from the master's cache, and then changed nothing"
    )

    daemon = state(await salt(master, "suse1", "state.single", "service.running", "nix-daemon.socket"), "suse1")
    assert daemon["result"] is True, daemon
    await suse1.succeed(f"{profile}/bin/nix store info --store daemon")
    print("[test] Nix's own daemon answers on openSUSE, through Nix's own units")


async def nixos(vms: Machines) -> None:
    """Switch, target, roll back and deploy from an event, on web1."""
    master, web1 = vms.master, vms.web1
    booted = web1.spec.toplevel
    nxt = web1.spec.configurations["next"]
    assert booted is not None
    assert await current(web1) == booted
    assert await generation(web1) == "base"

    dry = state(await salt(master, "web1", "state.single", "havsorm.system", nxt, "test=True"), "web1")
    assert dry["result"] is None, dry
    assert dry["changes"]["running"] == {"old": booted, "new": nxt}, dry
    assert await current(web1) == booted, "test=True switched the system"

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
    code, said = await master.execute(
        shlex.join(["salt", "--out", "json", "--static", "-t", "20", "-G", f"nixos:system:{booted}", "test.ping"])
    )
    assert "web1" not in said, (code, said)
    print("[test] a grain target finds the minions on the next system, and only them")

    back = state(await salt(master, "web1", "state.single", "havsorm.system", booted), "web1")
    assert back["result"] is True, back
    rolled = state(await salt(master, "web1", "state.single", "havsorm.rolled_back", "rb"), "web1")
    assert rolled["result"] is True, rolled
    assert rolled["changes"]["new"] == nxt, rolled
    assert await current(web1) == nxt
    assert await generation(web1) == "next"
    print("[test] havsorm.rolled_back took web1 back one generation, to the next system")

    await salt(master, "web1", "state.single", "havsorm.system", booted)
    assert await generation(web1) == "base"
    event = json.dumps({"minion": "web1", "path": nxt})
    await master.succeed(shlex.join(["salt-run", "event.send", "havsorm/release/web", event]))
    with anyio.fail_after(180):
        while await current(web1) != nxt:
            await anyio.sleep(2)
    assert await generation(web1) == "next"
    print("[test] an event on the bus deployed the next system through a #!nix reaction")


async def rolling(vms: Machines) -> None:
    """A rolling deploy through a #!nix orchestration, and a failed one."""
    master = vms.master
    webs = {"web1": vms.web1, "web2": vms.web2}
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
    tried = [name for name, web in webs.items() if broken[name] in (await history(web))[len(before[name]) :]]
    assert len(tried) == 1, (tried, out)
    print(
        f"[test] a broken system stopped the deploy at {tried[0]}, "
        "the other minion never got it, and both run the good system again"
    )


async def service(vms: Machines) -> None:
    """A web server on every web minion, NixOS and openSUSE alike, from
    the master's cache, behind a load balancer that reads the mine."""
    master = vms.master
    units = vms.settings["service"]
    for name in WEBS:
        await getattr(vms, name).fail(f"test -e {units['web']}")

    web = await salt(
        master, "-G", "roles:web", "state.apply", "web", f"pillar={json.dumps({'web': {'unit': units['web']}})}"
    )
    assert set(web) == set(WEBS), web
    for name in WEBS:
        applied(web, name)
        machine = getattr(vms, name)
        assert await machine.unit_state("havsorm-web.service") == "active", name
        said = await master.succeed(f"curl -sf --max-time 5 http://{WEBS[name]}:8080/")
        assert said.strip() == name, (name, said)
    print("[test] havsorm.unit runs the same Nix-built web server on NixOS and openSUSE")

    await salt(master, "*", "mine.update")
    mine = (await salt(master, "lb", "mine.get", "roles:web", "lab_ip", "tgt_type=grain"))["lb"]
    assert mine == {name: [ip] for name, ip in WEBS.items()}, mine
    print(f"[test] every web minion published its lab address to the mine: {mine}")
    lb_pillar = f"pillar={json.dumps({'lb': {'unit': units['lb']}})}"
    first = applied(await salt(master, "lb", "state.apply", "lb", lb_pillar), "lb")
    assert first["lb-config"]["changes"], first
    await until_lb(master, set(WEBS))
    print(f"[test] the load balancer, built from the mine, spreads requests over {dict(await through_lb(master))}")

    same = applied(await salt(master, "lb", "state.apply", "lb", lb_pillar), "lb")
    assert all(not r["changes"] for r in same.values()), same
    print("[test] applying the load balancer again changes nothing")

    # Drain web2: it takes its address out of the mine, and the config
    # follows. Not by its role: on NixOS that grain is in the minion
    # config Nix wrote, which overrides grains.setval.
    await salt(master, "web2", "mine.delete", "lab_ip")
    mine = (await salt(master, "lb", "mine.get", "roles:web", "lab_ip", "tgt_type=grain"))["lb"]
    assert sorted(mine) == ["suse1", "web1"], mine
    drained = applied(await salt(master, "lb", "state.apply", "lb", lb_pillar), "lb")
    assert "web2" in drained["lb-config"]["changes"].get("diff", ""), drained["lb-config"]
    await until_lb(master, {"web1", "suse1"})
    for _ in range(3):
        assert set(await through_lb(master)) == {"web1", "suse1"}
    print("[test] web2 left the mine, and the load balancer stopped sending it requests")


async def heal(vms: Machines) -> None:
    """A beacon sees the web server stop on SUSE, and a #!nix reaction
    starts it again."""
    master, suse1 = vms.master, vms.suse1
    await salt(master, "suse1", "service.stop", "havsorm-web")
    await anyio.sleep(10)
    assert await suse1.unit_state("havsorm-web.service") == "inactive", "the web server came back with no beacon"
    print("[test] with no beacon, a stopped web server stays stopped")

    beacon = json.dumps([{"services": {"havsorm-web": {"is_running_state": False}}}, {"interval": 2}])
    added = await salt(master, "suse1", "beacons.add", "service", beacon)
    assert added["suse1"]["result"] is True, added
    with anyio.fail_after(120):
        while await suse1.unit_state("havsorm-web.service") != "active":
            await anyio.sleep(1)
    print("[test] the service beacon fired, and a #!nix reaction started the web server")

    await salt(master, "suse1", "service.stop", "havsorm-web")
    with anyio.fail_after(120):
        while await suse1.unit_state("havsorm-web.service") != "active":
            await anyio.sleep(1)
    print("[test] it heals again: the master's reactor rendered the reaction again")

    journal = await master.journal("salt-master.service", lines=100000)
    assert "Traceback" not in journal, journal[-3000:]
    print("[test] salt-master logged no traceback")


PHASES = {
    "bootstrap": bootstrap,
    "nix-on-suse": nix_on_suse,
    "nixos": nixos,
    "rolling": rolling,
    "service": service,
    "heal": heal,
}


async def test(vms: Machines) -> None:
    assert vms.phase in PHASES, vms.phase
    await PHASES[vms.phase](vms)
