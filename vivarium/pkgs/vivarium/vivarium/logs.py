"""`vivarium logs`: a run's events, filtered, as `log` shows them.

`monitor` follows a run's progress to its verdict; this reads what the
run wrote, guests' consoles and journals included, from `events.jsonl`.
It works on a live run, a stopped environment and a kept `.attempt`
output alike.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import anyio

from . import environments, monitor
from .events import Event, Kind, NotAnEvent, render
from .exits import Exit
from .journal import Tail

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

EVENTS: Final = "events.jsonl"
POLL: Final = 0.25
"""Seconds between reads while following: a file has no notification here."""
CHECK: Final = 1.0
"""Seconds between looks whether the runner still runs. Its lock, not its
socket: each `state` request is an event in the very log being followed."""


@dataclass(frozen=True, slots=True)
class Filter:
    """Which events to show. Each field given narrows; within one field,
    any value matches."""

    kinds: tuple[str, ...] = ()
    machines: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    """A journal entry's unit, with or without `.service`, or its syslog identifier."""
    phase: str | None = None
    case: str | None = None
    contains: str | None = None

    def matches(self, event: Event) -> bool:
        return self._fields(event.kind.value, event.machine, event.phase, event.text, event.data)

    def matches_body(self, body: Mapping[str, Any]) -> bool:  # Any: one JSON line's values
        """The same test over a line this version's `Event` does not read,
        such as a newer runner's kind: its keys, as `Event.as_dict` writes them."""
        data = body.get("data")
        return self._fields(
            str(body.get("kind", "")),
            body.get("machine"),
            body.get("phase"),
            str(body.get("text", "")),
            data if isinstance(data, dict) else {},
        )

    def _fields(  # Any: an event's data, whose keys its kind decides
        self, kind: str, machine: object, phase: object, text: str, data: Mapping[str, Any]
    ) -> bool:
        if self.kinds and kind not in self.kinds:
            return False
        if self.machines and machine not in self.machines:
            return False
        if self.phase is not None and phase != self.phase:
            return False
        if self.units and not self._unit(data):
            return False
        if self.case is not None and self.case not in str(data.get("case", "")):
            return False
        return self.contains is None or self.contains in text

    def _unit(self, data: Mapping[str, Any]) -> bool:  # Any: as `_fields`
        unit = str(data.get("unit", ""))
        identifier = str(data.get("identifier", ""))
        return any(want in (unit, unit.removesuffix(".service"), identifier) for want in self.units)


def matching(lines: Iterable[str], wanted: Filter) -> Iterator[tuple[str, Event | None]]:
    """Each line *wanted* matches, with its event; None for a JSON object
    this version's `Event` does not read, which is kept, not dropped. A
    line that is no JSON object is skipped."""
    for line in lines:
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            continue
        try:
            event = Event.from_dict(body)
        except NotAnEvent:
            if wanted.matches_body(body):
                yield line, None
            continue
        if wanted.matches(event):
            yield line, event


def shown(lines: Iterable[str], wanted: Filter, *, as_json: bool) -> Iterator[str]:
    for line, event in matching(lines, wanted):
        yield line if as_json or event is None else render(event)


def last[T](lines: Iterable[T], count: int | None) -> list[T]:
    """The last *count* of *lines*, holding no more than that; all for None."""
    return list(lines) if count is None else list(collections.deque(lines, maxlen=count))


async def read(out: Path, wanted: Filter, *, count: int | None, follow: bool, as_json: bool) -> int:
    tail = Tail(out / EVENTS)
    if not await anyio.Path(out / EVENTS).exists():
        sys.stderr.write(f"vivarium logs: no run in {out}: no {EVENTS}\n")
        return Exit.NO_RUN
    for line in last(shown(await tail.read(), wanted, as_json=as_json), count):
        sys.stdout.write(line + "\n")
    # Inside `main`'s guard: a reader gone shows here, not at exit.
    sys.stdout.flush()
    if not follow:
        return 0
    checked = anyio.current_time()
    up = await anyio.to_thread.run_sync(environments.running, out)
    while up:
        await anyio.sleep(POLL)
        if anyio.current_time() - checked >= CHECK:
            checked = anyio.current_time()
            up = await anyio.to_thread.run_sync(environments.running, out)
        # After the look: a run gone now wrote its last line before it went.
        for line in shown(await tail.read(), wanted, as_json=as_json):
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
    return 0


def add(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`logs`: the runner and the `vivarium` CLI both take it."""
    logs = sub.add_parser(
        "logs",
        help="a run's events, guests' consoles and journals included, filtered",
        description=(
            "Print a run's events as its `log` file shows them, from events.jsonl. Works on a live"
            " run, a stopped environment and a kept output. Each filter given narrows; one given"
            " twice matches either value."
        ),
    )
    logs.add_argument("target", help="the run's --out directory, its id, or an environment's name")
    logs.add_argument("--guest", "-g", action="append", default=[], help="one guest's events")
    logs.add_argument(
        "--unit",
        "-u",
        action="append",
        default=[],
        help="journal entries of a unit (`kubelet` or `kubelet.service`) or a syslog identifier",
    )
    logs.add_argument(
        "--kind",
        "-k",
        action="append",
        default=[],
        choices=[kind.value for kind in Kind],
        help="one kind of event: journal, console, rpc, output, case, error...",
    )
    logs.add_argument("--phase", help="the events of one phase")
    logs.add_argument("--case", help="pytest cases whose name holds this")
    logs.add_argument("--grep", metavar="TEXT", help="events whose text holds TEXT")
    logs.add_argument("--lines", "-n", type=int, metavar="N", help="only the last N matches so far")
    logs.add_argument("--follow", "-f", action="store_true", help="then print new events until the run ends")
    logs.add_argument("--json", action="store_true", help="each event as its JSON line")


def main(args: argparse.Namespace) -> int:
    out = monitor.directory_of(args.target)
    wanted = Filter(
        kinds=tuple(args.kind),
        machines=tuple(args.guest),
        units=tuple(args.unit),
        phase=args.phase,
        case=args.case,
        contains=args.grep,
    )
    try:
        return anyio.run(
            lambda: read(out, wanted, count=args.lines, follow=args.follow, as_json=args.json),
        )
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `vivarium logs x | head`: the reader left. Python would report
        # the pipe again as it flushes stdout at exit.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
