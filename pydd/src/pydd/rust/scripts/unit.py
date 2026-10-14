"""Build one unit of a Cargo project: a node that `pydd.rust` wrote.

`PYDD_UNIT` holds the unit as JSON. Its paths into other nodes are
placeholders, which Nix has replaced with real paths by now.

A compile writes `$out/lib/<file>` (or `$out/bin/<name>`) and
`$out/link.json`, the search paths of the unit and of what it links, as
`[flag, value]` pairs: cargo passes `rustc-link-search` on to every
dependent, and `rustc-link-lib` and the link arguments to the package
alone. A run of a
build script writes `OUT_DIR` to `$out/out` and what the script printed, by
kind, to `$out/flags.json`.
"""

import json
import os
import subprocess
import sys

# A test imports this file with neither set.
unit = json.loads(os.environ.get("PYDD_UNIT", "{}"))
out = os.environ.get("out", "")  # noqa: SIM112 - Nix names it `out`


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, sort_keys=True)


def unique(items):
    return list(dict.fromkeys(items))


#: Target kinds that are a library, which takes the package's `-l`.
LIBRARIES = {"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"}


def link_argument_applies(scope, name):
    """Whether a `rustc-link-arg*` of the package reaches this unit."""
    kinds = set(unit.get("kinds", []))
    testing = unit.get("testing", False)
    match scope:
        case "all":
            return testing or bool(kinds & {"bin", "example", "bench", "test", "cdylib"})
        case "bins":
            return "bin" in kinds and not testing
        case "bin":
            return "bin" in kinds and not testing and name == unit["file"]
        case "tests":
            return testing or "test" in kinds
        case "examples":
            return "example" in kinds
        case "benches":
            return "bench" in kinds
        case "cdylib":
            return "cdylib" in kinds
    return False


def prepare():
    """The flags, environment and link pairs of a unit, with its build script's output.

    `search` goes on to every unit that links this one; `link` is the
    unit's own.
    """
    flags = list(unit["flags"])
    env = dict(os.environ) | unit["env"]
    search = []
    for dependency in unit["link_from"]:
        search += read_json(f"{dependency}/link.json", [])
    link = []
    if script := unit["build_script"]:
        printed = read_json(f"{script}/flags.json", {})
        env["OUT_DIR"] = f"{script}/out"
        env |= printed["env"]
        for cfg in printed["cfg"]:
            flags += ["--cfg", cfg]
        search = printed["search"] + search
        kinds = set(unit.get("kinds", []))
        if (kinds & LIBRARIES and not unit.get("testing")) or not unit.get("has_lib"):
            link += printed["libs"]
        arguments = [arg for scope, name, arg in printed["args"] if link_argument_applies(scope, name)]
        link += [["-C", f"link-arg={arg}"] for arg in arguments]
    # Pairs, so that `-l ssl -l crypto` keeps both `-l`.
    search = [list(pair) for pair in unique(tuple(pair) for pair in search)]
    link = search + [list(pair) for pair in unique(tuple(pair) for pair in link)]
    for path in unit["search"]:
        flags.append(f"-Ldependency={path}")
    for extern in unit["externs"]:
        flags += ["--extern", f"{extern['name']}={extern['path']}"]
    return flags, env, link, search


def compile_unit():
    flags, env, link, search = prepare()
    match unit["output"]:
        case "lib":
            directory = f"{out}/lib"
            target = ["--emit=link", "--out-dir", directory]
        case "bin":
            directory = f"{out}/bin"
            target = ["--emit=link", "-o", f"{directory}/{unit['file']}"]
        case "build-script":
            directory = f"{out}/bin"
            target = ["--emit=link", "-o", f"{directory}/build-script-build"]
    os.makedirs(directory)
    command = ["rustc", unit["src"], *flags, *target, *(arg for pair in link for arg in pair)]
    print(" ".join(command), file=sys.stderr)
    subprocess.run(command, env=env, check=True)
    write_json(f"{out}/link.json", search)


def logged(command, env, cwd=None):
    """Run a test, and keep what it printed as the output."""
    os.makedirs(out)
    print(" ".join(command), file=sys.stderr)
    result = subprocess.run(command, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    sys.stderr.write(result.stdout)
    with open(f"{out}/log", "w", encoding="utf-8") as log:
        log.write(result.stdout)
    if result.returncode != 0:
        sys.exit(result.returncode)


def doctest_unit():
    flags, env, _, search = prepare()
    # rustdoc takes search paths, and a library records the libraries it
    # links in its own metadata.
    paths = [arg for pair in search for arg in pair]
    command = ["rustdoc", "--test", unit["src"], *flags, *paths, "--test-run-directory", unit["cwd"]]
    logged(command, env)


def check_unit():
    logged([unit["program"]], dict(os.environ) | unit["env"], cwd=unit["cwd"])


def rustc_flags(value):
    """`cargo:rustc-flags`, which takes `-l` and `-L`, with or without a space."""
    pairs, words = [], value.split()
    while words:
        word = words.pop(0)
        if word in ("-l", "-L"):
            pairs.append([word, words.pop(0)])
        elif word[:2] in ("-l", "-L"):
            pairs.append([word[:2], word[2:]])
        else:
            sys.exit(f"cargo:rustc-flags takes only -l and -L, not {word!r}")
    return pairs


def parse_cfg(text):
    """`rustc --print cfg` as cargo gives it to a build script: CARGO_CFG_<KEY>, values joined by commas."""
    values = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        # Cargo leaves this out: the profile decides it, and `PROFILE` says so.
        if key == "debug_assertions":
            continue
        values.setdefault(key, [])
        if value:
            values[key].append(value.strip('"'))
    return {f"CARGO_CFG_{key.upper()}": ",".join(found) for key, found in values.items()}


def run_unit():
    out_dir = f"{out}/out"
    os.makedirs(out_dir)
    env = dict(os.environ) | unit["env"] | {"OUT_DIR": out_dir}
    cfg = subprocess.run(
        ["rustc", "--print", "cfg", "--target", unit["target"]], stdout=subprocess.PIPE, text=True, check=True
    )
    env = parse_cfg(cfg.stdout) | env
    for dependency in unit["metadata_from"]:
        env |= read_json(f"{dependency}/flags.json", {}).get("metadata", {})

    result = subprocess.run([unit["program"]], cwd=unit["cwd"], env=env, stdout=subprocess.PIPE, text=True, check=True)
    sys.stderr.write(result.stdout)
    printed = {"cfg": [], "env": {}, "search": [], "libs": [], "args": [], "metadata": {}}
    links = (unit["links"] or "").upper().replace("-", "_")
    for line in result.stdout.splitlines():
        if not line.startswith("cargo:"):
            continue
        key, _, value = line.removeprefix("cargo:").removeprefix(":").partition("=")
        match key:
            case "rustc-cfg":
                printed["cfg"].append(value)
            case "rustc-env":
                name, _, setting = value.partition("=")
                printed["env"][name] = setting
            case "rustc-link-lib":
                printed["libs"].append(["-l", value])
            case "rustc-link-search":
                printed["search"].append(["-L", value])
            case "rustc-flags":
                for flag, setting in rustc_flags(value):
                    printed["search" if flag == "-L" else "libs"].append([flag, setting])
            case "rustc-link-arg":
                printed["args"].append(["all", "", value])
            case "rustc-link-arg-bin":
                name, _, argument = value.partition("=")
                printed["args"].append(["bin", name, argument])
            case "rustc-link-arg-bins" | "rustc-link-arg-tests" | "rustc-link-arg-examples" | "rustc-link-arg-benches":
                printed["args"].append([key.removeprefix("rustc-link-arg-"), "", value])
            case "rustc-link-arg-cdylib" | "rustc-cdylib-link-arg":
                printed["args"].append(["cdylib", "", value])
            case "error":
                sys.exit(f"build script error: {value}")
            case "metadata" if links:
                name, _, setting = value.partition("=")
                printed["metadata"][f"DEP_{links}_{name.upper()}"] = setting
            case "warning" | "rerun-if-changed" | "rerun-if-env-changed" | "rustc-check-cfg":
                pass
            case _ if links and line.startswith("cargo:") and not line.startswith("cargo::"):
                printed["metadata"][f"DEP_{links}_{key.upper()}"] = value
    write_json(f"{out}/flags.json", printed)


if __name__ == "__main__":
    match unit["action"]:
        case "compile":
            compile_unit()
        case "run":
            run_unit()
        case "doctest":
            doctest_unit()
        case "check":
            check_unit()
        case "join":
            os.makedirs(f"{out}/bin")
            for directory in unit["bins"]:
                for name in sorted(os.listdir(directory)):
                    os.symlink(f"{directory}/{name}", f"{out}/bin/{name}")
            for name, path in unit.get("logs", {}).items():
                os.symlink(f"{path}/log", f"{out}/{name}.log")
