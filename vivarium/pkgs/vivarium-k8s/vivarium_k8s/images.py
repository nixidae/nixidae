"""What an image tarball holds, read on the host before it is loaded.

`vivarium load` and `load_images` check after the import that each name
the tarball gives is in the node's runtime, and report them; so both
read the names here, from the tarball's own index, not from the
runtime's output.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Final

STORE: Final = Path("/nix/store")
DOCKER_HUB: Final = "docker.io"
# The two annotations an OCI archive names its images by: OCI's own, and
# the one `ctr images export` and nerdctl write.
REF_ANNOTATIONS: Final = ("io.containerd.image.name", "org.opencontainers.image.ref.name")


class ImageError(ValueError):
    pass


def image_refs(tarball: Path) -> list[str]:
    """The image names a `docker save` or OCI archive gives, gzipped or
    not, normalized as the runtime lists them. Raises ImageError for a
    tarball that names none: an image without a name cannot be asked for."""
    try:
        with tarfile.open(tarball, "r:*") as archive:
            names = set(archive.getnames())
            if "manifest.json" in names:
                refs = _docker(archive)
            elif "index.json" in names:
                refs = _oci(archive)
            else:
                raise ImageError(f"{tarball} is no image archive: neither manifest.json nor index.json")
    except (tarfile.TarError, OSError, json.JSONDecodeError) as error:
        raise ImageError(f"{tarball}: {error}") from error
    if not refs:
        raise ImageError(f"{tarball} names no image: tag it (`docker save NAME:TAG`, or `name`/`tag` in dockerTools)")
    return sorted({normalized(ref) for ref in refs})


def _read(archive: tarfile.TarFile, name: str) -> object:
    member = archive.extractfile(name)
    if member is None:
        raise ImageError(f"{name} is not a file in the archive")
    return json.load(member)


def _docker(archive: tarfile.TarFile) -> list[str]:
    manifest = _read(archive, "manifest.json")
    if not isinstance(manifest, list):
        raise ImageError("manifest.json is not a list")
    return [str(tag) for entry in manifest if isinstance(entry, dict) for tag in entry.get("RepoTags") or []]


def _oci(archive: tarfile.TarFile) -> list[str]:
    index = _read(archive, "index.json")
    if not isinstance(index, dict):
        raise ImageError("index.json is not an object")
    refs: list[str] = []
    for manifest in index.get("manifests") or []:
        annotations = manifest.get("annotations") if isinstance(manifest, dict) else None
        if not isinstance(annotations, dict):
            continue
        found = next((annotations[key] for key in REF_ANNOTATIONS if key in annotations), None)
        if found is not None:
            refs.append(str(found))
    return refs


def normalized(ref: str) -> str:
    """*ref* as containerd lists it: `busybox:1` is
    `docker.io/library/busybox:1`. A first part with a dot or a port, or
    `localhost`, is a registry and stays."""
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return ref
    if not rest:
        return f"{DOCKER_HUB}/library/{ref}"
    return f"{DOCKER_HUB}/{ref}"


def listed_as(ref: str) -> set[str]:
    """The names a runtime may list the normalized *ref* by: containerd
    keeps it; podman (CRI-O) puts an image without a registry under
    `localhost/`."""
    return {ref, f"localhost/{ref.removeprefix(f'{DOCKER_HUB}/library/').removeprefix(f'{DOCKER_HUB}/')}"}


def latest(ref: str) -> bool:
    """Whether *ref* is `:latest`, or untagged: kubelet's default pull
    policy for it is Always, and a pull of a loaded image finds nothing."""
    name = ref.rsplit("/", 1)[-1]
    return "@" not in name and (":" not in name or name.endswith(":latest"))


def store_path(path: Path) -> Path | None:
    """The store path *path* is in, or None for a file outside the store."""
    if len(path.parts) > len(STORE.parts) and path.parts[: len(STORE.parts)] == STORE.parts:
        return Path(*path.parts[: len(STORE.parts) + 1])
    return None
