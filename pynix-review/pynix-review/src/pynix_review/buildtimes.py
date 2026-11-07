"""How long Hydra took to build a derivation, from what it publishes.

- cache.nixos.org/log/<drv>: one log per derivation Hydra built,
  intermediates included. stdenv prints `<phase> completed in ...` only for
  a phase of 30 s or more (pkgs/stdenv/generic/setup.sh, `showPhaseFooter`),
  so the sum is a lower bound. On NixOS/nixpkgs#584542 it was 86 % of
  Hydra's time over 57 builds, and 0 for 46 of them.
- hydra.nixos.org job JSON: `stoptime - starttime`, exact, for jobs only.
  A build Hydra reused from another jobset reads 0 s; its HTML page names the
  build that ran ("Cached from: ... build N"). Hydra is shared and
  backlogged: few requests at once.

The log's Last-Modified header is the end of the build. Nothing holds the
start, and Hydra's queue wait makes the gap between two ends no duration.
"""

from __future__ import annotations

import contextlib
import enum
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import anyio
import httpx
from compression import zstd
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

CACHE: Final = "https://cache.nixos.org"
HYDRA: Final = "https://hydra.nixos.org"
USER_AGENT: Final = "pynix-review/0.1"
PHASE_FOOTER: Final = re.compile(
    r"^(\w+) completed in (?:(\d+) hours )?(?:(\d+) minutes )?(\d+) seconds$",
    re.MULTILINE,
)
CACHED_FROM: Final = re.compile(r"Cached from:.*?/build/(\d+)", re.DOTALL)


class Source(enum.StrEnum):
    """Where a time comes from, best first.

    NEARBY is the latest trunk build of the same job, another `.drv`: the
    stand-in when Hydra has not reached the derivation asked about."""

    HYDRA = "hydra"
    LOG = "log-lower-bound"
    HYDRA_NEARBY = "hydra-nearby"
    LOG_NEARBY = "log-lower-bound-nearby"


@dataclass(frozen=True, slots=True)
class Timing:
    seconds: int
    source: Source
    drv: str


class _Build(BaseModel):
    id: int
    drvpath: str
    starttime: int
    stoptime: int


def phase_seconds(log: str) -> int:
    """The sum of the phase footers in a stdenv build log."""
    total = 0
    for _, hours, minutes, seconds in PHASE_FOOTER.findall(log):
        total += int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds)
    return total


async def _get(client: httpx.AsyncClient, url: str, *, accept: str | None = None) -> bytes | None:
    response = await client.get(url, headers={"Accept": accept} if accept else None)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    # Without the `zstandard` extra, httpx 0.28 returns a zstd body as it
    # came, and raises nothing.
    if response.headers.get("content-encoding") == "zstd":
        return zstd.decompress(response.content)
    return response.content


async def log_timing(client: httpx.AsyncClient, drv: str, source: Source = Source.LOG) -> Timing | None:
    body = await _get(client, f"{CACHE}/log/{drv}")
    if body is None:
        return None
    return Timing(phase_seconds(body.decode(errors="replace")), source, drv)


async def _build(client: httpx.AsyncClient, url: str) -> _Build | None:
    body = await _get(client, url, accept="application/json")
    return _Build.model_validate_json(body) if body else None


async def hydra_latest(client: httpx.AsyncClient, job: str) -> tuple[str, int | None] | None:
    """The latest trunk build of `job`: its `.drv`, and how long it ran.

    The seconds are None when Hydra reused the build and its page names no
    build that ran."""
    build = await _build(client, f"{HYDRA}/job/nixpkgs/trunk/{job}/latest")
    if build is None:
        return None
    drv = build.drvpath.removeprefix("/nix/store/")
    if build.stoptime > build.starttime:
        return drv, build.stoptime - build.starttime
    page = await _get(client, f"{HYDRA}/build/{build.id}")
    found = CACHED_FROM.search(page.decode(errors="replace")) if page else None
    origin = await _build(client, f"{HYDRA}/build/{found.group(1)}") if found else None
    return drv, (origin.stoptime - origin.starttime) if origin else None


async def fetch(
    candidates: Mapping[str, Sequence[str]],
    jobs: Mapping[str, str],
    *,
    hydra: bool,
    client: httpx.AsyncClient | None = None,
    hydra_limit: int = 2,
) -> dict[str, Timing]:
    """A timing for each key of `candidates`, where one exists.

    `candidates` names, per derivation, the `.drv`s whose log may stand in
    for it, best first. `jobs` maps a derivation to its Hydra job. Order:
    Hydra on the same `.drv`, then the logs, then the job's latest build."""
    hydra_slots = anyio.CapacityLimiter(hydra_limit)
    out: dict[str, Timing] = {}

    async def one(http: httpx.AsyncClient, key: str, drvs: Sequence[str]) -> None:
        latest = None
        if hydra and key in jobs:
            async with hydra_slots:
                latest = await hydra_latest(http, jobs[key])
        if latest is not None and latest[1] is not None and latest[0] in drvs:
            out[key] = Timing(latest[1], Source.HYDRA, latest[0])
            return
        for drv in drvs:
            if (timing := await log_timing(http, drv)) is not None:
                out[key] = timing
                return
        if latest is None:
            return
        drv, seconds = latest
        if seconds is not None:
            out[key] = Timing(seconds, Source.HYDRA_NEARBY, drv)
        elif (timing := await log_timing(http, drv, Source.LOG_NEARBY)) is not None:
            out[key] = timing

    async with contextlib.AsyncExitStack() as stack:
        http = client or await stack.enter_async_context(new_client())
        async with anyio.create_task_group() as tg:
            for key, drvs in candidates.items():
                tg.start_soon(one, http, key, drvs)
    return out


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=16),
        timeout=30,
        follow_redirects=True,
    )
