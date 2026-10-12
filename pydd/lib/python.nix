{
  lib,
  pkgs,
  pydd,
  writeText,
}:
{
  /**
    A Python environment from a `uv.lock`, resolved at build time.

    The Nix side reads the lock and makes one fetch per wheel and sdist. It
    knows nothing about wheels or markers. The planner walks the edges of
    the lock from the workspace members, chooses the artefacts this target
    needs with `packaging`, and writes a node for each. An artefact it does
    not choose is never downloaded.

    uv locks neither a PEP 517 backend nor the installer. Put both in a
    dependency group, such as `build = [ "installer", "flit_core" ]`, so
    they are in the lock. The planner never installs that group.

    Returns what `mkPlanner` returns.

    # Inputs

    `name`
    : The name of the environment.

    `src`
    : The directory that holds `uv.lock` and the workspace members.

    `groups`, `extras`
    : The dependency groups and extras of the members to install.

    `editableRoots`
    : `{ <member> = root; }`: where a member is read from at run time.
      `root` may name a variable, as in `"$MYAPP_ROOT"`. The default is the
      store copy of `src`.

    `buildSystems`
    : `{ <package> = { backend, requires }; }`, for each sdist the planner
      builds. uv does not lock what an sdist builds with.

    `noBinary`
    : Packages to build from their sdist, as uv's `no-binary-package`.

    `installer`
    : The requirement of the installer, chosen from the lock.

    `python`
    : The interpreter the environment is for.

    `platform`, `sysPlatform`
    : The target, in the terms of PEP 425 and PEP 508.
  */
  mkEnvironment =
    {
      name,
      src,
      groups ? [ ],
      extras ? [ ],
      editableRoots ? { },
      buildSystems ? { },
      noBinary ? [ ],
      installer ? "installer",
      python ? pkgs.python3,
      platform ? "linux_${pkgs.stdenv.hostPlatform.uname.processor}",
      sysPlatform ? "linux",
      ...
    }@args:
    let
      lock = builtins.fromTOML (builtins.readFile (src + "/uv.lock"));
      # A local sdist has a `path` and no `url`.
      files = lib.concatMap (
        package: lib.optional (package.sdist.url or null != null) package.sdist ++ package.wheels or [ ]
      ) lock.package;
      fetched = lib.listToAttrs (
        map (
          file:
          let
            candidate = pydd.candidate (pkgs.fetchurl {
              inherit (file) url hash;
              # A store path name refuses `%`, and PyPI quotes `+` in a local version.
              name = lib.replaceStrings [ "%2B" ] [ "+" ] (baseNameOf file.url);
            }) { };
          in
          lib.nameValuePair file.url {
            inherit (candidate) drv;
            out = candidate.outputs.out;
          }
        ) files
      );
    in
    pydd.mkPlanner (
      removeAttrs args [
        "src"
        "groups"
        "extras"
        "editableRoots"
        "buildSystems"
        "noBinary"
        "installer"
        "platform"
        "sysPlatform"
      ]
      // {
        inherit python;
        plan = writeText "plan.py" "from pydd.python import main\nmain()\n";
        pythonPath = [ python.pkgs.packaging ] ++ args.pythonPath or [ ];
        modules = [
          (lib.fileset.difference (lib.fileset.fileFilter (
            file: file.hasExt "py"
          ) ../src/pydd/python) ../src/pydd/python/scripts)
        ]
        ++ args.modules or [ ];
        menu = {
          inherit
            name
            platform
            sysPlatform
            groups
            extras
            editableRoots
            buildSystems
            noBinary
            installer
            fetched
            ;
          packages = lock.package;
          source = "${src}";
          pythonVersion = python.version;
          # Its own store path: a node that reads the whole of pydd would
          # rebuild on every edit to it.
          scripts = "${../src/pydd/python/scripts}";
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
