"""`built.md`: a build report as a GitHub comment.

The change's own failures come first, each with its log tail; failures on
an input the change does not touch follow apart, as the merge base's.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from .bundle import OVER_BUDGET, RAN, UNCHANGED_INPUT_FAILED

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .build import Built, Report
    from .bundle import Bundle

COMMENT_FILE: Final = "built.md"
LIMIT: Final = 60_000
"""Characters: GitHub refuses a comment over 65536."""
LISTED: Final = 25
"""Derivations named per list; the rest are counted."""
TAILS: Final = 5
"""Failures shown with their log tail."""
AFTER: Final = "after"
URL_FILE: Final = "built.url"
LINK_LIMIT: Final = 8000
"""Characters of a graph link the comment carries. A link with the DOT in
it grows with the graph (147 KB for hdf4's); a gist's is 228."""


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 120 else f"{seconds:.0f} s"


def _fence(text: str) -> str:
    """A code fence longer than any run of backticks in `text`: a log can hold one."""
    return "`" * max([3, *(len(run) + 1 for run in re.findall("`+", text))])


def _line(r: Built) -> str:
    after = f" after {r.seconds} s" if r.seconds is not None else ""
    said = f": {r.message}" if r.message else ""
    return f"- `{r.pname}` ({r.status}{after}){said}"


def _listed(rows: Sequence[Built], *, tails: int = 0) -> list[str]:
    lines: list[str] = []
    for n, r in enumerate(rows[:LISTED]):
        lines.append(_line(r))
        if n < tails and r.log_tail:
            fence = _fence(r.log_tail)
            lines += [
                "",
                f"  <details><summary>log tail of <code>{r.drv}</code></summary>",
                "",
                f"  {fence}",
                *(f"  {ln}" for ln in r.log_tail.splitlines()),
                f"  {fence}",
                "",
                "  </details>",
            ]
    if len(rows) > LISTED:
        lines.append(f"- and {len(rows) - LISTED} more")
    return lines


def comment(made: Report, source: Bundle, view: str) -> str:
    """The report as Markdown, at most `LIMIT` characters: past it, without
    the log tails, and then cut at a line."""
    text = _comment(made, source, view, TAILS)
    if len(text) > LIMIT:
        text = _comment(made, source, view, 0)
    if len(text) <= LIMIT:
        return text
    return text[: LIMIT - 100].rsplit("\n", 1)[0] + "\n\n(cut at the comment size limit: built.json has it all)\n"


def _comment(made: Report, source: Bundle, view: str, tails: int) -> str:
    ran = [r for r in made.results if r.ok and r.status == RAN]
    own = [r for r in made.results if not r.ok and r.status not in OVER_BUDGET and r.status != UNCHANGED_INPUT_FAILED]
    base = [r for r in made.results if r.status == UNCHANGED_INPUT_FAILED]
    late = [r for r in made.results if r.status in OVER_BUDGET]
    unevaluated = source.failed.get(AFTER, {})
    timed_out = f" ({made.timed_out} timed out)" if made.timed_out else ""
    took = f": builders took {_minutes(made.built_seconds)}, estimated {_minutes(made.built_estimate)}" if ran else ""
    graph = f"[Graph]({view})" if len(view) <= LINK_LIMIT else f"The graph's link is in `{URL_FILE}`."
    lines = [
        "### pynix-review build",
        "",
        f"Changed derivations on `{source.system}`: {len(made.results)}. {graph}",
        "",
        "| result | derivations |",
        "| --- | --- |",
        f"| built | {len(ran)}{took} |",
        f"| already built or substituted | {sum(1 for r in made.results if r.ok) - len(ran)} |",
        f"| failed | {len(own)} |",
        f"| failed on an unchanged input | {len(base)} |",
        f"| out of budget | {len(late)}{timed_out} |",
        "",
        f"Wall time {_minutes(made.wall_seconds)} of a {_minutes(made.budget)} budget.",
    ]
    if unevaluated:
        lines += ["", "#### Did not evaluate", ""]
        lines += [f"- `{attr}`: {why}" for attr, why in sorted(unevaluated.items())]
    if own:
        lines += ["", "#### Failed", "", *_listed(own, tails=tails)]
    if base:
        lines += [
            "",
            "#### Failed on an unchanged input",
            "",
            "An input the change does not touch failed: the merge base is broken there, not the change.",
            "",
            *_listed(base),
        ]
    return "\n".join(lines) + "\n"
