/*
  The same lab with VyOS in RouterOS's place, under UML. Its
  interfaces are eth1 and eth2, which VyOS requires; vivarium names
  them by MAC and adds their addresses.
*/
{
  vivarium,
  mkTest,
  images,
  lanHost,
  frrPeer,
  ...
}:
mkTest {
  name = "vyos-bgp";
  nodes.r1 = {
    vivarium.interfaces = {
      eth1 = {
        segment = "core";
        addresses = [ "10.0.0.1/30" ];
      };
      eth2 = {
        segment = "west";
        addresses = [ "10.1.0.1/24" ];
      };
    };
    vivarium.memory = "1024M";
    imports = [ vivarium.kinds.vyos ];
    vivarium.vyos = images.vyos-rolling // {
      config = ''
        protocols {
            bgp {
                address-family {
                    ipv4-unicast {
                        network 10.1.0.0/24 {
                        }
                    }
                }
                neighbor 10.0.0.2 {
                    address-family {
                        ipv4-unicast {
                        }
                    }
                    remote-as "65002"
                    timers {
                        holdtime "3"
                        keepalive "1"
                    }
                }
                parameters {
                    router-id "10.0.0.1"
                }
                system-as "65001"
            }
        }
        system {
            host-name "r1"
        }
      '';
    };
  };
  nodes.f1 = frrPeer;
  nodes.a = lanHost "west" "10.1.0.2" "10.1.0.1" "10.2.0.0";
  nodes.b = lanHost "east" "10.2.0.2" "10.2.0.1" "10.1.0.0";
  phases.bgp = {
    script = ../phases/vyos-bgp.py;
    after = [ "boot" ];
  };
}
