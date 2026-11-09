# How much does a segment between two guests actually carry?
{ mkTest, iperfNodes, ... }:
mkTest {
  name = "iperf";
  phases.test = {
    script = ../iperf.py;
    after = [ "boot" ];
  };
  nodes = iperfNodes;
}
