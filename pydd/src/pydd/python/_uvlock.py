"""Read a ``uv.lock``: walk its dependency edges for one target.

uv locks the dependencies of a project, with a marker on each edge. It
locks neither the PEP 517 backend of an sdist nor the installer. Both come
from the same lock, where a dependency group puts them, and an sdist names
its backend through ``build_systems``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from packaging.utils import canonicalize_name

from ._resolve import Artifact, Editable, Target, log

type Package = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Locked:
    """What ``resolve`` takes, read from a ``uv.lock``."""

    artifacts: list[Artifact]
    roots: list[str]
    editables: list[Editable]


def filename(url: str) -> str:
    return unquote(PurePosixPath(urlparse(url).path).name)


def is_member(package: Package) -> bool:
    """A workspace member: the project itself, not a dependency of it."""
    return "editable" in package["source"] or "virtual" in package["source"]


def read(
    packages: list[Package],
    fetched: Mapping[str, Mapping[str, str]],
    target: Target,
    *,
    source: str,
    groups: Iterable[str] = (),
    extras: Iterable[str] = (),
    build_systems: Mapping[str, Mapping[str, Any]] | None = None,
    no_binary: Iterable[str] = (),
    editable_roots: Mapping[str, str] | None = None,
) -> Locked:
    """The artefacts of every package, and the packages ``target`` needs.

    Every package becomes an artefact, so a backend or the installer can be
    chosen from the whole lock. Only the packages the edges reach from the
    workspace members become roots.
    """
    build_systems = {canonicalize_name(k): v for k, v in (build_systems or {}).items()}
    no_binary = {canonicalize_name(name) for name in no_binary}
    editable_roots = editable_roots or {}

    by_name: dict[str, list[Package]] = {}
    for package in packages:
        by_name.setdefault(canonicalize_name(package["name"]), []).append(package)

    def lookup(edge: Mapping[str, Any], subject: str) -> Package:
        candidates = by_name.get(canonicalize_name(edge["name"]), [])
        if "version" in edge:
            candidates = [p for p in candidates if p.get("version") == edge["version"]]
        if len(candidates) > 1:
            # A fork: uv locked one version per set of markers.
            candidates = [
                p
                for p in candidates
                if any(target.accepts(m) for m in p.get("resolution-markers", [None]))
            ]
        if len(candidates) != 1:
            raise RuntimeError(f"{subject}: uv.lock has no single '{edge['name']}' for this target")
        return candidates[0]

    reached: dict[tuple[str, str], set[str]] = {}
    pending: list[tuple[Package, set[str], list[Mapping[str, Any]]]] = []

    def visit(package: Package, wanted: set[str], also: Iterable[Mapping[str, Any]] = ()) -> None:
        key = (canonicalize_name(package["name"]), str(package.get("version")))
        seen = reached.get(key)
        if seen is not None and wanted <= seen:
            return
        reached[key] = (seen or set()) | wanted
        pending.append((package, wanted, list(also)))

    members = [package for package in packages if is_member(package)]
    for member in members:
        dev = member.get("dev-dependencies", {})
        visit(member, set(extras), [edge for group in groups for edge in dev.get(group, [])])

    while pending:
        package, wanted, also = pending.pop()
        optional = package.get("optional-dependencies", {})
        edges = [
            *package.get("dependencies", []),
            *(edge for extra in sorted(wanted) for edge in optional.get(extra, [])),
            *also,
        ]
        for edge in edges:
            if not target.accepts(edge.get("marker")):
                log(f"{package['name']} -> {edge['name']}: marker excludes this target")
                continue
            visit(lookup(edge, package["name"]), set(edge.get("extra", [])))

    roots: list[str] = []
    for name, version in sorted(reached):
        package = next(p for p in by_name[name] if str(p.get("version")) == version)
        if not is_member(package):
            roots.append(f"{name}=={version}")

    editables = [
        Editable(
            member["name"],
            f"{source}/{member['source']['editable']}",
            editable_roots.get(member["name"], f"{source}/{member['source']['editable']}"),
        )
        for member in members
        if "editable" in member["source"]
    ]

    artifacts = [
        artifact
        for package in packages
        if not is_member(package)
        for artifact in _artifacts(package, fetched, build_systems, no_binary)
    ]
    return Locked(artifacts, roots, editables)


def _artifacts(
    package: Package,
    fetched: Mapping[str, Mapping[str, str]],
    build_systems: Mapping[str, Mapping[str, Any]],
    no_binary: set[str],
) -> Iterable[Artifact]:
    name = canonicalize_name(package["name"])
    if "registry" not in package["source"]:
        raise RuntimeError(f"{name}: pydd reads registry packages only, not {package['source']}")

    def artifact(url: str, kind: Literal["wheel", "sdist"], **extra: Any) -> Artifact:
        return Artifact(
            package=name,
            version=str(package["version"]),
            filename=filename(url),
            kind=kind,
            drv=fetched[url]["drv"],
            out=fetched[url]["out"],
            **extra,
        )

    if name not in no_binary:
        for wheel in package.get("wheels", []):
            yield artifact(wheel["url"], "wheel")
    if "url" in package.get("sdist", {}):
        system = build_systems.get(name, {})
        yield artifact(
            package["sdist"]["url"],
            "sdist",
            backend=system.get("backend"),
            backend_requires=tuple(system.get("requires", ())),
        )
