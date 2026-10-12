"""Write a resolved plan as derivations: one per node, and the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .._derivation import Derivation
from .._planner import Node as Written
from .._planner import Planner
from ._resolve import Artifact, Node, Plan

#: The tools every node runs with.
BASE_TOOLS = ("bash", "coreutils", "unzip", "gnutar", "gzip", "python")


def store_path_of(path: Path) -> str:
    """The store path that holds ``path``."""
    return "/".join(str(path).split("/")[:4])


@dataclass
class Inputs:
    """What one node reads. Naming a path through this records it as an input,
    as string context does in a Nix expression."""

    srcs: set[str] = field(default_factory=set[str])
    drvs: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def src(self, path: str) -> str:
        self.srcs.add(store_path_of(Path(path)))
        return path

    def output(self, node: Written) -> str:
        self.drvs.setdefault(node.path, ["out"])
        return node.output()

    def fetched(self, artifact: Artifact) -> str:
        self.drvs.setdefault(artifact.drv, ["out"])
        return artifact.out


class GraphWriter:
    """Writes each node of a plan through a planner, dependencies first."""

    def __init__(
        self, planner: Planner, tools: Mapping[str, str], scripts: str, python_version: str
    ) -> None:
        self.planner = planner
        self.tools = tools
        self.scripts = Path(scripts)
        self.site = f"lib/python{python_version}/site-packages"
        self.written: dict[str, Written] = {}

    def write(self, plan: Plan, name: str) -> Written:
        """Write every node of ``plan`` and the environment over its members."""
        for node in plan.nodes.values():
            self._write(plan, node)
        inputs = Inputs()
        members = " ".join(inputs.output(self.written[member]) for member in plan.members)
        return self._add(
            name,
            inputs,
            self._run_python(inputs, "make-venv.py"),
            {"members": members, "installed": "\n".join(plan.installed)},
        )

    def _write(self, plan: Plan, node: Node) -> Written:
        if node.name in self.written:
            return self.written[node.name]
        # Leaves first: Nix checks each input of a node when it writes it.
        for dependency in (plan.installer, *node.backend_nodes):
            if dependency != node.name:
                self._write(plan, plan.nodes[dependency])

        inputs = Inputs()
        env: dict[str, str] = {}
        if node.kind != "unpack":
            env["installerPath"] = self._site_of(inputs, plan.installer)
        if node.backend is not None:
            env["backend"] = node.backend
            env["backendPath"] = ":".join(self._site_of(inputs, n) for n in node.backend_nodes)

        match node.kind:
            case "unpack" | "wheel" | "sdist":
                assert node.artifact is not None
                script = {
                    "unpack": "unpack-wheel.sh",
                    "wheel": "install-wheel.sh",
                    "sdist": "build-sdist.sh",
                }
                args = ["-c", f". {self._script(inputs, script[node.kind])}"]
                env["sdist" if node.kind == "sdist" else "wheel"] = inputs.fetched(node.artifact)
                if node.kind == "wheel":
                    env["wheelName"] = node.artifact.filename
                if node.artifact.is_platform_wheel:
                    env |= self._patchelf(inputs)
            case "editable":
                assert node.editable is not None
                args = self._run_python(inputs, "build-editable.py")
                env["source"] = inputs.src(node.editable.source)
                env["editableRoot"] = node.editable.root

        written = self._add(node.name, inputs, args, env)
        self.written[node.name] = written
        return written

    def _add(self, name: str, inputs: Inputs, args: list[str], env: dict[str, str]) -> Written:
        env = {"site": self.site, "PATH": self._path(inputs), **env}
        return self.planner.add(
            Derivation(
                name=name,
                system=self.planner.system,
                builder=f"{self._tool(inputs, 'bash')}/bin/bash",
                args=args,
                env=env,
                srcs=inputs.srcs,
                drvs=inputs.drvs,
            )
        )

    def _patchelf(self, inputs: Inputs) -> dict[str, str]:
        """What a node that installs a binary wheel needs, and no other node.

        ``auto-patchelf`` reads the loader from the bintools wrapper and calls
        ``patchelf``. ``gccLib`` holds the ``libgcc_s`` and ``libstdc++`` a
        manylinux binary needs.
        """
        return {
            "PATH": self._path(inputs, "patchelf"),
            "autoPatchelf": f"{self._tool(inputs, 'autoPatchelf')}/bin/auto-patchelf",
            "NIX_BINTOOLS": self._tool(inputs, "bintools"),
            "patchelfLibs": f"{self._tool(inputs, 'gccLib')}/lib",
        }

    def _path(self, inputs: Inputs, *extra: str) -> str:
        return ":".join(f"{self._tool(inputs, tool)}/bin" for tool in (*BASE_TOOLS, *extra))

    def _run_python(self, inputs: Inputs, script: str) -> list[str]:
        python = self._tool(inputs, "python")
        return ["-c", f'exec "{python}/bin/python3" "{self._script(inputs, script)}"']

    def _site_of(self, inputs: Inputs, name: str) -> str:
        return f"{inputs.output(self.written[name])}/{self.site}"

    def _tool(self, inputs: Inputs, name: str) -> str:
        return inputs.src(self.tools[name])

    def _script(self, inputs: Inputs, name: str) -> str:
        return inputs.src(str(self.scripts / name))
