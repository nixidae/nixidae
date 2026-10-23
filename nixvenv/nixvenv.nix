{
  lib,
  buildEnv,
  makeBinaryWrapper,
  runCommand,
}:
/**
  A Python interpreter that starts as a real venv: `sys.prefix` is the venv,
  `sys.executable` points into it, and subprocesses of `sys.executable` see
  the same packages. No PYTHONPATH, no NIX_PYTHONPATH.

  The venv's `bin/` holds the interpreter (`python`, `python3`,
  `python3.X`) and a script for every console and GUI entry point of the
  packages, each starting inside the venv. `executables` picks which of them
  appear in `$out/bin`. The venv itself is `$out/lib/venv`.

  # Inputs

  `python`
  : The interpreter, e.g. `pkgs.python3`.

  `packages`
  : `ps: [ ps.anyio ... ]`, as for `python.withPackages`.

  `executables`
  : Attribute set from a name in the venv's `bin/` to the name in `$out/bin`.
    `null` keeps the name. Default `{ python = null; }`. A list of names is
    shorthand for every one mapped to `null`.

  # Example

  ```nix
  nixvenv {
    python = pkgs.python3;
    packages = ps: [ ps.httpx ps.pytest ];
    executables = { python = "agent-python"; pytest = null; httpx = "httpx-cli"; };
  }
  ```
*/
{
  python,
  packages ? _: [ ],
  executables ? {
    python = null;
  },
}:
let
  modules = python.pkgs.requiredPythonModules (packages python.pkgs);
  site = buildEnv {
    name = "${python.name}-venv-site";
    paths = modules;
    pathsToLink = [ "/${python.sitePackages}" ];
  };
  interpreter = "${python}/bin/${python.executable}";
  interpreterNames = [
    "python"
    "python3"
    "python${python.pythonVersion}"
  ];

  exposed = lib.mapAttrs (from: to: if to == null then from else to) (
    if lib.isList executables then lib.genAttrs executables (_: null) else executables
  );
  targets = lib.attrValues exposed;
  duplicates = lib.unique (lib.filter (t: lib.count (x: x == t) targets > 1) targets);

  expose =
    from: to:
    if lib.elem from interpreterNames then
      # CPython looks for pyvenv.cfg beside argv[0] without resolving
      # symlinks, so a profile link to bin/python would start outside the
      # venv. argv[0] must name the venv's own python.
      ''
        makeBinaryWrapper ${interpreter} $out/bin/${lib.escapeShellArg to} \
          --argv0 $venv/bin/${lib.escapeShellArg from}
      ''
    else
      # A script's shebang names the venv's python, so a link is enough.
      ''
        if [ ! -e $venv/bin/${lib.escapeShellArg from} ]; then
          echo "nixvenv: no executable '${from}' in the venv; it has:" >&2
          ls $venv/bin >&2
          exit 1
        fi
        ln -s $venv/bin/${lib.escapeShellArg from} $out/bin/${lib.escapeShellArg to}
      '';
in
assert lib.assertMsg (
  duplicates == [ ]
) "nixvenv: executables maps several names to ${lib.concatStringsSep ", " duplicates}";
runCommand "${python.name}-venv"
  {
    nativeBuildInputs = [ makeBinaryWrapper ];
    passthru = { inherit python modules; };
    meta = lib.optionalAttrs (lib.length targets == 1) { mainProgram = lib.head targets; };
  }
  ''
    venv=$out/lib/venv
    mkdir -p $out/bin $venv/bin $venv/lib/${python.libPrefix}
    cat > $venv/pyvenv.cfg <<EOF
    home = ${python}/bin
    include-system-site-packages = false
    version = ${python.version}
    executable = ${interpreter}
    EOF
    ${lib.concatMapStrings (n: "ln -s ${interpreter} $venv/bin/${n}\n") interpreterNames}
    ln -s ${site}/${python.sitePackages} $venv/${python.sitePackages}

    ${interpreter} ${./entry-points.py} $venv/${python.sitePackages} $venv/bin $venv/bin/python

    ${lib.concatStrings (lib.mapAttrsToList expose exposed)}
  ''
