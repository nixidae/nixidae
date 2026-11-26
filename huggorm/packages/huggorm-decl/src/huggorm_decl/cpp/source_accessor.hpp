#pragma once
/**
 * A `nix::SourceAccessor` that Python implements (nixidae/nixidae#737).
 *
 * One class serves both directions, as `Sink` and `Source` do. A Python
 * subclass is a file tree Nix reads: the evaluator, `addToStore` and a
 * NAR dump all read through its hooks. A wrapper over an accessor Nix
 * made is what Python reads a Nix tree with: its hooks forward to that
 * accessor. A subclass made over another accessor forwards what it does
 * not override, and `super()` reaches the one below.
 *
 * The hooks take a path as the absolute string `CanonPath::abs()`
 * gives, so the trampoline needs no `CanonPath` type.
 */

#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>

#include "nix/util/error.hh"
#include "nix/util/serialise.hh"
#include "nix/store/path.hh"
#include "nix/util/source-accessor.hh"

namespace huggorm {

/**
 * Nix 2.35 gives `SourceAccessor` a private pure `anchor()`. Private,
 * so no expression names it; but a class that implements every other
 * pure virtual is still abstract exactly when Nix has it.
 */
struct AccessorProbe : nix::SourceAccessor
{
    void readFile(const nix::CanonPath &, nix::Sink &, nix::fun<void(uint64_t)>) override {}

    std::optional<Stat> maybeLstat(const nix::CanonPath &) override
    {
        return std::nullopt;
    }

    DirEntries readDirectory(const nix::CanonPath &) override
    {
        return {};
    }

    std::string readLink(const nix::CanonPath &) override
    {
        return {};
    }
};

template<typename Base>
struct AnchoredAccessor : Base
{
private:
    void anchor() override {}
};

using AccessorBase = std::conditional_t<
    std::is_abstract_v<AccessorProbe>,
    AnchoredAccessor<nix::SourceAccessor>,
    nix::SourceAccessor>;

class SourceAccessor : public AccessorBase
{
public:
    SourceAccessor() = default;

    explicit SourceAccessor(std::shared_ptr<nix::SourceAccessor> below)
        : below_(std::move(below))
    {
    }

    /** A subclass over `below`, or over nothing. */
    explicit SourceAccessor(const std::optional<std::shared_ptr<SourceAccessor>> & below)
        : below_(below.value_or(nullptr))
    {
    }

    virtual ~SourceAccessor() = default;

    virtual std::optional<Stat> maybe_lstat(const std::string & path)
    {
        return below("maybe_lstat").maybeLstat(nix::CanonPath(path));
    }

    virtual DirEntries read_directory(const std::string & path)
    {
        return below("read_directory").readDirectory(nix::CanonPath(path));
    }

    virtual std::string read_link(const std::string & path)
    {
        return below("read_link").readLink(nix::CanonPath(path));
    }

    virtual std::string read_file(const std::string & path)
    {
        return below("read_file").readFile(nix::CanonPath(path));
    }

    std::optional<Stat> maybeLstat(const nix::CanonPath & path) override
    {
        return maybe_lstat(path.abs());
    }

    DirEntries readDirectory(const nix::CanonPath & path) override
    {
        return read_directory(path.abs());
    }

    std::string readLink(const nix::CanonPath & path) override
    {
        return read_link(path.abs());
    }

    /** Nix wants the size before the first byte, so the hook answers the whole file. */
    void readFile(const nix::CanonPath & path, nix::Sink & sink, nix::fun<void(uint64_t)> sizeCallback) override
    {
        auto data = read_file(path.abs());
        sizeCallback(data.size());
        sink(data);
    }

private:
    nix::SourceAccessor & below(const char * hook) const
    {
        if (!below_)
            throw nix::Error("this SourceAccessor has nothing below it: a subclass overrides '%s'", hook);
        return *below_;
    }

    std::shared_ptr<nix::SourceAccessor> below_;
};

/**
 * One mount point in an EvalState's store filesystem, whose tree can be
 * replaced (nixidae/nixidae#738).
 *
 * Nix's mount table keeps the first accessor at a path:
 * `MountedSourceAccessor::mount` is `emplace`, and nothing unmounts. So
 * huggorm mounts this forwarder once and swaps what it forwards to. A
 * client that lost its connection reconnects, claims the state, and
 * rebinds the mount to its new accessor; the evaluator keeps its work.
 *
 * Reads take a copy of the target under the lock and run outside it, so
 * a rebind never waits for a read that is blocked on a client.
 *
 * Every answer is kept. The store path names the tree's content, so an
 * answer never goes stale, and a rebind keeps them: the new tree is the
 * same one. Nix caches no accessor read itself, so without this each
 * re-run of `readDir`, `pathExists` or `readFile` is one more round trip
 * to a remote client. A failure is not kept: a read through a dead
 * client succeeds after a rebind. File contents count against
 * `fileBudget`; past it a file is read again each time.
 */
class Mount : public AccessorBase
{
public:
    static constexpr size_t fileBudget = 64 << 20;

    Mount(nix::StorePath path, std::shared_ptr<nix::SourceAccessor> target, bool mounted)
        : path_(std::move(path))
        , mounted_(mounted)
        , target_(std::move(target))
    {
    }

    const nix::StorePath & path() const
    {
        return path_;
    }

    /** False when the store already held the path: the evaluator reads the store's copy, not this. */
    bool isMounted() const
    {
        return mounted_;
    }

    void rebind(std::shared_ptr<nix::SourceAccessor> target)
    {
        std::lock_guard lock(mutex_);
        target_ = std::move(target);
    }

    std::optional<Stat> maybeLstat(const nix::CanonPath & path) override
    {
        return kept(stats_, path, [&] { return target()->maybeLstat(path); });
    }

    DirEntries readDirectory(const nix::CanonPath & path) override
    {
        return kept(directories_, path, [&] { return target()->readDirectory(path); });
    }

    std::string readLink(const nix::CanonPath & path) override
    {
        return kept(links_, path, [&] { return target()->readLink(path); });
    }

    void readFile(const nix::CanonPath & path, nix::Sink & sink, nix::fun<void(uint64_t)> sizeCallback) override
    {
        std::string data;
        if (auto found = find(files_, path))
            data = *found;
        else {
            nix::StringSink collected;
            target()->readFile(path, collected, [](uint64_t) {});
            data = std::move(collected.s);
            std::lock_guard lock(mutex_);
            if (fileBytes_ + data.size() <= fileBudget && files_.emplace(path.abs(), data).second)
                fileBytes_ += data.size();
        }
        sizeCallback(data.size());
        sink(data);
    }

private:
    std::shared_ptr<nix::SourceAccessor> target() const
    {
        std::lock_guard lock(mutex_);
        return target_;
    }

    template<typename T>
    std::optional<T> find(const std::map<std::string, T> & from, const nix::CanonPath & path) const
    {
        std::lock_guard lock(mutex_);
        auto i = from.find(path.abs());
        if (i == from.end())
            return std::nullopt;
        return i->second;
    }

    template<typename T, typename Read>
    T kept(std::map<std::string, T> & into, const nix::CanonPath & path, Read read)
    {
        if (auto found = find(into, path))
            return *found;
        // Outside the lock: the read may wait on a remote client.
        T answer = read();
        std::lock_guard lock(mutex_);
        into.emplace(path.abs(), answer);
        return answer;
    }

    nix::StorePath path_;
    bool mounted_;
    mutable std::mutex mutex_;
    std::shared_ptr<nix::SourceAccessor> target_;
    std::map<std::string, std::optional<Stat>> stats_;
    std::map<std::string, DirEntries> directories_;
    std::map<std::string, std::string> links_;
    std::map<std::string, std::string> files_;
    size_t fileBytes_ = 0;
};

} // namespace huggorm
