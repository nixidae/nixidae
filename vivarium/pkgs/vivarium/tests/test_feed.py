"""The feed `watch` serves: what it keeps, and when a watcher's stream ends."""

from __future__ import annotations

import anyio
import pytest
from vivarium.events import Event, Kind, Level
from vivarium.feed import Feed
from vivarium.monitor import LIVE


def _event(kind: Kind, **data: object) -> Event:
    return Event(at=0.0, kind=kind, level=Level.INFO, text="", phase="boot", data=dict(data))


@pytest.mark.anyio
async def test_a_watcher_gets_what_came_before_then_live_then_the_rest() -> None:
    feed = Feed("r")
    feed.emit(_event(Kind.PHASE_STARTED))
    feed.emit(_event(Kind.CONSOLE))
    with feed.watch() as events:
        feed.emit(_event(Kind.RUN_FINISHED, passed=True, states={"boot": "passed"}))
        feed.close()
        with anyio.fail_after(5):
            seen = [item async for item in events]
    assert [item.get("event") for item in seen] == ["progress", "live", "finished"]
    assert seen[1] == LIVE


@pytest.mark.anyio
async def test_a_watcher_after_close_gets_the_whole_run_and_an_end() -> None:
    feed = Feed("r")
    feed.emit(_event(Kind.RUN_FINISHED, passed=False, states={}))
    feed.close()
    with feed.watch() as events, anyio.fail_after(5):
        seen = [item async for item in events]
    assert [item.get("event") for item in seen] == ["finished", "live"]


@pytest.mark.anyio
async def test_drained_waits_for_every_watcher() -> None:
    feed = Feed("r")
    with feed.watch():
        feed.close()
        with anyio.move_on_after(0.2) as scope:
            await feed.drained()
        assert scope.cancelled_caught, "drained while a watcher was still reading"
    with anyio.fail_after(5):
        await feed.drained()


@pytest.mark.anyio
async def test_an_event_from_a_worker_thread_reaches_the_watcher() -> None:
    """A pytest phase emits from the thread it runs in."""
    feed = Feed("r")
    with feed.watch() as events:
        await anyio.to_thread.run_sync(feed.emit, _event(Kind.PHASE_STARTED))
        feed.close()
        with anyio.fail_after(5):
            seen = [item async for item in events]
    assert [item.get("event") for item in seen] == ["live", "progress"]
