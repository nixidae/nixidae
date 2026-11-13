"""
Downloads: `nix/store/filetransfer.hh`.

The transfer every Nix download goes through, so a URL is fetched with
the process's own settings: netrc, proxies, `connect-timeout`, the
retries.
"""

from huggorm_dsl.declare import (
    Bint,
    Cxx,
    Local,
    Path,
    Str,
    blocks,
    needs,
    threading,
)


@needs(
    Local("nix/store/filetransfer.hh"),
    Local("nix/util/file-system.hh"),
    Local("nix/util/serialise.hh"),
    Local("nix/util/url.hh"),
)
@threading("pool")
@blocks
def download_file(url: Str, path: Path, executable: Bint = False) -> None:
    """Download `url` into the new file `path`, as it is served.

    The download block of `prefetchFile` in `src/nix/prefetch.cc`: no
    decompression, so a `.tar.gz` stays compressed and its hash is the
    hash of what the server sent. The file is created with mode 0600,
    or 0700 when `executable`, and an existing file is refused.

    `path` names a file on the filesystem of the process that runs
    the binding. Over RPC that is the server's."""
    Cxx("""
        auto fd = nix::openNewFileForWrite(path, executable ? 0700 : 0600, {.truncateExisting = false});
        if (!fd)
            throw nix::SysError("creating temporary file %s", nix::PathFmt(path));
        nix::FdSink sink(fd.get());
        nix::FileTransferRequest request(nix::VerbatimURL{url});
        request.decompress = false;
        nix::getFileTransfer()->download(std::move(request), sink);
        sink.flush();
    """)
