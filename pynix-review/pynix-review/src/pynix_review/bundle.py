"""The review bundle: a `Diff` as data a bot can keep, read back and draw.

`bundle.json` holds every changed derivation with both depths, its changed
inputs, its before twin and its time, so a consumer needs nothing else.
`changed.dot` is drawn from the bundle alone, never from a `Diff`.
"""

from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING, Final
from urllib.parse import quote

import anyio
from pydantic import BaseModel, ConfigDict

from . import graph
from .buildtimes import Source

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .build import Built, Report
    from .buildtimes import Timing
    from .review import Diff

FORMAT: Final = 1
BUNDLE_FILE: Final = "bundle.json"
SCHEMA_FILE: Final = "bundle.schema.json"
DOT_FILE: Final = "changed.dot"
URL_FILE: Final = "changed.url"
VIEWER: Final = "https://dreampuf.github.io/GraphvizOnline/?engine=dot#"
"""The author's own copy: no ads, and no third-party script but lz-string
from cdnjs. graphvizonline.net carries ads and reads base64 instead."""
FILL_BUILT: Final = "palegreen"
FILL_CACHED: Final = "lightgrey"
"""Substituted or already valid: no builder ran."""
FILL_FAILED: Final = "salmon"
FILL_OUT_OF_BUDGET: Final = "gold"
DEPENDENCY_FAILED: Final = "dependency-failed"
OUT_OF_BUDGET: Final = "out-of-budget"
ROW_WIDTH: Final = 12
"""Nodes per row. The fzf PR puts 51 derivations at depth 4, and one row of
them is a strip no browser shows readably; a wider depth stacks its rows."""
FRAGMENT_SAFE: Final = "!$&'()*+,;=:@/?"
"""RFC 3986 fragment characters `quote` escapes by default."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)


class Time(_Model):
    seconds: int
    """Exact from Hydra; from a log, the sum of its phase footers, a lower bound."""
    estimate: int
    """The best guess: `seconds` from Hydra; from a log, the footers plus a second per phase without one."""
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
    estimate: int
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
        estimate=sum(t.estimate for t in timed),
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
        return Time(seconds=t.seconds, estimate=t.estimate, source=t.source, drv=t.drv) if t else None

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


def _label(d: Derivation, prefix: str = "") -> str:
    version = d.name.removeprefix(d.pname).removeprefix("-")
    lines = [d.pname, *([version] if version else [])]
    if d.time is not None:
        logged = d.time.source in {Source.LOG, Source.LOG_NEARBY}
        nearby = " ~" if d.time.source in {Source.HYDRA_NEARBY, Source.LOG_NEARBY} else ""
        lines.append(f"{prefix}{'≈ ' if logged else ''}{d.time.estimate} s{nearby}")
    return "\\n".join(_escape(line) for line in lines)


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace('"', '\\"')


def _filled(fill: str) -> list[str]:
    return ['style="filled"', f'fillcolor="{fill}"']


def _outcome(r: Built | None) -> tuple[list[str], list[str]]:
    """Extra label lines and node attributes for a build result."""
    if r is None:
        return [], ['style="dotted"']
    if r.ok and r.seconds is not None:
        return [f"took {r.seconds} s"], _filled(FILL_BUILT)
    if r.ok:
        return [r.status], _filled(FILL_CACHED)
    if r.status == OUT_OF_BUDGET:
        return ["out of budget"], _filled(FILL_OUT_OF_BUDGET)
    after = f" after {r.seconds} s" if r.seconds is not None else ""
    return [f"{r.status}{after}"], _filled(FILL_FAILED)


def _built_line(made: Report, total: int) -> str:
    return (
        f"built {made.built} of {total}: took {made.built_seconds} s, est {made.built_estimate} s"
        f" | failed {made.failed} | out of budget {made.out_of_budget}"
        f" | wall {made.wall_seconds:.0f} s of {made.budget} s"
    )


def to_dot(bundle: Bundle, built: Report | None = None) -> str:
    """The changed subgraph: rows of `ROW_WIDTH` per longest-path depth, inputs below.

    An edge runs from an input to what needs it. An attribute's own `.drv`
    has a double border; a derivation with no time has a dashed one. `≈`
    marks an estimate from a log, `~` a time from another `.drv` of the same
    job. With `built`, the fill shows what the build did: green built, grey
    cached, red failed, yellow out of budget. A dotted border marks a
    derivation the build has no result for."""
    results = {r.drv: r for r in built.results} if built is not None else None
    out = [
        f"digraph {'changed' if built is None else 'built'} {{",
        "  rankdir=BT;",
        '  node [shape=box, fontname="monospace"];',
    ]
    if built is not None:
        out.append(
            f'  labelloc="t"; fontname="monospace"; label={_quote(_built_line(built, len(bundle.derivations)))};'
        )
    rows: list[list[str]] = []
    for depth in sorted({d.depth for d in bundle.derivations}):
        members = [d.drv for d in bundle.derivations if d.depth == depth]
        rows.extend(members[i : i + ROW_WIDTH] for i in range(0, len(members), ROW_WIDTH))
    out.extend(f"  {{ rank=same; {' '.join(_quote(m) for m in row)} }}" for row in rows)
    # Every row above the one before, across depths too, column by column:
    # with only the changed edges dot puts depth 5's first row beside depth
    # 4's second, and with only the first column it shifts each row aside
    # (fzf: 5943 pt wide at 12 a row).
    out.extend(
        f"  {_quote(x)} -> {_quote(y)} [style=invis];"
        for a, b in itertools.pairwise(rows)
        for x, y in zip(a, b, strict=False)
    )
    for d in bundle.derivations:
        extra, styled = _outcome(results.get(d.drv)) if results is not None else ([], [])
        label = "\\n".join([_label(d, "est " if results is not None else ""), *(_escape(line) for line in extra)])
        attrs = [f'label="{label}"', f"tooltip={_quote(d.drv)}"]
        if d.attrs:
            attrs.append("peripheries=2")
        if styled:
            attrs.extend(styled)
        elif d.time is None:
            attrs.append('style="dashed"')
        out.append(f"  {_quote(d.drv)} [{', '.join(attrs)}];")
    out.extend(f"  {_quote(i)} -> {_quote(d.drv)};" for d in bundle.derivations for i in d.changed_inputs)
    out.append("}")
    return "\n".join(out) + "\n"


def view_url(dot: str) -> str:
    """A GraphvizOnline link that renders `dot`.

    The page reads the fragment with `decodeURIComponent` and renders it in
    the browser. A browser never sends the fragment, so the graph stays
    with whoever opens the link. Not `?compressed=` (lz-string, shorter):
    the query reaches GitHub Pages' logs."""
    return VIEWER + quote(dot, safe=FRAGMENT_SAFE)


async def write(bundle: Bundle, directory: anyio.Path) -> list[anyio.Path]:
    """Write the bundle's files into `directory`, made if missing."""
    await directory.mkdir(parents=True, exist_ok=True)
    dot = to_dot(bundle)
    files = {
        BUNDLE_FILE: bundle.model_dump_json(indent=2) + "\n",
        SCHEMA_FILE: json.dumps(Bundle.model_json_schema(), indent=2) + "\n",
        DOT_FILE: dot,
        URL_FILE: view_url(dot) + "\n",
    }
    written = []
    for name, text in files.items():
        path = directory / name
        await path.write_text(text)
        written.append(path)
    return written
