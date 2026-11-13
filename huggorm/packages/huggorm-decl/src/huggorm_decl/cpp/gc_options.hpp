#pragma once

/**
 * Where a collection looks, in one spelling on every Nix huggorm builds.
 *
 * Nix 2.35 holds it as `GCOptions::GCPaths`, a variant of `WholeStore`
 * and `SpecificPaths`. Nix 2.34 holds a bare `StorePathSet` and has
 * neither arm, so on 2.34 the arms are the stand-ins below.
 *
 * The stand-ins carry a trap of 2.34. Only `gcDeleteSpecific` reads the
 * set there (gc.cc:636, :698): `gcDeleteDead` with a set collects the
 * whole store, and `gcReturnDead` answers for the whole store. 2.34 also
 * has no `deleteReferrers`. So `set_gc_paths` refuses both on 2.34,
 * where 2.35 does what they say. 2.35's own client refuses the same two
 * against a daemon too old for them (remote-store.cc:706-710).
 *
 * The stand-ins go when the floor reaches 2.35 (nixidae/nixidae#1316).
 */

#include <utility>
#include <variant>

#include "nix/store/gc-store.hh"
#include "nix/util/error.hh"

namespace huggorm {

struct GCWholeStoreBefore235
{};

struct GCSpecificPathsBefore235
{
    nix::StorePathSet paths;
    bool deleteReferrers = false;
};

template<typename Options>
struct gc_arms
{
    static constexpr bool native = false;
    using whole = GCWholeStoreBefore235;
    using specific = GCSpecificPathsBefore235;
};

template<typename Options>
    requires requires { typename Options::SpecificPaths; }
struct gc_arms<Options>
{
    static constexpr bool native = true;
    using whole = typename Options::WholeStore;
    using specific = typename Options::SpecificPaths;
};

using GCWholeStore = gc_arms<nix::GCOptions>::whole;
using GCSpecificPaths = gc_arms<nix::GCOptions>::specific;
using GCPaths = std::variant<GCWholeStore, GCSpecificPaths>;

/** Point `options` at `paths`. Set `options.action` first: 2.34 reads it. */
template<typename Options = nix::GCOptions>
void set_gc_paths(Options & options, GCPaths paths)
{
    if constexpr (gc_arms<Options>::native) {
        options.pathsToDelete = std::move(paths);
    } else {
        auto * specific = std::get_if<GCSpecificPaths>(&paths);
        if (!specific)
            return;
        if (options.action != Options::gcDeleteSpecific)
            throw nix::UsageError(
                "this Nix collects within a set of paths only to delete them (DELETE_SPECIFIC); "
                "any other action would act on the whole store");
        if (specific->deleteReferrers)
            throw nix::UsageError("this Nix cannot delete the referrers of a set of paths");
        options.pathsToDelete = std::move(specific->paths);
    }
}

/** Where `options` looks. On 2.34 a set means something only under `gcDeleteSpecific`. */
template<typename Options = nix::GCOptions>
GCPaths gc_paths_of(const Options & options)
{
    if constexpr (gc_arms<Options>::native) {
        return options.pathsToDelete;
    } else {
        if (options.action == Options::gcDeleteSpecific)
            return GCSpecificPaths{options.pathsToDelete};
        return GCWholeStore{};
    }
}

}  // namespace huggorm
