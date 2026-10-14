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
    }:
    assert lib.assertMsg (pydd.protocol != "out") (
      "pydd.rust.cargo: plannerProtocol = \"out\" needs the host to make every node, "
      + "and only cargo, in a build, knows the units. Use \"rpc\" or \"recursive\"."
    );
    let
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
        modules = [ ../../src/pydd/rust/__init__.py ../../src/pydd/rust/_cargo.py ];
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
        };
      };
    in
    import ../core/root.nix {
      inherit pkgs name program;
      application = planned.out;
      passthru = builtins.removeAttrs planned [
        "out"
        "overrideAttrs"
      ] // {
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
