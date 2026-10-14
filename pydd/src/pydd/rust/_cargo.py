"""Plan a Cargo project: one node for each unit of cargo's unit graph.

A unit is what cargo would give one `rustc` call: a library, a proc-macro, a
binary, the compile of a build script, or the run of one. Cargo batches
nothing, so neither does this planner, and a change to one crate rebuilds
that crate and what depends on it.

The output of a build script is known only when it runs, so a node reads the
output of its build script's node at build time (`scripts/unit.py`).
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from pydd.core import Derivation, Node, Planner

RUN = "run-custom-build"


def store_root(path: str) -> str:
    """The store path that holds `path`."""
    return "/".join(path.split("/")[:4])


@dataclass(frozen=True)
class Menu:
    """What `lib/rust/default.nix` gives the planner."""

    name: str
    project: str
    vendor: str
    cargo: str
    rustc: str
    cc: str
    coreutils: str
    python: str
    script: str
    target: str

    def tools(self) -> str:
        return ":".join(f"{tool}/bin" for tool in (self.rustc, self.cc, self.coreutils))

    def inside(self, path: str) -> str:
        """`path` with the vendor directory resolved to the crate's own store path."""
        if not path.startswith(self.vendor + "/"):
            return path
        entry, _, rest = path.removeprefix(self.vendor + "/").partition("/")
        crate = os.path.realpath(posixpath.join(self.vendor, entry))
        return posixpath.join(crate, rest) if rest else crate


def cargo(menu: Menu, home: str, *args: str) -> Any:
    with open(posixpath.join(home, "config.toml"), "w", encoding="utf-8") as config:
        config.write(
            '[source.crates-io]\nreplace-with = "vendored-sources"\n'
            f'[source.vendored-sources]\ndirectory = "{menu.vendor}"\n'
        )
    env = {
        **os.environ,
        "CARGO_HOME": home,
        "CARGO_TARGET_DIR": posixpath.join(home, "target"),
        # `--unit-graph` is unstable, and this is how a stable cargo takes it.
        "RUSTC_BOOTSTRAP": "1",
        "PATH": f"{menu.cargo}/bin:{menu.tools()}",
    }
    result = subprocess.run(
        [f"{menu.cargo}/bin/cargo", *args, "--offline", "--locked", "--manifest-path", f"{menu.project}/Cargo.toml"],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def version_env(version: str) -> dict[str, str]:
    core, _, pre = version.partition("+")[0].partition("-")
    major, minor, patch = core.split(".")
    return {
        "CARGO_PKG_VERSION": version,
        "CARGO_PKG_VERSION_MAJOR": major,
        "CARGO_PKG_VERSION_MINOR": minor,
        "CARGO_PKG_VERSION_PATCH": patch,
        "CARGO_PKG_VERSION_PRE": pre,
    }


def package_env(package: dict[str, Any], manifest_dir: str) -> dict[str, str]:
    """What cargo sets for every unit of `package`."""
    return {
        "CARGO_PKG_NAME": package["name"],
        "CARGO_PKG_AUTHORS": ":".join(package.get("authors") or []),
        "CARGO_PKG_DESCRIPTION": package.get("description") or "",
        "CARGO_PKG_HOMEPAGE": package.get("homepage") or "",
        "CARGO_PKG_REPOSITORY": package.get("repository") or "",
        "CARGO_PKG_LICENSE": package.get("license") or "",
        "CARGO_PKG_LICENSE_FILE": package.get("license_file") or "",
        "CARGO_PKG_README": package.get("readme") or "",
        "CARGO_PKG_RUST_VERSION": package.get("rust_version") or "",
        "CARGO_MANIFEST_DIR": manifest_dir,
        "CARGO_MANIFEST_PATH": f"{manifest_dir}/Cargo.toml",
        **version_env(package["version"]),
    }


def profile_flags(unit: dict[str, Any]) -> list[str]:
    profile = unit["profile"]
    flags = ["-C", "embed-bitcode=no"]
    if profile["opt_level"] != "0":
        flags += ["-C", f"opt-level={profile['opt_level']}"]
    if profile["debuginfo"]:
        flags += ["-C", f"debuginfo={profile['debuginfo']}"]
    flags += ["-C", f"debug-assertions={'on' if profile['debug_assertions'] else 'off'}"]
    flags += ["-C", f"overflow-checks={'on' if profile['overflow_checks'] else 'off'}"]
    if profile.get("codegen_units"):
        flags += ["-C", f"codegen-units={profile['codegen_units']}"]
    if profile["lto"] not in ("false", "off"):
        raise NotImplementedError(f"{unit['pkg_id']}: profile lto = {profile['lto']!r}")
    # A build script and a proc-macro run inside cargo's own process model,
    # which unwinds.
    kinds = set(unit["target"]["kind"])
    if profile["panic"] == "abort" and not kinds & {"custom-build", "proc-macro"}:
        flags += ["-C", "panic=abort"]
    strip = (profile.get("strip") or {}).get("resolved", {}).get("Named")
    if strip and strip != "none":
        flags += ["-C", f"strip={strip}"]
    return flags


class Graph:
    """The units of one unit graph, made into nodes leaves first."""

    def __init__(self, planner: Planner, menu: Menu, graph: Any, metadata: Any) -> None:
        self.planner = planner
        self.menu = menu
        self.units: list[dict[str, Any]] = graph["units"]
        self.roots: list[int] = graph["roots"]
        self.packages = {package["id"]: package for package in metadata["packages"]}
        self.members = set(metadata["workspace_members"])
        self.nodes: dict[int, Node] = {}
        self.hashes: dict[int, str] = {}

    def is_build_script(self, index: int) -> bool:
        return "custom-build" in self.units[index]["target"]["kind"]

    def linked(self, index: int) -> list[int]:
        """Every unit whose output `index` links: its dependencies, without build scripts, transitively."""
        seen: dict[int, None] = {}
        stack = [dep["index"] for dep in self.units[index]["dependencies"]]
        while stack:
            dep = stack.pop()
            if dep in seen or self.is_build_script(dep):
                continue
            seen[dep] = None
            stack += [d["index"] for d in self.units[dep]["dependencies"]]
        return sorted(seen)

    def metadata(self, index: int) -> str:
        """`-C metadata` of a unit, from what cargo decided and nothing of this host."""
        if index not in self.hashes:
            unit = self.units[index]
            facts = {
                "pkg_id": unit["pkg_id"],
                "target": [unit["target"]["name"], unit["target"]["kind"]],
                "mode": unit["mode"],
                "platform": unit["platform"],
                "features": unit["features"],
                "profile": unit["profile"],
                "dependencies": [self.metadata(dep["index"]) for dep in unit["dependencies"]],
            }
            digest = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
            self.hashes[index] = digest[:16]
        return self.hashes[index]

    def name(self, index: int) -> str:
        unit = self.units[index]
        package = self.packages[unit["pkg_id"]]
        base = f"{package['name']}-{package['version']}"
        if self.is_build_script(index):
            return f"{base}-build-script" + ("-run" if unit["mode"] == RUN else "")
        if "bin" in unit["target"]["kind"]:
            return unit["target"]["name"]
        return base

    def node(self, index: int) -> Node:
        if index not in self.nodes:
            for dep in self.units[index]["dependencies"]:
                self.node(dep["index"])
            unit = self.units[index]
            spec = self.run_spec(index) if unit["mode"] == RUN else self.compile_spec(index)
            self.nodes[index] = self.planner.add(self.derivation(self.name(index), spec, self.inputs(index)))
        return self.nodes[index]

    def inputs(self, index: int) -> list[int]:
        unit = self.units[index]
        if unit["mode"] == RUN:
            return [dep["index"] for dep in unit["dependencies"]]
        direct = [dep["index"] for dep in unit["dependencies"]]
        return sorted(set(direct) | set(self.linked(index)))

    def derivation(self, name: str, spec: dict[str, Any], inputs: list[int]) -> Derivation:
        menu = self.menu
        return Derivation(
            name=name,
            system=self.planner.system,
            builder=f"{menu.python}/bin/python3",
            args=["-I", menu.script],
            env={"PATH": menu.tools(), "PYDD_UNIT": json.dumps(spec, sort_keys=True)},
            srcs=sorted({menu.python, menu.script, menu.rustc, menu.cc, menu.coreutils, *spec.get("srcs", [])}),
            drvs={self.nodes[dep].path: ["out"] for dep in inputs},
        )

    def manifest_dir(self, unit: dict[str, Any]) -> str:
        package = self.packages[unit["pkg_id"]]
        return self.menu.inside(posixpath.dirname(package["manifest_path"]))

    def build_script_run(self, index: int) -> int | None:
        for dep in self.units[index]["dependencies"]:
            if self.units[dep["index"]]["mode"] == RUN:
                return dep["index"]
        return None

    def compile_spec(self, index: int) -> dict[str, Any]:
        unit = self.units[index]
        target = unit["target"]
        package = self.packages[unit["pkg_id"]]
        manifest_dir = self.manifest_dir(unit)
        crate_name = target["name"].replace("-", "_")
        build_script = self.is_build_script(index)
        crate_type = target["crate_types"][0]
        metadata = self.metadata(index)
        flags = [
            "--crate-name",
            crate_name,
            f"--edition={target['edition']}",
            "--crate-type",
            crate_type,
            *profile_flags(unit),
            *(arg for feature in unit["features"] for arg in ("--cfg", f'feature="{feature}"')),
            "-C",
            f"metadata={metadata}",
        ]
        # A library's file name carries the metadata, as `--extern` names it.
        # A binary and a build script are written with `-o`, which ignores it.
        if crate_type != "bin":
            flags += ["-C", f"extra-filename=-{metadata}"]
        if unit["platform"] is not None:
            flags += ["--target", unit["platform"]]
        if unit["pkg_id"] not in self.members:
            flags += ["--cap-lints", "warn"]
        if crate_type == "proc-macro":
            flags += ["-C", "prefer-dynamic", "--extern", "proc_macro"]

        externs = []
        for dep in unit["dependencies"]:
            dep_unit = self.units[dep["index"]]
            if dep_unit["mode"] == RUN or self.is_build_script(dep["index"]):
                continue
            dep_crate = dep_unit["target"]["name"].replace("-", "_")
            suffix = ".so" if dep_unit["target"]["crate_types"][0] == "proc-macro" else ".rlib"
            path = f"{self.nodes[dep['index']].output()}/lib/lib{dep_crate}-{self.metadata(dep['index'])}{suffix}"
            externs.append({"name": dep["extern_crate_name"], "path": path})
        run = self.build_script_run(index)
        env = package_env(package, manifest_dir) | {"CARGO_CRATE_NAME": crate_name}
        if "bin" in target["kind"]:
            env["CARGO_BIN_NAME"] = target["name"]
        if unit["pkg_id"] in self.members:
            env["CARGO_PRIMARY_PACKAGE"] = "1"
        return {
            "action": "compile",
            "src": self.menu.inside(target["src_path"]),
            "srcs": [store_root(manifest_dir)],
            "flags": flags,
            "externs": externs,
            "search": [self.nodes[dep].output() + "/lib" for dep in self.linked(index)],
            # A linked unit's link flags already hold those of its own dependencies.
            "link_from": [
                self.nodes[dep["index"]].output()
                for dep in unit["dependencies"]
                if not self.is_build_script(dep["index"]) and self.units[dep["index"]]["mode"] != RUN
            ],
            "build_script": self.nodes[run].output() if run is not None else None,
            "env": env,
            "output": "build-script" if build_script else ("bin" if crate_type == "bin" else "lib"),
            "file": target["name"],
        }

    def run_spec(self, index: int) -> dict[str, Any]:
        unit = self.units[index]
        package = self.packages[unit["pkg_id"]]
        manifest_dir = self.manifest_dir(unit)
        program = next(dep["index"] for dep in unit["dependencies"] if self.units[dep["index"]]["mode"] != RUN)
        profile = unit["profile"]
        target = unit["platform"] or self.menu.target
        env = package_env(package, manifest_dir) | {
            "TARGET": target,
            "HOST": self.menu.target,
            "PROFILE": profile["name"] if profile["name"] in ("release", "debug") else "release",
            "OPT_LEVEL": profile["opt_level"],
            "DEBUG": "true" if profile["debuginfo"] else "false",
            "NUM_JOBS": "1",
            "RUSTC": f"{self.menu.rustc}/bin/rustc",
            "RUSTDOC": f"{self.menu.rustc}/bin/rustdoc",
            "CARGO_ENCODED_RUSTFLAGS": "",
            "CARGO_CFG_FEATURE": ",".join(unit["features"]),
            **{f"CARGO_FEATURE_{feature.upper().replace('-', '_')}": "1" for feature in unit["features"]},
        }
        return {
            "action": "run",
            "program": f"{self.nodes[program].output()}/bin/build-script-build",
            "cwd": manifest_dir,
            "srcs": [store_root(manifest_dir)],
            "target": target,
            "links": package.get("links"),
            "metadata_from": [
                self.nodes[dep["index"]].output()
                for dep in unit["dependencies"]
                if self.units[dep["index"]]["mode"] == RUN
            ],
            "env": env,
        }

    def root(self) -> Node:
        bins = [self.node(index) for index in self.roots]
        spec = {"action": "join", "bins": [node.output() + "/bin" for node in bins]}
        return self.planner.add(self.derivation(self.menu.name, spec, list(self.roots)))


def main() -> int:
    planner = Planner.from_environ()
    menu = Menu(**planner.menu)
    with tempfile.TemporaryDirectory() as home:
        args = ["--release", "--target", menu.target]
        graph = cargo(menu, home, "build", *args, "--unit-graph", "-Z", "unstable-options")
        metadata = cargo(menu, home, "metadata", "--format-version", "1", "--filter-platform", menu.target)
    planner.submit(Graph(planner, menu, graph, metadata).root())
    return 0
