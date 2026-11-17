"""Do the docs name commands and library functions that exist?

Three claims, read from the source tree:

- Every `nix run --file . <test>.<driver> -- ARGS` in a Markdown file
  parses with the runner's own parser, with the flags that driver adds.
- Every `vivarium.lib.NAME` named anywhere is a name `lib` exports.
- Every name `lib` exports has a `/** */` doc comment where it is
  defined, the form nixdoc reads.

    docs SOURCE LIB_NAMES_JSON
"""

import json
import re
import shlex
import sys
from pathlib import Path

from vivarium.cli import parse

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

    if problems:
        sys.exit("\n".join(problems))
    print("ok: every driver command parses, and every lib name exists and has a doc comment")


if __name__ == "__main__":
    main()
