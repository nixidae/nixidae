/*
  Does a guest give its cold page cache back with nobody asking?
  DAMON_RECLAIM reclaims it and free page reporting returns it.
  krun, the backend it is on by default for; `.qemu` asks the same.
*/
{ mkTest, ... }:
mkTest {
  name = "reclaim";
  backend = "krun";
  phases.test = {
    script = ../reclaim.py;
    after = [ "boot" ];
  };
  nodes.node = {
    vivarium.memory = "1024M";
    vivarium.reclaim = {
      enable = true;
      minAge = 10;
    };
  };
}
