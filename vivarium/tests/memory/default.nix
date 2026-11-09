/*
  Does a guest give its memory back?

  Both backends, and the same script: UML reports free pages through
  `madvise(MADV_REMOVE)` and QEMU through virtio-balloon, and a test
  sees one number either way. Issues #12 and #4.
*/
{ mkTest, ... }:
mkTest {
  name = "memory";
  phases.test = {
    script = ../memory.py;
    after = [ "boot" ];
  };
  nodes.node = {
    # Large enough that reading the guest's own closure is page cache
    # and not pressure, which is what lets the test attribute what it
    # frees afterwards.
    vivarium.memory = "1024M";
  };
}
