{ pydd, bash }:
let
  # Released Nix gives a planner no way to add a derivation, so the host
  # makes every node except the root.
  leaf = derivation {
    name = "released-leaf";
    inherit (bash.stdenv.hostPlatform) system;
    builder = "${bash}/bin/sh";
    args = [
      "-c"
      ''echo "hello from a node the host made" > $out''
    ];
  };
in
pydd.mkPlanner {
  name = "released";
  plan = ./plan.py;
  protocol = "out";
  menu = {
    bash = "${bash}";
    leaf = pydd.candidate leaf { };
  };
}
