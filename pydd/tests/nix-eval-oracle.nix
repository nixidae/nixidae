# `lib/modes/nix-eval.nix` against `render-oracle.nix`: the document as plain
# JSON, with no context, must give every node the `.drv` that `graph.nix`
# gives the document with its context. Evaluated in the oracle's own store,
# where `builtins.storePath` finds each path.
let
  oracle = import ./render-oracle.nix;
  document = builtins.toFile "document.json" (
    builtins.unsafeDiscardStringContext (builtins.toJSON oracle.document)
  );
  pathOf = value: if builtins.isAttrs value then value.drvPath else value;
in
builtins.mapAttrs (
  name: expected:
  let
    got = pathOf (
      import ../lib/modes/nix-eval.nix {
        graph = document': import ../lib/python/graph.nix (document' // { root = name; });
        inherit document;
      }
    );
  in
  {
    inherit expected got;
    same = got == expected;
  }
) oracle.expected
