"""Choose the artefacts of a lock file for one target, with ``packaging``.

Every decision here is made by the reference implementation of the
packaging PEPs, and none is written again in Nix:

- PEP 508 markers and requirements: ``packaging.markers``, ``packaging.requirements``
- PEP 440 version specifiers: ``packaging.specifiers``
- PEP 503 name normalisation: ``packaging.utils.canonicalize_name``
- PEP 425 and PEP 600 tags: ``packaging.tags``, ``packaging.utils.parse_wheel_filename``
- PEP 517 and PEP 518 build systems, read from ``pyproject.toml``
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename

#: The backend PEP 517 names for a project that declares none.
FALLBACK_BACKEND = "setuptools.build_meta:__legacy__"

#: What a node does. The installer is a wheel, and its node unpacks it,
#: because nothing can install a wheel before that node exists.
type NodeKind = Literal["unpack", "wheel", "sdist", "editable"]


@dataclass(frozen=True, slots=True)
class Artifact:
    """One row of the lock file, with the fetch Nix instantiated for it.

    ``drv`` is the fetch derivation and ``out`` its output path. The menu
    carries neither as a dependency, so a row the plan does not pick is
    never downloaded.
    """

    package: str
    version: str
    filename: str
    kind: Literal["wheel", "sdist"]
    drv: str
    out: str
    marker: str | None = None
    backend: str | None = None
    backend_requires: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> Artifact:
        return cls(
            package=row["package"],
            version=row["version"],
            filename=row["filename"],
            kind=row["kind"],
            drv=row["drv"],
            out=row["out"],
            marker=row.get("marker"),
            backend=row.get("backend"),
            backend_requires=tuple(row.get("backend-requires") or ()),
        )

    @property
    def node_name(self) -> str:
        return f"{self.package}-{self.version}"

    @property
    def is_platform_wheel(self) -> bool:
        """Whether the wheel carries a binary: its tag names a platform."""
        if self.kind != "wheel":
            return False
        _, _, _, tags = parse_wheel_filename(self.filename)
        return not any(tag.platform == "any" for tag in tags)


@dataclass(frozen=True, slots=True)
class Target:
    """What the environment runs on. A value, because the planner plans for
    a machine that need not be its own."""

    tags: frozenset[Tag]
    markers: dict[str, str]

    @classmethod
    def cpython(cls, python_version: str, platform: str, sys_platform: str) -> Target:
        return cls(
            tags=frozenset(target_tags(python_version, platform)),
            markers={
                "sys_platform": sys_platform,
                "platform_system": sys_platform.title(),
                "os_name": "posix",
                "python_version": python_version,
                "platform_machine": platform.rsplit("_", 1)[-1],
                "implementation_name": "cpython",
            },
        )

    def accepts(self, marker: str | None) -> bool:
        return marker is None or Marker(marker).evaluate(self.markers)


@dataclass(frozen=True, slots=True)
class Editable:
    """A local project, built from a store copy and read from ``root``.

    ``root`` is a path, or a reference to a variable that the environment
    expands when the interpreter starts. A variable keeps the environment the
    same bytes for every tree.
    """

    name: str
    source: str
    root: str

    def build_system(self) -> tuple[str, list[Requirement]]:
        """The PEP 517 backend of the project, and what it needs."""
        with Path(self.source, "pyproject.toml").open("rb") as handle:
            table = tomllib.load(handle).get("build-system", {})
        backend = table.get("build-backend", FALLBACK_BACKEND)
        return backend, [Requirement(text) for text in table.get("requires", [])]


@dataclass(frozen=True, slots=True)
class Node:
    """One step of the graph: build or install one artefact or project."""

    name: str
    kind: NodeKind
    artifact: Artifact | None = None
    editable: Editable | None = None
    backend: str | None = None
    backend_nodes: tuple[str, ...] = ()


@dataclass
class Plan:
    """The graph the resolver decided, before any derivation exists."""

    installer: str = ""
    nodes: dict[str, Node] = field(default_factory=dict[str, Node])
    members: list[str] = field(default_factory=list[str])
    installed: list[str] = field(default_factory=list[str])

    def add(self, node: Node) -> str:
        """Add ``node`` once, and name it."""
        self.nodes.setdefault(node.name, node)
        return node.name

    def member(self, name: str, label: str) -> None:
        """Make a node part of the environment, not only a build input."""
        self.members.append(name)
        self.installed.append(label)


def target_tags(python_version: str, platform: str) -> set[Tag]:
    """The tags a CPython of ``python_version`` on ``platform`` accepts.

    By hand, not ``packaging.tags.sys_tags``: the target is not the machine
    that runs the planner.
    """
    major, minor = python_version.split(".")
    interpreter = f"cp{major}{minor}"

    platforms = [platform]
    if platform.startswith("linux_"):
        arch = platform.removeprefix("linux_")
        # PEP 600: a manylinux tag names the oldest glibc it runs on.
        platforms += [f"manylinux_2_{glibc}_{arch}" for glibc in range(17, 40)]
        platforms += [f"manylinux2014_{arch}"]

    tags = {Tag(interpreter, interpreter, plat) for plat in platforms}
    tags |= {Tag(interpreter, "abi3", plat) for plat in platforms}
    tags |= {Tag(f"py{major}", "none", plat) for plat in [*platforms, "any"]}
    tags |= {Tag(f"py{major}{minor}", "none", "any")}
    return tags


def wheel_rank(artifact: Artifact, accepted: frozenset[Tag]) -> int | None:
    """Lower fits better; ``None`` does not fit. A binary wheel beats a pure
    one, as with pip."""
    _, _, _, tags = parse_wheel_filename(artifact.filename)
    if not any(tag in accepted for tag in tags):
        return None
    return 1 if any(tag.platform == "any" for tag in tags) else 0


def choose(
    artifacts: Iterable[Artifact], requirement: Requirement, target: Target
) -> Artifact | None:
    """The one artefact that satisfies ``requirement`` on ``target``.

    The best wheel wins. An sdist is the answer only when no wheel fits.
    """
    package = canonicalize_name(requirement.name)
    best: tuple[int, Artifact] | None = None
    sdist: Artifact | None = None

    for artifact in artifacts:
        if canonicalize_name(artifact.package) != package:
            continue
        if artifact.version not in requirement.specifier:
            log(f"{artifact.filename}: {artifact.version} is outside {requirement}")
            continue
        if not target.accepts(artifact.marker):
            log(f"{artifact.filename}: marker excludes this target")
            continue
        if artifact.kind == "sdist":
            sdist = artifact
            continue
        rank = wheel_rank(artifact, target.tags)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, artifact)

    return best[1] if best is not None else sdist


def resolve(
    artifacts: list[Artifact],
    roots: list[str],
    installer: str,
    editables: list[Editable],
    target: Target,
) -> Plan:
    """Turn a lock file into the graph that builds the environment."""
    plan = Plan()

    bootstrap = choose(artifacts, Requirement(installer), target)
    if bootstrap is None:
        raise RuntimeError(f"the installer '{installer}' is not in the lock file")
    plan.installer = plan.add(Node(bootstrap.node_name, "unpack", artifact=bootstrap))
    log(f"installer {bootstrap.filename} (unpacked to bootstrap)")

    def backend_nodes(requirements: Iterable[Requirement], subject: str) -> tuple[str, ...]:
        """A node for each backend requirement. The backend comes from the
        same lock file as what it builds, so neither node exists before the
        plan runs: this is what one derivation cannot express."""
        names: list[str] = []
        for requirement in requirements:
            dependency = choose(artifacts, requirement, target)
            if dependency is None:
                raise RuntimeError(f"{subject}: nothing in the lock file satisfies {requirement}")
            names.append(plan.add(_artifact_node(dependency)))
            log(f"  backend {dependency.filename} for {requirement}")
        return tuple(names)

    for package in roots:
        chosen = choose(artifacts, Requirement(package), target)
        if chosen is None:
            log(f"{package}: no artefact fits this target")
            continue
        node = _artifact_node(chosen)
        if chosen.kind == "sdist":
            if chosen.backend is None:
                raise RuntimeError(
                    f"{chosen.filename}: an sdist needs a 'backend' in the lock file"
                )
            requires = [Requirement(text) for text in chosen.backend_requires]
            node = Node(
                node.name,
                "sdist",
                artifact=chosen,
                backend=chosen.backend,
                backend_nodes=backend_nodes(requires, chosen.filename),
            )
        plan.member(plan.add(node), f"{chosen.package}=={chosen.version}")
        log(f"{package}: {chosen.filename}")

    for editable in editables:
        backend, requires = editable.build_system()
        node = Node(
            editable.name,
            "editable",
            editable=editable,
            backend=backend,
            backend_nodes=backend_nodes(requires, editable.name),
        )
        plan.member(plan.add(node), f"{editable.name} (editable, from {editable.root})")
        log(f"{editable.name}: editable through {backend}, reading {editable.root}")

    return plan


def _artifact_node(artifact: Artifact) -> Node:
    return Node(artifact.node_name, artifact.kind, artifact=artifact)


def log(message: str) -> None:
    print(f"plan: {message}", file=sys.stderr)
