#pragma once

/**
 * The settings only the `nix` program registers.
 *
 * `nix` defines these in its own sources, not in a library:
 * `src/nix/unix/daemon.cc`, `develop.cc`, `nix-env/nix-env.cc` and
 * `upgrade-nix.cc`, in 2.34, 2.35 and master alike. A process that
 * links the libraries and reads a nix.conf naming one of them gets
 * `warning: unknown setting` from `initPlugins`, and every NixOS
 * nix.conf names `trusted-users` and `allowed-users`. `bash-prompt`
 * also reaches the configuration from a flake's `nixConfig`.
 *
 * Registered here with the same names, types and defaults, so the
 * configuration reads as it does for `nix`. Nothing in these bindings
 * reads a value yet; `list_settings` and `get_setting` report them.
 */

#include <string>

#include "nix/util/config-global.hh"
#include "nix/util/configuration.hh"
#include "nix/util/types.hh"

namespace huggorm {

struct BinarySettings : nix::Config
{
    nix::Setting<nix::Strings> trustedUsers{
        this, {"root"}, "trusted-users",
        "Users with additional rights when they connect to the Nix daemon. "
        "Registered for `nix`'s daemon; these bindings do not read it."};

    nix::Setting<nix::Strings> allowedUsers{
        this, {"*"}, "allowed-users",
        "Users allowed to connect to the Nix daemon. "
        "Registered for `nix`'s daemon; these bindings do not read it."};

    nix::Setting<std::string> bashPrompt{
        this, "", "bash-prompt", "The bash prompt (`PS1`) in `nix develop` shells."};

    nix::Setting<std::string> bashPromptPrefix{
        this, "", "bash-prompt-prefix", "Prefix prepended to the `PS1` environment variable in `nix develop` shells."};

    nix::Setting<std::string> bashPromptSuffix{
        this, "", "bash-prompt-suffix", "Suffix appended to the `PS1` environment variable in `nix develop` shells."};

    nix::Setting<bool> keepEnvDerivations{
        this, false, "keep-env-derivations",
        "Whether `nix-env` keeps the derivation of a package it adds to a user environment.",
        {"env-keep-derivations"}};

    nix::Setting<std::string> upgradeNixStorePathUrl{
        this,
        "https://github.com/NixOS/nixpkgs/raw/master/nixos/modules/installer/tools/nix-fallback-paths.nix",
        "upgrade-nix-store-path-url",
        "Used by `nix upgrade-nix`, the URL of the file that contains the store paths of the latest Nix release."};
};

/**
 * Function-local, so one object exists however many translation units
 * include this header.
 */
inline BinarySettings & binary_settings()
{
    static BinarySettings settings;
    return settings;
}

/**
 * At load, as libcmd registers its own: `loadConfFile` stores a name
 * no registered object takes as unknown, and a later registration
 * does not claim it.
 */
inline nix::GlobalConfig::Register register_binary_settings{&binary_settings()};

} // namespace huggorm
