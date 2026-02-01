self: pkgs: {
  # Overlay lib
  lib = pkgs.lib.extend (import ../lib);

  # First argument is NIX_STATE_DIR which is where we init the dumped database
  nix_init_db =
    pkgs.writeScriptBin "nix_init_db" # bash
      ''
        #! ${pkgs.runtimeShell}
        NSD="$1"
        shift
        export USER nobody
        nix-store --option store local --dump-db "$@" | NIX_STATE_DIR="$NSD" nix-store --load-db --option store local
      '';

  nix-csi = pkgs.python3Packages.callPackage ../python {
    inherit (self) csi-proto-python kr8s;
  };

  lruLix = pkgs.lixPackageSets.lix_2_94.lix.overrideAttrs (oldAttrs: {
    src = builtins.fetchTree {
      type = "github";
      owner = "lillecarl";
      repo = "lix";
      ref = "regtimeabuse2.94";
    };
    doCheck = false;
    doInstallCheck = false;
  });

  csi-proto-python = pkgs.python3Packages.callPackage ./csi-proto-python { };
  python-jsonpath = pkgs.python3Packages.callPackage ./python-jsonpath.nix { };
  kr8s = pkgs.python3Packages.callPackage ./kr8s.nix { inherit (self) python-jsonpath; };
  shellous = pkgs.python3Packages.callPackage ./shellous.nix { };

  nix-csi-validpaths-monitor = pkgs.callPackage ./nix-csi-validpaths-monitor { };
}
