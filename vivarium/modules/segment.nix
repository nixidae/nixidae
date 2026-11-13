# One Ethernet segment's own settings: `segments.<name>` of a run, and
# the copy each guest reads as `vivarium.segments`.
lib:
lib.types.submodule {
  options = {
    mtu = lib.mkOption {
      type = lib.types.nullOr (lib.types.ints.between 576 65534);
      default = null;
      example = 1500;
      description = ''
        MTU of every interface on this segment. Null leaves each one at
        its guest's `vivarium.mtu`. A segment some guest's uplink joins
        takes none: the uplink is `vivarium.mtu`.
      '';
    };

    hub = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Pass every frame through the runner, even with two members. Two
        members are otherwise one socketpair, and the runner never sees
        their frames. The hub copies each frame in the runner's event
        loop, which costs throughput.
      '';
    };
  };
}
