"""A saltenv's fileserver tree, as a Nix source accessor.

The evaluator reads every file of an SLS's imports through Salt's file
client, so `import ./lib.nix` works against a master as it does with
local file_roots. The file list is read once per tree, and a file is
fetched the first time Nix reads it.

Imported only from `render`: it loads huggorm's bindings.
"""

import hashlib
import os
import re
from collections.abc import Callable
from typing import Any

from huggorm_bindings import FileType, SourceAccessor, Stat, StorePath

# nix-base32: the 32 characters a store path's hash part may hold.
_BASE32 = "0123456789abcdfghijklmnpqrsvwxyz"


class CpClient:
    """The `cp` execution functions, as the file client methods SaltTree
    calls. For a render the loader gave no file client."""

    def __init__(self, salt: dict[str, Callable[..., Any]]) -> None:
        self._salt = salt

    def file_list(self, saltenv: str) -> list[str]:
        files: list[str] = self._salt["cp.list_master"](saltenv=saltenv)
        return files

    def dir_list(self, saltenv: str) -> list[str]:
        dirs: list[str] = self._salt["cp.list_master_dirs"](saltenv=saltenv)
        return dirs

    def symlink_list(self, saltenv: str) -> dict[str, str]:
        links: dict[str, str] = self._salt["cp.list_master_symlinks"](saltenv=saltenv)
        return links

    def cache_file(self, path: str, saltenv: str) -> str:
        cached: str = self._salt["cp.cache_file"](path, saltenv=saltenv)
        return cached


class SaltTree(SourceAccessor):
    """The files of one saltenv, as a Salt file client serves them. `/` is
    the root of the saltenv.

    The client is the render's own: in a state render it serves
    file_roots, and in a pillar render pillar_roots.
    """

    def __init__(self, client: Any, saltenv: str) -> None:
        super().__init__()
        self._saltenv = saltenv
        self.refresh(client)

    def refresh(self, client: Any) -> None:
        """List the saltenv again, through `client`, and drop every file
        read so far. A warm evaluator keeps one tree mounted for its
        life, because Nix ignores a second mount at the same path."""
        self._client = client
        saltenv = self._saltenv
        self._files = {"/" + p for p in client.file_list(saltenv)}
        self._links = {"/" + p: t for p, t in client.symlink_list(saltenv).items()}
        self._dirs = {"/"} | {"/" + p.strip("/") for p in client.dir_list(saltenv) if p.strip("/")}
        self._data: dict[str, bytes] = {}

    def digest(self, path: str) -> str | None:
        """The sha256 of the file at `path` as the fileserver serves it
        now, or None for a path it does not serve."""
        if path not in self._files:
            return None
        return hashlib.sha256(self._read(path)).hexdigest()

    def _read(self, path: str) -> bytes:
        if path not in self._data:
            cached = self._client.cache_file(f"salt:/{path}", self._saltenv)
            if not cached:
                raise FileNotFoundError(path)
            with open(cached, "rb") as f:
                self._data[path] = f.read()
        return self._data[path]

    def maybe_lstat(self, path: str) -> Stat | None:
        if path in self._dirs:
            return Stat(FileType.DIRECTORY)
        if path in self._links:
            return Stat(FileType.SYMLINK)
        if path in self._files:
            return Stat(FileType.REGULAR, len(self._read(path)), False)
        return None

    def read_directory(self, path: str) -> dict[str, FileType | None]:
        prefix = path.rstrip("/") + "/"
        entries: dict[str, FileType | None] = {}
        for kind, paths in (
            (FileType.DIRECTORY, self._dirs),
            (FileType.SYMLINK, self._links),
            (FileType.REGULAR, self._files),
        ):
            for p in paths:
                if p.startswith(prefix) and "/" not in p[len(prefix) :] and p != prefix:
                    entries.setdefault(p[len(prefix) :], kind)
        return entries

    def read_link(self, path: str) -> str:
        target: str = self._links[path]
        return target

    def read_file(self, path: str) -> bytes:
        return self._read(path)


def mount_point(saltenv: str) -> StorePath:
    """A store path for a saltenv's tree.

    `mount` trusts a path it is given and reads nothing to hash the tree.
    The tree has no hash before it is read, so the path is a name: one per
    saltenv, which no store holds.
    """
    digest = hashlib.sha256(f"havsorm:{saltenv}".encode()).digest()
    bits = int.from_bytes(digest[:20], "big")
    hash_part = "".join(_BASE32[(bits >> (5 * i)) & 31] for i in range(32))
    name = re.sub(r"[^A-Za-z0-9+._?=-]", "_", f"salt-{saltenv}")
    return StorePath(f"{hash_part}-{name}")


def sls_dir(sls: str, tmplpath: str | None) -> str:
    """The directory of an SLS within its saltenv: `a.b` is `a/b.sls` or
    `a/b/init.sls`, and the cached path says which."""
    rel = sls.replace(".", "/")
    if tmplpath and os.path.normpath(tmplpath).endswith(os.path.join(rel, "init.sls")):
        return rel
    return os.path.dirname(rel)
