# A second ecosystem: Cargo

pydd's core knows no ecosystem. The Rust lane tests that claim. It uses
only `pydd.core`: `Planner`, `Derivation`, the writers, and the root
wrapper `lib/core/root.nix`. It has no plan document and no `graph.nix`.

    root = pydd.rust.cargo { project = ./.; };

The result is an ordinary derivation of the project's binaries, as for
Python. It takes `override` over the arguments, `overrideAttrs` and the
install hooks.

## Cargo plans, pydd writes

Python needs a planner of its own, because a lock file of Python says
nothing of how to build a package. Cargo already has a planner.
`cargo build --unit-graph` gives each unit of a build: its features, its
profile, its edition, whether it builds for the host or the target, and
what it depends on. `Cargo.lock` holds none of the first four.

So the planner runs cargo, offline, over a vendor directory that nixpkgs
makes from `Cargo.lock` (`rustPlatform.importCargoLock`). Each crate of
that directory is its own store path, so a node depends only on its own
crate. The planner sets `RUSTC_BOOTSTRAP=1`, because `--unit-graph` is
unstable.

## One node per unit

The planner writes one node for each unit. That is cargo's own
granularity, and pydd batches nothing.

| unit | node | output |
| --- | --- | --- |
| library | `serde-1.0.229` | `lib/libserde-<metadata>.rlib`, `link.json` |
| proc-macro | `serde_derive-1.0.229` | `lib/libserde_derive-<metadata>.so` |
| build script, compile | `serde-1.0.229-build-script` | `bin/build-script-build` |
| build script, run | `serde-1.0.229-build-script-run` | `out/` (`OUT_DIR`), `flags.json` |
| binary | `serde-app` | `bin/serde-app` |

One more node joins the binaries of the roots, under the project's name.
`tests/rust/serde-app` has 24 units, so 25 nodes.

**A node reads its build script's output when it builds.** No evaluation,
and no planner, knows what a build script prints. The run node writes
`cargo:rustc-cfg`, `rustc-env`, the link flags and the `links` metadata
to `flags.json`. The library node reads that file, and passes the flags
to `rustc`. Link flags go on to every unit that links the library, through
`link.json`.

**`-C metadata` comes from what cargo decided.** It hashes the package ID,
the target, the mode, the platform, the features, the profile and the
metadata of each dependency. It holds no path of the host, so every mode
gives the same file names.

## Native libraries

A crate that links a system library, such as openssl-sys, needs that
library and often `pkg-config` (Lillecarl/pydd#17). Three sources give
the nodes of a crate its tools, and a later one wins for the crate it
names:

| source | example | holds |
| --- | --- | --- |
| nixpkgs hints, `nixpkgsHints = true` | `defaultCrateOverrides.openssl-sys` | tools, variables |
| the project | `[package.metadata.pydd.build-tools]`, `[package.metadata.pydd.overrides.<crate>]` | tools, `env` |
| the call | `buildTools.<crate>`, `overrides.<crate>.env` | tools, `env` |

**The hints are nixpkgs' own table.** `defaultCrateOverrides` tells
`buildRustCrate` the inputs of each crate that needs one, so an overlay
that changes it changes pydd's nodes too. A variable in capitals in an
entry becomes a variable of the node: zstd-sys gets
`ZSTD_SYS_USE_PKG_CONFIG`, and so links the libzstd of nixpkgs rather
than the copy it bundles.

**A tool reaches the build script and the compile of its crate, and no
other node.** `bin` goes on `PATH`, `lib/pkgconfig` and `share/pkgconfig`
on `PKG_CONFIG_PATH`, and `include` into `CFLAGS` and `CXXFLAGS` as
`-isystem`. The library is linked where the build script says, and the
linker wrapper of nixpkgs writes the RUNPATH.

`tests/rust/native-app` builds openssl and zstd with no declaration.
`tests/rust/native-declared` turns the hints off and declares the same
inputs, in `Cargo.toml` for openssl-sys and in the call for zstd-sys.
With the hints off and nothing declared, openssl-sys fails: it finds
neither `pkg-config` nor OpenSSL.

**cargo-dyndrv has no hints.** Its caller writes an `extern` set by hand,
keyed by the full crate ID, with the `PATH` and the environment of each
build script.

## The modes

Each project of `tests/rust` gives one binary and one root in four modes:
`nix run --file . test-rust`.

| mode | Nix | writes through |
| --- | --- | --- |
| `cli` | master, `builder-rpc-v0` | `nix derivation add` |
| `huggorm` | master, `builder-rpc-v0` | huggorm |
| `drv` | master, `builder-rpc-v0` | `pydd-drv` |
| `recursive` | 2.35, recursive-nix | `nix derivation add`, root to `$out` |

**`out` cannot build a Cargo project.** In `out`, the host makes every
node, and the planner writes only its root. Only cargo, in a build, knows
the units. So `pydd.rust.cargo` refuses `plannerProtocol = "out"`. Python
gets around this because the host can make a node for every wheel of the
lock. A Cargo lock gives no such list of nodes.

## Compared with cargo-dyndrv

[cargo-dyndrv](https://github.com/obsidiansystems/cargo-dyndrv) also
writes one derivation per unit of the unit graph. It is a Rust binary that
replaces `cargo` inside `buildRustPackage`, and talks to the daemon through
harmonia. It keeps build-script output as files of flags, and runs each
build script through a small wrapper binary.

pydd's lane is a Python planner over `pydd.core`, so it runs in every
mode that the core has, recursive-nix included. A node builds with a
Python script and `rustc`, and keeps the output of a build script as JSON.

## What is missing

| need | issue |
| --- | --- |
| workspaces, path and git sources, profiles, features | Lillecarl/pydd#18 |
| `CARGO`, `links` metadata, more build-script keys | Lillecarl/pydd#19 |
| fetch only the crates that the graph uses | Lillecarl/pydd#20 |
| cross-compilation | Lillecarl/pydd#21 |

**Every crate of the lock is fetched, also the ones that never build.**
`importCargoLock` makes a fetch for each crate of `Cargo.lock`, so a Linux
build fetches `windows-sys` too. #20 holds the alternative.
