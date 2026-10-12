"""The resolver against the uv.lock of the example, with no store involved."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement

from pydd.python import Locked, Target, read, resolve
from pydd.python._resolve import choose
from pydd.python._uvlock import filename

PROJECT = Path(__file__).parent.parent / "examples/venv/fixtures/myapp"
PACKAGES: list[dict[str, Any]] = tomllib.loads((PROJECT / "uv.lock").read_text())["package"]
TARGET = Target.cpython("3.14.7", "linux_x86_64", "linux")
IDNA = {"backend": "flit_core.buildapi", "requires": ["flit_core >=3.11,<5"]}


def fetched() -> dict[str, dict[str, str]]:
    urls = [
        file["url"]
        for package in PACKAGES
        for file in [package.get("sdist", {}), *package.get("wheels", [])]
        if "url" in file
    ]
    return {url: {"drv": f"/nix/store/{filename(url)}.drv", "out": "/x"} for url in urls}


def locked(**options: Any) -> Locked:
    return read(PACKAGES, fetched(), TARGET, source="/src", **options)


def test_a_binary_wheel_for_the_target_beats_the_pure_one() -> None:
    chosen = choose(locked().artifacts, Requirement("charset-normalizer"), TARGET)
    assert chosen is not None
    assert "manylinux2014_x86_64" in chosen.filename


def test_an_edge_marker_excludes_a_package() -> None:
    assert not any(root.startswith("colorama") for root in locked().roots)


def test_an_extra_on_an_edge_is_followed() -> None:
    assert any(root.startswith("pysocks==") for root in locked().roots)


def test_a_dependency_group_is_installed_only_when_asked() -> None:
    assert not any(root.startswith("installer") for root in locked().roots)
    assert any(root.startswith("installer==") for root in locked(groups=["build"]).roots)


def test_a_marker_reads_the_target_not_the_planner() -> None:
    older = Target.cpython("3.11.2", "linux_x86_64", "linux")
    assert older.accepts("python_full_version < '3.12'")
    assert not TARGET.accepts("python_full_version < '3.12'")
    assert older.accepts("platform_python_implementation == 'CPython'")


def test_a_name_is_normalised_before_it_is_compared() -> None:
    chosen = choose(locked().artifacts, Requirement("flit_core>=3.11,<5"), TARGET)
    assert chosen is not None
    assert chosen.package == "flit-core"


def test_the_member_is_an_editable_read_from_its_root() -> None:
    (myapp,) = locked(editable_roots={"myapp": "$ROOT"}).editables
    assert (myapp.name, myapp.source, myapp.root) == ("myapp", "/src/.", "$ROOT")


def test_an_sdist_gets_its_backend_as_a_node() -> None:
    lock = locked(no_binary=["idna"], build_systems={"idna": IDNA})
    plan = resolve(lock.artifacts, lock.roots, "installer", [], TARGET)
    (idna,) = [node for node in plan.nodes.values() if node.name.startswith("idna-")]
    assert idna.kind == "sdist"
    assert [name.split("-4")[0] for name in idna.backend_nodes] == ["flit-core"]
    assert not any(member.startswith("flit-core") for member in plan.members)
