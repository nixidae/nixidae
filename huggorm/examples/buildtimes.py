"""How long Hydra took to build a derivation, from what it publishes.

Two sources, neither complete:

- cache.nixos.org/log/<drv>: the build log, for every derivation Hydra
  built, intermediates included. stdenv prints `<phase> completed in ...`
  only for a phase of 30 s or more (pkgs/stdenv/generic/setup.sh,
  `showPhaseFooter`), so the sum is a lower bound. A log with no such
  line means every phase took under 30 s.
- hydra.nixos.org job JSON: `stoptime - starttime`, exact, for jobs only.
  A build that Hydra reused from another jobset reads 0 s; its HTML page
  names the build that ran ("Cached from: ... build N").

The log's Last-Modified header is the end time of the build. The cache
holds no start time, and Hydra's queue wait makes the gap between two end
times useless as a duration.
"""

from __future__ import annotations

import enum
import json
import re
import urllib.error
import urllib.request
from compression import zstd
from dataclasses import dataclass

import anyio
import anyio.to_thread

CACHE = "https://cache.nixos.org"
HYDRA = "https://hydra.nixos.org"
USER_AGENT = "attrdiff/0.1 (huggorm example)"
PHASE_FOOTER = re.compile(
    r"^(\w+) completed in (?:(\d+) hours )?(?:(\d+) minutes )?(\d+) seconds$",
    re.MULTILINE,
)
CACHED_FROM = re.compile(r"Cached from:.*?/build/(\d+)", re.DOTALL)


class Source(enum.StrEnum):
    """Where a time comes from, best first.

    NEARBY is the latest trunk build of the same job, another `.drv`: the
    stand-in when Hydra has not reached the derivation asked about yet."""

    HYDRA = "hydra"
    LOG = "log-lower-bound"
    HYDRA_NEARBY = "hydra-nearby"
    LOG_NEARBY = "log-lower-bound-nearby"


@dataclass(frozen=True, slots=True)
class Timing:
    seconds: int
    source: Source
    drv: str


def phase_seconds(log: str) -> int:
    """The sum of the phase footers in a stdenv build log."""
    total = 0
    for _, hours, minutes, seconds in PHASE_FOOTER.findall(log):
        total += int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds)
    return total


def _get(url: str, *, accept: str | None = None) -> bytes | None:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body: bytes = response.read()
            if response.headers.get("Content-Encoding") == "zstd":
                return zstd.decompress(body)
            return body
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def log_timing(drv: str, source: Source = Source.LOG) -> Timing | None:
    body = _get(f"{CACHE}/log/{drv}")
    if body is None:
        return None
    return Timing(phase_seconds(body.decode(errors="replace")), source, drv)


def _build_json(build: int | str) -> dict[str, object] | None:
    # Hydra's build JSON has no fixed schema worth modelling here; three
    # fields are read and checked.
    body = _get(f"{HYDRA}/build/{build}", accept="application/json")
    return json.loads(body) if body else None


@dataclass(frozen=True, slots=True)
class Latest:
    drv: str
    seconds: int | None


def hydra_latest(job: str) -> Latest | None:
    """The latest trunk build of `job`: its `.drv`, and how long it ran.

    `seconds` is None when Hydra reused the build and the page names no
    build that ran."""
    body = _get(f"{HYDRA}/job/nixpkgs/trunk/{job}/latest", accept="application/json")
    if body is None:
        return None
    build = json.loads(body)
    drv = str(build.get("drvpath", "")).removeprefix("/nix/store/")
    seconds: int | None = int(build["stoptime"]) - int(build["starttime"])
    if seconds == 0:
        page = _get(f"{HYDRA}/build/{build['id']}")
        found = CACHED_FROM.search(page.decode(errors="replace")) if page else None
        origin = _build_json(found.group(1)) if found else None
        seconds = int(str(origin["stoptime"])) - int(str(origin["starttime"])) if origin else None
    return Latest(drv, seconds)


async def fetch(
    candidates: dict[str, list[str]],
    jobs: dict[str, str],
    *,
    hydra: bool,
    cache_limit: int = 16,
    hydra_limit: int = 2,
) -> dict[str, Timing]:
    """A timing for each key of `candidates`, where one exists.

    `candidates` names, per derivation, the `.drv`s whose log may stand in
    for it, best first. `jobs` maps a derivation to its Hydra job name.
    Hydra is a shared, backlogged service: few requests at once."""
    cache_slots = anyio.CapacityLimiter(cache_limit)
    hydra_slots = anyio.CapacityLimiter(hydra_limit)
    out: dict[str, Timing] = {}

    async def log(drv: str, source: Source) -> Timing | None:
        return await anyio.to_thread.run_sync(log_timing, drv, source, limiter=cache_slots)

    async def one(key: str, drvs: list[str]) -> None:
        latest = None
        if hydra and key in jobs:
            latest = await anyio.to_thread.run_sync(hydra_latest, jobs[key], limiter=hydra_slots)
        if latest is not None and latest.seconds is not None and latest.drv in drvs:
            out[key] = Timing(latest.seconds, Source.HYDRA, latest.drv)
            return
        for drv in drvs:
            if (timing := await log(drv, Source.LOG)) is not None:
                out[key] = timing
                return
        if latest is None:
            return
        if latest.seconds is not None:
            out[key] = Timing(latest.seconds, Source.HYDRA_NEARBY, latest.drv)
        elif (timing := await log(latest.drv, Source.LOG_NEARBY)) is not None:
            out[key] = timing

    async with anyio.create_task_group() as tg:
        for key, drvs in candidates.items():
            tg.start_soon(one, key, drvs)
    return out
