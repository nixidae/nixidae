{
  lib,
  pkgs,
  pydd,
  writeText,
}:
{
  /**
    A Python environment from a lock file, resolved at build time.

    The Nix side reads the lock and makes one fetch per artefact. It knows
    nothing about wheels. The planner chooses the artefacts this target
    needs, with `packaging`, and writes a node for each. An artefact it does
    not choose is never downloaded.

    Returns what `mkPlanner` returns.

    # Inputs

    `name`
    : The name of the environment.

    `lock`
    : `{ roots, installer, artifacts }`. Each artefact is
      `{ package, version, filename, url, sha256, kind, marker?, backend?, backend-requires? }`,
      where `kind` is `"wheel"` or `"sdist"`.

    `editables`
    : `{ <name> = { src, root }; }`: local projects, built from `src` and
      read from `root` at run time. `root` may name a variable, as in
      `"$MYAPP_ROOT"`.

    `python`
    : The interpreter the environment is for.

    `platform`, `sysPlatform`
    : The target, in the terms of PEP 425 and PEP 508.
  */
  mkEnvironment =
    {
      name,
      lock,
      editables ? { },
      python ? pkgs.python3,
      platform ? "linux_${pkgs.stdenv.hostPlatform.uname.processor}",
      sysPlatform ? "linux",
      ...
    }@args:
    let
      fetch =
        entry:
        pkgs.fetchurl {
          inherit (entry) url;
          hash = "sha256:${entry.sha256}";
          name = entry.filename;
        };
    in
    pydd.mkPlanner (
      removeAttrs args [
        "lock"
        "editables"
        "platform"
        "sysPlatform"
      ]
      // {
        inherit python;
        plan = writeText "plan.py" "from pydd.python import main\nmain()\n";
        pythonPath = [ python.pkgs.packaging ] ++ args.pythonPath or [ ];
        menu = {
          inherit name platform sysPlatform;
          inherit (lock) roots installer;
          pythonVersion = lib.versions.majorMinor python.version;
          # Its own store path: a node that reads the whole of pydd would
          # rebuild on every edit to it.
          scripts = "${../src/pydd/python/scripts}";
          artifacts = map (
            entry:
            let
              fetched = pydd.candidate (fetch entry) { };
            in
            removeAttrs entry [ "url" ]
            // {
              inherit (fetched) drv;
              out = fetched.outputs.out;
            }
          ) lock.artifacts;
          editables = lib.mapAttrs (_: spec: {
            source = "${spec.src}";
            inherit (spec) root;
          }) editables;
          tools = {
            inherit python;
            inherit (pkgs)
              bash
              coreutils
              unzip
              gnutar
              gzip
              patchelf
              ;
            # A node runs no setup hook, so this is the program the hook calls.
            autoPatchelf = pkgs.auto-patchelf;
            bintools = pkgs.stdenv.cc.bintools;
            gccLib = pkgs.stdenv.cc.cc.lib;
          };
        };
      }
    );
}
