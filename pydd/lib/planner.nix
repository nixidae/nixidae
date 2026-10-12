{
  lib,
  bash,
  python3,
  nixVersions,
  linkFarmFromDrvs,
}:

let
  inherit (lib) fileset;

  # Only `.py` files, so a stray `__pycache__` cannot change a planner.
  # A mode adds its own subpackage through `modules`, so an edit to one mode
  # does not rebuild the planners of the others.
  core = fileset.difference (fileset.fileFilter (file: file.hasExt "py") ../src/pydd) (
    fileset.unions [
      ../src/pydd/python
      ../src/pydd/evaluate.py
    ]
  );
  pyddSrc =
    modules:
    fileset.toSource {
      root = ../src;
      fileset = fileset.unions ([ core ] ++ modules);
    };
in
{
  /**
    Discard every dependency a menu would carry, but keep the `.drv`.

    A planner may then name the derivation as an input of a node it writes,
    and Nix builds it only when a node does. A candidate the planner does
    not pick is never built.

    # Inputs

    `drv`
    : A derivation.

    `outputs`
    : The outputs to give the planner the paths of.
  */
  candidate =
    drv:
    {
      outputs ? drv.outputs or [ "out" ],
    }:
    {
      drv = builtins.unsafeDiscardOutputDependency drv.drvPath;
      outputs = lib.genAttrs outputs (output: builtins.unsafeDiscardStringContext drv.${output}.outPath);
    };

  /**
    A `builder-rpc-v0` derivation that runs a Python planner.

    The planner writes a graph and submits its root, a derivation named
    `name`. Nix asks the planner to carry the name of what it submits, so
    the planner itself is named `name.drv`.

    Returns `{ planner, out }`, where `out` is the root's output, through
    `builtins.outputOf`.

    # Inputs

    `name`
    : The name of the root derivation the planner submits.

    `plan`
    : The planner, a Python file.

    `menu`
    : Anything `builtins.toJSON` takes. The planner reads it as
      `Planner.menu`. Every store path in it becomes an input of the
      planner, so a node it writes may read that path.

    `writer`
    : `"cli"` writes through `nix derivation add`. `"huggorm"` writes in
      process, and needs `huggorm`.

    `huggorm`
    : A huggorm package built against a Nix with `builder-rpc-v0`.

    `pythonPath`
    : Packages the plan imports, such as `packaging`.

    `modules`
    : Filesets under `src/` that the plan imports beside the core of
      `pydd`, such as `src/pydd/python`.
  */
  mkPlanner =
    {
      name,
      plan,
      menu ? null,
      writer ? "cli",
      huggorm ? null,
      nix ? nixVersions.git,
      python ? if writer == "huggorm" then huggorm.pythonModule else python3,
      pythonPath ? [ ],
      modules ? [ ],
      system ? python.stdenv.hostPlatform.system,
    }:
    assert lib.assertOneOf "writer" writer [
      "cli"
      "huggorm"
    ];
    assert lib.assertMsg (
      writer == "huggorm" -> huggorm != null
    ) "mkPlanner: writer = \"huggorm\" needs huggorm";
    let
      planner = derivation {
        name = "${name}.drv";
        inherit system;
        builder = "${bash}/bin/bash";
        # The full path: CPython finds its prefix from argv[0], and Nix runs a
        # builder with only the basename there.
        args = [
          "-c"
          "exec ${python}/bin/python3 ${plan}"
        ];

        requiredSystemFeatures = [ "builder-rpc-v0" ];
        # The submitted object is a derivation, and a derivation is
        # text-ingested.
        __contentAddressed = true;
        outputHashMode = "text";
        outputHashAlgo = "sha256";

        NIX_CONFIG = "extra-experimental-features = nix-command ca-derivations dynamic-derivations";
        PATH = lib.makeBinPath [ nix ];
        PYTHONPATH = lib.concatStringsSep ":" (
          [ "${pyddSrc modules}" ]
          ++ lib.optional (writer == "huggorm") (python.pkgs.makePythonPath [ huggorm ])
          ++ map (package: python.pkgs.makePythonPath [ package ]) pythonPath
        );
        PYTHONDONTWRITEBYTECODE = "1";

        PYDD_WRITER = writer;
        PYDD_SYSTEM = system;
        PYDD_MENU = builtins.toJSON menu;
      };
    in
    {
      inherit planner;
      out = builtins.outputOf planner.outPath "out";
      # What the planner runs on, as one derivation, for copying to a store.
      closure = linkFarmFromDrvs "${name}-planner-closure" (
        [
          bash
          python
          nix
        ]
        ++ lib.optional (writer == "huggorm") huggorm
        ++ pythonPath
      );
    };
}
