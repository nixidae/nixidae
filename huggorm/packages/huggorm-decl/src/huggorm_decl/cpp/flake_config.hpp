#pragma once

/**
 * A flake's `nixConfig`, applied to one evaluator's own settings.
 *
 * A decision, not a binding. `nix::flake::ConfigFile::apply`
 * (`libflake/config.cc:55`) writes each value through `globalConfig`.
 * An evaluator here owns its evaluator and fetcher settings
 * (`EvalCore`), so a name that `apply` sets there would not reach the
 * state that locks, and it would reach every state made after it.
 *
 * This is that loop with the targets changed. Keep it equal to
 * `config.cc` on every lane: the 2.34, 2.35 and git sources hold the
 * same text (nixidae/nixidae#1284). Its trusted-list helpers are
 * `static` there, so they are copied too.
 */

#include <cctype>
#include <filesystem>
#include <map>
#include <string>
#include <variant>
#include <vector>

#include <nlohmann/json.hpp>

#include "nix/expr/eval-settings.hh"
#include "nix/fetchers/fetch-settings.hh"
#include "nix/flake/flake.hh"
#include "nix/flake/settings.hh"
#include "nix/util/ansicolor.hh"
#include "nix/util/config-global.hh"
#include "nix/util/file-system.hh"
#include "nix/util/logging.hh"
#include "nix/util/users.hh"

namespace huggorm {

// setting name -> setting value -> allow or ignore.
using TrustedList = std::map<std::string, std::map<std::string, bool>>;

inline std::filesystem::path trusted_list_path()
{
    return nix::getDataDir() / "trusted-settings.json";
}

inline TrustedList read_trusted_list()
{
    auto path = trusted_list_path();
    if (!nix::pathExists(path))
        return {};
    return nlohmann::json::parse(nix::readFile(path));
}

inline void write_trusted_list(const TrustedList & trusted_list)
{
    auto path = trusted_list_path();
    nix::createDirs(path.parent_path());
    nix::writeFile(path, nlohmann::json(trusted_list).dump());
}

/**
 * Set each trusted value of `config` on the first of these that holds
 * its name: `eval`, `fetch`, `flake` (the settings of this one call),
 * then `globalConfig`, which holds the store's settings. Nix calls
 * `store->setOptions()` after it; so does the caller.
 */
inline void apply_flake_config(
    const nix::flake::ConfigFile & config,
    nix::flake::Settings & flake,
    nix::EvalSettings & eval,
    nix::fetchers::Settings & fetch)
{
    nix::StringSet whitelist{
        "bash-prompt",
        "bash-prompt-prefix",
        "bash-prompt-suffix",
        "flake-registry",
        "commit-lock-file-summary",
        "commit-lockfile-summary"};

    for (auto & [name, value] : config.settings) {

        auto baseName = nix::hasPrefix(name, "extra-") ? std::string(name, 6) : name;

        std::string valueS;
        if (auto * s = std::get_if<std::string>(&value))
            valueS = *s;
        else if (auto * n = std::get_if<int64_t>(&value))
            valueS = nix::fmt("%d", *n);
        else if (auto * b = std::get_if<nix::Explicit<bool>>(&value))
            valueS = b->t ? "true" : "false";
        else if (auto ss = std::get_if<std::vector<std::string>>(&value)) {
            // Deprecated, and `config.cc` still calls it: `concatStringsSep`
            // would keep a leading empty string, which Nix drops.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
            valueS = nix::dropEmptyInitThenConcatStringsSep(" ", *ss);
#pragma GCC diagnostic pop
        }
        else
            throw nix::Error("flake configuration setting '%s' has a value of no known type", name);

        if (!whitelist.count(baseName) && !flake.acceptFlakeConfig) {
            bool trusted = false;
            auto trustedList = read_trusted_list();
            auto tlname = nix::get(trustedList, name);
            if (auto saved = tlname ? nix::get(*tlname, valueS) : nullptr) {
                trusted = *saved;
                // `printInfo`, written out: the macro names `logger`,
                // `fmt` and `lvlInfo` without the namespace.
                if (nix::lvlInfo <= nix::verbosity)
                    nix::logger->log(
                        nix::lvlInfo,
                        nix::fmt(
                            "Using saved setting for '%s = %s' from ~/.local/share/nix/trusted-settings.json.",
                            name,
                            valueS));
            } else {
                if (std::tolower(nix::logger
                                     ->ask(nix::fmt(
                                         "do you want to allow configuration setting '%s' to be set to '" ANSI_RED
                                         "%s" ANSI_NORMAL "' (y/N)?",
                                         name,
                                         valueS))
                                     .value_or('n'))
                    == 'y') {
                    trusted = true;
                }
                if (std::tolower(nix::logger
                                     ->ask(nix::fmt(
                                         "do you want to permanently mark this value as %s (y/N)?",
                                         trusted ? "trusted" : "untrusted"))
                                     .value_or('n'))
                    == 'y') {
                    trustedList[name][valueS] = trusted;
                    write_trusted_list(trustedList);
                }
            }
            if (!trusted) {
                nix::warn(
                    "ignoring untrusted flake configuration setting '%s'.\nPass '%s' to trust it",
                    name,
                    "--accept-flake-config");
                continue;
            }
        }

        if (!eval.set(name, valueS) && !fetch.set(name, valueS) && !flake.set(name, valueS))
            nix::globalConfig.set(name, valueS);
    }
}

}  // namespace huggorm
