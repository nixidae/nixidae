"""The plan document, and how a planner submits one.

**A plan document is the interface between the two halves of this example.**
A planner decides what the graph is and writes the document. `graph.nix` says
how a node of each kind is built, and it reads no lock file. So a reader of a
different lock format is a new producer of this document, and `graph.nix` does
not change when one arrives.

The document names one node as its `root`. The outer planner roots its
document at the virtual environment. The planner of a source distribution
roots its document at the build of that distribution.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from packaging.utils import canonicalize_name

from ..core._writer import CliWriter, HuggormWriter, OutWriter, Writer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._select import Artifact


def nix_string(text: str) -> str:
    """`text` as a Nix string literal.

    A Nix string takes the escapes of C for the backslash and the quote, and
    it also takes `${` as the start of an interpolation. The JSON below holds
    store paths and file names, so `${` cannot appear, but an escape that
    depends on the input is not an escape.
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


@dataclass(frozen=True, slots=True)
class LocalNode:
    """What a node that builds one tree of the user needs, whichever form.

    **The three forms differ in one hook, and in nothing else.** `build_editable`,
    `build_sdist` and `build_wheel` all run in the same directory, with the same
    backend, on the same path. So the three methods below take this value, and
    only `editable_node` takes anything more.

    `subdirectory` names the project inside `source`. `uv.lock` writes it for a
    git source and for a URL source that hold the project below their root.
    """

    name: str
    source: str
    backend: str
    backend_path: list[str]
    subdirectory: str = ""

    def entry(self, kind: str, suffix: str) -> dict[str, Any]:
        """The node, under the name that `kind` gives it.

        **The name carries a suffix, because the project may also name the
        environment.** `uv init --package` makes a project that is both a member
        of its own environment and the name of it, and two nodes may not share a
        name.
        """
        return {
            "name": f"{self.name}-{suffix}",
            "kind": kind,
            "platform": False,
            "source": self.source,
            "backend": self.backend,
            "backendPath": self.backend_path,
            "subdirectory": self.subdirectory,
        }


def build_tools_from(inputs: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """The store paths that each package needs to build, by PEP 503 name.

    `default.nix` takes the list at its `buildTools` argument, and it writes
    the store path of each entry. **The name is canonicalised here, and not in
    `graph.nix`.** A distribution name has more than one spelling, and
    `packaging` holds the rule that makes two spellings one name.
    """
    given: dict[str, list[dict[str, str]]] = inputs.get("buildTools", {})
    return {canonicalize_name(name): list(roots) for name, roots in given.items()}


@dataclass
class Plan:
    """The graph that a planner decided, in the shape `graph.nix` takes."""

    # A subscripted alias is callable and gives an empty container, and it also
    # tells the type checker what that container holds. A bare `list` here
    # leaves the element type unknown.
    nodes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    members: list[str] = field(default_factory=list[str])
    installed: list[str] = field(default_factory=list[str])
    #: The node of each member, by PEP 503 name. `application_node` reads
    #: this to find the one member that the caller asked for.
    by_package: dict[str, str] = field(default_factory=dict[str, str])
    #: The name of the node that every install step reads.
    installer: str = ""
    #: The node of each local project, by `<project>:<form>`, where the form is
    #: `editable`, `wheel`, `sdist` or `from-sdist`.
    local_nodes: dict[str, str] = field(default_factory=dict[str, str])
    #: The store paths that each package needs to build, by PEP 503 name.
    #: `build_tools_from` reads the value out of the inputs of the planner.
    build_tools: dict[str, list[dict[str, str]]] = field(default_factory=dict[str, list[dict[str, str]]])
    _seen: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

    def tools_for(self, package: str) -> dict[str, Any]:
        """The `buildTools` key of the node that builds `package`, or nothing.

        **A package with no entry gets no key at all.** A node is the name of a
        store path, so an empty key would change the output path of every node
        of every graph that this repository has already built.
        """
        roots = self.build_tools.get(canonicalize_name(package))
        return {"buildTools": roots} if roots else {}

    def add(self, entry: dict[str, Any]) -> str:
        """Add one node, and return its name.

        **A repeated node adds nothing, and a repeated *name* is an error.**
        The same wheel answers more than one requirement, and the second call
        must reuse the first node rather than make a second one. Two different
        nodes under one name are a different thing: `graph.nix` keys a node by
        its name, so the second would replace the first and the graph would
        build something that no planner asked for.
        """
        name: str = entry["name"]
        existing = self._seen.get(name)
        if existing is not None:
            if existing != entry:
                raise ValueError(
                    f"two different nodes are both called '{name}': a node of kind "
                    f"'{existing['kind']}' and a node of kind '{entry['kind']}'. "
                    "A node name is the name of a store path, so it has to be unique."
                )
            return name
        self._seen[name] = entry
        self.nodes.append(entry)
        return name

    def artifact_node(
        self,
        artifact: Artifact,
        kind: str,
        *,
        backend: str = "",
        backend_path: list[str] | None = None,
    ) -> str:
        """Add the node that builds one artefact of the lock file.

        `kind` is what the node *does*, and it is not always the kind of the
        artefact. The installer is a wheel, and its node unpacks it rather
        than installs it, because nothing can install a wheel before that node
        exists.

        **That bootstrap node carries a name of its own.** The same installer
        is a member of the environment when a group or an extra asks for it,
        and the node that installs it is a different node with a different
        output. One name for both makes `add` refuse the graph, so the
        bootstrap takes the `-bootstrap` suffix.

        A node of kind `sdist-build` runs one PEP 517 hook, so it takes the
        backend and the path that the backend lives on. Both belong in this
        call, and not in an assignment to the node afterwards: `add` returns
        the existing name when a node repeats, and a later assignment would
        then reach the wrong node.
        """
        entry: dict[str, Any] = {
            "name": f"{artifact.node_name}-bootstrap" if kind == "unpack" else artifact.node_name,
            "kind": kind,
            "artifact": artifact.as_plan(),
            # `graph.nix` gives a node the loader and the libraries of this
            # system only when the tag of the wheel says that the node needs
            # them. The tag is what `packaging` already parsed.
            "platform": artifact.is_platform_wheel,
        }
        if kind == "sdist-build":
            if not backend:
                raise ValueError(f"{artifact.filename}: a node of kind '{kind}' needs a backend")
            entry["backend"] = backend
            entry["backendPath"] = backend_path or []
            # **Only a node that runs a build gets a tool.** A wheel is
            # already built, so the node that installs one needs no compiler
            # and takes no dependency on one.
            entry |= self.tools_for(artifact.package)
        return self.add(entry)

    def editable_node(self, node: LocalNode, *, root: str) -> str:
        """Add the node that builds one local project as an editable.

        `root` is what the PEP 660 redirect names, and it is the one thing that
        the other two forms do not have. Both of those hold the code, so
        nothing redirects to a tree of the user.
        """
        return self.add(node.entry("editable", "editable") | {"root": root} | self.tools_for(node.name))

    def local_wheel_node(self, node: LocalNode) -> str:
        """Add the node that builds a wheel out of a tree, and installs it.

        **This is what `pip install .` does, and what a git source needs.** A
        git checkout is a directory, so no archive stands between the tree and
        the wheel: one PEP 517 `build_wheel` call answers it. A `directory`
        source of `uv.lock` takes the same route, because `uv` installs one
        that way and not as an editable.
        """
        return self.add(node.entry("local-wheel", "from-tree") | self.tools_for(node.name))

    def local_sdist_node(self, node: LocalNode) -> str:
        """Add the node that builds the source distribution of a local project.

        **This node is what `uv build` writes.** It runs the PEP 517
        `build_sdist` hook over the tree of the project, and its output is the
        archive that an index would serve. It installs nothing;
        `local_build_node` reads the archive and builds the wheel.
        """
        return self.add(node.entry("local-sdist", "sdist") | self.tools_for(node.name))

    def local_build_node(self, *, name: str, archive: str, backend: str, backend_path: list[str]) -> str:
        """Add the node that builds a wheel out of a local source distribution.

        **The kind is the same one that an artefact of the index takes.** A
        distribution that a node of this graph built and a distribution that
        the index served are the same thing to the build that reads it, so the
        same node kind and the same script serve both. `archive` names the node
        that holds the archive, and an artefact of the index carries a fetch
        instead.
        """
        return self.add(
            {
                "name": f"{name}-from-sdist",
                "kind": "sdist-build",
                "platform": False,
                "from": archive,
                "backend": backend,
                "backendPath": backend_path,
            }
            | self.tools_for(name)
        )

    def nixpkgs_node(self, *, package: str, version: str, drv: str, out: str) -> str:
        """Add the node that takes one package from nixpkgs, already built.

        **This node builds nothing.** `default.nix` instantiated the package
        outside the sandbox and handed in the `.drv` and the output path with
        the dependency between them discarded, which is the tier 4 mechanism
        that `docs/PACKAGING-FACTS.md` describes. `graph.nix` puts the
        dependency back with `builtins.appendContext`, and the outer store then
        **substitutes** the output from a cache rather than building it.

        The name carries a suffix, because the package may also be the name of
        the environment, and two nodes may not share a name.
        """
        return self.add(
            {
                "name": f"{package}-{version}-nixpkgs",
                "kind": "nixpkgs",
                # The store path is whatever nixpkgs built, and this graph does
                # not patch it. nixpkgs already linked it against this system.
                "platform": False,
                "artifact": {"package": package, "version": version, "drv": drv, "out": out},
            }
        )

    def planned_node(self, name: str, planner: dict[str, str]) -> str:
        """Add a node that another planner writes, as the root of its graph.

        **On released Nix a planner writes one derivation.** So a node whose
        shape only a build can decide, such as a local project whose backend
        its tree declares, is the root of a planner that the host made, and
        `graph.nix` reads it through `builtins.outputOf`. The node keeps the
        name that the node itself would have.
        """
        return self.add(
            {
                "name": name,
                "kind": "planned",
                "platform": False,
                "artifact": {"plannerDrv": planner["plannerDrv"], "plannerOut": planner["plannerOut"]},
            }
        )

    def venv_node(self, name: str) -> str:
        """Add the node that composes the members into an environment."""
        return self.add(
            {
                "name": name,
                "kind": "venv",
                "platform": False,
                "members": list(self.members),
                "installed": list(self.installed),
            }
        )

    def application_node(self, name: str, *, venv: str, bases: Sequence[str]) -> str:
        """Add the node that links the programs of one member out of `venv`.

        `bases` names the node that installed each member, and those nodes are
        the template of the shape. `scripts/make-application.py` gives the two
        rules that decide which paths of a template reach the output.
        """
        return self.add(
            {
                "name": name,
                "kind": "application",
                "platform": False,
                "venv": venv,
                "bases": list(bases),
            }
        )

    def root(
        self,
        name: str,
        *,
        application: str | Sequence[str] | None = None,
        venv_planner: dict[str, str] | None = None,
    ) -> str:
        """The node that the planner submits, which is one of two shapes.

        **An environment holds every program of every member, and an
        application holds the programs of the members that the caller names.**
        `uv.nix` gives the second shape through its `application` argument, and
        `scripts/make-application.py` gives the whole reason to want it: a
        virtual environment carries `bin/python`, so a NixOS configuration that
        installs one shadows the interpreter of the system.

        `venv_planner` is the planner that writes the environment, on released
        Nix, where the planner of an application writes only the application.

        `application` takes one name or a list of them. **A list is one output
        over one environment**, and not one output for each name: a project
        that ships three programs ships them together, and three separate
        applications would each hold a link into the same environment.

        The environment is a node either way. The application links out of it,
        so both nodes are in the graph and only the root differs. The
        environment then takes the `-env` suffix, because the application is
        what the caller builds and it carries the name of the project.
        """
        if application is None:
            return self.venv_node(name)

        wanted = [application] if isinstance(application, str) else list(application)
        if not wanted:
            raise ValueError(
                "application is an empty list. Name at least one member, or pass null for the environment itself."
            )

        bases = [self.by_package.get(canonicalize_name(package)) for package in wanted]
        missing = [package for package, base in zip(wanted, bases, strict=True) if base is None]
        if missing:
            held = ", ".join(sorted(self.by_package)) or "no member at all"
            names = ", ".join(f"'{package}'" for package in missing)
            raise ValueError(
                f"application names {names}, and the environment holds {held}. "
                "An application is one member of the environment, so the selection has to install it."
            )

        # `dict.fromkeys` keeps the order that the caller wrote and drops a
        # repeat. Two names of one member are one template.
        found = list(dict.fromkeys(base for base in bases if base is not None))
        venv = (
            self.planned_node(f"{name}-env", venv_planner)
            if venv_planner is not None
            else self.venv_node(f"{name}-env")
        )
        return self.application_node(name, venv=venv, bases=found)

    def member(self, name: str, label: str, *, package: str) -> None:
        """Mark a node as a member of the environment, and not only a build input.

        `label` is the line that the manifest of the environment carries.
        `package` is the distribution name, which `application_node` needs to
        find the node of one member again.
        """
        self.members.append(name)
        self.installed.append(label)
        self.by_package[canonicalize_name(package)] = name

    def document(self, *, root: str, system: str, python_version: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """The whole document, ready for `graph.nix`."""
        return {
            "system": system,
            "pythonVersion": python_version,
            "tools": inputs["tools"],
            "scripts": inputs["scripts"],
            "nodes": self.nodes,
            "installer": self.installer,
            "root": root,
        }


def open_restricted_store() -> Writer:
    """Open the restricted daemon socket that `builder-rpc-v0` gives a builder,
    with the writer that `PYDD_WRITER` names.

    A planner runs only inside such a build, so a different `NIX_REMOTE` is an
    error and not a fallback. With `PYDD_PROTOCOL=out` the build is one of
    released Nix instead, which has no socket and gives the planner `$out`.
    """
    if os.environ.get("PYDD_PROTOCOL") == "out":
        if "out" not in os.environ:
            raise RuntimeError("PYDD_PROTOCOL is 'out', and this build has no $out to write the root to.")
        return OutWriter()
    remote = os.environ.get("NIX_REMOTE", "")
    if not remote.startswith("unix://"):
        raise RuntimeError(
            f"NIX_REMOTE is {remote!r}. This script runs only inside a "
            "builder-rpc-v0 build, which sets it to the restricted socket."
        )
    # A `builder-rpc-v0` build gets no `$out`. The output arrives through
    # `submit_output` instead, so there is no path to write to.
    if "out" in os.environ:
        raise RuntimeError(f"unexpected: out is set to {os.environ['out']!r}")

    if os.environ.get("PYDD_WRITER", "huggorm") == "huggorm":
        return HuggormWriter(remote)
    return CliWriter()


#: A key whose value is the output path of the derivation that the paired key
#: names, as the menu hands both over with their dependency discarded.
BUILT_PAIRS = {"out": "drv", "plannerOut": "plannerDrv"}


def output_name(entry: dict[str, Any], key: str) -> str:
    """Which output of its derivation the value at `key` is.

    A tool may be an output other than `out`, such as the `lib` of a
    compiler, so its entry names the output.
    """
    return entry.get("output", "out") if key == "out" else "out"


def document_value(state: Any, data: Any, store_dir: str) -> Any:
    """`data` as a Nix value, with the string context that `graph.nix` needs.

    **The context is attached here, because the Nix language cannot.**
    `builtins.storePath` and `builtins.appendContext` call `EnsurePath`, and a
    `builder-rpc-v0` socket refuses it. So a store path gets plain context, and
    the output of a menu entry gets the context of its derivation: reading it
    builds that derivation, and an entry that no node reads is never built.
    """
    match data:
        case dict():
            value = state.make_attrs()
            for name, item in data.items():
                drv = data.get(BUILT_PAIRS.get(name, ""))
                if isinstance(drv, str) and isinstance(item, str):
                    element = f"!{output_name(data, name)}!{drv.split('/')[3]}"
                    state.attrs_set(value, name, state.make_string(item, [element]))
                elif name in BUILT_PAIRS.values():
                    state.attrs_set(value, name, state.make_string(item))
                else:
                    state.attrs_set(value, name, document_value(state, item, store_dir))
            return value
        case list():
            value = state.make_list()
            for item in data:
                state.list_append(value, document_value(state, item, store_dir))
            return value
        case str() if data.startswith(store_dir + "/"):
            return state.make_string(data, [data.split("/")[3]])
        case str():
            return state.make_string(data)
        case bool():
            return state.make_bool(data)
        case int():
            return state.make_int(data)
        case float():
            return state.make_float(data)
        case None:
            return state.make_null()
    raise TypeError(f"no Nix value for {type(data).__name__}")


def submit(writer: Writer, document: dict[str, Any]) -> None:
    """Turn `document` into derivations, and submit the root.

    `PYDD_RENDER` names the path. `nix` evaluates `graph.nix`, and needs the
    huggorm writer. `python` renders the same derivations with
    `pydd.python._render`, through either writer.
    """
    store_dir = os.environ.get("NIX_STORE", "/nix/store")
    match os.environ.get("PYDD_RENDER", "nix"):
        case "nix":
            if not isinstance(writer, HuggormWriter):
                raise RuntimeError("PYDD_RENDER=nix evaluates graph.nix in huggorm, so it needs PYDD_WRITER=huggorm")
            root_drv = submit_evaluated(writer, document, os.environ["PYDD_GRAPH_NIX"], store_dir)
        case "python":
            from ._render import render

            root_drv = render(document, writer.add, store_dir)
            print(f"==> the renderer wrote {root_drv}", file=sys.stderr)
        case other:
            raise RuntimeError(f"PYDD_RENDER is {other!r}, and it takes 'nix' or 'python'")

    writer.submit(root_drv, "out")
    print("==> submitted it as output 'out'", file=sys.stderr)


def submit_evaluated(writer: HuggormWriter, document: dict[str, Any], graph_nix: str, store_dir: str) -> str:
    """Evaluate `graph.nix` over `document`, and name the root `.drv` it wrote.

    **The evaluator reads a fixed file, and the plan is its argument.** The
    decisions of a planner are data, and `graph.nix` is the code that turns
    them into derivations. No planner writes a Nix expression by hand.
    """
    import huggorm

    state = huggorm.EvalState(writer.store)
    graph = state.eval_file(graph_nix)
    root = graph(document_value(state, document, store_dir))

    # The store path of the `.drv` that the evaluator wrote. It is not an
    # output path: this build cannot realise anything.
    root_drv = writer.store.print_store_path(root.drv_path())
    print(f"==> the evaluator wrote {root_drv}", file=sys.stderr)
    return root_drv
