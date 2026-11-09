# Do the guests boot, see each other on vec1, and answer the host?
{ mkTest, pair, ... }:
mkTest {
  name = "lan";
  phases.test = {
    script = ../lan.py;
    after = [ "boot" ];
  };
  nodes = pair "lan";
}
