"""The review bundle: a `Diff` as data a bot can keep, read back and draw.

`bundle.json` holds every changed derivation with both depths, its changed
inputs, its before twin and its time, so a consumer needs nothing else.
`changed.dot` is drawn from the bundle alone, never from a `Diff`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import anyio
from pydantic import BaseModel, ConfigDict

from . import graph
from .buildtimes import Source

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .buildtimes import Timing
    from .review import Diff

FORMAT: Final = 1
BUNDLE_FILE: Final = "bundle.json"
SCHEMA_FILE: Final = "bundle.schema.json"
DOT_FILE: Final = "changed.dot"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)


class Time(_Model):
    seconds: int
    source: Source
    drv: str
    """The `.drv` whose record gave the time: this one, its before twin, or a nearby build."""


class Derivation(_Model):
    drv: str
    name: str
    pname: str
    depth: int
    """Longest path: building every derivation of depth <= k builds exactly that set."""
    nearest: int
    """Distance from what the change touched; depth <= k may need more builds."""
    changed_inputs: list[str]
    twin: str | None
    time: Time | None
    attrs: list[str]
    """The attributes whose own `.drv` this is."""


class Attr(_Model):
    attr: str
    before: str | None
    after: str | None
    depth: int | None
    nearest: int | None


class TimeSummary(_Model):
    seconds: int
    by_source: dict[Source, int]
    untimed: int


class Depth(_Model):
    depth: int
    selected: int
    """Derivations at this depth or less."""
    builds: int
    """What building the selection builds: `selected` for longest path, its closure for nearest."""
    time: TimeSummary | None


class Bundle(_Model):
    format: int = FORMAT
    system: str
    before: str
    after: str
    timed: bool
    attrs: list[Attr]
    failed: dict[str, dict[str, str]]
    """Attributes that did not evaluate, per side, with the first line of the error."""
    closure: dict[str, int]
    """Derivations walked, per side."""
    derivations: list[Derivation]
    longest: list[Depth]
    nearest: list[Depth]
    seconds: dict[str, float]


def _summary(drvs: Iterable[str], timings: Mapping[str, Timing]) -> TimeSummary:
    listed = list(drvs)
    timed = [timings[d] for d in listed if d in timings]
    return TimeSummary(
        seconds=sum(t.seconds for t in timed),
        by_source={s: n for s in Source if (n := sum(1 for t in timed if t.source is s))},
        untimed=len(listed) - len(timed),
    )


def from_diff(result: Diff, *, before: str, after: str, system: str, timed: bool) -> Bundle:
    new = result.after.graph
    depth_of = {d: n for n, layer in enumerate(result.layers, 1) for d in layer}
    attrs_of: dict[str, list[str]] = {}
    for attr, base in result.after.roots.items():
        attrs_of.setdefault(base, []).append(attr)

    def time(base: str) -> Time | None:
        t = result.timings.get(base)
        return Time(seconds=t.seconds, source=t.source, drv=t.drv) if t else None

    def depths(selections: list[set[str]], builds: list[set[str]]) -> list[Depth]:
        return [
            Depth(
                depth=k,
                selected=len(sel),
                builds=len(built),
                time=_summary(built, result.timings) if timed else None,
            )
            for k, (sel, built) in enumerate(zip(selections, builds, strict=True), 1)
        ]

    cumulative: list[set[str]] = []
    for layer in result.layers:
        cumulative.append((cumulative[-1] if cumulative else set()) | layer)
    top = max(result.nearest.values(), default=0)
    near_sel = [{d for d, n in result.nearest.items() if n <= k} for k in range(1, top + 1)]

    every = {*result.before.roots, *result.after.roots, *result.before.failed, *result.after.failed}
    return Bundle(
        system=system,
        before=before,
        after=after,
        timed=timed,
        attrs=[
            Attr(
                attr=a,
                before=result.before.roots.get(a),
                after=result.after.roots.get(a),
                depth=depth_of.get(result.after.roots.get(a, "")),
                nearest=result.nearest.get(result.after.roots.get(a, "")),
            )
            for a in sorted(every)
        ],
        failed={"before": result.before.failed, "after": result.after.failed},
        closure={"before": len(result.before.graph), "after": len(new)},
        derivations=[
            Derivation(
                drv=d,
                name=new[d].name,
                pname=graph.pname(d),
                depth=depth_of[d],
                nearest=result.nearest[d],
                changed_inputs=sorted(new[d].inputs & result.changed),
                twin=result.twins.get(d),
                time=time(d),
                attrs=sorted(attrs_of.get(d, [])),
            )
            for d in sorted(result.changed, key=lambda d: (depth_of[d], d))
        ],
        longest=depths(cumulative, cumulative),
        nearest=depths(near_sel, result.nearest_builds()),
        seconds={k: round(v, 3) for k, v in result.seconds.items()},
    )


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _label(d: Derivation) -> str:
    version = d.name.removeprefix(d.pname).removeprefix("-")
    lines = [d.pname, *([version] if version else [])]
    if d.time is not None:
        logged = d.time.source in {Source.LOG, Source.LOG_NEARBY}
        nearby = " ~" if d.time.source in {Source.HYDRA_NEARBY, Source.LOG_NEARBY} else ""
        if logged and d.time.seconds == 0:
            lines.append(f"< 30 s/phase{nearby}")
        else:
            lines.append(f"{'≥ ' if logged else ''}{d.time.seconds} s{nearby}")
    return "\\n".join(line.replace("\\", "\\\\").replace('"', '\\"') for line in lines)


def to_dot(bundle: Bundle) -> str:
    """The changed subgraph: one rank per longest-path depth, inputs below.

    An edge runs from an input to what needs it. An attribute's own `.drv`
    has a double border; a derivation with no time has a dashed one. `≥`
    marks a log lower bound, and `< 30 s/phase` a log with no phase footer.
    `~` marks a time from another `.drv` of the same job."""
    out = [
        "digraph changed {",
        "  rankdir=BT;",
        '  node [shape=box, fontname="monospace"];',
    ]
    for depth in sorted({d.depth for d in bundle.derivations}):
        members = " ".join(_quote(d.drv) for d in bundle.derivations if d.depth == depth)
        out.append(f"  {{ rank=same; {members} }}")
    for d in bundle.derivations:
        attrs = [f'label="{_label(d)}"', f"tooltip={_quote(d.drv)}"]
        if d.attrs:
            attrs.append("peripheries=2")
        if d.time is None:
            attrs.append('style="dashed"')
        out.append(f"  {_quote(d.drv)} [{', '.join(attrs)}];")
    out.extend(f"  {_quote(i)} -> {_quote(d.drv)};" for d in bundle.derivations for i in d.changed_inputs)
    out.append("}")
    return "\n".join(out) + "\n"


async def write(bundle: Bundle, directory: anyio.Path) -> list[anyio.Path]:
    """Write the bundle's files into `directory`, made if missing."""
    await directory.mkdir(parents=True, exist_ok=True)
    files = {
        BUNDLE_FILE: bundle.model_dump_json(indent=2) + "\n",
        SCHEMA_FILE: json.dumps(Bundle.model_json_schema(), indent=2) + "\n",
        DOT_FILE: to_dot(bundle),
    }
    written = []
    for name, text in files.items():
        path = directory / name
        await path.write_text(text)
        written.append(path)
    return written
