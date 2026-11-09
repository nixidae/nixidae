"""Do phases on disjoint guests run at once, and is what they say filed right?

Reads the sandboxed run's `.attempt`. `left` holds `a`, `right` holds
`b`, `both` holds every guest and comes after them.

- `left` and `right` overlap in time.
- `both` overlaps nothing: a phase without `nodes` runs alone. The
  negative control, which a scheduler that ignored `nodes` fails.
- Every journal marker and every print carries the phase that held its
  guest, although two phases wrote at once.

    parallel ATTEMPT
"""

import sys
from dataclasses import dataclass
from pathlib import Path

from harness import Evidence, data

MARKERS = 12
"""3 on `a`, 3 on `b`, 6 from `both`."""
PRINTS = 9


@dataclass(frozen=True, slots=True)
class Span:
    start: float
    end: float


def first(events: list[dict[str, object]], kind: str) -> dict[str, float]:
    """The time of each phase's first event of `kind`."""
    found: dict[str, float] = {}
    for event in events:
        if event["kind"] == kind:
            found.setdefault(str(event["phase"]), float(str(event["at"])))
    return found


def spans(events: list[dict[str, object]]) -> dict[str, Span]:
    started, ended = first(events, "phase_started"), first(events, "phase_finished")
    return {phase: Span(started[phase], ended[phase]) for phase in started if phase in ended}


def main() -> None:
    run = Evidence(Path(sys.argv[1]))
    if run.status() != 0:
        run.fail("the run failed")
    events = run.events()
    found = spans(events)
    print(f"spans: {found}")
    left, right, both = found["left"], found["right"], found["both"]
    if not (left.start < right.end and right.start < left.end):
        run.fail("left and right did not overlap")
    print("ok: left and right ran at once")
    if both.start < max(left.end, right.end):
        run.fail("both started while left or right still ran")
    print("ok: and both, which holds every guest, ran alone")

    markers = [e for e in events if e["kind"] == "journal" and data(e).get("identifier") == "parallel"]
    if len(markers) != MARKERS:
        run.fail(f"expected {MARKERS} markers, found {len(markers)}: {markers}")
    if any(str(e["text"]).split("-")[0] != e.get("phase") for e in markers):
        run.fail(f"a journal marker was filed under a phase that did not write it: {markers}")
    print("ok: every journal marker names the phase that held its guest")

    said = [e for e in events if e["kind"] == "output" and " step " in str(e["text"])]
    if len(said) != PRINTS:
        run.fail(f"expected {PRINTS} printed lines, found {len(said)}")
    if any(str(e["text"]).split(" ")[0] != e.get("phase") for e in said):
        run.fail(f"a print was filed under the other phase: {said}")
    print("ok: and so does every print")


if __name__ == "__main__":
    main()
