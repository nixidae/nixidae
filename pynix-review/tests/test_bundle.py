from __future__ import annotations

import base64
import json

import anyio
from shapes import base, font_chain

from pynix_review import graph
from pynix_review.buildtimes import Source, Timing
from pynix_review.bundle import (
    BUNDLE_FILE,
    DOT_FILE,
    SCHEMA_FILE,
    URL_FILE,
    Bundle,
    from_diff,
    to_dot,
    view_url,
    write,
)
from pynix_review.evaluate import Side
from pynix_review.review import Diff

CAIRO = base("cairo-1.18", "b")
LIBPNG = base("libpng-1.6", "b")
FONTCONFIG = base("fontconfig-2.18", "b")
FREETYPE = base("freetype-2.14", "b")


def font_diff() -> Diff:
    """A libpng change: all four of the font chain rebuild."""
    old, new = font_chain("a"), font_chain("b")
    before = Side(graph=old, roots={"cairo": base("cairo-1.18", "a")})
    after = Side(graph=new, roots={"cairo": CAIRO, "libpng": LIBPNG})
    changed = graph.changed(old, new)
    return Diff(
        before=before,
        after=after,
        changed=changed,
        layers=graph.layers(new, changed),
        nearest=graph.nearest(new, changed),
        twins={CAIRO: base("cairo-1.18", "a")},
        timings={
            CAIRO: Timing(55, Source.HYDRA, CAIRO, 55),
            LIBPNG: Timing(33, Source.LOG, base("libpng-1.6", "a"), 38),
            FREETYPE: Timing(0, Source.LOG_NEARBY, base("freetype-2.13", "c"), 7),
        },
    )


def font_bundle() -> Bundle:
    return from_diff(font_diff(), before="old", after="new", system="x86_64-linux", timed=True)


def test_derivations_carry_both_depths() -> None:
    by_drv = {d.drv: d for d in font_bundle().derivations}
    assert (by_drv[CAIRO].depth, by_drv[CAIRO].nearest) == (4, 2)
    assert by_drv[CAIRO].changed_inputs == sorted([LIBPNG, FONTCONFIG])
    assert by_drv[CAIRO].attrs == ["cairo"]
    assert by_drv[CAIRO].twin == base("cairo-1.18", "a")
    assert by_drv[FONTCONFIG].time is None


def test_nearest_depth_two_selects_three_and_builds_four() -> None:
    depth2 = font_bundle().nearest[1]
    assert (depth2.depth, depth2.selected, depth2.builds) == (2, 3, 4)


def test_longest_depth_builds_what_it_selects() -> None:
    for depth in font_bundle().longest:
        assert depth.selected == depth.builds


def test_time_summary_counts_sources() -> None:
    whole = font_bundle().longest[-1].time
    assert whole is not None
    assert whole.seconds == 88
    assert whole.estimate == 55 + 38 + 7
    assert whole.by_source == {Source.HYDRA: 1, Source.LOG: 1, Source.LOG_NEARBY: 1}
    assert whole.untimed == 1


def test_bundle_round_trips() -> None:
    made = font_bundle()
    assert Bundle.model_validate_json(made.model_dump_json()) == made


def test_dot_ranks_by_depth_and_points_inputs_up() -> None:
    dot = to_dot(font_bundle())
    assert dot.startswith("digraph changed {")
    assert f'{{ rank=same; "{CAIRO}" }}' in dot
    assert f'"{LIBPNG}" -> "{CAIRO}";' in dot
    assert '"cairo\\n1.18\\n55 s"' in dot
    assert '"libpng\\n1.6\\n≈ 38 s"' in dot
    assert '"freetype\\n2.14\\n≈ 7 s ~"' in dot
    fontconfig_line = next(line for line in dot.splitlines() if line.startswith(f'  "{FONTCONFIG}" ['))
    assert 'style="dashed"' in fontconfig_line


async def test_write_makes_four_files(tmp_path: anyio.Path) -> None:
    written = await write(font_bundle(), anyio.Path(tmp_path) / "out")
    assert sorted(p.name for p in written) == sorted([BUNDLE_FILE, SCHEMA_FILE, DOT_FILE, URL_FILE])
    schema = json.loads(await (anyio.Path(tmp_path) / "out" / SCHEMA_FILE).read_text())
    assert schema["title"] == "Bundle"


def test_view_url_carries_the_dot_as_base64_utf8() -> None:
    dot = to_dot(font_bundle())
    url = view_url(dot)
    assert url.startswith("https://graphvizonline.net/?engine=dot#")
    assert base64.b64decode(url.split("#", 1)[1]).decode() == dot
