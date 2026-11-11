"""
Archives: `nix/util/tarfile.hh`.
"""

from huggorm_dsl.declare import (
    Cxx,
    Local,
    Path,
    blocks,
    needs,
    threading,
)


@needs(Local("nix/util/tarfile.hh"))
@threading("pool")
@blocks
def unpack_tarfile(archive: Path, destination: Path) -> None:
    """Unpack `archive` into the directory `destination`.

    libarchive reads it, so a tarball under any compression it knows
    and a zip file both work, which is what `nix store prefetch-file
    --unpack` takes. Both paths are on the filesystem of the process
    that runs the binding."""
    Cxx("nix::unpackTarfile(archive, destination);")
