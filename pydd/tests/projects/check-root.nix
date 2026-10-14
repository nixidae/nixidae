# The root of an environment as an ordinary derivation (Lillecarl/pydd#7):
# `override` over the arguments of `uv.nix`, then `overrideAttrs` with an
# install hook, then a run of Python from the tree that the root holds.
{
  pkgs,
  pydd,
  project ? ./pure-app,
  venvName ? "root-derivation",
}:

let
  base = pydd.python.uv {
    inherit project;
    venvName = "root-before-override";
  };
  root = (base.override { inherit venvName; }).overrideAttrs (old: {
    postInstall = (old.postInstall or "") + ''
      echo "the hook ran" > "$out/hooked"
    '';
  });
in
assert root.name == venvName;
(derivation {
  name = "${venvName}-check";
  system = pkgs.stdenv.hostPlatform.system;
  builder = "${pkgs.bash}/bin/bash";
  PATH = "${pkgs.coreutils}/bin";
  args = [
    "-c"
    ''
      set -eu
      report() {
        cat "${root}/hooked" || return 1
        prefix="$("${root}/bin/python" -c 'import sys; print(sys.prefix)')" || return 1
        echo "prefix       $prefix"
        if [ "$prefix" != "${root}" ]; then
          echo "the interpreter of the root reads another environment" >&2
          return 1
        fi
        "${root}/bin/python" -c 'import rich; print("rich", rich.__file__)' || return 1
      }
      if ! report > "$out" 2>&1; then
        while IFS= read -r line; do printf '%s\n' "$line" >&2; done < "$out"
        exit 1
      fi
    ''
  ];
})
// {
  # The environment, for `test-projects` to compare across writers.
  planner = root;
}
