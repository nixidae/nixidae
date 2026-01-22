import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Iterator

STORE_PATH_RE = re.compile(r"/nix/store/[a-z0-9]{32}-[^\s/]+")


def extract_store_paths(value: Any) -> Iterator[Path]:
    match value:
        case str():
            for m in STORE_PATH_RE.finditer(value):
                yield Path(m.group())
        case Mapping():
            for k, v in value.items():
                # volumeAttributes might contain multiarch paths which we don't want to include.
                # storePaths in volumeAttributes are handled as "primary package".
                if k != "volumeAttributes":
                    yield from extract_store_paths(v)
        case Sequence():
            for item in value:
                yield from extract_store_paths(item)


def extract_store_paths_set(value: Any) -> set[Path]:
    """Convenience wrapper that returns a deduplicated set of store paths."""
    return set(extract_store_paths(value))


def extract_store_name(path: Path | str) -> str:
    return str(path).removeprefix("/nix/store/")
