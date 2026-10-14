"""Build one unit of a Cargo project: a node that `pydd.rust` wrote.

`PYDD_UNIT` holds the unit as JSON. Its paths into other nodes are
placeholders, which Nix has replaced with real paths by now.

A compile writes `$out/lib/<file>` (or `$out/bin/<name>`) and
`$out/link.json`, the link flags of the unit and of what it links, as
`[flag, value]` pairs. A run of a
build script writes `OUT_DIR` to `$out/out` and what the script printed, by
kind, to `$out/flags.json`.
"""

import json
import os
import subprocess
import sys

unit = json.loads(os.environ["PYDD_UNIT"])
out = os.environ["out"]  # noqa: SIM112 - Nix names it `out`


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


def compile_unit():
    flags = list(unit["flags"])
    env = dict(os.environ) | unit["env"]
    link = []
    for dependency in unit["link_from"]:
        link += read_json(f"{dependency}/link.json", [])
    if script := unit["build_script"]:
        printed = read_json(f"{script}/flags.json", {})
        env["OUT_DIR"] = f"{script}/out"
        env |= printed["env"]
        for cfg in printed["cfg"]:
            flags += ["--cfg", cfg]
        link = printed["link"] + link
    # Pairs, so that `-l ssl -l crypto` keeps both `-l`.
    link = [list(pair) for pair in unique(tuple(pair) for pair in link)]
    for path in unit["search"]:
        flags.append(f"-Ldependency={path}")
    for extern in unit["externs"]:
        flags += ["--extern", f"{extern['name']}={extern['path']}"]

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
    write_json(f"{out}/link.json", link)


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
    printed = {"cfg": [], "env": {}, "link": [], "metadata": {}}
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
                printed["link"].append(["-l", value])
            case "rustc-link-search":
                printed["link"].append(["-L", value])
            case "rustc-link-arg":
                printed["link"].append(["-C", f"link-arg={value}"])
            case "rustc-flags":
                printed["link"] += rustc_flags(value)
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


match unit["action"]:
    case "compile":
        compile_unit()
    case "run":
        run_unit()
    case "join":
        os.makedirs(f"{out}/bin")
        for directory in unit["bins"]:
            for name in sorted(os.listdir(directory)):
                os.symlink(f"{directory}/{name}", f"{out}/bin/{name}")
