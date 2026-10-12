# Evaluated inside the planner. `bash` and the candidates carry their
# context, so no `builtins.storePath` is needed, and only the candidate this
# reads is built.
{
  system,
  bash,
  candidates,
}:
let
  floating = {
    __contentAddressed = true;
    outputHashMode = "nar";
    outputHashAlgo = "sha256";
  };
  leaf = derivation (
    floating
    // {
      name = "evaluated-leaf";
      inherit system;
      builder = "${bash}/bin/sh";
      args = [
        "-c"
        "read -r line < ${candidates.greeting.outputs.out}; echo \"leaf read: $line\" > $out"
      ];
    }
  );
in
derivation (
  floating
  // {
    name = "evaluated";
    inherit system;
    builder = "${bash}/bin/sh";
    args = [
      "-c"
      "read -r line < ${leaf}; echo \"root read: $line\" > $out"
    ];
  }
)
