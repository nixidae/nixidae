# strongSwan site-to-site with certificates: policy- and route-based
# tunnels, eBGP over them, MTU, rekey, failover and impostors.
{
  pkgs,
  lib,
  mkTest,
  ...
}:
import ../../examples/ipsec { inherit mkTest lib pkgs; }
