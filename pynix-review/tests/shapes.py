"""Hand-built derivation graphs, from shapes measured on nixpkgs."""

from __future__ import annotations

from pynix_review.graph import Drv, Graph


def base(name: str, tag: str = "a") -> str:
    return f"{tag * 32}-{name}.drv"


def drv(name: str, *inputs: str, fixed: tuple[str, ...] | None = None, tag: str = "a") -> Drv:
    return Drv(base=base(name, tag), name=name, inputs=frozenset(inputs), fixed=fixed)


def graph(*drvs: Drv) -> Graph:
    return {d.base: d for d in drvs}


def font_chain(tag: str = "a") -> Graph:
    """libpng -> freetype -> fontconfig -> cairo, and cairo uses libpng directly.

    The shape that sets cairo at depth 4 by longest path and 2 by nearest."""
    libpng = drv("libpng-1.6", tag=tag)
    freetype = drv("freetype-2.14", libpng.base, tag=tag)
    fontconfig = drv("fontconfig-2.18", freetype.base, tag=tag)
    cairo = drv("cairo-1.18", libpng.base, fontconfig.base, tag=tag)
    return graph(libpng, freetype, fontconfig, cairo)
