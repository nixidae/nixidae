"""A guest drives vivarium's cloud API: launch, capacity, list, notice, terminate. nixidae/nixidae#1408."""

from __future__ import annotations

import json
import shlex
from typing import Any

from vivarium_runner import Machine, Machines

API = "curl --silent --show-error --unix-socket /run/vivarium/cloud.sock --write-out '\\n%{http_code}'"


async def call(vm: Machine, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    data = "" if body is None else f" --header 'Content-Type: application/json' --data {shlex.quote(json.dumps(body))}"
    out = await vm.succeed(f"{API} --request {method}{data} http://cloud{path}")
    text, _, status = out.rpartition("\n")
    return int(status), json.loads(text)


async def test(vms: Machines) -> None:
    cloud, controller = vms.cloud, vms.controller
    assert cloud is not None
    cloud.capacity["worker"] = 1
    cloud.boot_delay = 1.0

    status, first = await call(
        controller, "POST", "/v1/instances", {"template": "worker", "tags": {"pool": "build"}, "user_data": "join-1"}
    )
    assert (status, first["state"], first["launched_by"]) == (202, "pending", "controller"), first
    status, refused = await call(controller, "POST", "/v1/instances", {"template": "worker"})
    assert status == 409, refused
    print(f"[test] {first['id']} pending; a second launch is refused: {refused['error']}")

    worker = await cloud.wait_running(first["id"])
    assert await worker.succeed("cat /run/vivarium/user-data") == "join-1"
    await controller.succeed("ping -c2 -W5 10.9.0.100")
    status, own = await call(worker, "GET", "/v1/self")
    assert (status, own["id"], own["state"], own["notice"]) == (200, first["id"], "running", None), own
    assert (await call(controller, "GET", "/v1/self"))[0] == 404
    _, listed = await call(controller, "GET", "/v1/instances?tag:pool=build")
    assert [one["id"] for one in listed["instances"]] == [first["id"]]
    _, other = await call(controller, "GET", "/v1/instances?tag:pool=other")
    assert other["instances"] == []
    print(f"[test] {first['id']} runs, reads its user data and itself, and lists by tag")

    await cloud.interrupt(first["id"], 5)
    _, own = await call(worker, "GET", "/v1/self")
    assert own["notice"] is not None, own
    await cloud.wait_ended(first["id"])
    _, gone = await call(controller, "GET", f"/v1/instances/{first['id']}")
    assert gone["state"] == "terminated", gone
    await controller.fail("ping -c1 -W2 10.9.0.100")
    print(f"[test] {first['id']} saw its notice and ended with it")

    status, second = await call(controller, "POST", "/v1/instances", {"template": "worker"})
    assert status == 202, second
    await cloud.wait_running(second["id"])
    status, _ = await call(controller, "DELETE", f"/v1/instances/{second['id']}")
    assert status == 202
    _, done = await call(controller, "GET", f"/v1/instances/{second['id']}")
    assert done["state"] == "terminated", done
    assert sorted(vms) == ["controller"]
    print(f"[test] capacity came back; {second['id']} was terminated through the API")

    status, third = await call(controller, "POST", "/v1/instances", {"template": "worker"})
    assert status == 202, third
    worker = await cloud.wait_running(third["id"])
    # Not `poweroff` itself: the agent goes down before it can answer.
    await worker.succeed("systemd-run --on-active=1 systemctl poweroff")
    await cloud.wait_ended(third["id"], timeout=120)
    _, off = await call(controller, "GET", f"/v1/instances/{third['id']}")
    assert (off["state"], off["error"]) == ("terminated", None), off
    status, again = await call(controller, "DELETE", f"/v1/instances/{third['id']}")
    assert (status, again["state"]) == (202, "terminated"), again
    assert sorted(vms) == ["controller"]
    print(f"[test] {third['id']} powered itself off and was terminated; a delete after it is no error")

    status, fourth = await call(controller, "POST", "/v1/instances", {"template": "worker"})
    assert status == 202, fourth
    worker = await cloud.wait_running(fourth["id"])
    await worker.crash()
    await cloud.wait_ended(fourth["id"], timeout=60)
    _, dead = await call(controller, "GET", f"/v1/instances/{fourth['id']}")
    assert dead["state"] == "failed" and "exit" in dead["error"], dead
    print(f"[test] {fourth['id']} was killed and failed: {dead['error']}")
