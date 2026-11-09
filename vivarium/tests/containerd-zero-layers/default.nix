/*
  containerd and a user-namespaced container from an image with zero
  layers: tests/phases/zero-layers.py. `stock` is nixpkgs' containerd
  and must fail those cases, which Lillecarl/containerd#1 describes;
  `fixed` is the same build with that fix and must run all of them.
  `unit` runs containerd's overlay snapshotter test as root, where it
  does not skip, and checks that it tells the fix from the unfixed
  code.

  When `stock` stops failing, containerd in nixpkgs carries a fix:
  drop the patch.
*/
{
  pkgs,
  lib,
  mkTest,
  ...
}:
let
  fix = ../../pkgs/containerd-userns-zero-layers.patch;
  fixed = pkgs.containerd.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [ fix ];
    # So `containerd --version` shows which guest carries the fix.
    makeFlags = map (f: if lib.hasPrefix "VERSION=" f then "${f}+zero-layers-fix" else f) old.makeFlags;
  });
  node =
    containerd: address:
    { ... }:
    {
      # The module names pkgs.containerd.
      nixpkgs.overlays = lib.optional (containerd != null) (_: _: { inherit containerd; });
      services.vivarium-k8s = {
        enable = true;
        role = "worker";
        runtimes = [ "crun" ];
      };
      vivarium = {
        memory = "1024M";
        diskSize = 2048;
        lan = {
          network = "zero-layers";
          inherit address;
        };
      };
    };
  image =
    name: layers:
    pkgs.runCommand "zero-layers-${name}.tar" { nativeBuildInputs = [ pkgs.python3 ]; } ''
      python3 ${../oci-image.py} $out zero-layers.test/${name}:1 ${lib.escapeShellArgs layers}
    '';
  /*
    `go test -c` of the overlay snapshotter. `control` is the unfixed
    code with the fixed test file, so a test that cannot tell the two
    apart fails the check.
  */
  overlayTests =
    name: patchCode:
    pkgs.runCommandCC "containerd-overlay-tests-${name}"
      {
        nativeBuildInputs = [
          pkgs.go
          pkgs.patch
        ];
      }
      ''
        cp -r ${pkgs.containerd.src} src && chmod -R u+w src && cd src
        cp plugins/snapshots/overlay/overlay.go overlay.go.orig
        patch -p1 < ${fix}
        ${lib.optionalString (!patchCode) "cp overlay.go.orig plugins/snapshots/overlay/overlay.go"}
        export HOME=$TMPDIR GOCACHE=$TMPDIR/cache GOFLAGS=-mod=vendor
        go test -c -o $out ./plugins/snapshots/overlay
      '';
in
mkTest {
  name = "containerd-zero-layers";
  vivarium.recipes.kubernetes.enable = true;
  defaults =
    { settings, ... }:
    {
      vivarium.lateClosure = [
        settings.busybox
        settings.fixedTests
        settings.controlTests
      ]
      ++ lib.attrValues settings.images;
    };
  nodes = {
    stock = node null "10.106.0.1/24";
    fixed = node fixed "10.106.0.2/24";
  };
  settings = {
    fixed = [ "fixed" ];
    busybox = "${pkgs.pkgsStatic.busybox}/bin/busybox";
    images = {
      zero = "${image "zero" [ ]}";
      # One layer and still no /proc: the failure is the layer count.
      one-no-proc = "${image "one-no-proc" [ "etc/,etc/marker" ]}";
      one-proc = "${image "one-proc" [ "proc/,dev/,sys/" ]}";
    };
    fixedTests = "${overlayTests "fixed" true}";
    controlTests = "${overlayTests "control" false}";
  };
  phases = {
    stock = {
      script = ../phases/zero-layers.py;
      after = [ "boot" ];
      nodes = [ "stock" ];
    };
    fixed = {
      script = ../phases/zero-layers.py;
      after = [ "boot" ];
      nodes = [ "fixed" ];
    };
    unit = {
      script = ../phases/overlay-unit.py;
      after = [ "stock" ];
      nodes = [ "stock" ];
    };
  };
}
