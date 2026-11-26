"""The run's pool: spares, clean and dirty guests, against a session that boots nothing. nixidae/nixidae#1413."""

from __future__ import annotations

import anyio
import pytest
from test_cloud import FakeSession, _run
from vivarium.cloud import Cloud, State
from vivarium.pool import TAG, Pool, PoolError


def test_a_clean_guest_is_a_spare_and_another_boots_behind_it() -> None:
    async def main(cloud: Cloud, session: FakeSession) -> None:
        pool = Pool(cloud, spares=1)
        first = await pool.acquire("worker")
        assert cloud.instances[first].tags == {TAG: "worker"}
        live = [i for i in cloud.instances.values() if i.state is not State.TERMINATED]
        assert len(live) == 2, "the guest it gave, and one spare behind it"
        second = await pool.acquire("worker")
        assert second != first

    _run(main)


def test_a_clean_release_terminates_and_a_dirty_one_is_handed_out_again() -> None:
    async def main(cloud: Cloud, session: FakeSession) -> None:
        pool = Pool(cloud, spares=0)
        clean = await pool.acquire("worker")
        await pool.release(clean)
        await cloud.wait_ended(clean, timeout=5)
        dirty = await pool.acquire("worker", clean=False)
        await pool.release(dirty, dirty=True)
        assert await pool.acquire("worker", clean=False) == dirty
        await pool.release(dirty, dirty=True)
        assert await pool.acquire("worker") != dirty, "a clean request never takes a used guest"

    _run(main)


def test_a_dirty_guest_that_stopped_is_not_handed_out() -> None:
    async def main(cloud: Cloud, session: FakeSession) -> None:
        pool = Pool(cloud, spares=0)
        used = await pool.acquire("worker", clean=False)
        await pool.release(used, dirty=True)
        session.vms_up[used].stop(1)
        await cloud.wait_ended(used, timeout=5)
        with anyio.fail_after(5):
            assert await pool.acquire("worker", clean=False) != used

    _run(main)


def test_a_release_of_a_guest_the_pool_did_not_give_is_refused() -> None:
    async def main(cloud: Cloud, session: FakeSession) -> None:
        with pytest.raises(PoolError, match="no guest the pool handed out"):
            await Pool(cloud).release("i-9999")
        with pytest.raises(PoolError, match="no template 'nope'"):
            await Pool(cloud).acquire("nope")

    _run(main)
