/*
  What the mapper carries beyond memory and cores, each a way a
  nixpkgs test passed under nixos-test and failed here.

  - `additionalPaths` makes `hello` valid in the guest: without it the
    same command exits 1, because the store holds the path and the
    database does not.
  - A path only the testScript names is in the guest.
  - `succeed` returns stdout alone; stderr goes to the console.
*/
{ pkgs, fromNixosTest, ... }:
fromNixosTest (
  let
    named = pkgs.writeText "named-by-the-script" "reached";
  in
  {
    name = "nixos-compat";
    nodes.machine = {
      virtualisation.diskSize = 1536;
      virtualisation.additionalPaths = [ pkgs.hello ];
    };
    testScript = ''
      machine.succeed("nix-store --check-validity ${pkgs.hello}")
      size = int(machine.succeed("df --block-size=1M --output=size / | tail -1"))
      assert size > 1024, f"the root image is {size} MiB, not the 1536 asked for"

      got = machine.succeed("cat ${named}")
      assert got == "reached", f"a path the script names reads {got!r}"

      out = machine.succeed("echo to-stdout; echo to-stderr >&2")
      assert out.strip() == "to-stdout", f"succeed returned {out!r}"
    '';
  }
)
