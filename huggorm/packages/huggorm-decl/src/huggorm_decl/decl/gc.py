"""
nix::GCOptions and nix::GCResults: `nix/store/gc-store.hh`.

The two halves of one call. `collectGarbage` takes an options struct
by const reference and fills a results struct through an out
parameter, so both are records and only one of them is a shape this
repo already had.

`GCResults` is `@produced`: libstore made it and Python reads it, like
every other record here.

`GCOptions` is the other direction, and it is the first of its kind. A
caller BUILDS one and hands it down. That is a class rather than four
keyword parameters on the method, because a struct that names a
concept Python has no word for is a class - and flattening it would
make this repo restate four defaults that upstream already states
(nixidae/nixidae#611).
"""

from typing import Annotated

from huggorm_decl.decl.path import StorePath
from huggorm_decl.decl.words import GCAction
from huggorm_dsl.declare import (
    U64,
    Bint,
    Cxx,
    Local,
    Opt,
    Std,
    Str,
    Variant,
    Vec,
    binding,
    header,
    needs,
    produced,
    reads,
    wire_value,
)


@header(Local("huggorm_decl/cpp/gc_options.hpp"))
@binding(
    cxx="huggorm::GCWholeStore",
    threading="pool",
    blocking=False,
)
@wire_value(unit=True)
class GCWholeStore:
    """Collect across the whole store."""

    def __init__(self) -> None:
        """Nothing to say: the arm is the whole fact."""
        Cxx("new (self) huggorm::GCWholeStore{};")


@header(Local("huggorm_decl/cpp/gc_options.hpp"))
@binding(
    cxx="huggorm::GCSpecificPaths",
    threading="pool",
    blocking=False,
)
@wire_value()
class GCSpecificPaths:
    """Collect within these paths only.

    An empty set collects nothing: upstream returns before it
    reads a root. Nix 2.34 reads the set only to delete it: a
    `GCOptions` with any other action, or with `delete_referrers`,
    refuses it there."""

    def __init__(self, paths: Vec[StorePath], delete_referrers: Bint = False) -> None:
        """The paths, and whether their dead referrers may go too."""
        Cxx("""
            new (self) huggorm::GCSpecificPaths{
                as_set<nix::StorePathSet>(paths), delete_referrers};
        """)

    @reads("paths")
    def paths(self) -> Vec[StorePath]:
        """The paths. Sorted, as Nix's set keeps them."""

    @reads("deleteReferrers")
    def delete_referrers(self) -> Bint:
        """Whether a dead referrer of one of the paths may go too."""


GCPaths = Annotated[
    GCWholeStore | GCSpecificPaths,
    Variant(
        "huggorm::GCPaths",
        header=Local("huggorm_decl/cpp/gc_options.hpp"),
        # `std::variant<WholeStore, SpecificPaths>`, in that order.
        bare=True,
    ),
]
"""Where a collection looks: the whole store, or some paths."""


@header(Local("nix/store/gc-store.hh"))
@needs(Std("limits"), Local("huggorm_decl/cpp/gc_options.hpp"))
@binding(
    cxx="nix::GCOptions",
    # Four members the object owns. Nothing here talks to a store.
    threading="pool",
    blocking=False,
)
@wire_value()
class GCOptions:
    """What to collect, and how far to go.

    The whole of `nix-store --gc`'s argument surface in one object.
    `action` decides whether anything is deleted at all, and the other
    three narrow it.

    Built by a caller, which is what makes it the first of its kind
    here. Every other record in this package is `@produced` - something
    libstore made and Python reads.
    """

    def __init__(
        self,
        action: GCAction = GCAction.DELETE_DEAD,
        ignore_liveness: Bint = False,
        paths_to_delete: Opt[GCPaths] = None,
        max_freed: Opt[U64] = None,
    ) -> None:
        """Say what to collect. Every answer has an upstream default.

        `paths_to_delete` None leaves upstream's default, the whole
        store, and `max_freed` None leaves no limit: neither default
        has a Python literal a declaration could carry.
        """
        Cxx("""
            new (self) nix::GCOptions{};
            self->action = action;
            self->ignoreLiveness = ignore_liveness;
            if (paths_to_delete)
                huggorm::set_gc_paths(*self, *paths_to_delete);
            if (max_freed)
                self->maxFreed = *max_freed;
        """)

    @reads("action")
    def action(self) -> GCAction:
        """Which of the four operations to run."""

    @reads("ignoreLiveness")
    def ignore_liveness(self) -> Bint:
        """Whether to delete paths the roots still reach.

        Dangerous, in upstream's own word. A path is still refused
        when another store path depends on it, so this drops the ROOT
        half of the check and keeps the reference half."""

    def paths_to_delete(self) -> GCPaths:
        """Where the collection looks: the whole store, or some paths."""
        Cxx("return huggorm::gc_paths_of(self);")

    def max_freed(self) -> Opt[U64]:
        """Stop once this many bytes have been freed, or None for no
        limit.

        Upstream spells "no limit" as the largest u64, which is a
        SENTINEL rather than a size - no store holds 18 exabytes - so
        None is the honest reading and it is what crosses. Exactly the
        shape `PathInfo.registration_time` has, where upstream spells
        "unknown" as 0."""
        # Read back as the number, the default `GCOptions()` could not
        # cross a signed wire field: `ValueError: Value out of range:
        # 18446744073709551615`. nixidae/nixidae#616 holds the width question.
        Cxx("""
            if (self.maxFreed == std::numeric_limits<std::uint64_t>::max())
                return std::nullopt;
            return self.maxFreed;
        """)


@header(Local("nix/store/gc-store.hh"))
@binding(
    cxx="nix::GCResults",
    threading="pool",
    blocking=False,
)
@produced
@wire_value()
class GCResults:
    """What a collection found, or removed.

    Both fields depend on the action, so neither means anything
    without it. That is why they arrive together rather than as a
    method's return value.
    """

    @reads("paths", collection="nix::StringSet")
    def paths(self) -> Vec[Str]:
        """The roots, or the paths that were or would be deleted.

        Strings, not store paths, and that is upstream's own choice:
        the field is a `StringSet`. For `RETURN_LIVE` and
        `RETURN_DEAD` these are store paths spelled out; for the two
        deleting actions they are what was removed. A root can be a
        path outside the store, so the type cannot promise more than
        a string."""

    @reads("bytesFreed")
    def bytes_freed(self) -> U64:
        """How many bytes the two deleting actions freed.

        Zero for `RETURN_LIVE` and `RETURN_DEAD`, which delete
        nothing."""
