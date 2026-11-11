"""
URLs: `nix/util/url.hh`.
"""

from huggorm_dsl.declare import (
    Cxx,
    Local,
    Opt,
    Str,
    needs,
    threading,
)


@needs(Local("nix/util/url.hh"))
@threading("pool")
def url_last_path_segment(url: Str) -> Opt[Str]:
    """The last non-empty path segment of `url`, percent-decoded, or None.

    `VerbatimURL::lastPathSegment`: the name `nix store prefetch-file`
    gives a download when no `--name` is given. A URL that does not
    parse falls back to the base name of the whole string."""
    Cxx("return nix::VerbatimURL{url}.lastPathSegment();")
