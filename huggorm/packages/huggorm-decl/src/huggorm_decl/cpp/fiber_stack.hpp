#pragma once

/**
 * Python on a Determinate Nix fiber.
 *
 * Determinate Nix runs evaluation work on fibers, each on a stack of its
 * own, so a primop or a store override that calls Python can run there.
 * CPython 3.14 reads its stack bounds from the thread, finds a fiber's
 * stack pointer below them, and in `_Py_Dealloc` defers every free on
 * it to a list that never drains: none of 100 objects freed
 * (nixidae/nixidae#1083). Older CPython counts calls, not addresses.
 *
 * `nix::currentFiberStack()` is huggorm's own patch,
 * `nix-determinate-fiber-stack.patch`.
 */

#include <cstddef>
#include <cstdint>

#include <nanobind/nanobind.h>

// `parallel-eval.hh` names `EvalSettings` without including it.
#if __has_include("nix/expr/parallel-eval.hh")
#include "nix/expr/eval.hh"
#include "nix/expr/parallel-eval.hh"
#endif

namespace huggorm {

/** Python's stack bounds set to the current fiber's, with the GIL held. */
class FiberStack
{
public:
    FiberStack()
    {
#if __has_include("nix/expr/parallel-eval.hh") && PY_VERSION_HEX >= 0x030E0000
        auto stack = nix::currentFiberStack();
        // The frame, as CPython reads it. Not the address of a local:
        // ASan can put a local on its fake stack, off the fiber's.
        auto here = reinterpret_cast<std::uintptr_t>(__builtin_frame_address(0));
        auto base = reinterpret_cast<std::uintptr_t>(stack.data());
        // A coroutine body inside the fiber runs on a stack of its own.
        if (here < base || here >= base + stack.size())
            return;
        tstate_ = PyThreadState_Get();
        if (PyUnstable_ThreadState_SetStackProtection(tstate_, stack.data(), stack.size()) != 0) {
            tstate_ = nullptr;
            throw nanobind::python_error();
        }
#endif
    }

    ~FiberStack()
    {
#if PY_VERSION_HEX >= 0x030E0000
        if (tstate_)
            PyUnstable_ThreadState_ResetStackProtection(tstate_);
#endif
    }

    FiberStack(const FiberStack &) = delete;
    FiberStack & operator=(const FiberStack &) = delete;

private:
    [[maybe_unused]] PyThreadState * tstate_ = nullptr;
};

} // namespace huggorm
