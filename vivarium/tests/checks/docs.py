"""Do the docs name commands and library functions that exist?

Four claims, read from the source tree:

- Every `nix run --file . <test>.<driver> -- ARGS` in a Markdown file
  parses with the runner's own parser, with the flags that driver adds.
- Every `vivarium.lib.NAME` named anywhere is a name `lib` exports.
- Every name `lib` exports has a `/** */` doc comment where it is
  defined, the form nixdoc reads.
- The howto's exit status table has each status of `vivarium.exits.Exit`
  and no other, and a short listing anywhere names them all.
- Every node of a Graphviz graph under `docs/` that names a `file` names
  one that exists, and its `symbol` (`name`, or `Class.method`) is
  defined in that file.

    docs SOURCE LIB_NAMES_JSON
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

from vivarium.cli import parse
from vivarium.exits import Exit, describe

# What each wrapper in lib.nix adds after `run --spec SPEC`.
DRIVER_FLAGS: dict[str, list[str]] = {
    "driver": [],
    "driverDebug": ["--break-on-failure"],
    "driverInteractive": ["--interactive"],
    "up": ["--hold"],
}
DRIVER = re.compile(r"nix run --file \. \S+?\.(" + "|".join(DRIVER_FLAGS) + r")\b(.*)")
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
INLINE = re.compile(r"`([^`\n]+(?:\n[^`\n]+)*)`")
SHELL_END = ("&&", "||", "|", ";", "&")
LIB_NAME = re.compile(r"vivarium\.lib\.([A-Za-z_][A-Za-z0-9_]*)")
EXIT_TABLE = "| Exit | Meaning |"
EXIT_ROW = re.compile(r"^\| (\d+) \|")
LISTING = "0 passed, 1 failed"
ATTRIBUTES = re.compile(r"\[([^\]]*)\]")
ATTRIBUTE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
DEFINES: dict[str, str] = {
    ".py": r"^\s*(?:async\s+)?def {name}\b|^\s*class {name}\b|^{name}\s*[:=]",
    ".nix": r"(?:^|\s){name}\s*=",
    ".rs": r"\b(?:fn|struct|enum|const|static) {name}\b",
}


def commands(markdown: str) -> list[str]:
    """Every shell command in a page: each line of a fenced block, and each
    inline code span with its line breaks joined."""
    found: list[str] = []
    for block in FENCE.findall(markdown):
        found.extend(line.removeprefix("$ ") for line in block.splitlines())
    prose = FENCE.sub("", markdown)
    found.extend(span.replace("\n", " ") for span in INLINE.findall(prose))
    return found


def driver_args(command: str) -> tuple[str, list[str]] | None:
    """The driver a command runs and the arguments it passes it, or None
    for a command that runs no driver."""
    match = DRIVER.search(command)
    if match is None:
        return None
    words = shlex.split(match.group(2), comments=True)
    kept: list[str] = []
    for word in words:
        if word in SHELL_END or word.startswith((">", "2>")):
            break
        kept.append(word)
    if kept[:1] != ["--"]:
        return match.group(1), []
    return match.group(1), kept[1:]


def refusal(driver: str, args: list[str]) -> str | None:
    try:
        parse(["run", "--spec", "/spec", *DRIVER_FLAGS[driver], *args])
    except SystemExit as exit:
        return f"exit {exit.code}"
    return None


def doc_comment_before(lines: list[str], at: int) -> bool:
    """Whether the definition on line `at` follows a `/** */` comment."""
    above = at - 1
    while above >= 0 and not lines[above].strip():
        above -= 1
    if above < 0 or not lines[above].strip().endswith("*/"):
        return False
    while above >= 0 and "/*" not in lines[above]:
        above -= 1
    return above >= 0 and lines[above].strip().startswith("/**")


def definition(sources: list[Path], name: str) -> tuple[Path, list[str], int] | None:
    pattern = re.compile(rf"^\s*{re.escape(name)} =")
    for path in sources:
        lines = path.read_text().splitlines()
        for number, line in enumerate(lines):
            if pattern.match(line):
                return path, lines, number
    return None


def exit_rows(markdown: str) -> list[int] | None:
    """The statuses in the table under `EXIT_TABLE`, or None with no table."""
    lines = markdown.splitlines()
    if EXIT_TABLE not in lines:
        return None
    rows: list[int] = []
    for line in lines[lines.index(EXIT_TABLE) + 2 :]:
        match = EXIT_ROW.match(line)
        if match is None:
            break
        rows.append(int(match.group(1)))
    return rows


def graph_claims(dot: str) -> list[tuple[str, str | None]]:
    """Each `file` a graph's nodes name, with the node's `symbol`, if any."""
    claims: list[tuple[str, str | None]] = []
    for block in ATTRIBUTES.findall(dot):
        attributes = dict(ATTRIBUTE.findall(block))
        if "file" in attributes:
            claims.append((attributes["file"], attributes.get("symbol")))
    return claims


def undefined(path: Path, symbol: str) -> list[str]:
    """The parts of *symbol* that *path* defines nowhere."""
    pattern = DEFINES.get(path.suffix)
    if pattern is None:
        return [f"{symbol} (no rule for {path.suffix} files)"]
    text = path.read_text()
    return [
        part
        for part in symbol.split(".")
        if re.search(pattern.format(name=re.escape(part)), text, re.MULTILINE) is None
    ]


def main() -> None:
    root, names = Path(sys.argv[1]), set(json.loads(Path(sys.argv[2]).read_text()))
    problems: list[str] = []

    pages = sorted(root.rglob("*.md"))
    if not pages:
        sys.exit(f"no Markdown under {root}: the check reads nothing")
    checked = 0
    for page in pages:
        for command in commands(page.read_text()):
            found = driver_args(command)
            if found is None:
                continue
            checked += 1
            why = refusal(*found)
            if why is not None:
                problems.append(f"{page.relative_to(root)}: the runner refuses ({why}): {command}")
    if checked == 0:
        sys.exit("no driver command found in the docs: the pattern is stale")
    print(f"read {checked} driver commands")

    for path in sorted(root.rglob("*")):
        if path.suffix not in {".md", ".nix"} or not path.is_file():
            continue
        for name in LIB_NAME.findall(path.read_text()):
            if name not in names:
                problems.append(f"{path.relative_to(root)}: vivarium.lib.{name} is not exported")

    sources = [root / "lib.nix", root / "default.nix"]
    for name in sorted(names):
        defined = definition(sources, name)
        if defined is None:
            problems.append(f"lib.{name}: no definition found in lib.nix or default.nix")
            continue
        source, lines, number = defined
        if not doc_comment_before(lines, number):
            problems.append(f"{source.name}:{number + 1}: lib.{name} has no /** */ doc comment")
    print(f"read {len(names)} lib names")

    rows = exit_rows((root / "docs" / "howto.md").read_text())
    statuses = [status.value for status in Exit]
    if rows is None:
        problems.append(f"docs/howto.md: no exit status table ({EXIT_TABLE})")
    elif rows != statuses:
        problems.append(f"docs/howto.md: the exit status table has {rows}, vivarium.exits.Exit has {statuses}")
    for page in pages:
        for number, line in enumerate(page.read_text().splitlines(), 1):
            if LISTING in line and describe() not in line:
                problems.append(f"{page.relative_to(root)}:{number}: lists exit statuses, not as `{describe()}`")
    print(f"read {len(statuses)} exit statuses")

    graphs = sorted((root / "docs").rglob("*.dot"))
    named = 0
    for graph in graphs:
        for file, symbol in graph_claims(graph.read_text()):
            named += 1
            where = f"{graph.relative_to(root)}: {file}"
            if not (root / file).is_file():
                problems.append(f"{where} does not exist")
            elif symbol is not None and (missing := undefined(root / file, symbol)):
                problems.append(f"{where} defines no {', '.join(missing)} (of {symbol})")
    if graphs and named == 0:
        sys.exit("graphs under docs/ name no file: the attribute pattern is stale")
    print(f"read {named} graph nodes in {len(graphs)} graphs")

    if problems:
        sys.exit("\n".join(problems))
    print(
        "ok: every driver command parses, every lib name exists and has a doc comment,"
        " the exit statuses match, every graph node names a definition that exists"
    )


if __name__ == "__main__":
    main()
