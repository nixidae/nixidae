"""Plan an npm project from its `package-lock.json`: one node for each package.

The lock names every package by the path that npm installs it at, with its
URL and its hash. A package is a fixed-output fetch and a node that unpacks
it. A package with an install script runs it in that node, over a copy of
the part of the tree that the script can `require`. The root copies every
node to the path the lock gives it, so Node resolves each `require` exactly
as it would after `npm ci`.

Node resolves a module from its real path, so a link into the store would
look for its dependencies beside the store path. That is why the root
copies, and why a node holds no dependency of its own.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from pydd.core import Derivation, Fixed, Node, Planner, tool_env

#: Nix's systems as npm's `cpu` and `os` name them.
CPU = {"x86_64": "x64", "aarch64": "arm64", "i686": "ia32", "armv7l": "arm", "riscv64": "riscv64"}
OS = {"linux": "linux", "darwin": "darwin", "freebsd": "freebsd"}

#: The order of preference among the hashes of one `integrity`.
ALGORITHMS = ("sha512", "sha384", "sha256", "sha1")

LIFECYCLE = ("preinstall", "install", "postinstall")


@dataclass(frozen=True)
class Menu:
    """What `lib/node/default.nix` gives the planner."""

    name: str
    project: str
    #: The project's directory inside `project`, which may hold more.
    subdir: str
    nodejs: str
    python: str
    script: str
    cacert: str
    #: What every script runs with: stdenv's own tools, a C compiler and
    #: the Python and make that node-gyp calls.
    tools: list[str]
    #: Tools and environment of a package, by package name.
    native: dict[str, Any]
    #: npm's `--omit`: `dev`, `optional` or `peer`.
    omit: list[str]
    #: The scripts of `package.json` that the root runs, in order, in the
    #: project and in each workspace member that defines them.
    scripts: list[str]
    libc: str = "glibc"

    @property
    def directory(self) -> str:
        return posixpath.normpath(posixpath.join(self.project, self.subdir))


def name_of(path: str, entry: dict[str, Any]) -> str:
    """The name that a package installs as. An alias installs under the alias."""
    if "node_modules/" not in path:
        return entry.get("name", posixpath.basename(path))
    return path.rsplit("node_modules/", 1)[1]


def store_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9+._?=-]", "-", name.removeprefix("@"))


def strongest(integrity: str) -> str:
    """The strongest hash of an `integrity`, which may list several."""
    hashes = dict(item.split("-", 1) for item in integrity.split())
    for algorithm in ALGORITHMS:
        if algorithm in hashes:
            return f"{algorithm}-{hashes[algorithm]}"
    raise ValueError(f"no hash that Nix reads in {integrity!r}")


def fits(wanted: list[str] | None, actual: str) -> bool:
    """npm's rule for `os`, `cpu` and `libc`: a `!` excludes, and any other entry is a choice."""
    if not wanted:
        return True
    if f"!{actual}" in wanted:
        return False
    chosen = [item for item in wanted if not item.startswith("!")]
    return not chosen or actual in chosen


def bins_of(path: str, entry: dict[str, Any]) -> dict[str, str]:
    """The `bin` of a package as a map, by program name."""
    match entry.get("bin"):
        case str(file):
            return {name_of(path, entry).rsplit("/", 1)[-1]: file}
        case dict(bins):
            return bins
        case _:
            return {}


@dataclass
class Lock:
    """The `packages` of a lock file, with npm's choices for one platform."""

    packages: dict[str, dict[str, Any]]
    system: str
    libc: str = "glibc"
    omit: list[str] = field(default_factory=lambda: ["dev"])

    @classmethod
    def read(cls, data: dict[str, Any], system: str, libc: str, omit: list[str]) -> Lock:
        if data.get("lockfileVersion", 1) < 2 or "packages" not in data:
            raise SystemExit(
                "pydd.node.npm: the lock file has version 1, which names no install paths. "
                "Run `npm install --package-lock-only` with npm 7 or newer, and commit the file."
            )
        return cls(data["packages"], system, libc, omit)

    def on_platform(self, path: str) -> bool:
        entry = self.packages[path]
        arch, _, kernel = self.system.partition("-")
        return (
            fits(entry.get("cpu"), CPU.get(arch, arch))
            and fits(entry.get("os"), OS.get(kernel, kernel))
            and (kernel != "linux" or fits(entry.get("libc"), self.libc))
        )

    def present(self, path: str) -> bool:
        """Whether npm installs `path` for a build: on this platform, or a required package."""
        entry = self.packages[path]
        return self.on_platform(path) or not entry.get("optional", False)

    def shipped(self, path: str) -> bool:
        """Whether `path` stays in the result, after npm's `--omit`."""
        entry = self.packages[path]
        if not self.present(path):
            return False
        if "dev" in self.omit and entry.get("dev", False):
            return False
        if "optional" in self.omit and entry.get("optional", False):
            return False
        return not ("peer" in self.omit and entry.get("peer", False))

    def resolve(self, path: str, name: str) -> str | None:
        """The path that Node finds `name` at from the package at `path`.

        Node tries `node_modules/<name>` in the directory of the package and
        then in each parent. A link resolves to the directory it names.
        """
        parts = path.split("/") if path else []
        for depth in range(len(parts), -1, -1):
            if depth and parts[depth - 1] == "node_modules":
                continue
            directory = "/".join(parts[:depth])
            candidate = f"{directory}/node_modules/{name}" if directory else f"node_modules/{name}"
            entry = self.packages.get(candidate)
            if entry is None:
                continue
            if entry.get("link"):
                return entry["resolved"]
            return candidate if self.present(candidate) else None
        return None

    def dependencies(self, path: str) -> list[str]:
        """The paths that the package at `path` can `require` directly."""
        entry = self.packages[path]
        optional = set(entry.get("optionalDependencies", {}))
        optional |= {name for name, meta in entry.get("peerDependenciesMeta", {}).items() if meta.get("optional")}
        names = [
            *entry.get("dependencies", {}),
            *entry.get("optionalDependencies", {}),
            *entry.get("peerDependencies", {}),
        ]
        if "node_modules/" not in path:
            # The project and a workspace member build with their development
            # dependencies.
            names += entry.get("devDependencies", {})
        found = []
        for name in dict.fromkeys(names):
            resolved = self.resolve(path, name)
            if resolved is None:
                if name in optional:
                    continue
                raise SystemExit(f"pydd.node.npm: the lock gives {path or 'the project'} no {name}.")
            found.append(resolved)
        return found

    def closure(self, path: str) -> list[str]:
        """Every path that a script of the package at `path` can reach, `path` not included."""
        seen: dict[str, None] = {}
        stack = self.dependencies(path)
        while stack:
            dep = stack.pop()
            if dep in seen or dep == path:
                continue
            seen[dep] = None
            stack += self.dependencies(dep)
        return sorted(seen)

    def nested(self, path: str) -> list[str]:
        """The packages that the lock installs inside `path`, which come from their own nodes."""
        prefix = f"{path}/node_modules/" if path else "node_modules/"
        return [
            other
            for other, entry in self.packages.items()
            if other.startswith(prefix) and not entry.get("inBundle") and not entry.get("link")
        ]

    def is_package(self, path: str) -> bool:
        """An entry that npm fetches, rather than a link, a workspace member or a bundled package."""
        entry = self.packages[path]
        return "node_modules/" in path and not entry.get("link") and not entry.get("inBundle")


def bin_links(lock: Lock, paths: list[str]) -> dict[str, str]:
    """The `.bin` link of each program of each package of `paths`, relative to its target.

    npm writes a package's programs into the `node_modules/.bin` that holds it.
    """
    links = {}
    for path in sorted(paths):
        entry = lock.packages[path]
        if entry.get("link"):
            target_path, entry = entry["resolved"], lock.packages[entry["resolved"]]
        else:
            target_path = path
        modules = path.rsplit("node_modules/", 1)[0] + "node_modules"
        for program, file in bins_of(path, entry).items():
            target = posixpath.relpath(posixpath.join(target_path, file), f"{modules}/.bin")
            links[f"{modules}/.bin/{posixpath.basename(program)}"] = target
    return links


class Graph:
    """The packages of one lock file, made into nodes leaves first."""

    def __init__(self, planner: Planner, menu: Menu, lock: Lock) -> None:
        self.planner = planner
        self.menu = menu
        self.lock = lock
        self.nodes: dict[str, Node] = {}
        self.fetches: dict[str, Node] = {}
        self.planning: list[str] = []

    def derivation(
        self,
        name: str,
        spec: dict[str, Any],
        inputs: list[Node],
        package: str | None = None,
        scripted: bool = False,
    ) -> Derivation:
        menu = self.menu
        native = menu.native.get(package or "", {"tools": [], "env": {}}) if scripted else {"tools": [], "env": {}}
        tools = [*native["tools"], *(menu.tools if scripted else [])]
        env = tool_env(tools, f"{menu.nodejs}/bin") | {"PYDD_NPM": json.dumps(spec, sort_keys=True)}
        env |= native["env"]
        return Derivation(
            name=name,
            system=self.planner.system,
            builder=f"{menu.python}/bin/python3",
            args=["-I", menu.script],
            env=env,
            srcs=sorted({menu.python, menu.script, menu.nodejs, *tools, *spec.get("srcs", [])}),
            drvs={node.path: ["out"] for node in inputs},
        )

    def fetch(self, path: str) -> Node:
        entry = self.lock.packages[path]
        url, integrity = entry["resolved"], entry.get("integrity")
        if url not in self.fetches:
            if integrity is None:
                raise SystemExit(f"pydd.node.npm: {path} comes from {url}, and the lock holds no hash of it.")
            spec = {"action": "fetch", "url": url}
            drv = Derivation(
                name=f"{store_name(name_of(path, entry))}-{entry.get('version', '0')}.tgz",
                system=self.planner.system,
                builder=f"{self.menu.python}/bin/python3",
                args=["-I", self.menu.script],
                env={
                    "PYDD_NPM": json.dumps(spec, sort_keys=True),
                    "SSL_CERT_FILE": f"{self.menu.cacert}/etc/ssl/certs/ca-bundle.crt",
                },
                srcs=[self.menu.python, self.menu.script, self.menu.cacert],
                outputs={"out": Fixed(strongest(integrity))},
            )
            self.fetches[url] = self.planner.add(drv)
        return self.fetches[url]

    def tarball(self, path: str) -> tuple[str, list[Node], list[str]]:
        """Where the tarball of `path` is, the node that fetches it, and the store path that holds it."""
        resolved: str = self.lock.packages[path]["resolved"]
        if re.match(r"^https?://", resolved):
            node = self.fetch(path)
            return node.output(), [node], []
        if "://" in resolved or resolved.startswith("git+"):
            raise SystemExit(
                f"pydd.node.npm: {path} comes from {resolved}. pydd fetches packages "
                "from a registry and from the project's own files, and no other source."
            )
        local = posixpath.normpath(posixpath.join(self.menu.directory, resolved.removeprefix("file:")))
        return local, [], [self.menu.project]

    def package(self, path: str) -> Node:
        if path in self.nodes:
            return self.nodes[path]
        if path in self.planning:
            cycle = " -> ".join([*self.planning[self.planning.index(path) :], path])
            raise SystemExit(f"pydd.node.npm: install scripts depend on each other in a cycle: {cycle}")
        self.planning.append(path)
        entry = self.lock.packages[path]
        tarball, inputs, srcs = self.tarball(path)
        name = name_of(path, entry)
        scripted = bool(entry.get("hasInstallScript"))
        tree: dict[str, str] = {}
        links: dict[str, str] = {}
        if scripted:
            closure = self.lock.closure(path)
            for dep in closure:
                if not self.lock.is_package(dep):
                    raise SystemExit(
                        f"pydd.node.npm: the install script of {path} needs {dep}, which is not a package."
                    )
                node = self.package(dep)
                tree[dep] = node.output()
                inputs.append(node)
            links = bin_links(self.lock, [*closure, path])
        spec = {
            "action": "package",
            "tarball": tarball,
            "srcs": srcs,
            "path": path,
            "name": name,
            "version": entry.get("version", "0"),
            "bins": bins_of(path, entry),
            "scripts": scripted,
            "tree": tree,
            "links": links,
            "nested": [nested for nested in self.lock.nested(path) if nested in tree],
            "nodejs": self.menu.nodejs,
        }
        drv = self.derivation(f"{store_name(name)}-{entry.get('version', '0')}", spec, inputs, name, scripted)
        self.planning.pop()
        self.nodes[path] = self.planner.add(drv)
        return self.nodes[path]

    def root(self, scripts: list[str]) -> Node:
        lock = self.lock
        present = [path for path in lock.packages if path and lock.present(path)]
        tree = {path: self.package(path).output() for path in present if lock.is_package(path)}
        links = {
            path: posixpath.relpath(lock.packages[path]["resolved"], posixpath.dirname(path))
            for path in lock.packages
            if lock.packages[path].get("link")
        }
        shipped = {path for path in present if lock.shipped(path)}
        members = [path for path in lock.packages if path and "node_modules/" not in path]
        bins = {
            program: posixpath.normpath(posixpath.join(member, file))
            for member in ["", *members]
            for program, file in bins_of(member, lock.packages[member]).items()
        }
        spec = {
            "action": "root",
            "name": lock.packages[""].get("name", self.menu.name),
            "source": self.menu.directory,
            "srcs": [self.menu.project],
            "tree": tree,
            "links": links,
            "build_bins": bin_links(lock, [*tree, *links]),
            "bins": bin_links(lock, [path for path in [*tree, *links] if path in shipped or path in links]),
            "omit": sorted(path for path in tree if path not in shipped),
            "scripts": scripts,
            "workspaces": bool(members),
            "programs": bins,
            "nodejs": self.menu.nodejs,
        }
        inputs = [self.nodes[path] for path in tree]
        return self.planner.add(self.derivation(self.menu.name, spec, inputs, lock.packages[""].get("name"), True))


def main() -> int:
    planner = Planner.from_environ()
    menu = Menu(**planner.menu)
    with open(posixpath.join(menu.directory, "package-lock.json"), encoding="utf-8") as handle:
        lock = Lock.read(json.load(handle), planner.system, menu.libc, menu.omit)
    planner.submit(Graph(planner, menu, lock).root(menu.scripts))
    return 0
