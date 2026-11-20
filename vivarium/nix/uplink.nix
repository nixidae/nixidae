/*
  The uplink's addresses: what the relay and the router give a guest on
  `vec0`.

  Not the host's own address, netmask and router: the host's LAN is then
  on-link to the guest, and a test depends on where the host is. QEMU's user-mode network has used
  10.0.2.0/24 for years, so a reader knows it.

  The one place they are written. modules/router.nix reads them, and
  `machineSpec` in lib.nix hands them to the runner as `uplinkNetwork`.
*/
rec {
  net = "10.0.2";
  prefixLength = 24;
  subnet = "${net}.0/${toString prefixLength}";
  gateway = "${net}.2";
  dns = "${net}.3";
  # A routed guest's host part, by its `vivarium.index`; every other guest
  # takes index 0's.
  host = index: 15 + index;
  address = index: "${net}.${toString (host index)}";

  # A ULA prefix: the egress makes every connection, so the guests'
  # addresses never leave the run. glibc prefers IPv4 to a ULA source.
  prefix6 = "fd00:10:0:2::";
  gateway6 = "${prefix6}2";
  address6 = index: "${prefix6}${toString (host index)}";

  /**
    What the runner reads: `UplinkNetworkJson` in vivarium_runner/forward.py.
  */
  spec = {
    address = address 0;
    inherit
      prefixLength
      gateway
      dns
      gateway6
      ;
  };
}
