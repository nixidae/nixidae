/**
  The installer and the common build backends, as rows of the menu, for a
  project whose own lock does not hold its build system. `./pyproject.toml`
  names them, and `./uv.lock` pins them.

  Returns `{ lock, rows }`. `rows` leaves out each file that `own` already
  lists, and marks the rest `buildSystems = true`: the planner and the host
  of `out` take the project's own installer before this one.
*/
{
  pkgs,
  # The rows of the project's own lock.
  own,
}:
let
  inherit (pkgs) lib;
  lock = ./uv.lock;
  document = builtins.fromTOML (builtins.readFile lock);
  owned = builtins.listToAttrs (
    map (row: {
      name = row.filename;
      value = true;
    }) own
  );
  rowOf =
    package: kind: file:
    let
      filename = lib.last (lib.splitString "/" file.url);
    in
    {
      inherit kind filename;
      package = package.name;
      inherit (package) version;
      buildSystems = true;
      drv = pkgs.fetchurl {
        inherit (file) url hash;
        name = filename;
      };
    };
  rowsOf =
    package:
    map (rowOf package "wheel") (package.wheels or [ ])
    ++ map (rowOf package "sdist") (lib.optional (package ? sdist) package.sdist);
  registry = builtins.filter (package: package.source ? registry) document.package;
in
{
  inherit lock;
  rows = builtins.filter (row: !(owned ? ${row.filename})) (lib.concatMap rowsOf registry);
}
