import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Iterator

# Nix base32 excludes: e, o, t, u
STORE_PATH_RE = re.compile(r"/?nix/store/([0-9a-df-np-sv-z]{32}-[^\s/]+)")


def extract_store_paths(value: Any) -> Iterator[Path]:
    match value:
        case str():
            for match in STORE_PATH_RE.findall(value):
                yield Path("/nix/store") / match
        case Mapping():
            for v in value.values():
                yield from extract_store_paths(v)
        case Sequence():
            for item in value:
                yield from extract_store_paths(item)


def extract_store_name(value: Path | str) -> str:
    return str(value).removeprefix("/nix/store/")
