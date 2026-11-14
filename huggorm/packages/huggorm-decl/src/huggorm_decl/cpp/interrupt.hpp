#pragma once

/**
 * Cancelling one call, from another thread.
 *
 * libutil stops work at `checkInterrupt`, which asks two things: the
 * process-wide `_isInterrupted` and a thread-local hook,
 * `nix::unix::interruptCheck`. The process-wide flag is what a SIGINT
 * sets and nothing clears, so it would stop every call and keep them
 * stopped. The hook is per thread, and it is the one used here.
 *
 * The hook cannot be set per call from the thread that cancels, because
 * it belongs to the thread doing the work. So it is installed once on
 * each thread that enters a call, and it asks this table whether the
 * call the thread is in (`thread_request`) was cancelled. The cancelling
 * side only writes the table.
 *
 * `checkInterrupt` runs on hot paths, so the hook reads an atomic count
 * first and takes the lock only while some cancellation is pending.
 *
 * Cancellation takes effect at the next `checkInterrupt`. A thread
 * blocked in a system call - a read on the daemon socket while a remote
 * build runs - is woken first: the cancel sends it `wake_signal()`, as
 * Nix's own `ReceiveInterrupts` sends SIGUSR1. The read fails with
 * EINTR, and `nix::read` checks for an interrupt before it reads again
 * (`file-descriptor.cc`). nixidae/nixidae#1219.
 *
 * A signal that lands between that check and the read is lost, as it is
 * in Nix: the canceller sends it again until the call stops.
 */

#include <atomic>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <mutex>
#include <pthread.h>
#include <unordered_map>
#include <unordered_set>

#include "nix/util/signals.hh"

#include "huggorm_decl/cpp/logging.hpp"

namespace huggorm {

/**
 * Real-time, not Nix's SIGUSR1: Python and its libraries use the
 * classic signals, and libmain's `initNix` (which huggorm never calls)
 * takes SIGUSR1.
 */
inline int wake_signal()
{
    return SIGRTMIN + 7;
}

inline void ignore_wake(int) {}

/**
 * Put the no-op handler on `wake_signal()` once, without SA_RESTART, so
 * a blocking call it lands in fails with EINTR. False when the signal
 * already has a handler: it belongs to someone else, and a cancel then
 * waits out a blocking call, as before.
 */
inline bool wake_installed()
{
    static const bool installed = [] {
        struct sigaction old{};
        if (sigaction(wake_signal(), nullptr, &old) != 0)
            return false;
        if ((old.sa_flags & SA_SIGINFO) != 0 || old.sa_handler != SIG_DFL)
            return false;
        struct sigaction act{};
        act.sa_handler = ignore_wake;
        sigemptyset(&act.sa_mask);
        act.sa_flags = 0;
        return sigaction(wake_signal(), &act, nullptr) == 0;
    }();
    return installed;
}

class Cancellations
{
public:
    /** Mark `request` cancelled, and wake the thread inside it. Again on
     * each call: the canceller repeats it for a signal that was lost. */
    void cancel(std::uint64_t request)
    {
        std::lock_guard guard(lock_);
        if (ids_.insert(request).second)
            pending_.fetch_add(1, std::memory_order_release);
        // Under the lock that `leave` takes, so a thread that left is never signalled.
        if (!wake_installed())
            return;
        auto [first, last] = threads_.equal_range(request);
        for (auto it = first; it != last; ++it)
            pthread_kill(it->second, wake_signal());
    }

    /** The calling thread works for `request`. A multimap: one scope can
     * hold calls on several threads at once. */
    void enter(std::uint64_t request)
    {
        if (request == 0)
            return;
        std::lock_guard guard(lock_);
        threads_.emplace(request, pthread_self());
    }

    void leave(std::uint64_t request)
    {
        if (request == 0)
            return;
        std::lock_guard guard(lock_);
        auto [first, last] = threads_.equal_range(request);
        for (auto it = first; it != last; ++it)
            if (pthread_equal(it->second, pthread_self())) {
                threads_.erase(it);
                return;
            }
    }

    void forget(std::uint64_t request)
    {
        std::lock_guard guard(lock_);
        if (ids_.erase(request))
            pending_.fetch_sub(1, std::memory_order_release);
    }

    bool cancelled(std::uint64_t request)
    {
        if (request == 0 || pending_.load(std::memory_order_acquire) == 0)
            return false;
        std::lock_guard guard(lock_);
        return ids_.contains(request);
    }

private:
    std::mutex lock_;
    std::unordered_set<std::uint64_t> ids_;
    std::unordered_multimap<std::uint64_t, pthread_t> threads_;
    std::atomic<std::size_t> pending_{0};
};

inline Cancellations & cancellations()
{
    static Cancellations table;
    return table;
}

/**
 * The interrupt scope this thread is inside, or 0.
 *
 * A second key beside `thread_request`, for a caller whose unit of
 * cancellation is not its unit of logging. nanopynix arms one token
 * around several calls, and each call names its own request for the
 * records it raises. One slot for both would let the request replace
 * the token, and the cancel would then match nothing.
 *
 * Its own table, so a scope and a request with the same number stay
 * two different things.
 */
inline std::uint64_t & thread_interrupt_scope()
{
    static thread_local std::uint64_t scope = 0;
    return scope;
}

inline Cancellations & scope_cancellations()
{
    static Cancellations table;
    return table;
}

/**
 * Put the hook on the calling thread, once.
 *
 * A hook already there is kept and asked too, so an embedder's own
 * interrupt still works on a thread that also runs huggorm calls. The
 * thread also takes `wake_signal()`: Nix's signal thread blocks the
 * classic signals in the threads it starts, and a thread inherits its
 * mask.
 */
inline void install_interrupt_check()
{
    thread_local bool installed = false;
    if (installed)
        return;
    installed = true;
    if (wake_installed()) {
        sigset_t wake;
        sigemptyset(&wake);
        sigaddset(&wake, wake_signal());
        pthread_sigmask(SIG_UNBLOCK, &wake, nullptr);
    }
    auto previous = std::move(nix::unix::interruptCheck);
    nix::unix::interruptCheck = [previous = std::move(previous)] {
        return cancellations().cancelled(thread_request())
            || scope_cancellations().cancelled(thread_interrupt_scope())
            || (previous && previous());
    };
}

}  // namespace huggorm
