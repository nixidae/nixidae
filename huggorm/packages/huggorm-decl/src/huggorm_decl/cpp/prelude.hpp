#pragma once
// The Nix calls the emitter writes into every unit, by name.
//
// The emitter is generic and writes one text for every Nix version. A
// Nix API it reaches through a name here can change in one place, and
// a version branch (`__has_include`, `requires`) goes here, not there.

#include <cstddef>
#include <string>
#include <type_traits>
#include <variant>

#include "nix/util/error.hh"

// `FiberStack`, which a trampoline holds while it calls Python.
#include "huggorm_decl/cpp/fiber_stack.hpp"

namespace huggorm {

/**
 * How many alternatives the std::variant under `v` has, for `decltype`
 * only. Not `std::variant_size`: that refuses a union which inherits
 * its variant, as `nix::SingleDerivedPath` does, and this deduces
 * through the base.
 */
template <typename... T>
std::integral_constant<std::size_t, sizeof...(T)> arm_count(const std::variant<T...> & v);

/**
 * Refuses a word a vocabulary does not hold, as upstream's own parsers
 * refuse one: `parseHashAlgo` throws `UsageError` listing the words.
 */
template <typename... Args>
[[noreturn]] inline void refuse_word(const std::string & fs, const Args &... args)
{
    throw nix::UsageError(fs, args...);
}

/** An enumerator no word names: unreachable once a switch is exhaustive. */
[[noreturn]] inline void unknown_enumerator(const std::string & type)
{
    throw nix::Error("unknown " + type);
}

}  // namespace huggorm
