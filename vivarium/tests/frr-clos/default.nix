# BGP unnumbered, EVPN L2 and symmetric L3 on a CLOS of FRR guests (#23).
{
  pkgs,
  lib,
  mkTest,
  ...
}:
import ../../examples/frr-clos { inherit mkTest lib pkgs; }
