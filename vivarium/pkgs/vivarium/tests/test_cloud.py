"""The host's cloud: launch, capacity, notice and terminate, against a session that boots nothing. nixidae/nixidae#1408."""

from __future__ import annotations

import asyncio
from http import HTTPStatus

from vivarium.cloud import Cloud, State


class FakeSession:
    """`Session` as `Cloud` uses it; a launch is up once *release* is set."""

    def __init__(self) -> None:
        self.templates = {"worker": object()}
        self.up: dict[str, str | None] = {}
        self.vms_up: dict[str, object] = {}
        self.release = asyncio.Event()
        self.release.set()
        self.started = asyncio.Event()

    async def launch(self, template: str, name: str, user_data: str | None) -> object:
        self.up[name] = None
        self.started.set()
        await self.release.wait()
        self.up[name] = user_data
        self.vms_up[name] = object()
        return self.vms_up[name]

    def holds(self, name: str) -> bool:
        return name in self.up

    async def terminate(self, name: str) -> None:
        del self.up[name]


def _cloud() -> tuple[Cloud, FakeSession]:
    session = FakeSession()
    return Cloud(session), session  # type: ignore[arg-type]  # a fake with the methods Cloud calls


def test_a_launch_is_pending_then_running_and_names_who_asked() -> None:
    async def main() -> None:
        cloud, session = _cloud()
        status, body = await cloud.handle("ctl", "launch", {"template": "worker", "tags": {"pool": "a"}, "user_data": "t"})
        assert (status, body["state"], body["launched_by"]) == (HTTPStatus.ACCEPTED, "pending", "ctl")
        await cloud.instances[body["id"]].settled.wait()
        assert cloud.instances[body["id"]].state is State.RUNNING
        assert session.up == {body["id"]: "t"}
        status, listed = await cloud.handle("ctl", "list", {"tags": {"pool": "a"}})
        assert [one["id"] for one in listed["instances"]] == [body["id"]]
        assert (await cloud.handle("ctl", "list", {"tags": {"pool": "b"}}))[1] == {"instances": []}
        status, own = await cloud.handle(body["id"], "self", {})
        assert (status, own["id"]) == (HTTPStatus.OK, body["id"])
        assert (await cloud.handle("ctl", "self", {}))[0] == HTTPStatus.NOT_FOUND

    asyncio.run(main())


def test_a_launch_past_capacity_is_refused_and_a_terminate_makes_room() -> None:
    async def main() -> None:
        cloud, _ = _cloud()
        cloud.capacity["worker"] = 1
        _, first = await cloud.handle(None, "launch", {"template": "worker"})
        status, refused = await cloud.handle(None, "launch", {"template": "worker"})
        assert status == HTTPStatus.CONFLICT
        assert "insufficient capacity" in refused["error"]
        await cloud.handle(None, "terminate", {"id": first["id"]})
        assert (await cloud.handle(None, "launch", {"template": "worker"}))[0] == HTTPStatus.ACCEPTED
        await cloud.close()

    asyncio.run(main())


def test_an_unknown_template_or_instance_is_not_found() -> None:
    async def main() -> None:
        cloud, _ = _cloud()
        assert (await cloud.handle(None, "launch", {"template": "nope"}))[0] == HTTPStatus.NOT_FOUND
        assert (await cloud.handle(None, "describe", {"id": "i-9999"}))[0] == HTTPStatus.NOT_FOUND

    asyncio.run(main())


def test_terminating_a_pending_instance_unplugs_what_its_boot_held() -> None:
    async def main() -> None:
        cloud, session = _cloud()
        session.release.clear()
        _, body = await cloud.handle(None, "launch", {"template": "worker"})
        await session.started.wait()
        assert session.holds(body["id"])
        await cloud.terminate(body["id"])
        assert not session.holds(body["id"])
        assert cloud.instances[body["id"]].state is State.TERMINATED

    asyncio.run(main())


def test_a_notice_is_visible_and_ends_the_instance() -> None:
    async def main() -> None:
        cloud, session = _cloud()
        _, body = await cloud.handle(None, "launch", {"template": "worker"})
        await cloud.wait_running(body["id"], timeout=5)
        await cloud.interrupt(body["id"], 0.1)
        assert (await cloud.handle(body["id"], "self", {}))[1]["notice"] is not None
        await cloud.wait_ended(body["id"], timeout=5)
        assert session.up == {}

    asyncio.run(main())
