# The remote store that nixidae's CI builds on and substitutes from: pynixd on
# dynhetz, croshome `hosts/dynhetz/pynixd.nix`. The arguments of ghanix's
# `steps.remoteBuilder`; nix/ci.nix puts that step into every source workflow.
#
# The runner keeps its memory for guests and test drivers. A run by anyone
# else, or with no secret, builds on the runner alone.
{
  uri = "ssh-ng://gha@37.27.129.237:2234";
  # `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAtGLJv5RjzUuOGWnsaTREsnAj6xVppAnegaXWfkJHHl`,
  # /var/lib/pynixd-ssh/ssh_host_ed25519_key.pub on dynhetz.
  hostKey = "c3NoLWVkMjU1MTkgQUFBQUMzTnphQzFsWkRJMU5URTVBQUFBSUF0R0xKdjVSanpVdU9HV25zYVRSRXNuQWo2eFZwcEFuZWdhWFdma0pISGw=";
  secret = "PYNIXD_BUILDER_KEY";
  actors = [ "Lillecarl" ];
  # dynhetz has every feature. `kvm`, `nixos-test` and `uid-range` stay off,
  # because a derivation that asks for one boots a guest (vivarium's
  # `featuresFor`), and guests run on the runner.
  supportedFeatures = [
    "benchmark"
    "big-parallel"
    "recursive-nix"
  ];
}
