from __future__ import annotations

import pytest
from shapes import base, drv, font_chain, graph

from pynix_review.graph import build_set, changed, identity, layers, nearest, pname, twins

H = "0" * 32


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("fzf-0.75.0", "fzf"),
        ("fzf-0.75.0-go-modules", "fzf"),
        ("source", "source"),
        ("python3.14-spyder-6.1.6", "python3.14-spyder"),
        ("absolute_gir_path.patch", "absolute_gir_path.patch"),
    ],
)
def test_pname(name: str, expected: str) -> None:
    assert pname(f"{H}-{name}.drv") == expected


def test_fixed_output_matches_by_content_address() -> None:
    old = drv("cliphist-0.7.0-go-modules", fixed=("nar:sha256:x",), tag="a")
    new = drv("cliphist-0.7.0-go-modules", fixed=("nar:sha256:x",), tag="b")
    assert old.base != new.base
    assert identity(old) == identity(new)
    assert changed(graph(old), graph(new)) == set()


def test_fixed_output_with_new_hash_changes() -> None:
    old = drv("source", fixed=("nar:sha256:x",), tag="a")
    new = drv("source", fixed=("nar:sha256:y",), tag="b")
    assert changed(graph(old), graph(new)) == {new.base}


def test_input_addressed_matches_by_drv() -> None:
    old = drv("hello-2.12", tag="a")
    same = drv("hello-2.12", tag="a")
    new = drv("hello-2.12", tag="b")
    assert changed(graph(old), graph(same)) == set()
    assert changed(graph(old), graph(new)) == {new.base}


def test_longest_path_and_nearest_disagree() -> None:
    g = font_chain()
    every = set(g)
    depth = {d: n for n, layer in enumerate(layers(g, every), 1) for d in layer}
    assert depth[base("cairo-1.18")] == 4
    assert nearest(g, every)[base("cairo-1.18")] == 2


def test_longest_path_depth_builds_what_it_selects() -> None:
    g = font_chain()
    every = set(g)
    selected: set[str] = set()
    for layer in layers(g, every):
        selected |= layer
        assert build_set(g, every, selected) == selected


def test_nearest_depth_pulls_in_the_chain() -> None:
    g = font_chain()
    every = set(g)
    near = nearest(g, every)
    selected = {d for d, n in near.items() if n <= 2}
    assert selected == {base("libpng-1.6"), base("freetype-2.14"), base("cairo-1.18")}
    assert build_set(g, every, selected) == every


def test_layers_ignore_unchanged_inputs() -> None:
    stdenv = drv("stdenv-linux")
    lib = drv("lib-1", stdenv.base)
    app = drv("app-1", lib.base, stdenv.base)
    g = graph(stdenv, lib, app)
    assert layers(g, {lib.base, app.base}) == [{lib.base}, {app.base}]


def test_layers_refuse_a_cycle() -> None:
    a = drv("a-1", base("b-1"))
    b = drv("b-1", a.base)
    with pytest.raises(ValueError, match="cycle"):
        layers(graph(a, b), {a.base, b.base})


def test_twins_follow_inputs_by_pname() -> None:
    old_src = drv("source", fixed=("nar:sha256:x",), tag="a")
    old_mods = drv("fzf-0.74.5-go-modules", old_src.base, tag="a")
    old_fzf = drv("fzf-0.74.5", old_src.base, old_mods.base, tag="a")
    new_src = drv("source", fixed=("nar:sha256:y",), tag="b")
    new_mods = drv("fzf-0.75.0-go-modules", new_src.base, tag="b")
    new_fzf = drv("fzf-0.75.0", new_src.base, new_mods.base, tag="b")
    before = graph(old_src, old_mods, old_fzf)
    after = graph(new_src, new_mods, new_fzf)
    found = twins(before, after, [(new_fzf.base, old_fzf.base)], changed(before, after))
    assert found == {
        new_fzf.base: old_fzf.base,
        new_mods.base: old_mods.base,
        new_src.base: old_src.base,
    }


def test_twins_skip_an_ambiguous_name() -> None:
    old_a = drv("source", fixed=("nar:sha256:1",), tag="a")
    old_b = drv("source", fixed=("nar:sha256:2",), tag="c")
    old = drv("pkg-1", old_a.base, old_b.base, tag="a")
    new_src = drv("source", fixed=("nar:sha256:3",), tag="b")
    new = drv("pkg-2", new_src.base, tag="b")
    before = graph(old_a, old_b, old)
    after = graph(new_src, new)
    assert twins(before, after, [(new.base, old.base)], changed(before, after)) == {new.base: old.base}
