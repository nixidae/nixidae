# SPDX-License-Identifier: MIT

# Node environment: nixkube is the sole entrypoint — no init system needed.
# All supervision (nix-daemon, GC, CSI, NRI) is handled by nixkube itself.
# Only debug/runtime tools are included alongside nixkube.
{ pkgs, ... }:
pkgs.buildEnv {
  name = "nodeEnv";
  paths = with pkgs; [
    nixkube
    tini
    bash
    coreutils
    fishMinimal
    lruLix
    openssh
    util-linuxMinimal
    gnugrep
    getent
    doggo
    iputils
    curl
  ];
}
