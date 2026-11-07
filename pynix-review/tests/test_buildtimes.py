from __future__ import annotations

import json

import httpx
from compression import zstd

from pynix_review.buildtimes import Source, Timing, fetch, phase_seconds

CAIRO_LOG = """Running phase: unpackPhase
Running phase: buildPhase
buildPhase completed in 33 seconds
Running phase: checkPhase
checkPhase completed in 1 hours 2 minutes 5 seconds
Running phase: installPhase
"""


def test_phase_seconds_sums_every_footer() -> None:
    assert phase_seconds(CAIRO_LOG) == 33 + 3600 + 120 + 5


def test_phase_seconds_is_zero_without_a_footer() -> None:
    assert phase_seconds("Running phase: buildPhase\n") == 0


def _client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client.seen = seen  # type: ignore[attr-defined]  # the test reads which URLs were asked
    return client


def _log(text: str) -> httpx.Response:
    return httpx.Response(200, content=zstd.compress(text.encode()), headers={"content-encoding": "zstd"})


def _build(drv: str, start: int, stop: int, build: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps({"id": build, "drvpath": f"/nix/store/{drv}", "starttime": start, "stoptime": stop}),
    )


NEW = "b" * 32 + "-cairo-1.18.6.drv"
OLD = "a" * 32 + "-cairo-1.18.4.drv"
OTHER = "c" * 32 + "-cairo-1.18.5.drv"
JOB = "https://hydra.nixos.org/job/nixpkgs/trunk/cairo.x86_64-linux/latest"


async def test_log_of_the_twin_when_the_new_drv_has_none() -> None:
    client = _client({f"https://cache.nixos.org/log/{OLD}": _log(CAIRO_LOG)})
    out = await fetch({NEW: [NEW, OLD]}, {}, hydra=False, client=client)
    assert out == {NEW: Timing(3758, Source.LOG, OLD)}


async def test_hydra_on_the_same_drv_wins_over_a_log() -> None:
    client = _client(
        {
            JOB: _build(NEW, 100, 155),
            f"https://cache.nixos.org/log/{NEW}": _log(CAIRO_LOG),
        }
    )
    out = await fetch({NEW: [NEW]}, {NEW: "cairo.x86_64-linux"}, hydra=True, client=client)
    assert out == {NEW: Timing(55, Source.HYDRA, NEW)}


async def test_reused_build_follows_cached_from() -> None:
    page = httpx.Response(200, text='<th>Cached from:</th><td><a href="https://hydra.nixos.org/build/7">build 7</a>')
    client = _client(
        {
            JOB: _build(NEW, 100, 100, build=9),
            "https://hydra.nixos.org/build/9": page,
            "https://hydra.nixos.org/build/7": _build(NEW, 10, 65, build=7),
        }
    )
    out = await fetch({NEW: [NEW]}, {NEW: "cairo.x86_64-linux"}, hydra=True, client=client)
    assert out == {NEW: Timing(55, Source.HYDRA, NEW)}


async def test_nearby_build_when_nothing_else_has_a_record() -> None:
    client = _client({JOB: _build(OTHER, 0, 40)})
    out = await fetch({NEW: [NEW, OLD]}, {NEW: "cairo.x86_64-linux"}, hydra=True, client=client)
    assert out == {NEW: Timing(40, Source.HYDRA_NEARBY, OTHER)}


async def test_hydra_is_not_asked_without_the_flag() -> None:
    client = _client({JOB: _build(NEW, 0, 40)})
    out = await fetch({NEW: [NEW]}, {NEW: "cairo.x86_64-linux"}, hydra=False, client=client)
    assert out == {}
    assert client.seen == [f"https://cache.nixos.org/log/{NEW}"]  # type: ignore[attr-defined]  # set in _client
