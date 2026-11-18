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
        loop, which costs throughput. Only a hub can be cut or captured
        while the run goes on.
      '';
    };

    instanceAddresses = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "10.9.0.100/24"
        "fd00:9::100/64"
      ];
      description = ''
        The addresses of the first instance a template launches onto this
        segment, with their prefix length. Instance slot n gets each one
        plus n. Empty gives an instance no static address here.
      '';
    };

    router = lib.mkOption {
      type = lib.types.nullOr (
        lib.types.submodule {
          options = {
            address = lib.mkOption {
              type = lib.types.str;
              example = "10.9.0.1/24";
              description = ''
                The router's IPv4 address on this segment, with its prefix
                length. The prefix is the network: the run refuses two
                router networks that overlap, or one that overlaps the
                uplink's 10.0.2.0/24.
              '';
            };
            address6 = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              example = "fd00:9::1/64";
              description = ''
                The router's IPv6 address on this segment, with its prefix
                length. The router advertises the prefix. Null gives the
                network no IPv6 beyond link-local addresses. As with
                `address`, the run refuses an overlap, here with the
                uplink's fd00:10:0:2::/64.
              '';
            };
            dhcp = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = ''
                Serve DHCP on this segment, with the router as the gateway
                and no DNS server. A NixOS guest's interface on the segment
                with no `addresses` leases its address.
              '';
            };
          };
        }
      );
      default = null;
      example = {
        address = "10.9.0.1/24";
      };
      description = ''
        Make the run's router a member of this segment, and its gateway to
        the outside through the egress (nixidae/nixidae#1523). Each such
        segment is a network of its own on the router, in a VRF: no packet
        crosses the router from it to the uplink or to another network.
        The segment's name is at most 11 characters, for the VRF's name.
      '';
    };

    capture = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Write every frame on this segment to `<out>/pcap/<name>.pcap`,
        from boot to the end of the run. It makes the segment a hub. The
        file has no size limit.
      '';
    };
  };
}
