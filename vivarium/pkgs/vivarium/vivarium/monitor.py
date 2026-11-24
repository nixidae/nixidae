"""`vivarium monitor`: a run's events, one line each, for a process that watches stdout.

Every run with a control socket serves `watch` on it (`vivarium/feed.py`):
the events `vivarium-mcp` pushes over its Claude channel -- progress, a
pause, a failed phase, the verdict. Channels need a development flag;
a command whose every stdout line is an event needs nothing, and Claude
Code's Monitor tool shows it as a running shell. So the socket carries
the channel to whoever is watching, flag or not.

A client connecting late gets the run's earlier events first, then the
live ones. The run closes the stream after the verdict, and the exit
status says what the verdict was.

A run with no control socket (`--no-control`), or a runner too old to
serve `watch`, leaves the monitor `events.jsonl`: it makes the same
events from it with `channel_event`. The run is up while `control.sock`
exists; the run writes its verdict before it removes the socket, so a
run gone with no verdict exits 2.

`--quiet` leaves out `progress` and `resumed`, so an agent that runs this
in a Monitor wakes for a pause, a failure and the verdict, and not for
every phase that passed.

The monitor exits with 4 when the run pauses, because a pause waits for
someone to act. A harness that wakes an agent only when a background
command ends then brings the agent back at the pause. After `resume`, arm
it again. `--through-pauses` prints the pause and goes on to the verdict.
A pause in the replayed events counts only when no `resumed` follows it,
and the server sends `LIVE` after the replay, so a monitor armed while
the run is paused exits at once, and one armed after a resume does not.

A monitor armed before its run starts waits for it (`--wait`): a driver
evaluates and builds for minutes before it writes `events.jsonl`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Final

import anyio

from . import places
from .control import RUN_FILE, SOCKET, watch
from .events import Event, Kind, NotAnEvent
from .exits import Exit
from .journal import Tail
from .phases import PhaseState


class Channel(StrEnum):
    """What a channel event says happened: its `event` key."""

    PROGRESS = "progress"
    PAUSED = "paused"
    READY = "ready"
    RESUMED = "resumed"
    FAILED = "failed"
    FINISHED = "finished"
    EXITED = "exited"
    BUILDING = "building"
    """`vivarium run` started to build one derivation."""
    BUILDS = "builds"
    """The build and download counts moved. JSON only."""


TERMINAL: Final = frozenset({Channel.FINISHED, Channel.EXITED})
"""The events after which a run sends nothing more."""

QUIET_SKIPS: Final = frozenset({Channel.PROGRESS, Channel.RESUMED, Channel.BUILDING, Channel.BUILDS})
"""What `--quiet` leaves out: nobody needs to wake for these."""

LIVE: Final = {"event": "live"}
"""What the server sends after the replayed events. It is not an event and
is not printed."""

HOLDS: Final = frozenset({Channel.PAUSED, Channel.READY})
"""The events after which the run waits for someone to act."""

WAIT: Final = 1800.0
"""Seconds a monitor waits for a run that has not started. A driver's
evaluation and build come first; a failed build is the driver's own exit,
which its caller sees, so the wait only bounds a monitor nobody stops."""

GRACE: Final = 10.0
"""Seconds a file monitor waits for `control.sock` to appear before it takes
the run as gone. The run binds it moments after `events.jsonl` starts."""


CLIP: Final = 4000
"""Characters of one text a channel event, a monitor line or a tool reply
carries. A failed `succeed` puts the command's whole output in the
phase's error."""


def clip(text: str, where: str, limit: int = CLIP) -> str:
    """`text`, or its head and tail when it is longer than `limit`, with a
    line that names `where` the whole text is."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[{len(text) - 2 * half} characters elided; the whole text is in {where}]\n{text[-half:]}"


def channel_event(event: Event, run: str) -> tuple[str, dict[str, str]] | None:
    """What of the run's event stream is worth interrupting Claude for.

    A pause, a failed phase and the verdict. Everything else stays in
    `events.jsonl`, where `events` can ask for it: a channel event is
    context in the conversation, and a journal line each is a flood.
    Meta keys are identifiers only; Claude Code drops any other key.
    """
    data = event.data
    phase = event.phase or ""
    meta = {"run": run}
    # Facts only. Claude Code frames channel content as untrusted and
    # tells the model not to act on imperative language in it, so what
    # to do next belongs in the server's `instructions`, which it trusts.
    if event.kind is Kind.RESUMED:
        meta |= {"event": Channel.RESUMED, "reason": str(data.get("reason", ""))}
        return f"resumed, paused {meta['reason']}", meta
    if event.kind is Kind.READY:
        meta |= {"event": Channel.READY}
        return "ready; the guests are up until the run is stopped", meta
    if event.kind is Kind.PAUSED:
        meta |= {"event": Channel.PAUSED, "reason": str(data.get("reason", ""))}
        return f"paused {meta['reason']}; the guests are up until the run is resumed or stopped", meta
    # Progress, so a run of many minutes is not silent until it ends:
    # measured, a nixkube run showed the user nothing for its first
    # quarter of an hour. One event per phase boundary, not per line.
    if event.kind is Kind.PHASE_STARTED:
        meta |= {"event": Channel.PROGRESS, "phase": phase, "state": "started"}
        return f"phase {phase} started", meta
    if event.kind is Kind.PHASE_FINISHED and data.get("state") == PhaseState.PASSED:
        meta |= {"event": Channel.PROGRESS, "phase": phase, "state": "passed"}
        return f"phase {phase} passed in {event.seconds or 0:.0f}s", meta
    if event.kind is Kind.PHASE_FINISHED and data.get("state") == PhaseState.FAILED:
        meta |= {"event": Channel.FAILED, "phase": phase}
        error = clip(str(data.get("error", "")), "events.jsonl and phases.json")
        return f"phase {phase} failed: {error}".strip(), meta
    if event.kind is Kind.RUN_FINISHED:
        passed = bool(data.get("passed"))
        meta |= {"event": Channel.FINISHED, "passed": "true" if passed else "false"}
        states: dict[str, str] = data.get("states") or {}
        summary = ", ".join(f"{name} {state}" for name, state in states.items())
        if stopped := data.get("stopped"):
            meta["stopped"] = "true"
            return f"run stopped ({stopped}): {summary}", meta
        return f"run {'passed' if passed else 'failed'}: {summary}", meta
    return None


def shown(event: dict[str, Any], *, quiet: bool, as_json: bool = False) -> bool:
    """`builds` is JSON only: one line per download is too many for a
    person, and each `building` line carries the counts."""
    if event.get("event") == Channel.BUILDS and not as_json:
        return False
    return not (quiet and event.get("event") in QUIET_SKIPS)


def status(event: dict[str, Any]) -> Exit | None:
    """The exit status a terminal event means, or `None` for any other."""
    if event.get("event") == Channel.FINISHED and event.get("stopped") == "true":
        return Exit.STOPPED
    if event.get("event") == Channel.FINISHED:
        return Exit.PASSED if event.get("passed") == "true" else Exit.FAILED
    if event.get("event") == Channel.EXITED:
        return Exit.NO_VERDICT
    return None


def held(events: list[dict[str, Any]]) -> Channel | None:
    """The open hold after these events, `paused` or `ready`: one with no
    `resumed` or verdict after it. `None` while the run runs."""
    for event in reversed(events):
        if event.get("event") in HOLDS:
            return Channel(event["event"])
        if event.get("event") in {Channel.RESUMED, *TERMINAL}:
            return None
    return None


def paused_now(events: list[dict[str, Any]]) -> bool:
    """Whether the run waits for someone after these events: paused, or an
    environment that is ready."""
    return held(events) is not None


def line(event: dict[str, Any], *, as_json: bool = False) -> str:
    """One event as one line. A watcher takes each line as one event, so a
    message of several lines, such as a traceback's tail, is joined."""
    if as_json:
        return json.dumps(event, separators=(",", ":"))
    head = " ".join(str(event[key]) for key in ("run", "event", "phase") if event.get(key))
    text = " | ".join(part.strip() for part in str(event.get("text", "")).splitlines() if part.strip())
    return f"{head}: {text}" if text else head


def directory_of(target: str) -> Path:
    """A run's directory, named by its `--out`, its id, or its environment.

    `vivarium-mcp` makes each run's directory under the temporary directory and
    names the run after it, so an id alone is enough. An environment's name
    is its directory under the state directory. A target that names none of
    these yet is a `--out` whose run has not started.
    """
    path = Path(target)
    if path.is_dir():
        return path.absolute()
    by_id = Path(tempfile.gettempdir()) / target
    if by_id.is_dir():
        return by_id
    if places.NAME.fullmatch(target) and (environment := places.environment(target)).is_dir():
        return environment
    return path.absolute()


def locate(target: str) -> Path:
    """The control socket of the run `directory_of(target)` names."""
    return places.socket(directory_of(target), SOCKET)


BUILD_LOG: Final = "build.jsonl"
"""What `vivarium run` writes while it evaluates and builds, before a runner exists."""

BUILD: Final = 105
"""`ActivityType.BUILD`: one derivation being built."""

SUBSTITUTE: Final = 108
"""`ActivityType.SUBSTITUTE`: one path fetched from a substituter."""

PLANNED: Final = "planned"
"""Not Nix's: `vivarium run` writes it before it builds, the paths of one
kind (`type`, `BUILD` or `SUBSTITUTE`) that Nix's `query_missing` named."""


def _store_name(path: str) -> str:
    """`<hash>-<name>`: a plan spells a path without the store directory,
    Nix's activities with it."""
    return path.rsplit("/", 1)[-1]


@dataclass
class BuildTally:
    """Builds and downloads of one run, by store path, so a path two plans
    name or two activities touch counts once. A build no plan named, as an
    import from a derivation, joins the total when it starts."""

    planned: dict[int, set[str]] = field(default_factory=lambda: {BUILD: set(), SUBSTITUTE: set()})
    started: dict[int, set[str]] = field(default_factory=lambda: {BUILD: set(), SUBSTITUTE: set()})
    done: dict[int, set[str]] = field(default_factory=lambda: {BUILD: set(), SUBSTITUTE: set()})
    running: dict[int, tuple[int, str]] = field(default_factory=dict)
    """Activity id to its kind and path, until it stops."""

    def counts(self) -> dict[str, str]:
        def total(kind: int) -> int:
            return len(self.planned[kind] | self.started[kind])

        return {
            "built": str(len(self.done[BUILD])),
            "builds": str(total(BUILD)),
            "running": str(sum(1 for kind, _ in self.running.values() if kind == BUILD)),
            "downloaded": str(len(self.done[SUBSTITUTE])),
            "downloads": str(total(SUBSTITUTE)),
        }

    def take(self, record: dict[str, Any], run: str) -> dict[str, str] | None:
        """The monitor event for one `build.jsonl` record, if it is one."""
        action, kind = record.get("action"), record.get("type")
        fields: list[Any] = record.get("fields") or []
        if action == PLANNED and kind in self.planned:
            self.planned[kind].update(_store_name(str(path)) for path in fields)
            return {"run": run, "event": Channel.BUILDS, "text": "planned", **self.counts()}
        if action == "start" and kind in self.started:
            path = _store_name(str(fields[0])) if fields else str(record.get("text", ""))
            self.started[kind].add(path)
            self.running[int(record.get("id", 0))] = (kind, path)
            if kind == BUILD:
                counts = self.counts()
                text = f"{record.get('text', '')} ({counts['built']}/{counts['builds']} built)"
                return {"run": run, "event": Channel.BUILDING, "text": text, **counts}
            return None
        if action == "stop" and int(record.get("id", 0)) in self.running:
            kind, path = self.running.pop(int(record.get("id", 0)))
            self.done[kind].add(path)
            return {"run": run, "event": Channel.BUILDS, "text": path, **self.counts()}
        if action == "failed":
            return {"run": run, "event": Channel.EXITED, "text": f"no run: {record.get('text', '')}"}
        return None


class Arrival(Enum):
    UP = "up"
    """The run has its events or its socket."""
    GONE = "gone"
    """The process `run.json` names exited before the run was up."""
    NEVER = "never"
    """Nothing within the wait."""


def starter_gone(out: Path) -> bool:
    """Whether `run.json` names a process that no longer exists."""
    try:
        pid = int(json.loads((out / RUN_FILE).read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


async def wait_for_run(out: Path, timeout: float = WAIT, printer: Watch | None = None) -> Arrival:
    """Until a run is up in `out`, for at most `timeout` seconds.

    Meanwhile each build that `build.jsonl` starts goes to `printer`, and a
    run that died first says why there. Polls: these are files, and a file
    has no notification here.
    """
    log = Tail(out / BUILD_LOG)
    tally = BuildTally()

    async def up() -> bool:
        return await anyio.Path(out / "events.jsonl").exists() or await anyio.Path(places.socket(out, SOCKET)).exists()

    with anyio.move_on_after(timeout):
        while True:
            if await up():
                return Arrival.UP
            for raw in await log.read():
                with contextlib.suppress(json.JSONDecodeError):
                    event = tally.take(json.loads(raw), out.name)
                    if event is not None and printer is not None:
                        printer.take(event)
            # `up` again: a run can come up and end between the two looks.
            if starter_gone(out) and not await up():
                if printer is not None and not any(e.get("event") == Channel.EXITED for e in printer.seen):
                    printer.take({"run": out.name, "event": Channel.EXITED, "text": "run exited before it started"})
                return Arrival.GONE
            await anyio.sleep(0.25)
    return Arrival.NEVER


class Watch:
    """Prints each event and says when to stop: the verdict, or with
    `until_pause` an open pause once the replay is over. Without `replay`,
    only what happens from now on is printed: `ctl continue --wait`."""

    def __init__(
        self, *, as_json: bool = False, quiet: bool = False, until_pause: bool = True, replay: bool = True
    ) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self.until_pause = until_pause
        self.replay = replay
        self.seen: list[dict[str, Any]] = []
        self.live = False

    def take(self, event: dict[str, Any]) -> Exit | None:
        if event == LIVE:
            self.live = True
        else:
            self.seen.append(event)
            if (self.live or self.replay) and shown(event, quiet=self.quiet, as_json=self.as_json):
                print(line(event, as_json=self.as_json), flush=True)
            code = status(event)
            if code is not None:
                return code
        if self.until_pause and self.live and (hold := held(self.seen)) is not None:
            return Exit.READY if hold is Channel.READY else Exit.PAUSED
        return None


async def follow(socket: Path, **options: bool) -> Exit:
    """Print the run's events from `watch` on its control socket until its
    verdict, or with `until_pause` until it is paused; the exit status.
    Raises `WatchRefused` for a run that serves no `watch`."""
    printer = Watch(**options)
    async for event in watch(socket):
        code = printer.take(event)
        if code is not None:
            return code
    return Exit.NO_RUN


async def follow_file(out: Path, *, grace: float = GRACE, **options: bool) -> Exit:
    """`follow`, from `<out>/events.jsonl`, for a run that serves no `watch`.

    Polls: the file is all such a run offers, and a file has no
    notification here."""
    printer = Watch(**options)
    tail = Tail(out / "events.jsonl")
    control = anyio.Path(places.socket(out, SOCKET))
    started = anyio.current_time()
    was_up = False
    while True:
        # Before the read: a socket gone now means the verdict, if any, is
        # already in the file.
        up = await control.exists()
        was_up = was_up or up
        for raw in await tail.read():
            try:
                pushed = channel_event(Event.from_dict(json.loads(raw)), out.name)
            except (json.JSONDecodeError, NotAnEvent):
                continue
            if pushed is not None:
                code = printer.take({**pushed[1], "text": pushed[0]})
                if code is not None:
                    return code
        if not printer.live:
            code = printer.take(LIVE)
            if code is not None:
                return code
        if not up and (was_up or anyio.current_time() - started > grace):
            return Exit.NO_VERDICT
        await anyio.sleep(0.25)
