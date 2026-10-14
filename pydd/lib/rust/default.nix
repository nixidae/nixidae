{ pkgs, pydd }:
let
  inherit (pkgs) lib;

  cargo =
    {
      # The directory that holds `Cargo.toml` and `Cargo.lock`.
      project,
      name ? (lib.importTOML (project + "/Cargo.toml")).package.name,
      rustc ? pkgs.rustc,
      cargo ? pkgs.cargo,
      # The linker, for binaries, proc-macros and build scripts.
      cc ? pkgs.stdenv.cc,
      target ? pkgs.stdenv.hostPlatform.rust.rustcTarget,
      # The program that `meta.mainProgram` names, or `null`.
      program ? name,
      # Take the tools and environment of each crate from nixpkgs'
      # `defaultCrateOverrides` (`./hints.nix`).
      nixpkgsHints ? true,
      # What the nodes of a crate build with, by crate name, as
      # `{ openssl-sys = [ pkgs.pkg-config pkgs.openssl.dev pkgs.openssl ]; }`.
      # It replaces the hints of that crate. Each entry reaches the build
      # script and the compile of the crate: `bin` on `PATH`,
      # `lib/pkgconfig` and `share/pkgconfig` on `PKG_CONFIG_PATH`, and
      # `include` as `-isystem` in `CFLAGS` and `CXXFLAGS`. A node runs no
      # setup hook, so name each output that the build reads.
      buildTools ? { },
      # More of a crate, by crate name, as
      # `{ zstd-sys.env.ZSTD_SYS_USE_PKG_CONFIG = "1"; }`. `env` adds to the
      # hints of the crate, and wins per variable.
      overrides ? { },
    }:
    assert lib.assertMsg (pydd.protocol != "out") (
      "pydd.rust.cargo: plannerProtocol = \"out\" needs the host to make every node, "
      + "and only cargo, in a build, knows the units. Use \"rpc\" or \"recursive\"."
    );
    let
      # `[package.metadata.pydd]` of the project, or `[workspace.metadata.pydd]`.
      #
      #     [package.metadata.pydd.build-tools]
      #     openssl-sys = ["pkg-config", "openssl.dev", "openssl"]
      #
      #     [package.metadata.pydd.overrides.zstd-sys]
      #     env = { ZSTD_SYS_USE_PKG_CONFIG = "1" }
      #
      # A value of `build-tools` names an attribute of `pkgs`, and a dot
      # walks into it.
      manifest = lib.importTOML (project + "/Cargo.toml");
      declared = (manifest.workspace.metadata.pydd or { }) // (manifest.package.metadata.pydd or { });
      attributeOf =
        crate: attribute:
        let
          parts = lib.splitString "." attribute;
        in
        if lib.hasAttrByPath parts pkgs then
          lib.getAttrFromPath parts pkgs
        else
          throw (
            "pydd.rust.cargo: [package.metadata.pydd.build-tools] of ${name} gives '${crate}' "
            + "the name '${attribute}', and nixpkgs holds no attribute of that name."
          );
      lock = lib.importTOML (project + "/Cargo.lock");
      hinted = lib.optionalAttrs nixpkgsHints (import ./hints.nix { inherit lib pkgs; } lock.package);
      tools =
        lib.mapAttrs (_: hint: hint.tools) hinted
        // lib.mapAttrs (crate: map (attributeOf crate)) (declared.build-tools or { })
        // buildTools;
      env = lib.zipAttrsWith (_: lib.foldl' (a: b: a // b) { }) [
        (lib.mapAttrs (_: hint: hint.env) hinted)
        (lib.mapAttrs (_: override: override.env or { }) (declared.overrides or { }))
        (lib.mapAttrs (_: override: override.env or { }) overrides)
      ];
      native = lib.mapAttrs (crate: _: {
        tools = map toString (tools.${crate} or [ ]);
        env = env.${crate} or { };
      }) (tools // env);

      source = lib.cleanSourceWith {
        src = project;
        filter = path: _: baseNameOf path != "target";
        name = "${name}-source";
      };
      # nixpkgs makes one fetch for each crate of the lock, and this
      # directory links to each one. A node then reads only its own crate.
      vendor = pkgs.rustPlatform.importCargoLock { lockFile = project + "/Cargo.lock"; };
      planned = pydd.mkPlanner {
        inherit name;
        plan = pkgs.writeText "plan.py" "import sys\nfrom pydd.rust._cargo import main\nsys.exit(main())\n";
        modules = [
          ../../src/pydd/rust/__init__.py
          ../../src/pydd/rust/_cargo.py
        ];
        menu = {
          inherit name target;
          project = "${source}";
          vendor = "${vendor}";
          cargo = "${cargo}";
          rustc = "${rustc}";
          cc = "${cc}";
          coreutils = "${pkgs.coreutils}";
          python = "${pkgs.python3}";
          script = "${../../src/pydd/rust/scripts/unit.py}";
          inherit native;
        };
      };
    in
    import ../core/root.nix {
      inherit pkgs name program;
      application = planned.out;
      passthru =
        builtins.removeAttrs planned [
          "out"
          "overrideAttrs"
        ]
        // {
          dynamic = planned.out;
          # What the planner and the nodes run on, for copying to a store.
          closure = pkgs.linkFarmFromDrvs "${name}-closure" [
            planned.closure
            vendor
            cargo
            rustc
            cc
            pkgs.coreutils
            pkgs.python3
          ];
        };
    };
in
{
  /**
    A Cargo project, planned at build time: one node for each unit of
    cargo's unit graph.

    Takes `project`, the directory that holds `Cargo.toml` and `Cargo.lock`.
    Returns an ordinary derivation of the project's binaries, with
    `override` over these arguments. Its `passthru` holds `planner`,
    `dynamic` (the output that the planner submits) and `closure`.
  */
  cargo = lib.makeOverridable cargo;
}
