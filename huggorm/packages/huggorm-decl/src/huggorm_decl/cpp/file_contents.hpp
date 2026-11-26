#pragma once
/**
 * File contents an EvalState's mounts have read, keyed by their SHA-256
 * (nixidae/nixidae#738).
 *
 * Every mount on one state keeps its file bytes here. A tree after an
 * edit mounts at a new store path, but most of its files are the ones
 * the last tree had; a client that sends each file's hash with the mount
 * gets those from here instead of over the link.
 *
 * The key is the hash this side computes of the bytes it received, never
 * one a client claims, so a lookup answers only bytes that really have
 * that hash. Per state, not per process: a client gets back only what
 * its own state read. Process-wide, knowing another client's file hash
 * would be enough to read that file.
 *
 * Past `budget` bytes, new contents are not kept; nothing is evicted.
 */

#include <cstddef>
#include <map>
#include <mutex>
#include <optional>
#include <string>

#include "nix/util/hash.hh"

namespace huggorm {

class FileContents
{
public:
    static constexpr size_t budget = 256 << 20;

    static std::string hashOf(std::string_view data)
    {
        return nix::hashString(nix::HashAlgorithm::SHA256, data).to_string(nix::HashFormat::Base16, false);
    }

    std::optional<std::string> find(const std::string & hash) const
    {
        std::lock_guard lock(mutex_);
        auto i = contents_.find(hash);
        if (i == contents_.end())
            return std::nullopt;
        return i->second;
    }

    /** Keep `data`, and answer the hash it is kept under. */
    std::string keep(std::string data)
    {
        auto hash = hashOf(data);
        std::lock_guard lock(mutex_);
        if (bytes_ + data.size() <= budget) {
            auto size = data.size();
            if (contents_.emplace(hash, std::move(data)).second)
                bytes_ += size;
        }
        return hash;
    }

private:
    mutable std::mutex mutex_;
    std::map<std::string, std::string> contents_;
    size_t bytes_ = 0;
};

} // namespace huggorm
