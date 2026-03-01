# SPDX-License-Identifier: MIT

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

  nixkube = pkgs.python3Packages.callPackage ./nixkube {
    inherit (self)
      csi-proto-python
      cri-proto-python
      nri-proto-python
      grpclib-ttrpc
      kr8s
      nri-wait
      ;
  };

  # kluctl = pkgs.kluctl.override {
  #   python310 = pkgs.python3;
  # };

  stdLix = pkgs.lixPackageSets.lix_2_94.lix;
  lruLix = pkgs.lixPackageSets.lix_2_94.lix.overrideAttrs (oldAttrs: {
    src = builtins.fetchTree {
      type = "github";
      owner = "lillecarl";
      repo = "lix";
      ref = "regtimeabuse2.94";
    };
    # src = pkgs.lib.fileset.toSource {
    #   root = /home/lillecarl/Code/lix;
    #   fileset = pkgs.lib.fileset.gitTracked /home/lillecarl/Code/lix;
    # };
    doCheck = false;
    doInstallCheck = false;
  });

  grpclib-ttrpc = pkgs.python3Packages.callPackage ./grpclib-ttrpc {
    inherit (self) ttrpc-proto-python;
  };
  csi-proto-python = pkgs.python3Packages.callPackage ./csi-proto-python { };
  cri-proto-python = pkgs.python3Packages.callPackage ./cri-proto-python { };
  nri-proto-python = pkgs.python3Packages.callPackage ./nri-proto-python { };
  ttrpc-proto-python = pkgs.python3Packages.callPackage ./ttrpc-proto-python { };
  python-jsonpath = pkgs.python3Packages.callPackage ./python-jsonpath.nix { };
  kr8s = pkgs.python3Packages.callPackage ./kr8s.nix { inherit (self) python-jsonpath; };
  shellous = pkgs.python3Packages.callPackage ./shellous.nix { };

  # NRI wait Python application for OCI hooks
  # Runs inside chroot(/var/lib/nix-csi), uses pyzmq for communication
  nri-wait = pkgs.python3Packages.callPackage ./nri-wait { };
}
