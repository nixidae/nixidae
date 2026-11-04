{
  # The Python package set that this package is a member of, passed
  # explicitly by the caller.
  #
  # **Not `python.pkgs`.** Measured: `pythonBase.pkgs.datamodel-code-generator`
  # is the overridden derivation, and
  # `pythonBase.pkgs.python.pkgs.datamodel-code-generator` is the plain
  # nixpkgs one. The interpreter carries the set it was built with, so a
  # round trip out to `python` and back in reaches the set from *before*
  # `packageOverrides` ran. Everything below this file resolved from that
  # earlier set, so an override of any dependency of tree-sitter-config
  # silently did nothing. That cost a day of the scheduled build.
  pythonPackages,
  treeSitterCli,
  nixpkgsPath,
  treeSitterNixSrc,
}:
let
  # Builds tree-sitter-nix's Python bindings from the tree-sitter-nix-numtide
  # flake input instead of nixpkgs' pinned fetch, so bumping the flake input
  # is all it takes to pick up grammar.js changes -- no round-trip through
  # nixpkgs (bump grammar-sources.nix rev/hash, wait for cache, etc).
  #
  # Points at numtide/tree-sitter-nix, not nix-community/tree-sitter-nix:
  # numtide's README states it's "kept moving while upstream is stalled"
  # and "new bug reports and PRs should be filed here" -- confirmed nixpkgs
  # itself still pins the stalled nix-community rev.
  grammarDrv = treeSitterCli.buildGrammar {
    language = "nix";
    version = "0.0.0-local";
    src = treeSitterNixSrc;
    generate = true;
    # numtide's tree-sitter.json advertises an "ocaml" bindings entry that
    # postdates the tree-sitter-config schema nixpkgs' pinned CLI generates
    # its pydantic model from -- the python wrapper's config() parsing
    # (below) rejects it as an unknown field. Drop just that key; the file
    # is otherwise still valid and still needed (config() uses it to find
    # queries/highlights.scm).
    postPatch = ''
      jq 'del(.bindings.ocaml)' tree-sitter.json > tree-sitter.json.tmp
      mv tree-sitter.json.tmp tree-sitter.json
    '';
  };
in
(pythonPackages.callPackage "${nixpkgsPath}/pkgs/development/python-modules/tree-sitter-grammars" {
  name = "tree-sitter-nix";
  inherit grammarDrv;
}).overridePythonAttrs
  (_: {
    # The builder's own pname is "python-tree-sitter-nix" (drvPrefix =
    # "python-${name}") -- pyproject.toml dependency-name matching
    # (pythonRuntimeDepsCheckHook) needs it to read "tree-sitter-nix",
    # matching the upstream nixpkgs override this replaces.
    pname = "tree-sitter-nix";
    # nixpkgs' `binding.c` does not declare `Py_mod_gil`, so importing it
    # on 3.14t turns the GIL back on for the whole process. The binding
    # keeps no state: it returns a pointer to a static. The source is a
    # symlinkJoin, so the file is a link into the store and is replaced,
    # not edited.
    postPatch = ''
      binding=tree_sitter_nix/binding.c
      cp --remove-destination "$(readlink -f "$binding")" "$binding"
      chmod +w "$binding"
      substituteInPlace "$binding" --replace-fail \
        'return PyModule_Create(&module);' \
        'PyObject *m = PyModule_Create(&module);
      #ifdef Py_GIL_DISABLED
          if (m != NULL && PyUnstable_Module_SetGIL(m, Py_MOD_GIL_NOT_USED) < 0) {
              Py_DECREF(m);
              return NULL;
          }
      #endif
          return m;'
    '';
  })
